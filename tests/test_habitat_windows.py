from __future__ import annotations


def make_plot(client, code: str = "T-01") -> int:
    response = client.post("/api/habitat/plots", json={
        "code": code, "name": "东侧芦苇滩涂", "wetland_type": "滩涂", "area_mu": 32.5,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_window(client, plot_id: int, start: str = "2026-09-01", end: str = "2026-10-31") -> int:
    response = client.post("/api/habitat/work-windows", json={
        "plot_id": plot_id, "start_on": start, "end_on": end, "crew": "湿地养护一班",
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_plot_window_and_rules_bootstrap(client):
    plot_id = make_plot(client)
    make_window(client, plot_id)
    rules = client.get("/api/habitat/rules").json()
    assert rules["version"] == 1
    assert rules["rules"]["nest_buffer_days"] == 30
    versions = client.get("/api/habitat/rules/versions").json()["items"]
    assert [item["version"] for item in versions] == [1]
    detail = client.get(f"/api/habitat/plots/{plot_id}").json()
    assert detail["code"] == "T-01"
    assert len(detail["work_windows"]) == 1


def test_plan_must_fall_inside_work_window(client):
    plot_id = make_plot(client, code="T-02")
    window_id = make_window(client, plot_id, "2026-09-01", "2026-09-10")
    bad = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-09-20",
    })
    assert bad.status_code == 422


def test_nest_observation_blocks_and_identifies_observation(client):
    plot_id = make_plot(client, code="T-03")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    assert plan["status"] == "scheduled"

    obs = client.post("/api/habitat/observations", json={
        "plot_id": plot_id, "kind": "nest_occupation", "species": "黑翅长脚鹬",
        "observed_on": "2026-09-25", "detail": "巢内有卵", "observer": "巡护员甲",
    }).json()
    # 新迹象自动延后尚未确认的计划。
    updated = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert updated["status"] == "postponed"
    blockers = updated["basis"]["blockers"]
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker["observation_id"] == obs["id"]
    assert blocker["kind"] == "nest_occupation"
    assert blocker["blocked_until"] == "2026-10-25"
    assert "鸟巢占用" in blocker["message"]

    # 涉禽停歇在另一个地块同样阻止割除。
    plot_b = make_plot(client, code="T-04")
    window_b = make_window(client, plot_b)
    client.post("/api/habitat/observations", json={
        "plot_id": plot_b, "kind": "shorebird_rest", "species": "反嘴鹬", "observed_on": "2026-10-10",
    })
    blocked_plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_b, "window_id": window_b, "planned_on": "2026-10-20",
    }).json()
    assert blocked_plan["status"] == "postponed"
    assert blocked_plan["basis"]["blockers"][0]["clause"] == "shorebird_buffer_days"


def test_resolve_observation_reopens_window(client):
    plot_id = make_plot(client, code="T-05")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    obs = client.post("/api/habitat/observations", json={
        "plot_id": plot_id, "kind": "willow_sprout", "species": "旱柳", "observed_on": "2026-10-05",
    }).json()
    assert client.get(f"/api/habitat/plans/{plan['id']}").json()["status"] == "postponed"
    resolved = client.post(f"/api/habitat/observations/{obs['id']}/resolve", json={"actor": "巡护员乙", "note": "萌蘖木质化"})
    assert resolved.status_code == 200
    assert client.get(f"/api/habitat/plans/{plan['id']}").json()["status"] == "scheduled"


def test_rule_adjustment_only_affects_unconfirmed_plans(client):
    plot_id = make_plot(client, code="T-06")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    client.post("/api/habitat/observations", json={
        "plot_id": plot_id, "kind": "nest_occupation", "observed_on": "2026-09-25",
    })
    postponed = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert postponed["status"] == "postponed"
    assert postponed["rule_version"] == 1

    # 管理者放宽鸟巢保护期：新版本只重新判定尚未确认的计划。
    adjust = client.post("/api/habitat/rules/adjust", json={
        "actor": "manager", "reason": "本年度繁殖季提前结束", "nest_buffer_days": 0,
    })
    assert adjust.status_code == 201, adjust.text
    assert adjust.json()["version"] == 2
    assert adjust.json()["affected_plan_ids"] == [plan["id"]]
    reopened_plan = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert reopened_plan["status"] == "scheduled"
    assert reopened_plan["rule_version"] == 2

    # 确认并完成：依据按规则 v2 冻结。
    assert client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "班长"}).status_code == 200
    assert client.post(f"/api/habitat/plans/{plan['id']}/complete", json={"actor": "班长"}).status_code == 200
    records = client.get(f"/api/habitat/maintenance-records?plot_id={plot_id}").json()["items"]
    assert len(records) == 1 and records[0]["rule_version"] == 2
    assert records[0]["basis"]["rules"]["nest_buffer_days"] == 0

    # 再次收紧规则到 v3：已完成维护保留当时依据，不被追溯改写。
    client.post("/api/habitat/rules/adjust", json={
        "actor": "manager", "reason": "次年繁殖季恢复", "nest_buffer_days": 45,
    })
    finished = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert finished["status"] == "completed"
    assert finished["rule_version"] == 2
    records = client.get(f"/api/habitat/maintenance-records?plot_id={plot_id}").json()["items"]
    assert records[0]["rule_version"] == 2
    assert client.get("/api/habitat/rules").json()["version"] == 3


