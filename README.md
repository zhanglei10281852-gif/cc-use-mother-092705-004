# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

服务包含**湿地生境维护窗口**边界（`/api/habitat`）：记录滩涂地块、物种迹象（涉禽停歇、鸟巢占用、旱柳萌蘖）、繁殖观察和可用工期，结合版本化保护规则给出可执行（`proposed`）或需延后（`deferred`）的芦苇割除计划。

## 生境维护窗口接口

- `GET/POST /api/habitat/rules`：核对 / 发布保护规则版本（季节窗口、鸟巢与涉禽缓冲天数、旱柳萌蘖保护天数等）。新规则只重新评估尚未确认的计划。
- `POST/GET /api/habitat/plots`、`GET /api/habitat/plots/{id}/history`：地块登记与地块历史（事件流、计划清单、撤回原因）。
- `POST/GET /api/habitat/plots/{id}/signs`：物种迹象（`shorebird_roost` / `nest` / `willow_sprout` / `other`，可带持续窗口）。
- `POST/GET /api/habitat/plots/{id}/breeding`：繁殖观察（阶段、预计离巢日，保护窗口延伸到离巢缓冲期末）。
- `POST/GET /api/habitat/plots/{id}/work-windows`：可用工期登记。
- `POST/GET /api/habitat/plans`、`GET /api/habitat/plans/{id}`：制定 / 核对割除计划。延后计划的 `blocks` 逐条给出阻止操作的观察记录编号、物种与保护窗口。
- `POST /api/habitat/plans/{id}/confirm|start|complete`：确认（冻结当时规则依据）、开工、完工；规则后续调整不影响已确认/已完工计划。
- `POST /api/habitat/plans/{id}/withdraw`：撤回尚未开始的安排，必须填写原因；已开工或已完工不可撤回。
- `POST /api/habitat/plans/{id}/deviations` 与 `/reopen`：登记现场偏差（未完成事项使计划挂起），重新打开时恢复未完成事项后才可完工。


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

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

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
