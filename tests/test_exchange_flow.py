"""可恢复预占兑换流程测试：

覆盖：原子锁定、重试幂等、载荷冲突、确认/拒绝/取消/超时释放、
实物部分履约退积分、优先时段券真正核销、人工补偿只追加、并发抢库存、
以及任何时点都能用流水重建四类余额且全部一致。
"""
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from database import Base, engine, SessionLocal
import models
import exchange_service
from main import app  # noqa: F401  导入即建表/初始化


client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    school = models.School(name="测试小学")
    db.add(school)
    db.flush()

    def make_volunteer(name, points):
        v = models.Volunteer(
            name=name, school_id=school.id, grade="五年级",
            status=models.VolunteerStatus.CERTIFIED,
            points_balance=points, points_frozen=0)
        db.add(v)
        db.flush()
        db.add(models.PointsRecord(
            volunteer_id=v.id, points_type=models.PointsType.EARN,
            points_amount=points, source=models.PointsSource.OTHER,
            description="测试初始积分"))
        return v

    v1 = make_volunteer("张同学", 500)
    v2 = make_volunteer("李同学", 500)
    badge = models.Benefit(name="金属徽章", benefit_type=models.BenefitType.BADGE,
                           points_cost=100, stock=10, is_active=True)
    coupon = models.Benefit(name="周末优先券", benefit_type=models.BenefitType.PRIORITY_SLOT,
                            points_cost=50, stock=10, is_active=True)
    db.add_all([badge, coupon])
    db.flush()
    # 建档即记初始入库流水（与 POST /api/benefits 一致）
    exchange_service.record_initial_stock(db, badge)
    exchange_service.record_initial_stock(db, coupon)
    db.commit()
    ids = {"v1": v1.id, "v2": v2.id, "badge": badge.id, "coupon": coupon.id}
    # 必须关闭 fixture 会话，避免其挂着打开的事务与应用请求的写事务互斥
    db.close()
    yield ids


# ---------------------------------------------------------------- helpers

def exchange_detail(exchange_id):
    return client.get(f"/api/benefits/exchanges/{exchange_id}").json()


def reconcile(volunteer_id=None, benefit_id=None):
    params = {}
    if volunteer_id:
        params["volunteer_id"] = volunteer_id
    if benefit_id:
        params["benefit_id"] = benefit_id
    data = client.get("/api/benefits/ledger/reconcile", params=params).json()
    assert data["all_consistent"] is True, data
    return data


def volunteer_points(vid):
    return client.get(f"/api/points/volunteer/{vid}").json()


def benefit(benefit_id):
    return client.get(f"/api/benefits/{benefit_id}").json()


# ---------------------------------------------------------------- 1. 预占

def test_reserve_locks_points_and_stock_atomically(fresh_db):
    ctx = fresh_db
    r = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 2, "request_no": "r-1"})
    assert r.status_code == 201, r.text
    ex = r.json()
    assert ex["status"] == "已预占"
    assert ex["points_frozen"] == 200 and ex["reserved_quantity"] == 2

    p = volunteer_points(ctx["v1"])
    assert p["points_balance"] == 500          # 总额未扣
    assert p["points_frozen"] == 200           # 冻结
    assert p["points_available"] == 300        # 可用减少

    b = benefit(ctx["badge"])
    assert b["stock"] == 8                     # 可售已锁定
    assert b["committed_quantity"] == 2        # 承诺增加

    reconcile(ctx["v1"], ctx["badge"])


def test_reserve_rejects_insufficient_available_points(fresh_db):
    ctx = fresh_db
    # 先冻结 450
    client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 4, "request_no": "r-freeze"})
    # 可用只剩 100，再申请 2 份（200）必须失败，且不得写任何账
    r = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 2, "request_no": "r-2"})
    assert r.status_code == 400
    assert "积分不足" in r.json()["detail"]
    assert benefit(ctx["badge"])["stock"] == 6
    reconcile(ctx["v1"], ctx["badge"])


def test_reserve_rejects_insufficient_stock(fresh_db):
    ctx = fresh_db
    r = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 11, "request_no": "r-3"})
    assert r.status_code == 400 and "库存不足" in r.json()["detail"]
    assert benefit(ctx["badge"])["stock"] == 10
    reconcile(benefit_id=ctx["badge"])


# ---------------------------------------------------------------- 2. 幂等/冲突

