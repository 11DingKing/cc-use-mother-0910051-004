"""积分与库存的原子流水原语。

所有兑换相关的资源变动都必须经由本模块落流水，禁止直接改
Volunteer.points_balance / Benefit.stock 而不留痕。调用方需自行加行锁
（with_for_update）并在同一事务内提交。

积分语义（Volunteer 账户）：
  points_balance 即可用积分；frozen_points 为冻结积分。
  FREEZE   : balance -n, frozen +n
  UNFREEZE : balance +n, frozen -n
  CONSUME  : frozen -n（balance 不变，申请时已扣）
  EARN     : balance +n
  SPEND    : balance -n（非兑换流程的直接扣减）
  MANUAL_ADJUST : balance += signed n
"""
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


class LedgerError(Exception):
    """余额/冻结额不足等业务前置条件不满足。"""


# ---------------------------------------------------------------- 积分

def _add_ledger(db: Session, volunteer_id: int, ledger_type: models.PointsLedgerType,
                amount: int, *, idempotency_key: str = None,
                reason: models.ExchangeReleaseReason = None,
                ref_type: str = None, ref_id: int = None,
                description: str = None) -> models.PointsLedger:
    """追加一条积分流水（幂等键已存在则直接返回旧流水，绝不重复入账）。"""
    if idempotency_key:
        existing = db.query(models.PointsLedger).filter(
            models.PointsLedger.idempotency_key == idempotency_key
        ).first()
        if existing is not None:
            return existing

    entry = models.PointsLedger(
        volunteer_id=volunteer_id,
        ledger_type=ledger_type,
        amount=amount,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        description=description,
    )
    db.add(entry)
    db.flush()
    return entry


def freeze_points(db: Session, volunteer: models.Volunteer, amount: int,
                  exchange_id: int, description: str) -> models.PointsLedger:
    """申请阶段：原子锁定积分。余额不足直接拒绝（不落任何流水）。"""
    if amount <= 0:
        raise LedgerError("冻结积分必须为正数")
    if (volunteer.points_balance or 0) < amount:
        raise LedgerError("可用积分不足")
    volunteer.points_balance -= amount
    volunteer.frozen_points += amount
    return _add_ledger(
        db, volunteer.id, models.PointsLedgerType.FREEZE, amount,
        idempotency_key=f"exchange:{exchange_id}:freeze",
        ref_type="exchange", ref_id=exchange_id, description=description)


def unfreeze_points(db: Session, volunteer: models.Volunteer, amount: int,
                    exchange_id: int, reason: models.ExchangeReleaseReason,
                    description: str) -> models.PointsLedger:
    """释放冻结：超时/拒绝/取消（全额）或部分履约差价（差额）。"""
    if amount <= 0:
        raise LedgerError("释放积分必须为正数")
    if (volunteer.frozen_points or 0) < amount:
        raise LedgerError("冻结积分不足，无法释放")
    volunteer.points_balance += amount
    volunteer.frozen_points -= amount
    return _add_ledger(
        db, volunteer.id, models.PointsLedgerType.UNFREEZE, amount,
        idempotency_key=f"exchange:{exchange_id}:unfreeze:{reason.name}:{amount}",
        reason=reason, ref_type="exchange", ref_id=exchange_id, description=description)


def consume_points(db: Session, volunteer: models.Volunteer, amount: int,
                   exchange_id: int, description: str) -> models.PointsLedger:
    """确认/履约：把冻结积分转为已消费（不触碰可用余额）。"""
    if amount <= 0:
        raise LedgerError("消费积分必须为正数")
    if (volunteer.frozen_points or 0) < amount:
        raise LedgerError("冻结积分不足，无法消费")
    volunteer.frozen_points -= amount
    return _add_ledger(
        db, volunteer.id, models.PointsLedgerType.CONSUME, amount,
        idempotency_key=f"exchange:{exchange_id}:consume:{amount}",
        ref_type="exchange", ref_id=exchange_id, description=description)


def direct_spend(db: Session, volunteer: models.Volunteer, amount: int,
                 description: str, *, service_record_id: int = None) -> models.PointsLedger:
    """非兑换流程的直接扣减（服务记录更正/删除等），同样只追加流水。"""
    if amount <= 0:
        raise LedgerError("扣减积分必须为正数")
    if (volunteer.points_balance or 0) < amount:
        raise LedgerError("可用积分不足")
    volunteer.points_balance -= amount
    return _add_ledger(
        db, volunteer.id, models.PointsLedgerType.SPEND, amount,
        ref_type="service_record", ref_id=service_record_id, description=description)


def manual_adjust_points(db: Session, volunteer: models.Volunteer, delta: int,
                         compensation_id: int, reason: str) -> models.PointsLedger:
    """人工补偿：带符号调整可用积分，只追加流水。"""
    if delta == 0:
        raise LedgerError("补偿积分不能为 0")
    if (volunteer.points_balance or 0) + delta < 0:
        raise LedgerError("扣回后可用积分将为负，拒绝补偿")
    volunteer.points_balance += delta
    return _add_ledger(
        db, volunteer.id, models.PointsLedgerType.MANUAL_ADJUST, delta,
        idempotency_key=f"compensation:{compensation_id}:points",
        reason=models.ExchangeReleaseReason.MANUAL_COMPENSATION,
        ref_type="compensation", ref_id=compensation_id, description=reason)


