"""Durable customer-product storage for one isolated Anubis Bot instance.

Financial values are integer minor units; ledger rows are immutable and idempotent.
This module intentionally contains no Telegram or provider side effects.
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
import uuid
import time
from pathlib import Path
from typing import Any


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
            migrations = {
                "panel_connections": {"metadata_json": "TEXT NOT NULL DEFAULT '{}'"},
                "reseller_accounts": {
                    "setup_fee_minor": "INTEGER NOT NULL DEFAULT 0",
                    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                    "purchased_volume_gib": "TEXT NOT NULL DEFAULT '0'",
                    "max_users": "INTEGER NOT NULL DEFAULT 0",
                    "provider_suspended": "INTEGER NOT NULL DEFAULT 0",
                },
                "customer_products": {
                    "category_id": "TEXT NOT NULL DEFAULT ''",
                    "pricing_mode": "TEXT NOT NULL DEFAULT 'fixed'",
                    "unit_price_minor": "INTEGER NOT NULL DEFAULT 0",
                    "panel_id": "TEXT NOT NULL DEFAULT ''",
                    "group_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                },
                "customer_orders": {"product_snapshot_json": "TEXT NOT NULL DEFAULT '{}'", "reseller_account_id": "TEXT NOT NULL DEFAULT ''", "discount_code": "TEXT NOT NULL DEFAULT ''", "discount_amount_minor": "INTEGER NOT NULL DEFAULT 0"},
                "reseller_accounts": {
                    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                    "purchased_volume_gb": "TEXT NOT NULL DEFAULT '0'",
                    "max_users": "INTEGER NOT NULL DEFAULT 0",
                    "setup_fee_minor": "INTEGER NOT NULL DEFAULT 0",
                    "provider_suspended": "INTEGER NOT NULL DEFAULT 0",
                    "outstanding_debt_minor": "INTEGER NOT NULL DEFAULT 0",
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
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, declaration in columns.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            db.execute("UPDATE reseller_accounts SET idempotency_key='legacy:' || account_id WHERE idempotency_key='' OR idempotency_key IS NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_reseller_idempotency ON reseller_accounts(tenant_id,idempotency_key)")

    def _ensure_manual_shop_tables(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS manual_products (
                    tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', price_minor INTEGER NOT NULL CHECK(price_minor >= 0),
                    currency TEXT NOT NULL CHECK(length(currency)=3),
                    delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('instant','manual','inventory')),
                    delivery_payload TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,product_id)
                );
                CREATE TABLE IF NOT EXISTS manual_product_inventory (
                    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                    product_id TEXT NOT NULL, secret_payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','delivered')), order_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id,product_id,secret_payload)
                );
                CREATE TABLE IF NOT EXISTS manual_product_orders (
                    tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, telegram_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('paid','fulfilled','pending_delivery','failed')),
                    idempotency_key TEXT NOT NULL, delivery_payload TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id,order_id), UNIQUE(tenant_id,idempotency_key)
                );
            """)

    def put_manual_product(self, *, product_id: str, title: str, price_minor: int,
                           currency: str = "IRR", delivery_mode: str = "instant",
                           delivery_payload: str = "", description: str = "", enabled: bool = True) -> None:
        self._ensure_manual_shop_tables()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id.strip()) or not title.strip():
            raise CustomerStoreError("Manual product ID and title are required; ID must use ASCII letters, digits, _ or -")
        if isinstance(price_minor, bool) or not isinstance(price_minor, int) or price_minor <= 0 or price_minor > 2**63-1:
            raise CustomerStoreError("Manual product price must be a positive integer")
        currency = currency.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise CustomerStoreError("Currency must be a three-letter ASCII code")
        if delivery_mode not in {"instant", "manual", "inventory"}:
            raise CustomerStoreError("Delivery mode must be instant, inventory or manual")
        if delivery_mode == "instant" and not delivery_payload.strip():
            raise CustomerStoreError("Instant text delivery requires a non-empty delivery payload")
        with self._connect() as db:
            db.execute("""INSERT INTO manual_products
                (tenant_id,product_id,title,description,price_minor,currency,delivery_mode,delivery_payload,enabled)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,product_id) DO UPDATE SET
                title=excluded.title,description=excluded.description,price_minor=excluded.price_minor,
                currency=excluded.currency,delivery_mode=excluded.delivery_mode,
                delivery_payload=excluded.delivery_payload,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id,product_id.strip(),title.strip()[:160],description[:4000],price_minor,currency,
                 delivery_mode,delivery_payload[:12000],int(enabled)))

    def add_manual_inventory_key(self, *, product_id: str, secret_payload: str) -> int:
        self._ensure_manual_shop_tables()
        if not secret_payload.strip() or len(secret_payload) > 12000:
            raise CustomerStoreError("Inventory key must be non-empty and at most 12000 characters")
        with self._connect() as db:
            product=db.execute("SELECT delivery_mode FROM manual_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                               (self.tenant_id,product_id)).fetchone()
            if not product: raise CustomerStoreError("Active manual product not found")
            if product["delivery_mode"] != "inventory": raise CustomerStoreError("Product must use inventory delivery mode")
            try:
                cur=db.execute("INSERT INTO manual_product_inventory(tenant_id,product_id,secret_payload) VALUES(?,?,?)",
                               (self.tenant_id,product_id,secret_payload.strip()))
            except sqlite3.IntegrityError:
                raise CustomerStoreError("This key is already in inventory") from None
            return int(cur.lastrowid)

    def manual_inventory_count(self, product_id: str) -> int:
        self._ensure_manual_shop_tables()
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM manual_product_inventory WHERE tenant_id=? AND product_id=? AND status='available'",
                                  (self.tenant_id,product_id)).fetchone()[0])

    def list_manual_products(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        self._ensure_manual_shop_tables()
        sql="SELECT product_id,title,description,price_minor,currency,delivery_mode FROM manual_products WHERE tenant_id=?"
        args: tuple[Any,...]=(self.tenant_id,)
        if enabled_only: sql += " AND enabled=1"
        sql += " ORDER BY created_at,product_id"
        with self._connect() as db: return [dict(r) for r in db.execute(sql,args).fetchall()]

    def get_manual_product(self, product_id: str, *, enabled_only: bool = True) -> dict[str, Any] | None:
        self._ensure_manual_shop_tables()
        sql="SELECT * FROM manual_products WHERE tenant_id=? AND product_id=?"
        args: tuple[Any,...]=(self.tenant_id,product_id)
        if enabled_only: sql += " AND enabled=1"
        with self._connect() as db:
            row=db.execute(sql,args).fetchone()
        return dict(row) if row else None

    def buy_manual_product(self, *, telegram_id: int, product_id: str, idempotency_key: str) -> dict[str, Any]:
        self._ensure_manual_shop_tables()
        db=self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT * FROM manual_product_orders WHERE tenant_id=? AND idempotency_key=?",
                             (self.tenant_id,idempotency_key)).fetchone()
            if prior:
                if int(prior["telegram_id"]) != telegram_id or prior["product_id"] != product_id:
                    raise CustomerStoreError("Manual product idempotency key reused")
                result=dict(prior); result["idempotent_replay"]=True; db.commit(); return result
            product=db.execute("SELECT * FROM manual_products WHERE tenant_id=? AND product_id=? AND enabled=1",
                               (self.tenant_id,product_id)).fetchone()
            if not product: raise CustomerStoreError("Manual product is unavailable")
            customer=db.execute("SELECT 1 FROM customer_users WHERE tenant_id=? AND telegram_id=?",
                                (self.tenant_id,telegram_id)).fetchone()
            if not customer: raise CustomerStoreError("Customer must be registered before purchase")
            balance=int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM wallet_ledger WHERE tenant_id=? AND telegram_id=? AND currency=?",
                                   (self.tenant_id,telegram_id,product["currency"])).fetchone()[0])
            if balance < int(product["price_minor"]): raise CustomerStoreError("Insufficient wallet balance")
            order_id=uuid.uuid4().hex
            mode=product["delivery_mode"]
            status="pending_delivery" if mode=="manual" else "fulfilled"
            payload=product["delivery_payload"] if mode=="instant" else ""
            inventory_id=None
            if mode == "inventory":
                key_row=db.execute("SELECT inventory_id,secret_payload FROM manual_product_inventory WHERE tenant_id=? AND product_id=? AND status='available' ORDER BY inventory_id LIMIT 1",
                                   (self.tenant_id,product_id)).fetchone()
                if not key_row: raise CustomerStoreError("Manual product is out of stock; no wallet debit was made")
                inventory_id=int(key_row["inventory_id"]); payload=str(key_row["secret_payload"])
                cur=db.execute("UPDATE manual_product_inventory SET status='delivered',order_id=? WHERE tenant_id=? AND inventory_id=? AND status='available'",
                               (order_id,self.tenant_id,inventory_id))
                if cur.rowcount != 1: raise CustomerStoreError("Inventory key was already reserved")
            db.execute("""INSERT INTO manual_product_orders
                (tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key,delivery_payload)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.tenant_id,order_id,telegram_id,product_id,int(product["price_minor"]),product["currency"],status,idempotency_key,payload))
            db.execute("""INSERT INTO wallet_ledger(tenant_id,telegram_id,idempotency_key,amount_minor,currency,entry_type,reference,note)
                VALUES(?,?,?,?,?,?,?,?)""",
                (self.tenant_id,telegram_id,"manual-product:"+order_id,-int(product["price_minor"]),product["currency"],
                 "purchase",order_id,"Manual digital product purchase"))
            result={"order_id":order_id,"product_id":product_id,"amount_minor":int(product["price_minor"]),
                    "currency":product["currency"],"status":status,"delivery_mode":mode,
                    "delivery_payload":payload,"title":product["title"],"idempotent_replay":False}
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
        sql = """SELECT o.order_id,o.telegram_id,o.product_id,p.title,o.amount_minor,o.currency,o.status,o.created_at
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
            db.execute("""CREATE TABLE IF NOT EXISTS manual_product_order_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL,
                event_type TEXT NOT NULL, actor_telegram_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("INSERT INTO manual_product_order_events(tenant_id,order_id,event_type,actor_telegram_id) VALUES(?,?,?,?)",
                       (self.tenant_id, order_id, "fulfilled", fulfilled_by))
            result = dict(row)
            result.update(status="fulfilled", delivery_payload=delivery_payload)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
                "SELECT status FROM customer_orders WHERE tenant_id=? AND order_id=?",
                (self.tenant_id, order_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Order not found")
            current = str(row["status"])
            if new_status == current:
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
        if not external_reference.strip():
            raise CustomerStoreError("Verified external payment reference is required")
        if isinstance(verified_amount_minor, bool) or not isinstance(verified_amount_minor, int) or not 0 < verified_amount_minor <= 2**63 - 1:
            raise CustomerStoreError("Verified payment amount must be a positive signed 64-bit integer")
        verified_currency = verified_currency.upper()
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
            db.execute(
                """UPDATE payment_attempts SET status='verified',external_reference=?,
                   updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND payment_id=? AND status='pending'""",
                (external_reference[:256], self.tenant_id, payment_id),
            )
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

    def fail_payment_attempt(self, *, payment_id: str, failure_code: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM payment_attempts WHERE tenant_id=? AND payment_id=?",
                (self.tenant_id, payment_id),
            ).fetchone()
            if not row:
                raise CustomerStoreError("Payment attempt not found")
            if row["status"] == "failed":
                return
            if row["status"] != "pending":
                raise CustomerStoreError("Only pending payments can be failed")
            db.execute(
                "UPDATE payment_attempts SET status='failed',failure_code=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND payment_id=?",
                (failure_code[:128], self.tenant_id, payment_id),
            )

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
                """SELECT telegram_id,amount_minor,currency,entry_type,ledger_id
                   FROM wallet_ledger WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if prior:
                same = (int(prior["telegram_id"]) == telegram_id
                        and int(prior["amount_minor"]) == amount_minor
                        and prior["currency"] == currency and prior["entry_type"] == entry_type)
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
            existing = db.execute("SELECT telegram_id,product_id,reseller_account_id FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            if (not existing or int(existing["telegram_id"]) != telegram_id
                    or existing["product_id"] != str(product.get("product_id", ""))
                    or str(existing["reseller_account_id"] or "") != str(reseller_account_id or "")):
                raise CustomerStoreError("Service ID is already bound to a different customer, reseller or product")

    def get_customer_service(self, service_id: str, telegram_id: int | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            if telegram_id is None:
                row = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=?", (self.tenant_id, service_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=? AND telegram_id=?", (self.tenant_id, service_id, telegram_id)).fetchone()
        return dict(row) if row else None

    def reserve_service_change(self, *, operation_id: str, telegram_id: int, service_id: str,
                               change_type: str, idempotency_key: str, now: int) -> dict[str, Any]:
        """Reserve wallet funds and persist a service-change intent before any provider mutation."""
        if change_type not in {"renewal", "traffic_topup"}:
            raise CustomerStoreError("Unsupported service change")
        if not operation_id or not idempotency_key or len(idempotency_key) > 200:
            raise CustomerStoreError("Valid operation and idempotency IDs are required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
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
            # Additive migration for databases created by earlier versions.
            operation_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(customer_service_operations)").fetchall()}
            for column, definition in (
                ("before_expires_at", "TEXT"),
                ("before_traffic_limit_bytes", "INTEGER"),
                ("target_expires_at", "TEXT"),
                ("target_traffic_limit_bytes", "INTEGER"),
            ):
                if column not in operation_columns:
                    db.execute(f"ALTER TABLE customer_service_operations ADD COLUMN {column} {definition}")
            previous = db.execute("SELECT * FROM customer_service_operations WHERE tenant_id=? AND idempotency_key=?",
                                  (self.tenant_id, idempotency_key)).fetchone()
            if previous:
                if int(previous["telegram_id"]) != telegram_id or previous["service_id"] != service_id or previous["change_type"] != change_type:
                    raise CustomerStoreError("Service-change idempotency key belongs to a different request")
                service = db.execute("SELECT * FROM customer_services WHERE tenant_id=? AND service_id=? AND telegram_id=?",
                                     (self.tenant_id, service_id, telegram_id)).fetchone()
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
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def flag_service_change_reconciliation(self, *, operation_id: str, reason_code: str) -> None:
        with self._connect() as db:
            cur = db.execute("UPDATE customer_service_operations SET status=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND operation_id=? AND status='pending'",
                             (f"reconcile_required:{''.join(c for c in reason_code.lower() if c.isalnum() or c in '_-')[:60] or 'unknown'}",
                              self.tenant_id, operation_id))
            if cur.rowcount != 1:
                raise CustomerStoreError("Service-change operation is not pending")

    def list_service_change_operations(self, *, status: str = "reconcile_required", limit: int = 30) -> list[dict[str, Any]]:
        """List tenant-scoped service changes without exposing panel credentials or subscription URLs."""
        if status not in {"pending", "reconcile_required", "completed", "refunded", "all"}:
            raise CustomerStoreError("Unsupported service-operation status filter")
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_service_operations'").fetchone()
            if not exists:
                return []
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(customer_service_operations)").fetchall()}
            for column, definition in (("before_expires_at", "TEXT"), ("before_traffic_limit_bytes", "INTEGER"),
                                       ("target_expires_at", "TEXT"), ("target_traffic_limit_bytes", "INTEGER")):
                if column not in columns:
                    db.execute(f"ALTER TABLE customer_service_operations ADD COLUMN {column} {definition}")
            query = """SELECT o.operation_id,o.telegram_id,o.service_id,o.change_type,o.amount_minor,
                o.currency,o.quantity,o.status,o.created_at,o.updated_at,o.before_expires_at,
                o.before_traffic_limit_bytes,o.target_expires_at,o.target_traffic_limit_bytes,s.username,s.product_id,
                s.status AS service_status FROM customer_service_operations o
                LEFT JOIN customer_services s ON s.tenant_id=o.tenant_id AND s.service_id=o.service_id
                WHERE o.tenant_id=?"""
            params: list[Any] = [self.tenant_id]
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
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_service_operations'").fetchone()
            if not exists:
                return None
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(customer_service_operations)").fetchall()}
            for column, definition in (("before_expires_at", "TEXT"), ("before_traffic_limit_bytes", "INTEGER"),
                                       ("target_expires_at", "TEXT"), ("target_traffic_limit_bytes", "INTEGER")):
                if column not in columns:
                    db.execute(f"ALTER TABLE customer_service_operations ADD COLUMN {column} {definition}")
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
            row = db.execute("SELECT status,telegram_id,product_id,amount_minor,currency FROM customer_orders WHERE tenant_id=? AND order_id=?", (self.tenant_id, order_id)).fetchone()
            if not row:
                raise CustomerStoreError("Order not found")
            if row["status"] == "active":
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
            db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()

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
