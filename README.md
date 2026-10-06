# 青少年志愿讲解成长服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖志愿者、培训考核、服务记录、积分权益、监护关系和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

数据库文件可通过环境变量 `DATABASE_URL` 覆盖（默认 `sqlite:///./redscarf.db`）。

## 权益兑换：可恢复预占流程

兑换不再"申请即扣款减库存"，而是拆分为可恢复的预占（reserve → confirm → fulfill / release）：

1. **申请预占** `POST /api/benefits/exchanges`（携带客户端幂等 `request_no`）
   同一事务内原子完成：校验 → 冻结积分（`points_frozen`）→ 预占库存（`stock-1, committed+1`）。
2. **后台确认** `POST /api/benefits/exchanges/{id}/confirm`
   冻结积分结算为实际消费；优先时段券此时**只发放资格券**。
3. **实物履约** `POST /api/benefits/exchanges/{id}/fulfill`
   支持分批复品与部分履约（`release_remaining=true` 时余量回库并按锁定价退积分）。
4. **释放资源**（原因明确）：
   - `POST /api/benefits/exchanges/{id}/reject` 后台拒绝
   - `POST /api/benefits/exchanges/{id}/cancel` 家长取消
   - 超时未确认（默认 15 分钟）由 `sweep_expired` 自动按"超时释放"处理
   - 三者都解冻积分、库存回库，并在事件流写明原因。
5. **优先时段券真正消费**：`POST /api/benefits/coupons/{coupon_id}/use`
   只有核销到具体讲解时段才消费券资格；时段取消自动退回券。
6. **人工更正** `POST /api/benefits/exchanges/{id}/compensations`
   只能**追加**补偿记录（补退/补扣积分、补回/补扣库存），永不改写历史流水。

### 幂等与冲突

- 相同 `request_no` 重复到达（含超时重试、后台双击）始终返回同一结果，不重复扣减/发放。
- 相同 `request_no` 但申请载荷变化（数量、权益、收货信息等）返回 **409 冲突**。
- 履约/补偿批次各自支持独立的 `request_no` 幂等键。

### 流水与对账

每一次积分与库存变动都只追加、不改写：

- 积分：`points_records`（冻结/解冻/结算支出/结算退回等成对科目）
- 库存：`stock_ledger`（初始入库/预占出库/释放回库/履约出库/补偿出入库）
- 兑换事件：`exchange_events`（每个状态迁移）；逐批复品：`fulfillment_items`

`GET /api/benefits/ledger/reconcile` 从流水重建**可用积分、冻结积分、可售库存、已承诺数量**并与当前值逐项核对，返回 `all_consistent`。单笔兑换恒等式：

```
数量：quantity      = reserved + fulfilled + released
积分：points_spent  = points_frozen + points_settled + points_released
库存：初始量(含补偿) = stock + committed + 已履约数量
```