def test_duplicate_request_returns_same_result(fresh_db):
    ctx = fresh_db
    payload = {"volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
               "quantity": 1, "delivery_info": "家里", "request_no": "idem-1"}
    r1 = client.post("/api/benefits/exchanges", json=payload)
    r2 = client.post("/api/benefits/exchanges", json=payload)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert benefit(ctx["badge"])["committed_quantity"] == 1


def test_same_request_no_with_changed_payload_is_conflict(fresh_db):
    ctx = fresh_db
    client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "idem-2"})
    r = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 3, "request_no": "idem-2"})
    assert r.status_code == 409 and "冲突" in r.json()["detail"]
    assert benefit(ctx["badge"])["committed_quantity"] == 1


# ---------------------------------------------------------------- 3. 确认

def test_confirm_settles_frozen_points(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 2, "request_no": "c-1"}).json()["id"]

    r = client.post(f"/api/benefits/exchanges/{ex_id}/confirm", json={"operator": "后台"})
    assert r.status_code == 200
    ex = r.json()
    assert ex["status"] == "已确认待履约"
    assert ex["points_settled"] == 200 and ex["points_frozen"] == 0

    p = volunteer_points(ctx["v1"])
    assert p["points_balance"] == 300 and p["points_frozen"] == 0
    # 确认只是把预占转为承诺，库存仍待实物复品出库
    assert benefit(ctx["badge"])["committed_quantity"] == 2
    reconcile(ctx["v1"], ctx["badge"])


def test_double_confirm_is_idempotent_not_double_settle(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "c-2"}).json()["id"]
    first = client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    assert first.status_code == 200
    # 重复确认返回同一结果（同一状态、不重复结算）
    r = client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    assert r.status_code == 200
    assert r.json()["id"] == ex_id and r.json()["points_settled"] == 100
    assert volunteer_points(ctx["v1"])["points_balance"] == 400


# ---------------------------------------------------------------- 4. 释放

@pytest.mark.parametrize("action,reason,status", [
    ("reject", "后台拒绝", "已拒绝"),
    ("cancel", "家长取消", "已取消"),
])
def test_reject_and_cancel_release_with_explicit_reason(fresh_db, action, reason, status):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 2, "request_no": f"{action}-1"}).json()["id"]

    r = client.post(f"/api/benefits/exchanges/{ex_id}/{action}")
    assert r.status_code == 200
    ex = r.json()
    assert ex["status"] == status and ex["cancel_reason"] == reason
    assert ex["released_quantity"] == 2 and ex["points_frozen"] == 0

    p = volunteer_points(ctx["v1"])
    assert p["points_balance"] == 500 and p["points_available"] == 500
    b = benefit(ctx["badge"])
    assert b["stock"] == 10 and b["committed_quantity"] == 0

    events = client.get(f"/api/benefits/exchanges/{ex_id}/events").json()
    assert events[-1]["reason"] == reason

    # 同动作重试返回同一结果，不二次释放
    again = client.post(f"/api/benefits/exchanges/{ex_id}/{action}")
    assert again.status_code == 200 and again.json()["id"] == ex_id
    assert benefit(ctx["badge"])["stock"] == 10
    reconcile(ctx["v1"], ctx["badge"])


def test_cancel_then_confirm_must_fail(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "x-1"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex_id}/cancel")
    r = client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    assert r.status_code == 409
    assert volunteer_points(ctx["v1"])["points_balance"] == 500


def test_timeout_releases_reserved_exchange(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "t-1"}).json()["id"]

    # 把到期时间拨到过去，再触发任意会扫描超时单的接口
    db = SessionLocal()
    ex = db.get(models.BenefitExchange, ex_id)
    ex.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()

    client.get("/api/benefits/")
    ex = exchange_detail(ex_id)
    assert ex["status"] == "超时取消" and ex["cancel_reason"] == "超时释放"
    assert benefit(ctx["badge"])["stock"] == 10
    assert volunteer_points(ctx["v1"])["points_available"] == 500
    reconcile(ctx["v1"], ctx["badge"])


# ---------------------------------------------------------------- 5. 部分履约

