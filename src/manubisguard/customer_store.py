"""Durable customer-product storage for one isolated Anubis Bot instance.

Financial values are integer minor units; ledger rows are immutable and idempotent.
This module intentionally contains no Telegram or provider side effects.
"""
from __future__ import annotations

import os
import re
import json
import hashlib
import sqlite3
import uuid
import time
from datetime import datetime, timezone
import ipaddress
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

from manubisguard.backup_policy import BackupPolicyError, validate_backup_policy


DEFAULT_PATH = "/var/lib/anubis-bot/customer.sqlite3"


class CustomerStoreError(ValueError):
    """Raised for invalid customer data or conflicting idempotent operations."""


class CustomerDataStore:
    def __init__(self, db_path: str | None = None, tenant_id: str | None = None):
        self.db_path = db_path or os.environ.get("ANUBIS_CUSTOMER_DB", DEFAULT_PATH)
        self.tenant_id = (tenant_id or os.environ.get("ANUBIS_CUSTOMER_TENANT", "default")).strip()
        if not self.tenant_id or len(self.tenant_id) > 120:
            raise CustomerStoreError("A valid tenant ID is required")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        # Financial/customer writes favor durability over a small fsync reduction.
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _enqueue_trust_event_in_transaction(self, db: sqlite3.Connection, *, event_type: str,
                                            idempotency_key: str, payload: dict[str, Any]) -> None:
        """Insert a privacy-filtered outbox row in the same SQLite business transaction."""
        outbox_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_outbox'").fetchone()
        if not outbox_exists:
            return
        bot_instance = os.environ.get("ANUBIS_CUSTOMER_BOT_INSTANCE", "customer").strip() or "customer"
        config = db.execute("SELECT enabled,chat_id,enabled_events_json FROM trust_channel_settings WHERE tenant_id=? AND bot_instance_id=?", (self.tenant_id, bot_instance)).fetchone()
        if not config or not config["enabled"]:
            return
        try: enabled_events = json.loads(config["enabled_events_json"] or "[]")
        except (TypeError, ValueError): return
        if event_type not in enabled_events:
            return
        safe = {key: payload[key] for key in ("public_order_id", "amount_minor", "currency", "volume_gb", "days") if key in payload}
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        db.execute(
            """INSERT OR IGNORE INTO notification_outbox
               (tenant_id,bot_instance_id,event_type,idempotency_key,payload_json,status,attempts,available_at,created_at)
               VALUES(?,?,?,?,?,'pending',0,?,?)""",
            (self.tenant_id, bot_instance, event_type, idempotency_key[:200], encoded, now, now),
        )

    def _initialize(self) -> None:
        with self._connect() as db:
            # WAL supports readers alongside a writer on this same host. Do not
            # place this database on a network filesystem; see SQLite WAL docs.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS customer_users (
                    tenant_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'fa',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','blocked')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, telegram_id)
                );
                CREATE TABLE IF NOT EXISTS customer_categories (
                    tenant_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, category_id)
                );
                CREATE TABLE IF NOT EXISTS customer_products (
                    tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    category_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price_minor INTEGER NOT NULL CHECK(price_minor >= 0),
                    currency TEXT NOT NULL CHECK(length(currency) = 3),
                    duration_hours INTEGER NOT NULL DEFAULT 0 CHECK(duration_hours >= 0),
                    traffic_bytes INTEGER NOT NULL DEFAULT 0 CHECK(traffic_bytes >= 0),
                    pricing_mode TEXT NOT NULL DEFAULT 'fixed' CHECK(pricing_mode IN ('fixed','unlimited','hourly','usage')),
                    unit_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(unit_price_minor >= 0),
                    panel_id TEXT NOT NULL DEFAULT '',
                    group_ids_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS wallet_ledger (
                    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor != 0),
                    currency TEXT NOT NULL CHECK(length(currency) = 3),
                    entry_type TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, idempotency_key),
                    FOREIGN KEY(tenant_id, telegram_id)
                        REFERENCES customer_users(tenant_id, telegram_id)
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_user
                    ON wallet_ledger(tenant_id, telegram_id, ledger_id);
                CREATE TRIGGER IF NOT EXISTS wallet_ledger_no_update
                BEFORE UPDATE ON wallet_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'wallet ledger is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS wallet_ledger_no_delete
                BEFORE DELETE ON wallet_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'wallet ledger is append-only');
                END;
                CREATE TABLE IF NOT EXISTS manual_deposit_settings (
                    tenant_id TEXT PRIMARY KEY,
                    card_number TEXT NOT NULL,
                    account_holder TEXT NOT NULL,
                    bank_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS manual_deposit_receipts (
                    tenant_id TEXT NOT NULL,
                    payment_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    transfer_reference TEXT NOT NULL,
                    payer_name TEXT NOT NULL DEFAULT '',
                    receipt_file_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','rejected')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,payment_id),
                    UNIQUE(tenant_id,transfer_reference),
                    FOREIGN KEY(tenant_id,payment_id) REFERENCES payment_attempts(tenant_id,payment_id),
                    FOREIGN KEY(tenant_id,telegram_id) REFERENCES customer_users(tenant_id,telegram_id)
                );
                CREATE INDEX IF NOT EXISTS idx_manual_deposit_receipts_queue
                    ON manual_deposit_receipts(tenant_id,status,created_at);
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    tenant_id TEXT NOT NULL,
                    payment_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL CHECK(length(currency) = 3),
                    status TEXT NOT NULL CHECK(status IN ('pending','verified','failed','expired','refunded')),
                    idempotency_key TEXT NOT NULL,
                    external_reference TEXT,
                    failure_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, payment_id),
                    UNIQUE(tenant_id, idempotency_key),
                    UNIQUE(tenant_id, provider, external_reference),
                    FOREIGN KEY(tenant_id, telegram_id)
                        REFERENCES customer_users(tenant_id, telegram_id)
                );
                CREATE TABLE IF NOT EXISTS payment_attempt_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    payment_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('created','verified','failed')),
                    actor_telegram_id INTEGER,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL CHECK(length(currency) = 3),
                    external_reference TEXT NOT NULL DEFAULT '',
                    failure_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(tenant_id,payment_id) REFERENCES payment_attempts(tenant_id,payment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_payment_attempt_events
                    ON payment_attempt_events(tenant_id,payment_id,event_id);
                CREATE TRIGGER IF NOT EXISTS payment_attempt_events_no_update
                BEFORE UPDATE ON payment_attempt_events
                BEGIN SELECT RAISE(ABORT, 'payment attempt events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS payment_attempt_events_no_delete
                BEFORE DELETE ON payment_attempt_events
                BEGIN SELECT RAISE(ABORT, 'payment attempt events are append-only'); END;
                CREATE TABLE IF NOT EXISTS customer_orders (
                    tenant_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
                    currency TEXT NOT NULL CHECK(length(currency) = 3),
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','paid','provisioning','active','failed','refunded','cancelled')),
                    idempotency_key TEXT NOT NULL,
                    provider_reference TEXT NOT NULL DEFAULT '',
                    product_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    reseller_account_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, order_id),
                    UNIQUE(tenant_id, idempotency_key),
                    FOREIGN KEY(tenant_id, telegram_id)
                        REFERENCES customer_users(tenant_id, telegram_id)
                );
                CREATE TABLE IF NOT EXISTS customer_services (
                    tenant_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL DEFAULT '',
                    panel_id TEXT NOT NULL DEFAULT '',
                    provider_user_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    subscription_url TEXT NOT NULL DEFAULT '',
                    reseller_account_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','active','limited','expired','suspended','failed','deleted')),
                    expires_at TEXT,
                    traffic_limit_bytes INTEGER NOT NULL DEFAULT 0 CHECK(traffic_limit_bytes >= 0),
                    traffic_used_bytes INTEGER NOT NULL DEFAULT 0 CHECK(traffic_used_bytes >= 0),
                    pricing_mode TEXT NOT NULL DEFAULT 'fixed' CHECK(pricing_mode IN ('fixed','unlimited','hourly','usage')),
                    currency TEXT NOT NULL DEFAULT 'IRR',
                    unit_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(unit_price_minor >= 0),
                    billing_last_at INTEGER NOT NULL DEFAULT 0,
                    billed_traffic_bytes INTEGER NOT NULL DEFAULT 0 CHECK(billed_traffic_bytes >= 0),
                    fractional_carry TEXT NOT NULL DEFAULT '0',
                    outstanding_debt_minor INTEGER NOT NULL DEFAULT 0 CHECK(outstanding_debt_minor >= 0),
                    billing_status TEXT NOT NULL DEFAULT 'not_metered' CHECK(billing_status IN ('not_metered','active','suspension_pending','suspended_balance','suspended_admin','reconcile_required')),
                    provider_checked_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, service_id),
                    FOREIGN KEY(tenant_id, telegram_id)
                        REFERENCES customer_users(tenant_id, telegram_id)
                );
                CREATE INDEX IF NOT EXISTS idx_customer_services_owner
                    ON customer_services(tenant_id, telegram_id, status);
                CREATE TABLE IF NOT EXISTS customer_tickets (
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','waiting_customer','waiting_staff','closed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, ticket_id),
                    FOREIGN KEY(tenant_id, telegram_id)
                        REFERENCES customer_users(tenant_id, telegram_id)
                );
                CREATE TABLE IF NOT EXISTS customer_ticket_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    sender_type TEXT NOT NULL CHECK(sender_type IN ('customer','staff','system')),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(tenant_id, ticket_id)
                        REFERENCES customer_tickets(tenant_id, ticket_id)
                );
                CREATE TABLE IF NOT EXISTS reseller_plans (
                    tenant_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    panel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    pricing_mode TEXT NOT NULL CHECK(pricing_mode IN ('fixed','unlimited','hourly','usage','per_gb','per_tb')),
                    price_minor INTEGER NOT NULL DEFAULT 0 CHECK(price_minor >= 0),
                    unit_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(unit_price_minor >= 0),
                    currency TEXT NOT NULL CHECK(length(currency)=3),
                    duration_hours INTEGER NOT NULL DEFAULT 0 CHECK(duration_hours >= 0),
                    data_limit_bytes INTEGER NOT NULL DEFAULT 0 CHECK(data_limit_bytes >= 0),
                    max_users INTEGER NOT NULL DEFAULT 0 CHECK(max_users >= 0),
                    min_volume_gib TEXT NOT NULL DEFAULT '0',
                    max_volume_gib TEXT NOT NULL DEFAULT '0',
                    volume_step_gib TEXT NOT NULL DEFAULT '1',
                    provider_role_id TEXT NOT NULL DEFAULT '',
                    allowed_group_ids_json TEXT NOT NULL DEFAULT '[]',
                    permission_overrides_json TEXT NOT NULL DEFAULT '{}',
                    addon_hour_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(addon_hour_price_minor >= 0),
                    addon_gib_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(addon_gib_price_minor >= 0),
                    addon_user_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(addon_user_price_minor >= 0),
                    display_label TEXT NOT NULL DEFAULT '',
                    display_icon TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,plan_id),
                    FOREIGN KEY(tenant_id,panel_id) REFERENCES panel_connections(tenant_id,panel_id)
                );
                CREATE TABLE IF NOT EXISTS reseller_accounts (
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    owner_telegram_id INTEGER NOT NULL,
                    panel_id TEXT NOT NULL,
                    provider_account_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    pricing_mode TEXT NOT NULL CHECK(pricing_mode IN ('fixed','unlimited','hourly','usage','per_gb','per_tb')),
                    status TEXT NOT NULL CHECK(status IN ('pending','active','paused_by_owner','paused_by_admin','suspended_balance','expired','failed','reconcile_required','deleted')),
                    currency TEXT NOT NULL CHECK(length(currency)=3),
                    unit_price_minor INTEGER NOT NULL DEFAULT 0 CHECK(unit_price_minor >= 0),
                    setup_fee_minor INTEGER NOT NULL DEFAULT 0 CHECK(setup_fee_minor >= 0),
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    purchased_volume_gib TEXT NOT NULL DEFAULT '0',
                    max_users INTEGER NOT NULL DEFAULT 0 CHECK(max_users >= 0),
                    expires_at TEXT,
                    traffic_cap_bytes INTEGER NOT NULL DEFAULT 0 CHECK(traffic_cap_bytes >= 0),
                    used_traffic_bytes INTEGER NOT NULL DEFAULT 0 CHECK(used_traffic_bytes >= 0),
                    billed_traffic_bytes INTEGER NOT NULL DEFAULT 0 CHECK(billed_traffic_bytes >= 0),
                    last_billed_at INTEGER NOT NULL DEFAULT 0,
                    fractional_carry TEXT NOT NULL DEFAULT '0',
                    usage_cap_bytes INTEGER NOT NULL DEFAULT 0 CHECK(usage_cap_bytes >= 0),
                    total_billed_minor INTEGER NOT NULL DEFAULT 0 CHECK(total_billed_minor >= 0),
                    outstanding_debt_minor INTEGER NOT NULL DEFAULT 0 CHECK(outstanding_debt_minor >= 0),
                    provider_suspended INTEGER NOT NULL DEFAULT 0 CHECK(provider_suspended IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,account_id),
                    UNIQUE(tenant_id,panel_id,username),
                    FOREIGN KEY(tenant_id,owner_telegram_id) REFERENCES customer_users(tenant_id,telegram_id),
                    FOREIGN KEY(tenant_id,plan_id) REFERENCES reseller_plans(tenant_id,plan_id)
                );
                CREATE INDEX IF NOT EXISTS idx_reseller_owner ON reseller_accounts(tenant_id,owner_telegram_id,status);
                CREATE TABLE IF NOT EXISTS reseller_credentials (
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    encrypted_password TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,account_id),
                    FOREIGN KEY(tenant_id,account_id) REFERENCES reseller_accounts(tenant_id,account_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reseller_service_usage_counters (
                    tenant_id TEXT NOT NULL,
                    reseller_account_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    last_seen_bytes INTEGER NOT NULL DEFAULT 0 CHECK(last_seen_bytes >= 0),
                    cumulative_bytes INTEGER NOT NULL DEFAULT 0 CHECK(cumulative_bytes >= 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,reseller_account_id,service_id),
                    FOREIGN KEY(tenant_id,reseller_account_id) REFERENCES reseller_accounts(tenant_id,account_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reseller_billing_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
                    currency TEXT NOT NULL CHECK(length(currency)=3),
                    event_type TEXT NOT NULL CHECK(event_type IN ('hourly','usage','setup','refund','addon')),
                    delta_bytes INTEGER NOT NULL DEFAULT 0 CHECK(delta_bytes >= 0),
                    start_at INTEGER,
                    end_at INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id,idempotency_key),
                    FOREIGN KEY(tenant_id,account_id) REFERENCES reseller_accounts(tenant_id,account_id)
                );
                CREATE TABLE IF NOT EXISTS trust_channel_settings (
                    tenant_id TEXT NOT NULL,
                    bot_instance_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    enabled_events_json TEXT NOT NULL DEFAULT '[]',
                    privacy_mode INTEGER NOT NULL DEFAULT 1 CHECK(privacy_mode IN (0,1)),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,bot_instance_id)
                );
                CREATE TABLE IF NOT EXISTS panel_connections (
                    tenant_id TEXT NOT NULL,
                    panel_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    auth_mode TEXT NOT NULL,
                    secret_ref TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    capability_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    last_check_at TEXT,
                    last_check_status TEXT NOT NULL DEFAULT 'not_tested',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, panel_id)
                );
            """)
            db.execute("""CREATE TABLE IF NOT EXISTS discount_codes (
                tenant_id TEXT NOT NULL,
                code TEXT NOT NULL,
                percentage INTEGER NOT NULL CHECK(percentage BETWEEN 1 AND 100),
                max_uses INTEGER NOT NULL DEFAULT 0 CHECK(max_uses >= 0),
                used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
                expires_at INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                allowed_product_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(tenant_id, code)
            )""")
            # Versioned, transactional migrations cover the core customer schema,
            # service-change audit records, and manual-shop tables. Other feature
            # schema helpers may still require separate centralization.
            db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY CHECK(version > 0),
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            db.execute("BEGIN IMMEDIATE")
            try:
                applied = {
                    int(row["version"])
                    for row in db.execute("SELECT version FROM schema_migrations").fetchall()
                }
                unknown_versions = sorted(version for version in applied if version > 18)
                if unknown_versions:
                    raise CustomerStoreError(
                        "Database schema is newer than this application supports"
                    )

                # v1 consolidates the additive migrations historically applied here.
                # Each DDL/DML operation and the version record commit atomically.
                if 1 not in applied:
                    migrations = {
                        "panel_connections": {
                            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                        },
                        "reseller_accounts": {
                            "setup_fee_minor": "INTEGER NOT NULL DEFAULT 0",
                            "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                            "purchased_volume_gib": "TEXT NOT NULL DEFAULT '0'",
                            "purchased_volume_gb": "TEXT NOT NULL DEFAULT '0'",
                            "max_users": "INTEGER NOT NULL DEFAULT 0",
                            "provider_suspended": "INTEGER NOT NULL DEFAULT 0",
                            "outstanding_debt_minor": "INTEGER NOT NULL DEFAULT 0",
                        },
                        "customer_products": {
                            "category_id": "TEXT NOT NULL DEFAULT ''",
                            "pricing_mode": "TEXT NOT NULL DEFAULT 'fixed'",
                            "unit_price_minor": "INTEGER NOT NULL DEFAULT 0",
                            "panel_id": "TEXT NOT NULL DEFAULT ''",
                            "group_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                        },
                        "customer_orders": {
                            "product_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                            "reseller_account_id": "TEXT NOT NULL DEFAULT ''",
                            "discount_code": "TEXT NOT NULL DEFAULT ''",
                            "discount_amount_minor": "INTEGER NOT NULL DEFAULT 0",
                        },
                        "customer_services": {
                            "outstanding_debt_minor": "INTEGER NOT NULL DEFAULT 0",
                            "reseller_account_id": "TEXT NOT NULL DEFAULT ''",
                            "username": "TEXT NOT NULL DEFAULT ''",
                            "subscription_url": "TEXT NOT NULL DEFAULT ''",
                            "pricing_mode": "TEXT NOT NULL DEFAULT 'fixed'",
                            "currency": "TEXT NOT NULL DEFAULT 'IRR'",
                            "unit_price_minor": "INTEGER NOT NULL DEFAULT 0",
                            "billing_last_at": "INTEGER NOT NULL DEFAULT 0",
                            "billed_traffic_bytes": "INTEGER NOT NULL DEFAULT 0",
                            "fractional_carry": "TEXT NOT NULL DEFAULT '0'",
                            "billing_status": "TEXT NOT NULL DEFAULT 'not_metered'",
                        },
                    }
                    for table, columns in migrations.items():
                        existing = {
                            row["name"]
                            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                        }
                        for column, declaration in columns.items():
                            if column not in existing:
                                db.execute(
                                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                                )
                    db.execute(
                        "UPDATE reseller_accounts SET idempotency_key='legacy:' || account_id "
                        "WHERE idempotency_key='' OR idempotency_key IS NULL"
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reseller_idempotency "
                        "ON reseller_accounts(tenant_id,idempotency_key)"
                    )
                    receipt_columns = {
                        row["name"]
                        for row in db.execute("PRAGMA table_info(manual_deposit_receipts)").fetchall()
                    }
                    if "receipt_file_id" not in receipt_columns:
                        db.execute(
                            "ALTER TABLE manual_deposit_receipts "
                            "ADD COLUMN receipt_file_id TEXT NOT NULL DEFAULT ''"
                        )
                    db.execute(
                        "INSERT INTO schema_migrations(version,name) VALUES(1,?)",
                        ("core_additive_legacy_migrations",),
                    )
                    applied.add(1)

                # v2 centralizes the service-change operation and append-only audit schemas.
                if 2 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_service_operations (
                        tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, telegram_id INTEGER NOT NULL,
                        service_id TEXT NOT NULL, change_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                        amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, quantity INTEGER NOT NULL,
                        status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        before_expires_at TEXT, before_traffic_limit_bytes INTEGER,
                        target_expires_at TEXT, target_traffic_limit_bytes INTEGER,
                        PRIMARY KEY(tenant_id,operation_id), UNIQUE(tenant_id,idempotency_key)
                    )""")
                    operation_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(customer_service_operations)").fetchall()}
                    for column, declaration in (("before_expires_at", "TEXT"),
                                                ("before_traffic_limit_bytes", "INTEGER"),
                                                ("target_expires_at", "TEXT"),
                                                ("target_traffic_limit_bytes", "INTEGER")):
                        if column not in operation_columns:
                            db.execute(f"ALTER TABLE customer_service_operations ADD COLUMN {column} {declaration}")
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_service_operation_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                        event_type TEXT NOT NULL CHECK(event_type='operator_finalized_target_present'),
                        actor_telegram_id INTEGER NOT NULL, comparison TEXT NOT NULL,
                        observed_value TEXT NOT NULL, reason TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(tenant_id,operation_id),
                        FOREIGN KEY(tenant_id,operation_id) REFERENCES customer_service_operations(tenant_id,operation_id)
                    )""")
                    db.execute("""CREATE TRIGGER IF NOT EXISTS customer_service_operation_events_no_update
                        BEFORE UPDATE ON customer_service_operation_events
                        BEGIN SELECT RAISE(ABORT, 'service operation events are append-only'); END""")
                    db.execute("""CREATE TRIGGER IF NOT EXISTS customer_service_operation_events_no_delete
                        BEFORE DELETE ON customer_service_operation_events
                        BEGIN SELECT RAISE(ABORT, 'service operation events are append-only'); END""")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(2,?)",
                               ("service_change_operations_and_audit",))
                # Re-assert immutable audit triggers on every open, including databases
                # that recorded migration v2 before these triggers were introduced.
                db.execute("""CREATE TRIGGER IF NOT EXISTS customer_service_operation_events_no_update
                    BEFORE UPDATE ON customer_service_operation_events
                    BEGIN SELECT RAISE(ABORT, 'service operation events are append-only'); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS customer_service_operation_events_no_delete
                    BEFORE DELETE ON customer_service_operation_events
                    BEGIN SELECT RAISE(ABORT, 'service operation events are append-only'); END""")
                # v3 centralizes the manual-shop schema previously created lazily.
                if 3 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_products (
                        tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '', price_minor INTEGER NOT NULL CHECK(price_minor >= 0),
                        currency TEXT NOT NULL CHECK(length(currency)=3),
                        delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('instant','manual','inventory')),
                        delivery_payload TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(tenant_id,product_id)
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_product_inventory (
                        inventory_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, secret_payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','delivered')), order_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(tenant_id,product_id,secret_payload)
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_product_orders (
                        tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, telegram_id INTEGER NOT NULL,
                        product_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('paid','fulfilled','pending_delivery','failed')),
                        idempotency_key TEXT NOT NULL, delivery_payload TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(tenant_id,order_id), UNIQUE(tenant_id,idempotency_key)
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_product_order_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL,
                        event_type TEXT NOT NULL, actor_telegram_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(3,?)",
                               ("manual_shop_tables",))
                db.execute("""CREATE TRIGGER IF NOT EXISTS manual_product_order_events_no_update
                    BEFORE UPDATE ON manual_product_order_events
                    BEGIN SELECT RAISE(ABORT, 'manual product order events are append-only'); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS manual_product_order_events_no_delete
                    BEFORE DELETE ON manual_product_order_events
                    BEGIN SELECT RAISE(ABORT, 'manual product order events are append-only'); END""")
                # v4 snapshots immutable product title and delivery mode onto each order.
                # Old rows are backfilled from the current product when it still exists;
                # if it does not, use the product ID and fail-safe manual delivery mode.
                if 4 not in applied:
                    order_columns = {
                        str(row["name"])
                        for row in db.execute("PRAGMA table_info(manual_product_orders)").fetchall()
                    }
                    for column, declaration in (
                        ("product_title_snapshot", "TEXT NOT NULL DEFAULT ''"),
                        ("delivery_mode_snapshot", "TEXT NOT NULL DEFAULT ''"),
                    ):
                        if column not in order_columns:
                            db.execute(
                                f"ALTER TABLE manual_product_orders ADD COLUMN {column} {declaration}"
                            )
                    db.execute("""UPDATE manual_product_orders
                        SET product_title_snapshot=COALESCE(
                            (SELECT title FROM manual_products
                             WHERE manual_products.tenant_id=manual_product_orders.tenant_id
                               AND manual_products.product_id=manual_product_orders.product_id),
                            product_id)
                        WHERE product_title_snapshot=''""")
                    db.execute("""UPDATE manual_product_orders
                        SET delivery_mode_snapshot=COALESCE(
                            (SELECT delivery_mode FROM manual_products
                             WHERE manual_products.tenant_id=manual_product_orders.tenant_id
                               AND manual_products.product_id=manual_product_orders.product_id),
                            'manual')
                        WHERE delivery_mode_snapshot=''""")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(4,?)",
                               ("manual_order_purchase_snapshot",))
                    applied.add(4)
                # v5 stores customer delivery notifications independently of the
                # privacy-filtered trust-channel outbox. It references the order so
                # delivery secrets are not duplicated in a second payload column.
                if 5 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_delivery_notifications (
                        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','processing','sent','dead')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        available_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        claimed_at REAL,
                        sent_at REAL,
                        last_error_code TEXT,
                        UNIQUE(tenant_id,order_id),
                        FOREIGN KEY(tenant_id,order_id)
                            REFERENCES manual_product_orders(tenant_id,order_id)
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_manual_delivery_notifications_due "
                               "ON manual_delivery_notifications(status,available_at,notification_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(5,?)",
                               ("manual_delivery_notification_outbox",))
                    applied.add(5)
                # v6 persists support-ticket admin notifications so Mini App tickets
                # are delivered by the Telegram bot worker after the DB transaction.
                if 6 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_ticket_notifications (
                        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','processing','sent','dead')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        available_at REAL NOT NULL, created_at REAL NOT NULL,
                        claimed_at REAL, sent_at REAL, last_error_code TEXT,
                        UNIQUE(tenant_id,ticket_id),
                        FOREIGN KEY(tenant_id,ticket_id)
                            REFERENCES customer_tickets(tenant_id,ticket_id)
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_customer_ticket_notifications_due "
                               "ON customer_ticket_notifications(status,available_at,notification_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(6,?)",
                               ("customer_ticket_notification_outbox",))
                    applied.add(6)
                # v7 defines per-event Telegram destinations, optionally pinned to a forum topic.
                if 7 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_notification_routes (
                        route_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL,
                        event_key TEXT NOT NULL CHECK(event_key IN ('support_ticket','purchase','payment','service_test','bot_log','backup','wallet','service_change','security_alert')),
                        chat_id INTEGER NOT NULL CHECK(chat_id != 0),
                        topic_id INTEGER NOT NULL DEFAULT 0 CHECK(topic_id >= 0),
                        created_by INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(tenant_id,event_key,chat_id,topic_id)
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_customer_notification_routes_event "
                               "ON customer_notification_routes(tenant_id,event_key,route_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(7,?)",
                               ("per_event_telegram_notification_routes",))
                    applied.add(7)
                # v8 stores a disabled-by-default backup policy. This migration
                # never creates a scheduler, timer, or operational backup.
                if 8 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_backup_policies (
                        tenant_id TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                        frequency TEXT NOT NULL DEFAULT 'daily' CHECK(frequency IN ('hourly','daily','interval')),
                        timezone_name TEXT NOT NULL DEFAULT 'UTC',
                        local_time TEXT NOT NULL DEFAULT '03:00',
                        interval_minutes INTEGER NOT NULL DEFAULT 1440 CHECK(interval_minutes BETWEEN 15 AND 10080),
                        destination TEXT NOT NULL DEFAULT 'local' CHECK(destination IN ('local','telegram','both')),
                        local_directory TEXT NOT NULL DEFAULT '/var/lib/anubis-bot/backups',
                        telegram_chat_id INTEGER,
                        telegram_topic_id INTEGER NOT NULL DEFAULT 0 CHECK(telegram_topic_id >= 0),
                        retention_count INTEGER NOT NULL DEFAULT 14 CHECK(retention_count BETWEEN 1 AND 1000),
                        retention_days INTEGER NOT NULL DEFAULT 30 CHECK(retention_days BETWEEN 1 AND 3650),
                        last_run_at REAL,
                        last_status TEXT NOT NULL DEFAULT 'never' CHECK(last_status IN ('never','success','failed','running')),
                        last_error_code TEXT,
                        next_run_at REAL,
                        updated_by INTEGER,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(8,?)",
                               ("tenant_backup_policy_disabled_by_default",))
                    applied.add(8)
                # v9 introduces a shared, durable admin-event outbox. Each destination
                # has its own delivery state so a retry never repeats successful routes.
                if 9 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_admin_notifications (
                        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL,
                        event_key TEXT NOT NULL CHECK(event_key IN
                            ('purchase','payment','service_test','bot_log','backup','wallet','service_change','security_alert')),
                        idempotency_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','sent','dead')),
                        available_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        sent_at REAL,
                        last_error_code TEXT,
                        UNIQUE(tenant_id,event_key,idempotency_key)
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_admin_notification_deliveries (
                        delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        notification_id INTEGER NOT NULL,
                        tenant_id TEXT NOT NULL,
                        chat_id INTEGER NOT NULL CHECK(chat_id != 0),
                        topic_id INTEGER NOT NULL DEFAULT 0 CHECK(topic_id >= 0),
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','processing','sent','dead')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        available_at REAL NOT NULL,
                        claimed_at REAL,
                        sent_at REAL,
                        last_error_code TEXT,
                        UNIQUE(notification_id,chat_id,topic_id),
                        FOREIGN KEY(notification_id) REFERENCES customer_admin_notifications(notification_id)
                            ON DELETE CASCADE
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_customer_admin_notifications_due "
                               "ON customer_admin_notifications(tenant_id,status,available_at,notification_id)")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_customer_admin_notification_deliveries_due "
                               "ON customer_admin_notification_deliveries(tenant_id,status,available_at,delivery_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(9,?)",
                               ("durable_per_destination_admin_notification_outbox",))
                    applied.add(9)
                # v10 adds optional category binding to the manual catalog without
                # changing existing products; legacy rows remain uncategorized.
                if 10 not in applied:
                    manual_columns = {
                        str(row["name"])
                        for row in db.execute("PRAGMA table_info(manual_products)").fetchall()
                    }
                    if "category_id" not in manual_columns:
                        db.execute("ALTER TABLE manual_products ADD COLUMN category_id TEXT NOT NULL DEFAULT ''")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_manual_products_tenant_category_enabled "
                               "ON manual_products(tenant_id,category_id,enabled,product_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(10,?)",
                               ("manual_product_category_binding",))
                    applied.add(10)
                # v11 adds an optional lifetime per-customer purchase cap for manual
                # products; 0 preserves the existing unlimited behavior.
                if 11 not in applied:
                    manual_columns = {
                        str(row["name"])
                        for row in db.execute("PRAGMA table_info(manual_products)").fetchall()
                    }
                    if "max_purchases_per_customer" not in manual_columns:
                        db.execute(
                            "ALTER TABLE manual_products ADD COLUMN max_purchases_per_customer "
                            "INTEGER NOT NULL DEFAULT 0 CHECK(max_purchases_per_customer >= 0)"
                        )
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(11,?)",
                               ("manual_product_customer_purchase_limit",))
                    applied.add(11)
                # v12 adds an explicit catalog sort key; legacy products keep the
                # neutral value and deterministic created_at/product_id tie-breaks.
                if 12 not in applied:
                    manual_columns = {
                        str(row["name"])
                        for row in db.execute("PRAGMA table_info(manual_products)").fetchall()
                    }
                    if "sort_order" not in manual_columns:
                        db.execute(
                            "ALTER TABLE manual_products ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                        )
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(12,?)",
                               ("manual_product_sort_order",))
                    applied.add(12)
                # v13 stores editable product terms and an immutable per-order snapshot.
                if 13 not in applied:
                    product_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_products)").fetchall()}
                    order_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_product_orders)").fetchall()}
                    if "terms_text" not in product_columns:
                        db.execute("ALTER TABLE manual_products ADD COLUMN terms_text TEXT NOT NULL DEFAULT ''")
                    if "terms_snapshot" not in order_columns:
                        db.execute("ALTER TABLE manual_product_orders ADD COLUMN terms_snapshot TEXT NOT NULL DEFAULT ''")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(13,?)", ("manual_product_terms_snapshot",))
                    applied.add(13)
                # v14 adds an optional public HTTPS image URL for manual product cards.
                if 14 not in applied:
                    product_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_products)").fetchall()}
                    if "image_url" not in product_columns:
                        db.execute("ALTER TABLE manual_products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(14,?)", ("manual_product_image_url",))
                    applied.add(14)
                # v15 adds optional UTC visibility bounds; empty bounds mean no schedule.
                if 15 not in applied:
                    product_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_products)").fetchall()}
                    for column in ("visible_from_utc", "visible_until_utc"):
                        if column not in product_columns:
                            db.execute(f"ALTER TABLE manual_products ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(15,?)", ("manual_product_visibility_schedule",))
                    applied.add(15)
                # v16 adds tenant-scoped product variants and immutable order snapshots.
                if 16 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS manual_product_variants (
                        tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, variant_id TEXT NOT NULL,
                        title TEXT NOT NULL, price_minor INTEGER NOT NULL CHECK(price_minor > 0),
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(tenant_id,product_id,variant_id),
                        FOREIGN KEY(tenant_id,product_id) REFERENCES manual_products(tenant_id,product_id)
                    )""")
                    inventory_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_product_inventory)").fetchall()}
                    if "variant_id" not in inventory_columns:
                        db.execute("ALTER TABLE manual_product_inventory ADD COLUMN variant_id TEXT NOT NULL DEFAULT ''")
                    order_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_product_orders)").fetchall()}
                    for column, declaration in (("variant_id_snapshot", "TEXT NOT NULL DEFAULT ''"),
                                                ("variant_title_snapshot", "TEXT NOT NULL DEFAULT ''")):
                        if column not in order_columns:
                            db.execute(f"ALTER TABLE manual_product_orders ADD COLUMN {column} {declaration}")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_manual_variants_catalog ON manual_product_variants(tenant_id,product_id,enabled,variant_id)")
                    db.execute("CREATE INDEX IF NOT EXISTS ix_manual_inventory_variant ON manual_product_inventory(tenant_id,product_id,variant_id,status,inventory_id)")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(16,?)", ("manual_product_variants",))
                    applied.add(16)
                # v17 adds optional per-variant delivery overrides. NULL inherits the parent.
                if 17 not in applied:
                    variant_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(manual_product_variants)").fetchall()}
                    if "delivery_mode" not in variant_columns:
                        db.execute("ALTER TABLE manual_product_variants ADD COLUMN delivery_mode TEXT")
                    if "delivery_payload" not in variant_columns:
                        db.execute("ALTER TABLE manual_product_variants ADD COLUMN delivery_payload TEXT")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(17,?)", ("manual_variant_delivery_overrides",))
                    applied.add(17)
                # v18 records immutable operator decisions for ambiguous initial product provisioning.
                if 18 not in applied:
                    db.execute("""CREATE TABLE IF NOT EXISTS customer_product_order_reconciliation_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenant_id TEXT NOT NULL, order_id TEXT NOT NULL,
                        event_type TEXT NOT NULL CHECK(event_type='operator_finalized_target_present'),
                        actor_telegram_id INTEGER NOT NULL,
                        provider_kind TEXT NOT NULL, provider_user_id TEXT NOT NULL, username TEXT NOT NULL,
                        comparison TEXT NOT NULL CHECK(comparison='target_present'),
                        evidence_json TEXT NOT NULL, reason TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(tenant_id,order_id),
                        FOREIGN KEY(tenant_id,order_id) REFERENCES customer_orders(tenant_id,order_id)
                    )""")
                    db.execute("INSERT INTO schema_migrations(version,name) VALUES(18,?)",
                               ("product_order_reconciliation_audit",))
                    applied.add(18)
                # Re-assert immutable audit triggers if an older database already has v18 recorded.
                db.execute("""CREATE TRIGGER IF NOT EXISTS customer_product_order_reconciliation_events_no_update
                    BEFORE UPDATE ON customer_product_order_reconciliation_events
                    BEGIN SELECT RAISE(ABORT, 'product order reconciliation events are append-only'); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS customer_product_order_reconciliation_events_no_delete
                    BEFORE DELETE ON customer_product_order_reconciliation_events
                    BEGIN SELECT RAISE(ABORT, 'product order reconciliation events are append-only'); END""")
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _ensure_manual_shop_tables(self) -> None:
        """Compatibility shim; manual-shop tables are installed by migration v3."""
        return

    def put_manual_product(self, *, product_id: str, title: str, price_minor: int,
                           currency: str = "IRR", delivery_mode: str = "instant",
                           delivery_payload: str = "", description: str = "", enabled: bool = True,
                           category_id: str = "", max_purchases_per_customer: int | None = None,
                           sort_order: int | None = None, terms_text: str = "",
                           image_url: str | None = None, visible_from_utc: str | None = None,
                           visible_until_utc: str | None = None) -> None:
        self._ensure_manual_shop_tables()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id.strip()) or not title.strip():
            raise CustomerStoreError("Manual product ID and title are required; ID must use ASCII letters, digits, _ or -")
        category_id = category_id.strip()
        if category_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", category_id):
            raise CustomerStoreError("Invalid manual product category ID")
        if isinstance(price_minor, bool) or not isinstance(price_minor, int) or price_minor <= 0 or price_minor > 2**63-1:
            raise CustomerStoreError("Manual product price must be a positive integer")
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        if delivery_mode not in {"instant", "manual", "inventory"}:
            raise CustomerStoreError("Delivery mode must be instant, inventory or manual")
        if delivery_mode == "instant" and not delivery_payload.strip():
            raise CustomerStoreError("Instant text delivery requires a non-empty delivery payload")
        if max_purchases_per_customer is not None and (
                isinstance(max_purchases_per_customer, bool)
                or not isinstance(max_purchases_per_customer, int)
                or not 0 <= max_purchases_per_customer <= 2**31-1):
            raise CustomerStoreError("Maximum purchases per customer must be a non-negative integer")
        if not isinstance(terms_text, str) or len(terms_text) > 2500:
            raise CustomerStoreError("Product terms must be text of at most 2500 characters")
        if image_url is not None:
            if not isinstance(image_url, str) or len(image_url) > 2048:
                raise CustomerStoreError("Product image must be an HTTPS URL of at most 2048 characters")
            image_url = image_url.strip()
            if image_url:
                try:
                    parsed_image = urlsplit(image_url)
                    hostname = (parsed_image.hostname or "").lower().rstrip(".")
                    if (parsed_image.scheme.lower() != "https" or not hostname
                            or parsed_image.username is not None or parsed_image.password is not None
                            or parsed_image.fragment or hostname in {"localhost", "localhost.localdomain"}
                            or hostname.endswith((".localhost", ".local", ".internal"))):
                        raise ValueError
                    try:
                        address = ipaddress.ip_address(hostname)
                    except ValueError:
                        address = None
                    if address is not None and not address.is_global:
                        raise ValueError
                    parsed_image.port
                except ValueError:
                    raise CustomerStoreError("Product image URL must use public HTTPS without credentials or fragments") from None
        if sort_order is not None and (
                isinstance(sort_order, bool) or not isinstance(sort_order, int)
                or not -(2**31) <= sort_order <= 2**31-1):
            raise CustomerStoreError("Sort order must be a signed 32-bit integer")
        def normalize_schedule(value: str | None, label: str) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise CustomerStoreError(f"{label} must use UTC format YYYY-MM-DDTHH:MM:SSZ or -")
            value = value.strip()
            if value == "":
                raise CustomerStoreError(f"{label} must use UTC format YYYY-MM-DDTHH:MM:SSZ or -")
            if value == "-":
                return ""
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                raise CustomerStoreError(f"{label} must use UTC format YYYY-MM-DDTHH:MM:SSZ or -") from None
            if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
                raise CustomerStoreError(f"{label} must use UTC format YYYY-MM-DDTHH:MM:SSZ or -")
            return value
        visible_from_utc = normalize_schedule(visible_from_utc, "Visibility start")
        visible_until_utc = normalize_schedule(visible_until_utc, "Visibility end")
        if visible_from_utc and visible_until_utc and visible_from_utc >= visible_until_utc:
            raise CustomerStoreError("Visibility end must be later than visibility start")
        with self._connect() as db:
            existing_schedule = db.execute(
                "SELECT visible_from_utc,visible_until_utc FROM manual_products WHERE tenant_id=? AND product_id=?",
                (self.tenant_id, product_id.strip()),
            ).fetchone()
            effective_from = (visible_from_utc if visible_from_utc is not None
                              else (str(existing_schedule["visible_from_utc"] or "") if existing_schedule else ""))
            effective_until = (visible_until_utc if visible_until_utc is not None
                               else (str(existing_schedule["visible_until_utc"] or "") if existing_schedule else ""))
            if effective_from and effective_until and effective_from >= effective_until:
                raise CustomerStoreError("Visibility end must be later than visibility start")
            if category_id and not db.execute(
                "SELECT 1 FROM customer_categories WHERE tenant_id=? AND category_id=?",
                (self.tenant_id, category_id),
            ).fetchone():
                raise CustomerStoreError("Manual product category does not exist")
            db.execute("""INSERT INTO manual_products
                (tenant_id,product_id,category_id,title,description,price_minor,currency,delivery_mode,delivery_payload,enabled,
                 max_purchases_per_customer,sort_order,terms_text,image_url,visible_from_utc,visible_until_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,COALESCE(?,0),COALESCE(?,0),?,COALESCE(?,''),COALESCE(?,''),COALESCE(?,'' ))
                ON CONFLICT(tenant_id,product_id) DO UPDATE SET
                category_id=excluded.category_id,title=excluded.title,description=excluded.description,
                price_minor=excluded.price_minor,currency=excluded.currency,delivery_mode=excluded.delivery_mode,
                delivery_payload=excluded.delivery_payload,enabled=excluded.enabled,terms_text=excluded.terms_text,
                image_url=CASE WHEN ? IS NULL THEN manual_products.image_url ELSE excluded.image_url END,
                visible_from_utc=CASE WHEN ? IS NULL THEN manual_products.visible_from_utc ELSE excluded.visible_from_utc END,
                visible_until_utc=CASE WHEN ? IS NULL THEN manual_products.visible_until_utc ELSE excluded.visible_until_utc END,
                max_purchases_per_customer=CASE WHEN ? IS NULL THEN manual_products.max_purchases_per_customer
                    ELSE excluded.max_purchases_per_customer END,
                sort_order=CASE WHEN ? IS NULL THEN manual_products.sort_order ELSE excluded.sort_order END,
                updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id,product_id.strip(),category_id,title.strip()[:160],description[:4000],price_minor,currency,
                 delivery_mode,delivery_payload[:12000],int(enabled),max_purchases_per_customer,sort_order,terms_text.strip(),
                 image_url,visible_from_utc,visible_until_utc,image_url,visible_from_utc,visible_until_utc,
                 max_purchases_per_customer,sort_order))

    @staticmethod
    def _effective_manual_variant_delivery(product: dict[str, Any], variant: dict[str, Any] | None = None) -> tuple[str, str]:
        mode_value = variant.get("delivery_mode") if variant else None
        payload_value = variant.get("delivery_payload") if variant else None
        mode = str(mode_value or product["delivery_mode"])
        payload = str(payload_value if payload_value is not None else (product.get("delivery_payload") or ""))
        if mode not in {"instant", "manual", "inventory"}:
            raise CustomerStoreError("Variant delivery mode is invalid")
        if mode == "instant" and not payload.strip():
            raise CustomerStoreError("Instant variant delivery requires a non-empty effective payload")
        return mode, payload

    def manual_variant_delivery(self, product_id: str, variant_id: str) -> dict[str, str]:
        product = self.get_manual_product(product_id, enabled_only=False)
        variant = self.get_manual_variant(product_id, variant_id, enabled_only=False)
        if not product or not variant:
            raise CustomerStoreError("Manual product variant not found")
        mode, payload = self._effective_manual_variant_delivery(product, variant)
        return {"delivery_mode": mode, "delivery_payload": payload}

    @staticmethod
    def _manual_variant_quote_hash(*, product_id: str, variant_id: str, product_title: str,
                                   variant_title: str, price_minor: int, currency: str,
                                   terms_text: str, delivery_mode: str,
                                   delivery_payload: str) -> str:
        # Bind confirmation to delivery configuration without exposing payload in callback data.
        payload_digest = hashlib.sha256(delivery_payload.encode("utf-8")).hexdigest()
        material = "\0".join((product_id, variant_id, product_title, variant_title,
                                 str(price_minor), currency, terms_text.strip(),
                                 delivery_mode, payload_digest))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]

    def manual_variant_quote_hash(self, product_id: str, variant_id: str) -> str:
        product = self.get_manual_product(product_id, enabled_only=True)
        variant = self.get_manual_variant(product_id, variant_id, enabled_only=True)
        if not product or not variant:
            raise CustomerStoreError("این نوع محصول دیگر در دسترس نیست")
        delivery_mode, delivery_payload = self._effective_manual_variant_delivery(product, variant)
        return self._manual_variant_quote_hash(product_id=product_id, variant_id=variant_id,
            product_title=str(product["title"]), variant_title=str(variant["title"]),
            price_minor=int(variant["price_minor"]), currency=str(product["currency"]),
            terms_text=str(product.get("terms_text") or ""),
            delivery_mode=delivery_mode, delivery_payload=delivery_payload)

    def put_manual_variant(self, *, product_id: str, variant_id: str, title: str,
                           price_minor: int, enabled: bool = True,
                           delivery_mode: str | None = None, delivery_payload: str | None = None,
                           delivery_config_provided: bool = False) -> None:
        self._ensure_manual_shop_tables()
        product_id, variant_id, title = product_id.strip(), variant_id.strip(), title.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,18}", variant_id):
            raise CustomerStoreError("Product ID must be at most 20 characters and variant ID at most 18; use ASCII letters, digits, _ or -")
        if not title or len(title) > 100:
            raise CustomerStoreError("Variant title must contain 1–100 characters")
        if isinstance(price_minor, bool) or not isinstance(price_minor, int) or not 0 < price_minor <= 2**63-1:
            raise CustomerStoreError("Variant price must be a positive integer")
        with self._connect() as db:
            product_row = db.execute("SELECT * FROM manual_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                                     (self.tenant_id, product_id)).fetchone()
            if not product_row:
                raise CustomerStoreError("Active manual product not found")
            product = dict(product_row)
            existing_variant = db.execute("SELECT * FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND variant_id=?",
                                          (self.tenant_id, product_id, variant_id)).fetchone()
            if delivery_config_provided:
                if delivery_mode is not None and delivery_mode not in {"instant", "manual", "inventory"}:
                    raise CustomerStoreError("Variant delivery mode must be instant, inventory, manual or inherited")
                if delivery_payload is not None and (not isinstance(delivery_payload, str) or len(delivery_payload) > 12000):
                    raise CustomerStoreError("Variant delivery payload must be text of at most 12000 characters")
                mode_for_validation = delivery_mode or product["delivery_mode"]
                payload_for_validation = delivery_payload if delivery_payload is not None else str(product.get("delivery_payload") or "")
                if mode_for_validation == "instant" and not payload_for_validation.strip():
                    raise CustomerStoreError("Instant variant delivery requires a non-empty effective payload")
                stored_delivery_mode, stored_delivery_payload = delivery_mode, delivery_payload
            elif existing_variant:
                stored_delivery_mode = existing_variant["delivery_mode"]
                stored_delivery_payload = existing_variant["delivery_payload"]
            else:
                stored_delivery_mode = stored_delivery_payload = None
            if not existing_variant:
                base_stock = int(db.execute("SELECT COUNT(*) FROM manual_product_inventory WHERE tenant_id=? AND product_id=? AND variant_id='' AND status='available'",
                                            (self.tenant_id, product_id)).fetchone()[0])
                if base_stock:
                    raise CustomerStoreError("Sell or clear the existing unassigned inventory before adding variants; existing keys cannot be safely assigned to a variant automatically")
            db.execute("""INSERT INTO manual_product_variants
                (tenant_id,product_id,variant_id,title,price_minor,enabled,delivery_mode,delivery_payload)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,product_id,variant_id) DO UPDATE SET
                title=excluded.title,price_minor=excluded.price_minor,enabled=excluded.enabled,
                delivery_mode=excluded.delivery_mode,delivery_payload=excluded.delivery_payload,
                updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, product_id, variant_id, title, price_minor, int(bool(enabled)),
                 stored_delivery_mode, stored_delivery_payload))

    def list_manual_variants(self, product_id: str, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        self._ensure_manual_shop_tables()
        sql = "SELECT variant_id,product_id,title,price_minor,enabled,delivery_mode FROM manual_product_variants WHERE tenant_id=? AND product_id=?"
        args: tuple[Any, ...] = (self.tenant_id, product_id)
        if enabled_only:
            sql += " AND enabled=1 AND EXISTS (SELECT 1 FROM manual_products p WHERE p.tenant_id=manual_product_variants.tenant_id AND p.product_id=manual_product_variants.product_id AND p.enabled=1)"
        sql += " ORDER BY variant_id"
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def get_manual_variant(self, product_id: str, variant_id: str, *, enabled_only: bool = True) -> dict[str, Any] | None:
        self._ensure_manual_shop_tables()
        sql = "SELECT * FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND variant_id=?"
        args: tuple[Any, ...] = (self.tenant_id, product_id, variant_id)
        if enabled_only:
            sql += " AND enabled=1 AND EXISTS (SELECT 1 FROM manual_products p WHERE p.tenant_id=manual_product_variants.tenant_id AND p.product_id=manual_product_variants.product_id AND p.enabled=1)"
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def add_manual_inventory_key(self, *, product_id: str, secret_payload: str, variant_id: str = "") -> int:
        self._ensure_manual_shop_tables()
        if not secret_payload.strip() or len(secret_payload) > 12000:
            raise CustomerStoreError("Inventory key must be non-empty and at most 12000 characters")
        variant_id = variant_id.strip()
        with self._connect() as db:
            product_row=db.execute("SELECT * FROM manual_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                                   (self.tenant_id,product_id)).fetchone()
            if not product_row: raise CustomerStoreError("Active manual product not found")
            product = dict(product_row)
            if variant_id:
                variant_row = db.execute("SELECT * FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND variant_id=? AND enabled=1",
                                         (self.tenant_id, product_id, variant_id)).fetchone()
                if not variant_row:
                    raise CustomerStoreError("Active product variant not found")
                mode, _ = self._effective_manual_variant_delivery(product, dict(variant_row))
            else:
                if db.execute("SELECT 1 FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND enabled=1 LIMIT 1",
                              (self.tenant_id, product_id)).fetchone():
                    raise CustomerStoreError("Specify a variant ID when adding inventory to a product with variants")
                mode = str(product["delivery_mode"])
            if mode != "inventory":
                raise CustomerStoreError("Selected product or variant must use inventory delivery mode")
            try:
                cur=db.execute("INSERT INTO manual_product_inventory(tenant_id,product_id,secret_payload,variant_id) VALUES(?,?,?,?)",
                               (self.tenant_id,product_id,secret_payload.strip(),variant_id))
            except sqlite3.IntegrityError:
                raise CustomerStoreError("This key is already in inventory") from None
            return int(cur.lastrowid)

    def manual_inventory_count(self, product_id: str, variant_id: str = "") -> int:
        self._ensure_manual_shop_tables()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM manual_product_inventory WHERE tenant_id=? AND product_id=? AND variant_id=? AND status='available'",
                                  (self.tenant_id,product_id,variant_id)).fetchone()[0])

    def list_manual_products(self, *, enabled_only: bool = True,
                             category_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_manual_shop_tables()
        if category_id is not None:
            category_id = category_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", category_id):
                raise CustomerStoreError("Invalid manual product category ID")
        sql="SELECT product_id,category_id,title,description,terms_text,image_url,visible_from_utc,visible_until_utc,price_minor,currency,delivery_mode,enabled,max_purchases_per_customer,sort_order FROM manual_products WHERE tenant_id=?"
        args: tuple[Any,...]=(self.tenant_id,)
        if enabled_only:
            sql += """ AND enabled=1 AND (category_id='' OR EXISTS (
                SELECT 1 FROM customer_categories c WHERE c.tenant_id=manual_products.tenant_id
                AND c.category_id=manual_products.category_id AND c.enabled=1))
                AND (visible_from_utc='' OR visible_from_utc<=strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                AND (visible_until_utc='' OR visible_until_utc>strftime('%Y-%m-%dT%H:%M:%SZ','now'))"""
        if category_id is not None:
            sql += " AND category_id=?"
            args += (category_id,)
        sql += " ORDER BY sort_order,created_at,product_id"
        with self._connect() as db: return [dict(r) for r in db.execute(sql,args).fetchall()]

    def get_manual_product(self, product_id: str, *, enabled_only: bool = True) -> dict[str, Any] | None:
        self._ensure_manual_shop_tables()
        sql="SELECT * FROM manual_products WHERE tenant_id=? AND product_id=?"
        args: tuple[Any,...]=(self.tenant_id,product_id)
        if enabled_only:
            sql += """ AND enabled=1 AND (category_id='' OR EXISTS (
                SELECT 1 FROM customer_categories c WHERE c.tenant_id=manual_products.tenant_id
                AND c.category_id=manual_products.category_id AND c.enabled=1))
                AND (visible_from_utc='' OR visible_from_utc<=strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                AND (visible_until_utc='' OR visible_until_utc>strftime('%Y-%m-%dT%H:%M:%SZ','now'))"""
        with self._connect() as db:
            row=db.execute(sql,args).fetchone()
        return dict(row) if row else None

    def buy_manual_product(self, *, telegram_id: int, product_id: str, idempotency_key: str,
                           expected_terms_hash: str | None = None, variant_id: str = "",
                           expected_variant_quote_hash: str | None = None) -> dict[str, Any]:
        self._ensure_manual_shop_tables()
        db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT * FROM manual_product_orders WHERE tenant_id=? AND idempotency_key=?",
                             (self.tenant_id,idempotency_key)).fetchone()
            if prior:
                if (int(prior["telegram_id"]) != telegram_id or prior["product_id"] != product_id
                        or str(prior["variant_id_snapshot"] or "") != variant_id):
                    raise CustomerStoreError("Manual product idempotency key reused")
                result = dict(prior)
                base_title = result.get("product_title_snapshot") or result["product_id"]
                variant_title = str(result.get("variant_title_snapshot") or "")
                result["title"] = f"{base_title} — {variant_title}" if variant_title else base_title
                result["variant_id"] = str(result.get("variant_id_snapshot") or "")
                result["delivery_mode"] = result.get("delivery_mode_snapshot") or "manual"
                result["idempotent_replay"] = True
                db.commit()
                return result
            product=db.execute("""SELECT * FROM manual_products WHERE tenant_id=? AND product_id=? AND enabled=1
                AND (category_id='' OR EXISTS (SELECT 1 FROM customer_categories c
                    WHERE c.tenant_id=manual_products.tenant_id
                      AND c.category_id=manual_products.category_id AND c.enabled=1))
                AND (visible_from_utc='' OR visible_from_utc<=strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                AND (visible_until_utc='' OR visible_until_utc>strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (self.tenant_id,product_id)).fetchone()
            if not product: raise CustomerStoreError("Manual product is unavailable")
            variant = None
            if variant_id:
                variant = db.execute("SELECT * FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND variant_id=? AND enabled=1",
                                     (self.tenant_id, product_id, variant_id)).fetchone()
                if not variant:
                    raise CustomerStoreError("این نوع محصول دیگر در دسترس نیست")
            elif db.execute("SELECT 1 FROM manual_product_variants WHERE tenant_id=? AND product_id=? AND enabled=1 LIMIT 1",
                            (self.tenant_id, product_id)).fetchone():
                raise CustomerStoreError("لطفاً ابتدا یکی از انواع محصول را انتخاب کنید")
            purchase_price = int(variant["price_minor"] if variant else product["price_minor"])
            variant_title = str(variant["title"] if variant else "")
            effective_mode, effective_payload = self._effective_manual_variant_delivery(dict(product), dict(variant) if variant else None)
            current_terms = str(product["terms_text"] or "").strip()
            current_terms_hash = hashlib.sha256(current_terms.encode("utf-8")).hexdigest()[:12]
            if variant:
                current_quote_hash = self._manual_variant_quote_hash(product_id=product_id, variant_id=variant_id,
                    product_title=str(product["title"]), variant_title=variant_title,
                    price_minor=purchase_price, currency=str(product["currency"]), terms_text=current_terms,
                    delivery_mode=effective_mode, delivery_payload=effective_payload)
                if not expected_variant_quote_hash or expected_variant_quote_hash != current_quote_hash:
                    raise CustomerStoreError("قیمت، نام یا شرایط این نوع محصول تغییر کرده است؛ تأیید خرید را دوباره باز کنید")
            elif (current_terms or expected_terms_hash is not None) and expected_terms_hash != current_terms_hash:
                raise CustomerStoreError("شرایط خرید تغییر کرده یا تأیید نشده است؛ صفحه تأیید خرید را دوباره باز کنید")
            customer=db.execute("SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?",
                                (self.tenant_id,telegram_id)).fetchone()
            if not customer: raise CustomerStoreError("Customer must be registered before purchase")
            purchase_cap = int(product["max_purchases_per_customer"] or 0)
            if purchase_cap:
                prior_purchases = int(db.execute(
                    """SELECT COUNT(*) FROM manual_product_orders
                       WHERE tenant_id=? AND telegram_id=? AND product_id=?
                         AND status IN ('paid','fulfilled','pending_delivery')""",
                    (self.tenant_id, telegram_id, product_id),
                ).fetchone()[0])
                if prior_purchases >= purchase_cap:
                    raise CustomerStoreError("Manual product purchase limit reached")
            balance=int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?",
                                   (self.tenant_id,telegram_id,product["currency"])).fetchone()[0])
            if balance < purchase_price: raise CustomerStoreError("Insufficient wallet balance")
            order_id=uuid.uuid4().hex
            mode=effective_mode
            status="pending_delivery" if mode=="manual" else "fulfilled"
            payload=effective_payload if mode=="instant" else ""
            inventory_id=None
            if mode == "inventory":
                key_row=db.execute("SELECT inventory_id,secret_payload FROM manual_product_inventory WHERE tenant_id=? AND product_id=? AND variant_id=? AND status='available' ORDER BY inventory_id LIMIT 1",
                                   (self.tenant_id,product_id,variant_id)).fetchone()
                if not key_row: raise CustomerStoreError("Manual product is out of stock; no wallet debit was made")
                inventory_id=int(key_row["inventory_id"]); payload=str(key_row["secret_payload"])
                cur=db.execute("UPDATE manual_product_inventory SET status='delivered',order_id=? WHERE tenant_id=? AND inventory_id=? AND status='available'",
                               (order_id,self.tenant_id,inventory_id))
                if cur.rowcount != 1: raise CustomerStoreError("Inventory key was already reserved")
            db.execute("""INSERT INTO manual_product_orders
                (tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key,
                 delivery_payload,product_title_snapshot,delivery_mode_snapshot,terms_snapshot,variant_id_snapshot,variant_title_snapshot)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id,order_id,telegram_id,product_id,purchase_price,product["currency"],
                 status,idempotency_key,payload,product["title"],mode,current_terms,variant_id,variant_title))
            db.execute("""INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                VALUES(?,?,?,?,?,?,?,?)""",
                (self.tenant_id,telegram_id,"manual-product:"+order_id,-purchase_price,product["currency"],
                 "purchase",order_id,"Manual digital product purchase"))
            self._enqueue_admin_notification_in_transaction(db, event_key="purchase",
                idempotency_key=f"manual-order:{order_id}",
                payload={"public_order_id": order_id[:20], "product_title": (str(product["title"]) + (" — " + variant_title if variant_title else "")),
                         "amount_minor": purchase_price, "currency": str(product["currency"]),
                         "status": status, "customer_id": telegram_id})
            self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                idempotency_key=f"manual-wallet:{order_id}",
                payload={"amount_minor": -purchase_price, "currency": str(product["currency"]),
                         "entry_type": "purchase", "customer_id": telegram_id, "status": "debited"})
            display_title = str(product["title"]) + (" — " + variant_title if variant_title else "")
            result={"order_id":order_id,"product_id":product_id,"variant_id":variant_id,"amount_minor":purchase_price,
                    "currency":product["currency"],"status":status,"delivery_mode":mode,
                    "delivery_payload":payload,"title":display_title,"idempotent_replay":False}
            db.commit(); return result
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def list_manual_orders(self, *, status: str = "pending_delivery", limit: int = 50) -> list[dict[str, Any]]:
        """List manual-shop orders for staff; never include delivery payloads in list views."""
        self._ensure_manual_shop_tables()
        if status not in {"paid", "fulfilled", "pending_delivery", "failed", "all"}:
            raise CustomerStoreError("Unsupported manual order status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Manual order limit must be between 1 and 100")
        sql = """SELECT o.order_id,o.telegram_id,o.product_id,
                        COALESCE(NULLIF(o.product_title_snapshot,''),p.title,o.product_id) ||
                            CASE WHEN COALESCE(o.variant_title_snapshot,'')='' THEN '' ELSE ' — ' || o.variant_title_snapshot END AS title,
                        o.amount_minor,o.currency,o.status,o.created_at
                 FROM manual_product_orders o LEFT JOIN manual_products p
                 ON p.tenant_id=o.tenant_id AND p.product_id=o.product_id
                 WHERE o.tenant_id=?"""
        args: tuple[Any, ...] = (self.tenant_id,)
        if status != "all":
            sql += " AND o.status=?"
            args += (status,)
        sql += " ORDER BY o.created_at,o.order_id LIMIT ?"
        args += (limit,)
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def fulfill_manual_order(self, *, order_id: str, delivery_payload: str,
                             fulfilled_by: int) -> dict[str, Any]:
        """Atomically fulfill one pending manual order and retain a staff audit event."""
        self._ensure_manual_shop_tables()
        order_id = order_id.strip()
        delivery_payload = delivery_payload.strip()
        if not order_id or len(order_id) > 80:
            raise CustomerStoreError("Invalid manual order ID")
        if not delivery_payload or len(delivery_payload) > 12000:
            raise CustomerStoreError("Delivery text must be non-empty and at most 12000 characters")
        if isinstance(fulfilled_by, bool) or not isinstance(fulfilled_by, int) or fulfilled_by <= 0:
            raise CustomerStoreError("Invalid staff ID")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM manual_product_orders WHERE tenant_id=? AND order_id=?",
                             (self.tenant_id, order_id)).fetchone()
            if not row:
                raise CustomerStoreError("Manual order not found")
            if row["status"] != "pending_delivery":
                raise CustomerStoreError("Order is not pending manual delivery")
            cur = db.execute("UPDATE manual_product_orders SET status='fulfilled',delivery_payload=? WHERE tenant_id=? AND order_id=? AND status='pending_delivery'",
                             (delivery_payload, self.tenant_id, order_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Order fulfillment conflict; refresh the order list")
            db.execute("INSERT INTO manual_product_order_events(tenant_id,order_id,event_type,actor_telegram_id) VALUES(?,?,?,?)",
                       (self.tenant_id, order_id, "fulfilled", fulfilled_by))
            now = time.time()
            db.execute("""INSERT OR IGNORE INTO manual_delivery_notifications
                (tenant_id,order_id,status,attempts,available_at,created_at)
                VALUES(?,?,'pending',0,?,?)""", (self.tenant_id, order_id, now, now))
            result = dict(row)
            result.update(status="fulfilled", delivery_payload=delivery_payload)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_manual_delivery_notifications(self, *, limit: int = 20, now: float | None = None,
                                             lease_seconds: int = 300) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Notification batch limit must be between 1 and 100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise CustomerStoreError("Notification lease must be between 30 and 3600 seconds")
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE manual_delivery_notifications
                SET status='pending',claimed_at=NULL,available_at=?
                WHERE tenant_id=? AND status='processing' AND claimed_at IS NOT NULL AND claimed_at<=?""",
                (ts, self.tenant_id, ts - lease_seconds))
            rows = db.execute("""SELECT n.notification_id,n.tenant_id,n.order_id,n.attempts,
                    o.telegram_id,o.product_id,o.delivery_payload,
                    COALESCE(NULLIF(o.product_title_snapshot,''),o.product_id) ||
                        CASE WHEN COALESCE(o.variant_title_snapshot,'')='' THEN '' ELSE ' — ' || o.variant_title_snapshot END AS product_title_snapshot
                FROM manual_delivery_notifications n
                JOIN manual_product_orders o ON o.tenant_id=n.tenant_id AND o.order_id=n.order_id
                WHERE n.tenant_id=? AND n.status='pending' AND n.available_at<=? AND o.status='fulfilled'
                ORDER BY n.notification_id LIMIT ?""", (self.tenant_id, ts, limit)).fetchall()
            claimed = []
            for row in rows:
                cur = db.execute("UPDATE manual_delivery_notifications SET status='processing',attempts=attempts+1,claimed_at=? WHERE tenant_id=? AND notification_id=? AND status='pending'",
                                 (ts, self.tenant_id, row["notification_id"]))
                if cur.rowcount != 1:
                    continue
                item = dict(row)
                item["attempts"] = int(row["attempts"]) + 1
                claimed.append(item)
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_manual_delivery_notification_sent(self, notification_id: int, *, now: float | None = None) -> None:
        ts = time.time() if now is None else float(now)
        with self._connect() as db:
            cur = db.execute("UPDATE manual_delivery_notifications SET status='sent',sent_at=?,claimed_at=NULL,last_error_code=NULL WHERE tenant_id=? AND notification_id=? AND status='processing'",
                             (ts, self.tenant_id, notification_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Delivery notification is not in processing state")

    def retry_manual_delivery_notification(self, notification_id: int, *, attempt: int,
                                           error_code: str, now: float | None = None,
                                           max_attempts: int = 8) -> str:
        if not error_code or len(error_code) > 80 or any(ch.isspace() for ch in error_code):
            raise CustomerStoreError("Notification error code must be short and non-sensitive")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise CustomerStoreError("Invalid notification attempt")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise CustomerStoreError("Invalid maximum notification attempts")
        ts = time.time() if now is None else float(now)
        status = "dead" if attempt >= max_attempts else "pending"
        next_at = ts if status == "dead" else ts + min(3600, 2 ** min(attempt, 10))
        with self._connect() as db:
            cur = db.execute("UPDATE manual_delivery_notifications SET status=?,available_at=?,claimed_at=NULL,last_error_code=? WHERE tenant_id=? AND notification_id=? AND status='processing'",
                             (status, next_at, error_code, self.tenant_id, notification_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Delivery notification is not in processing state")
        return status

    def get_manual_delivery_notification(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT notification_id,tenant_id,order_id,status,attempts,available_at,created_at,claimed_at,sent_at,last_error_code FROM manual_delivery_notifications WHERE tenant_id=? AND order_id=?",
                             (self.tenant_id, order_id)).fetchone()
        return dict(row) if row else None

    def retry_manual_delivery_notification_now(self, order_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE manual_delivery_notifications SET status='pending',attempts=0,available_at=?,claimed_at=NULL,last_error_code=NULL WHERE tenant_id=? AND order_id=? AND status='dead'",
                             (time.time(), self.tenant_id, order_id))
            return cur.rowcount == 1

    def ensure_customer(self, telegram_id: int, username: str = "", first_name: str = "") -> None:
        if not isinstance(telegram_id, int) or telegram_id <= 0:
            raise CustomerStoreError("Invalid Telegram user ID")
        with self._connect() as db:
            db.execute(
                """INSERT INTO customer_users(tenant_id,telegram_id,username,first_name)
                   VALUES(?,?,?,?) ON CONFLICT(tenant_id,telegram_id) DO UPDATE SET
                   username=excluded.username, first_name=excluded.first_name,
                   updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, telegram_id, username[:128], first_name[:256]),
            )

    def get_customer_language(self, telegram_id: int) -> str:
        """Return the persisted language for this tenant's customer, defaulting safely to Farsi."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise CustomerStoreError("Invalid customer ID")
        with self._connect() as db:
            row = db.execute(
                "SELECT language FROM customer_users WHERE tenant_id=? AND telegram_id=?",
                (self.tenant_id, telegram_id),
            ).fetchone()
        if not row:
            return "fa"
        language = str(row["language"] or "fa").strip().lower()
        return language if language in {"fa", "en"} else "fa"

    def set_customer_language(self, telegram_id: int, language: str) -> str:
        """Persist one supported customer language without changing profile identity fields."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise CustomerStoreError("Invalid customer ID")
        normalized = str(language).strip().lower() if isinstance(language, str) else ""
        if normalized not in {"fa", "en"}:
            raise CustomerStoreError("Unsupported customer language")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE customer_users SET language=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND telegram_id=?",
                (normalized, self.tenant_id, telegram_id),
            )
            if cur.rowcount != 1:
                raise CustomerStoreError("Customer not found")
        return normalized

    def put_category(self, *, category_id: str, title: str, description: str = "", enabled: bool = True) -> None:
        category_id = category_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", category_id) or not title.strip():
            raise CustomerStoreError("Category ID must use 1..20 ASCII letters, digits, _ or -; title is required")
        with self._connect() as db:
            db.execute(
                """INSERT INTO customer_categories (tenant_id,category_id,title,description,enabled)
                   VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,category_id) DO UPDATE SET
                   title=excluded.title,description=excluded.description,enabled=excluded.enabled,
                   updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, category_id, title.strip()[:120], description[:1000], int(enabled)),
            )

    def list_categories(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT category_id,title,description,enabled FROM customer_categories WHERE tenant_id=?"
        args: tuple[Any, ...] = (self.tenant_id,)
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY title COLLATE NOCASE, category_id"
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def set_category_enabled(self, category_id: str, enabled: bool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", category_id.strip()):
            raise CustomerStoreError("Invalid category ID")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE customer_categories SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND category_id=?",
                (int(enabled), self.tenant_id, category_id.strip()),
            )
            if cur.rowcount != 1:
                raise CustomerStoreError("Category not found")

    def get_product(self, product_id: str, *, enabled_only: bool = True) -> dict[str, Any] | None:
        sql = "SELECT p.* FROM customer_products p WHERE p.tenant_id=? AND p.product_id=?"
        args: tuple[Any, ...] = (self.tenant_id, product_id)
        if enabled_only:
            sql += """ AND p.enabled=1 AND (p.category_id='' OR EXISTS (
                SELECT 1 FROM customer_categories c WHERE c.tenant_id=p.tenant_id
                AND c.category_id=p.category_id AND c.enabled=1))"""
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get_order_by_idempotency(self, idempotency_key: str, telegram_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM customer_orders WHERE tenant_id=? AND idempotency_key=? AND telegram_id=?", (self.tenant_id, idempotency_key, telegram_id)).fetchone()
        if not row: return None
        result = dict(row)
        result["product_snapshot"] = json.loads(result.get("product_snapshot_json") or "{}")
        return result

    def list_customer_orders(self, telegram_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return a minimal, tenant- and customer-scoped history across panel and manual orders."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise CustomerStoreError("Invalid customer ID")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Invalid order history limit")
        with self._connect() as db:
            panel_rows = db.execute(
                """SELECT o.order_id,o.product_id,o.amount_minor,o.currency,o.status,o.created_at,o.updated_at,
                          o.discount_code,o.discount_amount_minor,o.product_snapshot_json,p.title AS current_title
                   FROM customer_orders o LEFT JOIN customer_products p
                     ON p.tenant_id=o.tenant_id AND p.product_id=o.product_id
                   WHERE o.tenant_id=? AND o.telegram_id=?
                   ORDER BY o.created_at DESC,o.order_id DESC LIMIT ?""",
                (self.tenant_id, telegram_id, limit),
            ).fetchall()
            manual_rows = db.execute(
                """SELECT order_id,product_id,amount_minor,currency,status,created_at,
                          product_title_snapshot
                   FROM manual_product_orders
                   WHERE tenant_id=? AND telegram_id=?
                   ORDER BY created_at DESC,order_id DESC LIMIT ?""",
                (self.tenant_id, telegram_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in panel_rows:
            item = dict(row)
            try:
                snapshot = json.loads(item.pop("product_snapshot_json") or "{}")
            except (TypeError, ValueError):
                snapshot = {}
            item["title"] = str(snapshot.get("title") or item.get("current_title") or item["product_id"])
            item.pop("current_title", None)
            item["amount_minor"] = int(item["amount_minor"])
            item["discount_amount_minor"] = int(item.get("discount_amount_minor") or 0)
            item["discount_code"] = str(item.get("discount_code") or "")
            item["order_kind"] = "panel"
            result.append(item)
        for row in manual_rows:
            item = dict(row)
            item["title"] = str(item.pop("product_title_snapshot", "") or item["product_id"])
            item["amount_minor"] = int(item["amount_minor"])
            item["discount_amount_minor"] = 0
            item["discount_code"] = ""
            item["updated_at"] = str(item.get("created_at") or "")
            item["order_kind"] = "manual"
            result.append(item)
        result.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("order_id") or "")), reverse=True)
        return result[:limit]

    def get_product_order_reconciliation(self, order_id: str) -> dict[str, Any] | None:
        """Read one tenant-scoped panel product order for recovery review."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM customer_orders WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            snapshot = json.loads(result.get("product_snapshot_json") or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        result["product_snapshot"] = snapshot if isinstance(snapshot, dict) else {}
        result["reconciliation_intent"] = result["product_snapshot"].get("_provisioning_intent")
        return result

    def list_product_order_reconciliation(self, *, status: str = "reconcile_required",
                                          limit: int = 50) -> list[dict[str, Any]]:
        """List ambiguous panel-backed product orders without returning private provisioning payloads."""
        if status not in {"reconcile_required", "all"}:
            raise CustomerStoreError("Unsupported product order filter")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Invalid product order list limit")
        sql = """SELECT order_id,telegram_id,product_id,amount_minor,currency,status,provider_reference,
                        product_snapshot_json,created_at,updated_at
                 FROM customer_orders WHERE tenant_id=?
                   AND provider_reference LIKE 'reconcile_required:%'"""
        args: tuple[Any, ...] = (self.tenant_id,)
        if status == "reconcile_required":
            sql += " AND status='provisioning'"
        sql += " ORDER BY updated_at,order_id LIMIT ?"
        args += (limit,)
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                snapshot = json.loads(item.pop("product_snapshot_json") or "{}")
            except (TypeError, ValueError):
                snapshot = {}
            item["title"] = str(snapshot.get("title") or item["product_id"])
            item["has_provisioning_intent"] = isinstance(snapshot.get("_provisioning_intent"), dict)
            result.append(item)
        return result

    def list_product_order_reconciliation_events(self, order_id: str) -> list[dict[str, Any]]:
        """Read tenant-scoped immutable operator evidence for one order."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT event_id,order_id,event_type,actor_telegram_id,provider_kind,
                          provider_user_id,username,comparison,evidence_json,reason,created_at
                   FROM customer_product_order_reconciliation_events
                   WHERE tenant_id=? AND order_id=? ORDER BY event_id""",
                (self.tenant_id, order_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except (TypeError, ValueError):
                item["evidence"] = {}
            result.append(item)
        return result

    def is_product_provider_identity_claimed(self, *, panel_id: str, provider_user_id: str,
                                             username: str, excluding_service_id: str = "") -> bool:
        """Avoid binding one provider identity to multiple local services in this tenant."""
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM customer_services
                   WHERE tenant_id=? AND panel_id=?
                     AND service_id<>?
                     AND ((?<>'' AND provider_user_id=?) OR (?<>'' AND username=?))
                   LIMIT 1""",
                (self.tenant_id, panel_id, excluding_service_id,
                 provider_user_id, provider_user_id, username, username),
            ).fetchone()
        return row is not None

    def record_product_order_provisioning_intent(self, *, order_id: str,
                                                  intent: dict[str, Any]) -> None:
        """Persist exact create/service intent and mark the no-retry reconciliation boundary before provider I/O."""
        if not isinstance(intent, dict) or intent.get("version") != 1:
            raise CustomerStoreError("Invalid product provisioning intent")
        username = intent.get("username")
        binding = intent.get("provider_binding")
        service_product = intent.get("service_product")
        if (not isinstance(username, str) or not username or len(username) > 128
                or not isinstance(binding, dict) or not isinstance(service_product, dict)):
            raise CustomerStoreError("Product provisioning intent is incomplete")
        encoded_intent = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,provider_reference,product_snapshot_json FROM customer_orders WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Order not found")
            if row["status"] != "provisioning":
                raise CustomerStoreError("Order is not awaiting provider provisioning")
            snapshot = json.loads(row["product_snapshot_json"] or "{}")
            if not isinstance(snapshot, dict):
                raise CustomerStoreError("Stored product snapshot is invalid")
            prior_intent = snapshot.get("_provisioning_intent")
            if prior_intent is not None:
                prior_encoded = json.dumps(prior_intent, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if prior_encoded != encoded_intent:
                    raise CustomerStoreError("Provisioning intent is immutable once recorded")
            elif str(row["provider_reference"] or "").strip():
                raise CustomerStoreError("Order already contains a provider reconciliation marker")
            snapshot["_provisioning_intent"] = intent
            db.execute(
                """UPDATE customer_orders SET product_snapshot_json=?,
                   provider_reference='reconcile_required:provider_create_started',updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND order_id=? AND status='provisioning'""",
                (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), self.tenant_id, order_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_products(self, panel_id: str | None = None, category_id: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT p.product_id,p.category_id,p.title,p.description,p.price_minor,p.currency,p.duration_hours,
                          p.traffic_bytes,p.pricing_mode,p.unit_price_minor,p.panel_id,p.group_ids_json
                   FROM customer_products p WHERE p.tenant_id=? AND p.enabled=1
                   AND (p.category_id='' OR EXISTS (SELECT 1 FROM customer_categories c
                        WHERE c.tenant_id=p.tenant_id AND c.category_id=p.category_id AND c.enabled=1))"""
        args: tuple[Any, ...] = (self.tenant_id,)
        if panel_id is not None:
            sql += " AND p.panel_id=?"; args += (panel_id,)
        if category_id is not None:
            sql += " AND p.category_id=?"; args += (category_id,)
        sql += " ORDER BY p.created_at,p.product_id"
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def put_product(self, *, product_id: str, title: str, description: str = "",
                    price_minor: int, currency: str = "IRR", duration_hours: int = 0,
                    traffic_bytes: int = 0, pricing_mode: str = "fixed", unit_price_minor: int = 0,
                    panel_id: str = "", group_ids: tuple[int, ...] | list[int] = (),
                    category_id: str = "", enabled: bool = True) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id.strip()) or not title.strip():
            raise CustomerStoreError("Product ID must use 1..20 ASCII letters, digits, _ or -; title is required")
        max_int = 2**63 - 1
        if isinstance(price_minor, bool) or not isinstance(price_minor, int) or not 0 <= price_minor <= max_int:
            raise CustomerStoreError("Price must be a non-negative signed 64-bit integer")
        if (isinstance(duration_hours, bool) or not isinstance(duration_hours, int) or not 0 <= duration_hours <= 87600
                or isinstance(traffic_bytes, bool) or not isinstance(traffic_bytes, int) or not 0 <= traffic_bytes <= max_int
                or isinstance(unit_price_minor, bool) or not isinstance(unit_price_minor, int) or not 0 <= unit_price_minor <= max_int):
            raise CustomerStoreError("Product duration must be 0..87600 hours and limits/prices must be non-negative signed 64-bit integers")
        if pricing_mode not in {"fixed", "unlimited", "hourly", "usage"}:
            raise CustomerStoreError("Unsupported customer product pricing mode")
        if pricing_mode in {"hourly", "usage"} and unit_price_minor <= 0:
            raise CustomerStoreError("Hourly and usage products require a positive unit price")
        if enabled and pricing_mode in {"fixed", "unlimited"} and price_minor <= 0:
            raise CustomerStoreError("Prepaid products require a positive fixed price")
        category_id = category_id.strip()
        if category_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", category_id):
            raise CustomerStoreError("Invalid product category ID")
        normalized_groups = []
        for group in group_ids:
            value = str(group).strip()
            if not value or len(value) > 128 or any(ch.isspace() for ch in value):
                raise CustomerStoreError("Panel group/service/inbound IDs must be short non-empty values")
            normalized_groups.append(value)
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        with self._connect() as db:
            if category_id and not db.execute("SELECT 1 FROM customer_categories WHERE tenant_id=? AND category_id=?", (self.tenant_id, category_id)).fetchone():
                raise CustomerStoreError("Product category does not exist")
            db.execute(
                """INSERT INTO customer_products
                   (tenant_id,product_id,category_id,title,description,price_minor,currency,duration_hours,
                    traffic_bytes,pricing_mode,unit_price_minor,panel_id,group_ids_json,enabled)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,product_id) DO UPDATE SET category_id=excluded.category_id,title=excluded.title,
                    description=excluded.description,price_minor=excluded.price_minor,
                    currency=excluded.currency,duration_hours=excluded.duration_hours,
                    traffic_bytes=excluded.traffic_bytes,pricing_mode=excluded.pricing_mode,
                    unit_price_minor=excluded.unit_price_minor,panel_id=excluded.panel_id,
                    group_ids_json=excluded.group_ids_json,enabled=excluded.enabled,
                    updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, product_id.strip(), category_id, title.strip(), description[:4000],
                 price_minor, currency, duration_hours, traffic_bytes, pricing_mode,
                 unit_price_minor, panel_id.strip(), json.dumps(normalized_groups), int(enabled)),
            )

    def set_product_enabled(self, product_id: str, enabled: bool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id.strip()):
            raise CustomerStoreError("Invalid product ID")
        with self._connect() as db:
            row = db.execute("SELECT pricing_mode,price_minor,unit_price_minor,panel_id,group_ids_json FROM customer_products WHERE tenant_id=? AND product_id=?",
                             (self.tenant_id, product_id.strip())).fetchone()
            if not row:
                raise CustomerStoreError("Product not found")
            if enabled:
                panel = db.execute("SELECT enabled FROM panel_connections WHERE tenant_id=? AND panel_id=?",
                                   (self.tenant_id, row["panel_id"])).fetchone()
                if not panel or not panel["enabled"]:
                    raise CustomerStoreError("Bind this product to an enabled panel before publishing it")
                if not json.loads(row["group_ids_json"] or "[]"):
                    raise CustomerStoreError("Product must define provider group/service/inbound IDs before publishing")
            if enabled and row["pricing_mode"] in {"fixed", "unlimited"} and int(row["price_minor"]) <= 0:
                raise CustomerStoreError("Prepaid product must have a positive setup price")
            if enabled and row["pricing_mode"] in {"hourly", "usage"} and int(row["unit_price_minor"]) <= 0:
                raise CustomerStoreError("Metered product must have a positive unit price")
            cur = db.execute("UPDATE customer_products SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND product_id=?",
                             (int(enabled), self.tenant_id, product_id.strip()))
            if cur.rowcount != 1:
                raise CustomerStoreError("Product not found")

    def bind_product_panel(self, *, product_id: str, panel_id: str,
                           group_ids: tuple[str, ...] | list[str]) -> None:
        normalized = []
        for value in group_ids:
            item = str(value).strip()
            if not item or len(item) > 128 or any(ch.isspace() for ch in item):
                raise CustomerStoreError("Panel service/group/inbound IDs must be short non-empty values")
            normalized.append(item)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            product = db.execute("SELECT 1 FROM customer_products WHERE tenant_id=? AND product_id=?",
                                 (self.tenant_id, product_id)).fetchone()
            panel = db.execute("SELECT enabled FROM panel_connections WHERE tenant_id=? AND panel_id=?",
                               (self.tenant_id, panel_id)).fetchone()
            if not product:
                raise CustomerStoreError("Product not found")
            if not panel or not panel["enabled"]:
                raise CustomerStoreError("An existing enabled panel connection is required")
            db.execute("UPDATE customer_products SET panel_id=?,group_ids_json=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND product_id=?",
                       (panel_id, json.dumps(normalized), self.tenant_id, product_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def create_order(self, *, order_id: str, telegram_id: int, product_id: str,
                     idempotency_key: str) -> dict[str, Any]:
        """Create a price-snapshotted pending order; never charges or provisions by itself."""
        if not all(x.strip() for x in (order_id, product_id, idempotency_key)):
            raise CustomerStoreError("Order ID, product ID and idempotency key are required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT order_id,telegram_id,product_id,amount_minor,currency,status FROM customer_orders WHERE tenant_id=? AND idempotency_key=?",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if prior:
                if int(prior["telegram_id"]) != telegram_id or prior["product_id"] != product_id:
                    raise CustomerStoreError("Order idempotency key reused for a different purchase")
                db.commit()
                return dict(prior)
            product = db.execute(
                "SELECT price_minor,currency FROM customer_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                (self.tenant_id, product_id),
            ).fetchone()
            if not product:
                raise CustomerStoreError("Product is unavailable")
            customer = db.execute(
                "SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?",
                (self.tenant_id, telegram_id),
            ).fetchone()
            if not customer:
                raise CustomerStoreError("Customer must be registered before ordering")
            db.execute(
                """INSERT INTO customer_orders
                   (tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key)
                   VALUES(?,?,?,?,?,?,'pending',?)""",
                (self.tenant_id, order_id, telegram_id, product_id, int(product["price_minor"]),
                 str(product["currency"]), idempotency_key),
            )
            db.commit()
            return {"order_id": order_id, "telegram_id": telegram_id, "product_id": product_id,
                    "amount_minor": int(product["price_minor"]), "currency": str(product["currency"]),
                    "status": "pending"}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def transition_order(self, *, order_id: str, new_status: str,
                         provider_reference: str = "") -> None:
        transitions = {
            "pending": {"paid", "failed", "cancelled"},
            "paid": {"provisioning", "refunded", "failed"},
            "provisioning": {"active", "failed", "refunded"},
            "failed": {"provisioning", "refunded", "cancelled"},
            "active": {"refunded"},
            "refunded": set(),
            "cancelled": set(),
        }
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,provider_reference FROM customer_orders WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Order not found")
            current = str(row["status"])
            if new_status == current:
                stored_reference = str(row["provider_reference"] or "")
                if provider_reference and stored_reference != provider_reference[:256]:
                    raise CustomerStoreError("Order status replay conflicts with provider reference")
                db.commit()
                return
            if new_status not in transitions.get(current, set()):
                raise CustomerStoreError(f"Invalid order transition: {current} -> {new_status}")
            db.execute(
                """UPDATE customer_orders SET status=?,provider_reference=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND order_id=?""",
                (new_status, provider_reference[:256], self.tenant_id, order_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def set_manual_deposit_details(self, *, card_number: str, account_holder: str, bank_name: str = '') -> None:
        digits = ''.join(ch for ch in card_number if ch.isdigit())
        if len(digits) != 16 or any(ch not in '0123456789 -' for ch in card_number):
            raise CustomerStoreError("Card number must contain exactly 16 digits")
        account_holder = account_holder.strip()
        bank_name = bank_name.strip()
        if not 2 <= len(account_holder) <= 100 or len(bank_name) > 100:
            raise CustomerStoreError("Account holder or bank name is invalid")
        with self._connect() as db:
            db.execute("""INSERT INTO manual_deposit_settings(tenant_id,card_number,account_holder,bank_name)
                VALUES(?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET card_number=excluded.card_number,
                account_holder=excluded.account_holder,bank_name=excluded.bank_name,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, digits, account_holder, bank_name))

    def get_manual_deposit_details(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT card_number,account_holder,bank_name,updated_at FROM manual_deposit_settings WHERE tenant_id=?",
                             (self.tenant_id,)).fetchone()
        return dict(row) if row else None

    def submit_manual_deposit_receipt(self, *, payment_id: str, telegram_id: int,
                                      transfer_reference: str, payer_name: str = '',
                                      receipt_file_id: str = '') -> None:
        reference = transfer_reference.strip()
        payer_name = payer_name.strip()
        if not 4 <= len(reference) <= 120 or any(ch.isspace() for ch in reference):
            raise CustomerStoreError("Transfer reference must be 4-120 characters without spaces")
        if len(payer_name) > 100 or len(receipt_file_id) > 512:
            raise CustomerStoreError("Payer name or receipt file reference is too long")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            payment = db.execute("""SELECT provider,status,telegram_id FROM payment_attempts
                WHERE tenant_id=? AND payment_id=?""", (self.tenant_id,payment_id)).fetchone()
            if not payment or payment["telegram_id"] != telegram_id:
                raise CustomerStoreError("Payment not found for this customer")
            if payment["provider"] != "manual" or payment["status"] != "pending":
                raise CustomerStoreError("Only pending manual deposits accept receipts")
            existing = db.execute("SELECT transfer_reference,payer_name,receipt_file_id,status FROM manual_deposit_receipts WHERE tenant_id=? AND payment_id=?",
                                  (self.tenant_id,payment_id)).fetchone()
            if existing:
                if (existing["transfer_reference"] == reference and existing["payer_name"] == payer_name
                        and existing["receipt_file_id"] == receipt_file_id and existing["status"] == "submitted"):
                    db.commit()
                    return
                raise CustomerStoreError("A receipt was already submitted for this payment")
            db.execute("""INSERT INTO manual_deposit_receipts(tenant_id,payment_id,telegram_id,transfer_reference,payer_name,receipt_file_id)
                VALUES(?,?,?,?,?,?)""", (self.tenant_id,payment_id,telegram_id,reference,payer_name,receipt_file_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_manual_deposit_receipts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT r.payment_id,r.telegram_id,r.transfer_reference,r.payer_name,r.receipt_file_id,r.status,r.created_at,
                p.amount_minor,p.currency FROM manual_deposit_receipts r JOIN payment_attempts p
                ON p.tenant_id=r.tenant_id AND p.payment_id=r.payment_id
                WHERE r.tenant_id=? AND r.status='submitted' AND p.status='pending'
                ORDER BY r.created_at LIMIT 100""", (self.tenant_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_manual_deposit_receipt(self, payment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("""SELECT payment_id,telegram_id,transfer_reference,payer_name,receipt_file_id,status,created_at
                FROM manual_deposit_receipts WHERE tenant_id=? AND payment_id=?""",
                (self.tenant_id,payment_id)).fetchone()
        return dict(row) if row else None

    def reject_manual_deposit_receipt(self, *, payment_id: str, admin_id: int, reason: str) -> None:
        reason = reason.strip()
        if not reason or len(reason) > 128:
            raise CustomerStoreError("A short rejection reason is required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            payment = db.execute("SELECT status,provider FROM payment_attempts WHERE tenant_id=? AND payment_id=?",
                                 (self.tenant_id,payment_id)).fetchone()
            receipt = db.execute("SELECT status FROM manual_deposit_receipts WHERE tenant_id=? AND payment_id=?",
                                 (self.tenant_id,payment_id)).fetchone()
            if not payment or payment["provider"] != "manual" or payment["status"] != "pending" or not receipt or receipt["status"] != "submitted":
                raise CustomerStoreError("No pending submitted manual receipt exists")
            db.execute("UPDATE manual_deposit_receipts SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND payment_id=? AND status='submitted'",
                       (self.tenant_id,payment_id))
            db.execute("UPDATE payment_attempts SET status='failed',failure_code=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND payment_id=? AND status='pending'",
                       (("manual_receipt_rejected:" + reason)[:128],self.tenant_id,payment_id))
            db.execute("""INSERT INTO payment_attempt_events(tenant_id,payment_id,event_type,actor_telegram_id,
                amount_minor,currency,failure_code) SELECT tenant_id,payment_id,'failed',?,amount_minor,currency,?
                FROM payment_attempts WHERE tenant_id=? AND payment_id=?""",
                (admin_id,("manual_receipt_rejected:" + reason)[:128],self.tenant_id,payment_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_payment_attempt(self, *, payment_id: str, telegram_id: int, provider: str,
                               amount_minor: int, currency: str, idempotency_key: str) -> str:
        """Create a pending payment intent; this does not prove or accept payment."""
        if not all(x.strip() for x in (payment_id, provider, idempotency_key)):
            raise CustomerStoreError("Payment ID, provider and idempotency key are required")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or not 0 < amount_minor <= 2**63 - 1:
            raise CustomerStoreError("Payment amount must be a positive signed 64-bit integer")
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT payment_id,telegram_id,provider,amount_minor,currency FROM payment_attempts
                   WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if existing:
                same = (existing["telegram_id"] == telegram_id and existing["provider"] == provider
                        and existing["amount_minor"] == amount_minor and existing["currency"] == currency)
                if not same:
                    raise CustomerStoreError("Payment idempotency key reused with different data")
                db.commit()
                return str(existing["payment_id"])
            db.execute(
                """INSERT INTO payment_attempts
                   (tenant_id,payment_id,telegram_id,provider,amount_minor,currency,status,idempotency_key)
                   VALUES(?,?,?,?,?,?,'pending',?)""",
                (self.tenant_id, payment_id, telegram_id, provider, amount_minor, currency, idempotency_key),
            )
            db.execute("""INSERT INTO payment_attempt_events
                (tenant_id,payment_id,event_type,amount_minor,currency) VALUES(?,?,'created',?,?)""",
                (self.tenant_id, payment_id, amount_minor, currency))
            db.commit()
            return payment_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settle_verified_wallet_deposit(self, *, payment_id: str, verified_amount_minor: int,
                                       verified_currency: str, external_reference: str,
                                       verified_by: int | None = None) -> int:
        """Atomically mark a pre-verified payment and credit its wallet exactly once.

        Call only after a trusted provider adapter has verified signature, merchant, amount,
        currency, and final settlement status. This method itself is not a gateway verifier.
        """
        external_reference = external_reference.strip()
        if not external_reference or len(external_reference) > 256:
            raise CustomerStoreError("Verified external payment reference is required and must be at most 256 characters")
        if isinstance(verified_amount_minor, bool) or not isinstance(verified_amount_minor, int) or not 0 < verified_amount_minor <= 2**63 - 1:
            raise CustomerStoreError("Verified payment amount must be a positive signed 64-bit integer")
        verified_currency = verified_currency.upper()
        if len(verified_currency) != 3 or not verified_currency.isascii() or not verified_currency.isalpha():
            raise CustomerStoreError("Verified payment currency must be a three-letter ASCII code")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT telegram_id,amount_minor,currency,status,provider,external_reference
                   FROM payment_attempts WHERE tenant_id=? AND payment_id=?""",
                (self.tenant_id, payment_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Payment attempt not found")
            if row["status"] == "verified":
                if (row["external_reference"] == external_reference
                        and int(row["amount_minor"]) == verified_amount_minor
                        and row["currency"] == verified_currency):
                    existing = db.execute(
                        "SELECT ledger_id FROM wallet_ledger WHERE tenant_id=? AND idempotency_key=?",
                        (self.tenant_id, f"payment:{payment_id}"),
                    ).fetchone()
                    if existing:
                        db.commit()
                        return int(existing[0])
                raise CustomerStoreError("Payment is already settled with different data")
            if row["status"] != "pending":
                raise CustomerStoreError("Payment is not pending")
            if int(row["amount_minor"]) != verified_amount_minor or row["currency"] != verified_currency:
                raise CustomerStoreError("Verified payment amount or currency does not match intent")
            duplicate_reference = db.execute(
                """SELECT payment_id FROM payment_attempts
                   WHERE tenant_id=? AND provider=? AND external_reference=? AND payment_id<>?""",
                (self.tenant_id, row["provider"], external_reference, payment_id),
            ).fetchone()
            if duplicate_reference:
                raise CustomerStoreError("External payment reference is already assigned to another payment")
            db.execute(
                """INSERT INTO wallet_ledger
                   (tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                   VALUES(?,?,?,?,?,'deposit_verified',?,?)""",
                (self.tenant_id, int(row["telegram_id"]), f"payment:{payment_id}",
                 verified_amount_minor, verified_currency, external_reference[:256],
                 f"Manually verified payment by admin {verified_by}" if verified_by is not None else "Verified payment settlement"),
            )
            ledger_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._enqueue_trust_event_in_transaction(db, event_type="wallet_deposit",
                idempotency_key=f"wallet-credit:payment:{payment_id}",
                payload={"public_order_id": payment_id[:20], "amount_minor": verified_amount_minor,
                         "currency": verified_currency})
            cur = db.execute(
                """UPDATE payment_attempts SET status='verified',external_reference=?,
                   updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND payment_id=? AND status='pending'""",
                (external_reference[:256], self.tenant_id, payment_id),
            )
            if cur.rowcount != 1:
                raise CustomerStoreError("Payment status changed during settlement")
            db.execute("""INSERT INTO payment_attempt_events
                (tenant_id,payment_id,event_type,actor_telegram_id,amount_minor,currency,external_reference)
                VALUES(?,?,'verified',?,?,?,?)""",
                (self.tenant_id, payment_id, verified_by, verified_amount_minor, verified_currency, external_reference[:256]))
            self._enqueue_admin_notification_in_transaction(db, event_key="payment",
                idempotency_key=f"payment:{payment_id}",
                payload={"public_order_id": payment_id[:20], "amount_minor": verified_amount_minor,
                         "currency": verified_currency, "status": "verified", "customer_id": int(row["telegram_id"])})
            self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                idempotency_key=f"payment-wallet:{payment_id}",
                payload={"amount_minor": verified_amount_minor, "currency": verified_currency,
                         "entry_type": "deposit_verified", "customer_id": int(row["telegram_id"]), "status": "credited"})
            db.commit()
            return ledger_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_payment_attempt(self, payment_id: str, telegram_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT payment_id,telegram_id,provider,amount_minor,currency,status,idempotency_key,external_reference,failure_code,created_at,updated_at FROM payment_attempts WHERE tenant_id=? AND payment_id=?"
        args: tuple[Any, ...] = (self.tenant_id, payment_id)
        if telegram_id is not None:
            sql += " AND telegram_id=?"; args += (telegram_id,)
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def list_payment_attempt_events(self, payment_id: str) -> list[dict[str, Any]]:
        """Return tenant-scoped immutable lifecycle events for a payment attempt."""
        with self._connect() as db:
            rows = db.execute("""SELECT event_id,payment_id,event_type,actor_telegram_id,amount_minor,
                currency,external_reference,failure_code,created_at FROM payment_attempt_events
                WHERE tenant_id=? AND payment_id=? ORDER BY event_id""", (self.tenant_id, payment_id)).fetchall()
        return [dict(row) for row in rows]

    def fail_payment_attempt(self, *, payment_id: str, failure_code: str) -> None:
        """Fail only a pending payment, atomically; never overwrite a verified settlement."""
        db = self._connect()
        try:
            # Serialize with settlement so a stale read cannot overwrite a verified payment.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM payment_attempts WHERE tenant_id=? AND payment_id=?",
                (self.tenant_id, payment_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Payment attempt not found")
            if row["status"] == "failed":
                db.commit()
                return
            if row["status"] != "pending":
                raise CustomerStoreError("Only pending payments can be failed")
            cur = db.execute(
                """UPDATE payment_attempts
                   SET status='failed',failure_code=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND payment_id=? AND status='pending'""",
                (failure_code[:128], self.tenant_id, payment_id),
            )
            if cur.rowcount != 1:
                raise CustomerStoreError("Payment state changed; refusing to mark it failed")
            db.execute("""INSERT INTO payment_attempt_events
                (tenant_id,payment_id,event_type,amount_minor,currency,failure_code)
                SELECT tenant_id,payment_id,'failed',amount_minor,currency,?
                FROM payment_attempts WHERE tenant_id=? AND payment_id=?""",
                (failure_code[:128], self.tenant_id, payment_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def outstanding_debt(self, telegram_id: int, currency: str = "IRR") -> int:
        with self._connect() as db:
            services = int(db.execute("SELECT COALESCE(SUM(outstanding_debt_minor),0) FROM customer_services WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, telegram_id, currency.upper())).fetchone()[0])
            resellers = int(db.execute("SELECT COALESCE(SUM(outstanding_debt_minor),0) FROM reseller_accounts WHERE tenant_id=? AND owner_telegram_id=? AND currency=?", (self.tenant_id, telegram_id, currency.upper())).fetchone()[0])
        return services + resellers

    def wallet_balance(self, telegram_id: int, currency: str = "IRR") -> int:
        with self._connect() as db:
            row = db.execute(
                """SELECT COALESCE(SUM(amount_minor),0) AS balance FROM wallet_ledger
                   WHERE tenant_id=? AND telegram_id=? AND currency=?""",
                (self.tenant_id, telegram_id, currency.upper()),
            ).fetchone()
        return int(row["balance"])

    def wallet_balances(self, telegram_id: int) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT currency,COALESCE(SUM(amount_minor),0) AS balance FROM wallet_ledger
                   WHERE tenant_id=? AND telegram_id=? GROUP BY currency ORDER BY currency""",
                (self.tenant_id, telegram_id),
            ).fetchall()
        return {str(row["currency"]): int(row["balance"]) for row in rows if int(row["balance"]) != 0}

    def list_wallet_transactions(self, telegram_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return customer-safe wallet history; never expose notes, references or idempotency keys."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise CustomerStoreError("Invalid customer ID")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Invalid wallet history limit")
        with self._connect() as db:
            rows = db.execute(
                """SELECT ledger_id,amount_minor,currency,entry_type,created_at
                   FROM wallet_ledger WHERE tenant_id=? AND telegram_id=?
                   ORDER BY ledger_id DESC LIMIT ?""",
                (self.tenant_id, telegram_id, limit),
            ).fetchall()
        return [{ "entry_id": int(row["ledger_id"]), "amount_minor": int(row["amount_minor"]),
                  "currency": str(row["currency"]), "entry_type": str(row["entry_type"]),
                  "created_at": str(row["created_at"]) } for row in rows]

    def outstanding_debts(self, telegram_id: int) -> dict[str, int]:
        with self._connect() as db:
            service_rows = db.execute(
                """SELECT currency,COALESCE(SUM(outstanding_debt_minor),0) AS debt FROM customer_services
                   WHERE tenant_id=? AND telegram_id=? GROUP BY currency""", (self.tenant_id, telegram_id),
            ).fetchall()
            reseller_rows = db.execute(
                """SELECT currency,COALESCE(SUM(outstanding_debt_minor),0) AS debt FROM reseller_accounts
                   WHERE tenant_id=? AND owner_telegram_id=? GROUP BY currency""", (self.tenant_id, telegram_id),
            ).fetchall()
        result: dict[str, int] = {}
        for row in [*service_rows, *reseller_rows]:
            result[str(row["currency"])] = result.get(str(row["currency"]), 0) + int(row["debt"])
        return {currency: amount for currency, amount in sorted(result.items()) if amount > 0}

    def post_wallet_entry(self, *, telegram_id: int, idempotency_key: str,
                          amount_minor: int, currency: str = "IRR", entry_type: str,
                          reference: str = "", note: str = "",
                          allow_negative: bool = False) -> int:
        if not idempotency_key.strip() or not entry_type.strip():
            raise CustomerStoreError("Idempotency key and entry type are required")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor == 0 or abs(amount_minor) > 2**63 - 1:
            raise CustomerStoreError("Ledger amount must be a non-zero signed 64-bit integer")
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                """SELECT telegram_id,amount_minor,currency,entry_type,reference,ledger_id
                   FROM wallet_ledger WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if prior:
                same = (int(prior["telegram_id"]) == telegram_id
                        and int(prior["amount_minor"]) == amount_minor
                        and prior["currency"] == currency and prior["entry_type"] == entry_type
                        and prior["reference"] == reference[:256])
                if not same:
                    raise CustomerStoreError("Idempotency key reused with different transaction data")
                db.commit()
                return int(prior["ledger_id"])
            exists = db.execute(
                "SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?",
                (self.tenant_id, telegram_id),
            ).fetchone()
            if not exists:
                raise CustomerStoreError("Customer must be registered before wallet operations")
            balance = int(db.execute(
                """SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger
                   WHERE tenant_id=? AND telegram_id=? AND currency=?""",
                (self.tenant_id, telegram_id, currency),
            ).fetchone()[0])
            if amount_minor < 0 and not allow_negative and balance + amount_minor < 0:
                raise CustomerStoreError("Insufficient wallet balance")
            cur = db.execute(
                """INSERT INTO wallet_ledger
                   (tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.tenant_id, telegram_id, idempotency_key, amount_minor, currency,
                 entry_type, reference[:256], note[:1000]),
            )
            ledger_id = int(cur.lastrowid)
            if amount_minor > 0 and entry_type in {"admin_credit", "deposit_verified"}:
                self._enqueue_trust_event_in_transaction(db, event_type="wallet_deposit",
                    idempotency_key=f"wallet-credit:{reference or idempotency_key}",
                    payload={"public_order_id": (reference or idempotency_key)[:20],
                             "amount_minor": amount_minor, "currency": currency})
            self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                idempotency_key=f"wallet:{idempotency_key}",
                payload={"amount_minor": amount_minor, "currency": currency, "entry_type": entry_type,
                         "customer_id": telegram_id, "status": "posted"})
            db.commit()
            return ledger_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_services(self, telegram_id: int) -> list[dict[str, Any]]:
        """Return only services owned by this Telegram customer in this tenant."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT service_id,product_id,panel_id,provider_user_id,username,subscription_url,reseller_account_id,status,expires_at,
                          traffic_limit_bytes,traffic_used_bytes,pricing_mode,currency,unit_price_minor,billing_status,outstanding_debt_minor,provider_checked_at
                   FROM customer_services WHERE tenant_id=? AND telegram_id=?
                   ORDER BY created_at DESC""",
                (self.tenant_id, telegram_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_service(self, *, service_id: str, telegram_id: int, product_id: str = "",
                       panel_id: str = "", provider_user_id: str = "", status: str,
                       expires_at: str | None = None, traffic_limit_bytes: int = 0,
                       traffic_used_bytes: int = 0, provider_checked_at: str | None = None) -> None:
        allowed = {"pending", "active", "limited", "expired", "suspended", "failed", "deleted"}
        if not service_id.strip() or status not in allowed:
            raise CustomerStoreError("Service ID or status is invalid")
        if traffic_limit_bytes < 0 or traffic_used_bytes < 0:
            raise CustomerStoreError("Traffic counters cannot be negative")
        with self._connect() as db:
            db.execute(
                """INSERT INTO customer_services
                   (tenant_id,service_id,telegram_id,product_id,panel_id,provider_user_id,status,
                    expires_at,traffic_limit_bytes,traffic_used_bytes,provider_checked_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,service_id) DO UPDATE SET
                    telegram_id=excluded.telegram_id,product_id=excluded.product_id,
                    panel_id=excluded.panel_id,provider_user_id=excluded.provider_user_id,
                    status=excluded.status,expires_at=excluded.expires_at,
                    traffic_limit_bytes=excluded.traffic_limit_bytes,
                    traffic_used_bytes=excluded.traffic_used_bytes,
                    provider_checked_at=excluded.provider_checked_at,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, service_id, telegram_id, product_id, panel_id, provider_user_id,
                 status, expires_at, traffic_limit_bytes, traffic_used_bytes, provider_checked_at),
            )

    def put_reseller_plan(self, *, plan_id: str, panel_id: str, title: str,
                          pricing_mode: str, price_minor: int, unit_price_minor: int = 0,
                          currency: str = "IRR", duration_hours: int = 0,
                          data_limit_bytes: int = 0, max_users: int = 0,
                          min_volume_gib: str = "0", max_volume_gib: str = "0",
                          volume_step_gib: str = "1", provider_role_id: str = "",
                          allowed_group_ids: list[str] | tuple[str, ...] = (),
                          permission_overrides: dict[str, bool] | None = None,
                          addon_hour_price_minor: int = 0, addon_gib_price_minor: int = 0,
                          addon_user_price_minor: int = 0, display_label: str = "",
                          display_icon: str = "", enabled: bool = True) -> None:
        from decimal import Decimal, InvalidOperation
        modes = {"fixed", "unlimited", "hourly", "usage", "per_gb", "per_tb"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", plan_id.strip()) or not panel_id.strip() or not title.strip() or pricing_mode not in modes:
            raise CustomerStoreError("Plan ID must use 1..24 ASCII letters/digits/_/-, and panel, title and mode are required")
        integer_values = (price_minor, unit_price_minor, duration_hours, data_limit_bytes,
                          max_users, addon_hour_price_minor, addon_gib_price_minor, addon_user_price_minor)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 2**63 - 1 for v in integer_values):
            raise CustomerStoreError("Plan prices, duration and limits must be non-negative signed 64-bit integers")
        if duration_hours > 87600:
            raise CustomerStoreError("Plan duration cannot exceed 87600 hours")
        if enabled and pricing_mode in {"fixed", "unlimited"} and price_minor <= 0:
            raise CustomerStoreError("Fixed and unlimited plans require a positive prepaid price")
        if pricing_mode in {"hourly", "usage", "per_gb", "per_tb"} and unit_price_minor <= 0:
            raise CustomerStoreError("Metered plans require a positive unit price")
        try:
            minimum, maximum, step = map(Decimal, (min_volume_gib, max_volume_gib, volume_step_gib))
        except InvalidOperation:
            raise CustomerStoreError("Volume limits must be decimal numbers") from None
        if any(not x.is_finite() or x < 0 for x in (minimum, maximum, step)):
            raise CustomerStoreError("Volume limits must be finite and non-negative")
        if maximum and minimum > maximum:
            raise CustomerStoreError("Minimum volume cannot exceed maximum volume")
        if (minimum or maximum) and step <= 0:
            raise CustomerStoreError("Volume step must be positive when volume bounds are set")
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        with self._connect() as db:
            panel = db.execute("SELECT 1 FROM panel_connections WHERE tenant_id=? AND panel_id=?", (self.tenant_id, panel_id)).fetchone()
            if not panel:
                raise CustomerStoreError("Register the panel connection before creating its reseller plan")
            db.execute(
                """INSERT INTO reseller_plans
                   (tenant_id,plan_id,panel_id,title,pricing_mode,price_minor,unit_price_minor,currency,
                    duration_hours,data_limit_bytes,max_users,min_volume_gib,max_volume_gib,volume_step_gib,
                    provider_role_id,allowed_group_ids_json,permission_overrides_json,addon_hour_price_minor,
                    addon_gib_price_minor,addon_user_price_minor,display_label,display_icon,enabled)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,plan_id) DO UPDATE SET panel_id=excluded.panel_id,title=excluded.title,
                    pricing_mode=excluded.pricing_mode,price_minor=excluded.price_minor,
                    unit_price_minor=excluded.unit_price_minor,currency=excluded.currency,
                    duration_hours=excluded.duration_hours,data_limit_bytes=excluded.data_limit_bytes,
                    max_users=excluded.max_users,min_volume_gib=excluded.min_volume_gib,
                    max_volume_gib=excluded.max_volume_gib,volume_step_gib=excluded.volume_step_gib,
                    provider_role_id=excluded.provider_role_id,allowed_group_ids_json=excluded.allowed_group_ids_json,
                    permission_overrides_json=excluded.permission_overrides_json,
                    addon_hour_price_minor=excluded.addon_hour_price_minor,addon_gib_price_minor=excluded.addon_gib_price_minor,
                    addon_user_price_minor=excluded.addon_user_price_minor,display_label=excluded.display_label,
                    display_icon=excluded.display_icon,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, plan_id.strip(), panel_id.strip(), title.strip(), pricing_mode, price_minor,
                 unit_price_minor, currency, duration_hours, data_limit_bytes, max_users, str(minimum),
                 str(maximum), str(step), provider_role_id, json.dumps(list(allowed_group_ids)),
                 json.dumps(permission_overrides or {}, sort_keys=True), addon_hour_price_minor,
                 addon_gib_price_minor, addon_user_price_minor, display_label[:160], display_icon[:32], int(enabled)),
            )

    def list_reseller_plans(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reseller_plans WHERE tenant_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at,plan_id"
        with self._connect() as db:
            rows = db.execute(sql, (self.tenant_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["allowed_group_ids"] = json.loads(item.pop("allowed_group_ids_json"))
            item["permission_overrides"] = json.loads(item.pop("permission_overrides_json"))
            result.append(item)
        return result

    def get_reseller_plan(self, plan_id: str, *, enabled_only: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM reseller_plans WHERE tenant_id=? AND plan_id=?"
        args: tuple[Any, ...] = (self.tenant_id, plan_id)
        if enabled_only: sql += " AND enabled=1"
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        if not row: return None
        item = dict(row)
        item["allowed_group_ids"] = json.loads(item.pop("allowed_group_ids_json"))
        item["permission_overrides"] = json.loads(item.pop("permission_overrides_json"))
        return item

    def set_reseller_plan_enabled(self, plan_id: str, enabled: bool) -> None:
        with self._connect() as db:
            cur = db.execute("UPDATE reseller_plans SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND plan_id=?", (int(enabled), self.tenant_id, plan_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Reseller plan not found")

    def register_panel_connection(self, *, panel_id: str, label: str, provider: str,
                                  base_url: str, auth_mode: str, secret_ref: str,
                                  enabled: bool = False, verify_tls: bool = True,
                                  metadata: dict[str, Any] | None = None) -> None:
        from urllib.parse import urlsplit
        parsed = urlsplit(base_url.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise CustomerStoreError("Panel URL must be HTTPS and must not contain embedded credentials")
        if not verify_tls:
            raise CustomerStoreError("TLS verification cannot be disabled")
        provider = provider.strip().lower()
        supported_auth = {"pasarguard": {"api_key", "username_password"},
                          "rebecca": {"api_key"},
                          "sanaei": {"access_token", "username_password"}}
        if provider not in supported_auth:
            raise CustomerStoreError("Only PasarGuard, Rebecca and Sanaei panel adapters are enabled")
        if auth_mode not in supported_auth[provider]:
            raise CustomerStoreError(f"Authentication mode {auth_mode} is not supported for {provider}")
        if not all(x.strip() for x in (panel_id, label, provider, secret_ref)):
            raise CustomerStoreError("Panel ID, label, provider and secret reference are required")
        if not re.fullmatch(r"env:[A-Z][A-Z0-9_]{1,127}", secret_ref.strip()):
            raise CustomerStoreError("Panel credentials must reference an environment variable using env:VARIABLE_NAME")
        forbidden = {"password", "token", "api_key", "secret", "client_secret", "access_token", "authorization", "cookie"}
        def check_metadata(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).lower() in forbidden:
                        raise CustomerStoreError("Panel metadata must not contain credentials or tokens")
                    check_metadata(child)
            elif isinstance(value, list):
                for child in value: check_metadata(child)
        check_metadata(metadata or {})
        if provider == "sanaei" and any(
            key in (metadata or {}) for key in ("subscription_base_url", "subscription_path")
        ):
            panel_meta = metadata or {}
            subscription_base = panel_meta.get("subscription_base_url")
            subscription_path = panel_meta.get("subscription_path")
            if not isinstance(subscription_base, str) or not isinstance(subscription_path, str):
                raise CustomerStoreError("Sanaei subscription metadata requires subscription_base_url and subscription_path")
            if not subscription_base or len(subscription_base) > 2048 or any(ord(ch) < 32 for ch in subscription_base):
                raise CustomerStoreError("Sanaei subscription_base_url is invalid")
            try:
                subscription_parsed = urlsplit(subscription_base.strip())
                if (subscription_parsed.scheme.lower() != "https" or not subscription_parsed.hostname
                        or subscription_parsed.username or subscription_parsed.password
                        or subscription_parsed.path not in ("", "/")
                        or subscription_parsed.query or subscription_parsed.fragment):
                    raise CustomerStoreError("Sanaei subscription_base_url must be an HTTPS origin without credentials or path")
                _ = subscription_parsed.port
            except ValueError:
                raise CustomerStoreError("Sanaei subscription_base_url is invalid") from None
            subscription_path = subscription_path.strip().strip("/")
            if (not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", subscription_path)
                    or subscription_path in {".", ".."}):
                raise CustomerStoreError("Sanaei subscription_path must be one safe path segment")
        with self._connect() as db:
            db.execute(
                """INSERT INTO panel_connections
                   (tenant_id,panel_id,label,provider,base_url,auth_mode,secret_ref,enabled,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,panel_id) DO UPDATE SET
                   label=excluded.label,provider=excluded.provider,base_url=excluded.base_url,
                   auth_mode=excluded.auth_mode,secret_ref=excluded.secret_ref,enabled=excluded.enabled,
                   metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, panel_id.strip(), label.strip(), provider.strip().lower(),
                 base_url.strip().rstrip("/"), auth_mode, secret_ref.strip(), int(enabled),
                 json.dumps(metadata or {}, sort_keys=True)),
            )

    def list_panel_connections(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT panel_id,label,provider,base_url,auth_mode,enabled,last_check_at,last_check_status FROM panel_connections WHERE tenant_id=? ORDER BY panel_id",
                (self.tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_trust_channel_config(self, *, bot_instance_id: str, chat_id: str, enabled: bool,
                                 enabled_events: list[str] | tuple[str, ...], privacy_mode: bool = True) -> None:
        from .notifications.trust_channel import TrustChannelConfig, TrustEvent
        normalized = frozenset(TrustEvent(event) for event in enabled_events)
        TrustChannelConfig(self.tenant_id, bot_instance_id, chat_id, enabled, normalized, privacy_mode)
        with self._connect() as db:
            db.execute(
                """INSERT INTO trust_channel_settings(tenant_id,bot_instance_id,chat_id,enabled,enabled_events_json,privacy_mode)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,bot_instance_id) DO UPDATE SET
                   chat_id=excluded.chat_id,enabled=excluded.enabled,enabled_events_json=excluded.enabled_events_json,
                   privacy_mode=excluded.privacy_mode,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, bot_instance_id, chat_id, int(enabled), json.dumps(sorted(e.value for e in normalized)), int(privacy_mode)),
            )

    def get_trust_channel_config(self, tenant_id: str | None = None, bot_instance_id: str = "customer") -> Any | None:
        from .notifications.trust_channel import TrustChannelConfig, TrustEvent
        target_tenant = tenant_id or self.tenant_id
        if target_tenant != self.tenant_id:
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM trust_channel_settings WHERE tenant_id=? AND bot_instance_id=?", (target_tenant, bot_instance_id)).fetchone()
        if not row: return None
        try:
            events = frozenset(TrustEvent(event) for event in json.loads(row["enabled_events_json"] or "[]"))
            return TrustChannelConfig(target_tenant, bot_instance_id, row["chat_id"], bool(row["enabled"]), events, bool(row["privacy_mode"]))
        except (ValueError, TypeError):
            return None

    def set_panel_enabled(self, panel_id: str, enabled: bool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", panel_id.strip()):
            raise CustomerStoreError("Invalid panel ID")
        with self._connect() as db:
            cur = db.execute("UPDATE panel_connections SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND panel_id=?",
                             (int(enabled), self.tenant_id, panel_id.strip()))
            if cur.rowcount != 1:
                raise CustomerStoreError("Panel connection not found")

    def record_panel_check(self, *, panel_id: str, status: str, capabilities: dict[str, Any]) -> None:
        if status not in {"healthy_read_only", "failed", "not_tested"}:
            raise CustomerStoreError("Invalid panel check status")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE panel_connections SET last_check_at=CURRENT_TIMESTAMP,last_check_status=?,capability_json=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND panel_id=?",
                (status, json.dumps(capabilities, sort_keys=True), self.tenant_id, panel_id),
            )
            if cur.rowcount != 1:
                raise CustomerStoreError("Panel connection not found")

    def get_panel_connection(self, panel_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM panel_connections WHERE tenant_id=? AND panel_id=?", (self.tenant_id, panel_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json", "{}"))
        result.pop("capability_json", None)
        return result

    def put_discount_code(self, *, code: str, percentage: int, max_uses: int = 0,
                          expires_at: int | None = None,
                          allowed_product_ids: tuple[str, ...] | list[str] = (),
                          enabled: bool = True) -> None:
        """Create/update a tenant-scoped percentage discount. max_uses=0 means unlimited."""
        normalized = code.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,64}", normalized):
            raise CustomerStoreError("Discount code must contain 2..64 ASCII letters, digits, _ or -")
        if isinstance(percentage, bool) or not isinstance(percentage, int) or not 1 <= percentage <= 100:
            raise CustomerStoreError("Discount percentage must be an integer from 1 to 100")
        if isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 0:
            raise CustomerStoreError("Discount usage limit must be a non-negative integer")
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0):
            raise CustomerStoreError("Discount expiry must be a positive Unix timestamp")
        products = sorted({str(item).strip() for item in allowed_product_ids})
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", item) for item in products):
            raise CustomerStoreError("Discount product IDs are invalid")
        with self._connect() as db:
            for product_id in products:
                if not db.execute("SELECT 1 FROM customer_products WHERE tenant_id=? AND product_id=?",
                                  (self.tenant_id, product_id)).fetchone():
                    raise CustomerStoreError("Discount references an unknown product")
            db.execute("""INSERT INTO discount_codes
                (tenant_id,code,percentage,max_uses,expires_at,enabled,allowed_product_ids_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,code) DO UPDATE SET
                percentage=excluded.percentage,max_uses=excluded.max_uses,expires_at=excluded.expires_at,
                enabled=excluded.enabled,allowed_product_ids_json=excluded.allowed_product_ids_json,
                updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, normalized, percentage, max_uses, expires_at, int(enabled),
                 json.dumps(products, separators=(",", ":"))))

    def get_discount_code(self, code: str) -> dict[str, Any] | None:
        normalized = code.strip().upper()
        with self._connect() as db:
            row = db.execute("SELECT code,percentage,max_uses,used_count,expires_at,enabled,allowed_product_ids_json FROM discount_codes WHERE tenant_id=? AND code=?",
                             (self.tenant_id, normalized)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["allowed_product_ids"] = json.loads(result.pop("allowed_product_ids_json") or "[]")
        return result

    def list_discount_codes(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT code,percentage,max_uses,used_count,expires_at,enabled,
                allowed_product_ids_json FROM discount_codes WHERE tenant_id=? ORDER BY code""",
                (self.tenant_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["allowed_product_ids"] = json.loads(item.pop("allowed_product_ids_json") or "[]")
            result.append(item)
        return result

    def set_discount_code_enabled(self, code: str, enabled: bool) -> None:
        normalized = code.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,64}", normalized):
            raise CustomerStoreError("Invalid discount code")
        with self._connect() as db:
            cur = db.execute("UPDATE discount_codes SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND code=?",
                             (int(enabled), self.tenant_id, normalized))
            if cur.rowcount != 1:
                raise CustomerStoreError("Discount code not found")

    def reserve_product_order(self, *, order_id: str, telegram_id: int, product_id: str,
                              idempotency_key: str, currency: str = "IRR",
                              reseller_account_id: str | None = None,
                              discount_code: str | None = None,
                              now: int | None = None) -> dict[str, Any]:
        """Reserve a customer order atomically; reseller child configs are covered by the plan."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM customer_orders WHERE tenant_id=? AND idempotency_key=?", (self.tenant_id, idempotency_key)).fetchone()
            if prior:
                if (int(prior["telegram_id"]) != telegram_id or prior["product_id"] != product_id
                        or str(prior["reseller_account_id"] or "") != str(reseller_account_id or "")
                        or str(prior["discount_code"] or "") != str(discount_code or "").strip().upper()):
                    raise CustomerStoreError("Order idempotency key reused for a different purchase")
                result = dict(prior)
                result.update(json.loads(prior["product_snapshot_json"] or "{}"))
                result.update({"order_id": prior["order_id"], "amount_minor": int(prior["amount_minor"]),
                    "currency": prior["currency"], "status": prior["status"], "idempotent_replay": True,
                    "reseller_account_id": str(prior["reseller_account_id"] or "")})
                db.commit(); return result
            product = db.execute("SELECT * FROM customer_products WHERE tenant_id=? AND product_id=? AND enabled=1", (self.tenant_id, product_id)).fetchone()
            if not product: raise CustomerStoreError("Product is unavailable")
            panel_state = db.execute("SELECT enabled FROM panel_connections WHERE tenant_id=? AND panel_id=?", (self.tenant_id, product["panel_id"])).fetchone()
            if not panel_state or not panel_state["enabled"]: raise CustomerStoreError("Product panel is disabled; no wallet debit was made")
            customer = db.execute("SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?", (self.tenant_id, telegram_id)).fetchone()
            if not customer: raise CustomerStoreError("Customer must be registered before ordering")
            currency = str(product["currency"])
            charge = int(product["price_minor"])
            if not reseller_account_id:
                debt = int(db.execute("SELECT COALESCE(SUM(outstanding_debt_minor),0) FROM customer_services WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, telegram_id, currency)).fetchone()[0])
                debt += int(db.execute("SELECT COALESCE(SUM(outstanding_debt_minor),0) FROM reseller_accounts WHERE tenant_id=? AND owner_telegram_id=? AND currency=?", (self.tenant_id, telegram_id, currency)).fetchone()[0])
                if debt > 0: raise CustomerStoreError("Settle outstanding metered debt before buying another product")
            reseller_account = None
            if reseller_account_id:
                reseller_account = db.execute("SELECT * FROM reseller_accounts WHERE tenant_id=? AND account_id=? AND owner_telegram_id=?", (self.tenant_id, reseller_account_id, telegram_id)).fetchone()
                if not reseller_account or reseller_account["status"] != "active":
                    raise CustomerStoreError("Reseller account is unavailable or suspended")
                if reseller_account["panel_id"] != product["panel_id"]:
                    raise CustomerStoreError("Reseller account and product must use the same panel")
                if reseller_account["expires_at"]:
                    from datetime import datetime, timezone
                    if datetime.fromisoformat(reseller_account["expires_at"]) <= datetime.now(timezone.utc):
                        raise CustomerStoreError("Reseller account has expired")
                plan_groups_row = db.execute("SELECT allowed_group_ids_json FROM reseller_plans WHERE tenant_id=? AND plan_id=?", (self.tenant_id, reseller_account["plan_id"])).fetchone()
                allowed_groups = {str(value) for value in (json.loads(plan_groups_row["allowed_group_ids_json"] or "[]") if plan_groups_row else [])}
                if allowed_groups:
                    product_groups = {str(value) for value in json.loads(product["group_ids_json"] or "[]")}
                    if not product_groups or not product_groups.issubset(allowed_groups):
                        raise CustomerStoreError("Product groups exceed the reseller plan's allowed group IDs")
                max_users = int(reseller_account["max_users"] or 0)
                if max_users > 0:
                    count = int(db.execute("SELECT COUNT(*) FROM customer_orders WHERE tenant_id=? AND reseller_account_id=? AND status IN ('paid','provisioning','active')", (self.tenant_id, reseller_account_id)).fetchone()[0])
                    if count >= max_users:
                        raise CustomerStoreError("Reseller plan has reached its maximum active service count")
                charge = 0
            normalized_discount = str(discount_code or "").strip().upper()
            discount_amount = 0
            if normalized_discount:
                if reseller_account_id:
                    raise CustomerStoreError("Discount codes cannot be applied to reseller-covered child configurations")
                if charge <= 0:
                    raise CustomerStoreError("Discount codes require a positive product price")
                current_time = int(time.time()) if now is None else int(now)
                discount = db.execute("SELECT * FROM discount_codes WHERE tenant_id=? AND code=?",
                                      (self.tenant_id, normalized_discount)).fetchone()
                if not discount or not discount["enabled"]:
                    raise CustomerStoreError("Discount code is invalid or disabled")
                if discount["expires_at"] is not None and int(discount["expires_at"]) <= current_time:
                    raise CustomerStoreError("Discount code has expired")
                allowed_products = json.loads(discount["allowed_product_ids_json"] or "[]")
                if allowed_products and product_id not in allowed_products:
                    raise CustomerStoreError("Discount code does not apply to this product")
                if int(discount["max_uses"]) > 0 and int(discount["used_count"]) >= int(discount["max_uses"]):
                    raise CustomerStoreError("Discount code usage limit has been reached")
                discount_amount = charge * int(discount["percentage"]) // 100
                charge -= discount_amount
                claimed = db.execute("""UPDATE discount_codes SET used_count=used_count+1,updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=? AND code=? AND enabled=1
                    AND (expires_at IS NULL OR expires_at>?)
                    AND (max_uses=0 OR used_count<max_uses)""",
                    (self.tenant_id, normalized_discount, current_time))
                if claimed.rowcount != 1:
                    raise CustomerStoreError("Discount code was consumed or expired; refresh and retry")
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, telegram_id, currency)).fetchone()[0])
            if balance < charge: raise CustomerStoreError("Insufficient wallet balance")
            if reseller_account is not None and reseller_account["pricing_mode"] in {"hourly", "usage"} and balance <= 0:
                raise CustomerStoreError("Hourly/usage reseller plans require a positive wallet balance")
            if reseller_account is None and str(product["pricing_mode"]) in {"hourly", "usage"} and balance - charge <= 0:
                raise CustomerStoreError("Hourly/usage purchases require a positive wallet balance after the setup charge")
            initial_status = "paid" if charge > 0 else "provisioning"
            snapshot_json = json.dumps(dict(product), ensure_ascii=False, separators=(",", ":"))
            db.execute(
                """INSERT INTO customer_orders(tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key,product_snapshot_json,reseller_account_id,discount_code,discount_amount_minor)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, order_id, telegram_id, product_id, charge, currency, initial_status,
                 idempotency_key, snapshot_json, reseller_account_id or "", normalized_discount, discount_amount),
            )
            if charge > 0:
                db.execute(
                    """INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'purchase',?,?)""",
                    (self.tenant_id, telegram_id, f"order:{order_id}:debit", -charge, currency, order_id,
                     "Reseller plan covered configuration" if reseller_account_id else "Customer product purchase reservation"),
                )
            db.execute("UPDATE customer_orders SET status='provisioning',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND order_id=?", (self.tenant_id, order_id))
            self._enqueue_admin_notification_in_transaction(db, event_key="purchase",
                idempotency_key=f"order:{order_id}",
                payload={"public_order_id": order_id[:20], "product_title": str(product["title"]),
                         "amount_minor": charge, "currency": currency, "status": "provisioning",
                         "customer_id": telegram_id})
            if charge > 0:
                self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                    idempotency_key=f"order-wallet:{order_id}",
                    payload={"amount_minor": -charge, "currency": currency, "entry_type": "purchase",
                             "customer_id": telegram_id, "status": "debited"})
            db.commit()
            result = dict(product)
            result.update({"order_id": order_id, "telegram_id": telegram_id, "amount_minor": charge,
                "currency": currency, "status": "provisioning", "idempotency_key": idempotency_key,
                "idempotent_replay": False, "reseller_account_id": reseller_account_id or "",
                "discount_code": normalized_discount, "discount_amount_minor": discount_amount})
            return result
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def fail_product_order_and_refund(self, *, order_id: str, reason_code: str,
                                      release_discount_claim: bool = False) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM customer_orders WHERE tenant_id=? AND order_id=?", (self.tenant_id, order_id)).fetchone()
            if not order:
                raise CustomerStoreError("Order not found")
            if order["status"] == "refunded":
                db.commit(); return
            if order["status"] not in {"provisioning", "paid", "failed"}:
                raise CustomerStoreError("Order cannot be refunded from its current state")
            if int(order["amount_minor"]) > 0:
                db.execute(
                    """INSERT OR IGNORE INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'refund',?,?)""",
                    (self.tenant_id, int(order["telegram_id"]), f"order:{order_id}:refund", int(order["amount_minor"]),
                     order["currency"], order_id, reason_code[:100]),
                )
            if release_discount_claim and order["discount_code"]:
                db.execute("UPDATE discount_codes SET used_count=CASE WHEN used_count>0 THEN used_count-1 ELSE 0 END,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND code=?",
                           (self.tenant_id, order["discount_code"]))
            db.execute("UPDATE customer_orders SET status='refunded',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND order_id=?", (self.tenant_id, order_id))
            self._enqueue_admin_notification_in_transaction(db, event_key="payment",
                idempotency_key=f"refund:{order_id}",
                payload={"public_order_id": order_id[:20], "amount_minor": int(order["amount_minor"]),
                         "currency": str(order["currency"]), "status": "refunded",
                         "customer_id": int(order["telegram_id"]), "reason_code": reason_code[:80]})
            if int(order["amount_minor"]) > 0:
                self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                    idempotency_key=f"refund-wallet:{order_id}",
                    payload={"amount_minor": int(order["amount_minor"]), "currency": str(order["currency"]),
                             "entry_type": "refund", "customer_id": int(order["telegram_id"]),
                             "status": "credited"})
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def create_metered_service(self, *, service_id: str, telegram_id: int, product: dict[str, Any],
                               provider_user_id: str, username: str, subscription_url: str | None,
                               reseller_account_id: str = "", now: int) -> None:
        mode = str(product.get("pricing_mode", "fixed"))
        billing_status = "active" if mode in {"hourly", "usage"} else "not_metered"
        with self._connect() as db:
            db.execute(
                """INSERT INTO customer_services
                   (tenant_id,service_id,telegram_id,product_id,panel_id,provider_user_id,username,subscription_url,reseller_account_id,status,expires_at,
                    traffic_limit_bytes,traffic_used_bytes,pricing_mode,currency,unit_price_minor,billing_last_at,
                    billed_traffic_bytes,fractional_carry,billing_status,provider_checked_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(tenant_id,service_id) DO NOTHING""",
                (self.tenant_id, service_id, telegram_id, str(product.get("product_id", "")),
                 str(product.get("panel_id", "")), provider_user_id, username, subscription_url or "", reseller_account_id,
                 product.get("expires_at"), int(product.get("traffic_bytes", 0)), 0, mode,
                 str(product.get("currency", "IRR")), int(product.get("unit_price_minor", 0)),
                 now, 0, "0", billing_status),
            )
            existing = db.execute("""SELECT telegram_id,product_id,panel_id,provider_user_id,username,
                subscription_url,reseller_account_id FROM customer_services
                WHERE tenant_id=? AND service_id=?""", (self.tenant_id, service_id)).fetchone()
            if (not existing or int(existing["telegram_id"]) != telegram_id
                    or existing["product_id"] != str(product.get("product_id", ""))
                    or str(existing["panel_id"] or "") != str(product.get("panel_id", ""))
                    or str(existing["provider_user_id"] or "") != str(provider_user_id or "")
                    or str(existing["username"] or "") != str(username or "")
                    or str(existing["subscription_url"] or "") != str(subscription_url or "")
                    or str(existing["reseller_account_id"] or "") != str(reseller_account_id or "")):
                raise CustomerStoreError("Service ID is already bound to a different customer, provider identity, reseller or product")

    def update_customer_service_snapshot(self, *, service_id: str, telegram_id: int, panel_id: str,
                                         provider_user_id: str, fields: dict[str, Any]) -> bool:
        """Persist a validated read-only provider snapshot without changing billing state."""
        allowed = {"status", "expires_at", "traffic_limit_bytes", "traffic_used_bytes", "subscription_url"}
        if not fields or set(fields) - allowed:
            raise CustomerStoreError("Invalid provider snapshot fields")
        if "status" in fields and fields["status"] not in {
            "pending", "active", "limited", "expired", "suspended", "failed", "deleted", "disabled"
        }:
            raise CustomerStoreError("Invalid provider snapshot status")
        for key in ("traffic_limit_bytes", "traffic_used_bytes"):
            if key in fields and (isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0):
                raise CustomerStoreError("Invalid provider traffic counter")
        if "subscription_url" in fields and (not isinstance(fields["subscription_url"], str) or len(fields["subscription_url"]) > 4096):
            raise CustomerStoreError("Invalid provider subscription URL")
        columns = {"status": "status", "expires_at": "expires_at", "traffic_limit_bytes": "traffic_limit_bytes",
                   "traffic_used_bytes": "traffic_used_bytes", "subscription_url": "subscription_url"}
        assignments = ",".join(f"{columns[key]}=?" for key in fields)
        values = list(fields.values())
        values.extend([self.tenant_id, service_id, telegram_id, panel_id, provider_user_id])
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE customer_services SET {assignments},provider_checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND service_id=? AND telegram_id=? AND panel_id=? AND provider_user_id=?",
                values,
            )
        return cur.rowcount == 1

    def get_customer_service(self, service_id: str, telegram_id: int | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            if telegram_id is None:
                row = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=? AND telegram_id=?", (self.tenant_id, service_id, telegram_id)).fetchone()
        return dict(row) if row else None

    def reserve_service_change(self, *, operation_id: str, telegram_id: int, service_id: str,
                               change_type: str, idempotency_key: str, now: int,
                               expected_amount_minor: int | None = None,
                               expected_currency: str | None = None,
                               expected_quantity: int | None = None) -> dict[str, Any]:
        """Reserve wallet funds and persist a service-change intent before any provider mutation."""
        if change_type not in {"renewal", "traffic_topup"}:
            raise CustomerStoreError("Unsupported service change")
        if not operation_id or not idempotency_key or len(idempotency_key) > 200:
            raise CustomerStoreError("Valid operation and idempotency IDs are required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND idempotency_key=?",
                                  (self.tenant_id, idempotency_key)).fetchone()
            if previous:
                if int(previous["telegram_id"]) != telegram_id or previous["service_id"] != service_id or previous["change_type"] != change_type:
                    raise CustomerStoreError("Service-change idempotency key belongs to a different request")
                service = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=? AND telegram_id=?",
                                     (self.tenant_id, service_id, telegram_id)).fetchone()
                if ((expected_amount_minor is not None and int(previous["amount_minor"]) != int(expected_amount_minor))
                        or (expected_currency is not None and str(previous["currency"]) != str(expected_currency))
                        or (expected_quantity is not None and int(previous["quantity"]) != int(expected_quantity))):
                    raise CustomerStoreError("Service-change idempotency key reused with a different quote")
                db.commit()
                replay = {**dict(previous), "idempotent_replay": True}
                if service:
                    replay.update(panel_id=service["panel_id"], provider_user_id=service["provider_user_id"],
                        username=service["username"], expires_at=service["expires_at"],
                        traffic_limit_bytes=int(service["traffic_limit_bytes"] or 0),
                        traffic_used_bytes=int(service["traffic_used_bytes"] or 0), product_id=service["product_id"])
                return replay
            service = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=? AND telegram_id=?",
                                 (self.tenant_id, service_id, telegram_id)).fetchone()
            if not service:
                raise CustomerStoreError("Service not found for this customer")
            unresolved = db.execute(
                """SELECT operation_id, status FROM customer_service_operations
                   WHERE tenant_id=? AND service_id=? AND telegram_id=?
                     AND (status='pending' OR status LIKE 'reconcile_required:%')
                   ORDER BY created_at DESC LIMIT 1""",
                (self.tenant_id, service_id, telegram_id),
            ).fetchone()
            if unresolved:
                raise CustomerStoreError(
                    f"Service already has an unresolved change ({unresolved['operation_id']}); "
                    "check its status or ask support before starting another change"
                )
            if service["status"] not in {"active", "limited", "expired"}:
                raise CustomerStoreError("Service is not eligible for renewal or traffic top-up")
            product = db.execute("SELECT * FROM customer_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                                 (self.tenant_id, service["product_id"])).fetchone()
            if not product or product["panel_id"] != service["panel_id"]:
                raise CustomerStoreError("The original product is unavailable or no longer matches this service")
            quantity = int(product["duration_hours"] or 0) if change_type == "renewal" else int(product["traffic_bytes"] or 0)
            if quantity <= 0:
                raise CustomerStoreError("The original product has no valid renewal duration or traffic amount")
            amount = int(product["price_minor"])
            currency = str(product["currency"])
            if ((expected_amount_minor is not None and amount != int(expected_amount_minor))
                    or (expected_currency is not None and currency != str(expected_currency))
                    or (expected_quantity is not None and quantity != int(expected_quantity))):
                raise CustomerStoreError("The service-change quote changed; request a fresh preview before confirming")
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?",
                                     (self.tenant_id, telegram_id, currency)).fetchone()[0])
            if balance < amount:
                raise CustomerStoreError("Insufficient wallet balance")
            if amount > 0:
                db.execute("""INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                              VALUES(?,?,?,?,?,'service_change',?,?)""",
                           (self.tenant_id, telegram_id, f"service-change:{operation_id}:debit", -amount,
                            currency, operation_id, change_type))
            db.execute("""INSERT INTO customer_service_operations
                (tenant_id,operation_id,telegram_id,service_id,change_type,idempotency_key,amount_minor,currency,quantity,status)
                VALUES(?,?,?,?,?,?,?,?,?,'pending')""",
                (self.tenant_id, operation_id, telegram_id, service_id, change_type, idempotency_key, amount, currency, quantity))
            self._enqueue_admin_notification_in_transaction(db, event_key="service_change",
                idempotency_key=f"service-change-start:{operation_id}",
                payload={"public_order_id": operation_id[:20], "amount_minor": amount, "currency": currency,
                         "status": "pending", "customer_id": telegram_id, "reason_code": change_type})
            if amount > 0:
                self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                    idempotency_key=f"service-change-wallet:{operation_id}",
                    payload={"amount_minor": -amount, "currency": currency, "entry_type": "service_change",
                             "customer_id": telegram_id, "status": "debited"})
            db.commit()
            return {"operation_id": operation_id, "telegram_id": telegram_id, "service_id": service_id,
                    "change_type": change_type, "amount_minor": amount, "currency": currency,
                    "quantity": quantity, "status": "pending", "idempotent_replay": False,
                    "panel_id": service["panel_id"], "provider_user_id": service["provider_user_id"],
                    "username": service["username"], "expires_at": service["expires_at"],
                    "traffic_limit_bytes": int(service["traffic_limit_bytes"] or 0),
                    "traffic_used_bytes": int(service["traffic_used_bytes"] or 0),
                    "product_id": service["product_id"]}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def persist_service_change_target(self, *, operation_id: str, before_expires_at: str | None,
                                      before_traffic_limit_bytes: int | None,
                                      target_expires_at: str | None,
                                      target_traffic_limit_bytes: int | None) -> None:
        """Persist provider-observed before/after values before issuing a provider write."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND operation_id=?",
                            (self.tenant_id, operation_id)).fetchone()
            if not op:
                raise CustomerStoreError("Service-change operation not found")
            if op["status"] != "pending":
                raise CustomerStoreError("Service-change target can only be stored for a pending operation")
            if op["change_type"] == "renewal" and not target_expires_at:
                raise CustomerStoreError("Renewal target expiry is required")
            if op["change_type"] == "traffic_topup" and (target_traffic_limit_bytes is None or int(target_traffic_limit_bytes) <= 0):
                raise CustomerStoreError("Traffic top-up target limit is required")
            db.execute("""UPDATE customer_service_operations
                SET before_expires_at=?,before_traffic_limit_bytes=?,
                    target_expires_at=?,target_traffic_limit_bytes=?,updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND operation_id=? AND status='pending'""",
                (before_expires_at, before_traffic_limit_bytes, target_expires_at,
                 target_traffic_limit_bytes, self.tenant_id, operation_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def complete_service_change(self, *, operation_id: str, expires_at: str | None,
                                traffic_limit_bytes: int | None) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND operation_id=?",
                            (self.tenant_id, operation_id)).fetchone()
            if not op:
                raise CustomerStoreError("Service-change operation not found")
            if op["status"] == "completed":
                db.commit()
                return
            if op["status"] != "pending":
                raise CustomerStoreError("Service-change operation requires reconciliation")
            if op["change_type"] == "renewal":
                cur = db.execute("UPDATE customer_services SET expires_at=?,status='active',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND service_id=? AND telegram_id=?",
                                 (expires_at, self.tenant_id, op["service_id"], op["telegram_id"]))
            else:
                cur = db.execute("UPDATE customer_services SET traffic_limit_bytes=?,status='active',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND service_id=? AND telegram_id=?",
                                 (traffic_limit_bytes, self.tenant_id, op["service_id"], op["telegram_id"]))
            if cur.rowcount != 1:
                raise CustomerStoreError("Customer service disappeared before finalization")
            db.execute("UPDATE customer_service_operations SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND operation_id=?",
                       (self.tenant_id, operation_id))
            self._enqueue_admin_notification_in_transaction(db, event_key="service_change",
                idempotency_key=f"service-change-complete:{operation_id}",
                payload={"public_order_id": operation_id[:20], "amount_minor": int(op["amount_minor"]),
                         "currency": str(op["currency"]), "status": "completed",
                         "customer_id": int(op["telegram_id"]), "reason_code": str(op["change_type"])})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def fail_service_change_before_provider(self, *, operation_id: str, reason_code: str) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND operation_id=?",
                            (self.tenant_id, operation_id)).fetchone()
            if not op:
                raise CustomerStoreError("Service-change operation not found")
            if op["status"] == "refunded":
                db.commit()
                return
            if op["status"] != "pending":
                raise CustomerStoreError("Service-change operation cannot be refunded from its current state")
            if int(op["amount_minor"]) > 0:
                db.execute("""INSERT OR IGNORE INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                              VALUES(?,?,?,?,?,'refund',?,?)""",
                           (self.tenant_id, op["telegram_id"], f"service-change:{operation_id}:refund",
                            int(op["amount_minor"]), op["currency"], operation_id, reason_code[:100]))
            db.execute("UPDATE customer_service_operations SET status='refunded',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND operation_id=?",
                       (self.tenant_id, operation_id))
            self._enqueue_admin_notification_in_transaction(db, event_key="service_change",
                idempotency_key=f"service-change-refund:{operation_id}",
                payload={"public_order_id": operation_id[:20], "amount_minor": int(op["amount_minor"]),
                         "currency": str(op["currency"]), "status": "refunded",
                         "customer_id": int(op["telegram_id"]), "reason_code": reason_code[:80]})
            if int(op["amount_minor"]) > 0:
                self._enqueue_admin_notification_in_transaction(db, event_key="wallet",
                    idempotency_key=f"service-change-refund-wallet:{operation_id}",
                    payload={"amount_minor": int(op["amount_minor"]), "currency": str(op["currency"]),
                             "entry_type": "refund", "customer_id": int(op["telegram_id"]),
                             "status": "credited"})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def resolve_service_change_target_present(self, *, operation_id: str, actor_telegram_id: int,
                                              comparison: str, observed_value: Any,
                                              identity_verified: bool, reason: str) -> dict[str, Any]:
        """Finalize only from a fresh identity-matched target read; record immutable operator evidence."""
        reason = reason.strip()
        if not 1 <= len(reason) <= 500:
            raise CustomerStoreError("A decision reason of 1..500 characters is required")
        if comparison != "target_present" or identity_verified is not True:
            raise CustomerStoreError("Only a fresh identity-verified target_present result can be finalized")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND operation_id=?",
                            (self.tenant_id, operation_id)).fetchone()
            if not op:
                raise CustomerStoreError("Service-change operation not found")
            prior = db.execute("SELECT * FROM customer_service_operation_events WHERE tenant_id=? AND operation_id=?",
                               (self.tenant_id, operation_id)).fetchone()
            if prior:
                if (int(prior["actor_telegram_id"]) == int(actor_telegram_id)
                    and prior["reason"] == reason
                    and prior["observed_value"] == str(observed_value)):
                    db.commit()
                    return {**dict(prior), "idempotent_replay": True}
                raise CustomerStoreError("A final decision is already recorded for this operation")
            if op["status"] != "pending" and not str(op["status"]).startswith("reconcile_required:"):
                raise CustomerStoreError("Operation is not eligible for reconciliation finalization")
            if op["change_type"] == "renewal":
                from datetime import datetime, timezone
                target = op["target_expires_at"]
                if not target:
                    raise CustomerStoreError("Stored target expiry is missing")
                def norm(value: str) -> str:
                    dt = datetime.fromisoformat(value)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                try:
                    matches = norm(str(observed_value)) == norm(str(target))
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    raise CustomerStoreError("Observed expiry does not match the persisted target")
                service_update = ("UPDATE customer_services SET expires_at=?,status='active',updated_at=CURRENT_TIMESTAMP "
                                  "WHERE tenant_id=? AND service_id=? AND telegram_id=?")
                update_args = (target, self.tenant_id, op["service_id"], op["telegram_id"])
            else:
                target = op["target_traffic_limit_bytes"]
                try:
                    matches = target is not None and int(observed_value) == int(target)
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    raise CustomerStoreError("Observed traffic limit does not match the persisted target")
                service_update = ("UPDATE customer_services SET traffic_limit_bytes=?,status='active',updated_at=CURRENT_TIMESTAMP "
                                  "WHERE tenant_id=? AND service_id=? AND telegram_id=?")
                update_args = (int(target), self.tenant_id, op["service_id"], op["telegram_id"])
            cur = db.execute(service_update, update_args)
            if cur.rowcount != 1:
                raise CustomerStoreError("Customer service disappeared before reconciliation finalization")
            db.execute("""INSERT INTO customer_service_operation_events
                (tenant_id,operation_id,event_type,actor_telegram_id,comparison,observed_value,reason)
                VALUES(?,?,'operator_finalized_target_present',?,'target_present',?,?)""",
                (self.tenant_id, operation_id, int(actor_telegram_id), str(observed_value), reason))
            db.execute("""UPDATE customer_service_operations SET status='completed',updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND operation_id=?""", (self.tenant_id, operation_id))
            db.commit()
            return {"operation_id": operation_id, "event_type": "operator_finalized_target_present",
                    "actor_telegram_id": int(actor_telegram_id), "comparison": comparison,
                    "observed_value": str(observed_value), "reason": reason, "idempotent_replay": False}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_service_change_events(self, operation_id: str) -> list[dict[str, Any]]:
        """Read tenant-scoped immutable final-decision events."""
        with self._connect() as db:
            rows = db.execute("""SELECT event_id,operation_id,event_type,actor_telegram_id,comparison,
                observed_value,reason,created_at FROM customer_service_operation_events
                WHERE tenant_id=? AND operation_id=? ORDER BY event_id""",
                (self.tenant_id, operation_id)).fetchall()
        return [dict(row) for row in rows]

    def flag_service_change_reconciliation(self, *, operation_id: str, reason_code: str) -> None:
        with self._connect() as db:
            cur = db.execute("UPDATE customer_service_operations SET status=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND operation_id=? AND status='pending'",
                             (f"reconcile_required:{''.join(c for c in reason_code.lower() if c.isalnum() or c in '_-')[:60] or 'unknown'}",
                              self.tenant_id, operation_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Service-change operation is not pending")

    def list_service_change_operations(self, *, status: str = "reconcile_required", limit: int = 30,
                                       telegram_id: int | None = None) -> list[dict[str, Any]]:
        """List tenant-scoped service changes without exposing panel credentials or subscription URLs."""
        if status not in {"pending", "reconcile_required", "completed", "refunded", "all"}:
            raise CustomerStoreError("Unsupported service-operation status filter")
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            query = """SELECT o.operation_id,o.telegram_id,o.service_id,o.change_type,o.amount_minor,
                o.currency,o.quantity,o.status,o.created_at,o.updated_at,o.before_expires_at,
                o.before_traffic_limit_bytes,o.target_expires_at,o.target_traffic_limit_bytes,s.username,s.product_id,
                s.status AS service_status FROM customer_service_operations o
                LEFT JOIN customer_services s ON s.tenant_id=o.tenant_id AND s.service_id=o.service_id
                WHERE o.tenant_id=?"""
            params: list[Any] = [self.tenant_id]
            if telegram_id is not None:
                query += " AND o.telegram_id=?"
                params.append(int(telegram_id))
            if status == "reconcile_required":
                query += " AND o.status LIKE 'reconcile_required:%'"
            elif status != "all":
                query += " AND o.status=?"
                params.append(status)
            query += " ORDER BY o.updated_at DESC,o.operation_id DESC LIMIT ?"
            params.append(limit)
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def get_service_change_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Return a single tenant-scoped operation summary for admin review."""
        with self._connect() as db:
            row = db.execute("""SELECT o.operation_id,o.telegram_id,o.service_id,o.change_type,
                o.idempotency_key,o.amount_minor,o.currency,o.quantity,o.status,o.created_at,o.updated_at,
                o.before_expires_at,o.before_traffic_limit_bytes,o.target_expires_at,o.target_traffic_limit_bytes,
                s.username,s.product_id,s.panel_id,s.provider_user_id,s.status AS service_status,s.expires_at,s.traffic_limit_bytes
                FROM customer_service_operations o LEFT JOIN customer_services s
                ON s.tenant_id=o.tenant_id AND s.service_id=o.service_id
                WHERE o.tenant_id=? AND o.operation_id=?""", (self.tenant_id, operation_id)).fetchone()
            return dict(row) if row else None

    def flag_product_order_reconciliation(self, *, order_id: str, reason_code: str) -> None:
        safe_code = "".join(c for c in reason_code.lower() if c.isalnum() or c in "_-:")[:80] or "unknown"
        with self._connect() as db:
            cur = db.execute("UPDATE customer_orders SET provider_reference=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND order_id=? AND status='provisioning'", (f"reconcile_required:{safe_code}", self.tenant_id, order_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Order is not awaiting provisioning reconciliation")

    def finalize_product_order(self, *, order_id: str, service_id: str, provider_reference: str) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,telegram_id,product_id,amount_minor,currency,provider_reference FROM customer_orders WHERE tenant_id=? AND order_id=?", (self.tenant_id, order_id)).fetchone()
            if not row:
                raise CustomerStoreError("Order not found")
            if row["status"] == "active":
                if service_id != order_id or str(row["provider_reference"] or "") != provider_reference[:256]:
                    raise CustomerStoreError("Completed order replay conflicts with service or provider reference")
                db.commit(); return
            if row["status"] != "provisioning":
                raise CustomerStoreError("Order is not in provisioning state")
            service = db.execute("SELECT telegram_id,product_id FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            if not service or service_id != order_id or int(service["telegram_id"]) != int(row["telegram_id"]) or service["product_id"] != row["product_id"]:
                raise CustomerStoreError("Provisioned service does not match the reserved order")
            db.execute("UPDATE customer_orders SET status='active',provider_reference=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND order_id=?", (provider_reference[:256], self.tenant_id, order_id))
            self._enqueue_trust_event_in_transaction(db, event_type="new_subscription",
                idempotency_key=f"purchase:{order_id}", payload={"public_order_id": order_id[:20],
                    "amount_minor": int(row["amount_minor"]), "currency": str(row["currency"])})
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def resolve_product_order_target_present(self, *, order_id: str, actor_telegram_id: int,
                                             provider_kind: str, provider_user_id: str, username: str,
                                             subscription_url: str | None, evidence: dict[str, Any],
                                             comparison: str, identity_verified: bool, reason: str,
                                             now: int) -> dict[str, Any]:
        """Atomically attach a freshly inspected existing provider target to its reserved order."""
        reason = reason.strip()
        provider_kind = provider_kind.strip().lower()
        provider_user_id = str(provider_user_id or "").strip()
        username = str(username or "").strip()
        if not 1 <= len(reason) <= 500:
            raise CustomerStoreError("A decision reason of 1..500 characters is required")
        if (isinstance(actor_telegram_id, bool) or not isinstance(actor_telegram_id, int)
                or actor_telegram_id <= 0):
            raise CustomerStoreError("A valid operator Telegram ID is required")
        if provider_kind not in {"pasarguard", "rebecca", "sanaei"}:
            raise CustomerStoreError("Unsupported provider for product-order reconciliation")
        if (not provider_user_id or len(provider_user_id) > 256
                or not username or len(username) > 128):
            raise CustomerStoreError("Observed provider identity is incomplete")
        if comparison != "target_present" or identity_verified is not True:
            raise CustomerStoreError("Only a fresh identity-verified target_present result can be finalized")
        if not isinstance(evidence, dict):
            raise CustomerStoreError("Reconciliation evidence is invalid")
        safe_evidence = dict(evidence)
        # Never persist full subscription URLs, credential material, or arbitrary provider response bodies.
        safe_evidence.pop("subscription_url", None)
        evidence_json = json.dumps(safe_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if subscription_url is not None and (
            not isinstance(subscription_url, str) or len(subscription_url) > 4096
            or any(ord(ch) < 32 for ch in subscription_url)
        ):
            raise CustomerStoreError("Observed subscription URL is invalid")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute(
                "SELECT * FROM customer_orders WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
            if not order:
                raise CustomerStoreError("Product order not found")
            prior_event = db.execute(
                "SELECT * FROM customer_product_order_reconciliation_events WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
            if prior_event:
                matches = (
                    int(prior_event["actor_telegram_id"]) == actor_telegram_id
                    and str(prior_event["provider_user_id"]) == provider_user_id
                    and str(prior_event["username"]) == username
                    and str(prior_event["reason"]) == reason
                    and str(prior_event["evidence_json"]) == evidence_json
                )
                if matches and order["status"] == "active":
                    existing = db.execute(
                        "SELECT telegram_id,product_id,panel_id,provider_user_id,username FROM customer_services "
                        "WHERE tenant_id=? AND service_id=?",
                        (self.tenant_id, order_id),
                    ).fetchone()
                    if (existing and int(existing["telegram_id"]) == int(order["telegram_id"])
                            and str(existing["product_id"]) == str(order["product_id"])
                            and str(existing["provider_user_id"]) == provider_user_id
                            and str(existing["username"]) == username):
                        db.commit()
                        return {"order_id": order_id, "event_type": "operator_finalized_target_present",
                                "actor_telegram_id": actor_telegram_id, "provider_user_id": provider_user_id,
                                "username": username, "comparison": comparison, "reason": reason,
                                "idempotent_replay": True}
                raise CustomerStoreError("A final product-order reconciliation decision is already recorded")
            if (order["status"] != "provisioning"
                    or not str(order["provider_reference"] or "").startswith("reconcile_required:")):
                raise CustomerStoreError("Order is not awaiting product provisioning reconciliation")
            try:
                product_snapshot = json.loads(order["product_snapshot_json"] or "{}")
            except (TypeError, ValueError):
                product_snapshot = {}
            intent = product_snapshot.get("_provisioning_intent") if isinstance(product_snapshot, dict) else None
            if not isinstance(intent, dict) or intent.get("version") != 1:
                raise CustomerStoreError("Order has no complete persisted provisioning intent; manual review is required")
            binding = intent.get("provider_binding")
            service_product = intent.get("service_product")
            if not isinstance(binding, dict) or not isinstance(service_product, dict):
                raise CustomerStoreError("Stored provisioning intent is incomplete")
            expected_panel_id = str(service_product.get("panel_id") or "")
            if (str(intent.get("username") or "") != username
                    or str(binding.get("provider_kind") or "") != provider_kind
                    or str(binding.get("panel_id") or "") != expected_panel_id
                    or str(product_snapshot.get("panel_id") or "") != expected_panel_id):
                raise CustomerStoreError("Observed provider identity or panel binding does not match the stored order")
            if str(service_product.get("product_id") or "") != str(order["product_id"]):
                raise CustomerStoreError("Stored service snapshot does not match the order product")
            # Sanaei's normalized provider_user_id is the client username; the actual
            # protocol UUID/password is exposed separately as metadata.uuid by the adapter.
            expected_extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
            observed_status = str(safe_evidence.get("observed_status") or "").strip().lower()
            if observed_status not in {"active", "enabled", "true", "1"}:
                raise CustomerStoreError("Observed provider status is not active")
            expected_limit = intent.get("data_limit_bytes")
            observed_limit = safe_evidence.get("observed_data_limit_bytes")
            try:
                if expected_limit is None:
                    limit_matches = observed_limit in (None, 0)
                else:
                    limit_matches = observed_limit is not None and int(observed_limit) == int(expected_limit)
            except (TypeError, ValueError, OverflowError):
                limit_matches = False
            if not limit_matches:
                raise CustomerStoreError("Observed provider data limit does not match the stored create intent")
            expected_expiry = intent.get("expire_at")
            observed_expiry = safe_evidence.get("observed_expire_at")
            if expected_expiry is None:
                expiry_matches = observed_expiry is None
            else:
                from datetime import datetime, timezone
                try:
                    expected_dt = datetime.fromisoformat(str(expected_expiry))
                    observed_dt = datetime.fromisoformat(str(observed_expiry))
                    if expected_dt.tzinfo is None:
                        expected_dt = expected_dt.replace(tzinfo=timezone.utc)
                    if observed_dt.tzinfo is None:
                        observed_dt = observed_dt.replace(tzinfo=timezone.utc)
                    expiry_matches = (expected_dt.astimezone(timezone.utc).replace(microsecond=0)
                                      == observed_dt.astimezone(timezone.utc).replace(microsecond=0))
                except (TypeError, ValueError):
                    expiry_matches = False
            if not expiry_matches:
                raise CustomerStoreError("Observed provider expiry does not match the stored create intent")
            observed_metadata = safe_evidence.get("observed_metadata")
            observed_metadata = observed_metadata if isinstance(observed_metadata, dict) else {}
            if provider_kind == "sanaei":
                if (not expected_extra.get("uuid_sha256")
                        or str(observed_metadata.get("uuid_sha256") or "") != str(expected_extra.get("uuid_sha256") or "")):
                    raise CustomerStoreError("Observed Sanaei client ID does not match the persisted create intent")
                expected_sub_hash = str(expected_extra.get("sub_id_sha256") or "")
                observed_sub_hash = str(observed_metadata.get("sub_id_sha256") or "")
                if expected_sub_hash != observed_sub_hash:
                    raise CustomerStoreError("Observed Sanaei subscription ID does not match the persisted create intent")
            if provider_kind == "rebecca":
                expected_service_id = expected_extra.get("service_id")
                observed_service_id = observed_metadata.get("service_id")
                if expected_service_id is None or str(observed_service_id) != str(expected_service_id):
                    raise CustomerStoreError("Observed Rebecca service ID does not match the persisted create intent")
            if provider_kind == "pasarguard":
                expected_groups = sorted(int(x) for x in (intent.get("group_ids") or []))
                observed_groups = observed_metadata.get("group_ids")
                if not isinstance(observed_groups, list) or sorted(int(x) for x in observed_groups) != expected_groups:
                    raise CustomerStoreError("Observed PasarGuard group IDs do not match the persisted create intent")
            if provider_kind == "sanaei":
                expected_inbounds = sorted(int(x) for x in (expected_extra.get("inbound_ids") or []))
                observed_inbounds = observed_metadata.get("inbound_ids")
                if not isinstance(observed_inbounds, list) or sorted(int(x) for x in observed_inbounds) != expected_inbounds:
                    raise CustomerStoreError("Observed Sanaei inbound IDs do not match the persisted create intent")
            claimed = db.execute(
                """SELECT service_id FROM customer_services
                   WHERE tenant_id=? AND panel_id=? AND service_id<>?
                     AND ((provider_user_id<>'' AND provider_user_id=?) OR (username<>'' AND username=?))
                   LIMIT 1""",
                (self.tenant_id, expected_panel_id, order_id, provider_user_id, username),
            ).fetchone()
            if claimed:
                raise CustomerStoreError("Provider identity is already attached to another local service")
            if subscription_url is not None and not subscription_url.strip():
                subscription_url = None
            product = dict(service_product)
            mode = str(product.get("pricing_mode", "fixed"))
            billing_status = "active" if mode in {"hourly", "usage"} else "not_metered"
            existing = db.execute(
                """SELECT * FROM customer_services WHERE tenant_id=? AND service_id=?""",
                (self.tenant_id, order_id),
            ).fetchone()
            if not existing:
                db.execute(
                    """INSERT INTO customer_services
                       (tenant_id,service_id,telegram_id,product_id,panel_id,provider_user_id,username,
                        subscription_url,reseller_account_id,status,expires_at,traffic_limit_bytes,
                        traffic_used_bytes,pricing_mode,currency,unit_price_minor,billing_last_at,
                        billed_traffic_bytes,fractional_carry,billing_status,provider_checked_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (self.tenant_id, order_id, int(order["telegram_id"]), str(product.get("product_id") or ""),
                     expected_panel_id, provider_user_id, username, subscription_url or "",
                     str(order["reseller_account_id"] or ""), product.get("expires_at"),
                     int(product.get("traffic_bytes", 0) or 0), 0, mode, str(product.get("currency") or order["currency"]),
                     int(product.get("unit_price_minor", 0) or 0), int(intent.get("service_created_at") or now),
                     0, "0", billing_status),
                )
                existing = db.execute(
                    "SELECT * FROM customer_services WHERE tenant_id=? AND service_id=?",
                    (self.tenant_id, order_id),
                ).fetchone()
            if (not existing or int(existing["telegram_id"]) != int(order["telegram_id"])
                    or str(existing["product_id"]) != str(order["product_id"])
                    or str(existing["panel_id"] or "") != expected_panel_id
                    or str(existing["provider_user_id"] or "") != provider_user_id
                    or str(existing["username"] or "") != username
                    or str(existing["reseller_account_id"] or "") != str(order["reseller_account_id"] or "")
                    or str(existing["status"]) != "active"):
                raise CustomerStoreError("Existing local service conflicts with the observed provider target")
            db.execute(
                """UPDATE customer_orders SET status='active',provider_reference=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND order_id=? AND status='provisioning'""",
                (provider_user_id[:256], self.tenant_id, order_id),
            )
            self._enqueue_trust_event_in_transaction(
                db, event_type="new_subscription", idempotency_key=f"purchase:{order_id}",
                payload={"public_order_id": order_id[:20], "amount_minor": int(order["amount_minor"]),
                         "currency": str(order["currency"])},
            )
            db.execute(
                """INSERT INTO customer_product_order_reconciliation_events
                   (tenant_id,order_id,event_type,actor_telegram_id,provider_kind,provider_user_id,
                    username,comparison,evidence_json,reason)
                   VALUES(?,?,'operator_finalized_target_present',?,?,?,?, 'target_present',?,?)""",
                (self.tenant_id, order_id, actor_telegram_id, provider_kind, provider_user_id,
                 username, evidence_json, reason),
            )
            db.commit()
            return {"order_id": order_id, "event_type": "operator_finalized_target_present",
                    "actor_telegram_id": actor_telegram_id, "provider_user_id": provider_user_id,
                    "username": username, "comparison": comparison, "reason": reason,
                    "idempotent_replay": False}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def bill_customer_service(self, *, service_id: str, now: int, used_traffic_bytes: int | None = None) -> dict[str, Any]:
        """Atomically accrue hourly/usage billing against wallet and advance its cursor exactly once.

        The caller must suspend the provider service when `suspend_required` is true, then call
        `confirm_customer_service_suspended` only after the provider confirms suspension.
        """
        from decimal import Decimal, ROUND_DOWN
        from .resellers.billing import BYTES_PER_GIB
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            service = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            if not service:
                raise CustomerStoreError("Service not found")
            if service["status"] != "active" or service["billing_status"] != "active":
                db.commit(); return {"service_id": service_id, "charged_minor": 0, "suspend_required": False, "skipped": True}
            mode = str(service["pricing_mode"])
            if mode not in {"hourly", "usage"}:
                db.commit(); return {"service_id": service_id, "charged_minor": 0, "suspend_required": False, "skipped": True}
            last = int(service["billing_last_at"] or now)
            if now < last:
                raise CustomerStoreError("Billing clock moved backwards")
            carry = Decimal(str(service["fractional_carry"] or "0"))
            rate = Decimal(int(service["unit_price_minor"]))
            delta_bytes = 0
            if mode == "hourly":
                minutes = max(0, (now - last) // 60)
                exact = rate * Decimal(minutes) / Decimal(60) + carry
                cursor = last + minutes * 60
            else:
                if used_traffic_bytes is None or isinstance(used_traffic_bytes, bool) or used_traffic_bytes < 0:
                    raise CustomerStoreError("Usage billing requires a non-negative provider traffic counter")
                previous = int(service["billed_traffic_bytes"] or 0)
                delta_bytes = used_traffic_bytes - previous if used_traffic_bytes >= previous else used_traffic_bytes
                exact = Decimal(delta_bytes) * rate / Decimal(BYTES_PER_GIB) + carry
                cursor = now
            if not exact.is_finite() or exact < 0 or not carry.is_finite() or carry < 0 or carry >= 1:
                raise CustomerStoreError("Invalid metering rate or fractional carry")
            due = int(exact.to_integral_value(rounding=ROUND_DOWN))
            remainder = exact - due
            currency = str(service["currency"])
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, int(service["telegram_id"]), currency)).fetchone()[0])
            charged = min(due, max(0, balance))
            insufficient = charged < due
            new_debt = int(service["outstanding_debt_minor"] or 0) + max(0, due - charged)
            next_status = "suspension_pending" if insufficient else "active"
            ledger_key = f"service:{service_id}:{mode}:{cursor}" if mode == "hourly" else f"service:{service_id}:usage:{cursor}:{int(used_traffic_bytes or 0)}"
            if charged > 0:
                db.execute(
                    """INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'metered_service',?,?)""",
                    (self.tenant_id, int(service["telegram_id"]), ledger_key, -charged, currency,
                     service_id, f"{mode} metered billing"),
                )
            new_carry = "0" if insufficient else str(remainder)
            db.execute(
                """UPDATE customer_services SET billing_last_at=?,billed_traffic_bytes=?,fractional_carry=?,
                   outstanding_debt_minor=?,billing_status=?,status=?,traffic_used_bytes=CASE WHEN ? IS NULL THEN traffic_used_bytes ELSE ? END,
                   updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND service_id=?""",
                (cursor, int(used_traffic_bytes) if mode == "usage" else int(service["billed_traffic_bytes"] or 0),
                 new_carry, new_debt, next_status, "suspended" if insufficient else "active",
                 used_traffic_bytes, used_traffic_bytes, self.tenant_id, service_id),
            )
            db.commit()
            return {"service_id": service_id, "mode": mode, "charged_minor": charged,
                    "due_minor": due, "delta_bytes": delta_bytes, "billing_cursor": cursor,
                    "suspend_required": insufficient, "billing_status": next_status}
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def list_billable_customer_services(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT service_id,telegram_id,panel_id,provider_user_id,username,pricing_mode,
                          currency,unit_price_minor,billing_last_at,billed_traffic_bytes,billing_status
                   FROM customer_services WHERE tenant_id=? AND status='active' AND billing_status='active'
                     AND reseller_account_id='' AND pricing_mode IN ('hourly','usage') ORDER BY billing_last_at LIMIT ?""",
                (self.tenant_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_customer_service_suspensions(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT service_id,telegram_id,panel_id,provider_user_id,username,reseller_account_id,pricing_mode,currency
                   FROM customer_services WHERE tenant_id=? AND billing_status='suspension_pending'
                   ORDER BY updated_at LIMIT ?""", (self.tenant_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def resume_customer_service_billing(self, *, service_id: str, now: int) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT telegram_id,currency,pricing_mode,billing_status,outstanding_debt_minor FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            if not row: raise CustomerStoreError("Service not found")
            if row["pricing_mode"] not in {"hourly", "usage"}: raise CustomerStoreError("Service does not use metered billing")
            if row["billing_status"] != "suspended_balance": raise CustomerStoreError("Service is not suspended for wallet balance")
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, int(row["telegram_id"]), str(row["currency"]))).fetchone()[0])
            debt = int(row["outstanding_debt_minor"] or 0)
            if balance < debt + 1:
                raise CustomerStoreError(f"Wallet needs at least {debt + 1:,} {row['currency']} to settle metered debt and resume")
            if debt:
                db.execute("INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note) VALUES(?,?,?,?,?,'metered_debt_settlement',?,?)",
                    (self.tenant_id, int(row["telegram_id"]), f"service-resume-debt:{service_id}:{now}", -debt,
                     str(row["currency"]), service_id, "Settle outstanding metered service debt before resuming"))
            db.execute("UPDATE customer_services SET status='active',billing_status='active',outstanding_debt_minor=0,billing_last_at=?,fractional_carry='0',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND service_id=?", (now, self.tenant_id, service_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def confirm_customer_service_suspended(self, *, service_id: str) -> None:
        with self._connect() as db:
            cur = db.execute("UPDATE customer_services SET billing_status='suspended_balance',status='suspended',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND service_id=? AND billing_status='suspension_pending'", (self.tenant_id, service_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Service is not awaiting balance suspension confirmation")

    def ensure_credential_vault_ready(self) -> None:
        from .resellers.credential_vault import validate_vault
        validate_vault()

    def save_reseller_credential(self, *, account_id: str, username: str, password: str) -> None:
        from .resellers.credential_vault import encrypt_secret
        self.ensure_credential_vault_ready()
        ciphertext = encrypt_secret(password)
        with self._connect() as db:
            row = db.execute("SELECT status,username FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id)).fetchone()
            if not row or row["status"] != "pending" or row["username"] != username:
                raise CustomerStoreError("Credential does not match a pending reseller purchase")
            db.execute("""INSERT INTO reseller_credentials(tenant_id,account_id,username,encrypted_password)
                          VALUES(?,?,?,?) ON CONFLICT(tenant_id,account_id) DO UPDATE SET
                          username=excluded.username,encrypted_password=excluded.encrypted_password,
                          updated_at=CURRENT_TIMESTAMP""", (self.tenant_id, account_id, username, ciphertext))

    def get_reseller_credential(self, account_id: str) -> dict[str, str] | None:
        from .resellers.credential_vault import decrypt_secret
        with self._connect() as db:
            row = db.execute("SELECT username,encrypted_password FROM reseller_credentials WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id)).fetchone()
        if not row: return None
        return {"username": str(row["username"]), "password": decrypt_secret(str(row["encrypted_password"]))}

    def delete_reseller_credential(self, account_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM reseller_credentials WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id))

    def get_reseller_account(self, account_id: str, telegram_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM reseller_accounts WHERE tenant_id=? AND account_id=?"
        args: tuple[Any, ...] = (self.tenant_id, account_id)
        if telegram_id is not None:
            sql += " AND owner_telegram_id=?"; args += (telegram_id,)
        with self._connect() as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def list_reseller_child_services(self, account_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT service_id,telegram_id,product_id,panel_id,provider_user_id,username,subscription_url,
                          reseller_account_id,status,expires_at,traffic_limit_bytes,traffic_used_bytes
                   FROM customer_services WHERE tenant_id=? AND reseller_account_id=? AND status IN ('active','limited','suspended')
                   ORDER BY created_at""", (self.tenant_id, account_id)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reseller_usage_counter(self, *, account_id: str, service_counters: dict[str, int]) -> int:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT 1 FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id)).fetchone()
            if not account: raise CustomerStoreError("Reseller account not found")
            for service_id, current in service_counters.items():
                if isinstance(current, bool) or not isinstance(current, int) or current < 0:
                    raise CustomerStoreError("Invalid child service traffic counter")
                prior = db.execute("SELECT last_seen_bytes,cumulative_bytes FROM reseller_service_usage_counters WHERE tenant_id=? AND reseller_account_id=? AND service_id=?", (self.tenant_id, account_id, service_id)).fetchone()
                previous = int(prior["last_seen_bytes"]) if prior else 0
                cumulative = int(prior["cumulative_bytes"]) if prior else 0
                delta = current - previous if current >= previous else current
                db.execute(
                    """INSERT INTO reseller_service_usage_counters(tenant_id,reseller_account_id,service_id,last_seen_bytes,cumulative_bytes)
                       VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,reseller_account_id,service_id) DO UPDATE SET
                       last_seen_bytes=excluded.last_seen_bytes,cumulative_bytes=excluded.cumulative_bytes,updated_at=CURRENT_TIMESTAMP""",
                    (self.tenant_id, account_id, service_id, current, cumulative + delta),
                )
            total = int(db.execute("SELECT COALESCE(SUM(cumulative_bytes),0) FROM reseller_service_usage_counters WHERE tenant_id=? AND reseller_account_id=?", (self.tenant_id, account_id)).fetchone()[0])
            db.commit(); return total
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def reserve_purchase(self, *, owner_id: str, idempotency_key: str, amount: Any,
                         currency: str, plan: Any, username: str,
                         volume_gb: Any = None) -> str:
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal
        from .resellers.models import PricingMode
        owner = int(owner_id)
        if owner <= 0 or not username.strip() or not idempotency_key.strip():
            raise CustomerStoreError("Valid owner, username and idempotency key are required")
        charge = int(Decimal(str(amount)))
        if charge < 0:
            raise CustomerStoreError("Purchase charge cannot be negative")
        mode = PricingMode(plan.pricing_mode)
        volume = Decimal(str(volume_gb)) if volume_gb is not None else Decimal("0")
        traffic_cap = int(volume * Decimal(1024**3)) if volume > 0 and mode in {PricingMode.PER_GB, PricingMode.PER_TB} else int(plan.data_limit_bytes or 0)
        expiry = None
        duration_hours = int(plan.duration_hours) if getattr(plan, "duration_hours", None) is not None else int(plan.duration_days or 0) * 24
        if duration_hours > 0 and mode in {PricingMode.FIXED, PricingMode.UNLIMITED}:
            expiry = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT account_id,status,owner_telegram_id,setup_fee_minor,currency FROM reseller_accounts WHERE tenant_id=? AND idempotency_key=?", (self.tenant_id, idempotency_key)).fetchone()
            if existing:
                if int(existing["owner_telegram_id"]) != owner:
                    raise CustomerStoreError("Reseller idempotency key belongs to another owner")
                if existing["status"] == "active":
                    db.commit(); return str(existing["account_id"])
                raise CustomerStoreError(f"Reseller purchase already exists in {existing['status']} state")
            customer = db.execute("SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?", (self.tenant_id, owner)).fetchone()
            if not customer:
                raise CustomerStoreError("Owner must be registered before reseller purchase")
            stored_plan = db.execute("SELECT enabled,panel_id,pricing_mode,currency,price_minor,unit_price_minor,data_limit_bytes,max_users,duration_hours,provider_role_id,min_volume_gib,max_volume_gib,volume_step_gib,allowed_group_ids_json,permission_overrides_json FROM reseller_plans WHERE tenant_id=? AND plan_id=?", (self.tenant_id, plan.plan_id)).fetchone()
            if not stored_plan or not stored_plan["enabled"]:
                raise CustomerStoreError("Reseller plan is unavailable")
            panel_state = db.execute("SELECT enabled,provider FROM panel_connections WHERE tenant_id=? AND panel_id=?", (self.tenant_id, stored_plan["panel_id"])).fetchone()
            if not panel_state or not panel_state["enabled"]:
                raise CustomerStoreError("Reseller panel is disabled; no wallet debit was made")
            if panel_state["provider"] == "pasarguard" and not stored_plan["provider_role_id"]:
                raise CustomerStoreError("PasarGuard reseller plans require a provider role ID")
            if panel_state["provider"] not in {"pasarguard", "rebecca", "sanaei"}:
                raise CustomerStoreError("Reseller provider is unsupported")
            from decimal import Decimal
            if (stored_plan["panel_id"] != plan.panel_id or stored_plan["pricing_mode"] != mode.value
                    or stored_plan["currency"] != currency or int(stored_plan["price_minor"]) != int(plan.price)
                    or int(stored_plan["unit_price_minor"]) != int(plan.unit_price)
                    or int(stored_plan["data_limit_bytes"]) != int(plan.data_limit_bytes)
                    or int(stored_plan["max_users"]) != int(plan.max_users)
                    or int(stored_plan["duration_hours"]) != (int(plan.duration_hours) if plan.duration_hours is not None else int(plan.duration_days) * 24)
                    or str(stored_plan["provider_role_id"]) != str(plan.provider_role_id or "")
                    or Decimal(stored_plan["min_volume_gib"]) != plan.min_volume_gb
                    or Decimal(stored_plan["max_volume_gib"]) != plan.max_volume_gb
                    or Decimal(stored_plan["volume_step_gib"]) != plan.volume_step_gb
                    or json.loads(stored_plan["allowed_group_ids_json"]) != list(plan.allowed_group_ids)
                    or json.loads(stored_plan["permission_overrides_json"]) != dict(plan.permission_overrides)):
                raise CustomerStoreError("Reseller plan changed before purchase")
            from .resellers.pricing import calculate_purchase_price
            expected_charge = int(calculate_purchase_price(plan, volume if mode in {PricingMode.PER_GB, PricingMode.PER_TB} else None))
            if charge != expected_charge:
                raise CustomerStoreError("Reseller purchase amount does not match stored plan pricing")
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, owner, currency)).fetchone()[0])
            if balance < charge:
                raise CustomerStoreError("Insufficient wallet balance")
            if mode in {PricingMode.HOURLY, PricingMode.USAGE} and balance - charge <= 0:
                raise CustomerStoreError("Hourly/usage reseller plans require a positive wallet balance after setup charge")
            account_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO reseller_accounts
                   (tenant_id,account_id,owner_telegram_id,panel_id,provider_account_id,username,plan_id,pricing_mode,status,currency,unit_price_minor,setup_fee_minor,idempotency_key,purchased_volume_gib,expires_at,traffic_cap_bytes,max_users,last_billed_at,billed_traffic_bytes,fractional_carry,usage_cap_bytes,total_billed_minor)
                   VALUES(?,?,?,?,?,?,?,?, 'pending',?,?,?,?,?,?,?,?,?,?, '0',0,0)""",
                (self.tenant_id, account_id, owner, plan.panel_id, "", username, plan.plan_id, mode.value,
                 currency, int(Decimal(str(plan.unit_price)).quantize(Decimal("1"))), charge, idempotency_key,
                 str(volume), expiry, traffic_cap, int(plan.max_users or 0), int(datetime.now(timezone.utc).timestamp()), 0),
            )
            if charge > 0:
                db.execute(
                    """INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'reseller_setup',?,'Reseller plan purchase reservation')""",
                    (self.tenant_id, owner, f"reseller:{account_id}:setup", -charge, currency, account_id),
                )
            db.commit()
            return account_id
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def finalize_purchase(self, *, purchase_id: str, account: Any) -> None:
        from decimal import Decimal
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,owner_telegram_id,username,plan_id FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id)).fetchone()
            if not row:
                raise CustomerStoreError("Reseller reservation not found")
            if row["status"] == "active":
                db.commit(); return
            if row["status"] != "pending" or int(row["owner_telegram_id"]) != int(account.owner_id) or row["username"] != account.username or row["plan_id"] != account.plan_id:
                raise CustomerStoreError("Reseller reservation does not match provider result")
            db.execute(
                """UPDATE reseller_accounts SET provider_account_id=?,status='active',max_users=?,traffic_cap_bytes=?,
                   expires_at=?,unit_price_minor=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=?""",
                (str(account.provider_account_id or ""), int(account.max_users or 0), int(account.traffic_cap_bytes or 0),
                 account.expires_at.isoformat() if account.expires_at else None,
                 int(Decimal(str(account.unit_price)).quantize(Decimal("1"))), self.tenant_id, purchase_id),
            )
            self._enqueue_trust_event_in_transaction(db, event_type="reseller_created",
                idempotency_key=f"reseller:{purchase_id}", payload={"public_order_id": purchase_id[:20],
                    "amount_minor": int(db.execute("SELECT setup_fee_minor FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id)).fetchone()[0]),
                    "currency": str(db.execute("SELECT currency FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id)).fetchone()[0])})
            db.commit()

    def refund_and_fail(self, *, purchase_id: str, reason_code: str) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_telegram_id,status,setup_fee_minor,currency FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id)).fetchone()
            if not row:
                raise CustomerStoreError("Reseller reservation not found")
            if row["status"] == "failed": db.commit(); return
            if row["status"] not in {"pending", "active"}:
                raise CustomerStoreError("Cannot refund a reseller requiring reconciliation")
            amount = int(row["setup_fee_minor"])
            if amount > 0:
                db.execute(
                    """INSERT OR IGNORE INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'refund',?,?)""",
                    (self.tenant_id, int(row["owner_telegram_id"]), f"reseller:{purchase_id}:refund", amount,
                     row["currency"], purchase_id, reason_code[:100]),
                )
            db.execute("UPDATE reseller_accounts SET status='failed',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id))
            db.execute("DELETE FROM reseller_credentials WHERE tenant_id=? AND account_id=?", (self.tenant_id, purchase_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def flag_reconciliation_required(self, *, purchase_id: str, reason_code: str) -> None:
        safe = "".join(c for c in reason_code.lower() if c.isalnum() or c in "_-:")[:80] or "unknown"
        with self._connect() as db:
            cur = db.execute("UPDATE reseller_accounts SET status='reconcile_required',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=? AND status IN ('pending','active')", (self.tenant_id, purchase_id))
            if cur.rowcount != 1: raise CustomerStoreError("Reseller reservation cannot be marked for reconciliation")

    def get_completed_purchase(self, *, owner_id: str, idempotency_key: str) -> Any:
        from datetime import datetime
        from decimal import Decimal
        from .resellers.models import PricingMode, ResellerAccount, ResellerStatus
        from .resellers.service import PurchaseResult
        with self._connect() as db:
            row = db.execute("SELECT * FROM reseller_accounts WHERE tenant_id=? AND owner_telegram_id=? AND idempotency_key=? AND status='active'", (self.tenant_id, int(owner_id), idempotency_key)).fetchone()
        if not row: return None
        def dt(value): return datetime.fromisoformat(value) if value else None
        account = ResellerAccount(account_id=row["account_id"], owner_id=str(row["owner_telegram_id"]),
            panel_id=row["panel_id"], provider_account_id=row["provider_account_id"], username=row["username"],
            plan_id=row["plan_id"], pricing_mode=PricingMode(row["pricing_mode"]), status=ResellerStatus(row["status"]),
            max_users=int(row["max_users"]), traffic_cap_bytes=int(row["traffic_cap_bytes"]),
            expires_at=dt(row["expires_at"]), created_at=dt(row["created_at"]), updated_at=dt(row["updated_at"]),
            unit_price=Decimal(int(row["unit_price_minor"])), currency=row["currency"],
            purchased_volume_gb=Decimal(row["purchased_volume_gib"]), total_billed=Decimal(int(row["total_billed_minor"])))
        return PurchaseResult(account, Decimal(int(row["setup_fee_minor"])), row["currency"], idempotency_key)

    def list_due_reseller_expirations(self, *, now: int, limit: int = 100) -> list[dict[str, Any]]:
        from datetime import datetime, timezone
        limit = max(1, min(int(limit), 500))
        cutoff = datetime.fromtimestamp(now, timezone.utc).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """SELECT account_id,owner_telegram_id,panel_id,provider_account_id,username,pricing_mode,currency,expires_at
                   FROM reseller_accounts WHERE tenant_id=? AND status IN ('active','suspended_balance') AND expires_at IS NOT NULL AND expires_at<=?
                   ORDER BY expires_at LIMIT ?""", (self.tenant_id, cutoff, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reseller_expired(self, *, account_id: str) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE reseller_accounts SET status='expired',provider_suspended=1,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=? AND status IN ('active','suspended_balance')", (self.tenant_id, account_id))
            if cur.rowcount != 1: raise CustomerStoreError("Reseller account is not active or was already changed")
            db.execute("UPDATE customer_services SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND reseller_account_id=? AND status IN ('active','limited','suspended')", (self.tenant_id, account_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def list_billable_reseller_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT account_id,owner_telegram_id,panel_id,provider_account_id,username,pricing_mode,
                          currency,unit_price_minor,last_billed_at,billed_traffic_bytes,fractional_carry
                   FROM reseller_accounts WHERE tenant_id=? AND status='active' AND pricing_mode IN ('hourly','usage')
                   ORDER BY last_billed_at LIMIT ?""", (self.tenant_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reseller_suspensions_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT account_id,owner_telegram_id,panel_id,provider_account_id,username,pricing_mode,currency
                   FROM reseller_accounts WHERE tenant_id=? AND status='suspended_balance' AND provider_suspended=0
                   ORDER BY updated_at LIMIT ?""", (self.tenant_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def bill_reseller_account(self, *, account_id: str, now: int, used_traffic_bytes: int | None = None) -> dict[str, Any]:
        from decimal import Decimal, ROUND_DOWN
        from .resellers.billing import BYTES_PER_GIB
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id)).fetchone()
            if not row: raise CustomerStoreError("Reseller account not found")
            if row["status"] != "active" or row["pricing_mode"] not in {"hourly", "usage"}:
                db.commit(); return {"account_id": account_id, "charged_minor": 0, "suspend_required": False, "skipped": True}
            mode = str(row["pricing_mode"])
            last = int(row["last_billed_at"] or now)
            if now < last: raise CustomerStoreError("Billing clock moved backwards")
            carry = Decimal(str(row["fractional_carry"] or "0"))
            rate = Decimal(int(row["unit_price_minor"]))
            delta_bytes = 0
            if mode == "hourly":
                minutes = max(0, (now - last) // 60)
                exact = rate * Decimal(minutes) / Decimal(60) + carry
                cursor = last + minutes * 60
            else:
                if used_traffic_bytes is None or isinstance(used_traffic_bytes, bool) or used_traffic_bytes < 0:
                    raise CustomerStoreError("Usage billing requires a non-negative provider traffic counter")
                previous = int(row["billed_traffic_bytes"] or 0)
                delta_bytes = used_traffic_bytes - previous if used_traffic_bytes >= previous else used_traffic_bytes
                exact = Decimal(delta_bytes) * rate / Decimal(BYTES_PER_GIB) + carry
                cursor = now
            if not exact.is_finite() or exact < 0 or not carry.is_finite() or carry < 0 or carry >= 1:
                raise CustomerStoreError("Invalid reseller metering rate or fractional carry")
            due = int(exact.to_integral_value(rounding=ROUND_DOWN))
            remainder = exact - due
            currency = str(row["currency"])
            owner = int(row["owner_telegram_id"])
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, owner, currency)).fetchone()[0])
            charged = min(due, max(0, balance))
            wallet_shortfall = charged < due
            new_debt = int(row["outstanding_debt_minor"] or 0) + max(0, due - charged)
            traffic_cap = int(row["traffic_cap_bytes"] or 0)
            quota_exhausted = mode == "usage" and traffic_cap > 0 and used_traffic_bytes is not None and int(used_traffic_bytes) >= traffic_cap
            insufficient = wallet_shortfall or quota_exhausted
            if due == 0 and not quota_exhausted:
                db.execute(
                    """UPDATE reseller_accounts SET last_billed_at=?,billed_traffic_bytes=?,fractional_carry=?,
                       used_traffic_bytes=CASE WHEN ? IS NULL THEN used_traffic_bytes ELSE ? END,
                       updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=?""",
                    (cursor, int(used_traffic_bytes) if mode == "usage" else int(row["billed_traffic_bytes"] or 0),
                     str(remainder), used_traffic_bytes, used_traffic_bytes, self.tenant_id, account_id),
                )
                db.commit()
                return {"account_id": account_id, "mode": mode, "charged_minor": 0, "due_minor": 0,
                        "delta_bytes": delta_bytes, "billing_cursor": cursor, "suspend_required": False,
                        "billing_status": "active", "skipped": True}
            key = f"reseller:{account_id}:{mode}:{cursor}" if mode == "hourly" else f"reseller:{account_id}:usage:{cursor}:{int(used_traffic_bytes or 0)}"
            if charged > 0:
                db.execute(
                    """INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                       VALUES(?,?,?,?,?,'reseller_metered',?,?)""",
                    (self.tenant_id, owner, key, -charged, currency, account_id, f"{mode} reseller billing"),
                )
            db.execute(
                """INSERT INTO reseller_billing_events
                   (tenant_id,account_id,idempotency_key,amount_minor,currency,event_type,delta_bytes,start_at,end_at,note)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id, account_id, key, charged, currency, mode, delta_bytes, last, cursor,
                 f"due={due};charged={charged};shortfall={max(0,due-charged)};suspension_reason={'wallet' if wallet_shortfall else 'traffic_limit' if quota_exhausted else 'none'}"),
            )
            db.execute(
                """UPDATE reseller_accounts SET last_billed_at=?,billed_traffic_bytes=?,fractional_carry=?,
                   outstanding_debt_minor=?,total_billed_minor=total_billed_minor+?,status=?,provider_suspended=0,
                   used_traffic_bytes=CASE WHEN ? IS NULL THEN used_traffic_bytes ELSE ? END,
                   updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=?""",
                (cursor, int(used_traffic_bytes) if mode == "usage" else int(row["billed_traffic_bytes"] or 0),
                 "0" if insufficient else str(remainder), new_debt, charged,
                 "suspended_balance" if insufficient else "active", used_traffic_bytes, used_traffic_bytes,
                 self.tenant_id, account_id),
            )
            db.commit()
            return {"account_id": account_id, "mode": mode, "charged_minor": charged, "due_minor": due,
                    "delta_bytes": delta_bytes, "billing_cursor": cursor, "suspend_required": insufficient,
                    "billing_status": "suspended_balance" if insufficient else "active"}
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def confirm_reseller_suspended(self, *, account_id: str) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE reseller_accounts SET provider_suspended=1,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=? AND status='suspended_balance' AND provider_suspended=0", (self.tenant_id, account_id))
            if cur.rowcount != 1: raise CustomerStoreError("Reseller is not awaiting suspension confirmation")
            db.execute("UPDATE customer_services SET status='suspended',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND reseller_account_id=? AND status IN ('active','limited')", (self.tenant_id, account_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def resume_reseller_billing(self, *, account_id: str, now: int) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner_telegram_id,currency,pricing_mode,status,outstanding_debt_minor,traffic_cap_bytes,used_traffic_bytes,expires_at FROM reseller_accounts WHERE tenant_id=? AND account_id=?", (self.tenant_id, account_id)).fetchone()
            if not row: raise CustomerStoreError("Reseller account not found")
            if row["pricing_mode"] not in {"hourly", "usage"}: raise CustomerStoreError("Reseller does not use metered billing")
            if row["status"] != "suspended_balance": raise CustomerStoreError("Reseller is not suspended for balance or quota")
            if row["expires_at"]:
                from datetime import datetime, timezone
                if datetime.fromisoformat(row["expires_at"]) <= datetime.fromtimestamp(now, timezone.utc):
                    raise CustomerStoreError("Reseller plan has expired and cannot be resumed")
            if int(row["traffic_cap_bytes"] or 0) > 0 and int(row["used_traffic_bytes"] or 0) >= int(row["traffic_cap_bytes"]):
                raise CustomerStoreError("Reseller traffic cap is exhausted; upgrade or renew the plan before resuming")
            balance = int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?", (self.tenant_id, int(row["owner_telegram_id"]), str(row["currency"]))).fetchone()[0])
            debt = int(row["outstanding_debt_minor"] or 0)
            if balance < debt + 1:
                raise CustomerStoreError(f"Wallet needs at least {debt + 1:,} {row['currency']} to settle reseller debt and resume")
            if debt:
                db.execute("INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note) VALUES(?,?,?,?,?,'reseller_debt_settlement',?,?)",
                    (self.tenant_id, int(row["owner_telegram_id"]), f"reseller-resume-debt:{account_id}:{now}", -debt,
                     str(row["currency"]), account_id, "Settle outstanding reseller metering debt before resuming"))
            db.execute("UPDATE reseller_accounts SET status='active',provider_suspended=0,outstanding_debt_minor=0,last_billed_at=?,fractional_carry='0',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND account_id=?", (now, self.tenant_id, account_id))
            db.execute("UPDATE customer_services SET status='active',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND reseller_account_id=? AND status='suspended'", (self.tenant_id, account_id))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def list_all_reseller_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT account_id,owner_telegram_id,panel_id,provider_account_id,username,plan_id,pricing_mode,status,currency,unit_price_minor,setup_fee_minor,expires_at,traffic_cap_bytes,used_traffic_bytes,billed_traffic_bytes,last_billed_at,total_billed_minor,outstanding_debt_minor,provider_suspended,created_at FROM reseller_accounts WHERE tenant_id=? ORDER BY created_at DESC", (self.tenant_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_reseller_accounts(self, telegram_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT account_id,panel_id,provider_account_id,username,plan_id,pricing_mode,status,currency,unit_price_minor,setup_fee_minor,expires_at,traffic_cap_bytes,used_traffic_bytes,billed_traffic_bytes,last_billed_at,total_billed_minor,outstanding_debt_minor,provider_suspended,created_at FROM reseller_accounts WHERE tenant_id=? AND owner_telegram_id=? ORDER BY created_at DESC", (self.tenant_id, telegram_id)).fetchall()
        return [dict(row) for row in rows]

    def create_ticket(self, *, ticket_id: str, telegram_id: int, subject: str,
                      first_message: str) -> None:
        if not ticket_id.strip() or not subject.strip() or not first_message.strip():
            raise CustomerStoreError("Ticket ID, subject and message are required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT telegram_id,subject FROM customer_tickets WHERE tenant_id=? AND ticket_id=?", (self.tenant_id, ticket_id)).fetchone()
            if prior:
                if int(prior["telegram_id"]) != telegram_id or str(prior["subject"]) != subject[:256]:
                    raise CustomerStoreError("Ticket ID already belongs to a different request")
                db.commit(); return
            db.execute("INSERT INTO customer_tickets(tenant_id,ticket_id,telegram_id,subject) VALUES(?,?,?,?)",
                       (self.tenant_id, ticket_id, telegram_id, subject[:256]))
            db.execute("INSERT INTO customer_ticket_messages(tenant_id,ticket_id,sender_type,body) VALUES(?,?, 'customer', ?)",
                       (self.tenant_id, ticket_id, first_message[:4000]))
            now = time.time()
            db.execute("""INSERT OR IGNORE INTO customer_ticket_notifications
                (tenant_id,ticket_id,status,attempts,available_at,created_at)
                VALUES(?,?,'pending',0,?,?)""", (self.tenant_id, ticket_id, now, now))
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def claim_customer_ticket_notifications(self, *, limit: int = 20, now: float | None = None,
                                            lease_seconds: int = 300) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Ticket notification batch limit must be between 1 and 100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise CustomerStoreError("Ticket notification lease must be between 30 and 3600 seconds")
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE customer_ticket_notifications SET status='pending',claimed_at=NULL,available_at=?
                WHERE tenant_id=? AND status='processing' AND claimed_at IS NOT NULL AND claimed_at<=?""",
                (ts, self.tenant_id, ts - lease_seconds))
            rows = db.execute("""SELECT n.notification_id,n.ticket_id,n.attempts,t.telegram_id,t.subject,t.status AS ticket_status,
                    t.created_at,u.username,u.first_name,m.body
                FROM customer_ticket_notifications n
                JOIN customer_tickets t ON t.tenant_id=n.tenant_id AND t.ticket_id=n.ticket_id
                LEFT JOIN customer_users u ON u.tenant_id=t.tenant_id AND u.telegram_id=t.telegram_id
                LEFT JOIN customer_ticket_messages m ON m.message_id=(SELECT MIN(m2.message_id) FROM customer_ticket_messages m2
                    WHERE m2.tenant_id=t.tenant_id AND m2.ticket_id=t.ticket_id)
                WHERE n.tenant_id=? AND n.status='pending' AND n.available_at<=?
                ORDER BY n.notification_id LIMIT ?""", (self.tenant_id, ts, limit)).fetchall()
            claimed = []
            for row in rows:
                cur = db.execute("""UPDATE customer_ticket_notifications SET status='processing',attempts=attempts+1,claimed_at=?
                    WHERE tenant_id=? AND notification_id=? AND status='pending'""",
                    (ts, self.tenant_id, row["notification_id"]))
                if cur.rowcount == 1:
                    item = dict(row); item["attempts"] = int(row["attempts"]) + 1; claimed.append(item)
            db.commit(); return claimed
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

    def mark_customer_ticket_notification_sent(self, notification_id: int, *, now: float | None = None) -> None:
        ts = time.time() if now is None else float(now)
        with self._connect() as db:
            cur = db.execute("""UPDATE customer_ticket_notifications SET status='sent',sent_at=?,claimed_at=NULL,last_error_code=NULL
                WHERE tenant_id=? AND notification_id=? AND status='processing'""", (ts, self.tenant_id, notification_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Ticket notification is not in processing state")

    def retry_customer_ticket_notification(self, notification_id: int, *, attempt: int, error_code: str,
                                           now: float | None = None, max_attempts: int = 8) -> str:
        if not error_code or len(error_code) > 80 or any(ch.isspace() for ch in error_code):
            raise CustomerStoreError("Ticket notification error code must be short and non-sensitive")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise CustomerStoreError("Invalid ticket notification attempt")
        ts = time.time() if now is None else float(now)
        status = "dead" if attempt >= max_attempts else "pending"
        available = ts if status == "dead" else ts + min(3600, 2 ** min(attempt, 10))
        with self._connect() as db:
            cur = db.execute("""UPDATE customer_ticket_notifications SET status=?,available_at=?,claimed_at=NULL,last_error_code=?
                WHERE tenant_id=? AND notification_id=? AND status='processing'""",
                (status, available, error_code, self.tenant_id, notification_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Ticket notification is not in processing state")
        return status

    def get_customer_ticket_notification(self, ticket_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("""SELECT notification_id,ticket_id,status,attempts,available_at,created_at,
                claimed_at,sent_at,last_error_code FROM customer_ticket_notifications
                WHERE tenant_id=? AND ticket_id=?""", (self.tenant_id, ticket_id)).fetchone()
        return dict(row) if row else None

    def retry_customer_ticket_notification_now(self, ticket_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("""UPDATE customer_ticket_notifications SET status='pending',attempts=0,
                available_at=?,claimed_at=NULL,last_error_code=NULL
                WHERE tenant_id=? AND ticket_id=? AND status='dead'""",
                (time.time(), self.tenant_id, ticket_id))
            return cur.rowcount == 1

    def get_backup_policy(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "tenant_id": self.tenant_id, "enabled": 0, "frequency": "daily",
            "timezone_name": "UTC", "local_time": "03:00", "interval_minutes": 1440,
            "destination": "local", "local_directory": "/var/lib/anubis-bot/backups",
            "telegram_chat_id": None, "telegram_topic_id": 0, "retention_count": 14,
            "retention_days": 30, "last_run_at": None, "last_status": "never",
            "last_error_code": None, "next_run_at": None, "updated_by": None,
            "updated_at": None,
        }
        with self._connect() as db:
            row = db.execute("SELECT * FROM customer_backup_policies WHERE tenant_id=?",
                             (self.tenant_id,)).fetchone()
        if row:
            defaults.update(dict(row))
        return defaults

    def record_backup_run(self, *, run_id: str, status: str, destination: str,
                          size_bytes: int | None, reason_code: str | None,
                          now: float | None = None) -> dict[str, Any]:
        """Persist a redacted one-shot backup outcome and enqueue its admin event atomically."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", run_id):
            raise CustomerStoreError("Invalid backup run ID")
        if status not in {"success", "failed"} or destination not in {"local", "telegram", "both"}:
            raise CustomerStoreError("Invalid backup run status or destination")
        if status == "success":
            if (isinstance(size_bytes, bool) or not isinstance(size_bytes, int)
                    or not 0 <= size_bytes <= 2**63 - 1 or reason_code is not None):
                raise CustomerStoreError("Successful backup requires a valid size and no error code")
        elif (size_bytes is not None or not isinstance(reason_code, str)
              or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", reason_code)):
            raise CustomerStoreError("Failed backup requires a fixed reason code and no size")
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("""UPDATE customer_backup_policies
                SET last_run_at=?,last_status=?,last_error_code=?
                WHERE tenant_id=?""", (ts, status, reason_code, self.tenant_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Backup policy must be configured before recording a run")
            payload = {"status": status, "destination": destination}
            if size_bytes is not None:
                payload["size_bytes"] = size_bytes
            if reason_code:
                payload["reason_code"] = reason_code
            self._enqueue_admin_notification_in_transaction(db, event_key="backup",
                idempotency_key=f"backup-run:{run_id}", payload=payload, now=ts)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_backup_policy()

    def update_backup_policy(self, *, frequency: str, timezone_name: str, local_time: str,
                             interval_minutes: int, destination: str, local_directory: str,
                             telegram_chat_id: int | None, telegram_topic_id: int,
                             retention_count: int, retention_days: int,
                             updated_by: int) -> dict[str, Any]:
        if isinstance(updated_by, bool) or not isinstance(updated_by, int) or updated_by <= 0:
            raise CustomerStoreError("Invalid administrator ID")
        try:
            policy = validate_backup_policy(
                frequency=frequency, timezone_name=timezone_name, local_time=local_time,
                interval_minutes=interval_minutes, destination=destination,
                local_directory=local_directory, telegram_chat_id=telegram_chat_id,
                telegram_topic_id=telegram_topic_id, retention_count=retention_count,
                retention_days=retention_days,
            )
        except BackupPolicyError as exc:
            raise CustomerStoreError(str(exc)) from None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO customer_backup_policies
                    (tenant_id,enabled,frequency,timezone_name,local_time,interval_minutes,
                     destination,local_directory,telegram_chat_id,telegram_topic_id,
                     retention_count,retention_days,last_status,updated_by,updated_at)
                    VALUES(?,0,?,?,?,?,?,?,?,?,?,?,'never',?,CURRENT_TIMESTAMP)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                     enabled=0,frequency=excluded.frequency,timezone_name=excluded.timezone_name,
                     local_time=excluded.local_time,interval_minutes=excluded.interval_minutes,
                     destination=excluded.destination,local_directory=excluded.local_directory,
                     telegram_chat_id=excluded.telegram_chat_id,telegram_topic_id=excluded.telegram_topic_id,
                     retention_count=excluded.retention_count,retention_days=excluded.retention_days,
                     next_run_at=NULL,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                    (self.tenant_id, policy["frequency"], policy["timezone_name"], policy["local_time"],
                     policy["interval_minutes"], policy["destination"], policy["local_directory"],
                     policy["telegram_chat_id"], policy["telegram_topic_id"], policy["retention_count"],
                     policy["retention_days"], updated_by))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.get_backup_policy()

    NOTIFICATION_EVENT_KEYS = frozenset({
        "support_ticket", "purchase", "payment", "service_test", "bot_log", "backup",
        "wallet", "service_change", "security_alert",
    })

    ADMIN_NOTIFICATION_EVENT_KEYS = frozenset({
        "purchase", "payment", "service_test", "bot_log", "backup",
        "wallet", "service_change", "security_alert",
    })

    def _admin_notification_payload(self, event_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Allowlist low-risk fields only; never persist arbitrary event payloads."""
        if event_key not in self.ADMIN_NOTIFICATION_EVENT_KEYS:
            raise CustomerStoreError("Unsupported admin notification event")
        if not isinstance(payload, dict):
            raise CustomerStoreError("Notification payload must be an object")
        safe: dict[str, Any] = {}
        for key in ("public_order_id", "product_title", "status", "reason_code", "entry_type", "currency", "destination"):
            value = payload.get(key)
            if isinstance(value, str):
                value = value.strip()
                if key == "currency":
                    value = value.upper()
                    if len(value) != 3 or not value.isascii() or not value.isalpha():
                        continue
                elif key == "destination":
                    if value not in {"local", "telegram", "both"}:
                        continue
                else:
                    value = "".join(ch for ch in value if ch.isprintable())[:120]
                if value:
                    safe[key] = value
        for key in ("amount_minor", "customer_id", "size_bytes"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                if key == "amount_minor" and abs(value) <= 2**63 - 1:
                    safe[key] = value
                elif key == "customer_id" and value > 0:
                    safe[key] = value
                elif key == "size_bytes" and 0 <= value <= 2**63 - 1:
                    safe[key] = value
        return safe

    def _snapshot_admin_notification_routes(self, db: sqlite3.Connection, notification_id: int,
                                            event_key: str, *, now: float) -> int:
        routes = db.execute(
            "SELECT chat_id,topic_id FROM customer_notification_routes WHERE tenant_id=? AND event_key=? ORDER BY route_id",
            (self.tenant_id, event_key),
        ).fetchall()
        for route in routes:
            db.execute("""INSERT OR IGNORE INTO customer_admin_notification_deliveries
                (notification_id,tenant_id,chat_id,topic_id,status,attempts,available_at)
                VALUES(?,?,?,?,'pending',0,?)""",
                (notification_id, self.tenant_id, int(route["chat_id"]), int(route["topic_id"]), now))
        return len(routes)

    def _enqueue_admin_notification_in_transaction(self, db: sqlite3.Connection, *, event_key: str,
                                                    idempotency_key: str, payload: dict[str, Any],
                                                    now: float | None = None) -> int:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise CustomerStoreError("Admin notification idempotency key is invalid")
        safe = self._admin_notification_payload(event_key, payload)
        ts = time.time() if now is None else float(now)
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        cur = db.execute("""INSERT OR IGNORE INTO customer_admin_notifications
            (tenant_id,event_key,idempotency_key,payload_json,status,available_at,created_at)
            VALUES(?,?,?,?,'pending',?,?)""",
            (self.tenant_id, event_key, idempotency_key, encoded, ts, ts))
        row = db.execute("""SELECT notification_id,payload_json FROM customer_admin_notifications
            WHERE tenant_id=? AND event_key=? AND idempotency_key=?""",
            (self.tenant_id, event_key, idempotency_key)).fetchone()
        if row is None:
            raise CustomerStoreError("Admin notification could not be persisted")
        notification_id = int(row["notification_id"])
        if cur.rowcount == 1:
            self._snapshot_admin_notification_routes(db, notification_id, event_key, now=ts)
        elif str(row["payload_json"]) != encoded:
            # Same event key may be replayed only with the original safe payload.
            raise CustomerStoreError("Admin notification idempotency key reused with different payload")
        return notification_id

    def enqueue_admin_notification(self, *, event_key: str, idempotency_key: str,
                                   payload: dict[str, Any], now: float | None = None) -> int:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            notification_id = self._enqueue_admin_notification_in_transaction(
                db, event_key=event_key, idempotency_key=idempotency_key, payload=payload, now=now)
            db.commit()
            return notification_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_admin_notification_deliveries(self, *, limit: int = 20, now: float | None = None,
                                            lease_seconds: int = 300,
                                            fallback_admin_ids: tuple[int, ...] | list[int] = ()) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CustomerStoreError("Admin notification batch limit must be between 1 and 100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise CustomerStoreError("Admin notification lease must be between 30 and 3600 seconds")
        fallback = sorted({int(value) for value in fallback_admin_ids
                           if isinstance(value, int) and not isinstance(value, bool) and value > 0})
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE customer_admin_notification_deliveries
                SET status='pending',claimed_at=NULL,available_at=?
                WHERE tenant_id=? AND status='processing' AND claimed_at IS NOT NULL AND claimed_at<=?""",
                (ts, self.tenant_id, ts - lease_seconds))
            empty = db.execute("""SELECT n.notification_id,n.event_key FROM customer_admin_notifications n
                LEFT JOIN customer_admin_notification_deliveries d ON d.notification_id=n.notification_id
                WHERE n.tenant_id=? AND n.status='pending' AND n.available_at<=?
                GROUP BY n.notification_id HAVING COUNT(d.delivery_id)=0 ORDER BY n.notification_id LIMIT ?""",
                (self.tenant_id, ts, limit)).fetchall()
            for row in empty:
                count = self._snapshot_admin_notification_routes(
                    db, int(row["notification_id"]), str(row["event_key"]), now=ts)
                if count == 0:
                    for admin_id in fallback:
                        db.execute("""INSERT OR IGNORE INTO customer_admin_notification_deliveries
                            (notification_id,tenant_id,chat_id,topic_id,status,attempts,available_at)
                            VALUES(?,?,?,0,'pending',0,?)""",
                            (int(row["notification_id"]), self.tenant_id, admin_id, ts))
                remaining = db.execute("SELECT COUNT(*) FROM customer_admin_notification_deliveries WHERE notification_id=?",
                                       (int(row["notification_id"]),)).fetchone()[0]
                if not remaining:
                    db.execute("UPDATE customer_admin_notifications SET status='dead',last_error_code='no_destination' WHERE notification_id=?",
                               (int(row["notification_id"]),))
            rows = db.execute("""SELECT d.delivery_id,d.notification_id,d.chat_id,d.topic_id,d.attempts,
                    n.event_key,n.payload_json
                FROM customer_admin_notification_deliveries d
                JOIN customer_admin_notifications n ON n.notification_id=d.notification_id
                WHERE d.tenant_id=? AND d.status='pending' AND d.available_at<=? AND n.status='pending'
                ORDER BY d.delivery_id LIMIT ?""", (self.tenant_id, ts, limit)).fetchall()
            claimed = []
            for row in rows:
                cur = db.execute("""UPDATE customer_admin_notification_deliveries
                    SET status='processing',attempts=attempts+1,claimed_at=?
                    WHERE tenant_id=? AND delivery_id=? AND status='pending'""",
                    (ts, self.tenant_id, int(row["delivery_id"])))
                if cur.rowcount == 1:
                    item = dict(row)
                    item["attempts"] = int(row["attempts"]) + 1
                    try:
                        item["payload"] = json.loads(item.pop("payload_json") or "{}")
                    except (TypeError, ValueError):
                        item["payload"] = {}
                    claimed.append(item)
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _refresh_admin_notification_status(self, db: sqlite3.Connection, notification_id: int,
                                           *, now: float) -> str:
        rows = db.execute("SELECT status FROM customer_admin_notification_deliveries WHERE notification_id=?",
                          (notification_id,)).fetchall()
        statuses = [str(row["status"]) for row in rows]
        if statuses and all(status == "sent" for status in statuses):
            status, sent_at, error = "sent", now, None
        elif any(status in {"pending", "processing"} for status in statuses):
            status, sent_at, error = "pending", None, None
        else:
            status, sent_at, error = "dead", None, "delivery_failed"
        db.execute("""UPDATE customer_admin_notifications SET status=?,sent_at=?,last_error_code=?
            WHERE tenant_id=? AND notification_id=?""",
            (status, sent_at, error, self.tenant_id, notification_id))
        return status

    def mark_admin_notification_delivery_sent(self, delivery_id: int, *, now: float | None = None) -> str:
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT notification_id FROM customer_admin_notification_deliveries
                WHERE tenant_id=? AND delivery_id=? AND status='processing'""",
                (self.tenant_id, delivery_id)).fetchone()
            if not row:
                raise CustomerStoreError("Admin notification delivery is not processing")
            db.execute("""UPDATE customer_admin_notification_deliveries SET status='sent',sent_at=?,
                claimed_at=NULL,last_error_code=NULL WHERE tenant_id=? AND delivery_id=?""",
                (ts, self.tenant_id, delivery_id))
            status = self._refresh_admin_notification_status(db, int(row["notification_id"]), now=ts)
            db.commit()
            return status
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def retry_admin_notification_delivery(self, delivery_id: int, *, attempt: int, error_code: str,
                                          now: float | None = None, max_attempts: int = 8) -> str:
        if not error_code or len(error_code) > 80 or any(ch.isspace() for ch in error_code):
            raise CustomerStoreError("Admin notification error code must be short and non-sensitive")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise CustomerStoreError("Invalid admin notification attempt")
        ts = time.time() if now is None else float(now)
        status = "dead" if attempt >= max_attempts else "pending"
        available = ts if status == "dead" else ts + min(3600, 2 ** min(attempt, 10))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT notification_id FROM customer_admin_notification_deliveries
                WHERE tenant_id=? AND delivery_id=? AND status='processing'""",
                (self.tenant_id, delivery_id)).fetchone()
            if not row:
                raise CustomerStoreError("Admin notification delivery is not processing")
            db.execute("""UPDATE customer_admin_notification_deliveries SET status=?,available_at=?,
                claimed_at=NULL,last_error_code=? WHERE tenant_id=? AND delivery_id=?""",
                (status, available, error_code, self.tenant_id, delivery_id))
            parent_status = self._refresh_admin_notification_status(db, int(row["notification_id"]), now=ts)
            db.commit()
            return parent_status
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_admin_notifications(self, *, status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
        if status not in {"all", "pending", "sent", "dead"}:
            raise CustomerStoreError("Unsupported admin notification status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise CustomerStoreError("Admin notification limit must be between 1 and 200")
        sql = """SELECT n.notification_id,n.event_key,n.status,COALESCE(MAX(d.attempts),0) AS attempts,n.created_at,n.sent_at,n.last_error_code,
                    COUNT(d.delivery_id) AS destination_count,
                    SUM(CASE WHEN d.status='sent' THEN 1 ELSE 0 END) AS sent_destinations,
                    SUM(CASE WHEN d.status='dead' THEN 1 ELSE 0 END) AS dead_destinations
                 FROM customer_admin_notifications n LEFT JOIN customer_admin_notification_deliveries d
                 ON d.notification_id=n.notification_id WHERE n.tenant_id=?"""
        args: tuple[Any, ...] = (self.tenant_id,)
        if status != "all":
            sql += " AND n.status=?"
            args += (status,)
        sql += " GROUP BY n.notification_id ORDER BY n.notification_id DESC LIMIT ?"
        args += (limit,)
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def retry_admin_notification_now(self, notification_id: int, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else float(now)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT notification_id FROM customer_admin_notifications
                WHERE tenant_id=? AND notification_id=? AND status='dead'""",
                (self.tenant_id, notification_id)).fetchone()
            if not row:
                db.commit()
                return False
            db.execute("""UPDATE customer_admin_notification_deliveries SET status='pending',attempts=0,
                available_at=?,claimed_at=NULL,last_error_code=NULL
                WHERE tenant_id=? AND notification_id=? AND status='dead'""",
                (ts, self.tenant_id, notification_id))
            db.execute("""UPDATE customer_admin_notifications SET status='pending',available_at=?,
                sent_at=NULL,last_error_code=NULL WHERE tenant_id=? AND notification_id=?""",
                (ts, self.tenant_id, notification_id))
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def add_notification_route(self, *, event_key: str, chat_id: int, topic_id: int = 0,
                               created_by: int) -> dict[str, Any]:
        if event_key not in self.NOTIFICATION_EVENT_KEYS:
            raise CustomerStoreError("Unsupported notification event")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise CustomerStoreError("Telegram chat ID must be a non-zero integer")
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id < 0:
            raise CustomerStoreError("Topic ID must be a non-negative integer")
        if isinstance(created_by, bool) or not isinstance(created_by, int) or created_by <= 0:
            raise CustomerStoreError("Invalid administrator ID")
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO customer_notification_routes
                (tenant_id,event_key,chat_id,topic_id,created_by) VALUES(?,?,?,?,?)""",
                (self.tenant_id,event_key,chat_id,topic_id,created_by))
            row = db.execute("""SELECT route_id,event_key,chat_id,topic_id,created_by,created_at
                FROM customer_notification_routes WHERE tenant_id=? AND event_key=? AND chat_id=? AND topic_id=?""",
                (self.tenant_id,event_key,chat_id,topic_id)).fetchone()
        return dict(row)

    def remove_notification_route(self, *, event_key: str, chat_id: int, topic_id: int = 0) -> bool:
        if event_key not in self.NOTIFICATION_EVENT_KEYS:
            raise CustomerStoreError("Unsupported notification event")
        with self._connect() as db:
            cur = db.execute("""DELETE FROM customer_notification_routes
                WHERE tenant_id=? AND event_key=? AND chat_id=? AND topic_id=?""",
                (self.tenant_id,event_key,chat_id,topic_id))
            return cur.rowcount == 1

    def list_notification_routes(self, event_key: str | None = None) -> list[dict[str, Any]]:
        if event_key is not None and event_key not in self.NOTIFICATION_EVENT_KEYS:
            raise CustomerStoreError("Unsupported notification event")
        with self._connect() as db:
            if event_key is None:
                rows = db.execute("""SELECT route_id,event_key,chat_id,topic_id,created_by,created_at
                    FROM customer_notification_routes WHERE tenant_id=? ORDER BY event_key,route_id""",
                    (self.tenant_id,)).fetchall()
            else:
                rows = db.execute("""SELECT route_id,event_key,chat_id,topic_id,created_by,created_at
                    FROM customer_notification_routes WHERE tenant_id=? AND event_key=? ORDER BY route_id""",
                    (self.tenant_id,event_key)).fetchall()
        return [dict(row) for row in rows]

    def list_all_tickets(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as db:
            rows = db.execute(
                """SELECT ticket_id,telegram_id,subject,status,created_at,updated_at
                   FROM customer_tickets WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?""",
                (self.tenant_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def ticket_owner(self, ticket_id: str) -> int | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT telegram_id FROM customer_tickets WHERE tenant_id=? AND ticket_id=?",
                (self.tenant_id, ticket_id),
            ).fetchone()
        return int(row[0]) if row else None

    def add_ticket_message(self, *, ticket_id: str, sender_type: str, body: str,
                           status: str | None = None) -> None:
        if sender_type not in {"customer", "staff", "system"} or not body.strip():
            raise CustomerStoreError("Ticket message sender or body is invalid")
        if status is not None and status not in {"open", "waiting_customer", "waiting_staff", "closed"}:
            raise CustomerStoreError("Ticket status is invalid")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM customer_tickets WHERE tenant_id=? AND ticket_id=?",
                (self.tenant_id, ticket_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Ticket not found")
            db.execute(
                "INSERT INTO customer_ticket_messages(tenant_id,ticket_id,sender_type,body) VALUES(?,?,?,?)",
                (self.tenant_id, ticket_id, sender_type, body[:4000]),
            )
            next_status = status or ("waiting_staff" if sender_type == "customer" else "waiting_customer")
            db.execute(
                "UPDATE customer_tickets SET status=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND ticket_id=?",
                (next_status, self.tenant_id, ticket_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ticket_messages(self, ticket_id: str, telegram_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if telegram_id is None:
                owner = db.execute(
                    "SELECT 1 FROM customer_tickets WHERE tenant_id=? AND ticket_id=?",
                    (self.tenant_id, ticket_id),
                ).fetchone()
            else:
                owner = db.execute(
                    "SELECT 1 FROM customer_tickets WHERE tenant_id=? AND ticket_id=? AND telegram_id=?",
                    (self.tenant_id, ticket_id, telegram_id),
                ).fetchone()
            if not owner:
                return []
            rows = db.execute(
                """SELECT sender_type,body,created_at FROM customer_ticket_messages
                   WHERE tenant_id=? AND ticket_id=? ORDER BY message_id""",
                (self.tenant_id, ticket_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tickets(self, telegram_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT ticket_id,subject,status,created_at,updated_at FROM customer_tickets
                   WHERE tenant_id=? AND telegram_id=? ORDER BY created_at DESC""",
                (self.tenant_id, telegram_id),
            ).fetchall()
        return [dict(row) for row in rows]
