"""幂等支持：同一请求键重复到达返回同一结果，载荷变化识别为冲突。

用法（必须在调用方自己的事务内，配合 with_for_update 使用）：

    guard = begin_idempotent(db, scope, key, payload_dict, volunteer_id)
    if guard.replayed:
        return guard.cached_response          # 直接回放首次结果
    # ...执行业务，得到 result...
    guard.finish("exchange", exchange.id, result)
    db.commit()
"""
import hashlib
import json
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

import models


class PayloadConflict(Exception):
    """同一幂等键再次到达但载荷指纹不同 —— 409 冲突。"""

    def __init__(self, existing: models.IdempotentRequest):
        self.existing = existing
        super().__init__("相同请求键的载荷与首次请求不一致")


class RequestInFlight(Exception):
    """同一请求仍在另一请求中处理，等待超时仍未落定。"""


_SETTLE_WAIT_SECONDS = 15.0
# 进行中的占位登记超过该秒数仍未落定，视为首个请求已崩溃，允许后来者接管同一键
_STALE_AFTER_SECONDS = 60.0


def hash_payload(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotentGuard:
    def __init__(self, db: Session, record: Optional[models.IdempotentRequest],
                 scope: str, request_key: str):
        self.db = db
        self.record = record
        self.scope = scope
        self.request_key = request_key
        self.replayed = record is not None and record.response_body is not None

    @property
    def cached_status_code(self) -> int:
        return self.record.response_status or 200

    @property
    def cached_response(self) -> Any:
        if self.record is None or self.record.response_body is None:
            return None
        return json.loads(self.record.response_body)

    def finish(self, resource_type: str, resource_id: Optional[int],
               response_body: Any, status_code: int = 200) -> None:
        """业务成功后把结果落定到幂等登记（随调用方事务一起提交）。"""
        self.record.resource_type = resource_type
        self.record.resource_id = resource_id
        self.record.response_status = status_code
        self.record.response_body = json.dumps(
            response_body, ensure_ascii=False, default=str)


def begin_idempotent(db: Session, scope: str, request_key: str,
                     payload: dict, volunteer_id: Optional[int] = None,
                     wait_seconds: float = _SETTLE_WAIT_SECONDS) -> IdempotentGuard:
    """登记/取回幂等请求。

    - 无记录：插入一条占位（IN-FLIGHT）登记并返回 replayed=False
    - 有记录且载荷一致、已落定：返回 replayed=True，由调用方回放 cached_response
    - 有记录但载荷指纹不同：抛 PayloadConflict（409）
    - 有记录但仍在处理（首个请求尚未提交）：等待其落定后回放；
      超时抛 RequestInFlight（崩溃残留可由后台清理）

    唯一约束 (scope, request_key) 保证并发同键请求只有一个插入成功；
    并发落败方会走进等待分支，最终看到同一结果。
    """
    payload_hash = hash_payload(payload)
    deadline = time.monotonic() + wait_seconds

    while True:
        existing = db.query(models.IdempotentRequest).filter(
            models.IdempotentRequest.scope == scope,
            models.IdempotentRequest.request_key == request_key,
        ).first()

        if existing is None:
            record = models.IdempotentRequest(
                request_key=request_key,
                scope=scope,
                volunteer_id=volunteer_id,
                payload_hash=payload_hash,
            )
            db.add(record)
            db.flush()  # 触发唯一约束，拦截并发同键请求
            return IdempotentGuard(db, record, scope, request_key)

        if existing.payload_hash != payload_hash:
            raise PayloadConflict(existing)

        if existing.response_body is not None:
            return IdempotentGuard(db, existing, scope, request_key)

        # 首个请求仍在处理：若其登记已陈旧（进程崩溃/长时间挂起），接管该键
        if existing.created_at is not None:
            age = (datetime.utcnow() - existing.created_at).total_seconds()
            if age > _STALE_AFTER_SECONDS:
                db.delete(existing)
                db.flush()
                continue

        # 否则释放快照、让出连接，轮询等待其提交
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestInFlight()
        db.rollback()
        time.sleep(min(0.05, remaining))
