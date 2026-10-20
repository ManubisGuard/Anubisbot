"""Wallet-backed customer purchase orchestration with no blind provider retries."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Callable, Any

from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.panels.contracts import PanelConnection, PanelError, UserCreate, PanelUser
from manubisguard.panels.registry import AdapterRegistry, default_registry


class PurchaseNeedsReconciliation(RuntimeError):
    """Provider result may be ambiguous; funds stay reserved until an operator reconciles."""


@dataclass(frozen=True, slots=True)
class PurchaseOutcome:
    order_id: str
    service_id: str
    status: str
    amount_minor: int
    currency: str
    username: str
    subscription_url: str | None = None
    provider_user_id: str | None = None
    idempotent_replay: bool = False


class CustomerPurchaseService:
    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 registry: AdapterRegistry | None = None):
        self.store = store
        self._secret_resolver = secret_resolver
        self.registry = registry or default_registry(secret_resolver=secret_resolver)

    def _connection(self, panel_id: str) -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row:
            raise CustomerStoreError("Product panel is not configured")
        metadata = row.get("metadata") or {}
        return PanelConnection(id=str(row["panel_id"]), tenant_id=self.store.tenant_id,
            name=str(row["label"]), kind=str(row["provider"]), base_url=str(row["base_url"]),
            auth_mode=str(row["auth_mode"]), secret_ref=str(row["secret_ref"]),
            enabled=bool(row["enabled"]), verify_tls=True, metadata=metadata)

    async def buy_from_wallet(self, *, telegram_id: int, product_id: str,
                              idempotency_key: str, reseller_account_id: str | None = None,
                              discount_code: str | None = None,
                              now: int | None = None) -> PurchaseOutcome:
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
        previous = self.store.get_order_by_idempotency(idempotency_key, telegram_id)
        if previous:
            same_request = (
                str(previous.get("product_id") or "") == str(product_id)
                and str(previous.get("reseller_account_id") or "") == str(reseller_account_id or "")
                and str(previous.get("discount_code") or "") == str(discount_code or "").strip().upper()
            )
            if not same_request:
                raise CustomerStoreError("Order idempotency key reused with a different request")
            existing_service = self.store.get_customer_service(str(previous["order_id"]), telegram_id)
            if previous["status"] == "active" and existing_service:
                return PurchaseOutcome(str(previous["order_id"]), str(previous["order_id"]), "active",
                    int(previous["amount_minor"]), str(previous["currency"]),
                    str(existing_service.get("username") or ""), existing_service.get("subscription_url") or None,
                    str(existing_service.get("provider_user_id") or ""), True)
            raise PurchaseNeedsReconciliation(f"Order {previous['order_id']} already exists in {previous['status']} state; no duplicate provider create was attempted")

        product = self.store.get_product(product_id, enabled_only=True)
        if not product:
            raise CustomerStoreError("Product is unavailable")
        panel_id = str(product.get("panel_id") or "")
        if not panel_id:
            raise CustomerStoreError("Product has no configured panel")
        connection = self._connection(panel_id)
        reseller_account = None
        if reseller_account_id:
            reseller_account = self.store.get_reseller_account(reseller_account_id, telegram_id)
            if not reseller_account or reseller_account["status"] != "active":
                raise CustomerStoreError("Reseller account is unavailable or suspended")
            if reseller_account["panel_id"] != panel_id:
                raise CustomerStoreError("Reseller account and product must use the same panel")
            if connection.kind == "rebecca" and reseller_account["provider_account_id"] != "platform-managed":
                raise CustomerStoreError("Native Rebecca reseller child provisioning is blocked until a scoped per-admin API key is configured; no wallet debit was made")
            if connection.kind == "pasarguard" and reseller_account["provider_account_id"] != "platform-managed":
                metadata = dict(connection.metadata)
                metadata["username"] = str(reseller_account["username"])
                connection = PanelConnection(id=connection.id, tenant_id=connection.tenant_id, name=connection.name,
                    kind=connection.kind, base_url=connection.base_url, auth_mode="username_password",
                    secret_ref=f"vault:{reseller_account_id}", enabled=connection.enabled,
                    verify_tls=True, metadata=metadata)
        adapter = self.registry.get(connection.kind)
        # Validate credentials and read-only API access before reserving customer funds.
        try:
            resolved_secret = self._secret_resolver(connection.secret_ref)
            if not isinstance(resolved_secret, str) or not resolved_secret:
                raise CustomerStoreError("Panel credential is not configured; no wallet debit was made")
            caps = await adapter.capabilities(connection)
            if not caps.create_user:
                raise CustomerStoreError(f"Provider {connection.kind} does not advertise create-user support")
            health = await adapter.health_check(connection)
            if not health.get("ok"):
                raise CustomerStoreError(f"Panel read-only health check failed ({health.get('error_type', 'unknown')})")
        except CustomerStoreError:
            raise
        except Exception as exc:
            raise CustomerStoreError(f"Panel preflight failed ({type(exc).__name__}); no wallet debit was made") from None

        order_id = uuid.uuid4().hex
        order = self.store.reserve_product_order(order_id=order_id, telegram_id=telegram_id,
            product_id=product_id, idempotency_key=idempotency_key, reseller_account_id=reseller_account_id,
            discount_code=discount_code, now=now)
        actual_order_id = str(order["order_id"])
        if order.get("idempotent_replay"):
            if order["status"] == "active":
                service = self.store.get_customer_service(actual_order_id, telegram_id)
                if service:
                    return PurchaseOutcome(actual_order_id, actual_order_id, "active", int(order["amount_minor"]),
                        str(order["currency"]), str(service.get("username") or ""),
                        service.get("subscription_url") or None, str(service.get("provider_user_id") or ""), True)
            raise PurchaseNeedsReconciliation(f"Order {actual_order_id} already exists in {order['status']} state; no duplicate provider create was attempted")

        provider_create_started = False
        try:
            group_ids_raw = order.get("group_ids_json", "[]")
            group_ids = json.loads(group_ids_raw) if isinstance(group_ids_raw, str) else list(group_ids_raw or [])
            username = f"u{telegram_id}_{actual_order_id[:8]}"
            mode = str(order.get("pricing_mode", "fixed"))
            data_limit: int | None = int(order.get("traffic_bytes", 0) or 0) or None
            if mode in {"unlimited", "hourly", "usage"}:
                data_limit = None
            expire_at = None
            duration_hours = int(order.get("duration_hours", 0) or 0)
            if mode in {"fixed", "unlimited"} and duration_hours > 0:
                expire_at = datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=duration_hours)
            extra: dict[str, Any] = {}
            if connection.kind == "rebecca":
                service_id = connection.metadata.get("service_id") or (group_ids[0] if group_ids else None)
                try: service_id = int(service_id)
                except (TypeError, ValueError): raise CustomerStoreError("Rebecca product requires a numeric service ID in panel metadata or product groups") from None
                if service_id <= 0: raise CustomerStoreError("Rebecca service ID must be positive")
                extra["service_id"] = service_id
            elif connection.kind == "sanaei":
                raw_inbound_ids = group_ids or connection.metadata.get("inbound_ids") or []
                try: inbound_ids = [int(x) for x in raw_inbound_ids]
                except (TypeError, ValueError): raise CustomerStoreError("Sanaei product requires numeric inbound IDs") from None
                if not inbound_ids or any(x <= 0 for x in inbound_ids):
                    raise CustomerStoreError("Sanaei product requires at least one positive inbound ID")
                extra["inbound_ids"] = inbound_ids
                extra["uuid"] = str(uuid.uuid4())
                if connection.metadata.get("default_flow"): extra["flow"] = connection.metadata["default_flow"]
                if connection.metadata.get("sub_id"): extra["sub_id"] = str(connection.metadata["sub_id"])
                if connection.metadata.get("ip_limit") is not None: extra["ip_limit"] = int(connection.metadata["ip_limit"])
            if connection.kind == "pasarguard":
                try: provider_groups = tuple(int(x) for x in group_ids)
                except (TypeError, ValueError): raise CustomerStoreError("PasarGuard product requires numeric group IDs") from None
            else:
                provider_groups = ()
            provider_create_started = True
            created: PanelUser = await adapter.create_user(connection, UserCreate(
                username=username, data_limit_bytes=data_limit, expire_at=expire_at,
                group_ids=provider_groups, note=f"Anubis order {actual_order_id}", extra=extra))
            subscription_url = created.subscription_url
            if not subscription_url and caps.subscription_link:
                subscription_url = await adapter.get_subscription_url(connection, created.username or username)
            snapshot = dict(order)
            snapshot["expires_at"] = expire_at.isoformat() if expire_at else None
            if reseller_account_id:
                # The reseller plan meters the account/aggregate; do not double-bill each child config.
                if mode in {"hourly", "usage"}:
                    snapshot["pricing_mode"] = "unlimited"
                    snapshot["unit_price_minor"] = 0
            self.store.create_metered_service(service_id=actual_order_id, telegram_id=telegram_id,
                product=snapshot, provider_user_id=created.provider_user_id, username=created.username or username,
                subscription_url=subscription_url, reseller_account_id=reseller_account_id or "", now=now)
            self.store.finalize_product_order(order_id=actual_order_id, service_id=actual_order_id,
                                              provider_reference=created.provider_user_id)
            return PurchaseOutcome(actual_order_id, actual_order_id, "active", int(order["amount_minor"]),
                str(order["currency"]), created.username or username, subscription_url,
                created.provider_user_id, False)
        except Exception as exc:
            if not provider_create_started:
                # Local product validation failed before any provider mutation; reversing the
                # reservation is safe and prevents invalid catalog data from trapping customer funds.
                try:
                    self.store.fail_product_order_and_refund(order_id=actual_order_id,
                        reason_code=f"preflight:{type(exc).__name__}", release_discount_claim=True)
                except Exception:
                    pass
                raise CustomerStoreError("Product configuration was invalid; the wallet reservation was reversed") from None
            # Once the provider mutation starts, a timeout may occur after creation. Never auto-retry
            # or refund until an operator has reconciled provider state.
            try:
                self.store.flag_product_order_reconciliation(order_id=actual_order_id,
                    reason_code=type(exc).__name__)
            except Exception:
                pass
            raise PurchaseNeedsReconciliation(
                f"Order {actual_order_id} could not be safely confirmed; funds remain reserved for reconciliation"
            ) from None


class CustomerServiceChangeService:
    """Wallet-backed renewal and traffic top-up; ambiguous provider writes require reconciliation."""

    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 registry: AdapterRegistry | None = None):
        self.store = store
        self._secret_resolver = secret_resolver
        self.registry = registry or default_registry(secret_resolver=secret_resolver)

    def _connection(self, panel_id: str) -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row or not row.get("enabled"):
            raise CustomerStoreError("Service panel is unavailable")
        return PanelConnection(id=str(row["panel_id"]), tenant_id=self.store.tenant_id,
            name=str(row["label"]), kind=str(row["provider"]), base_url=str(row["base_url"]),
            auth_mode=str(row["auth_mode"]), secret_ref=str(row["secret_ref"]),
            enabled=True, verify_tls=True, metadata=row.get("metadata") or {})

    async def preview(self, *, telegram_id: int, service_id: str,
                      change_type: str, now: int | None = None) -> dict[str, Any]:
        """Read-only live quote: no wallet reservation and no provider mutation."""
        if change_type not in {"renewal", "traffic_topup"}:
            raise CustomerStoreError("Unsupported service change")
        service = self.store.get_customer_service(service_id, telegram_id=telegram_id)
        if not service:
            raise CustomerStoreError("Service not found for this customer")
        product = self.store.get_product(str(service.get("product_id") or ""), enabled_only=True)
        if not product or product.get("panel_id") != service.get("panel_id"):
            raise CustomerStoreError("The original product is unavailable or no longer matches this service")
        quantity = int(product.get("duration_hours") or 0) if change_type == "renewal" else int(product.get("traffic_bytes") or 0)
        if quantity <= 0:
            raise CustomerStoreError("The original product has no valid renewal duration or traffic amount")
        connection = self._connection(str(service["panel_id"]))
        secret = self._secret_resolver(connection.secret_ref)
        if not isinstance(secret, str) or not secret:
            raise CustomerStoreError("Panel credential is not configured")
        adapter = self.registry.get(connection.kind)
        caps = await adapter.capabilities(connection)
        if not caps.update_user:
            raise CustomerStoreError("Panel does not advertise user-update support")
        if not service.get("provider_user_id") or not service.get("username"):
            raise CustomerStoreError("Service has no provider identity")
        current = await adapter.get_user(connection, str(service["username"]))
        if str(current.provider_user_id) != str(service["provider_user_id"]):
            raise CustomerStoreError("Provider identity does not match the stored service; refusing preview")
        now_dt = datetime.fromtimestamp(now if now is not None else datetime.now(timezone.utc).timestamp(), timezone.utc)
        result: dict[str, Any] = {
            "service_id": service_id, "change_type": change_type,
            "amount_minor": int(product["price_minor"]), "currency": str(product["currency"]),
            "quantity": quantity, "provider_status": str(current.status),
            "before_expires_at": current.expire_at.isoformat() if current.expire_at else service.get("expires_at"),
            "before_traffic_limit_bytes": current.data_limit_bytes if current.data_limit_bytes is not None else int(service.get("traffic_limit_bytes") or 0),
            "traffic_used_bytes": int(service.get("traffic_used_bytes") or 0),
        }
        if change_type == "renewal":
            base_expiry = current.expire_at
            if base_expiry is None and service.get("expires_at"):
                base_expiry = datetime.fromisoformat(str(service["expires_at"]))
            if base_expiry is None:
                raise CustomerStoreError("Unlimited or non-expiring service cannot be time-extended using this product")
            if base_expiry.tzinfo is None:
                base_expiry = base_expiry.replace(tzinfo=timezone.utc)
            target_expiry = max(base_expiry, now_dt) + timedelta(hours=quantity)
            result["target_expires_at"] = target_expiry.isoformat()
            result["remaining_seconds_before"] = max(0, int((base_expiry - now_dt).total_seconds()))
        else:
            current_limit = current.data_limit_bytes
            if current_limit is None:
                current_limit = int(service.get("traffic_limit_bytes") or 0)
            if current_limit <= 0:
                raise CustomerStoreError("Unlimited or unknown traffic limit cannot be topped up safely")
            target_limit = int(current_limit) + quantity
            if target_limit <= int(current_limit):
                raise CustomerStoreError("Traffic limit overflow")
            result["before_traffic_limit_bytes"] = int(current_limit)
            result["target_traffic_limit_bytes"] = target_limit
            result["remaining_traffic_bytes_before"] = max(0, int(current_limit) - int(service.get("traffic_used_bytes") or 0))
        return result

    async def inspect_reconciliation(self, *, operation_id: str) -> dict[str, Any]:
        """Read-only provider comparison for a persisted ambiguous service mutation."""
        op = self.store.get_service_change_operation(operation_id)
        if not op:
            raise CustomerStoreError("Service-change operation not found")
        if op["status"] != "pending" and not str(op["status"]).startswith("reconcile_required:"):
            raise CustomerStoreError("Only pending or reconciliation-required operations can be inspected")
        change_type = str(op["change_type"])
        if change_type == "renewal":
            target = op.get("target_expires_at")
            before = op.get("before_expires_at")
            if not target:
                return {**op, "comparison": "target_not_persisted", "observed_value": None}
        else:
            target = op.get("target_traffic_limit_bytes")
            before = op.get("before_traffic_limit_bytes")
            if target is None or int(target) <= 0:
                return {**op, "comparison": "target_not_persisted", "observed_value": None}
        try:
            connection = self._connection(str(op.get("panel_id") or ""))
            # Reconciliation is read-only and uses the normalized provider contract shared
            # by the three explicitly supported adapters. Never infer mutation success from
            # health checks; only compare the fresh, identity-matched user state below.
            if connection.kind not in {"pasarguard", "rebecca", "sanaei"}:
                return {**op, "comparison": "provider_unsupported", "observed_value": None}
            secret = self._secret_resolver(connection.secret_ref)
            if not isinstance(secret, str) or not secret:
                return {**op, "comparison": "read_failed", "reason_code": "CredentialUnavailable", "observed_value": None}
            adapter = self.registry.get(connection.kind)
            if not op.get("provider_user_id") or not op.get("username"):
                return {**op, "comparison": "identity_missing", "observed_value": None}
            current = await adapter.get_user(connection, str(op["username"]))
            if (str(current.provider_user_id) != str(op["provider_user_id"])
                    or str(current.username) != str(op["username"])):
                # Both the stable provider ID and requested username must match. A
                # matching ID alone is insufficient if a provider response is misrouted.
                return {**op, "comparison": "identity_mismatch", "observed_value": None}
            if change_type == "renewal":
                observed_dt = current.expire_at
                if observed_dt is None:
                    return {**op, "comparison": "observed_unknown", "observed_value": None}
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=timezone.utc)
                observed = observed_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                target_dt = datetime.fromisoformat(str(target))
                before_dt = datetime.fromisoformat(str(before)) if before else None
                if target_dt.tzinfo is None:
                    target_dt = target_dt.replace(tzinfo=timezone.utc)
                if before_dt and before_dt.tzinfo is None:
                    before_dt = before_dt.replace(tzinfo=timezone.utc)
                target_norm = target_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                before_norm = before_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if before_dt else None
                comparison = "target_present" if observed == target_norm else "before_present" if observed == before_norm else "different"
            else:
                if current.data_limit_bytes is None:
                    return {**op, "comparison": "observed_unknown", "observed_value": None}
                observed = int(current.data_limit_bytes)
                comparison = "target_present" if observed == int(target) else "before_present" if before is not None and observed == int(before) else "different"
            return {**op, "comparison": comparison, "observed_value": observed,
                    "identity_verified": True}
        except Exception as exc:
            return {**op, "comparison": "read_failed", "reason_code": type(exc).__name__, "observed_value": None}

    async def finalize_target_present(self, *, operation_id: str, actor_telegram_id: int,
                                      reason: str) -> dict[str, Any]:
        """Freshly inspect provider state, then atomically finalize with append-only operator audit."""
        op = self.store.get_service_change_operation(operation_id)
        if not op:
            raise CustomerStoreError("Service-change operation not found")
        # Exact replay is handled by the store only when the same actor/reason/evidence was recorded.
        existing_events = self.store.list_service_change_events(operation_id)
        if existing_events:
            event = existing_events[0]
            if int(event["actor_telegram_id"]) == int(actor_telegram_id) and event["reason"] == reason.strip():
                return {**event, "idempotent_replay": True}
            raise CustomerStoreError("A final decision is already recorded for this operation")
        result = await self.inspect_reconciliation(operation_id=operation_id)
        if result.get("comparison") != "target_present" or result.get("identity_verified") is not True:
            raise CustomerStoreError(
                f"Cannot finalize operation: fresh provider comparison is {result.get('comparison', 'unknown')}"
            )
        return self.store.resolve_service_change_target_present(
            operation_id=operation_id, actor_telegram_id=actor_telegram_id,
            comparison=str(result["comparison"]), observed_value=result.get("observed_value"),
            identity_verified=result.get("identity_verified") is True, reason=reason,
        )

    async def change(self, *, telegram_id: int, service_id: str, change_type: str,
                     idempotency_key: str, now: int | None = None,
                     expected_quote: dict[str, Any] | None = None) -> dict[str, Any]:
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
        operation_id = uuid.uuid4().hex
        quote = expected_quote or {}
        op = self.store.reserve_service_change(operation_id=operation_id, telegram_id=telegram_id,
            service_id=service_id, change_type=change_type, idempotency_key=idempotency_key, now=now,
            expected_amount_minor=quote.get("amount_minor"), expected_currency=quote.get("currency"),
            expected_quantity=quote.get("quantity"))
        if op.get("idempotent_replay"):
            if op["status"] == "completed":
                return {**op, "idempotent_replay": True}
            raise PurchaseNeedsReconciliation(
                f"Service change {op['operation_id']} already exists in {op['status']} state; no provider retry was attempted")
        mutation_started = False
        try:
            connection = self._connection(str(op["panel_id"]))
            secret = self._secret_resolver(connection.secret_ref)
            if not isinstance(secret, str) or not secret:
                raise CustomerStoreError("Panel credential is not configured")
            adapter = self.registry.get(connection.kind)
            caps = await adapter.capabilities(connection)
            if not caps.update_user:
                raise CustomerStoreError("Panel does not advertise user-update support")
            if not op.get("provider_user_id") or not op.get("username"):
                raise CustomerStoreError("Service has no provider identity; manual reconciliation is required")
            current = await adapter.get_user(connection, str(op["username"]))
            if str(current.provider_user_id) != str(op["provider_user_id"]):
                raise CustomerStoreError("Provider identity does not match the stored service; refusing mutation")
            from manubisguard.panels.contracts import UserUpdate
            if change_type == "renewal":
                base_expiry = current.expire_at
                if base_expiry is None and op.get("expires_at"):
                    base_expiry = datetime.fromisoformat(str(op["expires_at"]))
                    if base_expiry.tzinfo is None:
                        base_expiry = base_expiry.replace(tzinfo=timezone.utc)
                if base_expiry is None:
                    raise CustomerStoreError("Unlimited or non-expiring service cannot be time-extended using this product")
                if base_expiry.tzinfo is None:
                    base_expiry = base_expiry.replace(tzinfo=timezone.utc)
                expected_before = quote.get("before_expires_at")
                if expected_quote is not None:
                    if not isinstance(expected_before, str):
                        raise CustomerStoreError("The confirmed quote is incomplete; request a fresh preview")
                    try:
                        expected_dt = datetime.fromisoformat(expected_before)
                    except ValueError:
                        raise CustomerStoreError("The confirmed quote is invalid; request a fresh preview") from None
                    if expected_dt.tzinfo is None:
                        expected_dt = expected_dt.replace(tzinfo=timezone.utc)
                    if expected_dt.astimezone(timezone.utc).replace(microsecond=0) != base_expiry.astimezone(timezone.utc).replace(microsecond=0):
                        raise CustomerStoreError("The service expiry changed after preview; request a fresh quote")
                new_expiry = max(base_expiry, datetime.fromtimestamp(now, timezone.utc)) + timedelta(hours=int(op["quantity"]))
                if expected_quote is not None:
                    expected_target = quote.get("target_expires_at")
                    if not isinstance(expected_target, str):
                        raise CustomerStoreError("The confirmed quote target is missing")
                    try:
                        expected_target_dt = datetime.fromisoformat(expected_target)
                    except ValueError:
                        raise CustomerStoreError("The confirmed quote target is invalid") from None
                    if expected_target_dt.tzinfo is None:
                        expected_target_dt = expected_target_dt.replace(tzinfo=timezone.utc)
                    quote_issued_at = quote.get("quote_issued_at", now)
                    if isinstance(quote_issued_at, bool) or not isinstance(quote_issued_at, int):
                        raise CustomerStoreError("The confirmed quote timestamp is invalid")
                    quoted_base = max(base_expiry, datetime.fromtimestamp(quote_issued_at, timezone.utc))
                    quoted_target = quoted_base + timedelta(hours=int(op["quantity"]))
                    if expected_target_dt.astimezone(timezone.utc).replace(microsecond=0) != quoted_target.astimezone(timezone.utc).replace(microsecond=0):
                        raise CustomerStoreError("The calculated expiry differs from the confirmed quote; request a fresh preview")
                    new_expiry = expected_target_dt
                self.store.persist_service_change_target(
                    operation_id=str(op["operation_id"]),
                    before_expires_at=base_expiry.isoformat(),
                    before_traffic_limit_bytes=current.data_limit_bytes,
                    target_expires_at=new_expiry.isoformat(),
                    target_traffic_limit_bytes=None)
                mutation_started = True
                await adapter.update_user(connection, str(op["provider_user_id"]), UserUpdate(expire_at=new_expiry))
                self.store.complete_service_change(operation_id=str(op["operation_id"]),
                    expires_at=new_expiry.isoformat(), traffic_limit_bytes=None)
                return {**op, "expires_at": new_expiry.isoformat(), "status": "completed"}
            current_limit = current.data_limit_bytes
            if current_limit is None:
                current_limit = int(op.get("traffic_limit_bytes") or 0)
            if current_limit <= 0:
                raise CustomerStoreError("Unlimited or unknown traffic limit cannot be topped up safely")
            if expected_quote is not None:
                expected_limit = quote.get("before_traffic_limit_bytes")
                if isinstance(expected_limit, bool) or not isinstance(expected_limit, int) or int(expected_limit) != int(current_limit):
                    raise CustomerStoreError("The service traffic limit changed after preview; request a fresh quote")
            new_limit = int(current_limit) + int(op["quantity"])
            if new_limit <= int(current_limit):
                raise CustomerStoreError("Traffic limit overflow")
            if expected_quote is not None and quote.get("target_traffic_limit_bytes") != new_limit:
                raise CustomerStoreError("The calculated traffic limit differs from the confirmed quote; request a fresh preview")
            self.store.persist_service_change_target(
                operation_id=str(op["operation_id"]),
                before_expires_at=current.expire_at.isoformat() if current.expire_at else op.get("expires_at"),
                before_traffic_limit_bytes=int(current_limit),
                target_expires_at=None,
                target_traffic_limit_bytes=new_limit)
            mutation_started = True
            await adapter.update_user(connection, str(op["provider_user_id"]),
                                      UserUpdate(data_limit_bytes=new_limit))
            self.store.complete_service_change(operation_id=str(op["operation_id"]),
                expires_at=None, traffic_limit_bytes=new_limit)
            return {**op, "traffic_limit_bytes": new_limit, "status": "completed"}
        except Exception as exc:
            if mutation_started:
                try:
                    self.store.flag_service_change_reconciliation(
                        operation_id=str(op["operation_id"]), reason_code=type(exc).__name__)
                except Exception:
                    pass
                raise PurchaseNeedsReconciliation(
                    f"Service change {op['operation_id']} may have reached the provider; funds remain reserved for reconciliation"
                ) from None
            try:
                self.store.fail_service_change_before_provider(
                    operation_id=str(op["operation_id"]), reason_code=type(exc).__name__)
            except Exception:
                pass
            if isinstance(exc, CustomerStoreError):
                raise
            raise CustomerStoreError(f"Service change failed before provider mutation ({type(exc).__name__}); wallet reservation was reversed") from None

    async def renew(self, *, telegram_id: int, service_id: str,
                    idempotency_key: str, now: int | None = None) -> dict[str, Any]:
        return await self.change(telegram_id=telegram_id, service_id=service_id,
            change_type="renewal", idempotency_key=idempotency_key, now=now)

    async def top_up_traffic(self, *, telegram_id: int, service_id: str,
                             idempotency_key: str, now: int | None = None) -> dict[str, Any]:
        return await self.change(telegram_id=telegram_id, service_id=service_id,
            change_type="traffic_topup", idempotency_key=idempotency_key, now=now)
