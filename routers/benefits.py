from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from database import get_db
import models, schemas
from services import exchange_flow, ledger, reconciliation

router = APIRouter(prefix="/api/benefits", tags=["权益管理"])


# ------------------------------------------------------------- 权益商品

@router.get("/", response_model=List[schemas.Benefit])
def list_benefits(benefit_type: str = None, is_active: bool = None,
                  skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.Benefit)
    if benefit_type:
        query = query.filter(models.Benefit.benefit_type == benefit_type)
    if is_active is not None:
        query = query.filter(models.Benefit.is_active == is_active)
    return query.order_by(models.Benefit.sort_order, models.Benefit.created_at.desc()
                          ).offset(skip).limit(limit).all()


@router.get("/availability", response_model=List[schemas.BenefitAvailability])
def list_availability(db: Session = Depends(get_db)):
    """实时可售视图：可售/已承诺/已售全部由库存流水汇总。"""
    result = []
    for b in db.query(models.Benefit).order_by(models.Benefit.sort_order).all():
        reserved = ledger.committed_stock(db, b.id)
        sold = ledger.sold_stock(db, b.id)
        available = ledger.available_stock(db, b)
        result.append(schemas.BenefitAvailability(
            benefit_id=b.id, name=b.name, benefit_type=b.benefit_type,
            points_cost=b.points_cost, stock=b.stock,
            reserved_quantity=reserved, sold_quantity=sold,
            available_quantity=available,
            allow_partial_fulfillment=b.allow_partial_fulfillment,
            is_active=b.is_active))
    return result


@router.post("/", response_model=schemas.Benefit)
def create_benefit(benefit: schemas.BenefitCreate, db: Session = Depends(get_db)):
    db_benefit = models.Benefit(**benefit.model_dump())
    db.add(db_benefit)
    db.commit()
    db.refresh(db_benefit)
    return db_benefit


@router.put("/{benefit_id}", response_model=schemas.Benefit)
def update_benefit(benefit_id: int, benefit_update: schemas.BenefitUpdate, db: Session = Depends(get_db)):
    benefit = db.query(models.Benefit).filter(models.Benefit.id == benefit_id).first()
    if not benefit:
        raise HTTPException(status_code=404, detail="权益不存在")
    update_data = benefit_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(benefit, key, value)
    db.commit()
    db.refresh(benefit)
    return benefit


@router.delete("/{benefit_id}")
def delete_benefit(benefit_id: int, db: Session = Depends(get_db)):
    benefit = db.query(models.Benefit).filter(models.Benefit.id == benefit_id).first()
    if not benefit:
        raise HTTPException(status_code=404, detail="权益不存在")
    db.delete(benefit)
    db.commit()
    return {"message": "删除成功"}


# ------------------------------------------------------------- 兑换：预占流程

