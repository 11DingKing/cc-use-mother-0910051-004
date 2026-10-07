"""兑换预占流程端到端测试。

覆盖：
- 申请原子锁定积分与库存；不足时不留任何痕迹
- 幂等：同键重复返回同一单，载荷变化 409
- 确认/发货、拒绝/取消/超时按明确原因释放
- 实物部分履约按比例退还，不允许多退
- 优先时段券发资格与真正使用时消费分离
- 人工更正只能追加补偿
- 任意时刻积分/库存对账一致、流水可解释
- 家长重试与后台确认交错时结果收敛为同一状态
"""
import pytest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from main import app
from seed_data import seed_data
import models
from routers.points import add_points
from services import exchange_flow


@pytest.fixture()
def world(tmp_path):
    db_file = tmp_path / "test_exchange.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed = TestingSessionLocal()
    seed_data(seed)
    seed.commit()
    seed.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    db = TestingSessionLocal()
    volunteer = db.query(models.Volunteer).filter(
        models.Volunteer.status == models.VolunteerStatus.CERTIFIED).first()
    add_points(db, volunteer.id, 10000, models.PointsSource.OTHER,
               description="测试初始积分", commit=True)
    vid = volunteer.id
    db.close()

    yield {
        "client": client,
        "SessionLocal": TestingSessionLocal,
        "vid": vid,
    }

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ------------------------------------------------------------- helpers

def make_benefit(client, **overrides):
    payload = {
        "name": "测试权益",
        "benefit_type": models.BenefitType.BADGE.value,
        "points_cost": 100,
        "stock": 5,
    }
    payload.update(overrides)
    r = client.post("/api/benefits/", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def availability(client, benefit_id):
    items = {b["benefit_id"]: b for b in client.get("/api/benefits/availability").json()}
    return items[benefit_id]


def points_view(client, vid):
    return client.get(f"/api/points/volunteer/{vid}").json()


def points_ok(client, vid):
    return client.get(f"/api/benefits/reconciliation/points",
                      params={"volunteer_id": vid}).json()["consistent"]


def inventory_ok(client):
    return client.get("/api/benefits/reconciliation/inventory").json()["consistent"]


def apply(client, vid, bid, qty=1, key=None, extra=None):
    body = {"volunteer_id": vid, "benefit_id": bid, "quantity": qty}
    if key:
        body["idempotency_key"] = key
    if extra:
        body.update(extra)
    return client.post("/api/benefits/exchanges", json=body)


def force_expired(session_factory, exchange_id, seconds=1):
    db = session_factory()
    ex = db.query(models.BenefitExchange).get(exchange_id)
    ex.reserve_expires_at = datetime.utcnow() - timedelta(seconds=seconds)
    db.commit()
    db.close()


# ------------------------------------------------------------- 申请预占

def test_apply_locks_points_and_stock_atomically(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=2, points_cost=100)

    r = apply(c, vid, bid, 1, key="apply-1")
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["status"] == models.ExchangeStatus.RESERVED.value
    assert ex["points_spent"] == 100

    pv = points_view(c, vid)
    assert pv["points_balance"] == 9900          # 可用已扣减
    assert pv["frozen_points"] == 100            # 冻结可解释
    assert pv["available_points"] == 9900

    av = availability(c, bid)
    assert av["available_quantity"] == 1
    assert av["reserved_quantity"] == 1
    assert av["sold_quantity"] == 0
    assert points_ok(c, vid) and inventory_ok(c)


def test_insufficient_stock_or_points_leaves_no_trace(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=1, points_cost=100)

    r = apply(c, vid, bid, 2, key="apply-too-many")
    assert r.status_code == 400 and "库存不足" in r.json()["detail"]

    # 积分与库存都没有任何残留
    pv = points_view(c, vid)
    assert pv["points_balance"] == 10000 and pv["frozen_points"] == 0
    av = availability(c, bid)
    assert av["available_quantity"] == 1 and av["reserved_quantity"] == 0

    # 积分不足：给一个新志愿者 50 分
    db = world["SessionLocal"]()
    poor = models.Volunteer(name="积分不足同学", school_id=1,
                            status=models.VolunteerStatus.CERTIFIED)
    db.add(poor); db.commit(); db.refresh(poor)
    add_points(db, poor.id, 50, models.PointsSource.OTHER, "少量积分", commit=True)
    poor_id = poor.id; db.close()

    r = apply(c, poor_id, bid, 1, key="apply-poor")
    assert r.status_code == 400 and "积分不足" in r.json()["detail"]
    av = availability(c, bid)
    assert av["available_quantity"] == 1 and av["reserved_quantity"] == 0
    assert points_ok(c, poor_id) and inventory_ok(c)


# ------------------------------------------------------------- 幂等/冲突

def test_same_request_returns_same_exchange(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=3, points_cost=100)

    first = apply(c, vid, bid, 1, key="idem-key").json()
    second = apply(c, vid, bid, 1, key="idem-key")
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]

    av = availability(c, bid)
    assert av["reserved_quantity"] == 1          # 只锁定一次
    pv = points_view(c, vid)
    assert pv["frozen_points"] == 100


