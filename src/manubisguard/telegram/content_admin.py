"""Reusable Telegram admin controls for per-bot editable copy and images."""
from __future__ import annotations

from typing import Any

from manubisguard.telegram.content_store import BotContentStore, ContentStoreError


CONTENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "عنوان ربات"),
    ("welcome_text", "متن خوش‌آمد"),
    ("logo_file_id", "تصویر/لوگوی تلگرام"),
    ("logo_url", "آدرس لوگوی Mini App"),
    ("menu_create", "دکمه ساخت ربات"),
    ("menu_bots", "دکمه ربات‌های من"),
    ("menu_miniapp", "دکمه پنل گرافیکی"),
    ("menu_guide", "دکمه راهنما"),
    ("menu_factory_settings", "دکمه تنظیمات سازنده"),
    ("menu_status", "دکمه وضعیت"),
    ("menu_shop", "دکمه خرید سرویس"),
    ("menu_profile", "دکمه حساب کاربری"),
    ("menu_orders", "دکمه سفارش‌های من"),
    ("menu_miniapp", "دکمه ورود به مینی‌اپ"),
    ("menu_trials", "دکمه تست سرویس"),
    ("menu_myresellers", "دکمه پنل‌های من"),
    ("menu_admin", "دکمه پنل مدیریت"),
    ("menu_services", "دکمه سرویس‌های من"),
    ("menu_panels", "دکمه پنل‌ها"),
    ("menu_resellers", "دکمه نمایندگی‌ها"),
    ("menu_wallet", "دکمه کیف پول"),
    ("menu_trust", "دکمه کانال اعتماد"),
    ("menu_support", "دکمه پشتیبانی"),
    ("menu_settings", "دکمه تنظیمات محصول"),
    ("page_dashboard", "متن صفحه داشبورد"),
    ("page_bots", "متن صفحه ربات‌ها"),
    ("page_guide", "متن راهنما"),
    ("page_design", "متن هویت بصری"),
    ("page_settings", "متن تنظیمات"),
    ("page_shop", "متن فروشگاه"),
    ("page_services", "متن سرویس‌ها"),
    ("page_panels", "متن پنل‌ها"),
    ("page_resellers", "متن نمایندگی‌ها"),
    ("page_wallet", "متن کیف پول"),
    ("page_trust", "متن کانال اعتماد"),
    ("page_support", "متن پشتیبانی"),
    ("webapp_eyebrow", "متن بالای عنوان Mini App"),
    ("webapp_headline", "عنوان اصلی Mini App"),
    ("webapp_subtitle", "زیرعنوان Mini App"),
    ("webapp_new_bot", "دکمه ساخت ربات در Mini App"),
    ("webapp_nav_dashboard", "تب داشبورد Mini App"),
    ("webapp_nav_bots", "تب ربات‌ها Mini App"),
    ("webapp_nav_guide", "تب راهنما Mini App"),
    ("webapp_nav_settings", "تب تنظیمات Mini App"),
    ("webapp_metric_instances", "عنوان آمار ربات‌ها"),
    ("webapp_metric_instances_hint", "توضیح آمار ربات‌ها"),
    ("webapp_metric_isolation", "عنوان آمار ایزوله‌سازی"),
    ("webapp_metric_isolation_value", "وضعیت ایزوله‌سازی"),
    ("webapp_metric_isolation_hint", "توضیح ایزوله‌سازی"),
    ("webapp_metric_provisioning", "عنوان وضعیت Provisioning"),
    ("webapp_metric_provisioning_value", "مقدار وضعیت Provisioning"),
    ("webapp_metric_provisioning_hint", "توضیح Provisioning"),
    ("webapp_steps_title", "عنوان مراحل ساخت"),
    ("webapp_steps_hint", "توضیح مراحل ساخت"),
    ("webapp_step_token_title", "عنوان مرحله توکن"),
    ("webapp_step_token_body", "توضیح مرحله توکن"),
    ("webapp_step_admin_title", "عنوان مرحله مدیر"),
    ("webapp_step_admin_body", "توضیح مرحله مدیر"),
    ("webapp_step_isolation_title", "عنوان مرحله ایزوله‌سازی"),
    ("webapp_step_isolation_body", "توضیح مرحله ایزوله‌سازی"),
    ("webapp_step_install_title", "عنوان مرحله نصب"),
    ("webapp_step_install_body", "توضیح مرحله نصب"),
    ("webapp_bots_title", "عنوان صفحه ربات‌ها"),
    ("webapp_bots_empty_title", "عنوان حالت خالی ربات‌ها"),
    ("webapp_bots_empty_body", "متن حالت خالی ربات‌ها"),
    ("webapp_guide_title", "عنوان صفحه راهنما"),
    ("webapp_status_badge", "نشان وضعیت Mini App"),
    ("webapp_notice_title", "عنوان هشدار داشبورد"),
    ("webapp_notice_body", "متن هشدار داشبورد"),
    ("webapp_bots_subtitle", "زیرعنوان صفحه ربات‌ها"),
    ("webapp_guide_subtitle", "زیرعنوان راهنما"),
    ("webapp_guide_step1_title", "عنوان راهنمای مرحله ۱"),
    ("webapp_guide_step1_body", "متن راهنمای مرحله ۱"),
    ("webapp_guide_step2_title", "عنوان راهنمای مرحله ۲"),
    ("webapp_guide_step2_body", "متن راهنمای مرحله ۲"),
    ("webapp_guide_step3_title", "عنوان راهنمای مرحله ۳"),
    ("webapp_guide_step3_body", "متن راهنمای مرحله ۳"),
    ("webapp_guide_step4_title", "عنوان راهنمای مرحله ۴"),
    ("webapp_guide_step4_body", "متن راهنمای مرحله ۴"),
    ("webapp_guide_notice_title", "عنوان هشدار راهنما"),
    ("webapp_guide_notice_body", "متن هشدار راهنما"),
    ("webapp_settings_subtitle", "زیرعنوان تنظیمات سازنده"),
    ("webapp_settings_theme_title", "عنوان قالب پیش‌فرض"),
    ("webapp_settings_theme_value", "مقدار قالب پیش‌فرض"),
    ("webapp_settings_port_title", "عنوان سیاست پورت"),
    ("webapp_settings_port_body", "توضیح سیاست پورت"),
    ("webapp_settings_port_status", "وضعیت سیاست پورت"),
    ("webapp_settings_secrets_title", "عنوان نگهداری اسرار"),
    ("webapp_settings_secrets_body", "توضیح نگهداری اسرار"),
    ("webapp_settings_secrets_status", "وضعیت نگهداری اسرار"),
    ("webapp_settings_backup_title", "عنوان بکاپ"),
    ("webapp_settings_backup_body", "توضیح بکاپ"),
    ("webapp_settings_backup_status", "وضعیت بکاپ"),
    ("webapp_modal_title", "عنوان فرم ساخت ربات"),
    ("webapp_modal_intro", "توضیح فرم ساخت ربات"),
    ("webapp_form_name_label", "برچسب نام نمایشی"),
    ("webapp_form_username_label", "برچسب شناسه ربات"),
    ("webapp_form_admin_label", "برچسب شناسه مدیر"),
    ("webapp_form_theme_label", "برچسب قالب محصول"),
    ("webapp_form_submit", "دکمه تأیید فرم"),
    ("webapp_form_cancel", "دکمه انصراف فرم"),
    ("webapp_token_notice_title", "عنوان هشدار توکن"),
    ("webapp_token_notice_body", "متن هشدار توکن"),
    ("webapp_settings_title", "عنوان صفحه تنظیمات"),
)


