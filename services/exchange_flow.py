"""兑换预占流程编排（可恢复 Saga）。

状态机：
  RESERVED（预占：积分 FREEZE、库存 RESERVE）
    ├─ confirm ───────────────► CONFIRMED（实物待发货；优先券发资格并完成结算）
    │       └─ fulfill(n) ────► FULFILLED / PARTIALLY_FULFILLED（部分发货，其余释放）
    ├─ reject  ───────────────► REJECTED（UNFREEZE 全退积分 + RELEASE 全退库存）
    ├─ cancel  ───────────────► CANCELLED（同上，原因为用户取消）
    └─ timeout ───────────────► CANCELLED（同上，原因为超时未确认）

每个对外动作都支持幂等键；阶段流水本身也带唯一幂等键，因此任何步骤崩溃后
重试都能安全恢复，不会出现"积分已扣但库存不足"或"取消后多退积分"。
"""
from datetime import datetime, timedelta
from typing import Optional
import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import models
from services import ledger
from services.idempotency import (
    begin_idempotent, PayloadConflict, RequestInFlight)


class ExchangeError(HTTPException):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(status_code=status_code, detail=detail)


_TERMINAL_STATUSES = {
    models.ExchangeStatus.FULFILLED,
    models.ExchangeStatus.REJECTED,
    models.ExchangeStatus.CANCELLED,
}


def _fingerprint(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _begin_guard(db: Session, scope: str, key: str, payload: dict,
                 volunteer_id: Optional[int] = None):
    """打开幂等守卫；并发同键插入冲突时回滚重读（此时尚无其他业务写入）。"""
    try:
        return begin_idempotent(db, scope, key, payload, volunteer_id)
    except PayloadConflict:
        raise ExchangeError(f"请求冲突：幂等键 {key} 已用于不同载荷", status_code=409)
    except RequestInFlight:
        raise ExchangeError(
            "相同请求正在处理中且长时间未完成，请稍后凭原幂等键重试", status_code=503)
    except IntegrityError:
        db.rollback()
        try:
            return begin_idempotent(db, scope, key, payload, volunteer_id)
        except PayloadConflict:
            raise ExchangeError(
                f"请求冲突：幂等键 {key} 已用于不同载荷", status_code=409)
        except RequestInFlight:
            raise ExchangeError(
                "相同请求正在处理中且长时间未完成，请稍后凭原幂等键重试", status_code=503)


def _load_exchange(db: Session, exchange_id: int, lock: bool = True
                   ) -> models.BenefitExchange:
    q = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id)
    if lock:
        q = q.with_for_update()
    exchange = q.first()
    if not exchange:
        raise ExchangeError("兑换记录不存在", status_code=404)
    return exchange


def _assert_status(exchange: models.BenefitExchange, allowed: set, action: str):
    if exchange.status not in allowed:
        raise ExchangeError(
            f"当前状态「{exchange.status.value}」不允许{action}（允许状态："
            f"{'、'.join(s.value for s in allowed)}）")


def _remaining_reserved(exchange: models.BenefitExchange) -> int:
    """尚未处理的预占数量 = 总数量 - 已履约 - 已释放（释放按退还积分折算）。"""
    released_units = (exchange.points_refunded // exchange.points_cost
                      if exchange.points_cost else 0)
    return exchange.quantity - exchange.fulfilled_quantity - released_units


def _release_reserved(db: Session, exchange: models.BenefitExchange,
                      benefit: models.Benefit, volunteer: models.Volunteer,
                      units: int, reason: models.ExchangeReleaseReason) -> None:
    """释放指定份数的冻结积分与预占库存（拒绝/取消/超时/部分短缺共用）。

    流水幂等键含原因与份数，重复调用不会多退。
    """
    points = exchange.points_cost * units
    ledger.unfreeze_points(db, volunteer, points, exchange.id, reason,
                           f"{reason.value}释放 {benefit.name} x{units}")
    if ledger.stock_tracked(benefit):
        ledger.release_stock(db, benefit.id, units, exchange.id, reason)
    exchange.points_refunded += points


# ------------------------------------------------------------- 申请预占

