from __future__ import annotations

from fastapi import APIRouter, Query

from app.habitat.schemas import (
    BreedingObservationCreate,
    DeviationCreate,
    PlanComplete,
    PlanConfirm,
    PlanCreate,
    PlanWithdraw,
    PlotCreate,
    RuleVersionCreate,
    SignCreate,
    WorkWindowCreate,
)
from app.habitat.service import HabitatService

router = APIRouter(prefix="/api/habitat", tags=["湿地生境维护窗口"])


def service() -> HabitatService:
    return HabitatService()


# --------------------------------------------------------------------- rules

@router.get("/rules")
def list_rules():
    """核对规则版本列表。"""
    items = service().list_rule_versions()
    return {"items": items, "current": items[0]["version"] if items else None}


@router.post("/rules", status_code=201)
def publish_rules(payload: RuleVersionCreate):
    """发布新版保护规则，仅影响尚未确认的计划。"""
    return service().publish_rules(payload.model_dump())


# --------------------------------------------------------------------- plots

@router.post("/plots", status_code=201)
def create_plot(payload: PlotCreate):
    return service().create_plot(payload.model_dump())


@router.get("/plots")
def list_plots():
    return {"items": service().list_plots()}


@router.get("/plots/{plot_id}/history")
def plot_history(plot_id: int):
    """核对地块历史：事件流、计划清单与状态依据。"""
    return service().plot_history(plot_id)


# -------------------------------------------------------------- observations

@router.post("/plots/{plot_id}/signs", status_code=201)
def add_sign(plot_id: int, payload: SignCreate):
    return service().add_sign(plot_id, payload.model_dump())


@router.get("/plots/{plot_id}/signs")
def list_signs(plot_id: int):
    return {"items": service().list_signs(plot_id)}


@router.post("/plots/{plot_id}/breeding", status_code=201)
def add_breeding(plot_id: int, payload: BreedingObservationCreate):
    return service().add_breeding(plot_id, payload.model_dump())


@router.get("/plots/{plot_id}/breeding")
def list_breeding(plot_id: int):
    return {"items": service().list_breeding(plot_id)}


@router.post("/plots/{plot_id}/work-windows", status_code=201)
def add_work_window(plot_id: int, payload: WorkWindowCreate):
    return service().add_work_window(plot_id, payload.model_dump())


@router.get("/plots/{plot_id}/work-windows")
def list_work_windows(plot_id: int):
    return {"items": service().list_work_windows(plot_id)}


# --------------------------------------------------------------------- plans

@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate):
    """制定割除计划，立即返回可执行或需延后及具体阻止来源。"""
    return service().create_plan(payload.model_dump())


@router.get("/plans")
def list_plans(plot_id: int | None = Query(default=None), status: str | None = Query(default=None)):
    return {"items": service().list_plans(plot_id=plot_id, status=status)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int):
    """核对计划状态、阻止项、规则版本与现场偏差。"""
    return service().get_plan(plan_id)


@router.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: int, payload: PlanConfirm):
    return service().confirm_plan(plan_id, payload.actor)


@router.post("/plans/{plan_id}/start")
def start_plan(plan_id: int, payload: PlanConfirm):
    return service().start_plan(plan_id, payload.actor)


@router.post("/plans/{plan_id}/complete")
def complete_plan(plan_id: int, payload: PlanComplete):
    return service().complete_plan(plan_id, payload.actor, payload.note)


@router.post("/plans/{plan_id}/withdraw")
def withdraw_plan(plan_id: int, payload: PlanWithdraw):
    """撤回尚未开始的安排，并记录撤回原因。"""
    return service().withdraw_plan(plan_id, payload.reason, payload.actor)


@router.post("/plans/{plan_id}/deviations", status_code=201)
def register_deviation(plan_id: int, payload: DeviationCreate):
    """登记现场偏差；标记未完成时计划挂起。"""
    return service().register_deviation(plan_id, payload.model_dump())


@router.post("/plans/{plan_id}/reopen")
def reopen_plan(plan_id: int, payload: PlanConfirm):
    """重新打开挂起计划，恢复未完成事项。"""
    return service().reopen_plan(plan_id, payload.actor)
