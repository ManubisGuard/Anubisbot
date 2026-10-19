"""Privacy-safe trust-channel rendering and a SQLite-backed notification outbox."""
from __future__ import annotations
import json, sqlite3, time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

class TrustEvent(StrEnum):
    NEW_SUBSCRIPTION = "new_subscription"
    RENEWAL = "renewal"
    TRAFFIC_TOPUP = "traffic_topup"
    TIME_EXTENSION = "time_extension"
    WALLET_DEPOSIT = "wallet_deposit"
    RESELLER_CREATED = "reseller_created"
    PAYMENT_FAILED = "payment_failed"

@dataclass(frozen=True, slots=True)
class TrustChannelConfig:
    tenant_id: str
    bot_instance_id: str
    chat_id: str
    enabled: bool = True
    enabled_events: frozenset[TrustEvent] = frozenset({TrustEvent.NEW_SUBSCRIPTION, TrustEvent.RENEWAL, TrustEvent.TRAFFIC_TOPUP, TrustEvent.TIME_EXTENSION, TrustEvent.WALLET_DEPOSIT})
    privacy_mode: bool = True
    def __post_init__(self):
        if not self.tenant_id.strip() or not self.bot_instance_id.strip(): raise ValueError("tenant_id and bot_instance_id are required")
        if not self.chat_id.strip() or (not self.chat_id.lstrip("-").isdigit() and not self.chat_id.startswith("@")): raise ValueError("chat_id must be a numeric Telegram chat ID or @channel username")

@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    tenant_id: str
    bot_instance_id: str
    event_type: str
    idempotency_key: str
    payload_json: str
    status: str
    attempts: int
    available_at: float

# Arbitrary provider data is never rendered; only these fields are ever accepted.
def render_trust_message(event: TrustEvent, payload: dict[str, Any], *, privacy_mode: bool = True) -> str:
    labels = {TrustEvent.NEW_SUBSCRIPTION:"اشتراک جدید", TrustEvent.RENEWAL:"تمدید اشتراک", TrustEvent.TRAFFIC_TOPUP:"افزایش حجم", TrustEvent.TIME_EXTENSION:"افزایش اعتبار زمانی", TrustEvent.WALLET_DEPOSIT:"شارژ کیف پول", TrustEvent.RESELLER_CREATED:"ایجاد نمایندگی", TrustEvent.PAYMENT_FAILED:"پرداخت ناموفق"}
    lines = [f"📣 {labels[event]}"]
    if not privacy_mode:
        u = payload.get("username")
        if isinstance(u, str) and 0 < len(u) <= 64 and all(c.isalnum() or c in "_.-" for c in u): lines.append(f"کاربر: {u}")
    order = payload.get("public_order_id")
    if isinstance(order, str) and 0 < len(order) <= 40 and all(c.isalnum() or c in "_-" for c in order): lines.append(f"شناسه: {order}")
    amount, currency = payload.get("amount_minor"), payload.get("currency")
    if isinstance(amount, int) and amount >= 0 and isinstance(currency, str) and len(currency) <= 8: lines.append(f"مبلغ: {amount:,} {currency}")
    volume = payload.get("volume_gb")
    if isinstance(volume, (int,float)) and not isinstance(volume,bool) and 0 <= volume <= 1_000_000: lines.append(f"حجم: {volume:g} GB")
    days = payload.get("days")
    if isinstance(days,int) and 0 <= days <= 36500: lines.append(f"مدت: {days} روز")
    lines.append("🔒 اطلاعات محرمانه در این پیام نمایش داده نمی‌شود.")
    return "\n".join(lines)

