from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from datetime import datetime
from database import get_db
import models, schemas
import exchange_service
from exchange_service import ExchangeError

router = APIRouter(prefix="/api/benefits", tags=["权益管理"])


def _handle(exc: ExchangeError):
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/", response_model=List[schemas.Benefit])
def list_benefits(benefit_type: str = None, is_active: bool = None,
                  skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    exchange_service.sweep_expired(db)
    query = db.query(models.Benefit)
    if benefit_type:
        query = query.filter(models.Benefit.benefit_type == benefit_type)
    if is_active is not None:
        query = query.filter(models.Benefit.is_active == is_active)
    return query.order_by(models.Benefit.sort_order, models.Benefit.created_at.desc()).offset(skip).limit(limit).all()


# ---- 优先时段券：必须定义在 /{benefit_id} 动态路由之前，否则会被抢先匹配 ----

@router.get("/coupons", response_model=List[schemas.PriorityCoupon])
def list_coupons(volunteer_id: int = None, status: str = None,
                 db: Session = Depends(get_db)):
    query = db.query(models.PriorityCoupon)
    if volunteer_id:
        query = query.filter(models.PriorityCoupon.volunteer_id == volunteer_id)
    if status:
        for s in models.CouponStatus:
            if s.value == status or s.name == status:
                query = query.filter(models.PriorityCoupon.status == s)
                break
    return query.order_by(models.PriorityCoupon.id.desc()).all()


@router.post("/coupons/{coupon_id}/use", response_model=schemas.PriorityCoupon)
def use_coupon(coupon_id: int, payload: schemas.CouponUse, db: Session = Depends(get_db)):
    try:
        return exchange_service.use_coupon(db, coupon_id, payload.time_slot_id)
    except ExchangeError as exc:
        raise _handle(exc)


# 注意：GET /{benefit_id} 定义在文件后部，所有字面量 GET 路径
# （/coupons、/exchanges、/stats/*、/ledger/*）都必须先于它注册，
# 否则 FastAPI 会先匹配 {benefit_id} 并因 int 校验返回 422。


@router.post("/", response_model=schemas.Benefit)
def create_benefit(benefit: schemas.BenefitCreate, db: Session = Depends(get_db)):
    db_benefit = models.Benefit(**benefit.model_dump())
    db.add(db_benefit)
    db.flush()
    exchange_service.record_initial_stock(db, db_benefit)
    db.commit()
    db.refresh(db_benefit)
    return db_benefit


@router.put("/{benefit_id}", response_model=schemas.Benefit)
def update_benefit(benefit_id: int, benefit_update: schemas.BenefitUpdate, db: Session = Depends(get_db)):
    benefit = db.query(models.Benefit).filter(models.Benefit.id == benefit_id).first()
    if not benefit:
        raise HTTPException(status_code=404, detail="权益不存在")
    update_data = benefit_update.model_dump(exclude_unset=True)

    # 库存是流水账：直接改库存必须追加一条库存更正流水，保证可售库存始终可逐笔解释。
    stock_delta = None
    if "stock" in update_data:
        new_stock = update_data.pop("stock")
        stock_delta = (new_stock or 0) - (benefit.stock or 0)

    for key, value in update_data.items():
        setattr(benefit, key, value)

    if stock_delta:
        benefit.stock = (benefit.stock or 0) + stock_delta
        db.add(models.StockLedger(
            benefit_id=benefit.id, exchange_id=None,
            change_type=(models.StockChangeType.COMPENSATE_IN if stock_delta > 0
                         else models.StockChangeType.COMPENSATE_OUT),
            quantity=stock_delta, committed_delta=0,
            stock_after=benefit.stock,
            committed_after=benefit.committed_quantity or 0,
            remark="后台维护直接调整库存（append-only 更正流水）"))

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


@router.get("/exchanges", response_model=List[schemas.BenefitExchange])
def list_exchanges(volunteer_id: int = None, status: str = None,
                   skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    exchange_service.sweep_expired(db)
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
    return query.order_by(models.BenefitExchange.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/exchanges/volunteer/{volunteer_id}", response_model=List[schemas.BenefitExchange])
def get_volunteer_exchanges(volunteer_id: int, skip: int = 0, limit: int = 100,
                            db: Session = Depends(get_db)):
    volunteer = db.query(models.Volunteer).filter(models.Volunteer.id == volunteer_id).first()
    if not volunteer:
        raise HTTPException(status_code=404, detail="志愿者不存在")

    return db.query(models.BenefitExchange).filter(
        models.BenefitExchange.volunteer_id == volunteer_id
    ).order_by(models.BenefitExchange.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/exchanges/{exchange_id}", response_model=schemas.BenefitExchange)
def get_exchange(exchange_id: int, db: Session = Depends(get_db)):
    exchange = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    return exchange


@router.get("/exchanges/{exchange_id}/events", response_model=List[schemas.ExchangeEvent])
def get_exchange_events(exchange_id: int, db: Session = Depends(get_db)):
    exchange = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    return exchange.events


@router.get("/exchanges/{exchange_id}/stock-ledger")
def get_exchange_stock_ledger(exchange_id: int, db: Session = Depends(get_db)):
    exchange = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    rows = db.query(models.StockLedger).filter(
        models.StockLedger.exchange_id == exchange_id
    ).order_by(models.StockLedger.id).all()
    return [{
        "id": r.id, "benefit_id": r.benefit_id, "change_type": r.change_type.value,
        "quantity": r.quantity, "committed_delta": r.committed_delta,
        "stock_after": r.stock_after,
        "committed_after": r.committed_after, "remark": r.remark,
        "created_at": r.created_at.isoformat()
    } for r in rows]


@router.post("/exchanges", response_model=schemas.BenefitExchange, status_code=201)
def create_exchange(exchange: schemas.BenefitExchangeCreate, db: Session = Depends(get_db)):
    try:
        return exchange_service.reserve_exchange(
            db,
            volunteer_id=exchange.volunteer_id,
            benefit_id=exchange.benefit_id,
            quantity=exchange.quantity,
            delivery_info=exchange.delivery_info,
            notes=exchange.notes,
            request_no=exchange.request_no,
        )
    except ExchangeError as exc:
        raise _handle(exc)


@router.post("/exchanges/{exchange_id}/confirm", response_model=schemas.BenefitExchange)
def confirm_exchange(exchange_id: int, action: schemas.ExchangeAction = None,
                     db: Session = Depends(get_db)):
    try:
        operator = action.operator if action else None
        return exchange_service.confirm_exchange(db, exchange_id, operator=operator)
    except ExchangeError as exc:
        raise _handle(exc)


@router.post("/exchanges/{exchange_id}/reject", response_model=schemas.BenefitExchange)
def reject_exchange(exchange_id: int, action: schemas.ExchangeAction = None,
                    db: Session = Depends(get_db)):
    try:
        operator = action.operator if action else None
        return exchange_service.reject_exchange(db, exchange_id, operator=operator)
    except ExchangeError as exc:
        raise _handle(exc)


@router.post("/exchanges/{exchange_id}/cancel", response_model=schemas.BenefitExchange)
def cancel_exchange(exchange_id: int, action: schemas.ExchangeAction = None,
                    db: Session = Depends(get_db)):
    try:
        operator = action.operator if action else None
        return exchange_service.cancel_exchange(db, exchange_id, operator=operator)
    except ExchangeError as exc:
        raise _handle(exc)


@router.post("/exchanges/{exchange_id}/fulfill", response_model=schemas.BenefitExchange)
def fulfill_exchange(exchange_id: int, payload: schemas.ExchangeFulfill,
                     db: Session = Depends(get_db)):
    try:
        return exchange_service.fulfill_exchange(
            db, exchange_id,
            quantity=payload.quantity,
            release_remaining=payload.release_remaining,
            delivery_info=payload.delivery_info,
            operator=payload.operator,
            remark=payload.remark,
            request_no=payload.request_no,
        )
    except ExchangeError as exc:
        raise _handle(exc)


@router.post("/exchanges/{exchange_id}/compensations",
             response_model=schemas.ManualCompensation, status_code=201)
def add_compensation(exchange_id: int, payload: schemas.ManualCompensationCreate,
                     db: Session = Depends(get_db)):
    try:
        return exchange_service.add_compensation(
            db, exchange_id,
            compensation_type=payload.compensation_type,
            amount=payload.amount,
            reason=payload.reason,
            operator=payload.operator,
            request_no=payload.request_no,
        )
    except ExchangeError as exc:
        raise _handle(exc)


@router.get("/exchanges/{exchange_id}/compensations",
            response_model=List[schemas.ManualCompensation])
def list_compensations(exchange_id: int, db: Session = Depends(get_db)):
    exchange = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    return exchange.compensations


# ---- 优先时段券端点见文件前部（需先于 /{benefit_id} 注册） ----


@router.put("/exchanges/{exchange_id}", response_model=schemas.BenefitExchange, include_in_schema=False)
def update_exchange(exchange_id: int, exchange_update: schemas.BenefitExchangeUpdate,
                    db: Session = Depends(get_db)):
    # 旧的通用改状态入口保留，但只允许补充收货信息；状态变更必须走
    # confirm/reject/cancel/fulfill 等明确原因的动作接口。
    exchange = db.query(models.BenefitExchange).filter(models.BenefitExchange.id == exchange_id).first()
    if not exchange:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    if exchange_update.status is not None and exchange_update.status != exchange.status:
        raise HTTPException(
            status_code=400,
            detail="不允许直接修改状态，请使用 /confirm、/reject、/cancel、/fulfill 接口")
    if exchange_update.delivery_info is not None:
        exchange.delivery_info = exchange_update.delivery_info
    if exchange_update.notes is not None:
        exchange.notes = exchange_update.notes
    db.commit()
    db.refresh(exchange)
    return exchange


@router.get("/ledger/reconcile")
def get_reconcile(volunteer_id: int = None, benefit_id: int = None,
                  db: Session = Depends(get_db)):
    """从流水重建可用积分/冻结积分/可售库存/已承诺数量并与当前值核对。"""
    return exchange_service.reconcile(db, volunteer_id=volunteer_id, benefit_id=benefit_id)


@router.get("/stats/exchanges", response_model=schemas.ExchangeStats)
def get_exchange_stats(db: Session = Depends(get_db)):
    exchange_service.sweep_expired(db)
    total_exchanges = db.query(func.count(models.BenefitExchange.id)).scalar() or 0
    reserved_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.RESERVED, models.ExchangeStatus.PENDING])
    ).scalar() or 0
    confirmed_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status.in_([
            models.ExchangeStatus.CONFIRMED, models.ExchangeStatus.PARTIALLY_FULFILLED])
    ).scalar() or 0
    completed_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status == models.ExchangeStatus.COMPLETED
    ).scalar() or 0
    cancelled_statuses = [
        models.ExchangeStatus.CANCELLED,
        models.ExchangeStatus.TIMEOUT_CANCELLED,
        models.ExchangeStatus.REJECTED,
    ]
    cancelled_exchanges = db.query(func.count(models.BenefitExchange.id)).filter(
        models.BenefitExchange.status.in_(cancelled_statuses)
    ).scalar() or 0
    # 实际消费积分 = 已结算毛额 - 已退回（部分履约/补偿），取消的预占不计入。
    total_points_spent = db.query(
        func.coalesce(func.sum(
            models.BenefitExchange.points_settled - models.BenefitExchange.points_refunded), 0)
    ).scalar() or 0

    return schemas.ExchangeStats(
        total_exchanges=total_exchanges,
        pending_exchanges=reserved_exchanges,
        completed_exchanges=completed_exchanges + confirmed_exchanges,
        cancelled_exchanges=cancelled_exchanges,
        total_points_spent=total_points_spent
    )


