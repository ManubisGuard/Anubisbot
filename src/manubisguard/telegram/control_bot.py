"""Staging prototype of the Anubis Bot Creatore Telegram UI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from manubisguard.telegram.content_admin import (
    begin_content_edit, content_admin_keyboard, content_value, save_pending_content,
)
from manubisguard.telegram.content_store import BotContentStore

LOG = logging.getLogger("manubisguard.telegram.control_bot")
API_ROOT = "https://api.telegram.org/bot{token}/{method}"


class TelegramAPIError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BotConfig:
    token: str = field(repr=False)
    admin_ids: frozenset[int]
    poll_timeout: int = 25
    request_timeout: float = 35.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("MANUBISGUARD_BOT_TOKEN", "").strip()
        raw_ids = os.environ.get("MANUBISGUARD_ADMIN_IDS", "").strip()
        if not token:
            raise ValueError("MANUBISGUARD_BOT_TOKEN is required")
        if not raw_ids:
            raise ValueError("MANUBISGUARD_ADMIN_IDS is required")
        try:
            admin_ids = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        except ValueError:
            raise ValueError("MANUBISGUARD_ADMIN_IDS must contain numeric IDs") from None
        if not admin_ids or any(x <= 0 for x in admin_ids):
            raise ValueError("At least one positive numeric admin ID is required")
        return cls(token=token, admin_ids=admin_ids)


class TelegramBotAPI:
    def __init__(self, config: BotConfig):
        self._config = config

    def _request_sync(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = API_ROOT.format(token=self._config.token, method=method)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "ManubisGuard-Preview/0.2"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.request_timeout) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise TelegramAPIError(f"Telegram HTTP error ({exc.code}) for {method}") from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramAPIError(f"Telegram network/response error for {method}: {type(exc).__name__}") from None
        if not result.get("ok"):
            raise TelegramAPIError(f"Telegram API rejected {method} (code {result.get('error_code', 'unknown')})")
        return result.get("result")

    def _upload_sync(self, method: str, payload: dict[str, Any], filename: str,
                     content: bytes, content_type: str, file_field: str = "document") -> Any:
        boundary = "----ManubisGuardUploadBoundary"
        parts: list[bytes] = []
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"), b"\r\n",
            ])
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(), content, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            API_ROOT.format(token=self._config.token, method=method),
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": "ManubisGuard-Preview/0.2"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.request_timeout) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise TelegramAPIError(f"Telegram HTTP error ({exc.code}) for {method}") from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramAPIError(f"Telegram network/response error for {method}: {type(exc).__name__}") from None
        if not result.get("ok"):
            raise TelegramAPIError(f"Telegram API rejected {method} (code {result.get('error_code', 'unknown')})")
        return result.get("result")

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request_sync, method, payload)

    async def send_document_bytes(self, chat_id: int, filename: str, content: bytes,
                                  content_type: str = "application/octet-stream",
                                  caption: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "protect_content": True}
        if caption:
            payload["caption"] = caption[:1024]
        await asyncio.to_thread(self._upload_sync, "sendDocument", payload, filename, content, content_type)

    async def send_photo_bytes(self, chat_id: int, filename: str, content: bytes,
                               caption: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "protect_content": True}
        if caption:
            payload["caption"] = caption[:1024]
        await asyncio.to_thread(self._upload_sync, "sendPhoto", payload, filename, content, "image/png", "photo")

    async def send_text(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "protect_content": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        await self.call("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": self._config.poll_timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload)


MAIN_MENU = {
    "inline_keyboard": [
        [{"text": "＋ ساخت ربات جدید", "callback_data": "bot:create", "style": "success"}],
        [{"text": "🤖 ربات‌های من", "callback_data": "page:bots", "style": "primary"},
         {"text": "🖥 پنل گرافیکی", "web_app": {"url": "https://Bott.qoqnusradio.top/"}, "style": "primary"}],
        [{"text": "📘 راهنمای راه‌اندازی", "callback_data": "page:guide"},
         {"text": "⚙️ تنظیمات سازنده", "callback_data": "page:settings"}],
        [{"text": "🔄 وضعیت سرویس", "callback_data": "page:dashboard"}],
        [{"text": "📝 مدیریت متن‌ها و لوگو", "callback_data": "content:open", "style": "primary"}],
    ]
}
BACK_MENU = {"inline_keyboard": [[{"text": "⬅️ منوی اصلی", "callback_data": "page:home", "style": "primary"}]]}
PAGES = {
    "dashboard": (
        "🛠 وضعیت Anubis Bot Creatore\n\n"
        "🟡 ساخت خودکار نمونه‌ها: در حال توسعه\n"
        "🔐 ایزوله‌سازی هر مشتری: نیازمند تکمیل و تست\n"
        "🧩 تخصیص پورت و سرویس: هنوز فعال نشده\n\n"
        "این ربات فقط سازنده و مدیر نمونه‌های مشتری است. فروش، اتصال پنل و نمایندگی‌ها داخل ربات مستقل هر مشتری قرار می‌گیرد."
    ),
    "bots": (
        "🤖 ربات‌های مشتری\n\n"
        "اینجا فقط نمونه‌های مستقلی که با ربات‌ساز ایجاد می‌شوند فهرست خواهند شد. "
        "هر مشتری باید سرویس، داده‌ها، اسرار و پورت‌های اختصاصی و چرخه نصب و به‌روزرسانی جدا داشته باشد.\n\n"
        "وضعیت فعلی: فهرست پایدار و provisioning هنوز متصل نشده؛ هیچ رباتی از این صفحه ساخته نشده است."
    ),
    "guide": (
        "📘 راهنمای ساخت ربات مستقل\n\n"
        "۱. از طریق @BotFather یک ربات جدید بساز و توکن آن را دریافت کن.\n"
        "۲. شناسه عددی مدیر اصلی همان ربات را مشخص کن.\n"
        "۳. در نسخه عملیاتی، توکن از مسیر امن دریافت و در سمت سرور نگهداری می‌شود.\n"
        "۴. ربات‌ساز یک محیط مستقل با تنظیمات، داده‌ها، اسرار و پورت‌های جدا ایجاد می‌کند.\n\n"
        "⚠️ این نمونه آزمایشی هنوز توکن نمی‌گیرد و نصب خودکار انجام نمی‌دهد. توکن واقعی را در این نسخه ارسال نکن."
    ),
    "design": (
        "🎨 هویت بصری Anubis Bot Creatore\n\n"
        "• تم تیره گرافیتی و سطوح خنثی\n"
        "• سبز زمردی برای تأکیدها\n"
        "• دکمه‌های رنگی native تلگرام برای اقدام اصلی، تأیید و عملیات حساس\n"
        "• هویت مینیمال و هندسی؛ بدون افکت نئونی سنگین\n\n"
        "رابط گرافیکی این ربات‌ساز فقط مدیریت ساخت نمونه‌ها را پوشش می‌دهد؛ امکانات فروش و پنل در ربات مستقل مشتری قرار می‌گیرند."
    ),
    "settings": (
        "⚙️ تنظیمات سازنده\n\n"
        "بخش‌های هدف: پیش‌فرض نصب، محدوده پورت‌ها، نسخه محصول، دامنه، سیاست بکاپ و گزارش رویدادها.\n\n"
        "وضعیت فعلی: صفحه نمایشی؛ ذخیره تنظیمات و provisioning هنوز پیاده‌سازی نشده است."
    ),
}


class ControlBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.api = TelegramBotAPI(config)
        self._offset: int | None = None
        self._pending_content: dict[int, str] = {}
        self.content = BotContentStore(
            os.environ.get("ANUBIS_MOTHER_CONTENT_NAMESPACE", "mother"),
            {
                "title": "ANUBIS BOT CREATORE",
                "welcome_text": "این ربات برای ساخت و مدیریت ربات‌های مستقل مشتریان است. اتصال پنل، فروش، نمایندگی و پرداخت در ربات دوم هر مشتری انجام می‌شود.",
                "logo_file_id": "", "logo_url": "",
                "menu_create": "＋ ساخت ربات جدید", "menu_bots": "🤖 ربات‌های من",
                "menu_miniapp": "🖥 پنل گرافیکی", "menu_guide": "📘 راهنمای راه‌اندازی",
                "menu_factory_settings": "⚙️ تنظیمات سازنده", "menu_status": "🔄 وضعیت سرویس",
                "page_dashboard": PAGES["dashboard"], "page_bots": PAGES["bots"],
                "page_guide": PAGES["guide"], "page_design": PAGES["design"],
                "page_settings": PAGES["settings"],
                "webapp_eyebrow": "ISOLATED CUSTOMER BOT FACTORY",
                "webapp_headline": "مدیریت ربات‌های مستقل",
                "webapp_subtitle": "هر مشتری یک ربات جدا؛ پنل فروش و نمایندگی داخل ربات خودش.",
                "webapp_new_bot": "＋ ساخت ربات",
                "webapp_nav_dashboard": "داشبورد", "webapp_nav_bots": "ربات‌های من",
                "webapp_nav_guide": "راهنما", "webapp_nav_settings": "تنظیمات",
                "webapp_metric_instances": "نمونه‌های ثبت‌شده",
                "webapp_metric_instances_hint": "پیش‌نمایش محلی این صفحه",
                "webapp_metric_isolation": "ایزوله‌سازی", "webapp_metric_isolation_value": "در حال ساخت",
                "webapp_metric_isolation_hint": "پورت، داده و اسرار جدا باید تست شوند",
                "webapp_metric_provisioning": "Provisioning", "webapp_metric_provisioning_value": "غیرفعال",
                "webapp_metric_provisioning_hint": "هنوز هیچ سروری ساخته نمی‌شود",
                "webapp_steps_title": "مراحل ساخت هر ربات", "webapp_steps_hint": "جریان هدف محصول",
                "webapp_step_token_title": "توکن BotFather",
                "webapp_step_token_body": "توکن رباتی که کاربر خودش ساخته؛ دریافت امن در نسخه عملیاتی.",
                "webapp_step_admin_title": "شناسه مدیر",
                "webapp_step_admin_body": "ثبت شناسه عددی مدیر ربات مشتری و اعتبارسنجی دسترسی.",
                "webapp_step_isolation_title": "محیط ایزوله",
                "webapp_step_isolation_body": "سرویس، داده، اسرار و پورت اختصاصی با کنترل تداخل.",
                "webapp_step_install_title": "نصب و راه‌اندازی",
                "webapp_step_install_body": "بررسی سلامت، گزارش خطا و امکان بازگشت امن.",
                "webapp_bots_title": "ربات‌های ساخته‌شده",
                "webapp_bots_empty_title": "هنوز رباتی ثبت نشده",
                "webapp_bots_empty_body": "پس از ساخت، ربات‌های مستقل اینجا نمایش داده می‌شوند.",
                "webapp_guide_title": "راهنمای ساخت ربات",
                "webapp_settings_title": "تنظیمات سازنده",
            },
        )
        self.content.defaults.update({
            "webapp_status_badge": "محیط آزمایشی",
            "webapp_notice_title": "پیش‌نمایش بدون عملیات واقعی",
            "webapp_notice_body": "ساخت ربات، دریافت توکن، نصب سرور و ذخیره‌سازی هنوز به بک‌اند امن وصل نشده‌اند. در این نسخه توکن واقعی وارد نکن.",
            "webapp_bots_subtitle": "هر ردیف نمونه محلی است و به تلگرام یا سرور وصل نشده.",
            "webapp_guide_subtitle": "ساخت امن یک نمونه مستقل برای هر مشتری.",
            "webapp_guide_step1_title": "ساخت ربات در @BotFather",
            "webapp_guide_step1_body": "مالک ربات، توکن را از BotFather دریافت می‌کند؛ توکن نباید در لاگ یا رابط کاربری بازتاب داده شود.",
            "webapp_guide_step2_title": "شناسه عددی مدیر",
            "webapp_guide_step2_body": "ربات‌ساز باید مالکیت و دسترسی را ثبت کند؛ این شناسه با API key پنل VPN فرق دارد.",
            "webapp_guide_step3_title": "نصب و جداسازی",
            "webapp_guide_step3_body": "برای هر مشتری مسیر داده، اسرار، runtime، لاگ‌ها و پورت‌های مجزا با کنترل collision لازم است.",
            "webapp_guide_step4_title": "تنظیمات محصول داخل ربات دوم",
            "webapp_guide_step4_body": "اتصال PasarGuard/پنل‌های دیگر، محصولات، کاربران، نمایندگی، پرداخت و کانال اعتماد فقط در ربات مشتری قرار می‌گیرند.",
            "webapp_guide_notice_title": "محدودیت نسخه تست",
            "webapp_guide_notice_body": "فعلاً این راهنما و فرم نمایشی هستند؛ provisioning واقعی هنوز پیاده نشده است.",
            "webapp_settings_subtitle": "تنظیمات سراسری ربات‌ساز؛ نه تنظیمات پنل مشتری.",
            "webapp_settings_theme_title": "قالب پیش‌فرض", "webapp_settings_theme_value": "Graphite / Emerald",
            "webapp_settings_port_title": "سیاست پورت", "webapp_settings_port_body": "تخصیص خودکار با بررسی تداخل",
            "webapp_settings_port_status": "پیاده‌سازی نشده",
            "webapp_settings_secrets_title": "نگهداری اسرار", "webapp_settings_secrets_body": "مدیریت امن توکن و کلیدها سمت سرور",
            "webapp_settings_secrets_status": "پیاده‌سازی نشده",
            "webapp_settings_backup_title": "بکاپ و بازیابی", "webapp_settings_backup_body": "هر نمونه مستقل و قابل بازیابی",
            "webapp_settings_backup_status": "پیاده‌سازی نشده",
            "webapp_modal_title": "ساخت ربات مستقل",
            "webapp_modal_intro": "این فرم فقط پیش‌نمایش رابط است و هیچ توکن یا داده‌ای به سرور نمی‌فرستد.",
            "webapp_form_name_label": "نام نمایشی", "webapp_form_username_label": "شناسه ربات (اختیاری)",
            "webapp_form_admin_label": "شناسه عددی مدیر", "webapp_form_theme_label": "قالب محصول",
            "webapp_form_submit": "ساخت پیش‌نمایش", "webapp_form_cancel": "انصراف",
            "webapp_token_notice_title": "توکن در این نسخه دریافت نمی‌شود",
            "webapp_token_notice_body": "در نسخه عملیاتی، توکن BotFather باید فقط از مسیر امن به بک‌اند برسد و در متن، لاگ یا ذخیره‌سازی مرورگر باقی نماند.",
        })
        self.content.defaults.update({
            "webapp_status_badge": 'محیط آزمایشی',
            "webapp_notice_title": 'پیش\u200cنمایش بدون عملیات واقعی',
            "webapp_notice_body": 'ساخت ربات، دریافت توکن، نصب سرور و ذخیره\u200cسازی هنوز به بک\u200cاند امن وصل نشده\u200cاند. در این نسخه توکن واقعی وارد نکن.',
            "webapp_bots_subtitle": 'هر ردیف نمونه محلی است و به تلگرام یا سرور وصل نشده.',
            "webapp_guide_subtitle": 'ساخت امن یک نمونه مستقل برای هر مشتری.',
            "webapp_guide_step1_title": 'ساخت ربات در @BotFather',
            "webapp_guide_step1_body": 'مالک ربات، توکن را از BotFather دریافت می\u200cکند؛ توکن نباید در لاگ یا رابط کاربری بازتاب داده شود.',
            "webapp_guide_step2_title": 'شناسه عددی مدیر',
            "webapp_guide_step2_body": 'ربات\u200cساز باید مالکیت و دسترسی را ثبت کند؛ این شناسه با API key پنل VPN فرق دارد.',
            "webapp_guide_step3_title": 'نصب و جداسازی',
            "webapp_guide_step3_body": 'برای هر مشتری مسیر داده، اسرار، runtime، لاگ\u200cها و پورت\u200cهای مجزا با کنترل collision لازم است.',
            "webapp_guide_step4_title": 'تنظیمات محصول داخل ربات دوم',
            "webapp_guide_step4_body": 'اتصال PasarGuard/پنل\u200cهای دیگر، محصولات، کاربران، نمایندگی، پرداخت و کانال اعتماد فقط در ربات مشتری قرار می\u200cگیرند.',
            "webapp_guide_notice_title": 'محدودیت نسخه تست',
            "webapp_guide_notice_body": 'فعلاً این راهنما و فرم نمایشی هستند؛ provisioning واقعی هنوز پیاده نشده است.',
            "webapp_settings_subtitle": 'تنظیمات سراسری ربات\u200cساز؛ نه تنظیمات پنل مشتری.',
            "webapp_settings_theme_title": 'قالب پیش\u200cفرض',
            "webapp_settings_theme_value": 'Graphite / Emerald',
            "webapp_settings_port_title": 'سیاست پورت',
            "webapp_settings_port_body": 'تخصیص خودکار با بررسی تداخل',
            "webapp_settings_port_status": 'پیاده\u200cسازی نشده',
            "webapp_settings_secrets_title": 'نگهداری اسرار',
            "webapp_settings_secrets_body": 'مدیریت امن توکن و کلیدها سمت سرور',
            "webapp_settings_secrets_status": 'پیاده\u200cسازی نشده',
            "webapp_settings_backup_title": 'بکاپ و بازیابی',
            "webapp_settings_backup_body": 'هر نمونه مستقل و قابل بازیابی',
            "webapp_settings_backup_status": 'پیاده\u200cسازی نشده',
            "webapp_modal_title": 'ساخت ربات مستقل',
            "webapp_modal_intro": 'این فرم فقط پیش\u200cنمایش رابط است و هیچ توکن یا داده\u200cای به سرور نمی\u200cفرستد.',
            "webapp_form_name_label": 'نام نمایشی',
            "webapp_form_username_label": 'شناسه ربات (اختیاری)',
            "webapp_form_admin_label": 'شناسه عددی مدیر',
            "webapp_form_theme_label": 'قالب محصول',
            "webapp_form_submit": 'ساخت پیش\u200cنمایش',
            "webapp_form_cancel": 'انصراف',
            "webapp_token_notice_title": 'توکن در این نسخه دریافت نمی\u200cشود',
            "webapp_token_notice_body": 'در نسخه عملیاتی، توکن BotFather باید فقط از مسیر امن به بک\u200cاند برسد و در متن، لاگ یا ذخیره\u200cسازی مرورگر باقی نماند.',
        })

    def _main_menu(self) -> dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": content_value(self.content, "menu_create", "＋ ساخت ربات جدید"), "callback_data": "bot:create", "style": "success"}],
            [{"text": content_value(self.content, "menu_bots", "🤖 ربات‌های من"), "callback_data": "page:bots", "style": "primary"},
             {"text": content_value(self.content, "menu_miniapp", "🖥 پنل گرافیکی"), "web_app": {"url": "https://Bott.qoqnusradio.top/"}, "style": "primary"}],
            [{"text": content_value(self.content, "menu_guide", "📘 راهنمای راه‌اندازی"), "callback_data": "page:guide"},
             {"text": content_value(self.content, "menu_factory_settings", "⚙️ تنظیمات سازنده"), "callback_data": "page:settings"}],
            [{"text": content_value(self.content, "menu_status", "🔄 وضعیت سرویس"), "callback_data": "page:dashboard"}],
            [{"text": "📝 مدیریت متن‌ها و لوگو", "callback_data": "content:open", "style": "primary"}],
        ]}

    async def _send_brand_message(self, chat_id: int, keyboard: dict[str, Any]) -> None:
        logo = content_value(self.content, "logo_file_id")
        if logo:
            try:
                await self.api.call("sendPhoto", {"chat_id": chat_id, "photo": logo, "protect_content": True})
            except TelegramAPIError:
                LOG.warning("Could not send configured bot logo")
        title = content_value(self.content, "title", "Anubis Bot Creatore")
        welcome = content_value(self.content, "welcome_text", "این ربات برای ساخت ربات‌های مستقل مشتریان است.")
        await self.api.send_text(chat_id, f"🛡 {title}\n\n{welcome}", keyboard)

    def _authorized(self, user: dict[str, Any] | None, chat: dict[str, Any] | None) -> bool:
        user = user or {}
        chat = chat or {}
        uid = user.get("id")
        return isinstance(uid, int) and uid in self.config.admin_ids and chat.get("type") == "private" and chat.get("id") == uid

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or not self._authorized(message.get("from"), chat):
            return
        pending_reply = save_pending_content(self.content, self._pending_content, chat_id, message)
        if pending_reply is not None:
            await self.api.send_text(chat_id, pending_reply, content_admin_keyboard("mother"))
            return
        text = (message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        LOG.info("Authorized admin command received: %s", command or "[non-text]")
        if command in ("/start", "/help"):
            await self._send_brand_message(chat_id, self._main_menu())
        elif command == "/status":
            await self.api.send_text(chat_id, "🟡 پیش‌نمایش در حال اجراست؛ پنل‌های واقعی متصل نیستند.", self._main_menu())
        elif command == "/health":
            try:
                me = await self.api.get_me()
                await self.api.send_text(chat_id, f"✅ ارتباط تلگرام برقرار است.\nربات: @{me.get('username', 'unknown')}", self._main_menu())
            except TelegramAPIError:
                await self.api.send_text(chat_id, "⚠️ بررسی ارتباط تلگرام ناموفق بود.", self._main_menu())
        elif command == "/todo":
            await self.api.send_text(chat_id, "گزارش توسعه: /root/fork/ManubisGuard-MultiPanel-Core/TODO.md", self._main_menu())
        else:
            await self.api.send_text(chat_id, "از دکمه‌های منو استفاده کن یا /start را بفرست.", self._main_menu())

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        callback_id = query.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        if not self._authorized(user, chat):
            if callback_id:
                await self.api.answer_callback(callback_id, "دسترسی مجاز نیست")
            return
        data = query.get("data", "")
        page = data.partition(":")[2] if data.startswith("page:") else ""
        if callback_id:
            await self.api.answer_callback(callback_id)
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        if data == "content:open":
            await self.api.send_text(chat_id, "📝 مدیریت محتوای ربات مادر\nعنوان، متن‌ها، برچسب دکمه‌ها و لوگو را از اینجا ویرایش کن.", content_admin_keyboard("mother"))
        elif data.startswith("content:edit:"):
            prompt = begin_content_edit(data, self._pending_content, chat_id)
            await self.api.send_text(chat_id, prompt or "این فیلد قابل ویرایش نیست.", content_admin_keyboard("mother"))
        elif data == "content:back":
            await self._send_brand_message(chat_id, self._main_menu())
        elif data == "bot:create":
            await self.api.send_text(
                chat_id,
                "＋ ساخت ربات مشتری\\n\\n"
                "برای هر مشتری باید یک توکن جدا از @BotFather و شناسه عددی مدیر مشخص شود. "
                "بعد از اعتبارسنجی، ربات‌ساز باید نمونه‌ای مستقل با پورت و داده‌های جدا نصب کند.\\n\\n"
                "⚠️ این محیط هنوز به provisioning امن متصل نیست؛ فعلاً توکن واقعی نفرست. این دکمه فقط جریان ساخت را نمایش می‌دهد.",
                BACK_MENU,
            )
        elif page == "home":
            await self._send_brand_message(chat_id, self._main_menu())
        elif page in PAGES:
            await self.api.send_text(chat_id, content_value(self.content, f"page_{page}", PAGES[page]), BACK_MENU)
        else:
            await self.api.send_text(chat_id, "این گزینه در پیش‌نمایش تعریف نشده است.", self._main_menu())

    async def run(self) -> None:
        webhook = await self.api.call("getWebhookInfo")
        if webhook.get("url"):
            LOG.error("Webhook is configured; refusing to start polling and leaving it unchanged.")
            raise SystemExit("Webhook is configured; polling not started")
        me = await self.api.get_me()
        LOG.info("Anubis Bot Creatore authenticated; bot_id=%s username=%s; admin_count=%d", me.get("id"), me.get("username"), len(self.config.admin_ids))
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
                        LOG.warning("Telegram operation failed: %s", str(exc))
            except TelegramAPIError as exc:
                LOG.warning("Polling failed: %s; retrying", str(exc))
                await asyncio.sleep(3)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = BotConfig.from_env()
    if os.environ.get("MANUBISGUARD_TEST_MODE", "1").lower() not in {"1", "true", "yes"}:
        raise SystemExit("This entrypoint is staging-only; set MANUBISGUARD_TEST_MODE=1")
    try:
        asyncio.run(ControlBot(config).run())
    except KeyboardInterrupt:
        LOG.info("Anubis Bot Creatore stopped by operator")


if __name__ == "__main__":
    main()
