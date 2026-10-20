"""Authenticated WSGI API for the MANUBIS customer Mini App.

Telegram initData is validated on every request. The API exposes read-only customer
views and a wallet-backed purchase endpoint; payment gateway and admin routes are absent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit


class InitDataError(ValueError):
    """Telegram Web App initData failed validation."""


def validate_telegram_init_data(raw: str, bot_token: str, *, now: int | None = None,
                                max_age_seconds: int = 300, future_skew_seconds: int = 30) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise InitDataError("initData_missing_or_oversized")
    if not isinstance(bot_token, str) or not bot_token or len(bot_token) > 512:
        raise InitDataError("bot_token_unavailable")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise InitDataError("initData_malformed") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            raise InitDataError("initData_duplicate_field")
        fields[key] = value
    supplied_hash = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash):
        raise InitDataError("initData_hash_missing")
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash.lower()):
        raise InitDataError("initData_hash_invalid")
    try:
        auth_date = int(fields.get("auth_date", ""))
    except (TypeError, ValueError):
        raise InitDataError("initData_auth_date_invalid") from None
    current = int(time.time()) if now is None else int(now)
    if auth_date > current + future_skew_seconds or current - auth_date > max_age_seconds:
        raise InitDataError("initData_expired")
    try:
        user = json.loads(fields.get("user", ""))
    except (TypeError, ValueError):
        raise InitDataError("initData_user_invalid") from None
    if not isinstance(user, dict):
        raise InitDataError("initData_user_invalid")
    telegram_id = user.get("id")
    if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
        raise InitDataError("initData_user_id_invalid")
    first_name = user.get("first_name", "")
    if not isinstance(first_name, str) or len(first_name) > 128:
        raise InitDataError("initData_user_invalid")
    for optional in ("last_name", "username", "language_code"):
        value = user.get(optional, "")
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise InitDataError("initData_user_invalid")
    return {"telegram_id": telegram_id, "user": user, "auth_date": auth_date}


def _normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Allowed origins must be bare HTTPS origins")
    return f"https://{parsed.netloc}".lower()


def _safe_subscription_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value


class CustomerMiniAppWSGI:
    def __init__(self, store: Any, bot_token: str, *, allowed_origins: tuple[str, ...] | list[str],
                 clock: Callable[[], float] = time.time, max_age_seconds: int = 300,
                 purchase_service_factory: Callable[..., Any] | None = None):
        if not bot_token:
            raise ValueError("Customer bot token is required for Telegram initData verification")
        origins = {_normalize_origin(origin) for origin in allowed_origins}
        if not origins:
            raise ValueError("At least one HTTPS customer Mini App origin is required")
        self.store = store
        self.bot_token = bot_token
        self.allowed_origins = origins
        self.clock = clock
        self.max_age_seconds = max_age_seconds
        self.purchase_service_factory = purchase_service_factory

    @staticmethod
    def _response(start_response: Callable[..., Any], status: HTTPStatus, payload: dict[str, Any]) -> list[bytes]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))),
                   ("Cache-Control", "no-store, max-age=0"), ("Pragma", "no-cache"),
                   ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                   ("Vary", "Authorization, Origin")]
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]

    def _shop(self) -> dict[str, Any]:
        categories = [{"id": str(c["category_id"]), "title": str(c["title"]),
                       "description": str(c.get("description") or "")} for c in self.store.list_categories(enabled_only=True)]
        allowed = ("product_id", "category_id", "title", "description", "price_minor", "currency",
                   "duration_hours", "traffic_bytes", "pricing_mode", "unit_price_minor")
        products = [{key: product[key] for key in allowed if key in product}
                    for product in self.store.list_products()]
        return {"categories": categories, "products": products}

    def _services(self, telegram_id: int) -> list[dict[str, Any]]:
        allowed = ("service_id", "product_id", "username", "status", "expires_at", "traffic_limit_bytes",
                   "traffic_used_bytes", "pricing_mode", "currency", "billing_status",
                   "outstanding_debt_minor", "provider_checked_at")
        result = []
        for service in self.store.list_services(telegram_id):
            item = {key: service[key] for key in allowed if key in service}
            item["subscription_url"] = _safe_subscription_url(service.get("subscription_url"))
            result.append(item)
        return result

    def _resolve_panel_secret(self, secret_ref: str) -> str:
        if secret_ref.startswith("env:"):
            name = secret_ref[4:].strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
                raise ValueError("invalid_secret_reference")
            value = os.environ.get(name, "")
            if not value:
                raise ValueError("credential_unavailable")
            return value
        if secret_ref.startswith("vault:"):
            account_id = secret_ref[6:].strip()
            if not re.fullmatch(r"[a-f0-9]{32}", account_id):
                raise ValueError("invalid_secret_reference")
            credential = self.store.get_reseller_credential(account_id)
            if not credential:
                raise ValueError("credential_unavailable")
            return str(credential["password"])
        raise ValueError("invalid_secret_reference")

    def _purchase(self, environ: dict[str, Any], telegram_id: int, start_response: Callable[..., Any]) -> list[bytes]:
        if str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0].strip().lower() != "application/json":
            return self._response(start_response, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 4096:
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        try:
            raw_body = environ["wsgi.input"].read(length)
            body = json.loads(raw_body.decode("utf-8"))
        except (KeyError, UnicodeError, ValueError):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        if not isinstance(body, dict):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        product_id = body.get("product_id")
        idempotency_key = body.get("idempotency_key")
        if not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", product_id):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_product_id"})
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", idempotency_key):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_idempotency_key"})
        from manubisguard.panels.registry import default_registry
        from manubisguard.sales.service import CustomerPurchaseService, PurchaseNeedsReconciliation
        resolver = self._resolve_panel_secret
        registry = default_registry(secret_resolver=resolver)
        factory = self.purchase_service_factory or CustomerPurchaseService
        service = factory(self.store, secret_resolver=resolver, registry=registry)
        try:
            outcome = asyncio.run(service.buy_from_wallet(telegram_id=telegram_id, product_id=product_id,
                idempotency_key=f"miniapp:{telegram_id}:{idempotency_key}", now=int(self.clock())))
        except PurchaseNeedsReconciliation:
            return self._response(start_response, HTTPStatus.ACCEPTED,
                {"status": "reconciliation_required", "message": "وضعیت سفارش نیازمند بررسی است؛ خرید را دوباره ثبت نکنید."})
        except Exception as exc:
            from manubisguard.customer_store import CustomerStoreError
            if isinstance(exc, CustomerStoreError):
                return self._response(start_response, HTTPStatus.CONFLICT, {"error": "purchase_rejected"})
            return self._response(start_response, HTTPStatus.BAD_GATEWAY, {"error": "provider_operation_failed"})
        return self._response(start_response, HTTPStatus.CREATED, {
            "status": outcome.status, "order_id": outcome.order_id, "service_id": outcome.service_id,
            "amount_minor": outcome.amount_minor, "currency": outcome.currency, "username": outcome.username,
            "subscription_url": _safe_subscription_url(outcome.subscription_url),
            "idempotent_replay": bool(outcome.idempotent_replay),
        })

    def _create_ticket(self, environ: dict[str, Any], auth: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        if str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0].strip().lower() != "application/json":
            return self._response(start_response, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 4096:
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        try:
            body = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
        except (KeyError, UnicodeError, ValueError):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        if not isinstance(body, dict):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_request_body"})
        subject, message, key = body.get("subject"), body.get("message"), body.get("idempotency_key")
        if not isinstance(subject, str) or not 4 <= len(subject.strip()) <= 160:
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_ticket_subject"})
        if not isinstance(message, str) or not 8 <= len(message.strip()) <= 3000:
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_ticket_message"})
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", key):
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_idempotency_key"})
        telegram_id = int(auth["telegram_id"])
        user = auth["user"]
        ticket_id = "web-" + hashlib.sha256(f"{self.store.tenant_id}:{telegram_id}:{key}".encode()).hexdigest()[:24]
        try:
            self.store.ensure_customer(telegram_id, str(user.get("username") or ""), str(user.get("first_name") or ""))
            self.store.create_ticket(ticket_id=ticket_id, telegram_id=telegram_id,
                                     subject=subject.strip(), first_message=message.strip())
        except Exception as exc:
            from manubisguard.customer_store import CustomerStoreError
            if isinstance(exc, CustomerStoreError):
                return self._response(start_response, HTTPStatus.CONFLICT, {"error": "ticket_conflict"})
            return self._response(start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ticket_unavailable"})
        return self._response(start_response, HTTPStatus.CREATED, {"ticket_id": ticket_id, "status": "open"})

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        if path == "/healthz" and method == "GET":
            return self._response(start_response, HTTPStatus.OK, {"ok": True})
        if method not in {"GET", "POST"} or (method == "POST" and path not in {"/api/v1/purchase", "/api/v1/tickets"}) or (method == "GET" and path == "/api/v1/purchase"):
            return self._response(start_response, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
        origin = str(environ.get("HTTP_ORIGIN", "")).strip()
        if method == "POST" and not origin:
            return self._response(start_response, HTTPStatus.FORBIDDEN, {"error": "origin_required"})
        if origin:
            try:
                normalized_origin = _normalize_origin(origin)
            except ValueError:
                return self._response(start_response, HTTPStatus.FORBIDDEN, {"error": "origin_denied"})
            if normalized_origin not in self.allowed_origins:
                return self._response(start_response, HTTPStatus.FORBIDDEN, {"error": "origin_denied"})
        if not path.startswith("/api/v1/"):
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        auth_header = str(environ.get("HTTP_AUTHORIZATION", ""))
        raw_init_data = auth_header[4:].strip() if auth_header.startswith("tma ") else ""
        if not raw_init_data:
            raw_init_data = str(environ.get("HTTP_X_TELEGRAM_INIT_DATA", ""))
        try:
            auth = validate_telegram_init_data(raw_init_data, self.bot_token, now=int(self.clock()),
                                               max_age_seconds=self.max_age_seconds)
        except InitDataError:
            return self._response(start_response, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        telegram_id = auth["telegram_id"]
        if method == "POST" and path == "/api/v1/purchase":
            return self._purchase(environ, telegram_id, start_response)
        if method == "POST" and path == "/api/v1/tickets":
            return self._create_ticket(environ, auth, start_response)
        if path == "/api/v1/tickets":
            return self._response(start_response, HTTPStatus.OK, {"tickets": self.store.list_tickets(telegram_id)[:50]})
        if path == "/api/v1/me":
            user = auth["user"]
            safe_user = {key: user[key] for key in ("id", "first_name", "last_name", "username", "language_code") if key in user}
            return self._response(start_response, HTTPStatus.OK, {"user": safe_user})
        if path == "/api/v1/shop":
            return self._response(start_response, HTTPStatus.OK, self._shop())
        if path == "/api/v1/wallet":
            return self._response(start_response, HTTPStatus.OK, {
                "balances": self.store.wallet_balances(telegram_id),
                "debts": self.store.outstanding_debts(telegram_id),
            })
        if path == "/api/v1/services":
            return self._response(start_response, HTTPStatus.OK, {"services": self._services(telegram_id)})
        if path == "/api/v1/orders":
            return self._response(start_response, HTTPStatus.OK, {"orders": self.store.list_customer_orders(telegram_id, limit=50)})
        if path == "/api/v1/transactions":
            return self._response(start_response, HTTPStatus.OK, {"transactions": self.store.list_wallet_transactions(telegram_id, limit=50)})
        return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})


def create_customer_miniapp_app(store: Any, bot_token: str, *, allowed_origins: tuple[str, ...] | list[str],
                                clock: Callable[[], float] = time.time, max_age_seconds: int = 300) -> CustomerMiniAppWSGI:
    """Construct the WSGI app; deployment must mount it same-origin under /api/v1."""
    return CustomerMiniAppWSGI(store, bot_token, allowed_origins=allowed_origins,
                               clock=clock, max_age_seconds=max_age_seconds)


def create_customer_miniapp_app_from_env() -> CustomerMiniAppWSGI:
    """Explicit factory for WSGI servers; no app or DB is created at module import time."""
    import os
    from manubisguard.customer_store import CustomerDataStore
    token = os.environ.get("MANUBISGUARD_CUSTOMER_BOT_TOKEN", "").strip()
    origins = tuple(part.strip() for part in os.environ.get("MANUBIS_CUSTOMER_WEBAPP_ORIGINS", "").split(",") if part.strip())
    if not token or not origins:
        raise RuntimeError("Customer Mini App requires bot token and HTTPS origin allowlist")
    store = CustomerDataStore(os.environ.get("ANUBIS_CUSTOMER_DB"), tenant_id=os.environ.get("ANUBIS_CUSTOMER_TENANT", "default"))
    return create_customer_miniapp_app(store, token, allowed_origins=origins)