def points_totals(db: Session, volunteer_id: int) -> tuple[int, int]:
    """由流水汇总 (可用余额, 冻结额)，用于对账。"""
    rows = db.query(
        models.PointsLedger.ledger_type,
        func.coalesce(func.sum(models.PointsLedger.amount), 0),
    ).filter(models.PointsLedger.volunteer_id == volunteer_id).group_by(
        models.PointsLedger.ledger_type).all()
    sums = {t: int(v) for t, v in rows}
    earned = sums.get(models.PointsLedgerType.EARN, 0)
    freeze = sums.get(models.PointsLedgerType.FREEZE, 0)
    unfreeze = sums.get(models.PointsLedgerType.UNFREEZE, 0)
    consume = sums.get(models.PointsLedgerType.CONSUME, 0)
    spend = sums.get(models.PointsLedgerType.SPEND, 0)
    adjust = sums.get(models.PointsLedgerType.MANUAL_ADJUST, 0)
    balance = earned - freeze + unfreeze - spend + adjust
    frozen = freeze - unfreeze - consume
    return balance, frozen


# ---------------------------------------------------------------- 库存

def _add_inventory(db: Session, benefit_id: int, action: str, quantity: int,
                   committed_delta: int, exchange_id: Optional[int],
                   reason: models.ExchangeReleaseReason = None) -> models.InventoryRecord:
    record = models.InventoryRecord(
        benefit_id=benefit_id,
        exchange_id=exchange_id,
        action=action,
        quantity=quantity,
        committed_delta=committed_delta,
        reason=reason,
    )
    db.add(record)
    db.flush()
    return record


def available_stock(db: Session, benefit: models.Benefit) -> int:
    """可售库存 = 盘点库存 + 库存流水对可售量的增量（预占为负、释放/补库存为正）。"""
    delta = db.query(func.coalesce(func.sum(models.InventoryRecord.quantity), 0)).filter(
        models.InventoryRecord.benefit_id == benefit.id
    ).scalar() or 0
    return benefit.stock + int(delta)


def committed_stock(db: Session, benefit_id: int) -> int:
    """已承诺数量 = 库存流水 committed_delta 之和（预占 +，释放/履约 -）。"""
    total = db.query(func.coalesce(func.sum(models.InventoryRecord.committed_delta), 0)).filter(
        models.InventoryRecord.benefit_id == benefit_id
    ).scalar() or 0
    return int(total)


def sold_stock(db: Session, benefit_id: int) -> int:
    """已售/已履约数量 = FULFILL 流水绝对值之和。"""
    total = db.query(func.coalesce(func.sum(models.InventoryRecord.committed_delta), 0)).filter(
        models.InventoryRecord.benefit_id == benefit_id,
        models.InventoryRecord.action == "FULFILL",
    ).scalar() or 0
    return -int(total)


def stock_tracked(benefit: models.Benefit) -> bool:
    """stock == 0 表示不限量权益（如纯资格类），不跟踪可售/承诺数量。"""
    return (benefit.stock or 0) > 0


def reserve_stock(db: Session, benefit: models.Benefit, quantity: int,
                  exchange_id: int):
    """申请阶段：原子锁定库存（可售量不足直接拒绝，不落流水）。

    不限量权益（stock == 0）直接跳过，不产生库存流水。
    """
    if quantity <= 0:
        raise LedgerError("预占数量必须为正数")
    if not stock_tracked(benefit):
        return None
    if available_stock(db, benefit) < quantity:
        raise LedgerError("库存不足")
    return _add_inventory(db, benefit.id, "RESERVE", -quantity, quantity, exchange_id)


def release_stock(db: Session, benefit_id: int, quantity: int, exchange_id: int,
                  reason: models.ExchangeReleaseReason) -> models.InventoryRecord:
    """释放预占库存：超时/拒绝/取消，或部分履约后剩余未发部分。"""
    if quantity <= 0:
        raise LedgerError("释放库存必须为正数")
    return _add_inventory(db, benefit_id, "RELEASE", quantity, -quantity, exchange_id, reason)


def fulfill_stock(db: Session, benefit: models.Benefit, quantity: int,
                  exchange_id: int,
                  reason: models.ExchangeReleaseReason = None):
    """履约发货：已承诺转为已售（committed -quantity），可售量不再变化。

    不限量权益不产生库存流水。
    """
    if quantity <= 0:
        raise LedgerError("履约数量必须为正数")
    if not stock_tracked(benefit):
        return None
    return _add_inventory(db, benefit.id, "FULFILL", 0, -quantity, exchange_id, reason)


def adjust_stock(db: Session, benefit: models.Benefit, delta: int,
                 compensation_id: int) -> models.InventoryRecord:
    """人工补偿：带符号调整可售库存（committed 不变），只追加流水。"""
    if delta == 0:
        raise LedgerError("补偿库存不能为 0")
    if available_stock(db, benefit) + delta < 0:
        raise LedgerError("补偿后可售库存将为负，拒绝补偿")
    return _add_inventory(db, benefit.id, "ADMIN_ADJUST", delta, 0, None,
                          models.ExchangeReleaseReason.MANUAL_COMPENSATION)
