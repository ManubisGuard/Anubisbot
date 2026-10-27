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
    "/wallet-credit", "/payment-events", "/payment-notification-retry", "/trust-channel", "/trust-channel-status",
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
        self._pending_panel_registration: dict[int, dict[str, Any]] = {}
        self._pending_category_admin: dict[int, dict[str, Any]] = {}
        self._pending_product_admin: dict[int, dict[str, Any]] = {}
        self._pending_receipt_reject: dict[int, dict[str, Any]] = {}
        self._receipt_callback_refs: dict[str, str] = {}
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

    async def _send_purchase_error(self, chat_id: int, exc: Exception, *, title: str = "خرید انجام نشد") -> None:
        detail = str(exc)
        normalized = detail.casefold()
        insufficient_balance = (
            "wallet balance" in normalized
            and any(marker in normalized for marker in (
                "insufficient", "positive wallet balance", "needs at least",
            ))
        )
        if insufficient_balance:
            await self.api.send_text(chat_id,
                "موجودی کیف پول برای این خرید کافی نیست. ابتدا کیف پول را شارژ کن؛ "
                "ثبت درخواست کارت‌به‌کارت به‌تنهایی موجودی را افزایش نمی‌دهد و تأیید مدیر لازم است.",
                {"inline_keyboard": [
                    [{"text": "➕ شارژ دستی کیف پول", "callback_data": "customer:wallet:deposit", "style": "primary"}],
                    [{"text": "💳 مشاهده موجودی کیف پول", "callback_data": "customer:wallet"}],
                    [{"text": "🛍 بازگشت به فروشگاه", "callback_data": "customer:shop"}],
                ]})
            return
        await self.api.send_text(chat_id, f"{title}: {detail}", BACK_MENU)

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

    async def _deliver_customer_payment_notifications(self, *, now: float | None = None,
                                                       limit: int = 20) -> dict[str, int]:
        """Drain durable wallet approval/rejection notices; failures remain retryable."""
        counts = {"sent": 0, "retry": 0, "dead": 0}
        for item in self.data.claim_customer_payment_notifications(limit=limit, now=now):
            payment_id = str(item["payment_id"])
            target = int(item["telegram_id"])
            amount = int(item["amount_minor"])
            currency = str(item["currency"])
            if item["event_type"] == "verified":
                body = (f"✅ شارژ کیف پول تأیید شد.\nمبلغ: {amount:,} {currency}\n"
                        f"شناسه پرداخت: {payment_id}\nموجودی جدید را می‌توانی از بخش کیف پول ببینی.")
            else:
                body = (f"❌ رسید واریز تأیید نشد و مبلغی به کیف پول اضافه نشد.\n"
                        f"شناسه پرداخت: {payment_id}\nاگر فکر می‌کنی اشتباهی رخ داده، با پشتیبانی تماس بگیر.")
            try:
                await self.api.send_text(target, body,
                    self._customer_menu(customer_id=target, include_admin=False))
                self.data.mark_customer_payment_notification_sent(int(item["notification_id"]), now=now)
                counts["sent"] += 1
            except Exception as exc:
                try:
                    status = self.data.retry_customer_payment_notification(int(item["notification_id"]),
                        attempt=int(item["attempts"]), error_code=type(exc).__name__.lower(),
                        now=now, max_attempts=8)
                    counts["dead" if status == "dead" else "retry"] += 1
                except Exception as state_exc:
                    LOG.error("Customer payment notification state update failed (%s)", type(state_exc).__name__)
                LOG.warning("Customer payment notification failed for payment %s (%s)",
                            payment_id[:20], type(exc).__name__)
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
        if is_admin and command == "/cancel" and uid in self._pending_panel_registration:
            self._pending_panel_registration.pop(uid, None)
            await self.api.send_text(chat_id, "راه‌اندازی پنل لغو شد؛ هیچ تنظیمی ذخیره نشد.", ADMIN_MENU)
            return
        if is_admin and command == "/cancel" and uid in self._pending_category_admin:
            self._pending_category_admin.pop(uid, None)
            await self.api.send_text(chat_id, "فرم دسته‌بندی لغو شد؛ هیچ تغییری ذخیره نشد.", self._admin_category_keyboard())
            return
        if is_admin and command == "/cancel" and uid in self._pending_product_admin:
            self._pending_product_admin.pop(uid, None)
            await self.api.send_text(chat_id, "فرم محصول لغو شد؛ هیچ تغییری ذخیره نشد.", self._admin_product_keyboard())
            return
        if is_admin and command == "/cancel" and uid in self._pending_receipt_reject:
            self._pending_receipt_reject.pop(uid, None)
            await self.api.send_text(chat_id, "رد رسید لغو شد؛ وضعیت پرداخت تغییر نکرد.", {"inline_keyboard": [[
                {"text": "🧾 بازگشت به رسیدها", "callback_data": "admin:receipt-queue", "style": "primary"}]]})
            return
        receipt_reject_state = self._pending_receipt_reject.get(uid) if is_admin else None
        if receipt_reject_state and text and not text.startswith("/"):
            if time.time() - float(receipt_reject_state.get("started_at", 0)) > 15 * 60:
                self._pending_receipt_reject.pop(uid, None)
                await self.api.send_text(chat_id, "فرم رد رسید منقضی شد؛ پرداخت تغییر نکرد. دوباره از فهرست رسیدها شروع کن.",
                    {"inline_keyboard": [[{"text": "🧾 فهرست رسیدها", "callback_data": "admin:receipt-queue", "style": "primary"}]]})
                return
            reason = text.strip()
            if not reason or len(reason) > 128:
                await self.api.send_text(chat_id, "دلیل رد باید بین ۱ تا ۱۲۸ نویسه باشد. برای لغو /cancel را بفرست.")
                return
            payment_id = str(receipt_reject_state.get("payment_id") or "")
            try:
                receipt = self.data.get_manual_deposit_receipt(payment_id)
                if not receipt or receipt.get("status") != "submitted":
                    raise CustomerStoreError("رسید دیگر در وضعیت انتظار بررسی نیست")
                self.data.reject_manual_deposit_receipt(payment_id=payment_id, admin_id=uid, reason=reason)
                self._pending_receipt_reject.pop(uid, None)
                await self._deliver_customer_payment_notifications(limit=20)
                notice = self.data.get_customer_payment_notification(payment_id, "rejected")
                notice_state = str(notice.get("status") if notice else "missing")
                notice_text = {"sent":"اعلان مشتری ارسال شد", "pending":"اعلان در صف تلاش مجدد است",
                    "processing":"اعلان در حال ارسال است", "dead":"اعلان به سقف تلاش رسیده است"}.get(notice_state, "وضعیت اعلان نیازمند بررسی است")
                await self.api.send_text(chat_id, f"رسید {payment_id} رد شد؛ هیچ مبلغی شارژ نشد. {notice_text}.",
                    {"inline_keyboard": [[{"text": "🧾 فهرست رسیدهای منتظر", "callback_data": "admin:receipt-queue", "style": "primary"}],
                                         [{"text": "⬅️ مدیریت مالی", "callback_data": "admin:finance"}]]})
            except (ValueError, CustomerStoreError) as exc:
                self._pending_receipt_reject.pop(uid, None)
                await self.api.send_text(chat_id, f"رد رسید انجام نشد: {str(exc)[:180]}",
                    {"inline_keyboard": [[{"text": "🧾 بازگشت به رسیدها", "callback_data": "admin:receipt-queue", "style": "primary"}]]})
            return
        product_state = self._pending_product_admin.get(uid) if is_admin else None
        if product_state and product_state.get("step") == "file" and isinstance(message.get("document"), dict):
            await self._handle_product_admin_document(chat_id, uid, message)
            return
        if is_admin and uid in self._pending_panel_registration and text and not text.startswith("/"):
            await self._handle_panel_registration_message(chat_id, uid, text)
            return
        if is_admin and uid in self._pending_category_admin and text and not text.startswith("/"):
            await self._handle_category_admin_message(chat_id, uid, text)
            return
        if is_admin and uid in self._pending_product_admin and text and not text.startswith("/"):
            await self._handle_product_admin_message(chat_id, uid, text)
            return
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
            body, keyboard = self._admin_receipt_queue_screen()
            await self.api.send_text(chat_id, body, keyboard)
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
                await self._deliver_customer_payment_notifications(limit=20)
                customer_notice = self.data.get_customer_payment_notification(payment_id, "rejected")
                notice_state = str(customer_notice.get("status") if customer_notice else "missing")
                notice_text = {"sent":"مشتری مطلع شد", "pending":"اعلان مشتری در صف تلاش مجدد است",
                    "processing":"اعلان مشتری در حال ارسال است", "dead":"اعلان مشتری به سقف تلاش رسیده؛ از /payment-notification-retry استفاده کن"}.get(notice_state, "وضعیت اعلان مشتری نیازمند بررسی است")
                await self.api.send_text(chat_id, f"رسید رد شد؛ هیچ مبلغی به کیف پول اضافه نشد. {notice_text}.", menu)
            except (ValueError, CustomerStoreError) as exc:
                await self.api.send_text(chat_id, f"رد رسید انجام نشد: {exc}", menu)
            return
        if is_admin and command == "/wallet-verify":
            await self._admin_wallet_verify(chat_id, text, uid)
            return
        if is_admin and command == "/wallet-credit":
            await self._admin_wallet_credit(chat_id, text, uid)
            return
        if is_admin and command == "/payment-notification-retry":
            payment_id = text.partition(" ")[2].strip()
            if not payment_id or len(payment_id) > 128 or any(ch.isspace() for ch in payment_id):
                await self.api.send_text(chat_id, "فرمت: /payment-notification-retry PAYMENT_ID", menu)
                return
            if not self.data.retry_customer_payment_notification_now(payment_id):
                await self.api.send_text(chat_id, "اعلان مرده‌ای برای این پرداخت پیدا نشد؛ فقط اعلان‌های dead قابل تلاش مجدد هستند.", menu)
                return
            await self._deliver_customer_payment_notifications(limit=20)
            state = self.data.get_customer_payment_notification(payment_id)
            status = str(state.get("status") if state else "missing")
            await self.api.send_text(chat_id, f"وضعیت اعلان پرداخت {payment_id}: {status}.", menu)
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
                    result = self.data.set_panel_enabled(
                        parts[1], parts[2].lower() == "on", actor_telegram_id=uid,
                        source="telegram_admin_command",
                    )
                    if result["changed"]:
                        message_text = "وضعیت فروش پنل ذخیره و در گزارش مدیریتی ثبت شد؛ محصولات وابسته به پنل خاموش از فروش پنهان می‌شوند."
                    else:
                        message_text = "وضعیت فروش از قبل همین بود؛ تغییری اعمال نشد و رویداد جدیدی ثبت نشد."
                    await self.api.send_text(chat_id, message_text, menu)
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

[executed on device: 8017638839-32c511 (fc69b50d-fe64-47f9-ab6a-705be5de41dc)]