def test_payload_change_with_same_key_is_conflict(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=3, points_cost=100)

    apply(c, vid, bid, 1, key="conflict-key")
    r = apply(c, vid, bid, 2, key="conflict-key")
    assert r.status_code == 409

    av = availability(c, bid)
    assert av["reserved_quantity"] == 1          # 冲突载荷未执行
    assert points_ok(c, vid) and inventory_ok(c)


# ------------------------------------------------------------- 确认/发货

def test_confirm_then_fulfill_badge(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=2, points_cost=100)
    eid = apply(c, vid, bid, 1, key="cf-1").json()["id"]

    r = c.post(f"/api/benefits/exchanges/{eid}/confirm", json={"idempotency_key": "cf-confirm"})
    assert r.status_code == 200
    assert r.json()["status"] == models.ExchangeStatus.CONFIRMED.value
    # 确认尚未发货：库存仍属于已承诺，积分仍冻结
    assert availability(c, bid)["reserved_quantity"] == 1
    assert points_view(c, vid)["frozen_points"] == 100

    r = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
               json={"fulfill_quantity": 1, "idempotency_key": "cf-ship"})
    assert r.json()["status"] == models.ExchangeStatus.FULFILLED.value
    av = availability(c, bid)
    assert av["reserved_quantity"] == 0 and av["sold_quantity"] == 1
    pv = points_view(c, vid)
    assert pv["frozen_points"] == 0 and pv["points_balance"] == 9900
    assert points_ok(c, vid) and inventory_ok(c)

    # 已终结单不能再次发货
    r = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
               json={"fulfill_quantity": 1, "idempotency_key": "cf-ship-again"})
    assert r.status_code == 400
    # 用首次幂等键重放则返回同一结果，不会重复发货
    replay = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
                    json={"fulfill_quantity": 1, "idempotency_key": "cf-ship"})
    assert replay.status_code == 200
    assert availability(c, bid)["sold_quantity"] == 1


# ------------------------------------------------------------- 拒绝/取消/超时

def test_reject_releases_everything_with_reason(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=2, points_cost=100)
    eid = apply(c, vid, bid, 1, key="rj-1").json()["id"]

    r = c.post(f"/api/benefits/exchanges/{eid}/reject",
               json={"reason": "资质不符", "idempotency_key": "rj-go"})
    assert r.json()["status"] == models.ExchangeStatus.REJECTED.value
    assert r.json()["close_reason"] == models.ExchangeReleaseReason.REJECTED.value

    pv = points_view(c, vid)
    assert pv["points_balance"] == 10000 and pv["frozen_points"] == 0
    av = availability(c, bid)
    assert av["available_quantity"] == 2 and av["reserved_quantity"] == 0

    # 重复拒绝（新键）必须被挡下，不能多退
    r = c.post(f"/api/benefits/exchanges/{eid}/reject",
               json={"reason": "再拒一次", "idempotency_key": "rj-again"})
    assert r.status_code == 400
    assert points_view(c, vid)["points_balance"] == 10000
    assert points_ok(c, vid) and inventory_ok(c)