MOTHER_KEYS = frozenset({
    "title", "welcome_text", "logo_file_id", "logo_url", "menu_create", "menu_bots",
    "menu_miniapp", "menu_guide", "menu_factory_settings", "menu_status",
    "page_dashboard", "page_bots", "page_guide", "page_design", "page_settings",
    "webapp_eyebrow", "webapp_headline", "webapp_subtitle", "webapp_new_bot",
    "webapp_nav_dashboard", "webapp_nav_bots", "webapp_nav_guide", "webapp_nav_settings",
    "webapp_metric_instances", "webapp_metric_instances_hint", "webapp_metric_isolation",
    "webapp_metric_isolation_value", "webapp_metric_isolation_hint", "webapp_metric_provisioning",
    "webapp_metric_provisioning_value", "webapp_metric_provisioning_hint", "webapp_steps_title",
    "webapp_steps_hint", "webapp_step_token_title", "webapp_step_token_body",
    "webapp_step_admin_title", "webapp_step_admin_body", "webapp_step_isolation_title",
    "webapp_step_isolation_body", "webapp_step_install_title", "webapp_step_install_body",
    "webapp_bots_title", "webapp_bots_empty_title", "webapp_bots_empty_body",
    "webapp_guide_title", "webapp_settings_title",
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
})
CUSTOMER_KEYS = frozenset({
    "title", "welcome_text", "logo_file_id", "logo_url", "menu_shop", "menu_miniapp",
    "menu_trials", "menu_profile", "menu_orders", "menu_myresellers", "menu_admin", "menu_services",
    "menu_panels", "menu_resellers", "menu_wallet", "menu_trust", "menu_support",
    "menu_settings", "page_shop", "page_services", "page_panels", "page_resellers",
    "page_wallet", "page_trust", "page_support", "page_settings",
})


