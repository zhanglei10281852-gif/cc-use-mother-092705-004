from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS habitat_plots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habitat_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    shorebird_window TEXT,
    nest_window TEXT,
    willow_window TEXT,
    nest_buffer_days INTEGER NOT NULL,
    shorebird_buffer_days INTEGER NOT NULL,
    willow_protection_days INTEGER NOT NULL,
    require_work_window INTEGER NOT NULL CHECK(require_work_window IN (0,1)),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habitat_signs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('shorebird_roost','nest','willow_sprout','other')),
    species TEXT NOT NULL DEFAULT '',
    observed_on TEXT NOT NULL,
    active_window TEXT,
    detail TEXT NOT NULL DEFAULT '',
    observer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_signs_plot ON habitat_signs(plot_id, observed_on);
CREATE TABLE IF NOT EXISTS habitat_breeding_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    species TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('courtship','egg','incubating','chick','fledged')),
    observed_on TEXT NOT NULL,
    expected_fledge_on TEXT,
    nest_location TEXT NOT NULL DEFAULT '',
    observer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_breeding_plot ON habitat_breeding_observations(plot_id, observed_on);
CREATE TABLE IF NOT EXISTS habitat_work_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    capacity_hours REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK(ends_on >= starts_on)
);
CREATE INDEX IF NOT EXISTS idx_habitat_windows_plot ON habitat_work_windows(plot_id, starts_on);
CREATE TABLE IF NOT EXISTS habitat_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER NOT NULL REFERENCES habitat_plots(id) ON DELETE RESTRICT,
    scheduled_on TEXT NOT NULL,
    work_window_id INTEGER REFERENCES habitat_work_windows(id),
    method TEXT NOT NULL DEFAULT 'mow',
    note TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('proposed','deferred','confirmed','in_progress','suspended','completed','withdrawn')),
    is_executable INTEGER NOT NULL DEFAULT 0 CHECK(is_executable IN (0,1)),
    rule_version_id INTEGER REFERENCES habitat_rule_versions(id),
    evaluated_at TEXT NOT NULL DEFAULT '',
    decision_rule_version_id INTEGER REFERENCES habitat_rule_versions(id),
    decision_basis_json TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT NOT NULL DEFAULT '',
    withdraw_reason TEXT NOT NULL DEFAULT '',
    withdrawn_by TEXT NOT NULL DEFAULT '',
    withdrawn_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_plans_plot ON habitat_plans(plot_id, scheduled_on);
CREATE INDEX IF NOT EXISTS idx_habitat_plans_status ON habitat_plans(status, scheduled_on);
CREATE TABLE IF NOT EXISTS habitat_plan_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES habitat_plans(id) ON DELETE CASCADE,
    block_kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    species TEXT NOT NULL DEFAULT '',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    rule_version_id INTEGER REFERENCES habitat_rule_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_habitat_blocks_plan ON habitat_plan_blocks(plan_id);