def apply_exchange(db: Session, *, volunteer_id: int, benefit_id: int, quantity: int,
                   delivery_info: Optional[str] = None, notes: Optional[str] = None,
                   idempotency_key: Optional[str] = None) -> models.BenefitExchange:
    if quantity <= 0:
        raise ExchangeError("兑换数量必须大于 0")

    key = (idempotency_key
           or f"auto:apply:{volunteer_id}:{benefit_id}:{quantity}:"
              f"{_fingerprint(delivery_info, notes)}")
    payload = {"volunteer_id": volunteer_id, "benefit_id": benefit_id,
               "quantity": quantity, "delivery_info": delivery_info, "notes": notes}
    guard = _begin_guard(db, "exchange:apply", key, payload, volunteer_id)
    if guard.replayed:
        return _load_exchange(db, guard.record.resource_id, lock=False)

    # 志愿者行 + 商品行加锁：积分与库存两个串行化点在同一事务内原子完成
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == volunteer_id).with_for_update().first()
    if not volunteer:
        raise ExchangeError("志愿者不存在", status_code=404)

    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == benefit_id).with_for_update().first()
    if not benefit:
        raise ExchangeError("权益不存在", status_code=404)
    if not benefit.is_active:
        raise ExchangeError("该权益已下架")

    total_points = benefit.points_cost * quantity
    if (volunteer.points_balance or 0) < total_points:
        raise ExchangeError("可用积分不足")
    # 不限量权益 stock == 0；限量权益须有足够可售库存
    if benefit.stock > 0 and ledger.available_stock(db, benefit) < quantity:
        raise ExchangeError("库存不足")

    exchange = models.BenefitExchange(
        volunteer_id=volunteer_id, benefit_id=benefit_id,
        points_cost=benefit.points_cost, points_spent=total_points,
        quantity=quantity, status=models.ExchangeStatus.RESERVED,
        reserve_expires_at=datetime.utcnow() + timedelta(
            seconds=benefit.reserve_timeout_seconds or 900),
        delivery_info=delivery_info, notes=notes,
    )
    db.add(exchange)
    db.flush()  # 取得 exchange.id 供流水幂等键使用

    try:
        ledger.freeze_points(db, volunteer, total_points, exchange.id,
                             f"预占兑换 {benefit.name} x{quantity}")
        ledger.reserve_stock(db, benefit, quantity, exchange.id)
    except ledger.LedgerError as exc:
        db.rollback()
        raise ExchangeError(str(exc))

    guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
    db.commit()
    db.refresh(exchange)
    return exchange


# ------------------------------------------------------------- 确认/履约

def confirm_exchange(db: Session, exchange_id: int, *,
                     fulfill_quantity: Optional[int] = None,
                     delivery_info: Optional[str] = None,
                     notes: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> models.BenefitExchange:
    key = idempotency_key or f"auto:confirm:{exchange_id}:{fulfill_quantity}"
    payload = {"fulfill_quantity": fulfill_quantity,
               "delivery_info": delivery_info, "notes": notes}
    guard = _begin_guard(db, "exchange:confirm", key, payload)
    if guard.replayed:
        return _load_exchange(db, guard.record.resource_id or exchange_id, lock=False)

    exchange = _load_exchange(db, exchange_id)
    if exchange.status in _TERMINAL_STATUSES:
        # 延迟重复/网络重放到达：返回同一终态，不再发任何资源
        guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
        db.commit()
        return exchange

    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == exchange.benefit_id).with_for_update().first()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == exchange.volunteer_id).with_for_update().first()

    if exchange.status == models.ExchangeStatus.RESERVED:
        _assert_status(exchange, {models.ExchangeStatus.RESERVED}, "确认")
        exchange.status = models.ExchangeStatus.CONFIRMED
        exchange.confirmed_at = datetime.utcnow()
        if delivery_info:
            exchange.delivery_info = delivery_info
        if notes:
            exchange.notes = notes

        # 确认时未指定发货量：
        #  - 优先时段券：发资格即完成积分消费与库存履约（资格在真正认领时才消费）
        #  - 实物/其他：停在 CONFIRMED，库存继续保留为已承诺，等待发货
        if fulfill_quantity is None:
            if benefit.benefit_type == models.BenefitType.PRIORITY_SLOT:
                _issue_entitlements_and_settle(db, exchange, benefit, volunteer,
                                               exchange.quantity)
        else:
            _fulfill(db, exchange, benefit, volunteer, fulfill_quantity)
    else:
        # CONFIRMED / PARTIALLY_FULFILLED 下带发货量的重复确认 = 补发货
        if fulfill_quantity is not None:
            _fulfill(db, exchange, benefit, volunteer, fulfill_quantity)

    guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
    db.commit()
    db.refresh(exchange)
    return exchange


