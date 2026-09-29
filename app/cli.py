from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_habitat_demo() -> int:
    """通过一组本地接口请求核对生境维护窗口：规则版本、计划状态、撤回原因与地块历史。"""
    import time

    suffix = str(int(time.time()))
    with TestClient(app) as client:
        plot = client.post("/api/habitat/plots", json={
            "code": f"HD-{suffix[-6:]}", "name": "生境演示滩涂", "wetland_type": "滩涂", "area_mu": 18.0,
        })
        if plot.status_code != 201:
            print(plot.text)
            return 1
        plot_id = plot.json()["id"]
        window = client.post("/api/habitat/work-windows", json={
            "plot_id": plot_id, "start_on": "2026-09-01", "end_on": "2026-11-30", "crew": "演示班组",
        })
        plan_a = client.post("/api/habitat/plans", json={
            "plot_id": plot_id, "window_id": window.json()["id"], "planned_on": "2026-10-20", "created_by": "manager",
        })
        plan_b = client.post("/api/habitat/plans", json={
            "plot_id": plot_id, "window_id": window.json()["id"], "planned_on": "2026-11-10", "created_by": "manager",
        })
        # 鸟巢占用观察立即延后计划，并指出是哪条观察阻止了操作。
        client.post("/api/habitat/observations", json={
            "plot_id": plot_id, "kind": "nest_occupation", "species": "黑翅长脚鹬",
            "observed_on": "2026-09-26", "observer": "patrol",
        })
        blocked = client.get(f"/api/habitat/plans/{plan_a.json()['id']}").json()
        # 管理者调整规则：只影响尚未确认的计划。
        adjusted = client.post("/api/habitat/rules/adjust", json={
            "actor": "manager", "reason": "演示：繁殖季提前结束", "nest_buffer_days": 0,
        })
        # 撤回尚未开始的安排并登记原因。
        withdrawn = client.post(f"/api/habitat/plans/{plan_b.json()['id']}/withdraw", json={
            "actor": "dispatcher", "reason": "演示撤回：班组另有任务",
        })
        rules = client.get("/api/habitat/rules").json()
        history = client.get(f"/api/habitat/plots/{plot_id}/history").json()
    result = {
        "plot_id": plot_id,
        "rule_version": rules["version"],
        "blocked_plan_status": blocked["status"],
        "blocker_observation_id": blocked["basis"]["blockers"][0]["observation_id"],
        "blocker_message": blocked["basis"]["blockers"][0]["message"],
        "rule_adjustment_affected": adjusted.json()["affected_plan_ids"],
        "withdrawn_status": withdrawn.json()["status"],
        "withdraw_reason": withdrawn.json()["withdraw_reason"],
        "history_timeline_events": len(history["timeline"]),
    }
    print(json.dumps(result, ensure_ascii=False))
    ok = (
        blocked["status"] == "postponed"
        and blocked["basis"]["blockers"][0]["observation_id"]
        and adjusted.status_code == 201
        and withdrawn.json()["status"] == "withdrawn"
        and withdrawn.json()["withdraw_reason"]
        and len(history["timeline"]) >= 4
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("habitat-demo", help="执行生境维护窗口规则与撤回演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "habitat-demo": command_habitat_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