def test_withdraw_unstarted_plan_records_reason(client):
    plot_id = make_plot(client, code="T-07")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    withdrawn = client.post(f"/api/habitat/plans/{plan['id']}/withdraw", json={
        "actor": "调度员", "reason": "班组支援应急清淤",
    })
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["withdraw_reason"] == "班组支援应急清淤"
    # 已撤回安排不能再确认或再次撤回。
    assert client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={}).status_code == 409
    assert client.post(f"/api/habitat/plans/{plan['id']}/withdraw", json={
        "actor": "调度员", "reason": "再次撤回",
    }).status_code == 409

    # 被迹象延后的计划同样可以撤回。
    other = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-21",
    }).json()
    client.post("/api/habitat/observations", json={
        "plot_id": plot_id, "kind": "nest_occupation", "observed_on": "2026-10-01",
    })
    assert client.get(f"/api/habitat/plans/{other['id']}").json()["status"] == "postponed"
    assert client.post(f"/api/habitat/plans/{other['id']}/withdraw", json={
        "actor": "调度员", "reason": "本季不再安排",
    }).status_code == 200


def test_confirmed_plan_cannot_withdraw_and_deviation_reopens(client):
    plot_id = make_plot(client, code="T-08")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "班长"})
    # 已经开始（确认）的安排不能撤回。
    refused = client.post(f"/api/habitat/plans/{plan['id']}/withdraw", json={
        "actor": "调度员", "reason": "尝试撤回",
    })
    assert refused.status_code == 409

    # 现场只完成一半并遇天气中断：登记偏差，未完成事项重新打开。
    deviation = client.post(f"/api/habitat/plans/{plan['id']}/deviation", json={
        "actor": "班长", "deviation_type": "天气中断", "description": "午后涨水，机具撤离",
        "completion_ratio": 0.5, "reopen": True,
    })
    assert deviation.status_code == 200, deviation.text
    body = deviation.json()
    actions = [event["action"] for event in body["events"]]
    assert "confirm" in actions and "deviation" in actions and "reopen" in actions
    assert body["deviations"][0]["completion_ratio"] == 0.5
    # 重新打开后按当前规则恢复判定：无阻断迹象时回到可执行。
    assert body["status"] == "scheduled"

    # 重新安排、再次开工并完成。
    assert client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "班长"}).status_code == 200
    completed = client.post(f"/api/habitat/plans/{plan['id']}/complete", json={"actor": "班长"})
    assert completed.status_code == 200
    records = client.get(f"/api/habitat/maintenance-records").json()["items"]
    assert len(records) == 1 and records[0]["plan_id"] == plan["id"]


def test_reopened_plan_is_postponed_when_new_sign_appears(client):
    plot_id = make_plot(client, code="T-09")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "班长"})
    client.post("/api/habitat/observations", json={
        "plot_id": plot_id, "kind": "shorebird_rest", "observed_on": "2026-10-15",
    })
    deviation = client.post(f"/api/habitat/plans/{plan['id']}/deviation", json={
        "actor": "班长", "deviation_type": "物种新迹象", "description": "作业带发现涉禽停歇",
        "completion_ratio": 0.2,
    })
    assert deviation.status_code == 200
    assert deviation.json()["status"] == "postponed"
    assert deviation.json()["basis"]["blockers"][0]["kind"] == "shorebird_rest"


def test_deviation_with_full_completion_archives_maintenance(client):
    plot_id = make_plot(client, code="T-10")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "班长"})
    response = client.post(f"/api/habitat/plans/{plan['id']}/deviation", json={
        "actor": "班长", "deviation_type": "范围偏差", "description": "实际作业边界内移两米，仍全部完成",
        "completion_ratio": 1.0,
    })
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    records = client.get(f"/api/habitat/maintenance-records?plot_id={plot_id}").json()["items"]
    assert len(records) == 1 and records[0]["deviation_id"] is not None


def test_plot_history_timeline_and_status_filters(client):
    plot_id = make_plot(client, code="T-11")
    window_id = make_window(client, plot_id)
    plan = client.post("/api/habitat/plans", json={
        "plot_id": plot_id, "window_id": window_id, "planned_on": "2026-10-20",
    }).json()
    client.post(f"/api/habitat/plans/{plan['id']}/withdraw", json={"actor": "调度员", "reason": "演练占用"})

    history = client.get(f"/api/habitat/plots/{plot_id}/history")
    assert history.status_code == 200
    body = history.json()
    assert body["plot"]["code"] == "T-11"
    assert len(body["work_windows"]) == 1
    assert body["plans"][0]["withdraw_reason"] == "演练占用"
    timeline_types = {item["type"] for item in body["timeline"]}
    assert "plan" in timeline_types
    assert "plan.withdraw" in timeline_types

    scheduled = client.get("/api/habitat/plans", params={"plot_id": plot_id, "status": "scheduled"}).json()["items"]
    assert scheduled == []
    withdrawn = client.get("/api/habitat/plans", params={"plot_id": plot_id, "status": "withdrawn"}).json()["items"]
    assert [item["id"] for item in withdrawn] == [plan["id"]]