def fulfill_exchange(db: Session, exchange_id: int, fulfill_quantity: int,
                     idempotency_key: Optional[str] = None) -> models.BenefitExchange:
    """后台实物发货：可一次发清，也可在允许时分批/部分履约。"""
    key = idempotency_key or f"auto:fulfill:{exchange_id}:{fulfill_quantity}"
    guard = _begin_guard(db, "exchange:fulfill", key,
                         {"fulfill_quantity": fulfill_quantity})
    if guard.replayed:
        return _load_exchange(db, guard.record.resource_id or exchange_id, lock=False)

    exchange = _load_exchange(db, exchange_id)
    _assert_status(exchange, {models.ExchangeStatus.CONFIRMED,
                              models.ExchangeStatus.PARTIALLY_FULFILLED}, "发货")
    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == exchange.benefit_id).with_for_update().first()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == exchange.volunteer_id).with_for_update().first()

    _fulfill(db, exchange, benefit, volunteer, fulfill_quantity)

    guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
    db.commit()
    db.refresh(exchange)
    return exchange


def _validate_fulfill_qty(exchange: models.BenefitExchange, benefit: models.Benefit,
                          qty: int) -> tuple[int, Optional[models.ExchangeReleaseReason]]:
    remaining = _remaining_reserved(exchange)
    if qty <= 0 or qty > remaining:
        raise ExchangeError(f"发货数量须在 1~{remaining} 之间")
    if qty < remaining and not benefit.allow_partial_fulfillment:
        raise ExchangeError("该实物权益不允许部分履约，必须一次发清或拒绝整单")
    reason = (models.ExchangeReleaseReason.PARTIAL_SHORTAGE
              if qty < remaining else None)
    return qty, reason


def _fulfill(db: Session, exchange: models.BenefitExchange, benefit: models.Benefit,
             volunteer: models.Volunteer, qty: int) -> None:
    """本次发 qty 件：CONSUME 对应积分、FULFILL 库存承诺；
    若为收尾且仍有剩余预占（部分履约），按"库存不足"明确释放剩余资源。"""
    qty, shortage_reason = _validate_fulfill_qty(exchange, benefit, qty)
    unit = exchange.points_cost

    ledger.consume_points(db, volunteer, unit * qty, exchange.id,
                          f"履约发货 {benefit.name} x{qty}")
    ledger.fulfill_stock(db, benefit, qty, exchange.id)
    exchange.points_consumed += unit * qty
    exchange.fulfilled_quantity += qty

    remaining = _remaining_reserved(exchange)
    if remaining > 0 and shortage_reason is not None:
        _release_reserved(db, exchange, benefit, volunteer, remaining, shortage_reason)
        exchange.status = models.ExchangeStatus.PARTIALLY_FULFILLED
        exchange.fulfilled_at = datetime.utcnow()
    elif remaining == 0:
        exchange.status = models.ExchangeStatus.FULFILLED
        exchange.fulfilled_at = datetime.utcnow()
    else:
        exchange.status = models.ExchangeStatus.PARTIALLY_FULFILLED


# ------------------------------------------------------------- 拒绝/取消

def reject_exchange(db: Session, exchange_id: int, reason_text: Optional[str] = None,
                    idempotency_key: Optional[str] = None) -> models.BenefitExchange:
    key = idempotency_key or f"auto:reject:{exchange_id}"
    guard = _begin_guard(db, "exchange:reject", key, {"reason": reason_text})
    if guard.replayed:
        return _load_exchange(db, guard.record.resource_id or exchange_id, lock=False)

    exchange = _load_exchange(db, exchange_id)
    # 后台拒绝可发生在预占后，或确认后尚未发出任何一件实物时
    _assert_status(exchange, {models.ExchangeStatus.RESERVED,
                              models.ExchangeStatus.CONFIRMED}, "拒绝")
    if exchange.fulfilled_quantity > 0:
        raise ExchangeError("已发生履约的兑换单不能整单拒绝，请按部分履约/补偿处理")
    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == exchange.benefit_id).with_for_update().first()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == exchange.volunteer_id).with_for_update().first()

    remaining = _remaining_reserved(exchange)
    _release_reserved(db, exchange, benefit, volunteer, remaining,
                      models.ExchangeReleaseReason.REJECTED)
    exchange.status = models.ExchangeStatus.REJECTED
    exchange.close_reason = models.ExchangeReleaseReason.REJECTED
    if reason_text:
        exchange.notes = (exchange.notes or "") + f"｜拒绝原因：{reason_text}"

    guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
    db.commit()
    db.refresh(exchange)
    return exchange