def test_cancel_is_idempotent_and_does_not_over_refund(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=2, points_cost=100)
    eid = apply(c, vid, bid, 1, key="cx-1").json()["id"]

    for _ in range(2):  # 同键重放两次
        r = c.post(f"/api/benefits/exchanges/{eid}/cancel",
                   json={"reason": "不想要了", "idempotency_key": "cx-go"})
        assert r.status_code == 200
        assert r.json()["status"] == models.ExchangeStatus.CANCELLED.value

    tl = c.get(f"/api/benefits/exchanges/{eid}/timeline").json()
    unfreeze = [l for l in tl["points_ledgers"]
                if l["ledger_type"] == models.PointsLedgerType.UNFREEZE.value]
    assert len(unfreeze) == 1 and unfreeze[0]["amount"] == 100   # 只退一次
    assert points_view(c, vid)["points_balance"] == 10000
    assert points_ok(c, vid) and inventory_ok(c)


def test_timeout_sweep_recovers_expired_reservations(world):
    c, vid, sf = world["client"], world["vid"], world["SessionLocal"]
    bid = make_benefit(c, stock=2, points_cost=100,
                      reserve_timeout_seconds=60)
    eid = apply(c, vid, bid, 1, key="to-1").json()["id"]
    force_expired(sf, eid)

    r = c.post("/api/benefits/exchanges-timeouts/sweep")
    assert r.json()["count"] == 1 and eid in r.json()["recovered_exchange_ids"]
    ex = c.get(f"/api/benefits/exchanges/{eid}").json()
    assert ex["status"] == models.ExchangeStatus.CANCELLED.value
    assert ex["close_reason"] == models.ExchangeReleaseReason.TIMEOUT.value
    assert points_view(c, vid)["points_balance"] == 10000

    # 再扫一次：幂等，无新增回收
    assert c.post("/api/benefits/exchanges-timeouts/sweep").json()["count"] == 0
    assert inventory_ok(c)


# ------------------------------------------------------------- 部分履约

def test_physical_benefit_partial_fulfillment_refunds_remainder(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, name="限量实物", stock=5, points_cost=100,
                       benefit_type=models.BenefitType.PHYSICAL.value,
                       allow_partial_fulfillment=True)
    eid = apply(c, vid, bid, 5, key="pf-1").json()["id"]
    c.post(f"/api/benefits/exchanges/{eid}/confirm", json={"idempotency_key": "pf-c"})

    r = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
               json={"fulfill_quantity": 3, "idempotency_key": "pf-ship3"})
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["status"] == models.ExchangeStatus.PARTIALLY_FULFILLED.value
    assert ex["fulfilled_quantity"] == 3
    assert ex["points_consumed"] == 300
    assert ex["points_refunded"] == 200            # 剩余 2 件明确退还

    pv = points_view(c, vid)
    assert pv["points_balance"] == 9700           # 10000 - 500 + 200
    assert pv["frozen_points"] == 0
    av = availability(c, bid)
    assert av["sold_quantity"] == 3
    assert av["reserved_quantity"] == 0
    assert av["available_quantity"] == 2          # 未发 2 件回到可售

    tl = c.get(f"/api/benefits/exchanges/{eid}/timeline").json()
    unfreeze = [l for l in tl["points_ledgers"]
                if l["ledger_type"] == models.PointsLedgerType.UNFREEZE.value]
    assert len(unfreeze) == 1 and unfreeze[0]["amount"] == 200
    assert unfreeze[0]["reason"] == models.ExchangeReleaseReason.PARTIAL_SHORTAGE.value

    # 已收尾的部分单不能再发/再取消，杜绝多退
    assert c.post(f"/api/benefits/exchanges/{eid}/fulfill",
                  json={"fulfill_quantity": 1, "idempotency_key": "pf-ship-more"}
                  ).status_code == 400
    assert c.post(f"/api/benefits/exchanges/{eid}/cancel",
                  json={"idempotency_key": "pf-cx"}).status_code == 400
    assert points_view(c, vid)["points_balance"] == 9700
    assert points_ok(c, vid) and inventory_ok(c)


