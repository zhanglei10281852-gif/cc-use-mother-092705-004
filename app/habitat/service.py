from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# 尚未确认、仍受规则变化与新观察影响的计划状态。
OPEN_STATUSES = ("scheduled", "postponed", "reopened")

SCHEMA = """
CREATE TABLE IF NOT EXISTS habitat_plots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    wetland_type TEXT NOT NULL,
    area_mu REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habitat_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('shorebird_rest','nest_occupation','willow_sprout','other')),
    species TEXT NOT NULL DEFAULT '',
    observed_on TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    detail TEXT NOT NULL DEFAULT '',
    observer TEXT NOT NULL,
    resolved_at TEXT,
    resolved_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_obs_plot ON habitat_observations(plot_id, observed_on);
CREATE TABLE IF NOT EXISTS habitat_work_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    start_on TEXT NOT NULL,
    end_on TEXT NOT NULL,
    crew TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK(end_on >= start_on)
);
CREATE TABLE IF NOT EXISTS habitat_rule_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    rules_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habitat_mowing_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    window_id INTEGER NOT NULL REFERENCES habitat_work_windows(id) ON DELETE RESTRICT,
    planned_on TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '割除',
    status TEXT NOT NULL CHECK(status IN ('scheduled','postponed','confirmed','completed','withdrawn','reopened')),
    created_by TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    basis_json TEXT NOT NULL DEFAULT '{}',
    withdraw_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_plans_plot ON habitat_mowing_plans(plot_id, id);
CREATE INDEX IF NOT EXISTS idx_habitat_plans_status ON habitat_mowing_plans(status);
CREATE TABLE IF NOT EXISTS habitat_plan_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES habitat_mowing_plans(id) ON DELETE CASCADE,
    rule_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('executable','postponed')),
    blockers_json TEXT NOT NULL DEFAULT '[]',
    trigger TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_eval_plan ON habitat_plan_evaluations(plan_id, id);
CREATE TABLE IF NOT EXISTS habitat_plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES habitat_mowing_plans(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_events_plot ON habitat_plan_events(plan_id, id);
CREATE TABLE IF NOT EXISTS habitat_deviations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES habitat_mowing_plans(id) ON DELETE RESTRICT,
    deviation_type TEXT NOT NULL,
    description TEXT NOT NULL,
    completion_ratio REAL NOT NULL DEFAULT 0,
    reopened INTEGER NOT NULL DEFAULT 1 CHECK(reopened IN (0,1)),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habitat_maintenance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL UNIQUE REFERENCES habitat_mowing_plans(id) ON DELETE RESTRICT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    method TEXT NOT NULL,
    planned_on TEXT NOT NULL,
    completed_on TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    basis_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    deviation_id INTEGER REFERENCES habitat_deviations(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_records_plot ON habitat_maintenance_records(plot_id, completed_on);
"""

# 旱柳萌蘖保护期不开放单独调整，固定跟随 sprout_block 开关，写入规则快照保证依据可复核。
SPROUT_BUFFER_DAYS = 21

DEFAULT_RULES: dict[str, Any] = {
    "shorebird_buffer_days": 14,
    "nest_buffer_days": 30,
    "sprout_buffer_days": SPROUT_BUFFER_DAYS,
    "sprout_block": True,
    "blocked_kinds": ["shorebird_rest", "nest_occupation", "willow_sprout"],
}

KIND_LABELS = {
    "shorebird_rest": "涉禽停歇",
    "nest_occupation": "鸟巢占用",
    "willow_sprout": "旱柳萌蘖",
    "other": "其它迹象",
}


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    if connection.execute("SELECT COUNT(*) FROM habitat_rule_versions").fetchone()[0] == 0:
        now = to_storage(SystemClock().now())
        connection.execute(
            "INSERT INTO habitat_rule_versions(version,rules_json,created_by,reason,created_at) VALUES(1,?,?,?,?)",
            (json.dumps(DEFAULT_RULES, ensure_ascii=False, sort_keys=True), "system", "初始保护规则：涉禽停歇、鸟巢占用与旱柳萌蘖保护期", now),
        )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"日期格式不正确：{value}") from exc


