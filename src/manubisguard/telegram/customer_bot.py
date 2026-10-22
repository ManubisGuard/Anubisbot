"""Customer-facing sales/service bot UI, deliberately separate from the mother bot."""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from manubisguard.telegram.control_bot import TelegramAPIError, TelegramBotAPI
from manubisguard.telegram.content_admin import (
    begin_content_edit, content_admin_keyboard, content_value, save_pending_content,
)
from manubisguard.telegram.content_store import BotContentStore
from manubisguard.telegram.subscription_qr import (
    SubscriptionQRError, make_subscription_qr_png, validate_subscription_url,
)
from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.backup_policy import next_scheduled_run
from manubisguard.panels.registry import default_registry
from manubisguard.sales.service import CustomerPurchaseService, CustomerServiceChangeService, PurchaseNeedsReconciliation
from manubisguard.sales.billing_worker import CustomerBillingWorker
from manubisguard.resellers.billing_worker import ResellerBillingWorker
from manubisguard.resellers.models import PricingMode, ResellerPlan
from manubisguard.resellers.providers import MultiPanelResellerProvider
from manubisguard.resellers.service import PurchaseError, ResellerPurchaseService
from manubisguard.notifications.trust_channel import (SQLiteNotificationOutbox, TrustChannelDeliveryWorker, TrustEvent)

LOG = logging.getLogger("manubisguard.telegram.customer_bot")