def test_partial_fulfillment_disallowed_must_ship_all_or_reject(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, name="不可部分实物", stock=5, points_cost=100,
                       benefit_type=models.BenefitType.PHYSICAL.value,
                       allow_partial_fulfillment=False)
    eid = apply(c, vid, bid, 3, key="np-1").json()["id"]
    c.post(f"/api/benefits/exchanges/{eid}/confirm", json={"idempotency_key": "np-c"})

    r = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
               json={"fulfill_quantity": 2, "idempotency_key": "np-ship2"})
    assert r.status_code == 400
    # 被挡下后资源仍是完整预占
    assert availability(c, bid)["reserved_quantity"] == 3
    assert points_view(c, vid)["frozen_points"] == 300

    r = c.post(f"/api/benefits/exchanges/{eid}/fulfill",
               json={"fulfill_quantity": 3, "idempotency_key": "np-ship3"})
    assert r.json()["status"] == models.ExchangeStatus.FULFILLED.value
    assert points_ok(c, vid) and inventory_ok(c)


# ------------------------------------------------------------- 优先时段券

def test_priority_slot_issued_on_confirm_consumed_on_claim(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, name="优先时段券", stock=10, points_cost=50,
                       benefit_type=models.BenefitType.PRIORITY_SLOT.value)
    eid = apply(c, vid, bid, 2, key="pri-1").json()["id"]

    r = c.post(f"/api/benefits/exchanges/{eid}/confirm", json={"idempotency_key": "pri-c"})
    assert r.json()["status"] == models.ExchangeStatus.FULFILLED.value

    ents = c.get("/api/benefits/entitlements", params={"volunteer_id": vid}).json()
    issued = [e for e in ents if e["status"] == models.EntitlementStatus.ISSUED.value]
    assert len(issued) == 2                          # 资格已发但尚未使用
    pv = points_view(c, vid)
    assert pv["frozen_points"] == 0 and pv["points_balance"] == 9900
    assert availability(c, bid)["sold_quantity"] == 2

    # 建两个可认领时段
    def new_slot():
        r = c.post("/api/time-slots/", json={
            "slot_date": "2026-11-01", "start_time": "09:00", "end_time": "10:00",
            "topic": "测试"})
        return r.json()["id"]

    s1, s2 = new_slot(), new_slot()

    r = c.post("/api/benefits/entitlements/consume",
               json={"volunteer_id": vid, "time_slot_id": s1, "idempotency_key": "use-1"})
    assert r.status_code == 200
    used_code = r.json()["code"]
    assert r.json()["status"] == models.EntitlementStatus.USED.value
    slot = c.get(f"/api/time-slots/{s1}").json()
    assert slot["status"] == models.TimeSlotStatus.CLAIMED.value
    assert slot["volunteer_id"] == vid

    # 同键重放：返回同一张券，不消耗第二张
    replay = c.post("/api/benefits/entitlements/consume",
                    json={"volunteer_id": vid, "time_slot_id": s1, "idempotency_key": "use-1"})
    assert replay.json()["code"] == used_code
    issued_left = [e for e in c.get("/api/benefits/entitlements",
                                    params={"volunteer_id": vid}).json()
                   if e["status"] == models.EntitlementStatus.ISSUED.value]
    assert len(issued_left) == 1

    # 用掉第二张，再用第三张时应无券可用
    assert c.post("/api/benefits/entitlements/consume",
                  json={"volunteer_id": vid, "time_slot_id": s2, "idempotency_key": "use-2"}
                  ).status_code == 200
    r = c.post("/api/benefits/entitlements/consume",
               json={"volunteer_id": vid, "time_slot_id": s2, "idempotency_key": "use-3"})
    assert r.status_code == 400 and "优先时段券" in r.json()["detail"]
    assert points_ok(c, vid) and inventory_ok(c)


