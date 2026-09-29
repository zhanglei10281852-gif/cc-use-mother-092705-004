from __future__ import annotations


def _make_plot(client, code="T-01"):
    response = client.post("/api/habitat/plots", json={"code": code, "name": "芦苇滩涂", "location": "东岸"})
    assert response.status_code == 201, response.text
    return response.json()


def test_rules_have_initial_version_and_can_be_listed(client):
    rules = client.get("/api/habitat/rules")
    assert rules.status_code == 200
    body = rules.json()
    assert body["current"] == 1
    assert body["items"][0]["nest_buffer_days"] == 7


def test_plan_is_deferred_with_named_blocking_observation(client):
    plot = _make_plot(client)
    # 有可用工期，但繁殖观察落在计划日
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-05-01", "ends_on": "2026-08-31", "capacity_hours": 40},
    )
    breeding = client.post(
        f"/api/habitat/plots/{plot['id']}/breeding",
        json={"species": "震旦鸦雀", "stage": "incubating", "observed_on": "2026-05-10",
              "expected_fledge_on": "2026-06-10"},
    )
    assert breeding.status_code == 201
    plan = client.post(
        "/api/habitat/plans",
        json={"plot_id": plot["id"], "scheduled_on": "2026-05-20", "requested_by": "manager"},
    )
    assert plan.status_code == 201
    body = plan.json()
    assert body["status"] == "deferred"
    assert body["is_executable"] == 0
    assert len(body["blocks"]) == 1
    block = body["blocks"][0]
    # 必须明确是哪条观察阻止了操作
    assert block["source_type"] == "breeding"
    assert block["source_id"] == breeding.json()["id"]
    assert "震旦鸦雀" in block["reason"]

    # 保护窗口之外的计划可执行
    clear = client.post(
        "/api/habitat/plans",
        json={"plot_id": plot["id"], "scheduled_on": "2026-08-01", "requested_by": "manager"},
    )
    assert clear.json()["status"] == "proposed"
    assert clear.json()["is_executable"] == 1


def test_missing_work_window_blocks_and_is_relieved_when_window_added(client):
    plot = _make_plot(client, code="T-02")
    plan = client.post(
        "/api/habitat/plans",
        json={"plot_id": plot["id"], "scheduled_on": "2026-11-10"},
    ).json()
    assert plan["status"] == "deferred"
    assert plan["blocks"][0]["block_kind"] == "work_window"

    created = client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-11-01", "ends_on": "2026-11-30", "capacity_hours": 20},
    )
    assert created.status_code == 201
    refreshed = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert refreshed["status"] == "proposed"
    assert refreshed["blocks"] == []


def test_new_sign_defers_pending_plan(client):
    plot = _make_plot(client, code="T-03")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-10-01", "ends_on": "2026-10-31", "capacity_hours": 20},
    )
    plan = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-10-10"}
    ).json()
    assert plan["status"] == "proposed"

    sign = client.post(
        f"/api/habitat/plots/{plot['id']}/signs",
        json={"kind": "willow_sprout", "species": "旱柳", "observed_on": "2026-09-25"},
    )
    assert sign.status_code == 201
    refreshed = client.get(f"/api/habitat/plans/{plan['id']}").json()
    assert refreshed["status"] == "deferred"
    assert refreshed["blocks"][0]["source_type"] == "sign"
    assert refreshed["blocks"][0]["source_id"] == sign.json()["id"]


def test_cannot_confirm_blocked_plan_and_block_is_explained(client):
    plot = _make_plot(client, code="T-04")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-09-01", "ends_on": "2026-09-30", "capacity_hours": 20},
    )
    sign = client.post(
        f"/api/habitat/plots/{plot['id']}/signs",
        json={"kind": "shorebird_roost", "species": "黑腹滨鹬", "observed_on": "2026-09-15",
              "active_window": "09-10/09-25"},
    ).json()
    plan = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-09-16"}
    ).json()
    denied = client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "manager"})
    assert denied.status_code == 409
    blocks = denied.json()["error"]["context"]["blocks"]
    assert any(block["source_id"] == sign["id"] for block in blocks)


def test_rule_change_only_affects_unconfirmed_plans(client):
    plot = _make_plot(client, code="T-05")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-01-01", "ends_on": "2026-12-31", "capacity_hours": 200},
    )
    client.post(
        f"/api/habitat/plots/{plot['id']}/signs",
        json={"kind": "shorebird_roost", "species": "黑腹滨鹬", "observed_on": "2026-09-15"},
    )
    # 计划 A：确认并完工（在 v1 规则下）
    plan_a = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-10-10"}
    ).json()
    # 默认涉禽季节窗口 09-01/04-30 覆盖 10-10，先放宽规则以便确认完工
    relaxed = client.post(
        "/api/habitat/rules",
        json={"note": "缩小涉禽季节窗口", "shorebird_window": "09-01/09-20", "actor": "manager"},
    )
    assert relaxed.status_code == 201
    assert relaxed.json()["version"] == 2
    plan_a = client.get(f"/api/habitat/plans/{plan_a['id']}").json()
    assert plan_a["status"] == "proposed"
    assert client.post(f"/api/habitat/plans/{plan_a['id']}/confirm", json={"actor": "manager"}).status_code == 200
    assert client.post(f"/api/habitat/plans/{plan_a['id']}/start", json={"actor": "ranger"}).status_code == 200
    completed = client.post(
        f"/api/habitat/plans/{plan_a['id']}/complete", json={"actor": "ranger", "note": "按v2完成"}
    )
    assert completed.status_code == 200
    assert completed.json()["decision_rule_version"] == 2

    # 未确认计划 B：在 v2 下可执行（10-10 已超出 09-20，且超出观察缓冲）
    plan_b = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-10-10"}
    ).json()
    assert plan_b["status"] == "proposed"

    # 管理者再次收紧规则到 v3：B 回到延后，A 已完工保持 v2 依据
    tightened = client.post(
        "/api/habitat/rules",
        json={"note": "恢复长保护季", "shorebird_window": "09-01/04-30", "actor": "manager"},
    )
    assert tightened.json()["version"] == 3
    assert tightened.json()["affected_plan_ids"] == [plan_b["id"]]

    plan_b_after = client.get(f"/api/habitat/plans/{plan_b['id']}").json()
    assert plan_b_after["status"] == "deferred"
    assert plan_b_after["blocks"][0]["block_kind"] == "shorebird_roost"

    plan_a_after = client.get(f"/api/habitat/plans/{plan_a['id']}").json()
    assert plan_a_after["status"] == "completed"
    assert plan_a_after["decision_rule_version"] == 2
    assert plan_a_after["completed_at"]