def evaluate_window(rules: dict[str, Any], planned_on: date, observations: list[sqlite3.Row]) -> dict[str, Any]:
    """依据给定规则版本与当前有效迹象判定割除日是否可执行。

    返回 {"decision": ..., "blockers": [...]}；blockers 明确列出阻止操作的观察。
    """
    blockers: list[dict[str, Any]] = []
    blocked_kinds = set(rules.get("blocked_kinds", []))
    for observation in observations:
        kind = observation["kind"]
        if not observation["active"] or observation["resolved_at"]:
            continue
        observed_on = _parse_date(observation["observed_on"])
        if observed_on > planned_on or kind not in blocked_kinds:
            continue
        if kind == "shorebird_rest":
            buffer_days = int(rules["shorebird_buffer_days"])
            buffer_end = observed_on + timedelta(days=buffer_days)
            if planned_on <= buffer_end:
                blockers.append(_blocker(observation, "shorebird_buffer_days", buffer_days, buffer_end,
                                        f"涉禽停歇保护期：观察日后 {buffer_days} 天内（至 {buffer_end.isoformat()}）不得割除"))
        elif kind == "nest_occupation":
            buffer_days = int(rules["nest_buffer_days"])
            buffer_end = observed_on + timedelta(days=buffer_days)
            if planned_on <= buffer_end:
                blockers.append(_blocker(observation, "nest_buffer_days", buffer_days, buffer_end,
                                        f"鸟巢占用保护期：观察日后 {buffer_days} 天内（至 {buffer_end.isoformat()}）不得割除"))
        elif kind == "willow_sprout" and rules.get("sprout_block", True):
            buffer_days = int(rules.get("sprout_buffer_days", SPROUT_BUFFER_DAYS))
            buffer_end = observed_on + timedelta(days=buffer_days)
            if planned_on <= buffer_end:
                blockers.append(_blocker(observation, "sprout_block", buffer_days, buffer_end,
                                        f"旱柳萌蘖保护期：观察日后 {buffer_days} 天内（至 {buffer_end.isoformat()}）一律延后"))
    blockers.sort(key=lambda item: (item["observed_on"], item["observation_id"]))
    return {"decision": "executable" if not blockers else "postponed", "blockers": blockers}


def _blocker(observation: sqlite3.Row, clause: str, buffer_days: int, buffer_end: date, message: str) -> dict[str, Any]:
    return {
        "observation_id": observation["id"],
        "kind": observation["kind"],
        "kind_label": KIND_LABELS.get(observation["kind"], observation["kind"]),
        "species": observation["species"],
        "observed_on": observation["observed_on"],
        "clause": clause,
        "buffer_days": buffer_days,
        "blocked_until": buffer_end.isoformat(),
        "message": message,
    }