def cancel_exchange(db: Session, exchange_id: int, reason_text: Optional[str] = None,
                    idempotency_key: Optional[str] = None) -> models.BenefitExchange:
    key = idempotency_key or f"auto:cancel:{exchange_id}"
    guard = _begin_guard(db, "exchange:cancel", key, {"reason": reason_text})
    if guard.replayed:
        return _load_exchange(db, guard.record.resource_id or exchange_id, lock=False)

    exchange = _load_exchange(db, exchange_id)
    _assert_status(exchange, {models.ExchangeStatus.RESERVED}, "取消")
    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == exchange.benefit_id).with_for_update().first()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == exchange.volunteer_id).with_for_update().first()

    reason = models.ExchangeReleaseReason.USER_CANCELLED
    if reason_text and "超时" in reason_text:
        reason = models.ExchangeReleaseReason.TIMEOUT
    remaining = _remaining_reserved(exchange)
    _release_reserved(db, exchange, benefit, volunteer, remaining, reason)
    exchange.status = models.ExchangeStatus.CANCELLED
    exchange.close_reason = reason
    if reason_text:
        exchange.notes = (exchange.notes or "") + f"｜取消原因：{reason_text}"

    guard.finish("exchange", exchange.id, {"exchange_id": exchange.id})
    db.commit()
    db.refresh(exchange)
    return exchange


# ------------------------------------------------------------- 超时回收

def expire_timed_out_reservations(db: Session, now: datetime = None) -> list[int]:
    """回收所有已过期的预占单。每单独立事务，单条失败不影响其他单（可恢复）。"""
    now = now or datetime.utcnow()
    expired_ids = [row[0] for row in db.query(models.BenefitExchange.id).filter(
        models.BenefitExchange.status == models.ExchangeStatus.RESERVED,
        models.BenefitExchange.reserve_expires_at.isnot(None),
        models.BenefitExchange.reserve_expires_at < now,
    ).all()]

    recovered = []
    for exchange_id in expired_ids:
        try:
            _expire_one(db, exchange_id)
            recovered.append(exchange_id)
        except Exception:
            db.rollback()
    return recovered


def _expire_one(db: Session, exchange_id: int) -> None:
    exchange = _load_exchange(db, exchange_id)
    if exchange.status != models.ExchangeStatus.RESERVED:
        db.rollback()
        return
    if exchange.reserve_expires_at and exchange.reserve_expires_at >= datetime.utcnow():
        db.rollback()
        return
    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == exchange.benefit_id).with_for_update().first()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == exchange.volunteer_id).with_for_update().first()
    remaining = _remaining_reserved(exchange)
    _release_reserved(db, exchange, benefit, volunteer, remaining,
                      models.ExchangeReleaseReason.TIMEOUT)
    exchange.status = models.ExchangeStatus.CANCELLED
    exchange.close_reason = models.ExchangeReleaseReason.TIMEOUT
    db.commit()


# ------------------------------------------------------------- 优先时段资格

def _issue_entitlements_and_settle(db: Session, exchange: models.BenefitExchange,
                                   benefit: models.Benefit, volunteer: models.Volunteer,
                                   qty: int) -> None:
    """确认优先时段券：发放 qty 张资格，积分此时消费、库存此时履约；
    但资格本身要等真正认领时段时才消费（USED）。"""
    ledger.consume_points(db, volunteer, exchange.points_cost * qty, exchange.id,
                          f"确认兑换优先时段券 {benefit.name} x{qty}")
    ledger.fulfill_stock(db, benefit, qty, exchange.id)
    exchange.points_consumed += exchange.points_cost * qty
    exchange.fulfilled_quantity += qty
    for i in range(qty):
        db.add(models.PriorityEntitlement(
            volunteer_id=exchange.volunteer_id,
            benefit_id=benefit.id,
            exchange_id=exchange.id,
            code=f"PRI-{exchange.id:08d}-{i + 1:02d}-{uuid.uuid4().hex[:6].upper()}",
            status=models.EntitlementStatus.ISSUED,
        ))
    exchange.status = models.ExchangeStatus.FULFILLED
    exchange.fulfilled_at = datetime.utcnow()