@router.get("/stats/by-benefit", response_model=List[schemas.BenefitStats])
def get_benefit_stats(db: Session = Depends(get_db)):
    benefits = db.query(models.Benefit).all()
    result = []

    cancelled_statuses = [
        models.ExchangeStatus.CANCELLED,
        models.ExchangeStatus.TIMEOUT_CANCELLED,
        models.ExchangeStatus.REJECTED,
    ]
    for benefit in benefits:
        exchanges = db.query(models.BenefitExchange).filter(
            models.BenefitExchange.benefit_id == benefit.id,
            ~models.BenefitExchange.status.in_(cancelled_statuses)
        ).all()

        total_exchanged = len(exchanges)
        total_quantity = sum((e.fulfilled_quantity or 0) + (e.reserved_quantity or 0) for e in exchanges)
        total_points = sum(
            (e.points_settled or 0) - (e.points_refunded or 0) + (e.points_frozen or 0)
            for e in exchanges
        )

        result.append(schemas.BenefitStats(
            benefit_id=benefit.id,
            benefit_name=benefit.name,
            benefit_type=benefit.benefit_type,
            total_exchanged=total_exchanged,
            total_quantity=total_quantity,
            total_points=total_points
        ))

    return result


# 动态路由必须最后注册：确保 /coupons、/exchanges、/stats/*、/ledger/* 等字面量路径优先匹配
@router.get("/{benefit_id}", response_model=schemas.Benefit)
def get_benefit(benefit_id: int, db: Session = Depends(get_db)):
    benefit = db.query(models.Benefit).filter(models.Benefit.id == benefit_id).first()
    if not benefit:
        raise HTTPException(status_code=404, detail="权益不存在")
    return benefit