def _customer_webapp_url() -> str | None:
    value = os.environ.get("MANUBIS_CUSTOMER_WEBAPP_URL", "").strip()
    if not value or len(value) > 2048 or any(ord(ch) < 32 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return None
    except ValueError:
        return None
    return value


@dataclass(frozen=True, slots=True)
class CustomerBotConfig:
    token: str = field(repr=False)
    admin_ids: frozenset[int]
    poll_timeout: int = 25
    request_timeout: float = 35.0

    @classmethod
    def from_env(cls) -> "CustomerBotConfig":
        token = os.environ.get("MANUBISGUARD_CUSTOMER_BOT_TOKEN", "").strip()
        raw_ids = os.environ.get("MANUBISGUARD_CUSTOMER_ADMIN_IDS", "").strip()
        if not token:
            raise ValueError("MANUBISGUARD_CUSTOMER_BOT_TOKEN is required")
        if not raw_ids:
            raise ValueError("MANUBISGUARD_CUSTOMER_ADMIN_IDS is required")
        try:
            admin_ids = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        except ValueError:
            raise ValueError("MANUBISGUARD_CUSTOMER_ADMIN_IDS must contain numeric IDs") from None
        if not admin_ids or any(x <= 0 for x in admin_ids):
            raise ValueError("At least one positive numeric customer admin ID is required")
        return cls(token=token, admin_ids=admin_ids)


CUSTOMER_MENU = {
    "inline_keyboard": [
        [{"text": "🛒 خرید سرویس و اشتراک", "callback_data": "customer:shop", "style": "primary"}],
        [{"text": "⚙️ سرویس‌ها و کانفیگ‌های من", "callback_data": "customer:services"}],
        [{"text": "👤 حساب کاربری من", "callback_data": "customer:profile"}],
        [{"text": "🧾 سفارش‌های من", "callback_data": "customer:orders:0"}],
        [{"text": "💳 کیف پول و شارژ", "callback_data": "customer:wallet", "style": "success"},
         {"text": "🎫 پشتیبانی و ثبت تیکت", "callback_data": "customer:support"}],
    ]
}
ADMIN_MENU = {"inline_keyboard": [
    [{"text":"🖥 مدیریت پنل‌ها","callback_data":"admin:panels","style":"primary"}],
    [{"text":"🛍 مدیریت فروشگاه و محصولات","callback_data":"admin:products","style":"primary"}],
    [{"text":"👥 مدیریت پلن‌های نمایندگی","callback_data":"admin:resellers"}],
    [{"text":"✍️ متن‌ها، دکمه‌ها و لوگو","callback_data":"admin:content"}],
    [{"text":"💰 کیف پول و پرداخت‌ها","callback_data":"admin:finance"}],
    [{"text":"🎫 تیکت‌های پشتیبانی","callback_data":"admin:tickets"}],
    [{"text":"⚙️ تنظیمات ربات","callback_data":"admin:settings"}],
    [{"text":"⬅️ بازگشت به منوی مشتری","callback_data":"customer:home"}],
]}
BACK_MENU = {"inline_keyboard": [[{"text": "⬅️ منوی اصلی", "callback_data": "customer:home", "style": "primary"}]]}

ADMIN_ONLY_COMMANDS = frozenset({
    "/pending-deposits", "/deposit-receipt-view", "/wallet-reject", "/wallet-verify",
    "/wallet-credit", "/payment-events", "/trust-channel", "/trust-channel-status",
    "/panel", "/panel-enable", "/panels", "/panel-check", "/panel-roles",
    "/discount", "/category", "/categories", "/service-operations", "/service-operation",
    "/service-operation-check", "/service-operation-finalize",
    "/product-orders", "/product-order", "/product-order-check", "/product-order-finalize",
    "/backup-policy", "/notify-route",
    "/admin-notifications", "/admin-notification-retry", "/ticket-notification-retry",
    "/manual-key", "/manual-file", "/manual-orders", "/manual-fulfill", "/manual-notification-retry",
    "/manual-product", "/manual-variant", "/product", "/reseller-plan", "/reseller-plans",
    "/reseller-plan-enable", "/products", "/resellers", "/tickets", "/reply", "/admin",
})


def _provider_freshness_text(provider_checked_at: str | None, *, now: datetime | None = None) -> str:
    """Describe snapshot age without treating a missing or malformed timestamp as fresh."""
    if not provider_checked_at:
        return "⚪ تازگی اطلاعات: هنوز از پنل بررسی نشده است."
    raw = str(provider_checked_at).strip()
    try:
        checked = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP values are UTC timestamps without an offset.
            checked = checked.replace(tzinfo=timezone.utc)
        checked = checked.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return "⚪ تازگی اطلاعات: زمان آخرین بررسی قابل تشخیص نیست؛ از پنل به‌روزرسانی کنید."
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current.astimezone(timezone.utc) - checked).total_seconds()
    if age_seconds < -300:
        return "⚠️ تازگی اطلاعات: ساعت ثبت بررسی با ساعت فعلی سازگار نیست؛ دوباره از پنل بررسی کنید."
    if age_seconds < 0:
        age_seconds = 0
    if age_seconds <= 15 * 60:
        return "🟢 تازگی اطلاعات: بررسی پنل در ۱۵ دقیقه اخیر ثبت شده است."
    if age_seconds <= 24 * 60 * 60:
        age_minutes = max(1, int(age_seconds // 60))
        return f"🟡 تازگی اطلاعات: حدود {age_minutes} دقیقه از بررسی پنل گذشته است."
    age_hours = int(age_seconds // 3600)
    return f"🔴 تازگی اطلاعات: حدود {age_hours} ساعت از بررسی پنل گذشته؛ داده‌ها قدیمی‌اند."

PAGES = {
    "shop": ("🛍 فروشگاه و خرید سرویس", "محصولات فعال از کاتالوگ مدیر نمایش داده می‌شوند. خریدهای کیف پولی پس از تأیید اتصال پنل ثبت می‌شوند."),
    "services": ("🔐 سرویس‌های من", "سرویس‌های ثبت‌شده، حجم و اعتبار از پایگاه داده ربات نمایش داده می‌شوند. به‌روزرسانی مصرف و تعلیق/فعال‌سازی به اتصال سالم پنل و اجرای سرویس زمان‌بندی‌شده وابسته است."),
    "resellers": ("👥 نمایندگی‌ها", "پلن‌های ثابت، نامحدود، ساعتی و مصرفی از کاتالوگ مدیریت خوانده می‌شوند. ایجاد نمایندگی پنل فقط در پنل‌هایی مجاز است که API و مجوز لازم را واقعاً پشتیبانی کنند."),
    "panels": ("🧩 پنل‌ها و اتصال‌ها", "PasarGuard، Rebecca و Sanaei/3x-ui آداپترهای جداگانه دارند. مدیر می‌تواند اتصال خواندنی را بررسی کند؛ ساخت/ویرایش/حذف واقعی باید با حساب آزمایشی همان نسخه پنل تأیید شود."),
    "wallet": ("💳 کیف پول و پرداخت", "موجودی از دفترکل ثبت‌شده خوانده می‌شود. /deposit مبلغ را به‌صورت درخواست شارژ ثبت می‌کند؛ مدیر فقط پس از بررسی مستقل پرداخت می‌تواند آن را تأیید کند. درگاه خودکار و وب‌هوک امضاشده هنوز متصل نیستند."),
    "trust": ("📣 کانال اعتماد", "مدیر می‌تواند کانال و رویدادها را تنظیم کند. اعلان‌ها از صف پایدار با جلوگیری از تکرار و فیلتر اطلاعات محرمانه ارسال می‌شوند؛ ارسال دوره‌ای صف به اجرای ربات یا تایمر نیاز دارد."),
    "support": ("🎧 پشتیبانی", "تیکت‌ها و پیام‌های مشتری در پایگاه داده ذخیره می‌شوند. مدیر می‌تواند با /tickets آن‌ها را ببیند و با /reply TICKET_ID | پیام پاسخ دهد؛ پیوست‌ها هنوز پشتیبانی نمی‌شوند."),
    "settings": ("⚙️ تنظیمات محصول", "تنظیمات محصول و دسترسی‌ها باید از بخش مدیریت تنظیم و پس از اعتبارسنجی ذخیره شوند؛ اتصال فرم تنظیمات هنوز تکمیل نشده است."),
}


MENU_VISIBILITY_FIELDS = (
    ("menu_miniapp_enabled", "📱 ورود به مینی‌اپلیکیشن", "customer:miniapp"),
    ("menu_shop_enabled", "🛒 خرید سرویس و اشتراک", "customer:shop"),
    ("menu_services_enabled", "⚙️ سرویس‌های من", "customer:services"),
    ("menu_profile_enabled", "👤 حساب کاربری من", "customer:profile"),
    ("menu_orders_enabled", "🧾 سفارش‌های من", "customer:orders:0"),
    ("menu_resellers_enabled", "🏪 خرید پنل نمایندگی", "customer:resellers"),
    ("menu_myresellers_enabled", "🗂 پنل‌های من", "customer:myresellers"),
    ("menu_wallet_enabled", "💳 کیف پول و شارژ", "customer:wallet"),
    ("menu_support_enabled", "🎫 پشتیبانی و ثبت تیکت", "customer:support"),
    ("menu_admin_enabled", "🛡 دکمه پنل مدیریت (فقط ادمین)", "admin:home"),
)


def keyboard_mode(content: BotContentStore) -> str:
    mode = content_value(content, "home_keyboard_mode", "reply").strip().lower()
    return "inline" if mode == "inline" else "reply"


def keyboard_mode_admin(content: BotContentStore) -> dict[str, Any]:
    mode = keyboard_mode(content)
    label = "🟢 کیبورد شیشه‌ای: فعال" if mode == "inline" else "⚪ کیبورد شیشه‌ای: خاموش (کیبورد پایین)"
    return {"inline_keyboard": [[{"text": label, "callback_data": "admin:keyboard-mode"}],
                                [{"text": "⬅️ بازگشت", "callback_data": "admin:home"}]]}



def _enabled(content: BotContentStore, key: str, default: bool = True) -> bool:
    value = content_value(content, key, "on" if default else "off").strip().lower()
    return value not in {"0", "false", "off", "no", "disabled", "خاموش", "غیرفعال"}


def menu_visibility_keyboard(content: BotContentStore) -> dict[str, Any]:
    rows = []
    for key, label, _callback in MENU_VISIBILITY_FIELDS:
        enabled = _enabled(content, key)
        rows.append([{"text": f"{'🟢' if enabled else '⚫'} {label}: {'روشن' if enabled else 'خاموش'}",
                      "callback_data": f"content:toggle:{key}"}])
    rows.append([{"text": "⬅️ بازگشت به مدیریت متن‌ها", "callback_data": "admin:content"}])
    return {"inline_keyboard": rows}


class _TelegramTrustSender:
    def __init__(self, api: TelegramBotAPI): self.api = api
    async def send_message(self, chat_id: str, text: str) -> None:
        target: Any = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        await self.api.send_text(target, text)  # Telegram accepts numeric IDs or @channel usernames.


class CustomerBot:
    """A customer bot instance. Never start it with the mother-bot token."""

    def __init__(self, config: CustomerBotConfig):
        self.config = config
        self.api = TelegramBotAPI(config)  # type: ignore[arg-type]
        self._offset: int | None = None
        self._pending_content: dict[int, str] = {}
        self._pending_support: set[int] = set()
        self._pending_purchase_discount: dict[int, dict[str, str]] = {}
        self.data = CustomerDataStore()
        self.registry = default_registry(secret_resolver=self._resolve_panel_secret)
        self.purchase_service = CustomerPurchaseService(self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
        self.reseller_purchase_service = ResellerPurchaseService(self.data, MultiPanelResellerProvider(self.data, secret_resolver=self._resolve_panel_secret))
        self.bot_instance_id = os.environ.get("ANUBIS_CUSTOMER_BOT_INSTANCE", "customer").strip() or "customer"
        self.trust_outbox = SQLiteNotificationOutbox(self.data.db_path)
        self.trust_worker = TrustChannelDeliveryWorker(self.trust_outbox, self.data.get_trust_channel_config,
            lambda tenant, bot: _TelegramTrustSender(self.api) if bot == self.bot_instance_id and tenant == self.data.tenant_id else None)
        self.content = BotContentStore(
            os.environ.get("ANUBIS_CUSTOMER_CONTENT_NAMESPACE", "customer"),
            {
                "title": "ANUBIS BOT",
                "welcome_text": "ربات فروش و خدمات مستقل مشتری؛ متن و تصویر و تمام برچسب‌های منو از بخش مدیریت محتوا قابل ویرایش است.",
                "logo_file_id": "", "logo_url": "",
                "menu_miniapp_enabled": "on",
                "menu_shop_enabled": "on", "menu_services_enabled": "on",
                "menu_profile_enabled": "on", "menu_profile": "👤 حساب کاربری من",
                "menu_orders_enabled": "on", "menu_orders": "🧾 سفارش‌های من",
                "menu_resellers_enabled": "on", "menu_myresellers_enabled": "on",
                "menu_wallet_enabled": "on", "menu_support_enabled": "on", "menu_admin_enabled": "on",
                "menu_shop": "🛒 خرید سرویس", "menu_miniapp": "📱 ورود به مینی‌اپلیکیشن",
                "menu_myresellers": "🗂 پنل‌های من",
                "menu_admin": "🛡 پنل مدیریت ربات", "menu_services": "⚙️ سرویس‌های من",
                "menu_panels": "🧩 پنل‌ها و اتصال‌ها", "menu_resellers": "🏪 خرید پنل نمایندگی",
                "menu_wallet": "💳 کیف پول و شارژ", "menu_trust": "📣 کانال اعتماد",
                "menu_support": "🎫 ارتباط با پشتیبانی / ثبت تیکت", "menu_settings": "⚙️ تنظیمات محصول",
                **{f"page_{key}": f"{title}\n\n{body}" for key, (title, body) in PAGES.items()},
            },
        )

    def _resolve_panel_secret(self, secret_ref: str) -> str:
        if secret_ref.startswith("vault:"):
            account_id = secret_ref[6:].strip()
            if not re.fullmatch(r"[a-f0-9]{32}", account_id):
                raise ValueError("Invalid encrypted credential reference")
            credential = self.data.get_reseller_credential(account_id)
            if not credential:
                raise ValueError("Encrypted reseller credential is unavailable")
            return credential["password"]
        if not secret_ref.startswith("env:"):
            raise ValueError("Panel secret references must use env:VARIABLE_NAME or vault:ACCOUNT_ID")
        name = secret_ref[4:].strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ValueError("Invalid environment secret reference")
        value = os.environ.get(name, "")
        if not value:
            raise ValueError("Referenced panel credential environment variable is empty")
        return value

    def _customer_menu(self, *, include_admin: bool = False, customer_id: int | None = None) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        def add(key: str, label: str, callback: str, style: str | None = None) -> None:
            if not _enabled(self.content, key):
                return
            item: dict[str, Any] = {"text": content_value(self.content, label, label.replace("menu_", "")) if label.startswith("menu_") else label,
                                    "callback_data": callback}
            if style:
                item["style"] = style
            rows.append([item])
        miniapp_url = _customer_webapp_url()
        if _enabled(self.content, "menu_miniapp_enabled") and miniapp_url:
            miniapp_label = content_value(self.content, "menu_miniapp", "📱 ورود به مینی‌اپلیکیشن")
            rows.append([{"text": miniapp_label, "web_app": {"url": miniapp_url}, "style": "primary"}])
        customer_row: list[dict[str, Any]] = []
        if _enabled(self.content, "menu_shop_enabled"):
            customer_row.append({"text": content_value(self.content, "menu_shop", "🛒 خرید سرویس و اشتراک"),
                                 "callback_data": "customer:shop", "style": "primary"})
        if customer_row:
            rows.append(customer_row)
        add("menu_services_enabled", "menu_services", "customer:services")
        add("menu_profile_enabled", "menu_profile", "customer:profile")
        if customer_id is not None and self.data.list_reseller_accounts(customer_id) and _enabled(self.content, "menu_myresellers_enabled"):
            add("menu_myresellers_enabled", "menu_myresellers", "customer:myresellers")
        add("menu_resellers_enabled", "menu_resellers", "customer:resellers", "primary")
        account_row: list[dict[str, Any]] = []
        if _enabled(self.content, "menu_orders_enabled"):
            account_row.append({"text": content_value(self.content, "menu_orders", "🧾 سفارش‌های من"),
                                "callback_data": "customer:orders:0"})
        if _enabled(self.content, "menu_wallet_enabled"):
            account_row.append({"text": content_value(self.content, "menu_wallet", "💳 کیف پول و شارژ"),
                                "callback_data": "customer:wallet", "style": "success"})
        if _enabled(self.content, "menu_support_enabled"):
            account_row.append({"text": content_value(self.content, "menu_support", "🎫 پشتیبانی و ثبت تیکت"),
                                "callback_data": "customer:support"})
        if account_row:
            rows.append(account_row)
        if include_admin and _enabled(self.content, "menu_admin_enabled"):
            rows.append([{"text": content_value(self.content, "menu_admin", "🛡 پنل مدیریت ربات"),
                          "callback_data": "admin:home", "style": "primary"}])
        return {"inline_keyboard": rows}

    async def _send_brand_message(self, chat_id: int, keyboard: dict[str, Any]) -> None:
        logo = content_value(self.content, "logo_file_id")
        title = content_value(self.content, "title", "Anubis Bot")
        mode = keyboard_mode(self.content)
        reply_keyboard = {
            "keyboard": [
                [
                    ({"text": button["text"], "web_app": button["web_app"]}
                     if isinstance(button.get("web_app"), dict) else {"text": button["text"]})
                    for button in row
                ]
                for row in keyboard.get("inline_keyboard", []) if row
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "از منو انتخاب کن",
        }
        if not reply_keyboard["keyboard"]:
            reply_keyboard = {"remove_keyboard": True}
        welcome = content_value(self.content, "welcome_text", "این ربات مستقل فروش و خدمات مشتری است.")
        body = f"🛡 {title}\n\n{welcome}"
        selected_markup = keyboard if mode == "inline" else reply_keyboard
        if logo:
            try:
                await self.api.call("sendPhoto", {
                    "chat_id": chat_id, "photo": logo, "caption": body[:1024],
                    "reply_markup": selected_markup, "protect_content": True,
                })
                if len(body) > 1024:
                    await self.api.send_text(chat_id, body[1024:])
                return
            except TelegramAPIError:
                LOG.warning("Could not send configured customer bot start photo; falling back to text")
        await self.api.send_text(chat_id, body, selected_markup)

    def _authorized(self, user: dict[str, Any] | None, chat: dict[str, Any] | None) -> bool:
        user = user or {}
        chat = chat or {}
        uid = user.get("id")
        return isinstance(uid, int) and uid in self.config.admin_ids and chat.get("type") == "private" and chat.get("id") == uid

    async def _deliver_manual_delivery_notifications(self, *, now: float | None = None,
                                                       limit: int = 20) -> dict[str, int]:
        """Drain the durable customer-delivery queue; retries are at-least-once."""
        counts = {"sent": 0, "retry": 0, "dead": 0}
        for item in self.data.claim_manual_delivery_notifications(limit=limit, now=now):
            try:
                target = int(item["telegram_id"])
                title = str(item.get("product_title_snapshot") or item["product_id"])
                body = (f"✅ سفارش دیجیتال شما تحویل شد.\nمحصول: {title}\n"
                        f"شناسه سفارش: {item['order_id']}\n\nاطلاعات تحویل:\n{item['delivery_payload']}")
                await self.api.send_text(target, body,
                    self._customer_menu(customer_id=target, include_admin=False))
                self.data.mark_manual_delivery_notification_sent(item["notification_id"], now=now)
                counts["sent"] += 1
            except Exception as exc:
                code = type(exc).__name__.lower()
                try:
                    status = self.data.retry_manual_delivery_notification(
                        item["notification_id"], attempt=int(item["attempts"]),
                        error_code=code, now=now, max_attempts=8)
                    counts["dead" if status == "dead" else "retry"] += 1
                except Exception as state_exc:
                    LOG.error("Manual delivery notification state update failed for order %s (%s)",
                              item["order_id"], type(state_exc).__name__)
                LOG.warning("Manual delivery notification failed for order %s (%s)",
                            item["order_id"], type(exc).__name__)
        return counts

    async def _deliver_admin_notifications(self, *, now: float | None = None, limit: int = 20) -> dict[str, int]:
        """Deliver shared admin events one destination at a time; successful routes are never retried."""
        counts = {"sent": 0, "retry": 0, "dead": 0}
        items = self.data.claim_admin_notification_deliveries(
            limit=limit, now=now, fallback_admin_ids=tuple(self.config.admin_ids))
        labels = {
            "purchase": "خرید جدید", "payment": "وضعیت پرداخت", "service_test": "تست سرویس",
            "bot_log": "گزارش ربات", "backup": "وضعیت بک‌آپ", "wallet": "تغییر کیف پول",
            "service_change": "تغییر سرویس", "security_alert": "هشدار امنیتی",
        }
        for item in items:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            event_key = str(item["event_key"])
            lines = [f"📣 {labels.get(event_key, 'اعلان مدیریتی')}", f"رویداد: {event_key}"]
            if payload.get("public_order_id"):
                lines.append(f"شناسه: {payload['public_order_id']}")
            if payload.get("product_title"):
                lines.append(f"محصول: {payload['product_title']}")
            if payload.get("customer_id"):
                lines.append(f"شناسه کاربر: {payload['customer_id']}")
            if payload.get("amount_minor") is not None and payload.get("currency"):
                lines.append(f"مبلغ: {payload['amount_minor']} {payload['currency']}")
            if payload.get("entry_type"):
                lines.append(f"نوع تراکنش: {payload['entry_type']}")
            if event_key == "backup" and payload.get("destination"):
                lines.append(f"مقصد بک‌آپ: {payload['destination']}")
            if event_key == "backup" and payload.get("size_bytes") is not None:
                lines.append(f"حجم بک‌آپ: {int(payload['size_bytes']):,} بایت")
            if payload.get("status"):
                lines.append(f"وضعیت: {payload['status']}")
            if payload.get("reason_code"):
                lines.append(f"کد علت: {payload['reason_code']}")
            target = int(item["chat_id"])
            topic_id = int(item["topic_id"]) or None
            try:
                keyboard = ADMIN_MENU if target in self.config.admin_ids and topic_id is None else None
                await self.api.send_text(target, "\n".join(lines), keyboard, message_thread_id=topic_id)
                self.data.mark_admin_notification_delivery_sent(int(item["delivery_id"]), now=now)
                counts["sent"] += 1
            except Exception as exc:
                code = type(exc).__name__.lower()
                try:
                    status = self.data.retry_admin_notification_delivery(
                        int(item["delivery_id"]), attempt=int(item["attempts"]),
                        error_code=code, now=now, max_attempts=8)
                    counts["dead" if status == "dead" else "retry"] += 1
                except Exception as state_exc:
                    LOG.error("Admin notification state update failed (%s)", type(state_exc).__name__)
                LOG.warning("Admin notification delivery failed for event %s (%s)",
                            event_key, type(exc).__name__)
        return counts

    async def _deliver_customer_ticket_notifications(self, *, now: float | None = None, limit: int = 20) -> dict[str, int]:
        """Deliver durable Mini App and Telegram support tickets to configured bot admins."""
        counts = {"sent": 0, "retry": 0, "dead": 0, "no_admin": 0}
        for item in self.data.claim_customer_ticket_notifications(limit=limit, now=now):
            try:
                routes = self.data.list_notification_routes("support_ticket")
                targets = [(int(route["chat_id"]), int(route["topic_id"]) or None) for route in routes]
                if not targets:
                    targets = [(admin_id, None) for admin_id in sorted(self.config.admin_ids)]
                if not targets:
                    counts["no_admin"] += 1
                    self.data.retry_customer_ticket_notification(item["notification_id"], attempt=item["attempts"],
                        error_code="admin_destination_missing", now=now)
                    continue
                username = ("@" + str(item["username"])) if item.get("username") else "بدون نام کاربری"
                body = (f"🎫 تیکت پشتیبانی جدید\nشناسه: {item['ticket_id']}\nعنوان: {item['subject']}\n"
                        f"کاربر: {item['telegram_id']} ({username})\nوضعیت: {item['ticket_status']}\n\n"
                        f"{str(item.get('body') or '')[:3000]}\n\nپاسخ در گفت‌وگوی خصوصی ربات: /reply {item['ticket_id']} | متن پاسخ")
                for target_chat, topic_id in targets:
                    keyboard = ADMIN_MENU if target_chat in self.config.admin_ids and topic_id is None else None
                    await self.api.send_text(target_chat, body, keyboard, message_thread_id=topic_id)
                self.data.mark_customer_ticket_notification_sent(item["notification_id"], now=now)
                counts["sent"] += 1
            except Exception as exc:
                code = type(exc).__name__.lower()
                try:
                    status = self.data.retry_customer_ticket_notification(item["notification_id"],
                        attempt=int(item["attempts"]), error_code=code, now=now, max_attempts=8)
                    counts["dead" if status == "dead" else "retry"] += 1
                except Exception as state_exc:
                    LOG.error("Support ticket notification state update failed (%s)", type(state_exc).__name__)
                LOG.warning("Support ticket notification failed for %s (%s)", item.get("ticket_id"), type(exc).__name__)
        return counts

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")
        uid = user.get("id")
        if (not isinstance(chat_id, int) or not isinstance(uid, int)
                or chat.get("type") != "private" or chat_id != uid):
            return
        is_admin = uid in self.config.admin_ids
        if is_admin:
            pending_reply = save_pending_content(self.content, self._pending_content, chat_id, message)
            if pending_reply is not None:
                await self.api.send_text(chat_id, pending_reply, content_admin_keyboard("customer"))
                return
        text = (message.get("text") or message.get("caption") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        deposit_info_payload = text.partition(" ")[2].strip().lower() if command == "/deposit-info" else ""
        admin_command_attempt = command in ADMIN_ONLY_COMMANDS or (
            command == "/deposit-info" and deposit_info_payload.startswith("set "))
        if not is_admin and admin_command_attempt:
            # Coalesce repeated attempts from one user within a minute to prevent alert floods.
            alert_window = int(time.time() // 60)
            try:
                self.data.enqueue_admin_notification(
                    event_key="security_alert",
                    idempotency_key=f"unauthorized-admin-command:{uid}:{alert_window}",
                    payload={"status": "blocked", "reason_code": "unauthorized_admin_command", "customer_id": uid},
                )
            except Exception as exc:
                LOG.warning("Security alert enqueue failed (%s)", type(exc).__name__)
            await self.api.send_text(chat_id, "این دستور فقط برای مدیر مجاز است.", self._customer_menu())
            return
        self.data.ensure_customer(uid, user.get("username", ""), user.get("first_name", ""))
        if uid in self._pending_purchase_discount and text and not text.startswith("/"):
            await self._handle_discount_code_message(chat_id, uid, text)
            return
        if command == "/cancel" and uid in self._pending_purchase_discount:
            self._pending_purchase_discount.pop(uid, None)
            await self.api.send_text(chat_id, "ورود کد تخفیف لغو شد.", self._customer_menu())
            return
        if command and uid in self._pending_purchase_discount:
            self._pending_purchase_discount.pop(uid, None)
        if uid in self._pending_support and text and not text.startswith("/"):
            self._pending_support.discard(uid)
            ticket_id = uuid.uuid4().hex[:12]
            try:
                self.data.create_ticket(ticket_id=ticket_id, telegram_id=uid,
                                        subject=text[:80], first_message=text)
            except Exception:
                LOG.exception("Could not persist customer support ticket")
                await self.api.send_text(chat_id, "ثبت تیکت انجام نشد؛ لطفاً دوباره تلاش کن.", self._customer_menu())
                return
            await self.api.send_text(chat_id, f"✅ تیکت پشتیبانی ثبت شد.\nشماره پیگیری: {ticket_id}", self._customer_menu())
            return
        if text and not text.startswith("/"):
            home = self._customer_menu(customer_id=uid, include_admin=is_admin)
            for row in home.get("inline_keyboard", []):
                for button in row:
                    if text == button.get("text"):
                        await self._handle_callback({"from": user, "message": message,
                                                     "data": button.get("callback_data", "")})
                        return
        LOG.info("Customer command received: %s", command or "[non-text]")
        menu = self._customer_menu(customer_id=uid)
        if command == "/my-service-operations":
            try:
                operations = self.data.list_service_change_operations(status="all", limit=10, telegram_id=int(uid))
                lines = ["🧾 پیگیری تمدید و افزایش حجم سرویس‌ها"]
                status_labels = {
                    "pending": "در حال بررسی؛ تا تعیین تکلیف دوباره تلاش نکنید",
                    "completed": "تکمیل‌شده",
                    "refunded": "بازپرداخت‌شده",
                }
                for op in operations:
                    raw_status = str(op["status"])
                    if raw_status.startswith("reconcile_required:"):
                        status_label = "نیازمند بررسی پشتیبانی؛ دوباره تلاش نکنید"
                    else:
                        status_label = status_labels.get(raw_status, "نامشخص")
                    kind = "تمدید" if op["change_type"] == "renewal" else "افزایش حجم"
                    lines.append(
                        f"\n• شناسه: {op['operation_id']}\nنوع: {kind} | وضعیت: {status_label}\n"
                        f"مبلغ: {int(op['amount_minor']):,} {op['currency']} | ثبت: {op['created_at']}"
                    )
                if not operations:
                    lines.append("هنوز عملیات تمدید یا افزایش حجمی برای حساب شما ثبت نشده است.")
                lines.append("\nاین بخش فقط وضعیت ثبت‌شده را نشان می‌دهد و عملیات پنل یا پرداخت را تکرار نمی‌کند.")
                await self.api.send_text(chat_id, "\n".join(lines), menu)
            except CustomerStoreError:
                await self.api.send_text(chat_id, "فعلاً امکان خواندن وضعیت عملیات وجود ندارد؛ بعداً دوباره بررسی کنید.", menu)
            return
        if command == "/deposit-info":
            payload = text.partition(" ")[2].strip()
            if is_admin and payload.startswith("set "):
                fields = [part.strip() for part in payload[4:].split("|")]
                try:
                    if len(fields) != 3:
                        raise ValueError("فرمت: /deposit-info set شماره‌کارت | نام‌صاحب‌حساب | بانک")
                    self.data.set_manual_deposit_details(card_number=fields[0], account_holder=fields[1], bank_name=fields[2])
                    await self.api.send_text(chat_id, "اطلاعات کارت‌به‌کارت ذخیره شد.", menu)
                except (ValueError, CustomerStoreError) as exc:
                    await self.api.send_text(chat_id, f"اطلاعات ذخیره نشد: {exc}", menu)
                return
            details = self.data.get_manual_deposit_details()
            if not details:
                await self.api.send_text(chat_id, "اطلاعات کارت‌به‌کارت هنوز توسط مدیر تنظیم نشده است.", menu)
            else:
                await self.api.send_text(chat_id, f"💳 اطلاعات واریز دستی\nبانک: {details['bank_name'] or '—'}\nشماره کارت: {details['card_number']}\nبه نام: {details['account_holder']}\n\nپس از واریز، با /deposit-receipt PAYMENT_ID | کدپیگیری رسید را ثبت کن. شارژ فقط پس از بررسی مدیر انجام می‌شود.", menu)
            return
        if is_admin and command == "/pending-deposits":
            receipts = self.data.list_manual_deposit_receipts()
            lines = ["🧾 رسیدهای کارت‌به‌کارت در انتظار بررسی:"]
            for receipt in receipts:
                lines.append(f"• {receipt['payment_id']} | کاربر {receipt['telegram_id']} | {receipt['amount_minor']:,} {receipt['currency']} | پیگیری {receipt['transfer_reference']} | واریزکننده {receipt['payer_name'] or '—'}")
            lines.append("برای دیدن تصویر: /deposit-receipt-view PAYMENT_ID")
            lines.append("برای تأیید: /wallet-verify PAYMENT_ID | مبلغ | ارز | کدپیگیری")
            lines.append("برای رد: /wallet-reject PAYMENT_ID | دلیل")
            await self.api.send_text(chat_id, "\n".join(lines), menu)
            return
        if command == "/deposit-receipt":
            payload = text.partition(" ")[2].strip()
            fields = [part.strip() for part in payload.split("|")]
            try:
                if len(fields) not in {2,3}:
                    raise ValueError("فرمت: /deposit-receipt PAYMENT_ID | کدپیگیری | نام‌واریزکننده (اختیاری)")
                payment_id, reference = fields[:2]
                payer_name = fields[2] if len(fields) == 3 else ""
                photos = message.get("photo") or []
                receipt_file_id = str(photos[-1].get("file_id", "")) if photos else ""
                self.data.submit_manual_deposit_receipt(payment_id=payment_id, telegram_id=uid,
                    transfer_reference=reference, payer_name=payer_name, receipt_file_id=receipt_file_id)
                await self.api.send_text(chat_id, "رسید ثبت شد و در صف بررسی مدیر قرار گرفت. تا زمان تأیید، موجودی کیف پول تغییر نمی‌کند.", menu)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"رسید ثبت نشد: {exc}", menu)
            return
        if is_admin and command == "/deposit-receipt-view":
            payment_id = text.partition(" ")[2].strip()
            receipt = self.data.get_manual_deposit_receipt(payment_id) if payment_id else None
            if not receipt or not receipt.get("receipt_file_id"):
                await self.api.send_text(chat_id, "برای این پرداخت تصویر رسید ثبت نشده است.", menu)
            else:
                await self.api.call("sendPhoto", {"chat_id": chat_id, "photo": receipt["receipt_file_id"],
                    "caption": f"رسید پرداخت {payment_id} | کاربر {receipt['telegram_id']} | پیگیری {receipt['transfer_reference']}"})
            return
        if is_admin and command == "/wallet-reject":
            fields = [part.strip() for part in text.partition(" ")[2].split("|")]
            try:
                if len(fields) != 2: raise ValueError("فرمت: /wallet-reject PAYMENT_ID | دلیل")
                payment_id, reason = fields
                receipt = self.data.get_manual_deposit_receipt(payment_id)
                if not receipt: raise CustomerStoreError("رسید پیدا نشد")
                self.data.reject_manual_deposit_receipt(payment_id=payment_id, admin_id=uid, reason=reason)
                try:
                    await self.api.send_text(int(receipt["telegram_id"]), f"❌ رسید واریز {payment_id} تأیید نشد. دلیل: {reason}", self._customer_menu(include_admin=False))
                except Exception:
                    LOG.warning("Rejected manual deposit notification failed")
                await self.api.send_text(chat_id, "رسید رد شد؛ هیچ مبلغی به کیف پول اضافه نشد.", menu)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"رد رسید انجام نشد: {exc}", menu)
            return
        if is_admin and command == "/wallet-verify":
            await self._admin_wallet_verify(chat_id, text, uid)
            return
        if is_admin and command == "/wallet-credit":
            await self._admin_wallet_credit(chat_id, text, uid)
            return
        if is_admin and command == "/payment-events":
            await self._admin_payment_events(chat_id, text)
            return
        if is_admin and command == "/trust-channel":
            await self._admin_trust_channel_command(chat_id, text)
            return
        if is_admin and command == "/trust-channel-status":
            config = self.data.get_trust_channel_config(self.data.tenant_id, self.bot_instance_id)
            if not config:
                await self.api.send_text(chat_id, "کانال اعتماد هنوز تنظیم نشده است.", menu)
            else:
                await self.api.send_text(chat_id, f"کانال: {config.chat_id}\nوضعیت: {'فعال' if config.enabled else 'خاموش'}\nحریم خصوصی: {'روشن' if config.privacy_mode else 'خاموش'}\nرویدادها: {', '.join(e.value for e in sorted(config.enabled_events, key=lambda e:e.value))}", menu)
            return
        if is_admin and command == "/panel":
            await self._admin_panel_command(chat_id, text)
            return
        if is_admin and command == "/panel-enable":
            parts = text.split()
            if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
                await self.api.send_text(chat_id, "فرمت: /panel-enable PANEL_ID on|off", menu)
            else:
                try:
                    self.data.set_panel_enabled(parts[1], parts[2].lower() == "on")
                    await self.api.send_text(chat_id, "وضعیت اتصال پنل ذخیره شد؛ محصولات وابسته به پنل خاموش از فروش پنهان می‌شوند.", menu)
                except CustomerStoreError as exc:
                    await self.api.send_text(chat_id, f"وضعیت پنل تغییر نکرد: {exc}", menu)
            return
        if is_admin and command == "/panels":
            panels = self.data.list_panel_connections()
            body = "\n".join(f"• {p['panel_id']} — {p['label']} ({p['provider']}) — {'فعال' if p['enabled'] else 'غیرفعال'} — {p['last_check_status']}" for p in panels)
            await self.api.send_text(chat_id, body or "هنوز پنلی ثبت نشده است.", menu)
            return
        if is_admin and command == "/panel-check":
            await self._admin_panel_check(chat_id, text)
            return
        if is_admin and command == "/panel-roles":
            await self._admin_panel_roles(chat_id, text)
            return
        if is_admin and command == "/discount":
            await self._admin_discount_command(chat_id, text)
            return
        if is_admin and command == "/category":
            await self._admin_category_command(chat_id, text)
            return
        if is_admin and command == "/categories":
            categories = self.data.list_categories(enabled_only=False)
            body = "\n".join(f"• {c['category_id']} — {c['title']} — {'فعال' if c['enabled'] else 'خاموش'}" for c in categories)
            await self.api.send_text(chat_id, body or "دسته‌بندی‌ای ثبت نشده است.", menu)
            return
        if is_admin and command == "/service-operations":
            parts = text.split()
            status = parts[1].strip().lower() if len(parts) == 2 else "reconcile_required"
            try:
                operations = self.data.list_service_change_operations(status=status, limit=50)
                lines = [f"🧾 عملیات تمدید/شارژ ترافیک — فیلتر: {status}"]
                for op in operations:
                    kind = "تمدید" if op["change_type"] == "renewal" else "افزایش ترافیک"
                    reason = op["status"].partition(":")[2]
                    suffix = f" — علت: {reason}" if reason else ""
                    lines.append(
                        f"• {op['operation_id']} — {kind} — سرویس {op['service_id']} — "
                        f"کاربر {op['telegram_id']} — {int(op['amount_minor']):,} {op['currency']} — "
                        f"وضعیت {op['status']}{suffix}"
                    )
                if not operations:
                    lines.append("عملیاتی با این فیلتر پیدا نشد.")
                lines.append("\nجزئیات: /service-operation OPERATION_ID")
                lines.append("فیلترها: reconcile_required, pending, completed, refunded, all")
                lines.append("این فرمان فقط گزارش می‌دهد؛ هیچ عملیات پنل یا بازپرداختی انجام نمی‌دهد.")
                await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"فهرست عملیات خوانده نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/service-operation":
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                await self.api.send_text(chat_id, "فرمت: /service-operation OPERATION_ID", ADMIN_MENU)
                return
            op = self.data.get_service_change_operation(parts[1].strip())
            if not op:
                await self.api.send_text(chat_id, "عملیات پیدا نشد.", ADMIN_MENU)
                return
            kind = "تمدید" if op["change_type"] == "renewal" else "افزایش ترافیک"
            lines = [
                f"🧾 عملیات: {op['operation_id']}", f"نوع: {kind}",
                f"وضعیت عملیات: {op['status']}", f"شناسه سرویس: {op['service_id']}",
                f"نام کاربری پنل: {op.get('username') or 'نامشخص'}",
                f"شناسه مشتری تلگرام: {op['telegram_id']}",
                f"مبلغ رزروشده: {int(op['amount_minor']):,} {op['currency']}",
                f"مقدار: {int(op['quantity']):,} {'ساعت' if op['change_type'] == 'renewal' else 'بایت'}",
                f"محصول: {op.get('product_id') or 'نامشخص'}",
                f"وضعیت محلی سرویس: {op.get('service_status') or 'نامشخص'}",
                f"ثبت: {op['created_at']} | آخرین تغییر: {op['updated_at']}",
            ]
            if op["change_type"] == "renewal":
                lines.append(f"انقضای محلی فعلی: {op.get('expires_at') or 'نامشخص'}")
                lines.append(f"انقضای مشاهده‌شده قبل از تغییر: {op.get('before_expires_at') or 'ثبت نشده'}")
                lines.append(f"انقضای هدف ذخیره‌شده: {op.get('target_expires_at') or 'ثبت نشده'}")
            else:
                lines.append(f"سقف ترافیک محلی فعلی: {int(op.get('traffic_limit_bytes') or 0):,} بایت")
                lines.append(f"سقف مشاهده‌شده قبل از تغییر: {int(op.get('before_traffic_limit_bytes') or 0):,} بایت")
                lines.append(f"سقف هدف ذخیره‌شده: {int(op.get('target_traffic_limit_bytes') or 0):,} بایت")
            lines.append("این نمایش فقط از پایگاه داده محلی است؛ برای تعیین تکلیف باید وضعیت واقعی پنل جداگانه بررسی شود. هیچ retry یا بازپرداخت خودکار انجام نمی‌شود.")
            await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            return
        if is_admin and command == "/service-operation-check":
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                await self.api.send_text(chat_id, "فرمت: /service-operation-check OPERATION_ID", ADMIN_MENU)
                return
            try:
                checker = CustomerServiceChangeService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                result = await checker.inspect_reconciliation(operation_id=parts[1].strip())
                labels = {
                    "target_present": "مقدار هدف در پنل دیده می‌شود.",
                    "before_present": "پنل هنوز مقدار قبل از تغییر را نشان می‌دهد.",
                    "different": "مقدار پنل با مقدار قبل و هدف ذخیره‌شده متفاوت است.",
                    "target_not_persisted": "مقدار هدف پیش از درخواست پنل ذخیره نشده؛ مقایسه امن ممکن نیست.",
                    "provider_unsupported": "مقایسه خواندنی برای این نوع پنل پیاده‌سازی نشده است.",
                    "identity_missing": "شناسه کاربر پنل در رکورد موجود نیست.",
                    "identity_mismatch": "شناسه کاربر برگشتی پنل با رکورد محلی تطبیق ندارد.",
                    "observed_unknown": "پنل مقدار قابل مقایسه برنگرداند.",
                    "read_failed": f"خواندن پنل شکست خورد ({result.get('reason_code', 'unknown')}).",
                }
                lines = [
                    f"🔎 بررسی خواندنی عملیات: {result['operation_id']}",
                    f"وضعیت مقایسه: {labels.get(result['comparison'], result['comparison'])}",
                    f"نوع: {'تمدید' if result['change_type'] == 'renewal' else 'افزایش ترافیک'}",
                ]
                if result["change_type"] == "renewal":
                    lines.append(f"انقضای قبل: {result.get('before_expires_at') or 'ثبت نشده'}")
                    lines.append(f"انقضای هدف: {result.get('target_expires_at') or 'ثبت نشده'}")
                else:
                    lines.append(f"سقف قبل: {int(result.get('before_traffic_limit_bytes') or 0):,} بایت")
                    lines.append(f"سقف هدف: {int(result.get('target_traffic_limit_bytes') or 0):,} بایت")
                observed = result.get("observed_value")
                if observed is not None:
                    lines.append(f"مقدار خوانده‌شده از پنل: {observed}")
                lines.append("این فرمان فقط خواندن و مقایسه می‌کند؛ هیچ تغییر، retry، بازپرداخت یا نهایی‌سازی انجام نمی‌دهد.")
                await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"بررسی انجام نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/service-operation-finalize":
            fields = text.partition(" ")[2].split("|", 1)
            if len(fields) != 2 or not fields[0].strip() or not fields[1].strip():
                await self.api.send_text(chat_id,
                    "فرمت: /service-operation-finalize OPERATION_ID | دلیل تصمیم", ADMIN_MENU)
                return
            operation_id, reason = fields[0].strip(), fields[1].strip()
            try:
                checker = CustomerServiceChangeService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                result = await checker.finalize_target_present(
                    operation_id=operation_id, actor_telegram_id=int(uid), reason=reason)
                replay = " (درخواست تکراریِ بی‌اثر)" if result.get("idempotent_replay") else ""
                await self.api.send_text(chat_id,
                    f"ثبت شد{replay}: عملیات {operation_id} نهایی شد؛ مقایسه تازه و تطبیق هویت پنل هر دو target_present بودند. "
                    f"مدیر: {uid}. دلیل: {reason}. وجه مسترد یا عملیات پنل تکرار نشد.",
                    ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"نهایی‌سازی انجام نشد؛ عملیات و رزرو وجه حفظ شدند: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/product-orders":
            parts = text.split()
            status_filter = parts[1].strip().lower() if len(parts) == 2 else "reconcile_required"
            try:
                orders = self.data.list_product_order_reconciliation(status=status_filter, limit=50)
                lines = [f"🧾 سفارش‌های پنلی نیازمند تطبیق — فیلتر: {status_filter}"]
                for order in orders:
                    marker = str(order.get("provider_reference") or "")
                    reason = marker.partition(":")[2] if marker.startswith("reconcile_required:") else marker
                    intent_state = "intent ثبت‌شده" if order.get("has_provisioning_intent") else "بدون intent کامل؛ نهایی‌سازی خودکارمجاز نیست"
                    lines.append(
                        f"• {order['order_id']} — {order.get('title') or order['product_id']} — "
                        f"کاربر {order['telegram_id']} — {int(order['amount_minor']):,} {order['currency']} — "
                        f"{order['status']} — علت: {reason or 'نامشخص'} — {intent_state}"
                    )
                if not orders:
                    lines.append("سفارش مبهمی برای بررسی پیدا نشد.")
                lines.append("\nجزئیات: /product-order ORDER_ID")
                lines.append("بررسی فقط‌خواندنی پنل: /product-order-check ORDER_ID")
                lines.append("نهایی‌سازی پس از تطبیق تازه: /product-order-finalize ORDER_ID | دلیل تصمیم")
                lines.append("هیچ ساخت مجددی در پنل یا بازپرداخت خودکار انجام نمی‌شود.")
                await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"فهرست سفارش‌های بازیابی خوانده نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/product-order":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                await self.api.send_text(chat_id, "فرمت: /product-order ORDER_ID", ADMIN_MENU)
                return
            order = self.data.get_product_order_reconciliation(parts[1].strip())
            if not order:
                await self.api.send_text(chat_id, "سفارش پیدا نشد.", ADMIN_MENU)
                return
            snapshot = order.get("product_snapshot") if isinstance(order.get("product_snapshot"), dict) else {}
            intent = snapshot.get("_provisioning_intent") if isinstance(snapshot.get("_provisioning_intent"), dict) else {}
            binding = intent.get("provider_binding") if isinstance(intent.get("provider_binding"), dict) else {}
            lines = [
                f"🧾 سفارش: {order['order_id']}",
                f"محصول: {snapshot.get('title') or order['product_id']} ({order['product_id']})",
                f"مشتری: {order['telegram_id']}",
                f"وضعیت: {order['status']}",
                f"مبلغ: {int(order['amount_minor']):,} {order['currency']}",
                f"نشانگر بازیابی: {str(order.get('provider_reference') or 'ثبت نشده')}",
                f"intent پایدار: {'بله' if intent.get('version') == 1 else 'خیر'}",
            ]
            if intent:
                lines.extend([
                    f"نام کاربری مورد انتظار: {intent.get('username') or 'نامشخص'}",
                    f"پنل: {binding.get('panel_id') or 'نامشخص'}",
                    f"نوع پنل: {binding.get('provider_kind') or 'نامشخص'}",
                ])
            lines.append("جزئیات ذخیره‌شده نمایش داده شد؛ برای وضعیت زنده پنل از /product-order-check استفاده کن.")
            await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            return
        if is_admin and command == "/product-order-check":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                await self.api.send_text(chat_id, "فرمت: /product-order-check ORDER_ID", ADMIN_MENU)
                return
            try:
                checker = CustomerPurchaseService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                result = await checker.inspect_product_order_reconciliation(order_id=parts[1].strip())
                labels = {
                    "target_present": "کاربر هدف با هویت و تنظیمات ذخیره‌شده تطبیق دارد.",
                    "not_found": "خواندن پنل اعلام کرد کاربر پیدا نشد؛ ساخت مجدد خودکار ممنوع است.",
                    "read_failed": f"خواندن پنل شکست خورد ({result.get('reason_code', 'unknown')}).",
                    "binding_mismatch": "پیکربندی پنل با اتصال زمان خرید مطابقت ندارد.",
                    "identity_mismatch": "نام کاربری یا شناسه پنل با سفارش مطابقت ندارد.",
                    "identity_missing": "شناسه کافی برای تطبیق وجود ندارد.",
                    "intent_missing": "intent زمان خرید ثبت نشده؛ نهایی‌سازی خودکار امکان‌پذیر نیست.",
                    "different": "تنظیمات فعلی پنل با مقادیر مورد انتظار متفاوت است.",
                    "observed_unknown": "پنل داده کافی برای اثبات وضعیت هدف برنگرداند.",
                    "identity_claimed": "هویت پنل قبلاً به سرویس محلی دیگری متصل است.",
                }
                lines = [
                    f"🔎 بررسی فقط‌خواندنی سفارش: {result['order_id']}",
                    f"نتیجه: {labels.get(result.get('comparison'), str(result.get('comparison') or 'نامشخص'))}",
                    f"مشتری: {result.get('telegram_id', 'نامشخص')} — سفارش: {result.get('status', 'نامشخص')}",
                ]
                if result.get("username"):
                    lines.append(f"نام کاربری پنل: {result['username']}")
                if result.get("provider_user_id"):
                    lines.append(f"شناسه نرمال‌شده پنل: {result['provider_user_id']}")
                if result.get("observed_status"):
                    lines.append(f"وضعیت پنل: {result['observed_status']}")
                if result.get("observed_data_limit_bytes") is not None:
                    lines.append(f"سقف ترافیک پنل: {int(result['observed_data_limit_bytes']):,} بایت")
                if result.get("observed_expire_at"):
                    lines.append(f"انقضای پنل (UTC): {result['observed_expire_at']}")
                lines.append("این فرمان فقط خواندن و مقایسه می‌کند؛ هیچ تغییری در پنل/کیف پول یا سفارش انجام نمی‌دهد.")
                if result.get("comparison") == "target_present":
                    lines.append("برای تصمیم ثبت‌شده با کنترل تازه و audit: /product-order-finalize ORDER_ID | دلیل")
                await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"بررسی سفارش انجام نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/product-order-finalize":
            fields = text.partition(" ")[2].split("|", 1)
            if len(fields) != 2 or not fields[0].strip() or not fields[1].strip():
                await self.api.send_text(chat_id,
                    "فرمت: /product-order-finalize ORDER_ID | دلیل تصمیم", ADMIN_MENU)
                return
            order_id, reason = fields[0].strip(), fields[1].strip()
            try:
                checker = CustomerPurchaseService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                result = await checker.finalize_product_order_target_present(
                    order_id=order_id, actor_telegram_id=int(uid), reason=reason)
                replay = " (درخواست تکراریِ بی‌اثر)" if result.get("idempotent_replay") else ""
                await self.api.send_text(
                    chat_id,
                    f"ثبت شد{replay}: سفارش {order_id} پس از خواندن تازه و تطبیق هویت/تنظیمات پنل نهایی شد. "
                    f"مدیر: {uid}. دلیل: {reason}. هیچ ساخت مجدد در پنل، برداشت دوباره یا بازپرداخت انجام نشد.",
                    ADMIN_MENU,
                )
            except CustomerStoreError as exc:
                await self.api.send_text(
                    chat_id,
                    f"نهایی‌سازی انجام نشد؛ سفارش و وجه رزروشده حفظ شدند: {exc}",
                    ADMIN_MENU,
                )
            return
        if is_admin and command == "/backup-policy":
            payload = text.partition(" ")[2].strip()
            parts = payload.split(maxsplit=1)
            action = parts[0].lower() if parts else "show"
            policy = self.data.get_backup_policy()
            if action == "show":
                try:
                    preview = next_scheduled_run(now=datetime.now(timezone.utc),
                        frequency=policy["frequency"], timezone_name=policy["timezone_name"],
                        local_time=policy["local_time"], interval_minutes=int(policy["interval_minutes"]))
                    next_line = f"نوبت بعدی طبق برنامه: {preview.isoformat()} (UTC)"
                except (ValueError, TypeError):
                    next_line = "نوبت بعدی: برنامه نیاز به اصلاح دارد."
                destination_line = (f"{policy['destination']} | chat={policy['telegram_chat_id'] or '—'}"
                    f" | topic={policy['telegram_topic_id'] or 'بدون تاپیک'}")
                last_run_line = "آخرین اجرا: —"
                try:
                    if policy.get("last_run_at") is not None:
                        last_run_line = "آخرین اجرا (UTC): " + datetime.fromtimestamp(
                            float(policy["last_run_at"]), timezone.utc).isoformat(timespec="seconds")
                except (TypeError, ValueError, OSError, OverflowError):
                    last_run_line = "آخرین اجرا: داده نامعتبر"
                await self.api.send_text(chat_id,
                    "🗄 سیاست بک‌آپ (تنظیمات ذخیره‌شده، اجرا غیرفعال)\n"
                    f"وضعیت زمان‌بند: {'فعال' if policy['enabled'] else 'غیرفعال'}\n"
                    f"زمان‌بندی: {policy['frequency']} | منطقه زمانی: {policy['timezone_name']} | ساعت: {policy['local_time']} | بازه: {policy['interval_minutes']} دقیقه\n"
                    f"مقصد: {destination_line}\nمسیر محلی: {policy['local_directory']}\n"
                    f"نگهداری: حداکثر {policy['retention_count']} نسخه / {policy['retention_days']} روز\n"
                    f"آخرین وضعیت: {policy['last_status']}\n{last_run_line}\n{next_line}\n\n"
                    "این زمان فقط پیش‌نمایش است؛ زمان‌بند و ارسال فایل هنوز نصب/فعال نشده‌اند.",
                    ADMIN_MENU)
                return
            if action == "help":
                await self.api.send_text(chat_id,
                    "🗄 تنظیم سیاست بک‌آپ (بدون اجرای خودکار)\n"
                    "/backup-policy show\n"
                    "/backup-policy schedule | hourly/daily/interval | Europe/Berlin | 03:00 | 1440\n"
                    "/backup-policy destination | local/telegram/both | CHAT_ID-or-0 | TOPIC_ID | /absolute/path\n"
                    "/backup-policy retention | MAX_BACKUPS | MAX_AGE_DAYS\n"
                    "برای مقصد محلی، CHAT_ID و TOPIC_ID را 0 بگذار. زمان‌بند هنوز نصب نشده و enable عمداً رد می‌شود.", ADMIN_MENU)
                return
            if action == "enable":
                await self.api.send_text(chat_id,
                    "فعال‌سازی رد شد: زمان‌بند بک‌آپ هنوز پیاده‌سازی و آزمون عملیاتی نشده است. "
                    "تنظیمات ذخیره می‌شوند اما اجرای خودکار همچنان غیرفعال می‌ماند.", ADMIN_MENU)
                return
            try:
                if action == "schedule":
                    fields = [item.strip() for item in (parts[1] if len(parts) > 1 else "").strip().lstrip("|").strip().split("|")]
                    if len(fields) != 4:
                        raise ValueError("فرمت: /backup-policy schedule | hourly/daily/interval | Europe/Berlin | 03:00 | interval_minutes")
                    frequency, timezone_name, local_time, interval_raw = fields
                    policy = self.data.update_backup_policy(frequency=frequency, timezone_name=timezone_name,
                        local_time=local_time, interval_minutes=int(interval_raw),
                        destination=policy["destination"], local_directory=policy["local_directory"],
                        telegram_chat_id=policy["telegram_chat_id"], telegram_topic_id=int(policy["telegram_topic_id"]),
                        retention_count=int(policy["retention_count"]), retention_days=int(policy["retention_days"]),
                        updated_by=uid)
                elif action == "destination":
                    fields = [item.strip() for item in (parts[1] if len(parts) > 1 else "").strip().lstrip("|").strip().split("|")]
                    if len(fields) != 4:
                        raise ValueError("فرمت: /backup-policy destination | local/telegram/both | CHAT_ID-or-0 | TOPIC_ID | /absolute/backup/path")
                    destination, raw_chat, raw_topic, local_directory = fields
                    chat_id_value = int(raw_chat)
                    policy = self.data.update_backup_policy(frequency=policy["frequency"],
                        timezone_name=policy["timezone_name"], local_time=policy["local_time"],
                        interval_minutes=int(policy["interval_minutes"]), destination=destination,
                        local_directory=local_directory, telegram_chat_id=(chat_id_value or None),
                        telegram_topic_id=int(raw_topic), retention_count=int(policy["retention_count"]),
                        retention_days=int(policy["retention_days"]), updated_by=uid)
                elif action == "retention":
                    fields = [item.strip() for item in (parts[1] if len(parts) > 1 else "").strip().lstrip("|").strip().split("|")]
                    if len(fields) != 2:
                        raise ValueError("فرمت: /backup-policy retention | MAX_BACKUPS | MAX_AGE_DAYS")
                    policy = self.data.update_backup_policy(frequency=policy["frequency"],
                        timezone_name=policy["timezone_name"], local_time=policy["local_time"],
                        interval_minutes=int(policy["interval_minutes"]), destination=policy["destination"],
                        local_directory=policy["local_directory"], telegram_chat_id=policy["telegram_chat_id"],
                        telegram_topic_id=int(policy["telegram_topic_id"]), retention_count=int(fields[0]),
                        retention_days=int(fields[1]), updated_by=uid)
                elif action == "disable":
                    policy = self.data.update_backup_policy(frequency=policy["frequency"],
                        timezone_name=policy["timezone_name"], local_time=policy["local_time"],
                        interval_minutes=int(policy["interval_minutes"]), destination=policy["destination"],
                        local_directory=policy["local_directory"], telegram_chat_id=policy["telegram_chat_id"],
                        telegram_topic_id=int(policy["telegram_topic_id"]), retention_count=int(policy["retention_count"]),
                        retention_days=int(policy["retention_days"]), updated_by=uid)
                else:
                    raise ValueError("فرمان نامعتبر است.")
                await self.api.send_text(chat_id,
                    "تنظیمات بک‌آپ ذخیره شد؛ اجرای خودکار همچنان غیرفعال است.\n"
                    f"برنامه: {policy['frequency']} / {policy['timezone_name']} {policy['local_time']}\n"
                    f"مقصد: {policy['destination']}؛ نگهداری: {policy['retention_count']} نسخه / {policy['retention_days']} روز\n"
                    "برای مشاهده: /backup-policy show", ADMIN_MENU)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"تنظیمات ذخیره نشد: {exc}\nبرای راهنما: /backup-policy help", ADMIN_MENU)
            return
        if is_admin and command == "/notify-route":
            payload = text.partition(" ")[2].strip()
            parts = payload.split(maxsplit=1)
            action = parts[0].lower() if parts else "help"
            event_keys = sorted(self.data.NOTIFICATION_EVENT_KEYS)
            if action == "list":
                routes = self.data.list_notification_routes()
                body = ["📣 مسیرهای اعلان ربات:"]
                body.extend(f"• {r['event_key']} → chat_id={r['chat_id']}, topic_id={r['topic_id'] or 'بدون تاپیک'}" for r in routes)
                if not routes: body.append("هنوز مسیری ثبت نشده؛ اعلان تیکت‌ها فعلاً به ادمین‌های MANUBISGUARD_CUSTOMER_ADMIN_IDS می‌رود.")
                body.append("رویدادها: " + ", ".join(event_keys))
                await self.api.send_text(chat_id, "\n".join(body), ADMIN_MENU)
                return
            if action in {"add", "remove"}:
                fields = [part.strip() for part in (parts[1] if len(parts) > 1 else "").split("|")]
                try:
                    if len(fields) != 3: raise ValueError("فرمت: /notify-route add|remove EVENT_KEY | CHAT_ID | TOPIC_ID (برای بدون تاپیک 0)")
                    event_key, raw_chat, raw_topic = fields
                    if not re.fullmatch(r"[a-z_]{3,32}", event_key): raise ValueError("شناسه رویداد معتبر نیست.")
                    chat_target, topic_target = int(raw_chat), int(raw_topic)
                    if action == "add":
                        route = self.data.add_notification_route(event_key=event_key, chat_id=chat_target,
                            topic_id=topic_target, created_by=uid)
                        await self.api.send_text(chat_id, f"مسیر ذخیره شد: {route['event_key']} → chat_id={route['chat_id']}, topic_id={route['topic_id'] or 'بدون تاپیک'}. برای گروه، ربات باید اجازه ارسال پیام داشته باشد.", ADMIN_MENU)
                    else:
                        removed = self.data.remove_notification_route(event_key=event_key, chat_id=chat_target, topic_id=topic_target)
                        await self.api.send_text(chat_id, "مسیر حذف شد." if removed else "چنین مسیری پیدا نشد.", ADMIN_MENU)
                except (ValueError, CustomerStoreError) as exc:
                    await self.api.send_text(chat_id, f"مسیر ذخیره/حذف نشد: {exc}\nرویدادها: {', '.join(event_keys)}", ADMIN_MENU)
                return
            await self.api.send_text(chat_id, "📣 مسیریابی اعلان‌ها\n/notify-route list\n/notify-route add EVENT_KEY | CHAT_ID | TOPIC_ID\n/notify-route remove EVENT_KEY | CHAT_ID | TOPIC_ID\n\nTOPIC_ID را برای ارسال مستقیم 0 بگذار. رویدادهای موجود: " + ", ".join(event_keys) + "\nصف پایدار به خرید، پرداخت، کیف پول، تغییر سرویس، بررسی خواندنی پنل و هشدار تلاش غیرمجاز برای اجرای دستور مدیریتی متصل است؛ تیکت از مسیر outbox مستقل خود ارسال می‌شود. تولیدکننده‌های لاگ ربات و بک‌آپ هنوز متصل نیستند.", ADMIN_MENU)
            return
        if is_admin and command == "/admin-notifications":
            raw_status = text.partition(" ")[2].strip().lower() or "all"
            if raw_status not in {"all", "pending", "dead", "sent"}:
                await self.api.send_text(chat_id, "فرمت: /admin-notifications [all|pending|dead|sent]", ADMIN_MENU)
                return
            rows = self.data.list_admin_notifications(status=raw_status, limit=30)
            lines = [f"📣 صف اعلان‌های مدیریتی ({raw_status})"]
            for row in rows:
                lines.append(
                    f"#{row['notification_id']} {row['event_key']} — {row['status']} — "
                    f"مقصد: {row['sent_destinations'] or 0}/{row['destination_count'] or 0}"
                    + (f" — خطا: {row['last_error_code']}" if row.get("last_error_code") else ""))
            if not rows:
                lines.append("اعلانی برای نمایش وجود ندارد.")
            lines.append("برای تلاش مجدد: /admin-notification-retry ID")
            await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            return
        if is_admin and command == "/admin-notification-retry":
            parts = text.split(maxsplit=1)
            try:
                if len(parts) != 2 or not parts[1].strip().isdigit():
                    raise ValueError("فرمت: /admin-notification-retry ID")
                notification_id = int(parts[1].strip())
                if not self.data.retry_admin_notification_now(notification_id):
                    raise ValueError("اعلان متوقف‌شده‌ای با این شناسه پیدا نشد.")
                await self.api.send_text(chat_id,
                    f"اعلان #{notification_id} به صف تلاش مجدد برگشت. کارگر پس‌زمینه آن را با رعایت صف و محدودیت ارسال بررسی می‌کند.",
                    ADMIN_MENU)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, str(exc), ADMIN_MENU)
            return
        if is_admin and command == "/ticket-notification-retry":
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                await self.api.send_text(chat_id, "فرمت: /ticket-notification-retry TICKET_ID", ADMIN_MENU)
                return
            ticket_id = parts[1].strip()
            if self.data.retry_customer_ticket_notification_now(ticket_id):
                counts = await self._deliver_customer_ticket_notifications(limit=20)
                state = self.data.get_customer_ticket_notification(ticket_id)
                await self.api.send_text(chat_id, f"وضعیت اعلان تیکت {ticket_id}: {state['status'] if state else 'نامشخص'}؛ ارسال‌شده: {counts['sent']}، تلاش مجدد: {counts['retry']}، متوقف: {counts['dead']}.", ADMIN_MENU)
            else:
                await self.api.send_text(chat_id, "اعلان متوقف‌شده‌ای برای این تیکت پیدا نشد؛ فقط اعلان dead قابل راه‌اندازی مجدد است.", ADMIN_MENU)
            return
        if is_admin and command == "/manual-key":
            fields = [x.strip() for x in text.partition(" ")[2].split("|")]
            try:
                if len(fields) not in {2, 3} or not fields[0].startswith("add "):
                    raise ValueError("فرمت: /manual-key add PRODUCT_ID | KEY_OR_DELIVERY_TEXT | VARIANT_ID اختیاری")
                product_id = fields[0][4:].strip()
                variant_id = fields[2] if len(fields) == 3 else ""
                self.data.add_manual_inventory_key(product_id=product_id, secret_payload=fields[1], variant_id=variant_id)
                count = self.data.manual_inventory_count(product_id, variant_id)
                label = f" نوع {variant_id}" if variant_id else ""
                await self.api.send_text(chat_id, f"کلید به موجودی محصول {product_id}{label} اضافه شد. موجودی همین انتخاب: {count}", ADMIN_MENU)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"کلید ذخیره نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/manual-file":
            payload = text.partition(" ")[2].strip()
            document = message.get("document") or {}
            file_id = str(document.get("file_id") or "").strip()
            try:
                if not file_id:
                    raise ValueError("یک فایل را به‌صورت Document همراه کپشن /manual-file PRODUCT_ID [| VARIANT_ID] ارسال کن.")
                fields = [part.strip() for part in payload.split("|")]
                if len(fields) == 1 and fields[0]:
                    self.data.set_manual_product_file(product_id=fields[0], file_id=file_id)
                    await self.api.send_text(chat_id, "فایل محصول ذخیره شد. خریدهای بعدی این محصول فایل را پس از پرداخت دریافت می‌کنند.", ADMIN_MENU)
                elif len(fields) == 2 and all(fields):
                    self.data.set_manual_variant_file(product_id=fields[0], variant_id=fields[1], file_id=file_id)
                    await self.api.send_text(chat_id, "فایل این نوع محصول ذخیره شد. فایل فقط پس از خرید موفق همان نوع ارسال می‌شود.", ADMIN_MENU)
                else:
                    raise ValueError("فرمت: ارسال Document با کپشن /manual-file PRODUCT_ID یا /manual-file PRODUCT_ID | VARIANT_ID")
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"فایل محصول ذخیره نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/manual-variant":
            payload = text.partition(" ")[2].strip()
            try:
                if payload.startswith("list "):
                    product_id = payload[5:].strip()
                    variants = self.data.list_manual_variants(product_id, enabled_only=False)
                    lines = [f"انواع محصول {product_id}:"]
                    parent_product = self.data.get_manual_product(product_id, enabled_only=False) or {}
                    for item in variants:
                        delivery = self.data.manual_variant_delivery(product_id, item["variant_id"])
                        stock = self.data.manual_inventory_count(product_id, item["variant_id"]) if delivery["delivery_mode"] == "inventory" else None
                        delivery_label = {"instant":"خودکار", "manual":"دستی", "inventory":"کلید موجودی"}[delivery["delivery_mode"]]
                        override_label = "مستقل" if item.get("delivery_mode") else "ارث‌بری از محصول"
                        lines.append(f"• {item['variant_id']} — {item['title']} — {int(item['price_minor']):,} {parent_product.get('currency','')} — تحویل: {delivery_label} ({override_label}) — {'فعال' if item['enabled'] else 'غیرفعال'}" + (f" — موجودی: {stock}" if stock is not None else ""))
                    await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
                    return
                if payload.startswith("archive "):
                    parts = [x.strip() for x in payload[8:].split("|")]
                    if len(parts) != 2: raise ValueError("فرمت: /manual-variant archive PRODUCT_ID | VARIANT_ID")
                    product_id, variant_id = parts
                    variant = self.data.get_manual_variant(product_id, variant_id, enabled_only=False)
                    if not variant: raise ValueError("نوع محصول پیدا نشد")
                    self.data.put_manual_variant(product_id=product_id, variant_id=variant_id, title=variant["title"], price_minor=int(variant["price_minor"]), enabled=False)
                    await self.api.send_text(chat_id, f"نوع {variant_id} آرشیو شد؛ سفارش‌های قبلی و موجودی ثبت‌شده حذف نشدند.", ADMIN_MENU)
                    return
                fields = [x.strip() for x in payload.split("|")]
                if len(fields) not in {4, 6} or not (fields[0].startswith("add ") or fields[0].startswith("edit ")):
                    raise ValueError("فرمت: /manual-variant add/edit PRODUCT_ID | VARIANT_ID | عنوان | قیمت [| instant/manual/inventory/- | DELIVERY_PAYLOAD/-]")
                action, product_id = fields[0].split(maxsplit=1)
                delivery_config_provided = len(fields) == 6
                delivery_mode = (None if fields[4] == "-" else fields[4]) if delivery_config_provided else None
                delivery_payload = (None if fields[5] == "-" else fields[5]) if delivery_config_provided else None
                self.data.put_manual_variant(product_id=product_id, variant_id=fields[1], title=fields[2],
                    price_minor=int(fields[3]), enabled=True, delivery_mode=delivery_mode,
                    delivery_payload=delivery_payload, delivery_config_provided=delivery_config_provided)
                delivery_note = "روش/محتوای تحویل به‌روزرسانی شد." if delivery_config_provided else "روش تحویل از محصول اصلی به ارث می‌رسد."
                await self.api.send_text(chat_id, f"نوع محصول {fields[1]} ذخیره شد. قیمت مستقل این نوع: {int(fields[3]):,}. {delivery_note}", ADMIN_MENU)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"نوع محصول ذخیره نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/manual-orders":
            parts = text.split()
            status = parts[1].strip().lower() if len(parts) == 2 else "pending_delivery"
            try:
                orders = self.data.list_manual_orders(status=status)
                lines = ["📦 سفارش‌های محصولات دیجیتال:"]
                for order in orders:
                    title = order.get("title") or order["product_id"]
                    lines.append(
                        f"• {order['order_id']} — {title} — کاربر {order['telegram_id']} — "
                        f"{int(order['amount_minor']):,} {order['currency']} — {order['status']}"
                    )
                if not orders:
                    lines.append("سفارشی با این وضعیت پیدا نشد.")
                lines.append("\nبرای تحویل: /manual-fulfill ORDER_ID | متن تحویل")
                lines.append("برای اعلان dead: /manual-notification-retry ORDER_ID")
                lines.append("وضعیت‌های مجاز: pending_delivery, fulfilled, all")
                await self.api.send_text(chat_id, "\n".join(lines), ADMIN_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"فهرست سفارش‌ها خوانده نشد: {exc}", ADMIN_MENU)
            return
        if is_admin and command == "/manual-fulfill":
            payload = text.partition(" ")[2]
            order_id, sep, delivery_payload = payload.partition("|")
            if not sep:
                await self.api.send_text(chat_id, "فرمت: /manual-fulfill ORDER_ID | متن تحویل", ADMIN_MENU)
                return
            try:
                order = self.data.fulfill_manual_order(
                    order_id=order_id.strip(), delivery_payload=delivery_payload.strip(), fulfilled_by=uid
                )
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"تحویل ثبت نشد: {exc}", ADMIN_MENU)
                return
            await self._deliver_manual_delivery_notifications(limit=20)
            notification = self.data.get_manual_delivery_notification(order["order_id"])
            admin_result = f"تحویل سفارش {order['order_id']} ثبت شد؛ وضعیت fulfilled است."
            if notification and notification["status"] in {"pending", "processing"}:
                admin_result += " پیام مشتری در صف پایدار قرار دارد و به‌صورت خودکار دوباره تلاش می‌شود."
            elif notification and notification["status"] == "dead":
                admin_result += " ارسال خودکار پس از چند تلاش ناموفق متوقف شد؛ برای تلاش مجدد از /manual-notification-retry ORDER_ID استفاده کن."
            await self.api.send_text(
                chat_id,
                admin_result,
                ADMIN_MENU,
            )
            return
        if is_admin and command == "/manual-notification-retry":
            parts = text.split()
            if len(parts) != 2:
                await self.api.send_text(chat_id, "فرمت: /manual-notification-retry ORDER_ID", ADMIN_MENU)
                return
            if self.data.retry_manual_delivery_notification_now(parts[1]):
                counts = await self._deliver_manual_delivery_notifications(limit=20)
                state = self.data.get_manual_delivery_notification(parts[1])
                status = state["status"] if state else "unknown"
                await self.api.send_text(chat_id, f"وضعیت اعلان سفارش {parts[1]}: {status} (ارسال‌شده: {counts['sent']}، تلاش مجدد: {counts['retry']}، متوقف: {counts['dead']})", ADMIN_MENU)
            else:
                await self.api.send_text(chat_id, "اعلان متوقف‌شده‌ای با این شناسه پیدا نشد؛ فقط اعلان dead قابل راه‌اندازی مجدد است.", ADMIN_MENU)
            return
        if is_admin and command == "/manual-product":
            payload = text.partition(" ")[2].strip()
            help_text = ("مدیریت محصول دستی:\n"
                "/manual-product add ID | عنوان | قیمت | ارز | instant/inventory/manual | متن-تحویل | توضیح | category_id اختیاری | سقف خرید اختیاری | sort_order اختیاری | شرایط خرید اختیاری | image_url اختیاری | visible_from_utc اختیاری | visible_until_utc اختیاری\n"
                "/manual-product edit ID | عنوان | قیمت | ارز | instant/inventory/manual | متن-تحویل | توضیح | category_id اختیاری | سقف خرید اختیاری | sort_order اختیاری | شرایط خرید اختیاری | image_url اختیاری | visible_from_utc اختیاری | visible_until_utc اختیاری\n"
                "/manual-product archive ID\n/manual-product list\n"
                "/manual-variant add PRODUCT_ID | VARIANT_ID | عنوان | قیمت [| instant/manual/inventory/- | DELIVERY_PAYLOAD/-]\n"
                "/manual-variant edit PRODUCT_ID | VARIANT_ID | عنوان | قیمت [| instant/manual/inventory/- | DELIVERY_PAYLOAD/-]\n"
                "/manual-variant archive PRODUCT_ID | VARIANT_ID\n/manual-variant list PRODUCT_ID\n"
                "فایل محصول: ارسال Document با کپشن /manual-file PRODUCT_ID؛ فایل یک نوع: /manual-file PRODUCT_ID | VARIANT_ID\n"
                "برای موجودی محصول ساده: /manual-key add PRODUCT_ID | KEY؛ برای موجودی یک نوع: /manual-key add PRODUCT_ID | KEY | VARIANT_ID. "
                "اگر محصول نوع فعال داشته باشد، خرید باید با انتخاب یک نوع انجام شود.")
            if payload == "list":
                products = self.data.list_manual_products(enabled_only=False)
                lines = []
                for item in products[:50]:
                    status = "فعال" if item["enabled"] else "آرشیو"
                    variants = self.data.list_manual_variants(item["product_id"])
                    stock_count = (sum(self.data.manual_inventory_count(item["product_id"], v["variant_id"]) for v in variants)
                                   if variants else self.data.manual_inventory_count(item["product_id"]))
                    stock = f" — موجودی: {stock_count}" if item["delivery_mode"] == "inventory" and item["enabled"] else ""
                    category_label = item.get("category_id") or "بدون دسته"
                    purchase_cap = int(item.get("max_purchases_per_customer") or 0)
                    cap_label = f" — سقف خرید: {purchase_cap}" if purchase_cap else ""
                    image_label = " — تصویر دارد" if str(item.get("image_url") or "").strip() else ""
                    schedule_label = " — از " + str(item.get("visible_from_utc")) if item.get("visible_from_utc") else ""
                    if item.get("visible_until_utc"):
                        schedule_label += " — تا " + str(item.get("visible_until_utc"))
                    lines.append(f"• {item['product_id']} — {item['title']} — {item['price_minor']} {item['currency']} — {item['delivery_mode']} — {category_label} — ترتیب: {int(item.get('sort_order') or 0)} — {status}{stock}{cap_label}{image_label}{schedule_label}")
                await self.api.send_text(chat_id, "\n".join(lines) if lines else "محصول دستی ثبت نشده است.", ADMIN_MENU)
                return
            if payload.startswith("archive "):
                product_id = payload[8:].strip()
                try:
                    existing = self.data.get_manual_product(product_id, enabled_only=False)
                    if not existing:
                        raise ValueError("محصول دستی پیدا نشد.")
                    if not existing["enabled"]:
                        await self.api.send_text(chat_id, f"محصول {product_id} قبلاً آرشیو شده است.", ADMIN_MENU)
                        return
                    self.data.put_manual_product(product_id=product_id, title=existing["title"],
                        price_minor=int(existing["price_minor"]), currency=existing["currency"],
                        delivery_mode=existing["delivery_mode"], delivery_payload=existing["delivery_payload"],
                        description=existing["description"], enabled=False, category_id=existing.get("category_id", ""),
                        max_purchases_per_customer=int(existing.get("max_purchases_per_customer") or 0),
                        sort_order=int(existing.get("sort_order") or 0), terms_text=str(existing.get("terms_text") or ""),
                        image_url=str(existing.get("image_url") or ""),
                        visible_from_utc=str(existing.get("visible_from_utc") or "-"),
                        visible_until_utc=str(existing.get("visible_until_utc") or "-"))
                except (ValueError, CustomerStoreError) as exc:
                    await self.api.send_text(chat_id, f"آرشیو محصول انجام نشد: {exc}", ADMIN_MENU)
                    return
                await self.api.send_text(chat_id, f"محصول {product_id} آرشیو شد و دیگر قابل خرید نیست.", ADMIN_MENU)
                return
            if payload.startswith("edit "):
                fields = [x.strip() for x in payload[5:].split("|")]
                try:
                    if len(fields) not in {7, 8, 9, 10, 11, 12, 13, 14}:
                        raise ValueError("۷ فیلد لازم است؛ دسته‌بندی، سقف خرید، ترتیب، شرایط، تصویر و زمان‌بندی اختیاری‌اند.")
                    product_id, title, price_raw, currency, delivery_mode, delivery_payload, description = fields[:7]
                    existing = self.data.get_manual_product(product_id, enabled_only=False)
                    if not existing or not existing["enabled"]:
                        raise ValueError("محصول فعال پیدا نشد؛ شناسه را بررسی کنید.")
                    category_id = fields[7] if len(fields) >= 8 else existing.get("category_id", "")
                    purchase_cap = int(fields[8]) if len(fields) >= 9 else int(existing.get("max_purchases_per_customer") or 0)
                    sort_order = int(fields[9]) if len(fields) >= 10 else int(existing.get("sort_order") or 0)
                    terms_text = fields[10] if len(fields) >= 11 else str(existing.get("terms_text") or "")
                    image_url = (fields[11] if fields[11] != "-" else "") if len(fields) >= 12 else None
                    visible_from_utc = (fields[12] if fields[12] != "-" else "") if len(fields) >= 13 else None
                    visible_until_utc = (fields[13] if fields[13] != "-" else "") if len(fields) >= 14 else None
                    self.data.put_manual_product(product_id=product_id, title=title, price_minor=int(price_raw),
                        currency=currency, delivery_mode=delivery_mode.lower(), delivery_payload=delivery_payload,
                        description=description, category_id=category_id,
                        max_purchases_per_customer=purchase_cap, sort_order=sort_order, terms_text=terms_text,
                        image_url=image_url, visible_from_utc=visible_from_utc, visible_until_utc=visible_until_utc)
                except (ValueError, CustomerStoreError) as exc:
                    await self.api.send_text(chat_id, f"ویرایش محصول دستی انجام نشد: {exc}", ADMIN_MENU)
                    return
                await self.api.send_text(chat_id, f"محصول دستی «{title}» ویرایش شد.", ADMIN_MENU)
                return
            if not payload.startswith("add "):
                await self.api.send_text(chat_id, help_text, ADMIN_MENU)
                return
            fields = [x.strip() for x in payload[4:].split("|")]
            try:
                if len(fields) not in {7, 8, 9, 10, 11, 12, 13, 14}:
                    raise ValueError("۷ فیلد لازم است؛ دسته‌بندی، سقف خرید، ترتیب، شرایط، تصویر و زمان‌بندی اختیاری‌اند.")
                product_id, title, price_raw, currency, delivery_mode, delivery_payload, description = fields[:7]
                category_id = fields[7] if len(fields) >= 8 else ""
                purchase_cap = int(fields[8]) if len(fields) >= 9 else 0
                sort_order = int(fields[9]) if len(fields) >= 10 else 0
                terms_text = fields[10] if len(fields) >= 11 else ""
                image_url = (fields[11] if fields[11] != "-" else "") if len(fields) >= 12 else None
                visible_from_utc = (fields[12] if fields[12] != "-" else "") if len(fields) >= 13 else None
                visible_until_utc = (fields[13] if fields[13] != "-" else "") if len(fields) >= 14 else None
                self.data.put_manual_product(product_id=product_id, title=title, price_minor=int(price_raw),
                    currency=currency, delivery_mode=delivery_mode.lower(), delivery_payload=delivery_payload,
                    description=description, category_id=category_id,
                    max_purchases_per_customer=purchase_cap, sort_order=sort_order, terms_text=terms_text,
                    image_url=image_url, visible_from_utc=visible_from_utc, visible_until_utc=visible_until_utc)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"محصول دستی ذخیره نشد: {exc}", ADMIN_MENU)
                return
            await self.api.send_text(chat_id, f"محصول دستی «{title}» ذخیره شد. حالت تحویل: {delivery_mode}.", ADMIN_MENU)
            return
        if is_admin and command == "/product":
            await self._admin_product_command(chat_id, text)
            return
        if is_admin and command == "/reseller-plan":
            await self._admin_reseller_plan_command(chat_id, text)
            return
        if is_admin and command == "/reseller-plans":
            plans = self.data.list_reseller_plans()
            body = "\n".join(f"• {p['plan_id']} — {p['title']} — {p['pricing_mode']} — {'فعال' if p['enabled'] else 'خاموش'} — پنل {p['panel_id']}" for p in plans)
            await self.api.send_text(chat_id, body or "پلن نمایندگی تعریف نشده است.", menu)
            return
        if is_admin and command == "/reseller-plan-enable":
            parts = text.split()
            if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
                await self.api.send_text(chat_id, "فرمت: /reseller-plan-enable ID on|off", menu)
            else:
                try:
                    self.data.set_reseller_plan_enabled(parts[1], parts[2].lower() == "on")
                    await self.api.send_text(chat_id, "وضعیت پلن ذخیره شد.", menu)
                except CustomerStoreError as exc:
                    await self.api.send_text(chat_id, str(exc), menu)
            return
        if is_admin and command == "/products":
            products = self.data.list_products()
            body = "\n".join(f"• {p['product_id']} — {p['title']} — {p['price_minor']} {p['currency']} — {p['duration_hours']}h" for p in products)
            await self.api.send_text(chat_id, body or "کاتالوگ محصول خالی است.", menu)
            return
        if is_admin and command == "/resellers":
            accounts = self.data.list_all_reseller_accounts()
            body = "\n".join(f"• {a['account_id']} — {a['username']} — owner {a['owner_telegram_id']} — {a['pricing_mode']} — {a['status']}" for a in accounts)
            await self.api.send_text(chat_id, body or "هنوز نمایندگی ثبت نشده است.", menu)
            return
        if is_admin and command == "/tickets":
            tickets = self.data.list_all_tickets()
            body = "\n".join(f"• {t['ticket_id']} — user {t['telegram_id']} — {t['subject']} [{t['status']}]" for t in tickets)
            await self.api.send_text(chat_id, body or "تیکت بازی وجود ندارد.", menu)
            return
        if is_admin and command == "/reply":
            await self._admin_ticket_reply(chat_id, text)
            return
        if is_admin and command == "/admin":
            await self.api.send_text(chat_id, "🛡 پنل مدیریت ربات\nیک بخش را انتخاب کن:", ADMIN_MENU)
            return
        if is_admin and command == "/admin":
            await self.api.send_text(chat_id, "🛡 پنل مدیریت ربات\nیک بخش را انتخاب کن:", ADMIN_MENU)
            return
        if command in ("/start", "/help"):
            await self._send_brand_message(chat_id, self._customer_menu(customer_id=uid, include_admin=is_admin))
        elif command == "/status":
            await self.api.send_text(chat_id, "🟢 ربات فعال است. وضعیت سرویس‌ها و اتصال پنل فقط پس از اتصال و بررسی واقعی نمایش داده می‌شود.", menu)
        elif command == "/wallet":
            balances = self.data.wallet_balances(uid)
            debts = self.data.outstanding_debts(uid)
            balance_lines = [f"• {currency}: {amount:,}" for currency, amount in balances.items()] or ["• IRR: 0"]
            debt_lines = [f"• {currency}: {amount:,}" for currency, amount in debts.items()] or ["• بدون بدهی معوق"]
            await self.api.send_text(chat_id, "💳 موجودی کیف پول:\n" + "\n".join(balance_lines) +
                "\n\n⏳ بدهی ساعتی/مصرفی:\n" + "\n".join(debt_lines) +
                "\n\nموجودی از دفترکل ذخیره‌شده خوانده می‌شود. درگاه خودکار هنوز متصل نیست؛ شارژ دستی فقط پس از تأیید پرداخت ثبت می‌شود.", menu)
        elif command == "/shop":
            await self._show_shop(chat_id, menu)
        elif command == "/deposit":
            parts = text.split()
            try:
                if len(parts) not in {2, 3}: raise ValueError("فرمت: /deposit AMOUNT_MINOR [CURRENCY]")
                amount = int(parts[1]); currency = parts[2].upper() if len(parts) == 3 else "IRR"
                if amount <= 0 or amount > 2**63 - 1: raise ValueError("مبلغ باید مثبت و در محدوده مجاز باشد.")
                payment_id = uuid.uuid4().hex
                message_id = int(message.get("message_id") or 0)
                payment_id = self.data.create_payment_attempt(payment_id=payment_id, telegram_id=uid, provider="manual",
                    amount_minor=amount, currency=currency, idempotency_key=f"manual-deposit:{uid}:{message_id}")
                ticket_id = f"dep-{payment_id[:12]}"
                self.data.create_ticket(ticket_id=ticket_id, telegram_id=uid,
                    subject=f"درخواست شارژ {amount} {currency}",
                    first_message=f"درخواست شارژ دستی؛ شناسه پرداخت: {payment_id}؛ مبلغ: {amount} {currency}. پرداخت هنوز تأیید نشده است.")
                details = self.data.get_manual_deposit_details()
                if details and currency == "IRR":
                    instructions = f"\n\n💳 کارت‌به‌کارت\nبانک: {details['bank_name'] or '—'}\nشماره کارت: {details['card_number']}\nبه نام: {details['account_holder']}\nپس از واریز، ثبت رسید: /deposit-receipt {payment_id} | کدپیگیری"
                else:
                    instructions = "\n\nاطلاعات کارت‌به‌کارت برای این ارز تنظیم نشده است؛ پیش از واریز با پشتیبانی هماهنگ کن."
                await self.api.send_text(chat_id, f"درخواست شارژ ثبت شد.\nشناسه پرداخت: {payment_id}\nمبلغ: {amount:,} {currency}\nتیکت: {ticket_id}{instructions}\n\nموجودی فقط پس از بررسی و تأیید دستی مدیر اضافه می‌شود.", menu)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"درخواست شارژ ثبت نشد: {exc}", menu)
        elif command == "/resume-service":
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                await self.api.send_text(chat_id, "فرمت: /resume-service SERVICE_ID", menu)
            else:
                try:
                    worker = CustomerBillingWorker(self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                    await worker.resume(service_id=parts[1].strip(), telegram_id=uid, now=int(datetime.now(timezone.utc).timestamp()))
                    await self.api.send_text(chat_id, "✅ سرویس پس از بررسی موجودی دوباره فعال شد.", menu)
                except Exception as exc:
                    await self.api.send_text(chat_id, f"فعال‌سازی سرویس انجام نشد: {type(exc).__name__}", menu)
        elif command == "/reseller-resume":
            parts = text.split(maxsplit=1)
            owned = {a["account_id"] for a in self.data.list_reseller_accounts(uid)}
            if len(parts) != 2 or parts[1].strip() not in owned:
                await self.api.send_text(chat_id, "فرمت: /reseller-resume ACCOUNT_ID (فقط نمایندگی متعلق به خودت)", menu)
            else:
                try:
                    worker = ResellerBillingWorker(self.data, secret_resolver=self._resolve_panel_secret)
                    await worker.resume(account_id=parts[1].strip(), telegram_id=uid, now=int(datetime.now(timezone.utc).timestamp()))
                    await self.api.send_text(chat_id, "✅ نمایندگی پس از بررسی موجودی دوباره فعال شد.", menu)
                except Exception as exc:
                    await self.api.send_text(chat_id, f"فعال‌سازی نمایندگی انجام نشد: {type(exc).__name__}", menu)
        elif command == "/support":
            self._pending_support.add(uid)
            await self.api.send_text(chat_id, "🎧 پیام مشکل یا درخواستت را در یک پیام بفرست تا به‌صورت تیکت ذخیره شود. برای لغو، /cancel را بفرست.", menu)
        elif command == "/myresellers":
            accounts = self.data.list_reseller_accounts(uid)
            body = "\n".join(f"• {a['username']} — {a['pricing_mode']} — {a['status']} — پنل {a['panel_id']} — بدهی {int(a.get('outstanding_debt_minor') or 0):,} {a['currency']}" for a in accounts)
            buttons = [[{"text": f"مدیریت {a['username'][:20]}", "callback_data": f"customer:rshop:{a['account_id']}"}]
                       for a in accounts if a["status"] == "active"]
            buttons.append([{"text":"⬅️ منو","callback_data":"customer:home"}])
            await self.api.send_text(chat_id, body or "هنوز نمایندگی خریداری نکرده‌ای.", {"inline_keyboard":buttons})
        elif command == "/ticket":
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self.api.send_text(chat_id, "استفاده: /ticket شماره_تیکت", menu)
            else:
                ticket_id = parts[1].strip()
                messages = self.data.ticket_messages(ticket_id, telegram_id=uid)
                if not messages:
                    await self.api.send_text(chat_id, "تیکت پیدا نشد یا متعلق به حساب تو نیست.", menu)
                else:
                    lines = [f"تاریخچه تیکت {ticket_id}:"]
                    lines.extend(f"[{m['sender_type']}] {m['body']}" for m in messages[-15:])
                    await self.api.send_text(chat_id, "\n".join(lines), menu)
        elif command == "/myservices":
            await self._send_customer_services(chat_id, uid)
        elif command == "/ticket-reply":
            payload = text.partition(" ")[2]
            ticket_id, sep, body = payload.partition("|")
            ticket_id, body = ticket_id.strip(), body.strip()
            if not sep or not ticket_id or not body:
                await self.api.send_text(chat_id, "فرمت: /ticket-reply TICKET_ID | متن یا شناسه تراکنش", menu)
            elif self.data.ticket_owner(ticket_id) != uid:
                await self.api.send_text(chat_id, "تیکت پیدا نشد یا متعلق به این حساب نیست.", menu)
            else:
                try:
                    self.data.add_ticket_message(ticket_id=ticket_id, sender_type="customer", body=body)
                    await self.api.send_text(chat_id, "پیام به تیکت اضافه شد.", menu)
                except CustomerStoreError as exc:
                    await self.api.send_text(chat_id, f"پیام ثبت نشد: {exc}", menu)
        elif command == "/mytickets":
            tickets = self.data.list_tickets(uid)
            if not tickets:
                await self.api.send_text(chat_id, "هنوز تیکت پشتیبانی ثبت نکرده‌ای.", menu)
            else:
                lines = ["🎧 تیکت‌های تو:"]
                lines.extend(f"• {t['ticket_id']} — {t['subject']} ({t['status']})" for t in tickets[:20])
                await self.api.send_text(chat_id, "\n".join(lines), menu)
        elif command == "/cancel":
            self._pending_support.discard(uid)
            await self.api.send_text(chat_id, "عملیات لغو شد.", menu)
        else:
            await self.api.send_text(chat_id, "از منوی زیر استفاده کن یا /start را بفرست.", menu)

    async def _admin_wallet_verify(self, chat_id: int, text: str, admin_id: int) -> None:
        # This is a manual verification command, not a gateway/webhook verifier.
        payload = text.partition(" ")[2].strip()
        fields = [part.strip() for part in payload.split("|")]
        try:
            if len(fields) != 4: raise ValueError("فرمت: /wallet-verify PAYMENT_ID | amount_minor | currency | provider_transaction_reference")
            payment_id, amount_raw, currency, reference = fields
            amount = int(amount_raw)
            attempt = self.data.get_payment_attempt(payment_id)
            if not attempt: raise CustomerStoreError("شناسه پرداخت پیدا نشد")
            if attempt["provider"] == "manual":
                receipt = self.data.get_manual_deposit_receipt(payment_id)
                if not receipt or receipt["status"] != "submitted" or receipt["transfer_reference"] != reference:
                    raise CustomerStoreError("No matching customer-submitted receipt; review /pending-deposits first")
            ledger_id = self.data.settle_verified_wallet_deposit(payment_id=payment_id,
                verified_amount_minor=amount, verified_currency=currency,
                external_reference=reference, verified_by=admin_id)
        except Exception as exc:
            await self.api.send_text(chat_id, f"تأیید شارژ انجام نشد: {type(exc).__name__}: {str(exc)[:160]}", self._customer_menu())
            return
        target = int(attempt["telegram_id"])
        try:
            await self.api.send_text(target, f"✅ شارژ کیف پول تأیید شد.\nمبلغ: {amount:,} {currency.upper()}\nشناسه تراکنش: {reference}", self._customer_menu(include_admin=False))
        except Exception:
            LOG.warning("Wallet was credited but customer notification failed")
        await self._emit_trust_event(TrustEvent.WALLET_DEPOSIT,
            {"amount_minor": amount, "currency": currency.upper(), "public_order_id": payment_id[:20]},
            f"wallet-credit:payment:{payment_id}")
        await self.api.send_text(chat_id, f"شارژ تأیید و به‌صورت idempotent ثبت شد؛ کاربر {target}، مبلغ {amount:,} {currency.upper()}، دفترکل #{ledger_id}.", self._customer_menu())

    async def _admin_payment_events(self, chat_id: int, text: str) -> None:
        """Read-only private-admin view of the immutable payment lifecycle audit."""
        payment_id = text.partition(" ")[2].strip()
        if not payment_id or len(payment_id) > 128 or any(ch.isspace() for ch in payment_id):
            await self.api.send_text(chat_id, "فرمت: /payment-events PAYMENT_ID", self._customer_menu())
            return
        events = self.data.list_payment_attempt_events(payment_id)
        if not events:
            await self.api.send_text(chat_id, "برای این شناسه رویداد پرداختی پیدا نشد.", self._customer_menu())
            return
        labels = {"created": "ایجاد درخواست", "verified": "تأیید پرداخت", "failed": "رد/شکست پرداخت"}
        lines = [f"🧾 سابقه تغییرناپذیر پرداخت: {payment_id}"]
        for event in events[:30]:
            line = f"• {labels.get(event['event_type'], event['event_type'])} | {event['amount_minor']:,} {event['currency']} | {event['created_at']}"
            if event.get("actor_telegram_id") is not None:
                line += f" | مدیر: {event['actor_telegram_id']}"
            if event.get("external_reference"):
                line += f" | مرجع: {event['external_reference']}"
            if event.get("failure_code"):
                line += f" | کد خطا: {event['failure_code']}"
            lines.append(line)
        await self.api.send_text(chat_id, "\n".join(lines), self._customer_menu())

    async def _admin_wallet_credit(self, chat_id: int, text: str, admin_id: int) -> None:
        # Manual credit only after the operator has independently verified the receipt/payment.
        payload = text.partition(" ")[2].strip()
        fields = [part.strip() for part in payload.split("|")]
        try:
            if len(fields) != 4: raise ValueError("فرمت: /wallet-credit USER_ID | amount_minor | currency | receipt_reference")
            user_raw, amount_raw, currency, receipt = fields
            target, amount = int(user_raw), int(amount_raw)
            if target <= 0 or amount <= 0: raise ValueError("شناسه کاربر و مبلغ باید مثبت باشند.")
            if len(receipt) < 4 or len(receipt) > 120 or any(ch.isspace() for ch in receipt): raise ValueError("شناسه رسید باید یکتا و بدون فاصله باشد.")
            self.data.post_wallet_entry(telegram_id=target, idempotency_key=f"admin-credit:{receipt}",
                amount_minor=amount, currency=currency, entry_type="admin_credit", reference=receipt,
                note=f"Manually verified by admin {admin_id}")
        except (ValueError, CustomerStoreError) as exc:
            await self.api.send_text(chat_id, f"شارژ ثبت نشد: {exc}", self._customer_menu())
            return
        await self._emit_trust_event(TrustEvent.WALLET_DEPOSIT,
            {"amount_minor": amount, "currency": currency.upper(), "public_order_id": receipt[:20]},
            f"wallet-credit:{receipt}")
        await self.api.send_text(chat_id, f"شارژ دستی ثبت شد: کاربر {target}، مبلغ {amount:,} {currency.upper()}. این فرمان فقط برای پرداختی است که قبلاً مستقل تأیید شده باشد.", self._customer_menu())

    async def _admin_trust_channel_command(self, chat_id: int, text: str) -> None:
        # /trust-channel set CHAT_ID | on/off | privacy_on/privacy_off | event,event
        payload = text.partition(" ")[2].strip()
        if not payload.startswith("set "):
            await self.api.send_text(chat_id, "فرمت کانال اعتماد:\n/trust-channel set -1001234567890 | on | privacy_on | new_subscription,renewal,traffic_topup,time_extension,wallet_deposit,reseller_created,payment_failed\nبرای نمایش وضعیت: /trust-channel-status", self._customer_menu())
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        try:
            if len(fields) != 4: raise ValueError("۴ فیلد لازم است.")
            target, enabled_raw, privacy_raw, events_raw = fields
            if enabled_raw.lower() not in {"on", "off"}: raise ValueError("وضعیت باید on یا off باشد.")
            if privacy_raw.lower() not in {"privacy_on", "privacy_off"}: raise ValueError("حریم خصوصی باید privacy_on یا privacy_off باشد.")
            events = [e.strip() for e in events_raw.split(",") if e.strip()]
            self.data.set_trust_channel_config(bot_instance_id=self.bot_instance_id, chat_id=target,
                enabled=enabled_raw.lower() == "on", enabled_events=events,
                privacy_mode=privacy_raw.lower() == "privacy_on")
        except (ValueError, CustomerStoreError) as exc:
            await self.api.send_text(chat_id, f"تنظیم کانال اعتماد ذخیره نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id, "تنظیمات کانال اعتماد ذخیره شد. ربات باید دسترسی ارسال پیام در کانال را داشته باشد.", self._customer_menu())

    async def _emit_trust_event(self, event: TrustEvent, payload: dict[str, Any], idempotency_key: str) -> None:
        config = self.data.get_trust_channel_config(self.data.tenant_id, self.bot_instance_id)
        if not config or not config.enabled or event not in config.enabled_events:
            return
        try:
            self.trust_outbox.enqueue(config, event, payload, idempotency_key=idempotency_key)
            await self.trust_worker.deliver_batch(limit=10)
        except Exception as exc:
            LOG.warning("Trust notification queued/delivery failed (%s)", type(exc).__name__)

    async def _admin_panel_command(self, chat_id: int, text: str) -> None:
        # /panel add ID | label | provider | https://url | auth_mode | env:VARIABLE | enabled | metadata_json
        payload = text.partition(" ")[2].strip()
        if not payload.startswith("add "):
            await self.api.send_text(chat_id, "فرمت اتصال پنل:\n/panel add ID | نام | pasarguard/rebecca/sanaei | https://panel.example | api_key/username_password/access_token | env:SECRET_NAME | on/off | metadata JSON\nبرای لینک اشتراک واقعی Sanaei، metadata اختیاری باید شامل origin جداگانهٔ HTTPS و subPath پنل باشد؛ مثال: {\"subscription_base_url\":\"https://sub.example:2096\",\"subscription_path\":\"sub\"}. اگر تنظیم نشده باشد، لینک اشتراک فعال نمی‌شود.\nبرای تغییر تنظیمات، همان ID را با /panel add دوباره ثبت کن. برای توقف فروش بدون حذف تنظیمات: /panel-enable PANEL_ID off\nرمز یا API Key را در چت وارد نکن؛ فقط نام متغیر محیطی را ثبت کن.")
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        if len(fields) != 8:
            await self.api.send_text(chat_id, "برای ثبت پنل دقیقاً ۸ فیلد وارد کن.")
            return
        panel_id, label, provider, base_url, auth_mode, secret_ref, enabled_raw, metadata_raw = fields
        try:
            if provider.lower() not in {"pasarguard", "rebecca", "sanaei"}:
                raise CustomerStoreError("فعلاً فقط PasarGuard، Rebecca و Sanaei پشتیبانی می‌شوند.")
            if not secret_ref.startswith("env:"):
                raise CustomerStoreError("مرجع رمز باید env:VARIABLE_NAME باشد؛ خود رمز را وارد نکن.")
            metadata = json.loads(metadata_raw or "{}")
            if not isinstance(metadata, dict): raise ValueError
            enabled = enabled_raw.lower() in {"on", "true", "1", "yes"}
            if enabled_raw.lower() not in {"on", "off", "true", "false", "1", "0", "yes", "no"}:
                raise ValueError
            self.data.register_panel_connection(panel_id=panel_id, label=label, provider=provider,
                base_url=base_url, auth_mode=auth_mode, secret_ref=secret_ref, enabled=enabled, metadata=metadata)
        except (CustomerStoreError, ValueError, json.JSONDecodeError) as exc:
            await self.api.send_text(chat_id, f"پنل ذخیره نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id, f"پنل {panel_id} ذخیره شد. هنوز اتصال زنده تأیید نشده است؛ برای بررسی خواندنی از /panel-check {panel_id} استفاده کن.", self._customer_menu())

    def _queue_panel_check_notification(self, *, chat_id: int, status: str, reason_code: str) -> None:
        try:
            self.data.enqueue_admin_notification(
                event_key="service_test",
                idempotency_key=f"panel-check:{uuid.uuid4().hex}",
                payload={"status": status, "reason_code": reason_code, "customer_id": chat_id},
            )
        except Exception as exc:
            LOG.warning("Panel-check notification enqueue failed (%s)", type(exc).__name__)

    async def _admin_panel_check(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self.api.send_text(chat_id, "فرمت: /panel-check PANEL_ID", self._customer_menu())
            return
        row = self.data.get_panel_connection(parts[1].strip())
        if not row:
            await self.api.send_text(chat_id, "پنل پیدا نشد.", self._customer_menu())
            return
        from manubisguard.panels.contracts import PanelConnection
        connection = PanelConnection(id=row["panel_id"], tenant_id=self.data.tenant_id, name=row["label"],
            kind=row["provider"], base_url=row["base_url"], auth_mode=row["auth_mode"],
            secret_ref=row["secret_ref"], enabled=bool(row["enabled"]), verify_tls=True,
            metadata=row.get("metadata") or {})
        try:
            adapter = self.registry.get(connection.kind)
            health = await adapter.health_check(connection)
            capabilities = await adapter.capabilities(connection)
            capability_data = {name: bool(getattr(capabilities, name)) for name in capabilities.__dataclass_fields__}
            report_method = getattr(adapter, "capability_report", None)
            report = await report_method(connection) if callable(report_method) else None
            check_status = "healthy_read_only" if health.get("ok") else "failed"
            self.data.record_panel_check(panel_id=connection.id, status=check_status, capabilities=capability_data)
            self._queue_panel_check_notification(chat_id=chat_id, status=check_status,
                reason_code="panel_check_succeeded" if health.get("ok") else "panel_check_failed")
            summary = "اتصال خواندنی موفق بود." if health.get("ok") else f"اتصال ناموفق بود ({health.get('error_type', 'error')})."
            lines = [summary,
                f"قابلیت‌های تعریف‌شده در آداپتر: {', '.join(k for k,v in capability_data.items() if v)}"]
            if report and isinstance(report.get("operations"), dict):
                lines.append("وضعیت عملیات (محلی؛ مجوز واقعی پنل جداگانه تأیید نشده):")
                for operation, detail in report["operations"].items():
                    status = detail.get("status", "unknown")
                    if status == "unknown" and detail.get("adapter_method_present"):
                        status_text = "متد محلی موجود؛ مجوز پنل نامشخص"
                    elif status == "sdk_surface_present":
                        status_text = "سطح SDK موجود؛ مجوز پنل نامشخص"
                    elif status == "sdk_surface_missing":
                        status_text = "سطح SDK ناکافی"
                    elif status == "unsupported":
                        status_text = "پشتیبانی نمی‌شود"
                    else:
                        status_text = status
                    lines.append(f"• {operation}: {status_text}")
            lines.append("این بررسی فقط خواندنی است؛ مجوز عملیات تغییردهنده یا سازگاری نسخهٔ نصب‌شده را اثبات نمی‌کند.")
            await self.api.send_text(chat_id, "\n".join(lines), self._customer_menu())
        except Exception as exc:
            self.data.record_panel_check(panel_id=connection.id, status="failed", capabilities={})
            self._queue_panel_check_notification(chat_id=chat_id, status="failed", reason_code="panel_check_error")
            await self.api.send_text(chat_id, f"بررسی پنل ناموفق بود: {type(exc).__name__}", self._customer_menu())

    async def _admin_panel_roles(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self.api.send_text(chat_id, "فرمت: /panel-roles PANEL_ID", self._customer_menu()); return
        row = self.data.get_panel_connection(parts[1].strip())
        if not row or row["provider"] != "pasarguard":
            await self.api.send_text(chat_id, "فهرست نقش‌ها فعلاً فقط برای PasarGuard در دسترس است.", self._customer_menu()); return
        from manubisguard.panels.contracts import PanelConnection
        connection = PanelConnection(id=row["panel_id"], tenant_id=self.data.tenant_id, name=row["label"],
            kind=row["provider"], base_url=row["base_url"], auth_mode=row["auth_mode"],
            secret_ref=row["secret_ref"], enabled=bool(row["enabled"]), verify_tls=True,
            metadata=row.get("metadata") or {})
        try:
            roles = await self.registry.get("pasarguard").list_roles(connection)
            body = "\n".join(f"• ID {role['id']} — {role['name']}{' (Owner)' if role['is_owner'] else ''}" for role in roles)
            await self.api.send_text(chat_id, body or "نقشی برنگشت؛ اتصال/مجوز API را بررسی کن.", self._customer_menu())
        except Exception as exc:
            await self.api.send_text(chat_id, f"دریافت نقش‌ها ناموفق بود: {type(exc).__name__}", self._customer_menu())

    async def _send_customer_services(self, chat_id: int, telegram_id: int, page: int = 0) -> None:
        services = self.data.list_services(telegram_id)
        if not services:
            await self.api.send_text(chat_id, "🔐 هنوز سرویسی در این حساب ثبت نشده است.", BACK_MENU)
            return
        page_size = 1  # Each service can expose up to four actions; keep total buttons <= 8.
        page_count = max(1, (len(services) + page_size - 1) // page_size)
        page = max(0, min(page, page_count - 1))
        selected = services[page * page_size:(page + 1) * page_size]
        status_fa = {"pending":"در انتظار", "active":"فعال", "limited":"محدود", "expired":"منقضی", "suspended":"تعلیق‌شده", "failed":"ناموفق", "deleted":"حذف‌شده"}
        lines = [f"🔐 سرویس‌های ثبت‌شده — صفحه {page + 1} از {page_count}"]
        buttons = []
        for service in selected:
            lines.append(f"• {service['service_id']} — {service['username']} — {status_fa.get(service['status'], service['status'])}")
            if service.get("expires_at"): lines.append(f"  انقضا: {service['expires_at']}")
            if service.get("traffic_limit_bytes"):
                used = int(service["traffic_used_bytes"]) / (1024 ** 3); total = int(service["traffic_limit_bytes"]) / (1024 ** 3)
                lines.append(f"  حجم: {used:.2f}/{total:.2f} GiB")
            if int(service.get("outstanding_debt_minor") or 0):
                lines.append(f"  بدهی معوق: {int(service['outstanding_debt_minor']):,} {service['currency']}")
            buttons.append([{"text": f"🔎 جزئیات {service['service_id'][:12]}",
                             "callback_data": f"customer:service_detail:{service['service_id']}"}])
            if service.get("status") in {"active", "limited", "expired"} and service.get("pricing_mode") not in {"hourly", "usage"}:
                buttons.append([{"text": f"⏳ تمدید {service['service_id'][:12]}", "callback_data": f"customer:renew_confirm:{service['service_id']}"}])
                if int(service.get("traffic_limit_bytes") or 0) > 0:
                    buttons.append([{"text": f"📶 افزایش حجم {service['service_id'][:12]}", "callback_data": f"customer:topup_confirm:{service['service_id']}"}])
            if service.get("billing_status") == "suspended_balance":
                buttons.append([{"text": f"فعال‌سازی {service['service_id'][:12]}", "callback_data": f"customer:resume:{service['service_id']}"}])
        navigation = []
        if page > 0:
            navigation.append({"text": "⬅️ قبلی", "callback_data": f"customer:services:{page - 1}"})
        if page + 1 < page_count:
            navigation.append({"text": "بعدی ➡️", "callback_data": f"customer:services:{page + 1}"})
        if navigation:
            buttons.append(navigation)
        buttons.append([{"text":"🏠 منوی اصلی","callback_data":"customer:home"}])
        await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard":buttons})

    def _validate_product_target(self, panel_id: str, groups: list[str]) -> tuple[dict[str, Any], list[str]]:
        panel = self.data.get_panel_connection(panel_id)
        if not panel or not panel["enabled"]:
            raise CustomerStoreError("محصول باید به یک اتصال پنل موجود و فعال متصل باشد.")
        metadata = panel.get("metadata") or {}
        provider = panel["provider"]
        if provider == "pasarguard":
            if not groups:
                groups = [str(x) for x in (metadata.get("default_group_ids") or metadata.get("group_ids") or [])]
            try: valid = bool(groups) and all(int(x) > 0 for x in groups)
            except (TypeError, ValueError): valid = False
            if not valid: raise CustomerStoreError("برای PasarGuard حداقل یک group ID عددی و مثبت لازم است.")
        elif provider == "rebecca":
            if len(groups) > 1:
                raise CustomerStoreError("Rebecca product accepts exactly one service_id.")
            candidate = groups[0] if groups else metadata.get("service_id")
            try: candidate = int(candidate)
            except (TypeError, ValueError): raise CustomerStoreError("برای Rebecca یک service_id عددی در گروه محصول یا metadata پنل لازم است.") from None
            if candidate <= 0: raise CustomerStoreError("service_id مربوط به Rebecca باید مثبت باشد.")
            groups = [str(candidate)]
        elif provider == "sanaei":
            if not groups: groups = [str(x) for x in (metadata.get("inbound_ids") or [])]
            try: valid = bool(groups) and all(int(x) > 0 for x in groups)
            except (TypeError, ValueError): valid = False
            if not valid: raise CustomerStoreError("برای Sanaei حداقل یک inbound ID عددی و مثبت لازم است.")
            groups = [str(int(x)) for x in groups]
        else:
            raise CustomerStoreError("نوع پنل پشتیبانی نمی‌شود.")
        return panel, groups

    async def _handle_discount_code_message(self, chat_id: int, telegram_id: int, code_text: str) -> None:
        pending = self._pending_purchase_discount.get(telegram_id) or {}
        product_id = str(pending.get("product_id") or "")
        product = self.data.get_product(product_id, enabled_only=True) if product_id else None
        code = code_text.strip().upper()
        discount = self.data.get_discount_code(code)
        now = int(datetime.now(timezone.utc).timestamp())
        reason = None
        if not product:
            reason = "این محصول دیگر فعال نیست؛ از فروشگاه دوباره شروع کن."
        elif not discount or not discount["enabled"]:
            reason = "کد تخفیف معتبر یا فعال نیست."
        elif discount["expires_at"] is not None and int(discount["expires_at"]) <= now:
            reason = "اعتبار این کد تخفیف تمام شده است."
        elif int(discount["max_uses"]) > 0 and int(discount["used_count"]) >= int(discount["max_uses"]):
            reason = "ظرفیت استفاده از این کد تمام شده است."
        elif discount["allowed_product_ids"] and product_id not in discount["allowed_product_ids"]:
            reason = "این کد برای محصول انتخاب‌شده قابل استفاده نیست."
        elif int(product["price_minor"]) <= 0:
            reason = "کد تخفیف فقط برای محصول دارای هزینه اولیه مثبت قابل استفاده است."
        if reason:
            await self.api.send_text(chat_id, f"{reason}\nکد دیگری بفرست یا /cancel را برای لغو بزن.")
            return
        self._pending_purchase_discount[telegram_id] = {"product_id": product_id, "discount_code": code}
        original = int(product["price_minor"])
        final = original - (original * int(discount["percentage"]) // 100)
        await self.api.send_text(chat_id,
            f"🎟 پیش‌نمایش کد تخفیف\nمحصول: {product['title']}\nقیمت فعلی: {original:,} {product['currency']}\n"
            f"تخفیف: {int(discount['percentage'])}٪\nمبلغ نهایی: {final:,} {product['currency']}\n"
            "قیمت و سهمیه هنگام تأیید دوباره در پایگاه داده بررسی می‌شود.",
            {"inline_keyboard":[
                [{"text":"تأیید خرید با تخفیف","callback_data":f"customer:buy_discount_confirm:{product_id}"}],
                [{"text":"لغو","callback_data":"customer:discount_cancel"}]
            ]})

    async def _admin_discount_command(self, chat_id: int, text: str) -> None:
        payload = text.partition(" ")[2].strip()
        if payload == "list":
            codes = self.data.list_discount_codes()
            lines = ["🎟 کدهای تخفیف tenant جاری:"]
            for item in codes[:50]:
                limit = "نامحدود" if int(item["max_uses"]) == 0 else f"{item['used_count']}/{item['max_uses']}"
                expiry = "بدون انقضا" if item["expires_at"] is None else f"انقضا epoch={item['expires_at']}"
                products = ",".join(item["allowed_product_ids"]) or "همه محصولات"
                lines.append(f"• {item['code']} — {item['percentage']}٪ — استفاده {limit} — {expiry} — محصولات: {products} — {'فعال' if item['enabled'] else 'خاموش'}")
            await self.api.send_text(chat_id, "\n".join(lines) if codes else "کدی ثبت نشده است.")
            return
        if payload.startswith("enable "):
            parts = payload.split()
            if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
                await self.api.send_text(chat_id, "فرمت: /discount enable CODE on|off", self._customer_menu())
                return
            try:
                self.data.set_discount_code_enabled(parts[1], parts[2].lower() == "on")
                await self.api.send_text(chat_id, "وضعیت کد تخفیف ذخیره شد.", self._customer_menu())
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"وضعیت کد تغییر نکرد: {exc}", self._customer_menu())
            return
        if not payload.startswith("add "):
            await self.api.send_text(chat_id,
                "فرمت کد تخفیف:\n/discount add CODE | PERCENT | MAX_USES(0=unlimited) | EXPIRES_EPOCH(0=never) | PRODUCT_IDS_CSV(empty=all)\n"
                "/discount list\n/discount enable CODE on|off\n"
                "مثال: /discount add SAVE20 | 20 | 100 | 0 | basic,pro\n"
                "انقضا Unix timestamp است؛ کد به‌صورت tenant-local ذخیره می‌شود.", self._customer_menu())
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        try:
            if len(fields) != 5:
                raise ValueError("برای افزودن کد دقیقاً ۵ فیلد لازم است.")
            code, percentage_raw, max_uses_raw, expires_raw, products_raw = fields
            percentage, max_uses, expires = int(percentage_raw), int(max_uses_raw), int(expires_raw)
            product_ids = [part.strip() for part in products_raw.split(",") if part.strip()]
            self.data.put_discount_code(code=code, percentage=percentage, max_uses=max_uses,
                expires_at=expires or None, allowed_product_ids=product_ids, enabled=True)
        except (ValueError, CustomerStoreError) as exc:
            await self.api.send_text(chat_id, f"کد تخفیف ذخیره نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id,
            f"کد {code.strip().upper()} با تخفیف {percentage}٪ ذخیره شد. ورود کد در جریان خرید فعال است؛ هنگام تأیید، قیمت و سهمیه دوباره بررسی می‌شوند.",
            self._customer_menu())

    async def _admin_category_command(self, chat_id: int, text: str) -> None:
        payload = text.partition(" ")[2].strip()
        if payload.startswith("enable "):
            parts = payload.split()
            if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
                await self.api.send_text(chat_id, "فرمت: /category enable ID on|off", self._customer_menu())
                return
            try:
                self.data.set_category_enabled(parts[1], parts[2].lower() == "on")
                await self.api.send_text(chat_id, "وضعیت دسته‌بندی ذخیره شد؛ محصولات آن فقط در حالت فعال نمایش داده می‌شوند.", self._customer_menu())
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"وضعیت دسته‌بندی تغییر نکرد: {exc}", self._customer_menu())
            return
        if not payload.startswith("add "):
            await self.api.send_text(chat_id, "فرمت دسته‌بندی:\n/category add ID | عنوان | توضیح اختیاری\n/category enable ID on|off\n/categories", self._customer_menu())
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        if len(fields) not in {2, 3}:
            await self.api.send_text(chat_id, "فرمت: /category add ID | عنوان | توضیح اختیاری", self._customer_menu())
            return
        try:
            self.data.put_category(category_id=fields[0], title=fields[1],
                                   description=fields[2] if len(fields) == 3 else "")
        except CustomerStoreError as exc:
            await self.api.send_text(chat_id, f"دسته‌بندی ذخیره نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id, f"دسته‌بندی «{fields[1]}» ذخیره شد.", self._customer_menu())

    async def _admin_product_command(self, chat_id: int, text: str) -> None:
        # /product add ID | title | mode | setup_price | unit_price | currency | duration_hours | traffic_GiB | panel_id | groups_csv
        payload = text.partition(" ")[2].strip()
        if payload.startswith("enable "):
            parts = payload.split()
            if len(parts) != 3 or parts[2].lower() not in {"on", "off"}:
                await self.api.send_text(chat_id, "فرمت: /product enable PRODUCT_ID on|off", self._customer_menu()); return
            try:
                self.data.set_product_enabled(parts[1], parts[2].lower() == "on")
                await self.api.send_text(chat_id, "وضعیت محصول ذخیره شد.", self._customer_menu())
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"وضعیت محصول تغییر نکرد: {exc}", self._customer_menu())
            return
        if payload.startswith("bind "):
            fields = [part.strip() for part in payload[5:].split("|")]
            try:
                if len(fields) != 3: raise ValueError("فرمت: /product bind PRODUCT_ID | PANEL_ID | group/service/inbound IDs separated by comma")
                product_id, panel_id, groups_raw = fields
                panel, groups = self._validate_product_target(panel_id, [x.strip() for x in groups_raw.split(",") if x.strip()])
                self.data.bind_product_panel(product_id=product_id, panel_id=panel_id, group_ids=groups)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"اتصال محصول تغییر نکرد: {exc}", self._customer_menu()); return
            await self.api.send_text(chat_id, f"محصول {product_id} به پنل {panel_id} ({panel['provider']}) متصل شد.", self._customer_menu())
            return
        if not payload.startswith("add "):
            await self.api.send_text(chat_id, "فرمت محصول:\n/product add ID | عنوان | fixed/unlimited/hourly/usage | هزینه_اولیه | نرخ_واحد | ارز | ساعت | حجم_GiB | panel_id | group/service/inbound IDs\n/product bind ID | panel_id | IDs\n/product enable ID on|off\nفرم ۶ فیلدی قدیمی فقط وقتی پذیرفته می‌شود که دقیقاً یک پنل فعال با شناسه‌های پیش‌فرض معتبر وجود داشته باشد.", self._customer_menu())
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        try:
            category_id = ""
            if len(fields) == 6:
                product_id, title, price_raw, currency, hours_raw, gib_raw = fields
                active_panels = [p for p in self.data.list_panel_connections() if p["enabled"]]
                if len(active_panels) != 1:
                    raise ValueError("برای فرم قدیمی باید دقیقاً یک پنل فعال باشد؛ در غیر این صورت از فرم کامل استفاده کن.")
                panel_id = str(active_panels[0]["panel_id"])
                _, groups = self._validate_product_target(panel_id, [])
                mode, unit_price = "fixed", 0
            elif len(fields) in {10, 11}:
                product_id, title, mode, price_raw, unit_raw, currency, hours_raw, gib_raw, panel_id, groups_raw = fields[:10]
                category_id = fields[10] if len(fields) == 11 else ""
                unit_price = int(unit_raw)
                _, groups = self._validate_product_target(panel_id, [g.strip() for g in groups_raw.split(",") if g.strip()])
            else:
                raise ValueError("فرمت باید ۶ فیلد قدیمی، ۱۰ فیلد کامل یا ۱۱ فیلد کامل همراه دسته‌بندی باشد.")
            price = int(price_raw); hours = int(hours_raw); gib = Decimal(gib_raw)
            if not gib.is_finite() or gib < 0 or gib > Decimal("1000000"):
                raise ValueError("حجم نامعتبر است.")
            traffic_bytes = int(gib * Decimal(1024 ** 3))
            self.data.put_product(product_id=product_id, title=title, price_minor=price,
                currency=currency, duration_hours=hours, traffic_bytes=traffic_bytes,
                pricing_mode=mode, unit_price_minor=unit_price, panel_id=panel_id,
                group_ids=groups, category_id=category_id)
        except (ValueError, CustomerStoreError, InvalidOperation) as exc:
            await self.api.send_text(chat_id, f"محصول ثبت نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id, f"محصول «{title}» با مدل {mode} ذخیره شد و به پنل {panel_id} متصل است.", self._customer_menu())

    async def _admin_reseller_plan_command(self, chat_id: int, text: str) -> None:
        # Basic 12-field form remains supported; extended form also stores volume bounds, add-ons, display and permission overrides.
        payload = text.partition(" ")[2].strip()
        if not payload.startswith("add "):
            await self.api.send_text(chat_id, "فرمت پایه:\n/reseller-plan add ID | panel_id | عنوان | fixed/unlimited/hourly/usage | هزینه_اولیه | نرخ_واحد | ارز | ساعت | حجم_GiB | max_users | role_id | groups_csv\nفرمت پیشرفته ۲۱ فیلدی، سه حد حجم، قیمت افزودنی ساعت/GiB/کاربر، عنوان نمایشی، آیکون و permission_overrides_json را هم می‌پذیرد. برای PasarGuard ابتدا /panel-roles PANEL_ID را اجرا کن.", self._customer_menu())
            return
        fields = [part.strip() for part in payload[4:].split("|")]
        try:
            if len(fields) not in {12, 21}:
                raise ValueError("فرمت پلن باید دقیقاً ۱۲ یا ۲۱ فیلد داشته باشد.")
            plan_id, panel_id, title, mode, setup_raw, unit_raw, currency, hours_raw, gib_raw, users_raw, role_id, groups_raw = fields[:12]
            if mode not in {"fixed", "unlimited", "hourly", "usage"}:
                raise ValueError("نوع پلن باید fixed، unlimited، hourly یا usage باشد")
            setup, unit, hours, max_users = int(setup_raw), int(unit_raw), int(hours_raw), int(users_raw)
            gib = Decimal(gib_raw)
            if not gib.is_finite() or gib < 0 or gib > Decimal("1000000"):
                raise ValueError("حجم نامعتبر است.")
            data_limit = int(gib * Decimal(1024 ** 3))
            groups = [x.strip() for x in groups_raw.split(",") if x.strip()]
            options = {"min_volume_gib": "0", "max_volume_gib": "0", "volume_step_gib": "1",
                "addon_hour_price_minor": 0, "addon_gib_price_minor": 0, "addon_user_price_minor": 0,
                "display_label": "", "display_icon": "", "permission_overrides": {}}
            if len(fields) == 21:
                min_volume, max_volume, step, add_hour, add_gib, add_user, display_label, display_icon, overrides_raw = fields[12:21]
                for value in (min_volume, max_volume, step):
                    number = Decimal(value)
                    if not number.is_finite() or number < 0: raise ValueError("حد حجم نامعتبر است.")
                options.update({"min_volume_gib": min_volume, "max_volume_gib": max_volume, "volume_step_gib": step,
                    "addon_hour_price_minor": int(add_hour), "addon_gib_price_minor": int(add_gib),
                    "addon_user_price_minor": int(add_user), "display_label": display_label,
                    "display_icon": display_icon, "permission_overrides": json.loads(overrides_raw or "{}")})
                if not isinstance(options["permission_overrides"], dict): raise ValueError("permission overrides باید JSON object باشد.")
            self.data.put_reseller_plan(plan_id=plan_id, panel_id=panel_id, title=title,
                pricing_mode=mode, price_minor=setup, unit_price_minor=unit, currency=currency,
                duration_hours=hours, data_limit_bytes=data_limit, max_users=max_users,
                provider_role_id=role_id, allowed_group_ids=groups, **options)
        except (ValueError, CustomerStoreError, InvalidOperation, json.JSONDecodeError) as exc:
            await self.api.send_text(chat_id, f"پلن نمایندگی ذخیره نشد: {exc}", self._customer_menu())
            return
        await self.api.send_text(chat_id, f"پلن نمایندگی «{title}» ذخیره شد. PasarGuard در مسیر native با role ID است؛ Rebecca فعلاً مسیر مدیریت‌شده داخل ربات دارد. Sanaei برای نمایندگی پشتیبانی نمی‌شود.", self._customer_menu())

    async def _admin_ticket_reply(self, chat_id: int, text: str) -> None:
        # Format: /reply TICKET_ID | message
        payload = text.partition(" ")[2]
        ticket_id, sep, body = payload.partition("|")
        ticket_id, body = ticket_id.strip(), body.strip()
        if not sep or not ticket_id or not body:
            await self.api.send_text(chat_id, "فرمت پاسخ: /reply شماره_تیکت | متن پاسخ")
            return
        owner = self.data.ticket_owner(ticket_id)
        if owner is None:
            await self.api.send_text(chat_id, "تیکت پیدا نشد.")
            return
        try:
            self.data.add_ticket_message(ticket_id=ticket_id, sender_type="staff", body=body)
            await self.api.send_text(owner, f"🎧 پاسخ پشتیبانی برای تیکت {ticket_id}:\n\n{body}")
        except Exception:
            LOG.exception("Could not send support response")
            await self.api.send_text(chat_id, "ذخیره یا ارسال پاسخ با خطا روبه‌رو شد؛ تیکت را بررسی کن.")
            return
        await self.api.send_text(chat_id, "✅ پاسخ ذخیره و به کاربر ارسال شد.")

    async def _show_shop(self, chat_id: int, keyboard: dict[str, Any], category_id: str | None = None) -> None:
        categories = self.data.list_categories()
        category = next((c for c in categories if c["category_id"] == category_id), None) if category_id else None
        if category_id and not category:
            await self.api.send_text(chat_id, "این دسته‌بندی دیگر فعال نیست.", keyboard)
            return
        products = [p for p in self.data.list_products(category_id=category_id)
                    if (self.data.get_panel_connection(str(p.get("panel_id") or "")) or {}).get("enabled")]
        manual_products = self.data.list_manual_products(category_id=category_id)
        lines = [f"🛍 دسته‌بندی: {category['title']}" if category else "🛍 فروشگاه محصولات و اشتراک‌ها:"]
        buttons = []
        for item in manual_products[:30]:
            cap = int(item.get("max_purchases_per_customer") or 0)
            cap_text = f" — سقف خرید هر مشتری: {cap}" if cap else ""
            variants = self.data.list_manual_variants(item["product_id"])
            variant_modes = []
            variant_inventory_total = 0
            for variant_item in variants:
                variant_delivery = self.data.manual_variant_delivery(item["product_id"], variant_item["variant_id"])
                variant_mode = variant_delivery["delivery_mode"]
                variant_modes.append(variant_mode)
                if variant_mode == "inventory":
                    variant_inventory_total += self.data.manual_inventory_count(item["product_id"], variant_item["variant_id"])
            if variants:
                is_out_of_stock = bool(variant_modes) and all(
                    mode == "inventory" and self.data.manual_inventory_count(item["product_id"], v["variant_id"]) == 0
                    for v, mode in zip(variants, variant_modes)
                )
                stock_count = variant_inventory_total if "inventory" in variant_modes else None
                delivery_text = ({"instant":"تحویل خودکار", "manual":"تحویل توسط پشتیبانی", "inventory":"تحویل کلید یکتا از موجودی", "file":"ارسال فایل پس از خرید"}[variant_modes[0]]
                                 if len(set(variant_modes)) == 1 else "روش تحویل بر اساس نوع انتخابی")
                stock_text = " — ناموجود" if is_out_of_stock else (f" — موجودی انواع انباردار: {stock_count}" if stock_count is not None else "")
            else:
                is_out_of_stock = item["delivery_mode"] == "inventory" and self.data.manual_inventory_count(item["product_id"]) == 0
                stock_count = self.data.manual_inventory_count(item["product_id"]) if item["delivery_mode"] == "inventory" else None
                stock_text = " — ناموجود" if is_out_of_stock else (" — موجود" if item["delivery_mode"] == "inventory" else "")
                delivery_text = ("تحویل خودکار" if item["delivery_mode"] == "instant" else
                    "تحویل کلید یکتا از موجودی" if item["delivery_mode"] == "inventory" else
                    "ارسال فایل پس از خرید" if item["delivery_mode"] == "file" else "تحویل توسط پشتیبانی")
            description = str(item.get("description") or "").strip()
            summary = description if len(description) <= 180 else description[:177].rstrip() + "..."
            image_url = str(item.get("image_url") or "").strip()
            photo_sent = False
            if image_url:
                caption_parts = [f"🛍 {item['title']}", f"قیمت: {int(item['price_minor']):,} {item['currency']}",
                                 f"تحویل: {delivery_text}{stock_text}{cap_text}"]
                if summary:
                    caption_parts.append(summary)
                photo_payload: dict[str, Any] = {"chat_id": chat_id, "photo": image_url,
                    "caption": "\n".join(caption_parts)[:1000], "protect_content": True}
                if not is_out_of_stock:
                    photo_payload["reply_markup"] = {"inline_keyboard": [[{"text": f"خرید {item['title'][:28]}",
                        "callback_data": f"customer:manualbuy:{item['product_id']}"}]]}
                try:
                    await self.api.call("sendPhoto", photo_payload)
                    photo_sent = True
                except Exception as exc:
                    LOG.warning("Manual product image send failed for product %s (%s)",
                                item["product_id"], type(exc).__name__)
            if not photo_sent:
                lines.append(f"• {item['title']} — {int(item['price_minor']):,} {item['currency']} — {delivery_text}{stock_text}{cap_text}")
                if summary:
                    lines.append(f"  توضیحات: {summary}")
                if not is_out_of_stock:
                    buttons.append([{"text": f"خرید {item['title'][:28]}", "callback_data": f"customer:manualbuy:{item['product_id']}"}])
        if categories:
            category_buttons = [[{"text": "همه محصولات", "callback_data": "customer:category:all"}]]
            for item in categories[:24]:
                category_buttons.append([{"text": item["title"][:30], "callback_data": f"customer:category:{item['category_id']}"}])
            buttons.extend(category_buttons)
        if not products:
            if not manual_products and not categories and category_id is None:
                await self.api.send_text(chat_id, "🛍 فروشگاه فعلاً محصولی ندارد. مدیر می‌تواند محصول پنلی یا محصول دیجیتال دستی تعریف کند.", keyboard)
                return
            if category_id is not None and not manual_products:
                lines.append("در این دسته فعلاً محصول فعالی وجود ندارد.")
            elif category_id is None and not manual_products:
                lines.append("محصول پنلی فعالی با اتصال معتبر وجود ندارد.")
            buttons.append(keyboard.get("inline_keyboard", [[{"text":"⬅️ منو","callback_data":"customer:home"}]])[0])
            await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard": buttons})
            return
        mode_names = {"fixed":"ثابت", "unlimited":"نامحدود", "hourly":"ساعتی", "usage":"مصرفی"}
        for item in products[:30]:
            mode = item["pricing_mode"]
            if mode in {"hourly", "usage"}:
                rate_unit = "/ساعت" if mode == "hourly" else "/GiB"
                price_text = f"{int(item['unit_price_minor']):,} {item['currency']}{rate_unit}"
            else:
                price_text = f"{int(item['price_minor']):,} {item['currency']}"
            duration = f" · {item['duration_hours']} ساعت" if item["duration_hours"] else ""
            lines.append(f"• {item['title']} — {mode_names.get(mode,mode)} — {price_text}{duration}")
            buttons.append([{"text": f"خرید {item['title'][:28]} · {mode_names.get(mode,mode)}", "callback_data": f"customer:buy:{item['product_id']}"}])
        lines.append("\nمدل ساعتی/مصرفی از کیف پول به‌صورت دوره‌ای کسر می‌شود؛ در صورت اتمام موجودی، سرویس برای تعلیق به صف می‌رود.")
        buttons.append(keyboard.get("inline_keyboard", [[{"text":"⬅️ منو","callback_data":"customer:home"}]])[0])
        await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard": buttons})

    async def _refresh_customer_service_snapshot(self, *, service: dict[str, Any], telegram_id: int) -> bool:
        """Fetch a read-only provider snapshot; never mutate provider or billing state."""
        from manubisguard.panels.contracts import PanelConnection
        panel_id = str(service.get("panel_id") or "")
        username = str(service.get("username") or "")
        provider_user_id = str(service.get("provider_user_id") or "")
        if not panel_id or not username or not provider_user_id:
            return False
        row = self.data.get_panel_connection(panel_id)
        if not row or not row.get("enabled"):
            return False
        connection = PanelConnection(
            id=row["panel_id"], tenant_id=self.data.tenant_id, name=row["label"],
            kind=row["provider"], base_url=row["base_url"], auth_mode=row["auth_mode"],
            secret_ref=row["secret_ref"], enabled=bool(row["enabled"]), verify_tls=True,
            metadata=row.get("metadata") or {},
        )
        try:
            adapter = self.registry.get(connection.kind)
            capabilities = await adapter.capabilities(connection)
            if not capabilities.read_status:
                return False
            live_user = await adapter.get_user(connection, username)
            if not live_user.username or live_user.username.strip().casefold() != username.strip().casefold():
                return False
            if connection.kind == "pasarguard" and str(live_user.provider_user_id or "") != provider_user_id:
                return False
            fields: dict[str, Any] = {}
            status_map = {
                "active": "active", "enabled": "active", "on": "active", "true": "active", "1": "active",
                "limited": "limited", "expired": "expired", "disabled": "suspended", "disable": "suspended",
                "inactive": "suspended", "off": "suspended", "false": "suspended", "0": "suspended",
                "suspended": "suspended", "deleted": "deleted", "pending": "pending", "failed": "failed",
            }
            raw_status = str(live_user.status or live_user.raw_status or "").strip().lower()
            normalized_status = status_map.get(raw_status)
            if normalized_status:
                fields["status"] = normalized_status
            if live_user.expire_at is not None:
                expire_at = live_user.expire_at
                if expire_at.tzinfo is None:
                    expire_at = expire_at.replace(tzinfo=timezone.utc)
                fields["expires_at"] = expire_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            # Provider mappers may use zero as a fallback for a missing field. Keep
            # existing counters in that ambiguous case; prefer the dedicated usage
            # endpoint and accept only explicit byte counters.
            if isinstance(live_user.data_limit_bytes, int) and not isinstance(live_user.data_limit_bytes, bool) and live_user.data_limit_bytes > 0:
                fields["traffic_limit_bytes"] = live_user.data_limit_bytes
            if isinstance(live_user.used_bytes, int) and not isinstance(live_user.used_bytes, bool) and live_user.used_bytes > 0:
                fields["traffic_used_bytes"] = live_user.used_bytes
            get_usage = getattr(adapter, "get_usage", None)
            if capabilities.read_usage and callable(get_usage):
                identifier = provider_user_id if connection.kind == "pasarguard" else username
                try:
                    usage = await get_usage(connection, identifier)
                    if isinstance(usage, dict):
                        nested = next((usage[k] for k in ("usage", "data", "result", "obj") if isinstance(usage.get(k), dict)), {})
                        sources = [usage, nested]
                        used = next((source[k] for source in sources for k in
                            ("used_bytes", "used_traffic_bytes") if k in source), None)
                        limit = next((source[k] for source in sources for k
                            in ("data_limit_bytes", "total_bytes") if k in source), None)
                        if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                            fields["traffic_used_bytes"] = used
                        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
                            fields["traffic_limit_bytes"] = limit
                except Exception:
                    # A failed usage query must not erase previously stored counters.
                    pass
            if live_user.subscription_url:
                fields["subscription_url"] = live_user.subscription_url
            if not fields:
                return False
            return self.data.update_customer_service_snapshot(
                service_id=str(service["service_id"]), telegram_id=telegram_id, panel_id=panel_id,
                provider_user_id=provider_user_id, fields=fields,
            )
        except Exception as exc:
            LOG.warning("Customer service refresh failed (%s)", type(exc).__name__)
            return False

    def _queue_unauthorized_admin_callback_alert(self, uid: int) -> None:
        # One callback alert per user/minute prevents forged-button spam from flooding admins.
        alert_window = int(time.time() // 60)
        try:
            self.data.enqueue_admin_notification(
                event_key="security_alert",
                idempotency_key=f"unauthorized-admin-callback:{uid}:{alert_window}",
                payload={"status": "blocked", "reason_code": "unauthorized_admin_callback", "customer_id": uid},
            )
        except Exception as exc:
            LOG.warning("Security alert enqueue failed (%s)", type(exc).__name__)

    async def _send_manual_file_delivery(self, chat_id: int, order: dict[str, Any]) -> None:
        order_id = str(order.get("order_id") or "")
        caption = (f"✅ سفارش {order_id[:20]}\nمحصول: {str(order.get('title') or '')[:180]}\n"
                   f"مبلغ: {int(order.get('amount_minor') or 0):,} {order.get('currency') or ''}")[:1024]
        keyboard = {"inline_keyboard": [[{"text": "📥 دریافت دوباره فایل", "callback_data": f"customer:file_retry:{order_id}"}],
                                         [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}]]}
        try:
            await self.api.call("sendDocument", {"chat_id": chat_id, "document": str(order["delivery_payload"]),
                "caption": caption, "protect_content": True, "reply_markup": keyboard})
        except Exception as exc:
            LOG.warning("Manual file delivery failed for order %s (%s)", order_id[:20], type(exc).__name__)
            await self.api.send_text(chat_id,
                f"خرید ثبت شده و مبلغ دوباره کسر نمی‌شود؛ ارسال فایل ناموفق بود. با دکمه زیر دوباره تلاش کن.\nشناسه سفارش: {order_id}", keyboard)

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        callback_id = query.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        uid = user.get("id")
        chat_id_value = chat.get("id")
        is_private_owner = (isinstance(uid, int) and chat.get("type") == "private"
                            and chat_id_value == uid)
        if not is_private_owner:
            if callback_id:
                await self.api.answer_callback(callback_id, "برای ادامه از گفت‌وگوی خصوصی استفاده کن")
            return
        is_admin = uid in self.config.admin_ids
        data = query.get("data", "")
        if data.startswith("content:") and not is_admin:
            self._queue_unauthorized_admin_callback_alert(uid)
            if callback_id:
                await self.api.answer_callback(callback_id, "این بخش فقط برای مدیر است")
            return
        if callback_id:
            await self.api.answer_callback(callback_id)
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        # Visibility settings must be enforced by the dispatcher too, not only
        # while rendering the current menu. Old or forged buttons can outlive it.
        visibility_routes = (
            ("customer:miniapp", "menu_miniapp_enabled"),
            ("customer:shop", "menu_shop_enabled"),
            ("customer:manualbuy", "menu_shop_enabled"),
            ("customer:manualbuy_confirm", "menu_shop_enabled"),
            ("customer:mv", "menu_shop_enabled"),
            ("customer:mp", "menu_shop_enabled"),
            ("customer:file_retry", "menu_shop_enabled"),
            ("customer:category", "menu_shop_enabled"),
            ("customer:buy", "menu_shop_enabled"),
            ("customer:buy_confirm", "menu_shop_enabled"),
            ("customer:buy_discount", "menu_shop_enabled"),
            ("customer:buy_discount_confirm", "menu_shop_enabled"),
            ("customer:discount_cancel", "menu_shop_enabled"),
            ("customer:services", "menu_services_enabled"),
            ("customer:service_detail", "menu_services_enabled"),
            ("customer:service_refresh", "menu_services_enabled"),
            ("customer:service_qr", "menu_services_enabled"),
            ("customer:renew_confirm", "menu_services_enabled"),
            ("customer:renew_execute", "menu_services_enabled"),
            ("customer:topup_confirm", "menu_services_enabled"),
            ("customer:topup_execute", "menu_services_enabled"),
            ("customer:resume", "menu_services_enabled"),
            ("customer:profile", "menu_profile_enabled"),
            ("customer:orders", "menu_orders_enabled"),
            ("customer:resellers", "menu_resellers_enabled"),
            ("customer:reseller_buy", "menu_resellers_enabled"),
            ("customer:reseller_buy_confirm", "menu_resellers_enabled"),
            ("customer:myresellers", "menu_myresellers_enabled"),
            ("customer:rshop", "menu_myresellers_enabled"),
            ("customer:r", "menu_myresellers_enabled"),
            ("customer:c", "menu_myresellers_enabled"),
            ("customer:wallet", "menu_wallet_enabled"),
            ("customer:support", "menu_support_enabled"),
        )
        disabled_setting = next((setting for route, setting in visibility_routes
                                 if data == route or data.startswith(route + ":")), None)
        if disabled_setting and not _enabled(self.content, disabled_setting):
            await self.api.send_text(chat_id, "این بخش در حال حاضر غیرفعال است.", BACK_MENU)
            return
        if data == "customer:miniapp":
            await self.api.send_text(chat_id,
                "مینی‌اپ مشتری هنوز به دامنهٔ HTTPS متصل نشده است. پس از راه‌اندازی بک‌اند و رابط مشتری، مقدار امن MANUBIS_CUSTOMER_WEBAPP_URL را تنظیم کن؛ درگاه پرداخت در این مرحله لازم نیست.",
                BACK_MENU)
            return
        if data.startswith(("customer:service_detail:", "customer:service_refresh:", "customer:service_qr:")):
            if not isinstance(uid, int):
                return
            is_refresh = data.startswith("customer:service_refresh:")
            is_qr = data.startswith("customer:service_qr:")
            prefix = ("customer:service_refresh:" if is_refresh else
                      "customer:service_qr:" if is_qr else "customer:service_detail:")
            service_id = data.partition(prefix)[2]
            service = self.data.get_customer_service(service_id, telegram_id=uid)
            refresh_ok = False
            if service and is_refresh:
                refresh_ok = await self._refresh_customer_service_snapshot(service=service, telegram_id=uid)
                service = self.data.get_customer_service(service_id, telegram_id=uid)
            if service and is_qr:
                try:
                    qr_png = make_subscription_qr_png(service.get("subscription_url"))
                    safe_service_id = re.sub(r"[^A-Za-z0-9_-]", "_", service_id)[:48] or "service"
                    await self.api.send_photo_bytes(
                        chat_id, f"subscription-{safe_service_id}.png", qr_png,
                        caption="🔐 QR خصوصی اشتراک شما\nفقط در اختیار خودتان نگه دارید.",
                    )
                except SubscriptionQRError:
                    await self.api.send_text(chat_id, "لینک اشتراک معتبر نیست یا تولید QR در این محیط آماده نیست؛ لینک ذخیره‌شده تغییر نکرده است.", BACK_MENU)
                    return
                except TelegramAPIError as exc:
                    LOG.warning("Could not send subscription QR (%s)", type(exc).__name__)
                    await self.api.send_text(chat_id, "ارسال QR ناموفق بود؛ لطفاً کمی بعد دوباره تلاش کنید.", BACK_MENU)
                    return
            if not service:
                await self.api.send_text(chat_id, "این سرویس وجود ندارد یا متعلق به حساب شما نیست.", BACK_MENU)
                return
            status_fa = {"pending": "در انتظار", "active": "فعال", "limited": "محدود",
                         "expired": "منقضی", "suspended": "تعلیق‌شده",
                         "failed": "ناموفق", "deleted": "حذف‌شده"}
            lines = [
                "🔐 جزئیات سرویس",
                ("✅ اطلاعات از پنل تازه شد." if refresh_ok else "⚠️ به‌روزرسانی کامل نشد؛ اطلاعات ذخیره‌شده حفظ شده است." if is_refresh else ""),
                f"شناسه: {service['service_id']}",
                f"نام کاربری: {service.get('username') or '—'}",
                f"وضعیت: {status_fa.get(service.get('status'), service.get('status') or 'نامشخص')}",
                f"مدل صورتحساب: {service.get('pricing_mode') or '—'}",
            ]
            if service.get("expires_at"):
                lines.append(f"انقضا: {service['expires_at']}")
            limit_bytes = int(service.get("traffic_limit_bytes") or 0)
            used_bytes = int(service.get("traffic_used_bytes") or 0)
            if limit_bytes > 0:
                remaining_bytes = max(0, limit_bytes - used_bytes)
                lines.extend([
                    f"مصرف: {used_bytes / (1024 ** 3):.2f} GiB",
                    f"سقف حجم: {limit_bytes / (1024 ** 3):.2f} GiB",
                    f"حجم باقی‌مانده محاسباتی: {remaining_bytes / (1024 ** 3):.2f} GiB",
                ])
            if service.get("provider_checked_at"):
                lines.append(f"آخرین بررسی ثبت‌شده پنل: {service['provider_checked_at']}")
            lines.append(_provider_freshness_text(service.get("provider_checked_at")))
            if int(service.get("outstanding_debt_minor") or 0):
                lines.append(f"بدهی معوق: {int(service['outstanding_debt_minor']):,} {service.get('currency') or ''}")
            if service.get("subscription_url"):
                lines.extend(["", "لینک خصوصی اشتراک:", str(service["subscription_url"])])
            else:
                lines.extend(["", "لینک اشتراک در اطلاعات محلی ثبت نشده است."])
            if refresh_ok:
                lines.append("\nاین اطلاعات در همین درخواست از پنل خوانده شد؛ در صورت ناقص بودن پاسخ پنل، بعضی مقادیر قبلی حفظ می‌شوند.")
            else:
                lines.append("\nاطلاعات مصرف/انقضا ممکن است تا بررسی بعدی پنل به‌روز نشده باشد.")
            detail_keyboard = {"inline_keyboard": [
                [{"text": "🔄 به‌روزرسانی از پنل", "callback_data": f"customer:service_refresh:{service['service_id']}"}],
                [{"text": "⬅️ بازگشت به سرویس‌ها", "callback_data": "customer:services"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]}
            if service.get("subscription_url"):
                try:
                    validate_subscription_url(service["subscription_url"])
                    detail_keyboard["inline_keyboard"].insert(
                        1, [{"text": "▦ دریافت QR اشتراک", "callback_data": f"customer:service_qr:{service['service_id']}"}]
                    )
                except SubscriptionQRError:
                    pass
            await self.api.send_text(chat_id, "\n".join(lines), detail_keyboard)
            return
        if data.startswith(("customer:renew_confirm:", "customer:topup_confirm:")):
            if not isinstance(uid, int):
                return
            action, service_id = data.split(":", 2)[1], data.split(":", 2)[2]
            service = self.data.get_customer_service(service_id, telegram_id=uid)
            product = self.data.get_product(str(service.get("product_id") or ""), enabled_only=True) if service else None
            if not service or not product or product.get("panel_id") != service.get("panel_id"):
                await self.api.send_text(chat_id, "سرویس متعلق به این حساب نیست یا محصول اصلی در دسترس نیست.", BACK_MENU)
                return
            is_renewal = action == "renew_confirm"
            change_type = "renewal" if is_renewal else "traffic_topup"
            try:
                change_service = CustomerServiceChangeService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                live_preview = await change_service.preview(
                    telegram_id=uid, service_id=service_id, change_type=change_type,
                    now=int(datetime.now(timezone.utc).timestamp()))
            except Exception as exc:
                await self.api.send_text(chat_id,
                    f"فعلاً امکان بررسی زنده سرویس از پنل وجود ندارد ({type(exc).__name__}). هیچ مبلغی کسر نشده؛ بعداً دوباره تلاش کن.",
                    BACK_MENU)
                return
            quantity = int(live_preview["quantity"])
            if is_renewal:
                detail = (f"تمدید: {quantity} ساعت\n"
                          f"انقضای فعلی: {live_preview.get('before_expires_at') or 'نامشخص'}\n"
                          f"انقضای پس از تمدید: {live_preview['target_expires_at']}")
            else:
                before_limit = int(live_preview["before_traffic_limit_bytes"])
                remaining = int(live_preview["remaining_traffic_bytes_before"])
                target_limit = int(live_preview["target_traffic_limit_bytes"])
                detail = (f"افزایش حجم: {quantity / (1024 ** 3):.2f} GiB\n"
                          f"سقف فعلی: {before_limit / (1024 ** 3):.2f} GiB · باقی‌مانده تقریبی: {remaining / (1024 ** 3):.2f} GiB\n"
                          f"سقف پس از افزایش: {target_limit / (1024 ** 3):.2f} GiB")
            execute_action = "renew_execute" if is_renewal else "topup_execute"
            await self.api.send_text(chat_id,
                f"تأیید عملیات سرویس\n{detail}\nهزینه: {int(live_preview['amount_minor']):,} {live_preview['currency']}\n\nاین پیش‌نمایش از اطلاعات فعلی پنل ساخته شده است؛ هنگام تأیید، وضعیت دوباره بررسی می‌شود. با تأیید، مبلغ از کیف پول کسر می‌شود.",
                {"inline_keyboard":[[{"text":"تأیید و پرداخت","callback_data":f"customer:{execute_action}:{service_id}"}],
                                    [{"text":"انصراف","callback_data":"customer:services"}]]})
            return
        if data.startswith(("customer:renew_execute:", "customer:topup_execute:")):
            if not isinstance(uid, int):
                return
            action, service_id = data.split(":", 2)[1], data.split(":", 2)[2]
            message_id = int(message.get("message_id") or 0)
            change_type = "renewal" if action == "renew_execute" else "traffic_topup"
            idempotency_key = f"customer-service-change:{uid}:{service_id}:{change_type}:{message_id}"
            try:
                change_service = CustomerServiceChangeService(
                    self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                if change_type == "renewal":
                    result = await change_service.renew(telegram_id=uid, service_id=service_id,
                        idempotency_key=idempotency_key, now=int(datetime.now(timezone.utc).timestamp()))
                    expiry = result.get("expires_at", "")
                    await self.api.send_text(chat_id, f"✅ تمدید سرویس ثبت شد.\nانقضای جدید: {expiry}\nمبلغ: {int(result['amount_minor']):,} {result['currency']}\nشناسه پیگیری: {result['operation_id']}\nبرای مشاهده وضعیت: /my-service-operations", BACK_MENU)
                else:
                    result = await change_service.top_up_traffic(telegram_id=uid, service_id=service_id,
                        idempotency_key=idempotency_key, now=int(datetime.now(timezone.utc).timestamp()))
                    gib = int(result.get("traffic_limit_bytes") or 0) / (1024 ** 3)
                    await self.api.send_text(chat_id, f"✅ افزایش حجم ثبت شد.\nسقف جدید: {gib:.2f} GiB\nمبلغ: {int(result['amount_minor']):,} {result['currency']}\nشناسه پیگیری: {result['operation_id']}\nبرای مشاهده وضعیت: /my-service-operations", BACK_MENU)
            except PurchaseNeedsReconciliation as exc:
                await self.api.send_text(chat_id, f"⚠️ عملیات نیازمند بررسی مدیر است؛ مبلغ رزرو شده و برای جلوگیری از تغییر تکراری، دوباره تلاش نکن. شناسه: {str(exc)[:100]}", BACK_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"عملیات انجام نشد: {str(exc)[:180]}", BACK_MENU)
            except Exception as exc:
                await self.api.send_text(chat_id, f"عملیات با خطای ارائه‌دهنده مواجه شد ({type(exc).__name__})؛ وضعیت باید بررسی شود.", BACK_MENU)
            return
        if data == "content:open" and is_admin:
            await self.api.send_text(chat_id, "📝 مدیریت محتوا\nعنوان، متن‌ها، برچسب دکمه‌ها و لوگو را ویرایش کن.", content_admin_keyboard("customer"))
            return
        if data == "admin:keyboard-mode" and is_admin:
            next_mode = "reply" if keyboard_mode(self.content) == "inline" else "inline"
            self.content.set("home_keyboard_mode", next_mode)
            mode_label = "شیشه‌ای (Inline)" if next_mode == "inline" else "پایین صفحه (Reply)"
            await self.api.send_text(chat_id, f"حالت منوی اصلی ذخیره شد: {mode_label}\nبرای مشاهده، /start را دوباره بزن.", keyboard_mode_admin(self.content))
            return
        if data == "admin:keyboard-settings" and is_admin:
            await self.api.send_text(chat_id, "🎛 حالت نمایش منوی اصلی\nشیشه‌ای (Inline) یا کیبورد پایین صفحه را انتخاب کن:", keyboard_mode_admin(self.content))
            return
        if data == "content:buttons" and is_admin:
            await self.api.send_text(chat_id, "🎛 نمایش دکمه‌های منوی اصلی\nبرای روشن/خاموش کردن هر گزینه روی آن بزن. تغییرات بلافاصله ذخیره می‌شوند.", menu_visibility_keyboard(self.content))
            return
        if data.startswith("content:toggle:") and is_admin:
            key = data.removeprefix("content:toggle:")
            allowed = {field[0] for field in MENU_VISIBILITY_FIELDS}
            if key not in allowed:
                await self.api.send_text(chat_id, "این گزینه قابل تغییر نیست.", menu_visibility_keyboard(self.content))
                return
            current = _enabled(self.content, key)
            self.content.set(key, "off" if current else "on")
            await self.api.send_text(chat_id, f"وضعیت دکمه ذخیره شد: {'خاموش' if current else 'روشن'}", menu_visibility_keyboard(self.content))
            return
        if data.startswith("admin:"):
            if not is_admin:
                self._queue_unauthorized_admin_callback_alert(uid)
                await self.api.send_text(chat_id, "این بخش فقط برای مدیر مجاز است.", BACK_MENU)
                return
            section = data.split(":", 1)[1]
            if section == "home":
                admin_keyboard = {"inline_keyboard": list(ADMIN_MENU["inline_keyboard"]) + [
                    [{"text": "🎛 حالت کیبورد منوی اصلی", "callback_data": "admin:keyboard-settings"}]
                ]}
                await self.api.send_text(chat_id, "🛡 پنل مدیریت ربات\nیک بخش را انتخاب کن:", admin_keyboard)
                return
            if section == "panels":
                panel_buttons = [[{"text":"➕ افزودن پنل جدید","callback_data":"admin:add_panel"}],
                    [{"text":"📋 فهرست پنل‌ها","callback_data":"admin:list_panels"}],
                    [{"text":"🔎 بررسی اتصال","callback_data":"admin:check_panel"}],
                    [{"text":"⬅️ بازگشت به مدیریت","callback_data":"admin:settings"}]]
                await self.api.send_text(chat_id, "🖥 مدیریت پنل‌ها\nافزودن، بررسی و مدیریت اتصال‌ها:", {"inline_keyboard":panel_buttons})
                return
            elif section == "add_panel":
                await self.api.send_text(chat_id, "نوع پنلی که می‌خواهی اضافه کنی انتخاب کن:", {"inline_keyboard":[
                    [{"text":"PasarGuard","callback_data":"admin:provider:pasarguard"}],
                    [{"text":"Rebecca","callback_data":"admin:provider:rebecca"}],
                    [{"text":"Sanaei / 3x-ui","callback_data":"admin:provider:sanaei"}],
                    [{"text":"⬅️ بازگشت","callback_data":"admin:panels"}]]})
                return
            elif section.startswith("provider:"):
                provider = section.split(":",1)[1]
                if provider not in {"pasarguard","rebecca","sanaei"}:
                    await self.api.send_text(chat_id,"نوع پنل معتبر نیست.",ADMIN_MENU); return
                await self.api.send_text(chat_id, f"پنل {provider} انتخاب شد. روش احراز هویت را انتخاب کن:", {"inline_keyboard":[
                    [{"text":"🔑 API Key / Token","callback_data":f"admin:auth:{provider}:api_key"}],
                    [{"text":"👤 نام کاربری و رمز عبور","callback_data":f"admin:auth:{provider}:username_password"}],
                    [{"text":"⬅️ بازگشت","callback_data":"admin:add_panel"}]]})
                return
            elif section.startswith("auth:"):
                _, provider, auth_mode = section.split(":",2)
                await self.api.send_text(chat_id, f"روش انتخاب‌شده: {provider} / {auth_mode}\n\nاطلاعات پنل را با این قالب ثبت کن؛ مقدار محرمانه را در تلگرام ننویس و فقط مرجع متغیر محیطی بده:\n/panel add ID | نام | {provider} | https://panel.example | {auth_mode} | env:SECRET_VARIABLE | on | {{}}\n\nبرای روش نام کاربری/رمز عبور، secret variable باید به‌صورت امن در محیط سرور تنظیم شود.", ADMIN_MENU)
                return
            elif section == "list_panels":
                panels = self.data.list_panel_connections()
                body = "\n".join(f"• {p['panel_id']} — {p['label']} ({p['provider']}) — {'فعال' if p['enabled'] else 'غیرفعال'} — {p['last_check_status']}" for p in panels) or "هنوز پنلی ثبت نشده است."
            elif section == "check_panel":
                body = "برای بررسی فقط‌خواندنی اجرا کن: /panel-check PANEL_ID"
            elif section == "products":
                body = ("🛍 مدیریت فروشگاه\nمحصولات پنلی: /product add ...\n"
                        "محصول دستی: /manual-product add ID | عنوان | قیمت | ارز | instant/inventory/manual | متن-تحویل | توضیح | category_id | سقف خرید | ترتیب | شرایط خرید | image_url HTTPS | visible_from_utc | visible_until_utc\n"
                        "ویرایش: /manual-product edit ID | عنوان | قیمت | ارز | حالت | متن-تحویل | توضیح | category_id | سقف خرید | ترتیب | شرایط خرید | image_url | visible_from_utc | visible_until_utc؛ برای حذف تصویر/زمان مقدار -؛ زمان UTC به قالب YYYY-MM-DDTHH:MM:SSZ؛ آرشیو: /manual-product archive ID؛ فهرست: /manual-product list\n"
                        "فهرست محصولات پنلی: /products\n\n"
                        "📦 سفارش‌های تحویل دستی: /manual-orders [pending_delivery|fulfilled|all]\n"
                        "تحویل سفارش: /manual-fulfill ORDER_ID | متن تحویل")
            elif section == "resellers":
                body = "👥 مدیریت نمایندگی\n/reseller-plan add ...\n/reseller-plans"
            elif section == "content":
                await self.api.send_text(chat_id, "✍️ مدیریت متن‌ها، دکمه‌ها و لوگو", content_admin_keyboard("customer"))
                return
            elif section == "finance":
                body = ("💰 مدیریت مالی\n"
                        "تنظیم کارت‌به‌کارت: /deposit-info set CARD | HOLDER | BANK\n"
                        "رسیدهای منتظر بررسی: /pending-deposits؛ مشاهده تصویر: /deposit-receipt-view PAYMENT_ID\n"
                        "تأیید: /wallet-verify PAYMENT_ID | AMOUNT | IRR | TRACKING_CODE؛ رد: /wallet-reject PAYMENT_ID | REASON\n"
                        "شارژ دستی /wallet-credit فقط برای پرداختی است که خارج از ربات مستقل بررسی شده باشد.\n"
                        "سابقه تغییرناپذیر یک پرداخت: /payment-events PAYMENT_ID\n"
                        "کد تخفیف: /discount add CODE | PERCENT | MAX_USES | EXPIRES_EPOCH | PRODUCT_IDS_CSV، سپس /discount list یا /discount enable CODE on|off.\n"
                        "عملیات تمدید/افزایش ترافیک نیازمند بررسی: /service-operations [reconcile_required|pending|completed|refunded|all]\n"
                        "جزئیات عملیات: /service-operation OPERATION_ID\n"
                        "مقایسه خواندنی با پنل (PasarGuard): /service-operation-check OPERATION_ID\n"
                        "این فرمان‌ها فقط گزارش می‌دهند و وضعیت پنل یا بازپرداخت را خودکار تغییر نمی‌دهند.")
            elif section == "tickets":
                body = "🎫 تیکت‌های پشتیبانی\n/tickets\n/reply TICKET_ID | پاسخ\n\nمسیر ارسال اعلان تیکت: /notify-route add support_ticket | CHAT_ID | TOPIC_ID\nفهرست مسیرها: /notify-route list"
            else:
                body = "⚙️ تنظیمات ربات\nتنظیمات محرمانه فقط از محیط سرور مدیریت می‌شوند. کلیدها در تلگرام نمایش داده نمی‌شوند."
            await self.api.send_text(chat_id, body, ADMIN_MENU)
            return
        if data.startswith("content:edit:"):
            prompt = begin_content_edit(data, self._pending_content, chat_id)
            await self.api.send_text(chat_id, prompt or "این فیلد قابل ویرایش نیست.", content_admin_keyboard("customer"))
            return
        if data == "content:back":
            await self._send_brand_message(chat_id, self._customer_menu(customer_id=uid, include_admin=is_admin))
            return
        if data == "customer:home":
            self.data.ensure_customer(uid, user.get("username", ""), user.get("first_name", ""))
            await self._send_brand_message(chat_id, self._customer_menu(customer_id=uid, include_admin=is_admin))
            return
        key = data.partition(":")[2] if data.startswith("customer:") else ""
        discount_code = None
        if key == "profile":
            self.data.ensure_customer(uid, user.get("username", ""), user.get("first_name", ""))
            services = self.data.list_services(uid)
            orders = self.data.list_customer_orders(uid, limit=50)
            balances = self.data.wallet_balances(uid)
            name = str(user.get("first_name") or "—").strip()
            last_name = str(user.get("last_name") or "").strip()
            if last_name:
                name = f"{name} {last_name}"
            username = str(user.get("username") or "").strip()
            balance_lines = [f"• {currency}: {amount:,}" for currency, amount in balances.items()] or ["• IRR: 0"]
            body = (f"👤 حساب کاربری من\n\nنام: {name}\n"
                    f"نام کاربری: @{username if username else 'ثبت نشده'}\n"
                    f"شناسه تلگرام: {uid}\n\n"
                    f"🔐 تعداد سرویس‌ها: {len(services)}\n"
                    f"🧾 تعداد سفارش‌های اخیر: {len(orders)}\n\n"
                    "💳 موجودی کیف پول:\n" + "\n".join(balance_lines))
            await self.api.send_text(chat_id, body, {"inline_keyboard": [
                [{"text": "⚙️ سرویس‌های من", "callback_data": "customer:services"}],
                [{"text": "🧾 سفارش‌های من", "callback_data": "customer:orders:0"}],
                [{"text": "💳 کیف پول", "callback_data": "customer:wallet"},
                 {"text": "🎫 پشتیبانی", "callback_data": "customer:support"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]})
            return
        if key == "support":
            await self.api.send_text(chat_id, "🎫 پشتیبانی\n\nاز این بخش می‌توانی تیکت جدید ثبت کنی یا تیکت‌های قبلی خودت را ببینی. برای ادامه گفتگو از فرمان /ticket-reply استفاده کن.", {"inline_keyboard": [
                [{"text": "✍️ ثبت تیکت جدید", "callback_data": "customer:support:new"}],
                [{"text": "📂 تیکت‌های من", "callback_data": "customer:support:list:0"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]})
            return
        if key == "support:new":
            self._pending_support.add(uid)
            await self.api.send_text(chat_id, "✍️ متن مشکل یا درخواستت را در یک پیام بفرست تا تیکت ثبت شود. برای لغو، /cancel را بفرست.", {"inline_keyboard": [
                [{"text": "❌ لغو ثبت تیکت", "callback_data": "customer:support:cancel"}],
                [{"text": "⬅️ بازگشت به پشتیبانی", "callback_data": "customer:support"}],
            ]})
            return
        if key == "support:cancel":
            self._pending_support.discard(uid)
            await self.api.send_text(chat_id, "ثبت تیکت لغو شد.", {"inline_keyboard": [
                [{"text": "⬅️ بازگشت به پشتیبانی", "callback_data": "customer:support"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]})
            return
        if key.startswith("support:list"):
            tickets = self.data.list_tickets(uid)
            page_size = 5
            page_count = max(1, (len(tickets) + page_size - 1) // page_size)
            try:
                page = max(0, int(key.rpartition(":")[2])) if key.count(":") >= 2 else 0
            except ValueError:
                page = 0
            page = min(page, page_count - 1)
            status_labels = {"open": "باز", "pending": "در انتظار پاسخ", "closed": "بسته", "resolved": "حل‌شده"}
            lines = [f"📂 تیکت‌های من — صفحه {page + 1} از {page_count}"]
            selected = tickets[page * page_size:(page + 1) * page_size]
            for ticket in selected:
                status = str(ticket.get("status") or "unknown")
                lines.append(f"\n• {ticket['subject']}\nشناسه: {ticket['ticket_id']}\nوضعیت: {status_labels.get(status, status)}\nبرای مشاهده پیام‌ها: /ticket {ticket['ticket_id']}\nبرای پاسخ: /ticket-reply {ticket['ticket_id']} | متن")
            if not tickets:
                lines.append("\nهنوز تیکت پشتیبانی ثبت نکرده‌ای.")
            navigation = []
            if page > 0:
                navigation.append({"text": "⬅️ قبلی", "callback_data": f"customer:support:list:{page - 1}"})
            if page + 1 < page_count:
                navigation.append({"text": "بعدی ➡️", "callback_data": f"customer:support:list:{page + 1}"})
            rows = ([navigation] if navigation else []) + [
                [{"text": "✍️ ثبت تیکت جدید", "callback_data": "customer:support:new"}],
                [{"text": "⬅️ بازگشت به پشتیبانی", "callback_data": "customer:support"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]
            await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard": rows})
            return
        if key == "orders" or key.startswith("orders:"):
            try:
                page = max(0, min(9, int(key.partition(":")[2] or "0")))
            except ValueError:
                page = 0
            orders = self.data.list_customer_orders(uid, limit=50)
            page_size = 5
            page_count = max(1, (len(orders) + page_size - 1) // page_size)
            page = min(page, page_count - 1)
            status_labels = {
                "pending": "در انتظار پرداخت", "pending_payment": "در انتظار پرداخت",
                "awaiting_payment": "در انتظار پرداخت",
                "paid": "پرداخت‌شده", "fulfilling": "در حال تحویل", "fulfilled": "تحویل‌شده",
                "failed": "ناموفق", "cancelled": "لغوشده", "refunded": "بازپرداخت‌شده",
                "reconcile_required": "نیازمند بررسی مدیر", "pending_delivery": "در انتظار تحویل",
            }
            lines = [f"🧾 سفارش‌های من — صفحه {page + 1} از {page_count}"]
            selected = orders[page * page_size:(page + 1) * page_size]
            file_buttons = []
            for order in selected:
                status = str(order.get("status") or "unknown")
                lines.append(
                    f"\n• {order['title']}\nشناسه: {order['order_id']}\n"
                    f"مبلغ: {int(order['amount_minor']):,} {order['currency']}\n"
                    f"وضعیت: {status_labels.get(status, status)}"
                    + (f"\nتخفیف: {order['discount_code']} (−{int(order['discount_amount_minor']):,})"
                       if order.get("discount_code") else "")
                    + (f"\nآخرین تغییر: {order['updated_at']}" if order.get("updated_at") else "")
                )
                if order.get("can_redeliver_file"):
                    file_buttons.append([{"text": f"📥 دریافت فایل {str(order['order_id'])[:8]}",
                        "callback_data": f"customer:file_retry:{order['order_id']}"}])
            if not orders:
                lines.append("\nهنوز سفارشی برای این حساب ثبت نشده است.")
            rows = []
            navigation = []
            if page > 0:
                navigation.append({"text": "⬅️ قبلی", "callback_data": f"customer:orders:{page - 1}"})
            if page + 1 < page_count:
                navigation.append({"text": "بعدی ➡️", "callback_data": f"customer:orders:{page + 1}"})
            if navigation:
                rows.append(navigation)
            rows.extend(file_buttons)
            rows.extend([
                [{"text": "🛒 بازگشت به فروشگاه", "callback_data": "customer:shop"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ])
            await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard": rows})
            return
        if key == "shop":
            await self._show_shop(chat_id, self._customer_menu(customer_id=uid))
            return
        if key == "miniapp":
            await self.api.send_text(chat_id, "📱 مینی‌اپلیکیشن هنوز به این ربات متصل نشده است. پس از تنظیم آدرس امن، این گزینه فعال می‌شود.", BACK_MENU)
            return
        if key == "trials":
            await self.api.send_text(chat_id, "🧪 قابلیت تست/اشتراک آزمایشی هنوز پیاده‌سازی نشده و فعلاً در دسترس نیست. این گزینه تا زمان اتصال جریان واقعی فعال نخواهد شد.", BACK_MENU)
            return
        if key.startswith("file_retry:"):
            order_id = key.partition(":")[2].strip()
            order = self.data.get_manual_order_delivery(order_id=order_id, telegram_id=uid) if order_id else None
            if not order:
                await self.api.send_text(chat_id, "فایل این سفارش در دسترس نیست یا سفارش متعلق به حساب شما نیست.", BACK_MENU)
                return
            await self._send_manual_file_delivery(chat_id, order)
            return
        if key.startswith("manualbuy:") and not key.startswith("manualbuy_confirm:"):
            selection = key.partition(":")[2]
            product_id, separator, variant_id = selection.partition("~")
            product = self.data.get_manual_product(product_id)
            if not product:
                await self.api.send_text(chat_id, "این محصول دیگر در دسترس نیست.", BACK_MENU)
                return
            variants = self.data.list_manual_variants(product_id)
            if variants and not separator:
                rows = []
                for variant in variants[:20]:
                    variant_delivery = self.data.manual_variant_delivery(product_id, variant["variant_id"])
                    stock = self.data.manual_inventory_count(product_id, variant["variant_id"]) if variant_delivery["delivery_mode"] == "inventory" else None
                    unavailable = stock == 0
                    label = f"{variant['title'][:22]} — {int(variant['price_minor']):,} {product['currency']}"
                    if stock is not None: label += f" — موجودی {stock}"
                    if unavailable: label += " — ناموجود"
                    if not unavailable:
                        rows.append([{"text": label[:60], "callback_data": f"customer:manualbuy:{product_id}~{variant['variant_id']}"}])
                rows.append([{"text":"بازگشت به فروشگاه","callback_data":"customer:shop"}])
                await self.api.send_text(chat_id, f"یک نوع از «{product['title']}» را انتخاب کن. قیمت و موجودی مربوط به همان انتخاب است:", {"inline_keyboard":rows})
                return
            selected_variant = self.data.get_manual_variant(product_id, variant_id) if separator else None
            if separator and not selected_variant:
                await self.api.send_text(chat_id, "این نوع محصول دیگر در دسترس نیست.", BACK_MENU)
                return
            inventory_variant = variant_id if separator else ""
            selected_delivery = self.data.manual_variant_delivery(product_id, variant_id) if separator else {
                "delivery_mode": str(product["delivery_mode"]), "delivery_payload": str(product.get("delivery_payload") or "")}
            selected_mode = selected_delivery["delivery_mode"]
            if selected_mode == "inventory" and self.data.manual_inventory_count(product_id, inventory_variant) == 0:
                await self.api.send_text(chat_id, "این انتخاب فعلاً ناموجود است؛ پس از شارژ همین موجودی دوباره در دسترس قرار می‌گیرد.", BACK_MENU)
                return
            price = int(selected_variant["price_minor"] if selected_variant else product["price_minor"])
            display_title = str(product["title"]) + (" — " + str(selected_variant["title"]) if selected_variant else "")
            delivery_label = ("خودکار پس از پرداخت" if selected_mode in {"instant", "inventory"} else
                "ارسال فایل تلگرام پس از پرداخت" if selected_mode == "file" else "دستی توسط پشتیبانی")
            cap = int(product.get("max_purchases_per_customer") or 0)
            cap_text = f"\nسقف خرید هر مشتری: {cap}" if cap else "\nسقف خرید: نامحدود"
            description = str(product.get("description") or "").strip()
            description_text = "\nتوضیحات: " + (description if len(description) <= 1200 else description[:1197].rstrip() + "...") if description else ""
            terms_text = str(product.get("terms_text") or "").strip()
            terms_hash = hashlib.sha256(terms_text.encode("utf-8")).hexdigest()[:12] if terms_text else ""
            terms_display = "\n\nشرایط خرید:\n" + terms_text if terms_text else ""
            selected_key = product_id + ("~" + variant_id if separator else "")
            if separator:
                # Bind the confirmation to the exact variant title, price, terms and effective delivery configuration.
                quote_hash = self.data.manual_variant_quote_hash(product_id, variant_id)
                callback_data = f"customer:mv:{selected_key}:{quote_hash}"
            else:
                # Base products also bind the final confirmation to price and delivery config.
                quote_hash = self.data.manual_product_quote_hash(product_id)
                callback_data = f"customer:mp:{product_id}:{quote_hash}"
            confirm_label = "می‌پذیرم و خرید" if terms_text else "تأیید خرید"
            await self.api.send_text(chat_id, f"تأیید خرید «{display_title}»\nقیمت نهایی: {price:,} {product['currency']}{description_text}{terms_display}\nموجودی کیف پول: {self.data.wallet_balance(uid, product['currency']):,} {product['currency']}{cap_text}\nتحویل: {delivery_label}",
                {"inline_keyboard":[[{"text":confirm_label,"callback_data":callback_data,"style":"success"}],[{"text":"بازگشت به فروشگاه","callback_data":"customer:shop"}]]})
            return
        if key.startswith(("manualbuy_confirm:", "mv:", "mp:")):
            confirmation = key.partition(":")[2]
            selection, sep, accepted_hash = confirmation.partition(":")
            product_id, variant_sep, variant_id = selection.partition("~")
            is_variant_confirmation = key.startswith("mv:")
            is_product_quote_confirmation = key.startswith("mp:")
            try:
                result = self.data.buy_manual_product(telegram_id=uid, product_id=product_id,
                    variant_id=variant_id if variant_sep else "",
                    idempotency_key=f"manual-buy:{uid}:{message.get('message_id') or 0}:{product_id}:{variant_id if variant_sep else 'base'}",
                    expected_terms_hash=accepted_hash if sep and not is_variant_confirmation and not is_product_quote_confirmation else None,
                    expected_variant_quote_hash=accepted_hash if sep and is_variant_confirmation else None,
                    expected_manual_quote_hash=accepted_hash if sep and is_product_quote_confirmation else None)
                already_processed = bool(result.get("idempotent_replay"))
                purchase_label = "این خرید قبلاً ثبت شده است" if already_processed else "خرید انجام شد"
                if result["delivery_mode"] == "file":
                    await self._send_manual_file_delivery(chat_id, result)
                    return
                if result["delivery_mode"] in {"instant", "inventory"}:
                    body = f"✅ {purchase_label}\nمحصول: {result['title']}\nمبلغ: {int(result['amount_minor']):,} {result['currency']}\nشناسه سفارش: {result['order_id']}\n\nاطلاعات محصول:\n{result['delivery_payload']}"
                elif result.get("status") == "fulfilled" and result.get("delivery_payload"):
                    body = f"✅ این سفارش قبلاً تحویل شده است.\nمحصول: {result['title']}\nشناسه سفارش: {result['order_id']}\n\nاطلاعات تحویل:\n{result['delivery_payload']}"
                else:
                    status_text = "این سفارش قبلاً ثبت شده و در انتظار تحویل است." if already_processed else "سفارش ثبت شد و مبلغ کسر شد."
                    body = f"✅ {status_text}\nمحصول: {result['title']}\nمبلغ: {int(result['amount_minor']):,} {result['currency']}\nشناسه سفارش: {result['order_id']}\nتحویل توسط مدیر/پشتیبانی انجام می‌شود."
                await self.api.send_text(chat_id, body, BACK_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"خرید انجام نشد: {exc}", BACK_MENU)
            return
        if key.startswith("category:"):
            selected = key.partition(":")[2]
            await self._show_shop(chat_id, self._customer_menu(customer_id=uid), None if selected == "all" else selected)
            return
        if key.startswith("buy:"):
            product_id = key.partition(":")[2]
            product = next((p for p in self.data.list_products() if p["product_id"] == product_id), None)
            if not product:
                await self.api.send_text(chat_id, "این محصول دیگر فعال نیست.", BACK_MENU)
                return
            mode = product["pricing_mode"]
            rate = int(product["unit_price_minor"])
            price = int(product["price_minor"])
            price_line = f"هزینه اولیه: {price:,} {product['currency']}"
            if mode == "hourly": price_line += f"\nنرخ مصرف زمان: {rate:,} {product['currency']} در ساعت"
            elif mode == "usage": price_line += f"\nنرخ ترافیک: {rate:,} {product['currency']} به‌ازای هر GiB"
            await self.api.send_text(chat_id, f"تأیید خرید «{product['title']}»\nمدل: {mode}\n{price_line}\nموجودی فعلی: {self.data.wallet_balance(uid, product['currency']):,} {product['currency']}\n\nبا تأیید، مبلغ اولیه از کیف پول رزرو می‌شود و درخواست ساخت سرویس به پنل ارسال خواهد شد.", {"inline_keyboard":[[{"text":"تأیید خرید","callback_data":f"customer:buy_confirm:{product_id}","style":"success"}],[{"text":"🎟 ورود کد تخفیف","callback_data":f"customer:buy_discount:{product_id}"}],[{"text":"انصراف","callback_data":"customer:shop"}]]})
            return
        if key == "discount_cancel":
            self._pending_purchase_discount.pop(uid, None)
            await self.api.send_text(chat_id, "ورود کد تخفیف لغو شد.", BACK_MENU)
            return
        if key.startswith("buy_discount:"):
            product_id = key.partition(":")[2]
            product = self.data.get_product(product_id, enabled_only=True)
            if not product:
                await self.api.send_text(chat_id, "این محصول دیگر فعال نیست.", BACK_MENU)
                return
            self._pending_purchase_discount[uid] = {"product_id": product_id, "discount_code": ""}
            await self.api.send_text(chat_id, f"کد تخفیف محصول «{product['title']}» را در پیام بعدی بفرست. برای لغو /cancel را بزن.")
            return
        if key.startswith("buy_discount_confirm:"):
            product_id = key.partition(":")[2]
            pending = self._pending_purchase_discount.get(uid) or {}
            if pending.get("product_id") != product_id or not pending.get("discount_code"):
                await self.api.send_text(chat_id, "پیش‌نمایش کد تخفیف منقضی یا نامعتبر است؛ دوباره از فروشگاه شروع کن.", BACK_MENU)
                return
            discount_code = str(pending["discount_code"])
            self._pending_purchase_discount.pop(uid, None)
            key = f"buy_confirm:{product_id}"
        if key.startswith("buy_confirm:"):
            product_id = key.partition(":")[2]
            if not discount_code:
                pending = self._pending_purchase_discount.get(uid) or {}
                if pending.get("product_id") == product_id:
                    self._pending_purchase_discount.pop(uid, None)
            message_id = (message.get("message_id") or 0)
            idempotency_key = f"customer-purchase:{uid}:{chat_id}:{message_id}:{product_id}"
            if discount_code:
                idempotency_key += f":discount:{discount_code}"
            try:
                outcome = await self.purchase_service.buy_from_wallet(telegram_id=uid, product_id=product_id,
                    idempotency_key=idempotency_key, discount_code=discount_code,
                    now=int(datetime.now(timezone.utc).timestamp()))
                body = f"خرید و ساخت سرویس تأیید شد.\nشناسه سفارش: {outcome.order_id}\nنام کاربری: {outcome.username}\nمبلغ اولیه: {outcome.amount_minor:,} {outcome.currency}"
                if outcome.subscription_url: body += f"\n\nلینک اشتراک خصوصی:\n{outcome.subscription_url}"
                if not outcome.idempotent_replay:
                    await self._emit_trust_event(TrustEvent.NEW_SUBSCRIPTION,
                        {"public_order_id": outcome.order_id[:20], "amount_minor": outcome.amount_minor,
                         "currency": outcome.currency}, f"purchase:{outcome.order_id}")
                await self.api.send_text(chat_id, body, BACK_MENU)
            except PurchaseNeedsReconciliation as exc:
                await self.api.send_text(chat_id, f"سفارش برای جلوگیری از ساخت تکراری در وضعیت بررسی قرار گرفت. مبلغ رزرو شده است و باید نتیجه پنل بررسی شود.\n{exc}", BACK_MENU)
            except (CustomerStoreError, ValueError) as exc:
                await self.api.send_text(chat_id, f"خرید انجام نشد: {exc}", BACK_MENU)
            return
        if key == "resellers":
            plans = [plan for plan in self.data.list_reseller_plans(enabled_only=True)
                     if (self.data.get_panel_connection(str(plan["panel_id"])) or {}).get("enabled")
                     and (self.data.get_panel_connection(str(plan["panel_id"])) or {}).get("provider") != "sanaei"]
            if not plans:
                await self.api.send_text(chat_id, "فعلاً پلن نمایندگی فعالی وجود ندارد.", BACK_MENU)
            else:
                lines = ["👥 پلن‌های نمایندگی:"]
                buttons = []
                for plan in plans[:25]:
                    mode = plan["pricing_mode"]
                    rate = f" · نرخ {plan['unit_price_minor']:,} {plan['currency']}" if mode in {"hourly","usage","per_gb","per_tb"} else f" · {plan['price_minor']:,} {plan['currency']}"
                    panel = self.data.get_panel_connection(plan["panel_id"])
                    provider = panel["provider"] if panel else "نامشخص"
                    display_name = plan.get("display_label") or plan["title"]
                    display_icon = (plan.get("display_icon") or "").strip()
                    lines.append(f"• {display_icon + ' ' if display_icon else ''}{display_name} — {mode}{rate} · پنل {plan['panel_id']} ({provider})")
                    if provider == "pasarguard" and mode in {"fixed", "unlimited", "hourly", "usage"} and plan.get("provider_role_id"):
                        buttons.append([{"text": f"خرید native {plan['title'][:22]}", "callback_data": f"customer:reseller_buy:{plan['plan_id']}"}])
                    elif provider == "rebecca" and mode in {"fixed", "unlimited", "hourly", "usage"}:
                        buttons.append([{"text": f"خرید رباتی {plan['title'][:22]}", "callback_data": f"customer:reseller_buy:{plan['plan_id']}"}])
                        lines.append("  این پلن به‌صورت نمایندگی مدیریت‌شده داخل ربات اجرا می‌شود؛ اکانت ادمین جداگانه در پنل ساخته نمی‌شود.")
                    else:
                        lines.append("  خرید خودکار برای این پلن فعال نیست؛ نوع پلن یا اتصال پنل تأیید نشده است.")
                lines.append("\nپلن‌های ساعتی و مصرفی از کیف پول مالک کسر می‌شوند؛ هنگام اتمام موجودی، اکانت نمایندگی برای تعلیق به صف می‌رود.")
                buttons.append([{"text":"📋 نمایندگی‌های من","callback_data":"customer:myresellers"},{"text":"⬅️ بازگشت","callback_data":"customer:home"}])
                await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard":buttons})
            return
        if key == "myresellers":
            accounts = self.data.list_reseller_accounts(uid)
            body = "\n".join(f"• {a['username']} — {a['pricing_mode']} — {a['status']} — پنل {a['panel_id']} — بدهی {int(a.get('outstanding_debt_minor') or 0):,} {a['currency']}" for a in accounts)
            buttons = [[{"text": f"مدیریت {a['username'][:20]}", "callback_data": f"customer:rshop:{a['account_id']}"}]
                       for a in accounts if a["status"] == "active"]
            buttons.append([{"text":"⬅️ بازگشت","callback_data":"customer:resellers"}])
            await self.api.send_text(chat_id, body or "هنوز نمایندگی خریداری نکرده‌ای.", {"inline_keyboard":buttons})
            return
        if key.startswith("rshop:"):
            account_id = key.partition(":")[2]
            account = self.data.get_reseller_account(account_id, uid)
            if not account or account["status"] != "active":
                await self.api.send_text(chat_id, "نمایندگی فعال و متعلق به این حساب پیدا نشد.", BACK_MENU); return
            panel_connection = self.data.get_panel_connection(account["panel_id"])
            if not panel_connection or not panel_connection["enabled"]:
                await self.api.send_text(chat_id, "اتصال پنل این نمایندگی غیرفعال است؛ ساخت کانفیگ موقتاً متوقف شده.", BACK_MENU); return
            products = self.data.list_products(panel_id=account["panel_id"])
            buttons = []
            for product in products[:25]:
                buttons.append([{"text": f"ساخت {product['title'][:20]}", "callback_data": f"customer:r:{account_id}:{product['product_id']}"}])
            buttons.append([{"text":"⬅️ نمایندگی‌های من","callback_data":"customer:myresellers"}])
            await self.api.send_text(chat_id, "ساخت کانفیگ برای مشتریان\nهزینه ساخت داخل پلن نمایندگی پوشش داده می‌شود؛ نرخ ساعتی/مصرفی از کیف پول نمایندگی کسر می‌شود.", {"inline_keyboard":buttons})
            return
        if key.startswith("r:"):
            parts = key.split(":")
            if len(parts) != 3:
                await self.api.send_text(chat_id, "درخواست نامعتبر است.", BACK_MENU); return
            account_id, product_id = parts[1], parts[2]
            account = self.data.get_reseller_account(account_id, uid)
            product = self.data.get_product(product_id, enabled_only=True)
            if not account or account["status"] != "active" or not product or product["panel_id"] != account["panel_id"]:
                await self.api.send_text(chat_id, "نمایندگی یا محصول در دسترس نیست.", BACK_MENU); return
            await self.api.send_text(chat_id, f"ساخت کانفیگ «{product['title']}» از سهمیه نمایندگی «{account['username']}» انجام شود؟\nاین عملیات از موجودی جداگانه محصول کم نمی‌کند؛ هزینه پلن نمایندگی اعمال می‌شود.", {"inline_keyboard":[[{"text":"تأیید ساخت کانفیگ","callback_data":f"customer:c:{account_id}:{product_id}"}],[{"text":"انصراف","callback_data":f"customer:rshop:{account_id}"}]]})
            return
        if key.startswith("c:"):
            parts = key.split(":")
            if len(parts) != 3:
                await self.api.send_text(chat_id, "درخواست نامعتبر است.", BACK_MENU); return
            account_id, product_id = parts[1], parts[2]
            account = self.data.get_reseller_account(account_id, uid)
            if not account or account["status"] != "active":
                await self.api.send_text(chat_id, "نمایندگی فعال و متعلق به این حساب پیدا نشد.", BACK_MENU); return
            message_id = message.get("message_id") or 0
            idempotency_key = f"reseller-service:{uid}:{message_id}:{account_id}:{product_id}"
            try:
                outcome = await self.purchase_service.buy_from_wallet(telegram_id=uid, product_id=product_id,
                    reseller_account_id=account_id, idempotency_key=idempotency_key,
                    now=int(datetime.now(timezone.utc).timestamp()))
                body = f"کانفیگ نمایندگی ساخته شد.\nشناسه سفارش: {outcome.order_id}\nنام کاربری: {outcome.username}"
                if outcome.subscription_url: body += f"\nلینک کانفیگ/اشتراک:\n{outcome.subscription_url}"
                await self.api.send_text(chat_id, body, BACK_MENU)
            except (CustomerStoreError, PurchaseNeedsReconciliation) as exc:
                await self.api.send_text(chat_id, f"ساخت کانفیگ انجام نشد یا نیازمند بررسی است: {exc}", BACK_MENU)
            return
        if key.startswith("reseller_buy:"):
            plan_id = key.partition(":")[2]
            plan = self.data.get_reseller_plan(plan_id, enabled_only=True)
            if not plan:
                await self.api.send_text(chat_id, "این پلن دیگر فعال نیست.", BACK_MENU)
                return
            panel = self.data.get_panel_connection(plan["panel_id"])
            if not panel or panel["provider"] not in {"pasarguard", "rebecca"}:
                await self.api.send_text(chat_id, "این پنل قابلیت نمایندگی ندارد؛ Sanaei فقط برای مدیریت سرویس‌های کاربران قابل استفاده است.", BACK_MENU)
                return
            if panel["provider"] == "pasarguard" and not plan.get("provider_role_id"):
                await self.api.send_text(chat_id, "برای PasarGuard ابتدا role ID را با /panel-roles دریافت و در پلن ثبت کن.", BACK_MENU)
                return
            mode = plan["pricing_mode"]
            price = int(plan["price_minor"])
            unit = int(plan["unit_price_minor"])
            detail = f"پلن: {plan['title']}\nنوع: {mode}\nهزینه اولیه: {price:,} {plan['currency']}"
            if mode == "hourly": detail += f"\nنرخ ساعتی: {unit:,} {plan['currency']}"
            elif mode == "usage": detail += f"\nنرخ مصرفی هر GiB: {unit:,} {plan['currency']}"
            detail += f"\nموجودی: {self.data.wallet_balance(uid, plan['currency']):,} {plan['currency']}"
            if panel["provider"] == "pasarguard":
                detail += "\nاکانت native در PasarGuard ساخته می‌شود؛ رمز اولیه فقط یک بار نمایش داده می‌شود و نسخه رمزگذاری‌شده برای مدیریت کانفیگ‌ها نگهداری می‌شود."
            else:
                detail += "\nدر Rebecca فقط در صورت وجود قرارداد API سازگار و مجوز ادمین، حساب نماینده native ساخته می‌شود؛ در غیر این صورت خرید پیش از کسر وجه رد خواهد شد."
            await self.api.send_text(chat_id, detail, {"inline_keyboard":[[{"text":"تأیید خرید نمایندگی","callback_data":f"customer:reseller_buy_confirm:{plan_id}"}],[{"text":"انصراف","callback_data":"customer:resellers"}]]})
            return
        if key.startswith("reseller_buy_confirm:"):
            plan_id = key.partition(":")[2]
            plan_data = self.data.get_reseller_plan(plan_id, enabled_only=True)
            if not plan_data:
                await self.api.send_text(chat_id, "این پلن دیگر فعال نیست.", BACK_MENU); return
            panel = self.data.get_panel_connection(plan_data["panel_id"])
            if not panel or panel["provider"] not in {"pasarguard", "rebecca"} or not panel["enabled"]:
                await self.api.send_text(chat_id, "این پنل نمایندگی را پشتیبانی نمی‌کند یا اتصال آن فعال نیست؛ مبلغی کسر نشد.", BACK_MENU); return
            try:
                if panel["provider"] == "pasarguard":
                    try: role_id = int(plan_data["provider_role_id"])
                    except (TypeError, ValueError): raise CustomerStoreError("پلن PasarGuard فاقد role ID معتبر است") from None
                    if not plan_data["provider_role_id"]: raise CustomerStoreError("پلن PasarGuard فاقد role ID است")
                else:
                    role_id = None
                try: mode = PricingMode(plan_data["pricing_mode"])
                except ValueError: raise CustomerStoreError("این نوع پلن از خرید خودکار پشتیبانی نمی‌شود") from None
                if mode not in {PricingMode.FIXED, PricingMode.UNLIMITED, PricingMode.HOURLY, PricingMode.USAGE}:
                    raise CustomerStoreError("این نوع پلن از خرید خودکار پشتیبانی نمی‌شود")
                overrides = plan_data.get("permission_overrides") or {}
                plan = ResellerPlan(plan_id=plan_data["plan_id"], panel_id=plan_data["panel_id"],
                    pricing_mode=mode, price=Decimal(int(plan_data["price_minor"])),
                    unit_price=Decimal(int(plan_data["unit_price_minor"])),
                    min_volume_gb=Decimal(plan_data["min_volume_gib"]),
                    max_volume_gb=Decimal(plan_data["max_volume_gib"]),
                    volume_step_gb=Decimal(plan_data["volume_step_gib"]),
                    data_limit_bytes=int(plan_data["data_limit_bytes"]), max_users=int(plan_data["max_users"]),
                    duration_days=(int(plan_data["duration_hours"]) + 23) // 24,
                    duration_hours=int(plan_data["duration_hours"]),
                    provider_role_id=role_id, allowed_group_ids=tuple(plan_data.get("allowed_group_ids") or []),
                    permission_overrides=tuple((str(k), v) for k,v in overrides.items()),
                    enabled=bool(plan_data["enabled"]), currency=plan_data["currency"])
                message_id = message.get("message_id") or 0
                idempotency_key = f"reseller-purchase:{uid}:{chat_id}:{message_id}:{plan_id}"
                username = f"r{uid}_{uuid.uuid4().hex[:8]}"
                purchase_service = self.reseller_purchase_service
                result = await purchase_service.purchase(owner_id=str(uid), plan=plan,
                    username=username, idempotency_key=idempotency_key)
                body = f"نمایندگی ایجاد شد.\nنام کاربری: {result.account.username}\nپنل: {panel['base_url']}\nمدل: {mode.value}\nهزینه اولیه: {int(result.charged_amount):,} {result.currency}\nشناسه نمایندگی: {result.account.account_id}"
                if result.initial_password:
                    body += f"\nرمز اولیه (فقط همین بار نمایش داده می‌شود): {result.initial_password}"
                else:
                    body += "\nاین سفارش قبلاً ثبت شده؛ برای تغییر رمز با مدیر تماس بگیر."
                await self._emit_trust_event(TrustEvent.RESELLER_CREATED,
                    {"public_order_id": result.account.account_id[:20], "amount_minor": int(result.charged_amount),
                     "currency": result.currency}, f"reseller:{result.account.account_id}")
                await self.api.send_text(chat_id, body, BACK_MENU)
            except (CustomerStoreError, PurchaseError, ValueError) as exc:
                await self.api.send_text(chat_id, f"خرید نمایندگی انجام نشد یا نیازمند بررسی است: {exc}", BACK_MENU)
            return
        if key == "trust":
            config = self.data.get_trust_channel_config(self.data.tenant_id, self.bot_instance_id)
            if not config or not config.enabled:
                await self.api.send_text(chat_id, "📣 کانال اعتماد هنوز توسط مدیر تنظیم یا فعال نشده است.", BACK_MENU)
            else:
                target = config.chat_id if config.chat_id.startswith("@") else "کانال فعال است"
                await self.api.send_text(chat_id, f"📣 کانال اعتماد فعال است: {target}\nاطلاعیه‌ها بدون لینک اشتراک خصوصی، رمز یا توکن ارسال می‌شوند.", BACK_MENU)
            return
        if key == "services" or key.startswith("services:"):
            try:
                page = max(0, min(9999, int(key.partition(":")[2] or "0")))
            except ValueError:
                page = 0
            await self._send_customer_services(chat_id, uid, page=page)
            return
        if key.startswith("resume:"):
            service_id = key.partition(":")[2]
            try:
                worker = CustomerBillingWorker(self.data, secret_resolver=self._resolve_panel_secret, registry=self.registry)
                await worker.resume(service_id=service_id, telegram_id=uid, now=int(datetime.now(timezone.utc).timestamp()))
                await self.api.send_text(chat_id, "✅ سرویس فعال شد و بدهی معوق از کیف پول تسویه شد.", BACK_MENU)
            except CustomerStoreError as exc:
                await self.api.send_text(chat_id, f"فعال‌سازی انجام نشد: {str(exc)[:200]}", BACK_MENU)
            except Exception as exc:
                await self.api.send_text(chat_id, f"فعال‌سازی با خطای ارائه‌دهنده روبه‌رو شد ({type(exc).__name__}).", BACK_MENU)
            return
        if key == "wallet":
            self.data.ensure_customer(uid, user.get("username", ""), user.get("first_name", ""))
            balances = self.data.wallet_balances(uid)
            debts = self.data.outstanding_debts(uid)
            balance_lines = [f"• {currency}: {amount:,}" for currency, amount in balances.items()] or ["• IRR: 0"]
            debt_lines = [f"• {currency}: {amount:,}" for currency, amount in debts.items()] or ["• بدون بدهی معوق"]
            await self.api.send_text(chat_id, "💳 موجودی کیف پول:\n" + "\n".join(balance_lines) +
                "\n\n⏳ بدهی ساعتی/مصرفی:\n" + "\n".join(debt_lines) +
                "\n\nدرگاه خودکار متصل نیست؛ شارژ دستی فقط پس از بررسی و تأیید مدیر به موجودی اضافه می‌شود.",
                {"inline_keyboard": [
                    [{"text": "➕ شارژ دستی کیف پول", "callback_data": "customer:wallet:deposit"}],
                    [{"text": "📜 گردش حساب", "callback_data": "customer:wallet:history"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
                ]})
            return
        if key == "wallet:deposit":
            details = self.data.get_manual_deposit_details()
            if not details:
                await self.api.send_text(chat_id,
                    "فعلاً اطلاعات کارت‌به‌کارت توسط مدیر تنظیم نشده است. پیش از واریز با پشتیبانی هماهنگ کن.",
                    {"inline_keyboard": [
                        [{"text": "🎫 پشتیبانی", "callback_data": "customer:support"}],
                        [{"text": "⬅️ بازگشت به کیف پول", "callback_data": "customer:wallet"}],
                    ]})
                return
            await self.api.send_text(chat_id,
                f"➕ شارژ دستی کیف پول\n\nبانک: {details['bank_name'] or '—'}\n"
                f"شماره کارت: {details['card_number']}\nبه نام: {details['account_holder']}\n\n"
                "برای ساخت درخواست شارژ، مبلغ را به کوچک‌ترین واحد ارز وارد کن؛ برای ریال نمونه: /deposit 250000 IRR\n"
                "پس از ساخت درخواست، شناسه پرداخت و دستور ثبت رسید نمایش داده می‌شود. موجودی فقط بعد از بررسی مدیر افزایش می‌یابد.",
                {"inline_keyboard": [
                    [{"text": "📜 گردش حساب", "callback_data": "customer:wallet:history"}],
                    [{"text": "⬅️ بازگشت به کیف پول", "callback_data": "customer:wallet"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
                ]})
            return
        if key == "wallet:history":
            entries = self.data.list_wallet_transactions(uid, limit=10)
            labels = {
                "deposit": "واریز", "deposit_verified": "شارژ تأییدشده", "purchase": "خرید",
                "refund": "بازپرداخت", "debit": "برداشت", "adjustment": "اصلاح موجودی",
                "service_charge": "هزینه سرویس", "renewal": "تمدید", "traffic_topup": "افزایش حجم",
            }
            lines = ["📜 گردش حساب کیف پول (۱۰ تراکنش اخیر)"]
            for entry in entries:
                amount = int(entry["amount_minor"])
                sign = "+" if amount > 0 else ""
                lines.append(f"\n• {labels.get(entry['entry_type'], 'تراکنش')} | {sign}{amount:,} {entry['currency']}\n"
                             f"شناسه: {entry['entry_id']} | زمان: {entry['created_at']}")
            if not entries:
                lines.append("\nهنوز تراکنشی برای این حساب ثبت نشده است.")
            await self.api.send_text(chat_id, "\n".join(lines), {"inline_keyboard": [
                [{"text": "➕ شارژ دستی کیف پول", "callback_data": "customer:wallet:deposit"}],
                [{"text": "⬅️ بازگشت به کیف پول", "callback_data": "customer:wallet"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "customer:home"}],
            ]})
            return
        if key in PAGES:
            title, description = PAGES[key]
            body = content_value(self.content, f"page_{key}", f"{title}\n\n{description}")
            if key == "wallet":
                self.data.ensure_customer(uid, user.get("username", ""), user.get("first_name", ""))
                balances = self.data.wallet_balances(uid)
                debts = self.data.outstanding_debts(uid)
                body = f"{body}\n\nموجودی دفترکل:\n" + ("\n".join(f"• {c}: {v:,}" for c, v in balances.items()) or "• IRR: 0")
                body += "\nبدهی ساعتی/مصرفی:\n" + ("\n".join(f"• {c}: {v:,}" for c, v in debts.items()) or "• بدون بدهی معوق")
            await self.api.send_text(chat_id, body, BACK_MENU)
        else:
            await self.api.send_text(chat_id, "این گزینه در نسخه نمایشی تعریف نشده است.", self._customer_menu())

    async def run(self) -> None:
        webhook = await self.api.call("getWebhookInfo")
        if webhook.get("url"):
            LOG.error("Customer bot webhook is configured; refusing to start polling and leaving it unchanged.")
            raise SystemExit("Customer bot webhook is configured; polling not started")
        me = await self.api.get_me()
        LOG.info("Customer preview authenticated; bot_id=%s username=%s; admin_count=%d",
                 me.get("id"), me.get("username"), len(self.config.admin_ids))
        while True:
            try:
                updates = await self.api.get_updates(self._offset)
                for update in updates:
                    self._offset = int(update["update_id"]) + 1
                    try:
                        if update.get("message"):
                            await self._handle_message(update["message"])
                        elif update.get("callback_query"):
                            await self._handle_callback(update["callback_query"])
                    except TelegramAPIError as exc:
                        LOG.warning("Customer Telegram operation failed (%s)", type(exc).__name__)
                try:
                    await self._deliver_manual_delivery_notifications(limit=20)
                except Exception as exc:
                    LOG.warning("Manual delivery notification worker failed (%s)", type(exc).__name__)
                try:
                    await self._deliver_customer_ticket_notifications(limit=20)
                except Exception as exc:
                    LOG.warning("Support ticket notification worker failed (%s)", type(exc).__name__)
                try:
                    await self._deliver_admin_notifications(limit=20)
                except Exception as exc:
                    LOG.warning("Admin notification worker failed (%s)", type(exc).__name__)
            except TelegramAPIError as exc:
                LOG.warning("Customer polling failed (%s); retrying", type(exc).__name__)
                await asyncio.sleep(3)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if os.environ.get("MANUBISGUARD_TEST_MODE", "").strip().lower() not in {"1", "true", "yes"}:
        raise SystemExit("This entrypoint is staging-only; explicitly set MANUBISGUARD_TEST_MODE=1")
    try:
        asyncio.run(CustomerBot(CustomerBotConfig.from_env()).run())
    except KeyboardInterrupt:
        LOG.info("Customer preview stopped by operator")


if __name__ == "__main__":
    main()