def consume_entitlement(db: Session, *, volunteer_id: int, time_slot_id: int,
                        idempotency_key: Optional[str] = None
                        ) -> models.PriorityEntitlement:
    """真正认领优先时段时消费一张资格券（资格的延迟消费点）。"""
    key = idempotency_key or f"auto:entitlement:{volunteer_id}:{time_slot_id}"
    guard = _begin_guard(db, "entitlement:consume", key,
                         {"volunteer_id": volunteer_id, "time_slot_id": time_slot_id},
                         volunteer_id)
    if guard.replayed:
        return db.query(models.PriorityEntitlement).filter(
            models.PriorityEntitlement.id == guard.record.resource_id
        ).with_for_update().first()

    slot = db.query(models.TimeSlot).filter(
        models.TimeSlot.id == time_slot_id).with_for_update().first()
    if not slot:
        raise ExchangeError("时段不存在", status_code=404)
    if slot.status in (models.TimeSlotStatus.CANCELLED, models.TimeSlotStatus.COMPLETED):
        raise ExchangeError("该时段已取消或已完成，不能使用优先券")
    if slot.status == models.TimeSlotStatus.CLAIMED and slot.volunteer_id != volunteer_id:
        raise ExchangeError("该时段已被他人认领", status_code=409)

    entitlement = db.query(models.PriorityEntitlement).filter(
        models.PriorityEntitlement.volunteer_id == volunteer_id,
        models.PriorityEntitlement.status == models.EntitlementStatus.ISSUED,
    ).order_by(models.PriorityEntitlement.created_at.asc()).with_for_update().first()
    if not entitlement:
        raise ExchangeError("没有可用的优先时段券")

    entitlement.status = models.EntitlementStatus.USED
    entitlement.time_slot_id = time_slot_id
    entitlement.used_at = datetime.utcnow()

    if slot.status == models.TimeSlotStatus.AVAILABLE:
        slot.status = models.TimeSlotStatus.CLAIMED
        slot.volunteer_id = volunteer_id

    guard.finish("entitlement", entitlement.id, {"entitlement_id": entitlement.id})
    db.commit()
    db.refresh(entitlement)
    return entitlement


# ------------------------------------------------------------- 人工补偿

def manual_compensate(db: Session, *, exchange_id: Optional[int], volunteer_id: int,
                      benefit_id: Optional[int], points_delta: int,
                      inventory_delta: int, reason: str,
                      operator: Optional[str]) -> models.ManualCompensation:
    """人工更正：只能追加补偿记录与补偿流水，绝不回改原始兑换单/流水。"""
    if not reason:
        raise ExchangeError("补偿原因必填")
    if points_delta == 0 and inventory_delta == 0:
        raise ExchangeError("积分补偿与库存补偿不能同时为 0")

    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == volunteer_id).with_for_update().first()
    if not volunteer:
        raise ExchangeError("志愿者不存在", status_code=404)

    benefit = None
    if benefit_id:
        benefit = db.query(models.Benefit).filter(
            models.Benefit.id == benefit_id).with_for_update().first()
        if not benefit:
            raise ExchangeError("权益不存在", status_code=404)

    if exchange_id:
        exchange = db.query(models.BenefitExchange).filter(
            models.BenefitExchange.id == exchange_id).with_for_update().first()
        if not exchange:
            raise ExchangeError("兑换记录不存在", status_code=404)

    comp = models.ManualCompensation(
        exchange_id=exchange_id, volunteer_id=volunteer_id, benefit_id=benefit_id,
        points_delta=points_delta, inventory_delta=inventory_delta,
        reason=reason, operator=operator)
    db.add(comp)
    db.flush()

    try:
        if points_delta:
            ledger.manual_adjust_points(db, volunteer, points_delta, comp.id, reason)
        if inventory_delta and benefit is not None:
            inv = ledger.adjust_stock(db, benefit, inventory_delta, comp.id)
            inv.exchange_id = exchange_id
    except ledger.LedgerError as exc:
        db.rollback()
        raise ExchangeError(str(exc))

    db.commit()
    db.refresh(comp)
    return comp