def test_physical_benefit_partial_fulfillment_refunds_remainder(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 3, "request_no": "f-1"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex_id}/confirm")

    # 先复品 1 份（仍有 2 份待履约）
    r = client.post(f"/api/benefits/exchanges/{ex_id}/fulfill", json={
        "quantity": 1, "request_no": "fulfill-batch-1"})
    assert r.status_code == 200 and r.json()["status"] == "部分履约"
    assert benefit(ctx["badge"])["committed_quantity"] == 2

    # 再复品 1 份并明确终止：剩余 1 份释放回库并按锁定价退 100 积分
    r = client.post(f"/api/benefits/exchanges/{ex_id}/fulfill", json={
        "quantity": 1, "release_remaining": True, "delivery_info": "已发2份"})
    assert r.status_code == 200
    ex = r.json()
    assert ex["status"] == "部分履约"
    assert ex["fulfilled_quantity"] == 2 and ex["released_quantity"] == 1
    assert ex["points_settled"] == 300 and ex["points_refunded"] == 100

    b = benefit(ctx["badge"])
    # 初始10：预占出库3(剩7) + 部分释放回库1 = 可售8；已履约2，committed=0
    assert b["stock"] == 8 and b["committed_quantity"] == 0
    p = volunteer_points(ctx["v1"])
    assert p["points_balance"] == 300  # 500 - 实付200
    reconcile(ctx["v1"], ctx["badge"])


def test_fulfillment_request_no_is_idempotent(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 3, "request_no": "f-2"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    payload = {"quantity": 1, "request_no": "dup-batch"}
    assert client.post(f"/api/benefits/exchanges/{ex_id}/fulfill", json=payload).status_code == 200
    assert client.post(f"/api/benefits/exchanges/{ex_id}/fulfill", json=payload).status_code == 200
    assert exchange_detail(ex_id)["fulfilled_quantity"] == 1


# ---------------------------------------------------------------- 6. 优先券

def test_priority_coupon_consumed_only_when_used_on_slot(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["coupon"],
        "quantity": 1, "request_no": "p-1"}).json()["id"]

    # 确认前没有券
    assert client.get("/api/benefits/coupons", params={"volunteer_id": ctx["v1"]}).json() == []
    r = client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "已完成"

    coupons = client.get("/api/benefits/coupons", params={"volunteer_id": ctx["v1"]}).json()
    assert len(coupons) == 1 and coupons[0]["status"] == "已发放"
    coupon_id = coupons[0]["id"]

    # 建一个可认领时段，用券核销
    slot = client.post("/api/time-slots/", json={
        "slot_date": "2026-11-01", "start_time": "09:00", "end_time": "10:00",
        "topic": "周末专场"}).json()
    r = client.post(f"/api/benefits/coupons/{coupon_id}/use", json={"time_slot_id": slot["id"]})
    assert r.status_code == 200 and r.json()["status"] == "已使用"

    slot_after = client.get(f"/api/time-slots/{slot['id']}").json()
    assert slot_after["status"] == "已认领" and slot_after["volunteer_id"] == ctx["v1"]

    # 同券同时段重复核销：幂等返回同一结果
    r2 = client.post(f"/api/benefits/coupons/{coupon_id}/use", json={"time_slot_id": slot["id"]})
    assert r2.status_code == 200 and r2.json()["id"] == coupon_id

    # 时段取消：券资格退回，可再次使用
    client.post(f"/api/time-slots/{slot['id']}/cancel")
    coupon_after = client.get("/api/benefits/coupons").json()[0]
    assert coupon_after["status"] == "已发放" and coupon_after["time_slot_id"] is None
    reconcile(ctx["v1"], ctx["coupon"])


# ---------------------------------------------------------------- 7. 人工补偿