def content_admin_keyboard(scope: str = "all") -> dict[str, Any]:
    allowed = MOTHER_KEYS if scope == "mother" else CUSTOMER_KEYS if scope == "customer" else None
    fields = [(key, label) for key, label in CONTENT_FIELDS if allowed is None or key in allowed]
    rows = [[{"text": "🎛 مدیریت نمایش/مخفی‌سازی دکمه‌ها", "callback_data": "content:buttons"}]]
    for index in range(0, len(fields), 2):
        row = [
            {"text": f"✏️ {label}", "callback_data": f"content:edit:{key}"}
            for key, label in fields[index:index + 2]
        ]
        rows.append(row)
    rows.append([{"text": "⬅️ بازگشت", "callback_data": "content:back", "style": "primary"}])
    return {"inline_keyboard": rows}


def begin_content_edit(data: str, pending: dict[int, str], admin_id: int) -> str | None:
    if not data.startswith("content:edit:"):
        return None
    key = data.removeprefix("content:edit:")
    labels = dict(CONTENT_FIELDS)
    if key not in labels:
        return None
    pending[admin_id] = key
    if key == "logo_file_id":
        return f"عکس جدید {labels[key]} را همین‌جا ارسال کن. برای لغو /cancel بفرست."
    if key == "logo_url":
        return f"آدرس HTTPS تصویر برای {labels[key]} را بفرست. برای حذف، /clear و برای لغو /cancel بفرست."
    return f"مقدار جدید «{labels[key]}» را بفرست. برای حذف مقدار سفارشی /clear و برای لغو /cancel بفرست."


def save_pending_content(
    store: BotContentStore,
    pending: dict[int, str],
    admin_id: int,
    message: dict[str, Any],
) -> str | None:
    key = pending.get(admin_id)
    if not key:
        return None
    text = (message.get("text") or "").strip()
    if text.lower() == "/cancel":
        pending.pop(admin_id, None)
        return "ویرایش لغو شد."
    if text.lower() == "/clear":
        store.reset(key)
        pending.pop(admin_id, None)
        return "مقدار سفارشی حذف شد و مقدار پیش‌فرض استفاده می‌شود."
    if key == "logo_file_id":
        photos = message.get("photo") or []
        if not photos:
            return "لطفاً خود تصویر را ارسال کن؛ برای لغو /cancel بفرست."
        value = str(photos[-1].get("file_id") or "")
        if not value:
            return "شناسه تصویر دریافت نشد؛ دوباره ارسال کن."
    else:
        value = text
        if not value:
            return "مقدار خالی پذیرفته نیست؛ برای حذف /clear بفرست."
    try:
        store.set(key, value)
    except ContentStoreError as exc:
        return f"ذخیره نشد: {exc}"
    pending.pop(admin_id, None)
    return "ذخیره شد. از این پس نسخه جدید محتوا استفاده می‌شود."


def content_value(store: BotContentStore, key: str, fallback: str = "") -> str:
    try:
        return store.get(key) or fallback
    except ContentStoreError:
        return fallback
