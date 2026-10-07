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

## 权益兑换：可恢复的预占流程

兑换不再"一步扣分减库存"，而是拆成资源锁定 → 确认消费的 Saga，全程由
**不可变流水**（`points_ledgers` 积分账、`inventory_records` 库存账）解释：

| 阶段 | 接口 | 资源动作 |
| --- | --- | --- |
| 申请 | `POST /api/benefits/exchanges`（带 `idempotency_key`） | 同一事务内 `FREEZE` 积分 + `RESERVE` 库存，进入「已预占」 |
| 后台确认 | `POST /exchanges/{id}/confirm` | 优先时段券：发资格并完成消费（资格在认领时段时才用掉）；实物：进入「已确认」待发货 |
| 实物发货 | `POST /exchanges/{id}/fulfill` | 支持分批与**部分履约**（商品开启 `allow_partial_fulfillment`），未发部分按「部分履约库存不足」解冻积分、释放库存 |
| 拒绝 / 取消 | `POST /exchanges/{id}/reject`、`/cancel` | 按明确原因 `UNFREEZE` + `RELEASE` 全额释放 |
| 超时回收 | `POST /exchanges-timeouts/sweep` | 超过 `reserve_timeout_seconds` 未确认的预占单按「超时未确认」释放，可重复执行 |
| 资格消费 | `POST /api/benefits/entitlements/consume` | 优先时段券真正认领时段时才 `USED` |
| 人工更正 | `POST /api/benefits/compensations` | **只能追加**补偿记录（积分/库存带符号调整），原始兑换单与流水永不回改 |

关键保证：

- **幂等**：相同 `idempotency_key` 的重复请求（家长超时重试、后台重复确认）返回同一结果；
  同一键但载荷变化返回 `409` 冲突。并发同键请求会排队等待首个请求落定后回放。
- **原子锁定**：申请时对志愿者行与商品行加锁，积分或库存不足整体回滚，不留半成品。
- **不会多退**：每条阶段流水带唯一幂等键，重复取消/拒绝不会二次退还。
- **可解释对账**：
  - `GET /api/benefits/reconciliation/points?volunteer_id=`：可用积分、冻结积分与积分流水汇总逐项核对；
  - `GET /api/benefits/reconciliation/inventory`：`可售 + 已承诺 + 已售 = 盘点库存 + 人工补偿`；
  - `GET /api/benefits/exchanges/{id}/timeline`：单笔兑换的状态、积分流水、库存流水、资格与补偿全轨迹；
  - `GET /api/benefits/availability`：各商品实时可售/已承诺/已售视图。

