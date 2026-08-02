from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.workflows import (
    cancel_workflow_run,
    create_or_update_workflow_definition,
    create_workflow_run,
    get_workflow_run,
    list_workflow_definitions,
    list_workflow_events,
    list_workflow_runs,
    retry_workflow_step,
    update_workflow_step_input,
)
from services.workflow_runtime import workflow_runtime

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowDefinitionRequest(BaseModel):
    definition: dict = Field(default_factory=dict)


class WorkflowRunRequest(BaseModel):
    definition_key: str
    version: int = 0
    name: str = ""
    input: dict = Field(default_factory=dict)


class WorkflowStepInputRequest(BaseModel):
    input: dict = Field(default_factory=dict)


@router.get("/definitions")
def list_definitions(include_disabled: bool = False):
    return {"items": list_workflow_definitions(include_disabled=include_disabled)}


@router.post("/definitions")
def upsert_definition(body: WorkflowDefinitionRequest):
    try:
        return create_or_update_workflow_definition(body.definition)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs")
def list_runs(limit: int = 50, offset: int = 0, status: str = "", definition_key: str = ""):
    return list_workflow_runs(limit=limit, offset=offset, status=status, definition_key=definition_key)


@router.post("/runs")
def start_run(body: WorkflowRunRequest):
    run = create_workflow_run(
        definition_key=body.definition_key,
        version=body.version,
        name=body.name,
        inputs=body.input,
    )
    if not run:
        raise HTTPException(404, "工作流定义不存在或未启用")
    workflow_runtime.wake_up()
    return run


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    run = get_workflow_run(run_id)
    if not run:
        raise HTTPException(404, "工作流不存在")
    return run


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
):
    events = list_workflow_events(run_id, since=since, before=before, limit=limit, latest=latest)
    if events is None:
        raise HTTPException(404, "工作流不存在")
    return events


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run = cancel_workflow_run(run_id)
    if not run:
        raise HTTPException(404, "工作流不存在")
    workflow_runtime.wake_up()
    return run


@router.post("/runs/{run_id}/steps/{step_id}/retry")
def retry_step(run_id: str, step_id: str):
    try:
        run = retry_workflow_step(run_id, step_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not run:
        raise HTTPException(404, "工作流或步骤不存在")
    workflow_runtime.wake_up()
    return run


@router.patch("/runs/{run_id}/steps/{step_id}/input")
def update_step_input(run_id: str, step_id: str, body: WorkflowStepInputRequest):
    try:
        run = update_workflow_step_input(run_id, step_id, body.input)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not run:
        raise HTTPException(404, "工作流或步骤不存在")
    return run