class HabitatService:
    """地块、物种迹象、可用工期与割除计划窗口的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ----- 规则版本 -----

    def current_rules(self, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM habitat_rule_versions ORDER BY version DESC LIMIT 1").fetchone()
        return {"version": row["version"], "rules": json.loads(row["rules_json"]), "created_by": row["created_by"], "reason": row["reason"], "created_at": row["created_at"]}

    def list_rule_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM habitat_rule_versions ORDER BY version DESC").fetchall()
        return [{"version": row["version"], "rules": json.loads(row["rules_json"]), "created_by": row["created_by"], "reason": row["reason"], "created_at": row["created_at"]} for row in rows]

    def adjust_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发布新规则版本，并只对尚未确认的计划重新判定。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            current = self.current_rules(connection)
            rules = dict(current["rules"])
            for field in ("shorebird_buffer_days", "nest_buffer_days", "sprout_block"):
                if payload.get(field) is not None:
                    rules[field] = payload[field]
            if payload.get("blocked_kinds") is not None:
                rules["blocked_kinds"] = payload["blocked_kinds"]
            if rules == current["rules"]:
                raise ValidationError("规则内容没有变化，无需发布新版本")
            cursor = connection.execute(
                "INSERT INTO habitat_rule_versions(rules_json,created_by,reason,created_at) VALUES(?,?,?,?)",
                (json.dumps(rules, ensure_ascii=False, sort_keys=True), payload["actor"], payload["reason"], now),
            )
            new_version = int(cursor.lastrowid)
            affected = self._reevaluate_open(connection, rule_version=new_version, rules=rules, trigger="rule_adjustment", actor=payload["actor"], now=now)
            return {"version": new_version, "rules": rules, "reason": payload["reason"], "created_by": payload["actor"], "created_at": now, "affected_plan_ids": affected}

    # ----- 地块 -----

    def create_plot(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO habitat_plots(code,name,wetland_type,area_mu,note,created_at) VALUES(?,?,?,?,?,?)",
                    (payload["code"], payload["name"], payload["wetland_type"], payload["area_mu"], payload.get("note", ""), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("地块编码已存在") from exc
            return self._plot(connection, int(cursor.lastrowid))

    def list_plots(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM habitat_plots ORDER BY code").fetchall()]

    def get_plot(self, plot_id: int) -> dict[str, Any]:
        plot = self._plot(self.connection, plot_id)
        plot["observations"] = self.list_observations(plot_id)
        plot["work_windows"] = self.list_windows(plot_id)
        return plot

    @staticmethod
    def _plot(connection: sqlite3.Connection, plot_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM habitat_plots WHERE id=?", (plot_id,)).fetchone()
        if row is None:
            raise NotFoundError("地块不存在")
        return dict(row)

    # ----- 物种迹象 -----

    def add_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._plot(connection, payload["plot_id"])
            cursor = connection.execute(
                "INSERT INTO habitat_observations(plot_id,kind,species,observed_on,active,detail,observer,created_at) VALUES(?,?,?,?,1,?,?,?)",
                (payload["plot_id"], payload["kind"], payload.get("species", ""), payload["observed_on"], payload.get("detail", ""), payload["observer"], now),
            )
            observation_id = int(cursor.lastrowid)
            # 新迹象可能立即关闭尚未确认计划的作业窗口。
            current = self.current_rules(connection)
            self._reevaluate_open(connection, rule_version=current["version"], rules=current["rules"], trigger="observation", actor=payload["observer"], now=now, plot_id=payload["plot_id"])
            return dict(connection.execute("SELECT * FROM habitat_observations WHERE id=?", (observation_id,)).fetchone())

    def resolve_observation(self, observation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM habitat_observations WHERE id=?", (observation_id,)).fetchone()
            if row is None:
                raise NotFoundError("观察记录不存在")
            if row["resolved_at"]:
                raise ConflictError("该观察已经解除")
            connection.execute("UPDATE habitat_observations SET active=0,resolved_at=?,resolved_note=? WHERE id=?", (now, payload.get("note", ""), observation_id))
            current = self.current_rules(connection)
            self._reevaluate_open(connection, rule_version=current["version"], rules=current["rules"], trigger="observation_resolved", actor=payload["actor"], now=now, plot_id=row["plot_id"])
            return dict(connection.execute("SELECT * FROM habitat_observations WHERE id=?", (observation_id,)).fetchone())

    def list_observations(self, plot_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM habitat_observations WHERE plot_id=? ORDER BY observed_on DESC,id DESC", (plot_id,)).fetchall()]

    # ----- 可用工期 -----

    def add_work_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        start, end = _parse_date(payload["start_on"]), _parse_date(payload["end_on"])
        if end < start:
            raise ValidationError("可用工期结束日期不能早于开始日期")
        with transaction(immediate=True) as connection:
            self._plot(connection, payload["plot_id"])
            cursor = connection.execute(
                "INSERT INTO habitat_work_windows(plot_id,start_on,end_on,crew,note,created_at) VALUES(?,?,?,?,?,?)",
                (payload["plot_id"], payload["start_on"], payload["end_on"], payload.get("crew", ""), payload.get("note", ""), now),
            )
            return dict(connection.execute("SELECT * FROM habitat_work_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_windows(self, plot_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM habitat_work_windows WHERE plot_id=? ORDER BY start_on,id", (plot_id,)).fetchall()]

    # ----- 割除计划 -----

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        planned_on = _parse_date(payload["planned_on"])
        with transaction(immediate=True) as connection:
            self._plot(connection, payload["plot_id"])
            window = connection.execute("SELECT * FROM habitat_work_windows WHERE id=?", (payload["window_id"],)).fetchone()
            if window is None:
                raise NotFoundError("可用工期不存在")
            if window["plot_id"] != payload["plot_id"]:
                raise ValidationError("工期与地块不匹配", context={"window_id": window["id"], "plot_id": payload["plot_id"]})
            if not (_parse_date(window["start_on"]) <= planned_on <= _parse_date(window["end_on"])):
                raise ValidationError("计划割除日期不在可用工期内", context={"window": {"start_on": window["start_on"], "end_on": window["end_on"]}, "planned_on": payload["planned_on"]})
            current = self.current_rules(connection)
            observations = connection.execute("SELECT * FROM habitat_observations WHERE plot_id=?", (payload["plot_id"],)).fetchall()
            judgment = evaluate_window(current["rules"], planned_on, observations)
            status = "scheduled" if judgment["decision"] == "executable" else "postponed"
            basis = {"rule_version": current["version"], "rules": current["rules"], "blockers": judgment["blockers"], "decided_at": now, "decided_by": payload["created_by"]}
            cursor = connection.execute(
                "INSERT INTO habitat_mowing_plans(plot_id,window_id,planned_on,method,status,created_by,rule_version,basis_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["plot_id"], payload["window_id"], payload["planned_on"], payload.get("method", "割除"), status, payload["created_by"], current["version"], json.dumps(basis, ensure_ascii=False), now, now),
            )
            plan_id = int(cursor.lastrowid)
            self._add_evaluation(connection, plan_id, current["version"], judgment, "create", payload["created_by"], now)
            self._add_event(connection, plan_id, "create", payload["created_by"], {"status": status, "blockers": judgment["blockers"]}, now)
            return self.get_plan(plan_id)

    def list_plans(self, plot_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        if plot_id is not None:
            clauses.append("plot_id=?")
            values.append(plot_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(500)
        rows = self.connection.execute(f"SELECT * FROM habitat_mowing_plans{where} ORDER BY planned_on,id LIMIT ?", values).fetchall()
        return [self._plan_view(row) for row in rows]

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM habitat_mowing_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("割除计划不存在")
        return self._plan_view(row)

    def _plan_view(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["basis"] = json.loads(row["basis_json"] or "{}")
        del result["basis_json"]
        result["deviations"] = [dict(item) for item in self.connection.execute("SELECT * FROM habitat_deviations WHERE plan_id=? ORDER BY id", (row["id"],)).fetchall()]
        result["events"] = [dict(item) for item in self.connection.execute("SELECT id,action,actor,payload_json,created_at FROM habitat_plan_events WHERE plan_id=? ORDER BY id", (row["id"],)).fetchall()]
        for event in result["events"]:
            event["payload"] = json.loads(event.pop("payload_json") or "{}")
        result["evaluations"] = [dict(item) for item in self.connection.execute("SELECT id,rule_version,decision,blockers_json,trigger,actor,created_at FROM habitat_plan_evaluations WHERE plan_id=? ORDER BY id", (row["id"],)).fetchall()]
        for evaluation in result["evaluations"]:
            evaluation["blockers"] = json.loads(evaluation.pop("blockers_json") or "[]")
        return result

    def reevaluate_plan(self, plan_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            current = self.current_rules(connection)
            self._reevaluate_one(connection, plan, current["version"], current["rules"], "manual", actor, now)
            return self.get_plan(plan_id)

    def withdraw_plan(self, plan_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in OPEN_STATUSES:
                raise ConflictError("只有尚未开始（含重新打开）的安排可以撤回", context={"status": plan["status"]})
            connection.execute("UPDATE habitat_mowing_plans SET status='withdrawn',withdraw_reason=?,updated_at=? WHERE id=?", (reason, now, plan_id))
            self._add_event(connection, plan_id, "withdraw", actor, {"reason": reason, "previous_status": plan["status"]}, now)
            return self.get_plan(plan_id)

    def confirm_plan(self, plan_id: int, actor: str) -> dict[str, Any]:
        """现场确认开工；确认后的计划冻结依据，不再受规则调整影响。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in {"scheduled", "reopened"}:
                blockers = json.loads(plan["basis_json"] or "{}").get("blockers", [])
                raise ConflictError("当前计划不能确认开工", context={"status": plan["status"], "blockers": blockers})
            connection.execute("UPDATE habitat_mowing_plans SET status='confirmed',updated_at=? WHERE id=?", (now, plan_id))
            self._add_event(connection, plan_id, "confirm", actor, {"rule_version": plan["rule_version"]}, now)
            return self.get_plan(plan_id)

    def complete_plan(self, plan_id: int, actor: str, deviation_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] != "confirmed":
                raise ConflictError("只有已确认开工的计划可以登记完成", context={"status": plan["status"]})
            completed_on = self.clock.now().date().isoformat()
            connection.execute("UPDATE habitat_mowing_plans SET status='completed',updated_at=? WHERE id=?", (now, plan_id))
            # 完成的维护永久保留当时的规则版本与判定依据。
            connection.execute(
                "INSERT INTO habitat_maintenance_records(plan_id,plot_id,method,planned_on,completed_on,rule_version,basis_json,actor,deviation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, plan["plot_id"], plan["method"], plan["planned_on"], completed_on, plan["rule_version"], plan["basis_json"], actor, deviation_id, now),
            )
            self._add_event(connection, plan_id, "complete", actor, {"rule_version": plan["rule_version"], "completed_on": completed_on}, now)
            return self.get_plan(plan_id)

    def register_deviation(self, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """登记现场偏差；未全部完成时计划重新打开，未完成事项回到待安排队列。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] != "confirmed":
                raise ConflictError("只有开工中的计划可以登记现场偏差", context={"status": plan["status"]})
            ratio = payload["completion_ratio"]
            reopened = payload["reopen"] and ratio < 1.0
            cursor = connection.execute(
                "INSERT INTO habitat_deviations(plan_id,deviation_type,description,completion_ratio,reopened,actor,created_at) VALUES(?,?,?,?,?,?,?)",
                (plan_id, payload["deviation_type"], payload["description"], ratio, 1 if reopened else 0, payload["actor"], now),
            )
            deviation_id = int(cursor.lastrowid)
            if ratio >= 1.0 or not payload["reopen"]:
                # 偏差登记但作业实际完成：按完成归档并保留当时依据。
                connection.execute("UPDATE habitat_mowing_plans SET status='completed',updated_at=? WHERE id=?", (now, plan_id))
                completed_on = self.clock.now().date().isoformat()
                connection.execute(
                    "INSERT INTO habitat_maintenance_records(plan_id,plot_id,method,planned_on,completed_on,rule_version,basis_json,actor,deviation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, plan["plot_id"], plan["method"], plan["planned_on"], completed_on, plan["rule_version"], plan["basis_json"], payload["actor"], deviation_id, now),
                )
                self._add_event(connection, plan_id, "complete", payload["actor"], {"via_deviation": deviation_id, "rule_version": plan["rule_version"]}, now)
            else:
                connection.execute("UPDATE habitat_mowing_plans SET status='reopened',updated_at=? WHERE id=?", (now, plan_id))
                self._add_event(connection, plan_id, "reopen", payload["actor"], {"deviation_id": deviation_id, "completion_ratio": ratio}, now)
                # 重新打开即按最新规则恢复未完成事项的窗口判定。
                current = self.current_rules(connection)
                self._reevaluate_one(connection, connection.execute("SELECT * FROM habitat_mowing_plans WHERE id=?", (plan_id,)).fetchone(), current["version"], current["rules"], "reopen", payload["actor"], now)
            self._add_event(connection, plan_id, "deviation", payload["actor"], {"deviation_id": deviation_id, "deviation_type": payload["deviation_type"], "completion_ratio": ratio, "reopened": reopened}, now)
            return self.get_plan(plan_id)

    # ----- 已完成维护依据 -----

    def list_maintenance_records(self, plot_id: int | None = None) -> list[dict[str, Any]]:
        if plot_id is not None:
            rows = self.connection.execute("SELECT * FROM habitat_maintenance_records WHERE plot_id=? ORDER BY completed_on,id", (plot_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM habitat_maintenance_records ORDER BY completed_on,id").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["basis"] = json.loads(item.pop("basis_json") or "{}")
            items.append(item)
        return items

    # ----- 地块历史 -----

    def plot_history(self, plot_id: int) -> dict[str, Any]:
        plot = self.get_plot(plot_id)
        plans = self.list_plans(plot_id=plot_id)
        records = [dict(row) for row in self.connection.execute("SELECT * FROM habitat_maintenance_records WHERE plot_id=? ORDER BY completed_on,id", (plot_id,)).fetchall()]
        for record in records:
            record["basis"] = json.loads(record.pop("basis_json") or "{}")
        timeline: list[dict[str, Any]] = []
        for plan in plans:
            timeline.append({"type": "plan", "at": plan["updated_at"], "plan_id": plan["id"], "status": plan["status"], "planned_on": plan["planned_on"], "method": plan["method"], "rule_version": plan["rule_version"]})
            for event in plan["events"]:
                timeline.append({"type": f"plan.{event['action']}", "at": event["created_at"], "plan_id": plan["id"], "actor": event["actor"], "payload": event["payload"]})
        for observation in plot["observations"]:
            timeline.append({"type": "observation", "at": observation["created_at"], "observation_id": observation["id"], "kind": observation["kind"], "kind_label": KIND_LABELS.get(observation["kind"], observation["kind"]), "observed_on": observation["observed_on"], "resolved": bool(observation["resolved_at"])})
        timeline.sort(key=lambda item: (item["at"], item.get("plan_id", 0)))
        return {"plot": {key: plot[key] for key in ("id", "code", "name", "wetland_type", "area_mu")}, "observations": plot["observations"], "work_windows": plot["work_windows"], "plans": plans, "maintenance_records": records, "timeline": timeline}

    # ----- 内部辅助 -----

    @staticmethod
    def _require_plan(connection: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        plan = connection.execute("SELECT * FROM habitat_mowing_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("割除计划不存在")
        return plan

    def _reevaluate_open(self, connection: sqlite3.Connection, *, rule_version: int, rules: dict[str, Any], trigger: str, actor: str, now: str, plot_id: int | None = None) -> list[int]:
        affected: list[int] = []
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        sql = f"SELECT * FROM habitat_mowing_plans WHERE status IN ({placeholders})"
        params: list[Any] = list(OPEN_STATUSES)
        if plot_id is not None:
            sql += " AND plot_id=?"
            params.append(plot_id)
        sql += " ORDER BY id"
        for plan in connection.execute(sql, params).fetchall():
            if self._reevaluate_one(connection, plan, rule_version, rules, trigger, actor, now):
                affected.append(int(plan["id"]))
        return affected

    def _reevaluate_one(self, connection: sqlite3.Connection, plan: sqlite3.Row, rule_version: int, rules: dict[str, Any], trigger: str, actor: str, now: str) -> bool:
        """对单个未确认计划按指定规则版本重新判定；返回状态是否发生变化。"""
        if plan["status"] not in OPEN_STATUSES:
            return False
        observations = connection.execute("SELECT * FROM habitat_observations WHERE plot_id=?", (plan["plot_id"],)).fetchall()
        judgment = evaluate_window(rules, _parse_date(plan["planned_on"]), observations)
        new_status = "scheduled" if judgment["decision"] == "executable" else "postponed"
        basis = {"rule_version": rule_version, "rules": rules, "blockers": judgment["blockers"], "decided_at": now, "decided_by": actor, "previous_rule_version": plan["rule_version"]}
        previous_status = plan["status"]
        connection.execute(
            "UPDATE habitat_mowing_plans SET status=?,rule_version=?,basis_json=?,updated_at=? WHERE id=?",
            (new_status, rule_version, json.dumps(basis, ensure_ascii=False), now, plan["id"]),
        )
        self._add_evaluation(connection, plan["id"], rule_version, judgment, trigger, actor, now)
        if new_status != previous_status or rule_version != plan["rule_version"]:
            self._add_event(connection, plan["id"], "reevaluate", actor, {"trigger": trigger, "previous_status": previous_status, "status": new_status, "rule_version": rule_version, "blockers": judgment["blockers"]}, now)
            return True
        return False

    @staticmethod
    def _add_evaluation(connection: sqlite3.Connection, plan_id: int, rule_version: int, judgment: dict[str, Any], trigger: str, actor: str, now: str) -> None:
        connection.execute(
            "INSERT INTO habitat_plan_evaluations(plan_id,rule_version,decision,blockers_json,trigger,actor,created_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, rule_version, judgment["decision"], json.dumps(judgment["blockers"], ensure_ascii=False), trigger, actor, now),
        )

    @staticmethod
    def _add_event(connection: sqlite3.Connection, plan_id: int, action: str, actor: str, payload: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO habitat_plan_events(plan_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
            (plan_id, action, actor, json.dumps(payload, ensure_ascii=False), now),
        )
