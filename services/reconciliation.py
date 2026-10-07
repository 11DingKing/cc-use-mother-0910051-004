"""对账：任何时刻都能从流水解释积分与库存数字为何一致。"""
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from services import ledger


def reconcile_points(db: Session, volunteer_id: int) -> dict:
    """核对账户字段与积分流水汇总是否一致。

    返回可用积分、冻结积分及流水推导值。账户总积分 = 可用 + 冻结。
    """
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        return None

    ledger_balance, ledger_frozen = ledger.points_totals(db, volunteer_id)
    balance = volunteer.points_balance or 0
    frozen = volunteer.frozen_points or 0
    return {
        "volunteer_id": volunteer_id,
        "name": volunteer.name,
        "points_balance": balance,
        "frozen_points": frozen,
        "available_points": balance,
        "ledger_total": ledger_balance,
        "ledger_frozen": ledger_frozen,
        "consistent": balance == ledger_balance and frozen == ledger_frozen
                       and ledger_frozen >= 0 and ledger_balance >= 0,
    }


def _open_exchange_reserved(db: Session, benefit_id: int) -> int:
    """未决兑换单（预占/已确认/部分履约）尚占用的承诺数量。"""
    rows = db.query(
        models.BenefitExchange.quantity,
        models.BenefitExchange.fulfilled_quantity,
        models.BenefitExchange.points_refunded,
        models.BenefitExchange.points_cost,
    ).filter(
        models.BenefitExchange.benefit_id == benefit_id,
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.RESERVED,
            models.ExchangeStatus.CONFIRMED,
            models.ExchangeStatus.PARTIALLY_FULFILLED,
        ]),
    ).all()
    total = 0
    for qty, fulfilled, refunded, unit in rows:
        released = (refunded // unit) if unit else 0
        total += qty - fulfilled - released
    return total


def reconcile_inventory(db: Session) -> list[dict]:
    """逐商品核对：盘点库存 = 可售 + 已承诺 + 已售 - 人工补偿增量。"""
    items = []
    all_consistent = True
    for benefit in db.query(models.Benefit).order_by(models.Benefit.id).all():
        available = ledger.available_stock(db, benefit)
        reserved = ledger.committed_stock(db, benefit.id)
        sold = ledger.sold_stock(db, benefit.id)
        admin_delta = db.query(
            func.coalesce(func.sum(models.InventoryRecord.quantity), 0)
        ).filter(
            models.InventoryRecord.benefit_id == benefit.id,
            models.InventoryRecord.action == "ADMIN_ADJUST",
        ).scalar() or 0
        open_reserved = _open_exchange_reserved(db, benefit.id)

        identity_ok = available + reserved + sold == benefit.stock + int(admin_delta)
        reserved_ok = reserved == open_reserved
        consistent = identity_ok and reserved_ok and available >= 0 and reserved >= 0
        all_consistent = all_consistent and consistent

        items.append({
            "benefit_id": benefit.id,
            "name": benefit.name,
            "stock": benefit.stock,
            "reserved_quantity": reserved,
            "sold_quantity": sold,
            "available_quantity": available,
            "open_reserved_quantity": open_reserved,
            "consistent": consistent,
        })
    return {"items": items, "consistent": all_consistent}


def exchange_timeline(db: Session, exchange_id: int) -> Optional[dict]:
    """单笔兑换的完整解释轨迹。"""
    exchange = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        return None
    ledgers = db.query(models.PointsLedger).filter(
        models.PointsLedger.ref_type == "exchange",
        models.PointsLedger.ref_id == exchange_id,
    ).order_by(models.PointsLedger.id).all()
    inv = db.query(models.InventoryRecord).filter(
        models.InventoryRecord.exchange_id == exchange_id
    ).order_by(models.InventoryRecord.id).all()
    entitlements = db.query(models.PriorityEntitlement).filter(
        models.PriorityEntitlement.exchange_id == exchange_id
    ).order_by(models.PriorityEntitlement.id).all()
    compensations = db.query(models.ManualCompensation).filter(
        models.ManualCompensation.exchange_id == exchange_id
    ).order_by(models.ManualCompensation.id).all()
    return {
        "exchange": exchange,
        "points_ledgers": ledgers,
        "inventory_records": inv,
        "entitlements": entitlements,
        "compensations": compensations,
    }
