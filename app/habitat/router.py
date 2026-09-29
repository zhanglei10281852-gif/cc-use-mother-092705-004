from __future__ import annotations

from fastapi import APIRouter, Query

from app.habitat.schemas import (
    ConfirmRequest,
    DeviationRequest,
    ObservationCreate,
    ObservationResolve,
    PlanCreate,
    PlotCreate,
    RuleAdjustment,
    WithdrawRequest,
    WorkWindowCreate,
)
from app.habitat.service import HabitatService

router = APIRouter(prefix="/api/habitat", tags=["湿地生境维护窗口"])


def service() -> HabitatService:
    return HabitatService()


# ----- 保护规则版本 -----

@router.get("/rules")
def current_rules():
    return service().current_rules()


@router.get("/rules/versions")
def list_rule_versions():
    return {"items": service().list_rule_versions()}


@router.post("/rules/adjust", status_code=201)
def adjust_rules(payload: RuleAdjustment):
    return service().adjust_rules(payload.model_dump(exclude_none=True))


# ----- 地块 -----

@router.post("/plots", status_code=201)
def create_plot(payload: PlotCreate):
    return service().create_plot(payload.model_dump())


@router.get("/plots")
def list_plots():
    return {"items": service().list_plots()}


@router.get("/plots/{plot_id}")
def get_plot(plot_id: int):
    return service().get_plot(plot_id)


@router.get("/plots/{plot_id}/history")
def plot_history(plot_id: int):
    return service().plot_history(plot_id)


# ----- 物种迹象 -----

@router.post("/observations", status_code=201)
def add_observation(payload: ObservationCreate):
    return service().add_observation(payload.model_dump())


@router.post("/observations/{observation_id}/resolve")
def resolve_observation(observation_id: int, payload: ObservationResolve):
    return service().resolve_observation(observation_id, payload.model_dump())


# ----- 可用工期 -----

@router.post("/work-windows", status_code=201)
def add_work_window(payload: WorkWindowCreate):
    return service().add_work_window(payload.model_dump())


# ----- 割除计划 -----

@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate):
    return service().create_plan(payload.model_dump())


@router.get("/plans")
def list_plans(plot_id: int | None = None, status: str | None = Query(default=None, pattern="^(scheduled|postponed|confirmed|completed|withdrawn|reopened)$")):
    return {"items": service().list_plans(plot_id=plot_id, status=status)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int):
    return service().get_plan(plan_id)


@router.post("/plans/{plan_id}/reevaluate")
def reevaluate_plan(plan_id: int, actor: str = Query(default="manager", min_length=1)):
    return service().reevaluate_plan(plan_id, actor)


@router.post("/plans/{plan_id}/withdraw")
def withdraw_plan(plan_id: int, payload: WithdrawRequest):
    return service().withdraw_plan(plan_id, payload.actor, payload.reason)


@router.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: int, payload: ConfirmRequest):
    return service().confirm_plan(plan_id, payload.actor)


@router.post("/plans/{plan_id}/complete")
def complete_plan(plan_id: int, payload: ConfirmRequest):
    return service().complete_plan(plan_id, payload.actor)


@router.post("/plans/{plan_id}/deviation")
def register_deviation(plan_id: int, payload: DeviationRequest):
    return service().register_deviation(plan_id, payload.model_dump())


# ----- 已完成维护依据 -----

@router.get("/maintenance-records")
def list_maintenance_records(plot_id: int | None = None):
    return {"items": service().list_maintenance_records(plot_id)}
