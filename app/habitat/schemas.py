from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 允许登记的物种迹象：涉禽停歇、占用鸟巢、旱柳萌蘖等都会改变割除时机。
SIGN_KINDS = {"shorebird_rest", "nest_occupation", "willow_sprout", "other"}
# 只有部分迹象参与窗口判定，其它迹象仅作现场记录留痕。
BLOCKING_KINDS = {"shorebird_rest", "nest_occupation", "willow_sprout"}


class PlotCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, description="地块编码，如 T-01")
    name: str = Field(..., min_length=2, max_length=120)
    wetland_type: Literal["滩涂", "芦苇荡", "滨水带", "人工湿地"] = Field(..., description="湿地类型")
    area_mu: float = Field(..., gt=0, le=100000, description="面积（亩）")
    note: str = Field(default="", max_length=500)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class ObservationCreate(BaseModel):
    plot_id: int
    kind: Literal["shorebird_rest", "nest_occupation", "willow_sprout", "other"]
    species: str = Field(default="", max_length=120, description="物种名，如 黑翅长脚鹬、旱柳")
    observed_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="观察日期 YYYY-MM-DD")
    active: bool = Field(default=True, description="迹象是否仍在影响该地块")
    detail: str = Field(default="", max_length=500)
    observer: str = Field(default="patrol", min_length=1, max_length=80)


class ObservationResolve(BaseModel):
    actor: str = Field(default="patrol", min_length=1, max_length=80)
    note: str = Field(default="", max_length=500, description="解除原因，如 雏鸟已离巢")


class WorkWindowCreate(BaseModel):
    plot_id: int
    start_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="可用工期开始")
    end_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="可用工期结束")
    crew: str = Field(default="", max_length=120, description="可投入的班组")
    note: str = Field(default="", max_length=500)


class PlanCreate(BaseModel):
    plot_id: int
    window_id: int
    planned_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="计划割除日期")
    method: Literal["割除", "间伐", "清理"] = Field(default="割除")
    created_by: str = Field(default="manager", min_length=1, max_length=80)


class WithdrawRequest(BaseModel):
    actor: str = Field(..., min_length=1, max_length=80)
    reason: str = Field(..., min_length=2, max_length=1000, description="撤回原因")


class ConfirmRequest(BaseModel):
    actor: str = Field(default="crew", min_length=1, max_length=80)


class DeviationRequest(BaseModel):
    """登记现场偏差：开工后发现实际与计划不符时记录，未完成事项随重新打开恢复。"""

    actor: str = Field(default="crew", min_length=1, max_length=80)
    deviation_type: Literal["范围偏差", "物候偏差", "设备偏差", "天气中断", "物种新迹象", "其它"] = Field(...)
    description: str = Field(..., min_length=2, max_length=1000)
    completion_ratio: float = Field(default=0.0, ge=0.0, le=1.0, description="已完成比例，<1 时计划重新打开")
    reopen: bool = Field(default=True, description="是否将未完成事项恢复为待安排")


class RuleAdjustment(BaseModel):
    """管理者调整保护规则；新版本只影响之后创建、以及尚未确认的计划。"""

    actor: str = Field(..., min_length=1, max_length=80)
    reason: str = Field(..., min_length=2, max_length=1000)
    shorebird_buffer_days: int | None = Field(default=None, ge=0, le=365)
    nest_buffer_days: int | None = Field(default=None, ge=0, le=365)
    sprout_block: bool | None = Field(default=None, description="旱柳萌蘖期是否一律延后")
    blocked_kinds: list[Literal["shorebird_rest", "nest_occupation", "willow_sprout"]] | None = Field(default=None, max_length=3)
