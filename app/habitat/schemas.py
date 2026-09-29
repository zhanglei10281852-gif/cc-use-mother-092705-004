from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# 观察类别 -> 该类别默认保护规则的标志类型
SIGN_KINDS = ("shorebird_roost", "nest", "willow_sprout")
OBSERVATION_KINDS = ("shorebird_roost", "nest", "willow_sprout", "other")
PLAN_STATUSES = ("proposed", "confirmed", "in_progress", "completed", "withdrawn", "deferred")


def _parse_window(value: str) -> tuple[str, str]:
    parts = value.split("/", 1)
    if len(parts) != 2:
        raise ValueError("时间窗口必须使用 MM-DD/MM-DD 格式")
    return parts[0].strip(), parts[1].strip()


class PlotCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=200)

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("地块编码不能为空")
        return value


class SignCreate(BaseModel):
    """物种迹象记录：涉禽停歇、鸟巢占用、旱柳萌蘖等。"""

    kind: str = Field(..., pattern="^(shorebird_roost|nest|willow_sprout|other)$")
    species: str = Field(default="", max_length=80)
    observed_on: str = Field(..., min_length=8, max_length=10, description="观察日期 YYYY-MM-DD")
    active_window: str | None = Field(default=None, description="该迹象持续的日期窗口 MM-DD/MM-DD；为空则仅当天")
    detail: str = Field(default="", max_length=400)
    observer: str = Field(default="ranger", max_length=40)

    @field_validator("observed_on")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _validate_date(value)
        return value

    @field_validator("active_window")
    @classmethod
    def _check_window(cls, value: str | None) -> str | None:
        if value is not None:
            start, end = _parse_window(value)
            _validate_month_day(start)
            _validate_month_day(end)
        return value


class BreedingObservationCreate(BaseModel):
    """繁殖观察：产卵、孵卵、育雏等，保护窗口可延伸到预计离巢日。"""

    species: str = Field(..., min_length=1, max_length=80)
    stage: str = Field(..., pattern="^(courtship|egg|incubating|chick|fledged)$")
    observed_on: str = Field(..., min_length=8, max_length=10)
    expected_fledge_on: str | None = Field(default=None, min_length=8, max_length=10)
    nest_location: str = Field(default="", max_length=200)
    observer: str = Field(default="ranger", max_length=40)
    note: str = Field(default="", max_length=400)

    @field_validator("observed_on", "expected_fledge_on")
    @classmethod
    def _check_dates(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_date(value)
        return value


class WorkWindowCreate(BaseModel):
    """可用工期：允许割除作业的日期区间。"""

    starts_on: str = Field(..., min_length=8, max_length=10)
    ends_on: str = Field(..., min_length=8, max_length=10)
    capacity_hours: float = Field(default=8, gt=0, le=240)
    note: str = Field(default="", max_length=200)

    @field_validator("starts_on", "ends_on")
    @classmethod
    def _check_dates(cls, value: str) -> str:
        _validate_date(value)
        return value


class PlanCreate(BaseModel):
    plot_id: int
    scheduled_on: str = Field(..., min_length=8, max_length=10)
    work_window_id: int | None = None
    method: str = Field(default="mow", max_length=40)
    note: str = Field(default="", max_length=300)
    requested_by: str = Field(default="manager", max_length=40)

    @field_validator("scheduled_on")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _validate_date(value)
        return value


class PlanConfirm(BaseModel):
    actor: str = Field(default="manager", min_length=1, max_length=40)


class PlanComplete(BaseModel):
    actor: str = Field(default="ranger", min_length=1, max_length=40)
    note: str = Field(default="", max_length=300)


class PlanWithdraw(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)
    actor: str = Field(default="manager", min_length=1, max_length=40)


class DeviationCreate(BaseModel):
    """现场偏差登记：作业未按计划完成时记录实际情况。"""

    kind: str = Field(default="scope_change", pattern="^(scope_change|incomplete|obstruction|weather|early_stop|other)$")
    description: str = Field(..., min_length=1, max_length=500)
    unfinished: bool = Field(default=True, description="是否仍有未完成事项需要在重新打开时恢复")
    actor: str = Field(default="ranger", min_length=1, max_length=40)


class RuleVersionCreate(BaseModel):
    note: str = Field(default="", max_length=300)
    actor: str = Field(default="manager", min_length=1, max_length=40)
    # 各类迹象默认保护窗口（MM-DD/MM-DD），留空表示不按固定季节窗口保护
    shorebird_window: str | None = None
    nest_window: str | None = None
    willow_window: str | None = None
    # 缓冲天数：鸟巢在预计离巢日后继续保护的天数
    nest_buffer_days: int = Field(default=7, ge=0, le=180)
    # 涉禽停歇前后缓冲天数
    shorebird_buffer_days: int = Field(default=3, ge=0, le=60)
    # 旱柳萌蘖保护至萌蘖观察日之后 N 天（萌蘖枝条未木质化前避免割除碾压）
    willow_protection_days: int = Field(default=30, ge=0, le=365)
    # 是否允许在无可用工期记录时制定计划
    require_work_window: bool = True

    @field_validator("shorebird_window", "nest_window", "willow_window")
    @classmethod
    def _check_windows(cls, value: str | None) -> str | None:
        if value is not None:
            start, end = _parse_window(value)
            _validate_month_day(start)
            _validate_month_day(end)
        return value


def _validate_date(value: str) -> None:
    from datetime import datetime

    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc


def _validate_month_day(value: str) -> None:
    from datetime import datetime

    try:
        datetime.strptime(f"2000-{value}", "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("月日必须使用 MM-DD 格式且合法") from exc