# ------------------------------------------------------------- 人工补偿

def test_manual_correction_is_append_only(world):
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, stock=2, points_cost=100)
    eid = apply(c, vid, bid, 1, key="mc-1").json()["id"]
    c.post(f"/api/benefits/exchanges/{eid}/confirm", json={"idempotency_key": "mc-c"})
    c.post(f"/api/benefits/exchanges/{eid}/fulfill",
           json={"fulfill_quantity": 1, "idempotency_key": "mc-f"})

    before = points_view(c, vid)["points_balance"]
    r = c.post("/api/benefits/compensations", json={
        "exchange_id": eid, "volunteer_id": vid, "benefit_id": bid,
        "points_delta": 50, "inventory_delta": 1,
        "reason": "发货瑕疵补还", "operator": "管理员甲"})
    assert r.status_code == 200, r.text
    assert points_view(c, vid)["points_balance"] == before + 50
    assert availability(c, bid)["available_quantity"] == 2  # 售1补1

    # 原兑换单不被回改
    ex = c.get(f"/api/benefits/exchanges/{eid}").json()
    assert ex["points_consumed"] == 100 and ex["fulfilled_quantity"] == 1
    tl = c.get(f"/api/benefits/exchanges/{eid}/timeline").json()
    assert len(tl["compensations"]) == 1

    # 再补一笔（扣回）也只是追加
    r = c.post("/api/benefits/compensations", json={
        "volunteer_id": vid, "points_delta": -20, "reason": "复核冲回多补"})
    assert r.status_code == 200
    assert points_view(c, vid)["points_balance"] == before + 30

    # 非法补偿（扣成负数）被拒绝且不落记录
    r = c.post("/api/benefits/compensations", json={
        "volunteer_id": vid, "points_delta": -999999, "reason": "非法"})
    assert r.status_code == 400
    comps = c.get("/api/benefits/compensations", params={"volunteer_id": vid}).json()
    assert len(comps) == 2
    assert points_ok(c, vid) and inventory_ok(c)


# ------------------------------------------------------------- 竞争收敛

def test_parent_retry_racing_with_backend_confirm_converges(world):
    """家长超时重试与后台确认交错：所有参与者看到同一个最终结果。"""
    c, vid = world["client"], world["vid"]
    bid = make_benefit(c, name="徽章", stock=2, points_cost=100)

    # 家长首次申请 + 超时重试（同键）
    first = apply(c, vid, bid, 1, key="race-apply").json()
    retry = apply(c, vid, bid, 1, key="race-apply")
    assert retry.json()["id"] == first["id"]
    eid = first["id"]

    # 后台确认并发货
    c.post(f"/api/benefits/exchanges/{eid}/confirm",
           json={"fulfill_quantity": 1, "idempotency_key": "race-confirm"})

    # 家长网络恢复后再次重放申请：仍是同一单，且已经是终态
    again = apply(c, vid, bid, 1, key="race-apply").json()
    assert again["id"] == eid
    assert again["status"] == models.ExchangeStatus.FULFILLED.value

    # 此时家长再点取消必须失败（资源已消费），积分不会被多退
    r = c.post(f"/api/benefits/exchanges/{eid}/cancel",
               json={"reason": "我刚点的取消", "idempotency_key": "race-cancel"})
    assert r.status_code == 400
    pv = points_view(c, vid)
    assert pv["points_balance"] == 9900 and pv["frozen_points"] == 0
    assert availability(c, bid)["sold_quantity"] == 1
    assert points_ok(c, vid) and inventory_ok(c)


