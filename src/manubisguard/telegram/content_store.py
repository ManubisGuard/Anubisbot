"""Persistent, per-bot editable content settings.

Each deployment points ANUBIS_CONTENT_DB at its own writable database. The
namespace prevents the mother factory and customer product from sharing text.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = "/var/lib/anubis-bot/content.sqlite3"
MAX_TEXT_LENGTH = 4000
EDITABLE_KEYS = frozenset({
    "title",
    "welcome_text",
    "logo_file_id",
    "logo_url",
    "menu_shop",
    "menu_miniapp",
    "menu_trials",
    "menu_profile",
    "menu_orders",
    "menu_myresellers",
    "menu_admin",
    "menu_services",
    "menu_panels",
    "menu_resellers",
    "menu_wallet",
    "menu_trust",
    "menu_support",
    "menu_settings",
    "menu_miniapp_enabled",
    "menu_trials_enabled",
    "menu_shop_enabled",
    "menu_services_enabled",
    "menu_profile_enabled",
    "menu_orders_enabled",
    "menu_resellers_enabled",
    "menu_myresellers_enabled",
    "menu_wallet_enabled",
    "menu_support_enabled",
    "menu_admin_enabled",
    "home_keyboard_mode",
    "menu_create",
    "menu_bots",
    "menu_miniapp",
    "menu_guide",
    "menu_factory_settings",
    "menu_status",
    "page_dashboard",
    "page_bots",
    "page_guide",
    "page_design",
    "page_settings",
    "page_shop",
    "page_services",
    "page_panels",
    "page_resellers",
    "page_wallet",
    "page_trust",
    "page_support",
    "webapp_eyebrow",
    "webapp_headline",
    "webapp_subtitle",
    "webapp_new_bot",
    "webapp_nav_dashboard",
    "webapp_nav_bots",
    "webapp_nav_guide",
    "webapp_nav_settings",
    "webapp_metric_instances",
    "webapp_metric_instances_hint",
    "webapp_metric_isolation",
    "webapp_metric_isolation_value",
    "webapp_metric_isolation_hint",
    "webapp_metric_provisioning",
    "webapp_metric_provisioning_value",
    "webapp_metric_provisioning_hint",
    "webapp_steps_title",
    "webapp_steps_hint",
    "webapp_step_token_title",
    "webapp_step_token_body",
    "webapp_step_admin_title",
    "webapp_step_admin_body",
    "webapp_step_isolation_title",
    "webapp_step_isolation_body",
    "webapp_step_install_title",
    "webapp_step_install_body",
    "webapp_bots_title",
    "webapp_bots_empty_title",
    "webapp_bots_empty_body",
    "webapp_guide_title",
    "webapp_status_badge",
    "webapp_notice_title",
    "webapp_notice_body",
    "webapp_bots_subtitle",
    "webapp_guide_subtitle",
    "webapp_guide_step1_title",
    "webapp_guide_step1_body",
    "webapp_guide_step2_title",
    "webapp_guide_step2_body",
    "webapp_guide_step3_title",
    "webapp_guide_step3_body",
    "webapp_guide_step4_title",
    "webapp_guide_step4_body",
    "webapp_guide_notice_title",
    "webapp_guide_notice_body",
    "webapp_settings_subtitle",
    "webapp_settings_theme_title",
    "webapp_settings_theme_value",
    "webapp_settings_port_title",
    "webapp_settings_port_body",
    "webapp_settings_port_status",
    "webapp_settings_secrets_title",
    "webapp_settings_secrets_body",
    "webapp_settings_secrets_status",
    "webapp_settings_backup_title",
    "webapp_settings_backup_body",
    "webapp_settings_backup_status",
    "webapp_modal_title",
    "webapp_modal_intro",
    "webapp_form_name_label",
    "webapp_form_username_label",
    "webapp_form_admin_label",
    "webapp_form_theme_label",
    "webapp_form_submit",
    "webapp_form_cancel",
    "webapp_token_notice_title",
    "webapp_token_notice_body",
    "webapp_settings_title",
})


class ContentStoreError(ValueError):
    """Raised when an editable content value is invalid."""


class BotContentStore:
    """SQLite-backed content overrides isolated by bot namespace."""

    def __init__(
        self,
        namespace: str,
        defaults: dict[str, str],
        db_path: str | None = None,
    ) -> None:
        if not namespace or len(namespace) > 120:
            raise ContentStoreError("A valid content namespace is required")
        self.namespace = namespace
        self.defaults = {key: str(value) for key, value in defaults.items() if key in EDITABLE_KEYS}
        self.db_path = db_path or os.environ.get("ANUBIS_CONTENT_DB", DEFAULT_DB_PATH)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS content_schema_migrations (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            connection.execute("BEGIN IMMEDIATE")
            try:
                applied = {int(row[0]) for row in connection.execute(
                    "SELECT version FROM content_schema_migrations"
                ).fetchall()}
                if any(version > 1 for version in applied):
                    raise ContentStoreError("Content database schema is newer than this application supports")
                if 1 not in applied:
                    # Adopt the existing lazy-created table without replacing user content.
                    connection.execute("""CREATE TABLE IF NOT EXISTS bot_content (
                        namespace TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(namespace, key)
                    )""")
                    connection.execute(
                        "INSERT INTO content_schema_migrations(version,name) VALUES(1,?)",
                        ("bot_content_namespace_overrides",),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get(self, key: str) -> str:
        if key not in EDITABLE_KEYS:
            raise ContentStoreError("Unsupported content key")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_content WHERE namespace=? AND key=?",
                (self.namespace, key),
            ).fetchone()
        if row is not None:
            return str(row[0])
        return self.defaults.get(key, "")

    def _publish_public_brand(self) -> None:
        if self.namespace != "mother":
            return
        output = os.environ.get("ANUBIS_BRAND_JSON")
        if not output and Path("/opt/manubisguard-miniapp-stage/public").is_dir():
            output = "/opt/manubisguard-miniapp-stage/public/brand.json"
        if not output:
            return
        values = self.all()
        payload = {key: values.get(key, "") for key in ("title", "logo_url", "welcome_text")}
        payload["texts"] = {key: value for key, value in values.items() if key.startswith("webapp_")}
        target = Path(output)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            # The database remains authoritative even if static Mini App publishing fails.
            return

    def set(self, key: str, value: str) -> None:
        if key not in EDITABLE_KEYS:
            raise ContentStoreError("Unsupported content key")
        if not isinstance(value, str):
            raise ContentStoreError("Content must be text")
        value = value.strip()
        if key != "logo_file_id" and len(value) > MAX_TEXT_LENGTH:
            raise ContentStoreError(f"Content exceeds {MAX_TEXT_LENGTH} characters")
        if key == "logo_url" and value and not value.startswith("https://"):
            raise ContentStoreError("Logo URL must start with https://")
        if key == "logo_file_id" and len(value) > 1024:
            raise ContentStoreError("Invalid Telegram image reference")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO bot_content(namespace, key, value, updated_at)
                   VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(namespace, key) DO UPDATE SET
                     value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (self.namespace, key, value),
            )
        self._publish_public_brand()

    def all(self) -> dict[str, str]:
        result = dict(self.defaults)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value FROM bot_content WHERE namespace=?",
                (self.namespace,),
            ).fetchall()
        result.update({str(key): str(value) for key, value in rows})
        return result

    def reset(self, key: str) -> None:
        if key not in EDITABLE_KEYS:
            raise ContentStoreError("Unsupported content key")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM bot_content WHERE namespace=? AND key=?",
                (self.namespace, key),
            )
        self._publish_public_brand()