CREATE TABLE IF NOT EXISTS habitat_deviations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES habitat_plans(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    unfinished INTEGER NOT NULL DEFAULT 1 CHECK(unfinished IN (0,1)),
    resolved_at TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_deviations_plan ON habitat_deviations(plan_id);
CREATE TABLE IF NOT EXISTS habitat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plot_id INTEGER,
    plan_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_events_plot ON habitat_events(plot_id, id);
CREATE INDEX IF NOT EXISTS idx_habitat_events_plan ON habitat_events(plan_id, id);
"""

# 内置初始保护规则（v1）
DEFAULT_RULES: dict[str, Any] = {
    "shorebird_window": "09-01/04-30",
    "nest_window": "04-01/07-31",
    "willow_window": None,
    "nest_buffer_days": 7,
    "shorebird_buffer_days": 3,
    "willow_protection_days": 30,
    "require_work_window": True,
}

KIND_LABELS = {
    "work_window": "可用工期",
    "shorebird_roost": "涉禽停歇",
    "nest": "鸟巢占用",
    "willow_sprout": "旱柳萌蘖",
    "breeding": "繁殖观察",
}


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def ensure_schema() -> None:
    """启动时建表并写入内置 v1 规则。"""
    HabitatService()


def _month_day_windows_contains(window: str, day: date) -> bool:
    """判断 MM-DD/MM-DD 窗口（允许跨年）是否覆盖某日。"""
    start_text, end_text = (part.strip() for part in window.split("/", 1))
    start = datetime.strptime(f"{day.year}-{start_text}", "%Y-%m-%d").date()
    end = datetime.strptime(f"{day.year}-{end_text}", "%Y-%m-%d").date()
    if start <= end:
        return start <= day <= end
    return day >= start or day <= end


def _iso(value: date) -> str:
    return value.isoformat()


class HabitatService:
    """地块、物种迹象、繁殖观察、可用工期与割除维护窗口的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.connection.executescript(SCHEMA)
        self._ensure_default_rules()

    # ------------------------------------------------------------------ rules

    def _ensure_default_rules(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) FROM habitat_rule_versions").fetchone()[0]
        if count == 0:
            with transaction(immediate=True) as connection:
                self._insert_rule_version(connection, DEFAULT_RULES, note="内置默认保护规则", actor="system", now=to_storage(self.clock.now()))

    def _insert_rule_version(self, connection: sqlite3.Connection, payload: dict[str, Any], *, note: str, actor: str, now: str) -> dict[str, Any]:
        version_row = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM habitat_rule_versions").fetchone()
        version = version_row[0]
        cursor = connection.execute(
            """INSERT INTO habitat_rule_versions(version,shorebird_window,nest_window,willow_window,nest_buffer_days,
               shorebird_buffer_days,willow_protection_days,require_work_window,note,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (version, payload.get("shorebird_window"), payload.get("nest_window"), payload.get("willow_window"),
             payload["nest_buffer_days"], payload["shorebird_buffer_days"], payload["willow_protection_days"],
             1 if payload["require_work_window"] else 0, note, actor, now),
        )
        row = connection.execute("SELECT * FROM habitat_rule_versions WHERE id=?", (cursor.lastrowid,)).fetchone()
        connection.execute(
            "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(NULL,NULL,'rule.publish',?,?,?)",
            (actor, json.dumps({"version": version}, ensure_ascii=False), now),
        )
        return dict(row)

    def current_rules(self, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM habitat_rule_versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:  # 极端情况下兜底
            raise NotFoundError("保护规则尚未发布")
        return row

    def list_rule_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM habitat_rule_versions ORDER BY version DESC").fetchall()
        return [dict(row) for row in rows]

    def publish_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发布新版保护规则：仅重新评估尚未确认的计划。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            rule = self._insert_rule_version(
                connection, payload, note=payload.get("note", ""), actor=payload.get("actor", "manager"), now=now
            )
            # 已确认及之后状态的计划保持原有判定依据，不受新规则影响
            pending = connection.execute(
                "SELECT * FROM habitat_plans WHERE status IN ('proposed','deferred') ORDER BY id"
            ).fetchall()
            for plan in pending:
                self._evaluate_plan(connection, dict(plan), rule, now)
                connection.execute(
                    "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.reevaluate',?,?,?)",
                    (plan["plot_id"], plan["id"], payload.get("actor", "manager"),
                     json.dumps({"reason": "rule_version", "rule_version": rule["version"]}, ensure_ascii=False), now),
                )
            rule["affected_plan_ids"] = [plan["id"] for plan in pending]
            return dict(rule)

    # ------------------------------------------------------------------ plots

    def create_plot(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO habitat_plots(code,name,location,created_at) VALUES(?,?,?,?)",
                    (payload["code"], payload.get("name", ""), payload.get("location", ""), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("地块编码已存在") from exc
            plot_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,NULL,'plot.create','manager',?,?)",
                (plot_id, json.dumps({"code": payload["code"]}, ensure_ascii=False), now),
            )
            return dict(connection.execute("SELECT * FROM habitat_plots WHERE id=?", (plot_id,)).fetchone())

    def list_plots(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM habitat_plots ORDER BY id").fetchall()]

    def _require_plot(self, connection: sqlite3.Connection, plot_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM habitat_plots WHERE id=?", (plot_id,)).fetchone()
        if row is None:
            raise NotFoundError("地块不存在")
        return row

    # ------------------------------------------------------------ observations

    def add_sign(self, plot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_plot(connection, plot_id)
            if payload.get("active_window"):
                self._validate_window_text(payload["active_window"])
            cursor = connection.execute(
                """INSERT INTO habitat_signs(plot_id,kind,species,observed_on,active_window,detail,observer,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (plot_id, payload["kind"], payload.get("species", ""), payload["observed_on"],
                 payload.get("active_window"), payload.get("detail", ""), payload.get("observer", "ranger"), now),
            )
            sign_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,NULL,'sign.create',?,?,?)",
                (plot_id, payload.get("observer", "ranger"),
                 json.dumps({"sign_id": sign_id, "kind": payload["kind"]}, ensure_ascii=False), now),
            )
            row = dict(connection.execute("SELECT * FROM habitat_signs WHERE id=?", (sign_id,)).fetchone())
            # 新迹象可能改变尚未确认计划的窗口判定
            self._refresh_plot_plans(connection, plot_id, actor=payload.get("observer", "ranger"), now=now, reason="sign_created")
            return row

    def list_signs(self, plot_id: int) -> list[dict[str, Any]]:
        self._require_plot(self.connection, plot_id)
        rows = self.connection.execute("SELECT * FROM habitat_signs WHERE plot_id=? ORDER BY observed_on,id", (plot_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_breeding(self, plot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        observed = _parse_date(payload["observed_on"])
        if payload.get("expected_fledge_on"):
            fledge = _parse_date(payload["expected_fledge_on"])
            if fledge < observed:
                raise ValidationError("预计离巢日不能早于观察日")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_plot(connection, plot_id)
            cursor = connection.execute(
                """INSERT INTO habitat_breeding_observations(plot_id,species,stage,observed_on,expected_fledge_on,
                   nest_location,observer,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (plot_id, payload["species"], payload["stage"], payload["observed_on"],
                 payload.get("expected_fledge_on"), payload.get("nest_location", ""),
                 payload.get("observer", "ranger"), payload.get("note", ""), now),
            )
            obs_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,NULL,'breeding.create',?,?,?)",
                (plot_id, payload.get("observer", "ranger"),
                 json.dumps({"breeding_id": obs_id, "stage": payload["stage"], "species": payload["species"]}, ensure_ascii=False), now),
            )
            row = dict(connection.execute("SELECT * FROM habitat_breeding_observations WHERE id=?", (obs_id,)).fetchone())
            self._refresh_plot_plans(connection, plot_id, actor=payload.get("observer", "ranger"), now=now, reason="breeding_created")
            return row

    def list_breeding(self, plot_id: int) -> list[dict[str, Any]]:
        self._require_plot(self.connection, plot_id)
        rows = self.connection.execute(
            "SELECT * FROM habitat_breeding_observations WHERE plot_id=? ORDER BY observed_on,id", (plot_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def add_work_window(self, plot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        starts = _parse_date(payload["starts_on"])
        ends = _parse_date(payload["ends_on"])
        if ends < starts:
            raise ValidationError("可用工期结束日不能早于开始日")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_plot(connection, plot_id)
            cursor = connection.execute(
                "INSERT INTO habitat_work_windows(plot_id,starts_on,ends_on,capacity_hours,note,created_at) VALUES(?,?,?,?,?,?)",
                (plot_id, payload["starts_on"], payload["ends_on"], payload["capacity_hours"], payload.get("note", ""), now),
            )
            window_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,NULL,'work_window.create',?,?,?)",
                (plot_id, "manager", json.dumps({"work_window_id": window_id}, ensure_ascii=False), now),
            )
            row = dict(connection.execute("SELECT * FROM habitat_work_windows WHERE id=?", (window_id,)).fetchone())
            # 新增工期可能解除“无可用工期”的阻塞
            self._refresh_plot_plans(connection, plot_id, actor="manager", now=now, reason="work_window_created")
            return row

    def list_work_windows(self, plot_id: int) -> list[dict[str, Any]]:
        self._require_plot(self.connection, plot_id)
        rows = self.connection.execute(
            "SELECT * FROM habitat_work_windows WHERE plot_id=? ORDER BY starts_on,id", (plot_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ plans

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_plot(connection, payload["plot_id"])
            window_id = payload.get("work_window_id")
            if window_id is not None:
                window = connection.execute(
                    "SELECT * FROM habitat_work_windows WHERE id=? AND plot_id=?",
                    (window_id, payload["plot_id"]),
                ).fetchone()
                if window is None:
                    raise ValidationError("可用工期不存在或不属于该地块")
                if not (window["starts_on"] <= payload["scheduled_on"] <= window["ends_on"]):
                    raise ValidationError("计划日期不在所指定的可用工期区间内")
            cursor = connection.execute(
                """INSERT INTO habitat_plans(plot_id,scheduled_on,work_window_id,method,note,requested_by,
                   status,is_executable,rule_version_id,evaluated_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (payload["plot_id"], payload["scheduled_on"], window_id, payload.get("method", "mow"),
                 payload.get("note", ""), payload.get("requested_by", "manager"),
                 "deferred", 0, None, "", now, now),
            )
            plan_id = cursor.lastrowid
            rule = self.current_rules(connection)
            plan = dict(connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone())
            plan = self._evaluate_plan(connection, plan, rule, now)
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.create',?,?,?)",
                (payload["plot_id"], plan_id, payload.get("requested_by", "manager"),
                 json.dumps({"scheduled_on": payload["scheduled_on"]}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    def list_plans(self, plot_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM habitat_plans"
        clauses: list[str] = []
        params: list[Any] = []
        if plot_id is not None:
            clauses.append("plot_id=?")
            params.append(plot_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scheduled_on,id"
        rows = self.connection.execute(sql, params).fetchall()
        return [self._plan_detail(dict(row), connection=self.connection) for row in rows]

    def get_plan(self, plan_id: int, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("维护计划不存在")
        return self._plan_detail(dict(row), connection=connection)

    def _plan_detail(self, plan: dict[str, Any], *, connection: sqlite3.Connection) -> dict[str, Any]:
        blocks = [dict(row) for row in connection.execute(
            "SELECT * FROM habitat_plan_blocks WHERE plan_id=? ORDER BY id", (plan["id"],)
        ).fetchall()]
        deviations = [dict(row) for row in connection.execute(
            "SELECT * FROM habitat_deviations WHERE plan_id=? ORDER BY id", (plan["id"],)
        ).fetchall()]
        plan["blocks"] = blocks
        plan["deviations"] = deviations
        try:
            plan["decision_basis"] = json.loads(plan.get("decision_basis_json") or "[]")
        except json.JSONDecodeError:
            plan["decision_basis"] = []
        rule_version = None
        if plan.get("rule_version_id"):
            rule_row = connection.execute(
                "SELECT version FROM habitat_rule_versions WHERE id=?", (plan["rule_version_id"],)
            ).fetchone()
            rule_version = rule_row["version"] if rule_row else None
        plan["rule_version"] = rule_version
        decision_version = None
        if plan.get("decision_rule_version_id"):
            rule_row = connection.execute(
                "SELECT version FROM habitat_rule_versions WHERE id=?", (plan["decision_rule_version_id"],)
            ).fetchone()
            decision_version = rule_row["version"] if rule_row else None
        plan["decision_rule_version"] = decision_version
        return plan

    def confirm_plan(self, plan_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan_row = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("维护计划不存在")
            if plan_row["status"] not in ("proposed", "deferred"):
                raise ConflictError(f"当前状态 {plan_row['status']} 不能确认")
            # 确认前按最新规则再评估一次，仍被阻止则明确指出阻止来源
            plan = self._evaluate_plan(connection, dict(plan_row), self.current_rules(connection), now)
            if not plan["is_executable"]:
                blocks = connection.execute(
                    "SELECT * FROM habitat_plan_blocks WHERE plan_id=? ORDER BY id", (plan_id,)
                ).fetchall()
                raise ConflictError(
                    "计划仍处于保护窗口内，不能确认",
                    context={"blocks": [dict(row) for row in blocks]},
                )
            basis = [dict(row) for row in connection.execute(
                "SELECT * FROM habitat_plan_blocks WHERE plan_id=? ORDER BY id", (plan_id,)
            ).fetchall()]
            rule = self.current_rules(connection)
            connection.execute(
                """UPDATE habitat_plans SET status='confirmed',decision_rule_version_id=?,decision_basis_json=?,
                   decided_at=?,rule_version_id=?,evaluated_at=?,updated_at=? WHERE id=?""",
                (rule["id"], json.dumps(basis, ensure_ascii=False), now, rule["id"], now, now, plan_id),
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.confirm',?,?,?)",
                (plan_row["plot_id"], plan_id, actor,
                 json.dumps({"rule_version": rule["version"]}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    def start_plan(self, plan_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("维护计划不存在")
            if plan["status"] != "confirmed":
                raise ConflictError(f"当前状态 {plan['status']} 不能开工")
            connection.execute(
                "UPDATE habitat_plans SET status='in_progress',updated_at=? WHERE id=?", (now, plan_id)
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.start',?,?,?)",
                (plan["plot_id"], plan_id, actor, "{}", now),
            )
            return self.get_plan(plan_id, connection=connection)

    def complete_plan(self, plan_id: int, actor: str, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("维护计划不存在")
            if plan["status"] not in ("in_progress", "suspended"):
                raise ConflictError(f"当前状态 {plan['status']} 不能完成")
            unresolved = connection.execute(
                "SELECT id FROM habitat_deviations WHERE plan_id=? AND unfinished=1 AND resolved_at=''", (plan_id,)
            ).fetchall()
            if unresolved:
                raise ConflictError(
                    "存在登记为未完成的现场偏差，请先重新打开处理后再完工",
                    context={"unresolved_deviation_ids": [row["id"] for row in unresolved]},
                )
            # 完工即冻结：保留确认时的规则依据，之后规则变更不再影响本计划
            connection.execute(
                "UPDATE habitat_plans SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                (now, now, plan_id),
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.complete',?,?,?)",
                (plan["plot_id"], plan_id, actor,
                 json.dumps({"note": note, "decision_rule_version": self._rule_version_of(connection, plan["decision_rule_version_id"])}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    def withdraw_plan(self, plan_id: int, reason: str, actor: str) -> dict[str, Any]:
        """撤回尚未开始的安排（已完工或已开工的不允许撤回）。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("维护计划不存在")
            if plan["status"] in ("in_progress", "suspended", "completed", "withdrawn"):
                raise ConflictError(f"当前状态 {plan['status']} 不能撤回，仅尚未开始的安排可撤回")
            connection.execute(
                "UPDATE habitat_plans SET status='withdrawn',withdraw_reason=?,withdrawn_by=?,withdrawn_at=?,updated_at=? WHERE id=?",
                (reason, actor, now, now, plan_id),
            )
            connection.execute(
                "DELETE FROM habitat_plan_blocks WHERE plan_id=?", (plan_id,)
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.withdraw',?,?,?)",
                (plan["plot_id"], plan_id, actor, json.dumps({"reason": reason}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    def register_deviation(self, plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """登记现场偏差；若存在未完成事项，计划挂起，等待重新打开。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("维护计划不存在")
            if plan["status"] != "in_progress":
                raise ConflictError(f"当前状态 {plan['status']} 不能登记现场偏差")
            cursor = connection.execute(
                "INSERT INTO habitat_deviations(plan_id,kind,description,unfinished,recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                (plan_id, payload["kind"], payload["description"], 1 if payload.get("unfinished", True) else 0,
                 payload.get("actor", "ranger"), now),
            )
            deviation_id = cursor.lastrowid
            new_status = "suspended" if payload.get("unfinished", True) else "in_progress"
            connection.execute(
                "UPDATE habitat_plans SET status=?,updated_at=? WHERE id=?", (new_status, now, plan_id)
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'deviation.create',?,?,?)",
                (plan["plot_id"], plan_id, payload.get("actor", "ranger"),
                 json.dumps({"deviation_id": deviation_id, "kind": payload["kind"], "unfinished": payload.get("unfinished", True)}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    def reopen_plan(self, plan_id: int, actor: str) -> dict[str, Any]:
        """重新打开挂起的计划，恢复未完成事项。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM habitat_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("维护计划不存在")
            if plan["status"] != "suspended":
                raise ConflictError(f"当前状态 {plan['status']} 无需重新打开")
            pending = connection.execute(
                "SELECT * FROM habitat_deviations WHERE plan_id=? AND unfinished=1 AND resolved_at='' ORDER BY id",
                (plan_id,),
            ).fetchall()
            if not pending:
                raise ConflictError("没有需要恢复的未完成事项")
            deviation_ids = [row["id"] for row in pending]
            connection.execute(
                "UPDATE habitat_deviations SET resolved_at=? WHERE id IN (%s)" % ",".join("?" * len(deviation_ids)),
                (now, *deviation_ids),
            )
            connection.execute(
                "UPDATE habitat_plans SET status='in_progress',updated_at=? WHERE id=?", (now, plan_id)
            )
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.reopen',?,?,?)",
                (plan["plot_id"], plan_id, actor,
                 json.dumps({"restored_deviation_ids": deviation_ids}, ensure_ascii=False), now),
            )
            return self.get_plan(plan_id, connection=connection)

    # ------------------------------------------------------------- evaluation

    def _refresh_plot_plans(self, connection: sqlite3.Connection, plot_id: int, *, actor: str, now: str, reason: str) -> None:
        rule = self.current_rules(connection)
        rows = connection.execute(
            "SELECT * FROM habitat_plans WHERE plot_id=? AND status IN ('proposed','deferred') ORDER BY id",
            (plot_id,),
        ).fetchall()
        for row in rows:
            self._evaluate_plan(connection, dict(row), rule, now)
            connection.execute(
                "INSERT INTO habitat_events(plot_id,plan_id,action,actor,detail_json,created_at) VALUES(?,?,'plan.reevaluate',?,?,?)",
                (plot_id, row["id"], actor, json.dumps({"reason": reason}, ensure_ascii=False), now),
            )

    def _sign_protection(self, sign: sqlite3.Row, rules: sqlite3.Row, target: date) -> tuple[str, str] | None:
        """返回迹象在目标年份覆盖目标日的保护区间 [start,end]；不覆盖返回 None。"""
        observed = _parse_date(sign["observed_on"])
        kind = sign["kind"]
        if kind == "shorebird_roost":
            start = observed - timedelta(days=rules["shorebird_buffer_days"])
            end = observed + timedelta(days=rules["shorebird_buffer_days"])
            season_window = rules["shorebird_window"]
        elif kind == "nest":
            start = observed - timedelta(days=rules["nest_buffer_days"])
            end = observed + timedelta(days=21 + rules["nest_buffer_days"])
            season_window = rules["nest_window"]
        elif kind == "willow_sprout":
            start = observed
            end = observed + timedelta(days=rules["willow_protection_days"])
            season_window = rules["willow_window"]
        else:
            return None
        # 登记的持续窗口优先；否则并入规则定义的季节窗口
        window_text = sign["active_window"] or season_window
        if window_text and _month_day_windows_contains(window_text, target):
            season_start_text, season_end_text = (part.strip() for part in window_text.split("/", 1))
            year = target.year
            season_start = _parse_date(f"{year}-{season_start_text}")
            season_end = _parse_date(f"{year}-{season_end_text}")
            if season_start > season_end:  # 跨年窗口，取覆盖目标日的那一段
                if target >= season_start:
                    season_end = _parse_date(f"{year + 1}-{season_end_text}")
                else:
                    season_start = _parse_date(f"{year - 1}-{season_start_text}")
            start = min(start, season_start)
            end = max(end, season_end)
        return _iso(start), _iso(end)

    def _breeding_protection(self, breeding: sqlite3.Row, rules: sqlite3.Row) -> tuple[str, str]:
        observed = _parse_date(breeding["observed_on"])
        start = observed - timedelta(days=3)
        if breeding["expected_fledge_on"]:
            fledge = _parse_date(breeding["expected_fledge_on"])
        else:
            fledge = observed + timedelta(days=21)
        end = fledge + timedelta(days=rules["nest_buffer_days"])
        return _iso(start), _iso(end)

    def _compute_blocks(self, connection: sqlite3.Connection, plan: sqlite3.Row, rules: sqlite3.Row) -> list[dict[str, Any]]:
        target = _parse_date(plan["scheduled_on"])
        blocks: list[dict[str, Any]] = []

        # 1. 可用工期
        if rules["require_work_window"]:
            window = None
            if plan["work_window_id"]:
                candidate = connection.execute(
                    "SELECT * FROM habitat_work_windows WHERE id=?", (plan["work_window_id"],)
                ).fetchone()
                # 显式指定的工期也必须覆盖计划日，否则视为无覆盖并继续匹配
                if candidate is not None and candidate["starts_on"] <= plan["scheduled_on"] <= candidate["ends_on"]:
                    window = candidate
            if window is None:
                window = connection.execute(
                    "SELECT * FROM habitat_work_windows WHERE plot_id=? AND starts_on<=? AND ends_on>=? ORDER BY id LIMIT 1",
                    (plan["plot_id"], plan["scheduled_on"], plan["scheduled_on"]),
                ).fetchone()
            if window is None:
                blocks.append({
                    "block_kind": "work_window",
                    "source_type": "work_window",
                    "source_id": None,
                    "species": "",
                    "window_start": "",
                    "window_end": "",
                    "reason": "计划日期不在任何已登记的可用工期内",
                    "rule_version_id": rules["id"],
                })

        # 2. 物种迹象
        signs = connection.execute(
            "SELECT * FROM habitat_signs WHERE plot_id=? AND kind != 'other' ORDER BY id", (plan["plot_id"],)
        ).fetchall()
        for sign in signs:
            protection = self._sign_protection(sign, rules, target)
            if protection and protection[0] <= plan["scheduled_on"] <= protection[1]:
                label = KIND_LABELS.get(sign["kind"], sign["kind"])
                species = sign["species"] or "未记名物种"
                blocks.append({
                    "block_kind": sign["kind"],
                    "source_type": "sign",
                    "source_id": sign["id"],
                    "species": sign["species"],
                    "window_start": protection[0],
                    "window_end": protection[1],
                    "reason": f"{label}迹象（记录 #{sign['id']}，{species}）保护窗口 {protection[0]} 至 {protection[1]}，阻止割除",
                    "rule_version_id": rules["id"],
                })

        # 3. 繁殖观察
        breedings = connection.execute(
            "SELECT * FROM habitat_breeding_observations WHERE plot_id=? AND stage != 'fledged' ORDER BY id",
            (plan["plot_id"],),
        ).fetchall()
        for breeding in breedings:
            start_text, end_text = self._breeding_protection(breeding, rules)
            if start_text <= plan["scheduled_on"] <= end_text:
                blocks.append({
                    "block_kind": "breeding",
                    "source_type": "breeding",
                    "source_id": breeding["id"],
                    "species": breeding["species"],
                    "window_start": start_text,
                    "window_end": end_text,
                    "reason": (
                        f"繁殖观察（记录 #{breeding['id']}，{breeding['species']}，阶段 {breeding['stage']}）"
                        f"保护至预计离巢缓冲期末 {end_text}，阻止割除"
                    ),
                    "rule_version_id": rules["id"],
                })
        return blocks

    def _evaluate_plan(self, connection: sqlite3.Connection, plan: dict[str, Any], rules: sqlite3.Row, now: str) -> dict[str, Any]:
        """依据当前规则与观察重算计划的阻止项与可执行性（不改动已确认计划）。"""
        blocks = self._compute_blocks(connection, plan, rules)
        connection.execute("DELETE FROM habitat_plan_blocks WHERE plan_id=?", (plan["id"],))
        for block in blocks:
            connection.execute(
                """INSERT INTO habitat_plan_blocks(plan_id,block_kind,source_type,source_id,species,window_start,
                   window_end,reason,rule_version_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                (plan["id"], block["block_kind"], block["source_type"], block["source_id"], block["species"],
                 block["window_start"], block["window_end"], block["reason"], block["rule_version_id"]),
            )
        executable = not blocks
        new_status = "proposed" if executable else "deferred"
        # 已确认/进行中/完工等状态的计划不应通过此方法改状态
        if plan["status"] in ("proposed", "deferred"):
            connection.execute(
                "UPDATE habitat_plans SET is_executable=?,status=?,rule_version_id=?,evaluated_at=?,updated_at=? WHERE id=?",
                (1 if executable else 0, new_status, rules["id"], now, now, plan["id"]),
            )
            plan["status"] = new_status
        plan["is_executable"] = 1 if executable else 0
        plan["blocks"] = blocks
        return plan

    # ---------------------------------------------------------------- history

    def plot_history(self, plot_id: int) -> dict[str, Any]:
        connection = self.connection
        plot = connection.execute("SELECT * FROM habitat_plots WHERE id=?", (plot_id,)).fetchone()
        if plot is None:
            raise NotFoundError("地块不存在")
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM habitat_events WHERE plot_id=? ORDER BY id", (plot_id,)
        ).fetchall()]
        plans = [dict(row) for row in connection.execute(
            "SELECT id,scheduled_on,status,is_executable,withdraw_reason,completed_at,rule_version_id,"
            "decision_rule_version_id FROM habitat_plans WHERE plot_id=? ORDER BY id",
            (plot_id,),
        ).fetchall()]
        for event in events:
            try:
                event["detail"] = json.loads(event.pop("detail_json") or "{}")
            except json.JSONDecodeError:
                event["detail"] = {}
        return {"plot": dict(plot), "events": events, "plans": plans}

    def _rule_version_of(self, connection: sqlite3.Connection, rule_id: Any) -> int | None:
        if not rule_id:
            return None
        row = connection.execute("SELECT version FROM habitat_rule_versions WHERE id=?", (rule_id,)).fetchone()
        return row["version"] if row else None

    def _validate_window_text(self, window: str) -> None:
        try:
            start_text, end_text = (part.strip() for part in window.split("/", 1))
            datetime.strptime(f"2000-{start_text}", "%Y-%m-%d")
            datetime.strptime(f"2000-{end_text}", "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("时间窗口必须使用 MM-DD/MM-DD 格式且日期合法") from exc
