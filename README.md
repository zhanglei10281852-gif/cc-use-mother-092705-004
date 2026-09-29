# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入、现有生态计算接口，以及湿地生境维护窗口的规则判定、版本冻结、撤回与现场偏差恢复。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

```bash
python -m app.cli habitat-demo
```

`habitat-demo` 通过一组本地接口请求演示生境维护窗口：规则版本、被观察阻断的计划、规则调整对未确认计划的影响、撤回原因与地块历史。

## 湿地生境维护窗口

`/api/habitat` 系列接口记录滩涂地块、物种迹象（涉禽停歇、鸟巢占用、旱柳萌蘖等）、可用工期，并结合版本化保护规则给出**可执行（scheduled）或需延后（postponed）**的割除计划。

判定原则：

- 每条计划在创建、规则调整或迹象变化时按**当时规则版本**重新判定；阻断信息明确给出是哪一条观察（`observation_id`、物种、保护期截止日）阻止了操作。
- 规则调整只重新判定尚未确认的计划（scheduled/postponed/reopened）；计划一经确认开工即冻结依据，已完成维护永久保留当时的规则版本与判定快照。
- 仅尚未开始的安排可撤回（withdrawn），撤回必须填写原因；已确认开工的安排不能撤回。
- 开工后可登记现场偏差；完成比例不足时计划重新打开（reopened）并按最新规则恢复未完成事项的窗口判定，全部完成则归档为已完成维护。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/habitat/rules` / `/api/habitat/rules/versions` | 当前规则与历史版本 |
| POST | `/api/habitat/rules/adjust` | 管理者发布新版本规则（仅影响未确认计划） |
| POST/GET | `/api/habitat/plots`、`/api/habitat/plots/{id}` | 地块登记与详情 |
| GET | `/api/habitat/plots/{id}/history` | 地块历史（迹象、工期、计划、完成维护与时间线） |
| POST | `/api/habitat/observations`、`.../{id}/resolve` | 登记/解除物种迹象并自动重判计划 |
| POST | `/api/habitat/work-windows` | 登记可用工期 |
| POST/GET | `/api/habitat/plans`、`/api/habitat/plans/{id}` | 创建割除计划、查询状态（可按状态过滤） |
| POST | `/api/habitat/plans/{id}/reevaluate` | 手动按当前规则重新判定 |
| POST | `/api/habitat/plans/{id}/withdraw` | 撤回尚未开始的安排（必填原因） |
| POST | `/api/habitat/plans/{id}/confirm` | 现场确认开工（冻结依据） |
| POST | `/api/habitat/plans/{id}/deviation` | 登记现场偏差，未完成则重新打开 |
| POST | `/api/habitat/plans/{id}/complete` | 登记完成并保留当时依据 |
| GET | `/api/habitat/maintenance-records` | 已完成维护及其规则版本/依据快照 |