def test_reconciliation_explains_everything_after_mixed_ops(world):
    c, vid = world["client"], world["vid"]
    b1 = make_benefit(c, name="徽章A", stock=3, points_cost=100)
    b2 = make_benefit(c, name="实物B", stock=4, points_cost=200,
                      benefit_type=models.BenefitType.PHYSICAL.value,
                      allow_partial_fulfillment=True)

    e1 = apply(c, vid, b1, 1, key="mix-1").json()["id"]          # 预占中
    e2 = apply(c, vid, b2, 2, key="mix-2").json()["id"]
    c.post(f"/api/benefits/exchanges/{e2}/confirm", json={"idempotency_key": "mix-2c"})
    c.post(f"/api/benefits/exchanges/{e2}/fulfill",
           json={"fulfill_quantity": 1, "idempotency_key": "mix-2f"})  # 部分: 发1退1

    rec = c.get("/api/benefits/reconciliation/inventory").json()
    assert rec["consistent"] is True
    by_id = {i["benefit_id"]: i for i in rec["items"]}
    assert by_id[b1]["reserved_quantity"] == 1
    assert by_id[b1]["available_quantity"] == 2
    assert by_id[b2]["sold_quantity"] == 1
    assert by_id[b2]["available_quantity"] == 3
    assert by_id[b2]["reserved_quantity"] == 0

    pr = c.get("/api/benefits/reconciliation/points",
               params={"volunteer_id": vid}).json()
    assert pr["consistent"] is True
    # 冻结 100（e1），已消费 200（e2 一件），退还 200（e2 缺一件）
    assert pr["frozen_points"] == 100
    assert pr["points_balance"] == 10000 - 100 - 200


# ------------------------------------------------------------- 真实并发

def test_concurrent_same_key_creates_single_exchange(world):
    """10 个线程同键并发申请：只有一单、只锁一次积分与库存。"""
    import threading

    sf, vid = world["SessionLocal"], world["vid"]
    c = world["client"]
    bid = make_benefit(c, stock=5, points_cost=100)

    results, errors = [], []

    def worker(i):
        db = sf()
        try:
            ex = exchange_flow.apply_exchange(
                db, volunteer_id=vid, benefit_id=bid, quantity=1,
                idempotency_key="concurrent-same")
            results.append(ex.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 10 and set(results) == {results[0]}
    assert availability(c, bid)["reserved_quantity"] == 1
    assert points_view(c, vid)["frozen_points"] == 100
    assert points_ok(c, vid) and inventory_ok(c)


def test_concurrent_distinct_requests_never_oversell(world):
    """12 个线程争抢库存 3 的商品：恰好 3 单成功，其余明确失败，绝不超卖。"""
    import threading

    sf, vid = world["SessionLocal"], world["vid"]
    c = world["client"]
    bid = make_benefit(c, stock=3, points_cost=100)

    won, lost = [], []

    def worker(i):
        db = sf()
        try:
            ex = exchange_flow.apply_exchange(
                db, volunteer_id=vid, benefit_id=bid, quantity=1,
                idempotency_key=f"race-stock-{i}")
            won.append(ex.id)
        except Exception as exc:  # noqa: BLE001
            lost.append(str(exc))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(won) == 3, f"胜出 {len(won)} 单: {won}"
    assert len(lost) == 9
    assert all("库存不足" in msg for msg in lost), lost
    av = availability(c, bid)
    assert av["reserved_quantity"] == 3 and av["available_quantity"] == 0
    assert points_view(c, vid)["frozen_points"] == 300
    assert points_ok(c, vid) and inventory_ok(c)