def test_manual_correction_is_append_only_compensation(fresh_db):
    ctx = fresh_db
    ex_id = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "m-1"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex_id}/confirm")
    assert volunteer_points(ctx["v1"])["points_balance"] == 400

    # 补退 30 积分（善意补偿）
    r = client.post(f"/api/benefits/exchanges/{ex_id}/compensations", json={
        "compensation_type": "补退积分", "amount": 30,
        "reason": "复品有划痕", "operator": "主管", "request_no": "comp-1"})
    assert r.status_code == 201
    points_comp_id = r.json()["id"]
    assert volunteer_points(ctx["v1"])["points_balance"] == 430

    # 补回 2 份库存
    r = client.post(f"/api/benefits/exchanges/{ex_id}/compensations", json={
        "compensation_type": "补回库存", "amount": 2, "reason": "盘点盘盈"})
    assert r.status_code == 201
    b = benefit(ctx["badge"])
    # 实物确认但尚未复品时，库存仍计入已承诺；补偿回库只增可售
    assert b["stock"] == 11 and b["committed_quantity"] == 1  # 10-1+2

    # 补偿请求重试幂等：返回同一条且不再次加积分（状态码与首次一致）
    dup = client.post(f"/api/benefits/exchanges/{ex_id}/compensations", json={
        "compensation_type": "补退积分", "amount": 30,
        "reason": "复品有划痕", "request_no": "comp-1"})
    assert dup.status_code == 201
    assert dup.json()["id"] == points_comp_id
    assert volunteer_points(ctx["v1"])["points_balance"] == 430

    # 必须有原因
    bad = client.post(f"/api/benefits/exchanges/{ex_id}/compensations", json={
        "compensation_type": "补退积分", "amount": 1, "reason": ""})
    assert bad.status_code == 400

    comps = client.get(f"/api/benefits/exchanges/{ex_id}/compensations").json()
    assert len(comps) == 2
    reconcile(ctx["v1"], ctx["badge"])


# ---------------------------------------------------------------- 8. 并发

def test_concurrent_reserve_of_last_stock_only_one_wins(fresh_db):
    ctx = fresh_db
    # 通过后台库存调整（append-only 补偿流水）把可售库存调到 1
    r = client.put(f"/api/benefits/{ctx['badge']}", json={"stock": 1})
    assert r.status_code == 200 and r.json()["stock"] == 1

    barrier = threading.Barrier(2)
    outcomes = []

    def reserve(vid, req_no):
        # TestClient 不能跨线程使用，直接用独立会话调用服务层
        session = SessionLocal()
        try:
            barrier.wait()
            try:
                exchange_service.reserve_exchange(
                    session, volunteer_id=vid, benefit_id=ctx["badge"],
                    quantity=1, request_no=req_no)
                outcomes.append("ok")
            except exchange_service.ExchangeError as e:
                outcomes.append(f"err:{e.status_code}")
        finally:
            session.close()

    t1 = threading.Thread(target=reserve, args=(ctx["v1"], "cc-1"))
    t2 = threading.Thread(target=reserve, args=(ctx["v2"], "cc-2"))
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    assert sorted(outcomes) == ["err:400", "ok"], outcomes
    b = benefit(ctx["badge"])
    assert b["stock"] == 0 and b["committed_quantity"] == 1
    reconcile(benefit_id=ctx["badge"])


# ---------------------------------------------------------------- 9. 流水解释

def test_ledger_explains_every_number_end_to_end(fresh_db):
    ctx = fresh_db
    # v1: 预占 2 → 确认 → 复品1 + 余量1释放退款
    ex1 = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v1"], "benefit_id": ctx["badge"],
        "quantity": 2, "request_no": "e2e-1"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex1}/confirm")
    client.post(f"/api/benefits/exchanges/{ex1}/fulfill",
                json={"quantity": 1, "release_remaining": True})

    # v2: 预占 1 → 取消
    ex2 = client.post("/api/benefits/exchanges", json={
        "volunteer_id": ctx["v2"], "benefit_id": ctx["badge"],
        "quantity": 1, "request_no": "e2e-2"}).json()["id"]
    client.post(f"/api/benefits/exchanges/{ex2}/cancel")

    data = reconcile()
    v1_row = next(v for v in data["volunteers"] if v["volunteer_id"] == ctx["v1"])
    v2_row = next(v for v in data["volunteers"] if v["volunteer_id"] == ctx["v2"])
    b_row = next(b for b in data["benefits"] if b["benefit_id"] == ctx["badge"])

    # v1 实付 100；v2 全额退回
    assert v1_row["points_balance"] == 400
    assert v1_row["points_frozen"] == 0 and v1_row["points_available"] == 400
    assert v2_row["points_balance"] == 500 and v2_row["points_available"] == 500
    # 10 -2(预占) +1(部分释放) -1(再预占) +1(取消释放) = 9；无在途承诺；已履约1
    assert b_row["sellable_stock"] == 9 and b_row["committed_quantity"] == 0
    assert b_row["fulfilled_quantity"] == 1 and b_row["released_quantity"] == 2
    assert data["all_consistent"] is True