def test_withdraw_unstarted_plan_keeps_reason(client):
    plot = _make_plot(client, code="T-06")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-07-01", "ends_on": "2026-07-31", "capacity_hours": 20},
    )
    plan = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-07-10"}
    ).json()
    withdrawn = client.post(
        f"/api/habitat/plans/{plan['id']}/withdraw",
        json={"reason": "工期让位于水文监测", "actor": "manager"},
    )
    assert withdrawn.status_code == 200
    body = withdrawn.json()
    assert body["status"] == "withdrawn"
    assert body["withdraw_reason"] == "工期让位于水文监测"
    assert body["withdrawn_by"] == "manager"
    assert body["blocks"] == []

    # 已开工的不能撤回
    other = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-07-15"}
    ).json()
    client.post(f"/api/habitat/plans/{other['id']}/confirm", json={"actor": "manager"})
    client.post(f"/api/habitat/plans/{other['id']}/start", json={"actor": "ranger"})
    denied = client.post(
        f"/api/habitat/plans/{other['id']}/withdraw", json={"reason": "x", "actor": "manager"}
    )
    assert denied.status_code == 409


def test_deviation_suspends_and_reopen_restores_unfinished(client):
    plot = _make_plot(client, code="T-07")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-08-01", "ends_on": "2026-08-31", "capacity_hours": 30},
    )
    plan = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-08-10"}
    ).json()
    client.post(f"/api/habitat/plans/{plan['id']}/confirm", json={"actor": "manager"})
    client.post(f"/api/habitat/plans/{plan['id']}/start", json={"actor": "ranger"})

    deviation = client.post(
        f"/api/habitat/plans/{plan['id']}/deviations",
        json={"kind": "incomplete", "description": "西北角芦苇未割，机具故障", "unfinished": True},
    )
    assert deviation.status_code == 201
    body = deviation.json()
    assert body["status"] == "suspended"
    assert body["deviations"][0]["unfinished"] == 1

    # 未完成事项未恢复前不能完工
    blocked_complete = client.post(
        f"/api/habitat/plans/{plan['id']}/complete", json={"actor": "ranger"}
    )
    assert blocked_complete.status_code == 409

    reopened = client.post(f"/api/habitat/plans/{plan['id']}/reopen", json={"actor": "ranger"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "in_progress"
    assert reopened.json()["deviations"][0]["resolved_at"]

    done = client.post(f"/api/habitat/plans/{plan['id']}/complete", json={"actor": "ranger"})
    assert done.status_code == 200
    assert done.json()["status"] == "completed"


def test_plot_history_records_full_timeline(client):
    plot = _make_plot(client, code="T-08")
    client.post(
        f"/api/habitat/plots/{plot['id']}/work-windows",
        json={"starts_on": "2026-07-01", "ends_on": "2026-07-31", "capacity_hours": 20},
    )
    plan = client.post(
        "/api/habitat/plans", json={"plot_id": plot["id"], "scheduled_on": "2026-07-20"}
    ).json()
    client.post(
        f"/api/habitat/plans/{plan['id']}/withdraw",
        json={"reason": "连续降雨", "actor": "manager"},
    )
    history = client.get(f"/api/habitat/plots/{plot['id']}/history")
    assert history.status_code == 200
    body = history.json()
    actions = [event["action"] for event in body["events"]]
    assert "plot.create" in actions
    assert "work_window.create" in actions
    assert "plan.create" in actions
    assert "plan.withdraw" in actions
    withdraw_event = next(event for event in body["events"] if event["action"] == "plan.withdraw")
    assert withdraw_event["detail"]["reason"] == "连续降雨"
    assert body["plans"][0]["status"] == "withdrawn"
    assert body["plans"][0]["withdraw_reason"] == "连续降雨"


def test_breeding_validation_rejects_reversed_dates(client):
    plot = _make_plot(client, code="T-09")
    response = client.post(
        f"/api/habitat/plots/{plot['id']}/breeding",
        json={"species": "白鹭", "stage": "egg", "observed_on": "2026-05-10",
              "expected_fledge_on": "2026-05-01"},
    )
    assert response.status_code == 422