class SQLiteNotificationOutbox:
    """SQLite outbox for local staging. Production enqueue must share the business transaction."""
    def __init__(self, database: str | Path): self.database=str(database); self._initialize()
    def _connect(self):
        c=sqlite3.connect(self.database, timeout=10, isolation_level=None); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=10000"); return c
    def _initialize(self):
        with self._connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, bot_instance_id TEXT NOT NULL, event_type TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','sent','dead')), attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, created_at REAL NOT NULL, sent_at REAL, claimed_at REAL, last_error_code TEXT, UNIQUE(tenant_id,bot_instance_id,idempotency_key))""")
            columns = {str(row[1]) for row in c.execute("PRAGMA table_info(notification_outbox)").fetchall()}
            if "claimed_at" not in columns:
                c.execute("ALTER TABLE notification_outbox ADD COLUMN claimed_at REAL")
            c.execute("CREATE INDEX IF NOT EXISTS ix_notification_outbox_due ON notification_outbox(status,available_at,id)")
    def enqueue(self, config: TrustChannelConfig, event: TrustEvent, payload: dict[str,Any], *, idempotency_key: str, now: float|None=None) -> int|None:
        if not config.enabled or event not in config.enabled_events: return None
        if not idempotency_key.strip() or len(idempotency_key)>200: raise ValueError("idempotency_key must be 1..200 characters")
        safe={k:payload[k] for k in ("public_order_id","username","amount_minor","currency","volume_gb","days") if k in payload}
        encoded=json.dumps(safe,ensure_ascii=False,separators=(",",":")); ts=time.time() if now is None else now; c=self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur=c.execute("INSERT OR IGNORE INTO notification_outbox(tenant_id,bot_instance_id,event_type,idempotency_key,payload_json,status,attempts,available_at,created_at) VALUES(?,?,?,?,?,'pending',0,?,?)",(config.tenant_id,config.bot_instance_id,event.value,idempotency_key,encoded,ts,ts))
            if cur.rowcount==1: row_id=int(cur.lastrowid)
            else: row_id=int(c.execute("SELECT id FROM notification_outbox WHERE tenant_id=? AND bot_instance_id=? AND idempotency_key=?",(config.tenant_id,config.bot_instance_id,idempotency_key)).fetchone()["id"])
            c.execute("COMMIT"); return row_id
        except Exception: c.execute("ROLLBACK"); raise
        finally: c.close()
    def claim_due(self, *, limit:int=20, now:float|None=None, lease_seconds:int=300)->list[OutboxMessage]:
        if not 1<=limit<=500: raise ValueError("limit must be between 1 and 500")
        if not 30 <= lease_seconds <= 3600: raise ValueError("lease_seconds must be between 30 and 3600")
        ts=time.time() if now is None else now; c=self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            # Recover work abandoned by a process crash. Old rows without claimed_at use
            # their enqueue timestamp as a conservative legacy recovery signal.
            c.execute("""UPDATE notification_outbox SET status='pending',claimed_at=NULL,available_at=?
                         WHERE status='processing' AND ((claimed_at IS NOT NULL AND claimed_at<=?)
                         OR (claimed_at IS NULL AND available_at<=?))""", (ts, ts-lease_seconds, ts-lease_seconds))
            rows=c.execute("SELECT * FROM notification_outbox WHERE status='pending' AND available_at<=? ORDER BY id LIMIT ?",(ts,limit)).fetchall()
            for r in rows: c.execute("UPDATE notification_outbox SET status='processing',attempts=attempts+1,claimed_at=? WHERE id=? AND status='pending'",(ts,r["id"]))
            c.execute("COMMIT")
            return [OutboxMessage(int(r["id"]),r["tenant_id"],r["bot_instance_id"],r["event_type"],r["idempotency_key"],r["payload_json"],"processing",int(r["attempts"])+1,float(r["available_at"])) for r in rows]
        except Exception: c.execute("ROLLBACK"); raise
        finally: c.close()
    def mark_sent(self, message_id:int, *, now:float|None=None):
        with self._connect() as c:
            cur=c.execute("UPDATE notification_outbox SET status='sent',sent_at=?,last_error_code=NULL WHERE id=? AND status='processing'",(time.time() if now is None else now,message_id))
            if cur.rowcount!=1: raise ValueError("message is not in processing state")
    def retry_or_dead(self,message_id:int,*,attempt:int,error_code:str,now:float|None=None,max_attempts:int=8):
        if not error_code or len(error_code)>80 or any(ch.isspace() for ch in error_code): raise ValueError("error_code must be a short non-sensitive code")
        ts=time.time() if now is None else now; status="dead" if attempt>=max_attempts else "pending"; next_at=ts if status=="dead" else ts+min(3600,2**min(max(attempt,1),10))
        with self._connect() as c:
            cur=c.execute("UPDATE notification_outbox SET status=?,available_at=?,last_error_code=? WHERE id=? AND status='processing'",(status,next_at,error_code,message_id))
            if cur.rowcount!=1: raise ValueError("message is not in processing state")
    def get(self,message_id:int)->OutboxMessage|None:
        with self._connect() as c: r=c.execute("SELECT * FROM notification_outbox WHERE id=?",(message_id,)).fetchone()
        if not r:return None
        return OutboxMessage(int(r["id"]),r["tenant_id"],r["bot_instance_id"],r["event_type"],r["idempotency_key"],r["payload_json"],r["status"],int(r["attempts"]),float(r["available_at"]))

class TrustChannelSender:
    """Small port implemented by Telegram Bot API client wrappers."""
    async def send_message(self, chat_id: str, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class TrustChannelDeliveryWorker:
    """Deliver claimed outbox messages using tenant-scoped config and sender lookup."""
    def __init__(self, outbox: SQLiteNotificationOutbox, config_lookup, sender_lookup, *, max_attempts: int = 8):
        self.outbox = outbox
        self.config_lookup = config_lookup
        self.sender_lookup = sender_lookup
        self.max_attempts = max_attempts

    async def deliver_batch(self, *, limit: int = 20, now: float | None = None) -> dict[str, int]:
        counts = {"sent": 0, "retry": 0, "dead": 0, "skipped": 0}
        for message in self.outbox.claim_due(limit=limit, now=now):
            try:
                config = self.config_lookup(message.tenant_id, message.bot_instance_id)
                if config is None or not config.enabled or TrustEvent(message.event_type) not in config.enabled_events:
                    self.outbox.retry_or_dead(message.id, attempt=self.max_attempts, error_code="config_disabled", now=now, max_attempts=self.max_attempts)
                    counts["dead"] += 1
                    continue
                sender = self.sender_lookup(message.tenant_id, message.bot_instance_id)
                if sender is None:
                    raise RuntimeError("sender_unavailable")
                payload = json.loads(message.payload_json)
                body = render_trust_message(TrustEvent(message.event_type), payload, privacy_mode=config.privacy_mode)
                await sender.send_message(config.chat_id, body)
                self.outbox.mark_sent(message.id, now=now)
                counts["sent"] += 1
            except Exception as exc:
                code = str(exc) if str(exc).replace("_", "").isalnum() and len(str(exc)) <= 60 else type(exc).__name__.lower()
                self.outbox.retry_or_dead(message.id, attempt=message.attempts, error_code=code, now=now, max_attempts=self.max_attempts)
                if message.attempts >= self.max_attempts:
                    counts["dead"] += 1
                else:
                    counts["retry"] += 1
        return counts