@router.get("/exchanges", response_model=List[schemas.BenefitExchange])
def list_exchanges(volunteer_id: int = None, status: str = None,
                   skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(models.BenefitExchange)
    if volunteer_id:
        query = query.filter(models.BenefitExchange.volunteer_id == volunteer_id)
    if status:
        status_enum = None
        for s in models.ExchangeStatus:
            if s.value == status or s.name == status:
                status_enum = s
                break
        if status_enum:
            query = query.filter(models.BenefitExchange.status == status_enum)
    return query.order_by(models.BenefitExchange.created_at.desc()
                          ).offset(skip).limit(limit).all()


@router.post("/exchanges", response_model=schemas.BenefitExchange)
def apply_exchange(exchange: schemas.BenefitExchangeApply, db: Session = Depends(get_db)):
    """申请兑换：原子锁定积分与库存（预占）。重复请求返回同一兑换单。"""
    return exchange_flow.apply_exchange(
        db, volunteer_id=exchange.volunteer_id, benefit_id=exchange.benefit_id,
        quantity=exchange.quantity, delivery_info=exchange.delivery_info,
        notes=exchange.notes, idempotency_key=exchange.idempotency_key)


@router.get("/exchanges/volunteer/{volunteer_id}", response_model=List[schemas.BenefitExchange])
def get_volunteer_exchanges(volunteer_id: int, skip: int = 0, limit: int = 100,
                            db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")
    return db.query(models.BenefitExchange).filter(
        models.BenefitExchange.volunteer_id == volunteer_id
    ).order_by(models.BenefitExchange.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/exchanges/{exchange_id}/timeline", response_model=schemas.ExchangeTimeline)
def get_exchange_timeline(exchange_id: int, db: Session = Depends(get_db)):
    timeline = reconciliation.exchange_timeline(db, exchange_id)
    if not timeline:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    return timeline


@router.get("/exchanges/{exchange_id}", response_model=schemas.BenefitExchange)
def get_exchange(exchange_id: int, db: Session = Depends(get_db)):
    exchange = db.query(models.BenefitExchange).filter(
        models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    return exchange


@router.post("/exchanges/{exchange_id}/confirm", response_model=schemas.BenefitExchange)
def confirm_exchange(exchange_id: int, body: schemas.BenefitExchangeConfirm,
                     db: Session = Depends(get_db)):
    """后台确认。可同时给出本次发货数量（实物部分履约）；
    不带数量时优先时段券立即发资格，实物停在已确认等待发货。"""
    return exchange_flow.confirm_exchange(
        db, exchange_id, fulfill_quantity=body.fulfill_quantity,
        delivery_info=body.delivery_info, notes=body.notes,
        idempotency_key=body.idempotency_key)


@router.post("/exchanges/{exchange_id}/fulfill", response_model=schemas.BenefitExchange)
def fulfill_exchange(exchange_id: int, body: schemas.BenefitExchangeConfirm,
                     db: Session = Depends(get_db)):
    """实物发货（可分批/部分履约）。剩余未发部分按库存不足释放。"""
    if body.fulfill_quantity is None:
        raise HTTPException(status_code=400, detail="发货数量必填")
    return exchange_flow.fulfill_exchange(
        db, exchange_id, body.fulfill_quantity, idempotency_key=body.idempotency_key)


@router.post("/exchanges/{exchange_id}/reject", response_model=schemas.BenefitExchange)
def reject_exchange(exchange_id: int, body: schemas.BenefitExchangeReject,
                    db: Session = Depends(get_db)):
    """后台拒绝：全额释放冻结积分与预占库存。"""
    return exchange_flow.reject_exchange(
        db, exchange_id, reason_text=body.reason,
        idempotency_key=body.idempotency_key)


@router.post("/exchanges/{exchange_id}/cancel", response_model=schemas.BenefitExchange)
def cancel_exchange(exchange_id: int, body: schemas.BenefitExchangeCancel,
                    db: Session = Depends(get_db)):
    """家长主动取消（仅预占中可取消）：全额释放资源。"""
    return exchange_flow.cancel_exchange(
        db, exchange_id, reason_text=body.reason,
        idempotency_key=body.idempotency_key)


@router.post("/exchanges-timeouts/sweep")
def sweep_timeouts(db: Session = Depends(get_db)):
    """超时回收：释放所有过期未确认的预占单（可由定时任务调用，可重复执行）。"""
    recovered = exchange_flow.expire_timed_out_reservations(db)
    return {"recovered_exchange_ids": recovered, "count": len(recovered)}


# ------------------------------------------------------------- 优先时段资格

@router.get("/entitlements", response_model=List[schemas.PriorityEntitlement])
def list_entitlements(volunteer_id: int = None, status: str = None,
                      db: Session = Depends(get_db)):
    query = db.query(models.PriorityEntitlement)
    if volunteer_id:
        query = query.filter(models.PriorityEntitlement.volunteer_id == volunteer_id)
    if status:
        for s in models.EntitlementStatus:
            if s.value == status or s.name == status:
                query = query.filter(models.PriorityEntitlement.status == s)
                break
    return query.order_by(models.PriorityEntitlement.created_at.desc()).all()


@router.post("/entitlements/consume", response_model=schemas.PriorityEntitlement)
def consume_entitlement(body: schemas.EntitlementConsume, db: Session = Depends(get_db)):
    """真正认领时段时消费优先券资格（资格延迟消费点）。"""
    return exchange_flow.consume_entitlement(
        db, volunteer_id=body.volunteer_id, time_slot_id=body.time_slot_id,
        idempotency_key=body.idempotency_key)


# ------------------------------------------------------------- 人工补偿

@router.post("/compensations", response_model=schemas.ManualCompensation)
def create_compensation(body: schemas.ManualCompensationCreate, db: Session = Depends(get_db)):
    """后台人工更正：只能追加补偿记录与补偿流水，不能回改原始单据。"""
    return exchange_flow.manual_compensate(
        db, exchange_id=body.exchange_id, volunteer_id=body.volunteer_id,
        benefit_id=body.benefit_id, points_delta=body.points_delta,
        inventory_delta=body.inventory_delta, reason=body.reason,
        operator=body.operator)


@router.get("/compensations", response_model=List[schemas.ManualCompensation])
def list_compensations(volunteer_id: int = None, exchange_id: int = None,
                       db: Session = Depends(get_db)):
    query = db.query(models.ManualCompensation)
    if volunteer_id:
        query = query.filter(models.ManualCompensation.volunteer_id == volunteer_id)
    if exchange_id:
        query = query.filter(models.ManualCompensation.exchange_id == exchange_id)
    return query.order_by(models.ManualCompensation.created_at.desc()).all()


# ------------------------------------------------------------- 对账

@router.get("/reconciliation/points", response_model=schemas.PointsReconciliation)
def reconcile_points(volunteer_id: int, db: Session = Depends(get_db)):
    result = reconciliation.reconcile_points(db, volunteer_id)
    if not result:
        raise HTTPException(status_code=404, detail="志愿者不存在")
    return result


@router.get("/reconciliation/inventory", response_model=schemas.InventoryReconciliation)
def reconcile_inventory(db: Session = Depends(get_db)):
    return reconciliation.reconcile_inventory(db)


# 动态路径放在所有静态 GET 之后注册，避免吞掉 /availability、/entitlements 等
@router.get("/{benefit_id}", response_model=schemas.Benefit)
def get_benefit(benefit_id: int, db: Session = Depends(get_db)):
    benefit = db.query(models.Benefit).filter(models.Benefit.id == benefit_id).first()
    if not benefit:
        raise HTTPException(status_code=404, detail="权益不存在")
    return benefit


# ------------------------------------------------------------- 统计

@router.get("/stats/exchanges", response_model=schemas.ExchangeStats)
def get_exchange_stats(db: Session = Depends(get_db)):
    total_exchanges = db.query(func.count(models.BenefitExchange.id)).scalar() or 0
    pending_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status == models.ExchangeStatus.RESERVED
    ).scalar() or 0
    completed_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.CONFIRMED,
            models.ExchangeStatus.PARTIALLY_FULFILLED,
            models.ExchangeStatus.FULFILLED,
        ])
    ).scalar() or 0
    cancelled_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.CANCELLED, models.ExchangeStatus.REJECTED,
        ])
    ).scalar() or 0
    # 已实际消费的积分（不含仍冻结/已退还的部分），由兑换单汇总
    total_points_spent = db.query(
        func.coalesce(func.sum(models.BenefitExchange.points_consumed), 0)
    ).scalar() or 0

    return schemas.ExchangeStats(
        total_exchanges=total_exchanges,
        pending_exchanges=pending_exchanges,
        completed_exchanges=completed_exchanges,
        cancelled_exchanges=cancelled_exchanges,
        total_points_spent=total_points_spent
    )


@router.get("/stats/by-benefit", response_model=List[schemas.BenefitStats])
def get_benefit_stats(db: Session = Depends(get_db)):
    benefits = db.query(models.Benefit).all()
    result = []
    open_statuses = [
        models.ExchangeStatus.RESERVED, models.ExchangeStatus.CONFIRMED,
        models.ExchangeStatus.PARTIALLY_FULFILLED, models.ExchangeStatus.FULFILLED,
    ]
    for benefit in benefits:
        exchanges = db.query(models.BenefitExchange).filter(
            models.BenefitExchange.benefit_id == benefit.id,
            models.BenefitExchange.status.in_(open_statuses)
        ).all()

        total_exchanged = len(exchanges)
        total_quantity = sum(e.quantity for e in exchanges)
        # 真实消耗积分 = 已消费（已发货/已发资格），不含已退还
        total_points = sum(e.points_consumed for e in exchanges)

        result.append(schemas.BenefitStats(
            benefit_id=benefit.id,
            benefit_name=benefit.name,
            benefit_type=benefit.benefit_type,
            total_exchanged=total_exchanged,
            total_quantity=total_quantity,
            total_points=total_points
        ))

    return result
