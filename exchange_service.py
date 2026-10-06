"""兑换可恢复预占流程（reserve → confirm → fulfill / release）。

账本原则（全部在同一事务内提交，事务由 database.py 以 BEGIN IMMEDIATE 开启）：

积分（volunteers 列 + points_records 流水）：
  points_balance  = 账户总额 = 可用 + 冻结
  points_frozen   = 冻结额
  可用积分        = points_balance - points_frozen
  流水恒等式：
    balance = ΣEARN + ΣSETTLE_REFUND − ΣSPEND − ΣSETTLE_SPEND
    frozen  = ΣFREEZE − ΣUNFREEZE − ΣSETTLE_SPEND

库存（benefits 列 + stock_ledger 流水）：
  stock             = 可售库存（可被新申请预占）
  committed_quantity= 已承诺数量（已预占未履约）
  守恒：初始库存(含补偿调整) = stock + committed + 已履约数量

单笔兑换数量账：quantity = reserved_quantity + fulfilled_quantity + released_quantity
单笔兑换积分账：points_spent = points_frozen + points_settled + points_released
  （refunded 为结算后退回，净消费 = settled − refunded）
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

import models

# 预占保留时长：家长申请后必须在此时限内被后台确认，否则按超时明确释放。
RESERVE_TTL_MINUTES = 15


class ExchangeError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 积分账本原语（只写流水 + 改列，不提交；提交由各状态机动作统一完成）
# ---------------------------------------------------------------------------

def _points_line(db, volunteer, ptype, amount, source, description, ex, event):
    record = models.PointsRecord(
        volunteer_id=volunteer.id,
        points_type=ptype,
        points_amount=amount,
        source=source,
        description=description,
        exchange_id=ex.id if ex else None,
        exchange_event_id=event.id if event else None,
    )
    db.add(record)
    return record


def _freeze_points(db, volunteer, amount, ex, event):
    volunteer.points_frozen = (volunteer.points_frozen or 0) + amount
    _points_line(db, volunteer, models.PointsType.FREEZE, amount,
                 models.PointsSource.EXCHANGE_RESERVE,
                 f"兑换预占冻结 {amount} 积分（兑换#{ex.id}）", ex, event)


def _unfreeze_points(db, volunteer, amount, ex, event, reason_desc):
    if (volunteer.points_frozen or 0) < amount:
        raise ExchangeError(500, "积分冻结账不平：试图解冻超过冻结额")
    volunteer.points_frozen -= amount
    _points_line(db, volunteer, models.PointsType.UNFREEZE, amount,
                 models.PointsSource.EXCHANGE_RELEASE,
                 f"{reason_desc}，解冻 {amount} 积分（兑换#{ex.id}）", ex, event)


def _settle_spend(db, volunteer, amount, ex, event):
    if (volunteer.points_frozen or 0) < amount:
        raise ExchangeError(500, "积分冻结账不平：试图结算超过冻结额")
    volunteer.points_frozen -= amount
    volunteer.points_balance = (volunteer.points_balance or 0) - amount
    _points_line(db, volunteer, models.PointsType.SETTLE_SPEND, amount,
                 models.PointsSource.EXCHANGE_SETTLE,
                 f"确认兑换，结算消费 {amount} 积分（兑换#{ex.id}）", ex, event)


def _settle_refund(db, volunteer, amount, ex, event, source, description):
    # 已结算后的退回（部分履约少发 / 人工补偿）：直接退回可用余额。
    volunteer.points_balance = (volunteer.points_balance or 0) + amount
    _points_line(db, volunteer, models.PointsType.SETTLE_REFUND, amount,
                 source, description, ex, event)


# ---------------------------------------------------------------------------
# 库存账本原语
# ---------------------------------------------------------------------------

def _stock_move(db, benefit, ex, change_type, stock_delta, committed_delta,
                event, operator=None, remark=None):
    if (benefit.stock or 0) + stock_delta < 0:
        raise ExchangeError(500, "库存账不平：可售库存将变为负数")
    if (benefit.committed_quantity or 0) + committed_delta < 0:
        raise ExchangeError(500, "库存账不平：已承诺数量将变为负数")

    # 兜底：若该权益还没有任何流水（历史数据/直接建档），先补记初始基线。
    if not db.query(models.StockLedger.id).filter(
            models.StockLedger.benefit_id == benefit.id).first():
        record_initial_stock(db, benefit, remark="初始入库（首次变动补记）")

    benefit.stock = (benefit.stock or 0) + stock_delta
    benefit.committed_quantity = (benefit.committed_quantity or 0) + committed_delta
    db.add(models.StockLedger(
        benefit_id=benefit.id,
        exchange_id=ex.id if ex else None,
        change_type=change_type,
        quantity=stock_delta,
        committed_delta=committed_delta,
        stock_after=benefit.stock,
        committed_after=benefit.committed_quantity,
        event_id=event.id if event else None,
        operator=operator,
        remark=remark,
    ))


def record_initial_stock(db, benefit, operator=None, remark="初始入库"):
    """权益建档时把初始可售量记入流水，使库存可从流水逐笔还原。幂等：已有流水则跳过。"""
    exists = db.query(models.StockLedger).filter(
        models.StockLedger.benefit_id == benefit.id
    ).first()
    if exists:
        return
    initial = benefit.stock or 0
    db.add(models.StockLedger(
        benefit_id=benefit.id, exchange_id=None,
        change_type=models.StockChangeType.INITIAL,
        quantity=initial, committed_delta=0,
        stock_after=initial, committed_after=0,
        operator=operator, remark=remark))


def _add_event(db, ex, event_type, to_status, quantity=0, points_amount=0,
               reason=None, detail=None, operator=None):
    event = models.ExchangeEvent(
        exchange_id=ex.id,
        event_type=event_type,
        from_status=ex.status,
        to_status=to_status,
        quantity=quantity,
        points_amount=points_amount,
        reason=reason,
        detail=detail,
        operator=operator,
    )
    db.add(event)
    db.flush()
    if to_status is not None:
        ex.status = to_status
    return event


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def payload_fingerprint(volunteer_id: int, benefit_id: int, quantity: int,
                        delivery_info: Optional[str], notes: Optional[str]) -> str:
    raw = json.dumps(
        {"volunteer_id": volunteer_id, "benefit_id": benefit_id,
         "quantity": quantity, "delivery_info": delivery_info, "notes": notes},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_exchange(db, exchange_id: int) -> models.BenefitExchange:
    ex = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.id == exchange_id
    ).first()
    if not ex:
        raise ExchangeError(404, "兑换记录不存在")
    return ex


def _is_reserved(ex) -> bool:
    return ex.status in (models.ExchangeStatus.RESERVED, models.ExchangeStatus.PENDING)


# ---------------------------------------------------------------------------
# 超时扫描（任何写接口进入前先执行，保证超时单按 TIMEOUT 原因被明确释放）
# ---------------------------------------------------------------------------

def sweep_expired(db, commit: bool = True) -> int:
    now = datetime.utcnow()
    rows = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.RESERVED, models.ExchangeStatus.PENDING
        ]),
        models.BenefitExchange.expires_at.isnot(None),
        models.BenefitExchange.expires_at <= now,
    ).all()
    for ex in rows:
        _release(db, ex, models.ExchangeEventType.TIMEOUT,
                 models.ReleaseReason.TIMEOUT,
                 models.ExchangeStatus.TIMEOUT_CANCELLED)
    if rows and commit:
        db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 1) 申请：原子锁定积分（冻结）与库存（预占）
# ---------------------------------------------------------------------------

def reserve_exchange(db, *, volunteer_id: int, benefit_id: int, quantity: int,
                     delivery_info: Optional[str] = None, notes: Optional[str] = None,
                     request_no: Optional[str] = None) -> models.BenefitExchange:
    sweep_expired(db, commit=False)

    request_no = request_no or f"auto-{uuid.uuid4().hex}"
    fingerprint = payload_fingerprint(volunteer_id, benefit_id, quantity,
                                      delivery_info, notes)

    # 幂等：相同 request_no 重复到达必须返回同一结果。
    existing = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.request_no == request_no
    ).first()
    if existing:
        if existing.request_payload_hash != fingerprint:
            raise ExchangeError(409, "相同请求编号的申请载荷不一致，判定为冲突请求")
        return existing

    if quantity is None or quantity <= 0:
        raise ExchangeError(400, "兑换数量必须大于0")

    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == volunteer_id
    ).first()
    if not volunteer:
        raise ExchangeError(404, "志愿者不存在")

    benefit = db.query(models.Benefit).filter(
        models.Benefit.id == benefit_id
    ).first()
    if not benefit:
        raise ExchangeError(404, "权益不存在")
    if not benefit.is_active:
        raise ExchangeError(400, "该权益已下架")

    total_points = benefit.points_cost * quantity
    # 先校验库存再校验积分，报错原因与实际资源状况一致。
    if (benefit.stock or 0) < quantity:
        raise ExchangeError(
            400, f"库存不足：可售 {benefit.stock}，需 {quantity}")
    available_points = (volunteer.points_balance or 0) - (volunteer.points_frozen or 0)
    if available_points < total_points:
        raise ExchangeError(400, f"积分不足：可用 {available_points}，需 {total_points}")

    ex = models.BenefitExchange(
        volunteer_id=volunteer_id,
        benefit_id=benefit_id,
        points_spent=total_points,
        status=models.ExchangeStatus.RESERVED,
        quantity=quantity,
        request_no=request_no,
        request_payload_hash=fingerprint,
        reserved_quantity=quantity,
        points_frozen=total_points,
        expires_at=datetime.utcnow() + timedelta(minutes=RESERVE_TTL_MINUTES),
        delivery_info=delivery_info,
        notes=notes,
    )
    db.add(ex)
    try:
        db.flush()
    except IntegrityError:
        # 并发下 request_no 唯一索引兜底：回滚后按幂等重读。
        db.rollback()
        race = db.query(models.BenefitExchange).filter(
            models.BenefitExchange.request_no == request_no
        ).first()
        if race is None:
            raise
        if race.request_payload_hash != fingerprint:
            raise ExchangeError(409, "相同请求编号的申请载荷不一致，判定为冲突请求")
        return race

    event = _add_event(db, ex, models.ExchangeEventType.RESERVE,
                       models.ExchangeStatus.RESERVED,
                       quantity=quantity, points_amount=total_points,
                       detail=f"按单价 {benefit.points_cost} 积分锁定 {quantity} 份")

    _freeze_points(db, volunteer, total_points, ex, event)
    _stock_move(db, benefit, ex, models.StockChangeType.RESERVE,
                stock_delta=-quantity, committed_delta=quantity, event=event)

    db.commit()
    db.refresh(ex)
    return ex


# ---------------------------------------------------------------------------
# 2) 释放：超时 / 拒绝 / 家长取消（原因必须明确）
# ---------------------------------------------------------------------------

def _release(db, ex, event_type, reason, to_status, operator=None):
    if not _is_reserved(ex):
        raise ExchangeError(409, f"兑换当前状态为 {ex.status.value}，不可释放")

    qty = ex.reserved_quantity
    pts = ex.points_frozen
    event = _add_event(db, ex, event_type, to_status,
                       quantity=qty, points_amount=pts, reason=reason, operator=operator)

    if pts:
        _unfreeze_points(db, ex.volunteer, pts, ex, event, reason.value)
    if qty:
        _stock_move(db, ex.benefit, ex, models.StockChangeType.RELEASE,
                    stock_delta=qty, committed_delta=-qty, event=event,
                    operator=operator, remark=reason.value)

    ex.reserved_quantity = 0
    ex.released_quantity = (ex.released_quantity or 0) + qty
    ex.points_frozen = 0
    ex.points_released = (ex.points_released or 0) + pts
    ex.cancel_reason = reason
    return event


def reject_exchange(db, exchange_id: int, operator: Optional[str] = None) -> models.BenefitExchange:
    ex = _get_exchange(db, exchange_id)
    sweep_expired(db)
    db.refresh(ex)
    # 重复拒绝（含网络重试）返回同一结果，绝不二次释放资源。
    if ex.status == models.ExchangeStatus.REJECTED:
        return ex
    _release(db, ex, models.ExchangeEventType.REJECT,
             models.ReleaseReason.REJECTED, models.ExchangeStatus.REJECTED, operator)
    db.commit()
    db.refresh(ex)
    return ex


def cancel_exchange(db, exchange_id: int, operator: Optional[str] = None) -> models.BenefitExchange:
    ex = _get_exchange(db, exchange_id)
    sweep_expired(db)
    db.refresh(ex)
    # 家长取消与超时取消同为"已释放回可用"，重复取消返回同一结果。
    if ex.status in (models.ExchangeStatus.CANCELLED,
                     models.ExchangeStatus.TIMEOUT_CANCELLED):
        return ex
    _release(db, ex, models.ExchangeEventType.CANCEL,
             models.ReleaseReason.USER_CANCEL, models.ExchangeStatus.CANCELLED, operator)
    db.commit()
    db.refresh(ex)
    return ex


# ---------------------------------------------------------------------------
# 3) 后台确认：冻结积分转为结算消费；优先时段券同时发放资格
# ---------------------------------------------------------------------------

def confirm_exchange(db, exchange_id: int, operator: Optional[str] = None) -> models.BenefitExchange:
    ex = _get_exchange(db, exchange_id)
    sweep_expired(db)
    db.refresh(ex)
    # 同动作重试（后台双击/网络重发）返回同一结果，绝不二次结算；
    # 已被拒绝/取消/超时的单子再来确认则是冲突动作。
    if ex.status in (models.ExchangeStatus.CONFIRMED,
                     models.ExchangeStatus.PARTIALLY_FULFILLED,
                     models.ExchangeStatus.COMPLETED):
        return ex
    if not _is_reserved(ex):
        raise ExchangeError(409, f"兑换当前状态为 {ex.status.value}，不可确认")

    pts = ex.points_frozen
    qty = ex.reserved_quantity
    event = _add_event(db, ex, models.ExchangeEventType.CONFIRM,
                       models.ExchangeStatus.CONFIRMED,
                       quantity=qty, points_amount=pts, operator=operator)

    if pts:
        _settle_spend(db, ex.volunteer, pts, ex, event)
    ex.points_settled = (ex.points_settled or 0) + pts
    ex.points_frozen = 0
    ex.confirmed_at = datetime.utcnow()

    if ex.benefit.benefit_type == models.BenefitType.PRIORITY_SLOT:
        # 优先时段券：确认即发放资格（券），资格本身在核销讲解时段时才消费。
        _issue_coupons(db, ex, qty, event)
        _stock_move(db, ex.benefit, ex, models.StockChangeType.FULFILL,
                    stock_delta=0, committed_delta=-qty, event=event, operator=operator,
                    remark="优先时段券资格已发放")
        ex.reserved_quantity = 0
        ex.fulfilled_quantity = (ex.fulfilled_quantity or 0) + qty
        ex.fulfilled_at = datetime.utcnow()
        ex.status = models.ExchangeStatus.COMPLETED
        db.add(models.ExchangeEvent(
            exchange_id=ex.id, event_type=models.ExchangeEventType.FULFILL,
            from_status=models.ExchangeStatus.CONFIRMED,
            to_status=models.ExchangeStatus.COMPLETED, quantity=qty,
            points_amount=0, operator=operator, detail="优先券已发放"))
        db.flush()
    # 实物/其他权益：停在 CONFIRMED，等待分批复品。

    db.commit()
    db.refresh(ex)
    return ex


def _issue_coupons(db, ex, qty, event):
    for seq in range(1, qty + 1):
        db.add(models.PriorityCoupon(
            exchange_id=ex.id,
            volunteer_id=ex.volunteer_id,
            benefit_id=ex.benefit_id,
            coupon_no=f"PSC{ex.benefit_id:04d}-{ex.id}-{seq:02d}-{uuid.uuid4().hex[:6]}",
            status=models.CouponStatus.ISSUED,
        ))


# ---------------------------------------------------------------------------
# 4) 实物权益分批复品，允许部分履约
# ---------------------------------------------------------------------------

def fulfill_exchange(db, exchange_id: int, quantity: int,
                     release_remaining: bool = False,
                     delivery_info: Optional[str] = None,
                     operator: Optional[str] = None,
                     remark: Optional[str] = None,
                     request_no: Optional[str] = None) -> models.BenefitExchange:
    ex = _get_exchange(db, exchange_id)

    if request_no:
        dup = db.query(models.FulfillmentItem).filter(
            models.FulfillmentItem.request_no == request_no
        ).first()
        if dup:
            # 同一批复品请求重复提交：幂等返回，不重复出库。
            return ex

    if ex.status not in (models.ExchangeStatus.CONFIRMED,
                         models.ExchangeStatus.PARTIALLY_FULFILLED):
        raise ExchangeError(409, f"兑换当前状态为 {ex.status.value}，不可登记履约")
    if quantity is None or quantity <= 0:
        raise ExchangeError(400, "履约数量必须大于0")
    if quantity > ex.reserved_quantity:
        raise ExchangeError(
            400, f"履约数量超过待履约数：待履约 {ex.reserved_quantity}，本次 {quantity}")

    event = _add_event(db, ex, models.ExchangeEventType.FULFILL, None,
                       quantity=quantity, operator=operator,
                       detail=delivery_info or remark)
    # 复品只消耗承诺数量，可售库存早在申请预占时就已扣减。
    _stock_move(db, ex.benefit, ex, models.StockChangeType.FULFILL,
                stock_delta=0, committed_delta=-quantity, event=event,
                operator=operator, remark=remark)
    db.add(models.FulfillmentItem(
        exchange_id=ex.id, request_no=request_no, quantity=quantity,
        delivery_info=delivery_info, operator=operator, remark=remark,
    ))
    ex.fulfilled_quantity = (ex.fulfilled_quantity or 0) + quantity
    ex.reserved_quantity -= quantity
    if delivery_info:
        ex.delivery_info = delivery_info

    # 部分履约：剩余数量明确释放回可售库存，并按锁定价退回对应积分。
    if release_remaining and ex.reserved_quantity > 0:
        rest = ex.reserved_quantity
        unit_points = ex.points_spent // ex.quantity
        refund_points = rest * unit_points
        pre_status = ex.status
        release_event = _add_event(
            db, ex, models.ExchangeEventType.PARTIAL_FULFILL,
            models.ExchangeStatus.PARTIALLY_FULFILLED,
            quantity=rest, points_amount=refund_points,
            reason=models.ReleaseReason.PARTIAL_RELEASE,
            operator=operator, detail=f"实物仅复品 {ex.fulfilled_quantity} 份，余款退回")
        _stock_move(db, ex.benefit, ex, models.StockChangeType.RELEASE,
                    stock_delta=rest, committed_delta=-rest, event=release_event,
                    operator=operator, remark="部分履约余量释放")
        _settle_refund(db, ex.volunteer, refund_points, ex, release_event,
                       models.PointsSource.EXCHANGE_RELEASE,
                       f"部分履约少发 {rest} 份，退回积分 {refund_points}（兑换#{ex.id}）")
        ex.released_quantity = (ex.released_quantity or 0) + rest
        ex.points_refunded = (ex.points_refunded or 0) + refund_points
        ex.reserved_quantity = 0

    if ex.reserved_quantity == 0:
        ex.status = models.ExchangeStatus.COMPLETED
        ex.fulfilled_at = datetime.utcnow()
        if ex.fulfilled_quantity < ex.quantity:
            ex.status = models.ExchangeStatus.PARTIALLY_FULFILLED
    else:
        ex.status = models.ExchangeStatus.PARTIALLY_FULFILLED

    db.commit()
    db.refresh(ex)
    return ex


# ---------------------------------------------------------------------------
# 5) 优先时段券：真正使用（核销讲解时段）才消费资格
# ---------------------------------------------------------------------------

def use_coupon(db, coupon_id: int, time_slot_id: int) -> models.PriorityCoupon:
    coupon = db.query(models.PriorityCoupon).filter(
        models.PriorityCoupon.id == coupon_id
    ).first()
    if not coupon:
        raise ExchangeError(404, "优先券不存在")

    slot = db.query(models.TimeSlot).filter(
        models.TimeSlot.id == time_slot_id
    ).first()
    if not slot:
        raise ExchangeError(404, "讲解时段不存在")

    # 幂等：同券同时段重复核销返回同一结果；券已用于其他时段则冲突。
    if coupon.status == models.CouponStatus.USED:
        if coupon.time_slot_id == time_slot_id:
            return coupon
        raise ExchangeError(409, "优先券已用于其他讲解时段")
    if coupon.status != models.CouponStatus.ISSUED:
        raise ExchangeError(409, f"优先券当前状态为 {coupon.status.value}，不可使用")

    if slot.status != models.TimeSlotStatus.AVAILABLE:
        raise ExchangeError(409, "该讲解时段已不可认领")
    volunteer = coupon.volunteer
    if volunteer.status != models.VolunteerStatus.CERTIFIED:
        raise ExchangeError(400, "只有已持证志愿者才能认领时段")

    slot.status = models.TimeSlotStatus.CLAIMED
    slot.volunteer_id = coupon.volunteer_id
    coupon.status = models.CouponStatus.USED
    coupon.time_slot_id = time_slot_id
    coupon.used_at = datetime.utcnow()

    ex = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.id == coupon.exchange_id
    ).first()
    db.add(models.ExchangeEvent(
        exchange_id=ex.id, event_type=models.ExchangeEventType.COUPON_USE,
        from_status=ex.status, to_status=ex.status,
        detail=f"优先券 {coupon.coupon_no} 核销时段 #{time_slot_id}"))
    db.flush()

    db.commit()
    db.refresh(coupon)
    return coupon


def return_coupon_for_slot(db, time_slot_id: int) -> int:
    """认领时段被取消时，把已核销的优先券资格退回（可恢复流程）。"""
    coupons = db.query(models.PriorityCoupon).filter(
        models.PriorityCoupon.time_slot_id == time_slot_id,
        models.PriorityCoupon.status == models.CouponStatus.USED,
    ).all()
    for coupon in coupons:
        coupon.status = models.CouponStatus.ISSUED
        old_slot = coupon.time_slot_id
        coupon.time_slot_id = None
        coupon.used_at = None
        db.add(models.ExchangeEvent(
            exchange_id=coupon.exchange_id,
            event_type=models.ExchangeEventType.COUPON_RETURN,
            from_status=models.ExchangeStatus.COMPLETED,
            to_status=models.ExchangeStatus.COMPLETED,
            detail=f"时段 #{old_slot} 取消，优先券 {coupon.coupon_no} 资格退回"))
    if coupons:
        db.flush()
    return len(coupons)


# ---------------------------------------------------------------------------
# 6) 后台人工更正：只能追加补偿记录，永不改写历史流水
# ---------------------------------------------------------------------------

def add_compensation(db, exchange_id: int, compensation_type: models.CompensationType,
                     amount: int, reason: str,
                     operator: Optional[str] = None,
                     request_no: Optional[str] = None) -> models.ManualCompensation:
    ex = _get_exchange(db, exchange_id)
    if not amount or amount <= 0:
        raise ExchangeError(400, "补偿数量必须大于0")
    if not reason or not reason.strip():
        raise ExchangeError(400, "人工补偿必须填写更正原因")

    request_no = request_no or f"comp-{uuid.uuid4().hex}"
    dup = db.query(models.ManualCompensation).filter(
        models.ManualCompensation.exchange_id == exchange_id,
        models.ManualCompensation.request_no == request_no,
    ).first()
    if dup:
        return dup

    event = _add_event(db, ex, models.ExchangeEventType.COMPENSATE, ex.status,
                       points_amount=amount if compensation_type in (
                           models.CompensationType.POINTS_REFUND,
                           models.CompensationType.POINTS_DEDUCT) else 0,
                       detail=f"{compensation_type.value} x{amount}：{reason}",
                       operator=operator)

    if compensation_type == models.CompensationType.POINTS_REFUND:
        _settle_refund(db, ex.volunteer, amount, ex, event,
                       models.PointsSource.MANUAL_COMPENSATION,
                       f"人工补偿退回积分 {amount}（兑换#{ex.id}：{reason}）")
        ex.points_refunded = (ex.points_refunded or 0) + amount
    elif compensation_type == models.CompensationType.POINTS_DEDUCT:
        available = (ex.volunteer.points_balance or 0) - (ex.volunteer.points_frozen or 0)
        if available < amount:
            raise ExchangeError(400, f"可用积分不足，无法补扣：可用 {available}，需 {amount}")
        ex.volunteer.points_balance -= amount
        _points_line(db, ex.volunteer, models.PointsType.SPEND, amount,
                     models.PointsSource.MANUAL_COMPENSATION,
                     f"人工补偿补扣积分 {amount}（兑换#{ex.id}：{reason}）", ex, event)
    elif compensation_type == models.CompensationType.STOCK_RETURN:
        _stock_move(db, ex.benefit, ex, models.StockChangeType.COMPENSATE_IN,
                    stock_delta=amount, committed_delta=0, event=event,
                    operator=operator, remark=reason)
    elif compensation_type == models.CompensationType.STOCK_DEDUCT:
        if (ex.benefit.stock or 0) < amount:
            raise ExchangeError(400, "可售库存不足，无法补扣")
        _stock_move(db, ex.benefit, ex, models.StockChangeType.COMPENSATE_OUT,
                    stock_delta=-amount, committed_delta=0, event=event,
                    operator=operator, remark=reason)

    comp = models.ManualCompensation(
        exchange_id=ex.id, request_no=request_no,
        compensation_type=compensation_type, amount=amount,
        reason=reason, operator=operator)
    db.add(comp)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.query(models.ManualCompensation).filter(
            models.ManualCompensation.exchange_id == exchange_id,
            models.ManualCompensation.request_no == request_no,
        ).first()
    db.refresh(comp)
    return comp


# ---------------------------------------------------------------------------
# 7) 对账：任何时候都能从流水解释四类数字为何一致
# ---------------------------------------------------------------------------

def _points_sums(db, volunteer_id: int):
    rows = db.query(models.PointsRecord.points_type,
                    func.coalesce(func.sum(models.PointsRecord.points_amount), 0)).filter(
        models.PointsRecord.volunteer_id == volunteer_id
    ).group_by(models.PointsRecord.points_type).all()
    sums = {t: 0 for t in models.PointsType}
    for ptype, total in rows:
        sums[ptype] = total or 0
    return sums


def reconcile(db, volunteer_id: Optional[int] = None,
              benefit_id: Optional[int] = None) -> dict:
    volunteer_q = db.query(models.Volunteer)
    if volunteer_id:
        volunteer_q = volunteer_q.filter(models.Volunteer.id == volunteer_id)
    benefit_q = db.query(models.Benefit)
    if benefit_id:
        benefit_q = benefit_q.filter(models.Benefit.id == benefit_id)

    volunteer_reports = []
    for v in volunteer_q.all():
        s = _points_sums(db, v.id)
        ledger_balance = (s[models.PointsType.EARN]
                          + s[models.PointsType.SETTLE_REFUND]
                          - s[models.PointsType.SPEND]
                          - s[models.PointsType.SETTLE_SPEND])
        ledger_frozen = (s[models.PointsType.FREEZE]
                         - s[models.PointsType.UNFREEZE]
                         - s[models.PointsType.SETTLE_SPEND])
        frozen_in_exchanges = db.query(
            func.coalesce(func.sum(models.BenefitExchange.points_frozen), 0)).filter(
            models.BenefitExchange.volunteer_id == v.id).scalar() or 0
        volunteer_reports.append({
            "volunteer_id": v.id,
            "name": v.name,
            "points_balance": v.points_balance or 0,
            "points_frozen": v.points_frozen or 0,
            "points_available": (v.points_balance or 0) - (v.points_frozen or 0),
            "ledger_balance": ledger_balance,
            "ledger_frozen": ledger_frozen,
            "frozen_in_exchanges": frozen_in_exchanges,
            "consistent": (v.points_balance or 0) == ledger_balance
                          and (v.points_frozen or 0) == ledger_frozen
                          and ledger_frozen == frozen_in_exchanges,
        })

    benefit_reports = []
    for b in benefit_q.all():
        reserved_sum = db.query(
            func.coalesce(func.sum(models.BenefitExchange.reserved_quantity), 0)).filter(
            models.BenefitExchange.benefit_id == b.id).scalar() or 0
        fulfilled_sum = db.query(
            func.coalesce(func.sum(models.BenefitExchange.fulfilled_quantity), 0)).filter(
            models.BenefitExchange.benefit_id == b.id).scalar() or 0
        released_sum = db.query(
            func.coalesce(func.sum(models.BenefitExchange.released_quantity), 0)).filter(
            models.BenefitExchange.benefit_id == b.id).scalar() or 0
        ledger_qty = db.query(
            func.coalesce(func.sum(models.BenefitExchange.quantity), 0)).filter(
            models.BenefitExchange.benefit_id == b.id).scalar() or 0

        last_stock_row = db.query(models.StockLedger).filter(
            models.StockLedger.benefit_id == b.id
        ).order_by(models.StockLedger.id.desc()).first()
        ledger_stock = db.query(
            func.coalesce(func.sum(models.StockLedger.quantity), 0)).filter(
            models.StockLedger.benefit_id == b.id).scalar() or 0
        ledger_committed = db.query(
            func.coalesce(func.sum(models.StockLedger.committed_delta), 0)).filter(
            models.StockLedger.benefit_id == b.id).scalar() or 0
        snapshot_matches = (
            last_stock_row is None
            or (last_stock_row.stock_after == (b.stock or 0)
                and last_stock_row.committed_after == (b.committed_quantity or 0))
        )
        stock_matches_ledger = (
            snapshot_matches
            and ledger_stock == (b.stock or 0)
            and ledger_committed == (b.committed_quantity or 0)
        )
        benefit_reports.append({
            "benefit_id": b.id,
            "name": b.name,
            "sellable_stock": b.stock or 0,
            "committed_quantity": b.committed_quantity or 0,
            "reserved_in_exchanges": reserved_sum,
            "fulfilled_quantity": fulfilled_sum,
            "released_quantity": released_sum,
            "quantity_account_balanced": ledger_qty == reserved_sum + fulfilled_sum + released_sum,
            "stock_matches_ledger": stock_matches_ledger,
            "consistent": (b.committed_quantity or 0) == reserved_sum and stock_matches_ledger,
        })

    exchange_q = db.query(models.BenefitExchange)
    if benefit_id:
        exchange_q = exchange_q.filter(models.BenefitExchange.benefit_id == benefit_id)
    if volunteer_id:
        exchange_q = exchange_q.filter(models.BenefitExchange.volunteer_id == volunteer_id)
    exchange_reports = []
    for ex in exchange_q.all():
        qty_ok = ex.quantity == (ex.reserved_quantity or 0) + (ex.fulfilled_quantity or 0) + (ex.released_quantity or 0)
        # 积分总额恒等式：锁定价 = 冻结中 + 已结算(毛额) + 整单释放回可用；refunded 是结算后退回的去向说明。
        points_ok = ex.points_spent == (
            (ex.points_frozen or 0) + (ex.points_settled or 0) + (ex.points_released or 0))
        net_consumed = (ex.points_settled or 0) - (ex.points_refunded or 0)
        # 人工善意补偿可使退回大于结算（净消费为负），属正常，不计为账不平。
        exchange_reports.append({
            "exchange_id": ex.id,
            "status": ex.status.value,
            "points_locked": ex.points_spent,
            "points_frozen": ex.points_frozen or 0,
            "points_settled": ex.points_settled or 0,
            "points_released": ex.points_released or 0,
            "points_refunded": ex.points_refunded or 0,
            "points_net_consumed": net_consumed,
            "quantity_balanced": qty_ok,
            "points_balanced": points_ok,
            "consistent": qty_ok and points_ok,
        })

    all_consistent = (all(r["consistent"] for r in volunteer_reports)
                      and all(r["consistent"] for r in benefit_reports)
                      and all(r["consistent"] for r in exchange_reports))
    return {
        "checked_at": datetime.utcnow().isoformat(),
        "all_consistent": all_consistent,
        "volunteers": volunteer_reports,
        "benefits": benefit_reports,
        "exchanges": exchange_reports,
    }
