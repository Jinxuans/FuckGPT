from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from application.workflow_registry import (
    get_step_adapter,
    list_step_adapters,
    registered_workflow_definitions,
)
from core.datetime_utils import ensure_utc_datetime, format_local_clock, serialize_datetime
from core.db import (
    WorkflowDefinitionModel,
    WorkflowEventModel,
    WorkflowRunModel,
    WorkflowStepRunModel,
    engine,
)
from domain.workflows import (
    RUN_CANCEL_REQUESTED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_NEEDS_ATTENTION,
    RUN_PENDING,
    RUN_RETRY_SCHEDULED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_TERMINAL,
    RUN_WAITING_EXTERNAL,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_NEEDS_ATTENTION,
    STEP_PENDING,
    STEP_READY,
    STEP_RETRY_SCHEDULED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    STEP_TERMINAL,
    STEP_WAITING_EXTERNAL,
    StepTransition,
    evaluate_condition,
    parse_duration_seconds,
    resolve_value,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _load_json(value: str) -> dict[str, Any]:
    data = json.loads(value or "{}")
    return data if isinstance(data, dict) else {}


def _due(value: datetime | str | None, now: datetime) -> bool:
    normalized = ensure_utc_datetime(value)
    return normalized is None or normalized <= now


def _event_line(event: WorkflowEventModel) -> str:
    prefix = f"[{format_local_clock(event.created_at)}]"
    step = f"[{event.step_id}]" if event.step_id else ""
    return f"{prefix}{step} {event.message}"


def _serialize_event(event: WorkflowEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "workflow_run_id": event.workflow_run_id,
        "step_id": event.step_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": _event_line(event),
        "detail": event.get_detail(),
        "created_at": serialize_datetime(event.created_at),
    }


def _serialize_definition(model: WorkflowDefinitionModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "key": model.key,
        "version": model.version,
        "name": model.name,
        "description": model.description,
        "enabled": bool(model.enabled),
        "definition": model.get_definition(),
        "created_at": serialize_datetime(model.created_at),
        "updated_at": serialize_datetime(model.updated_at),
    }


def _serialize_step(step: WorkflowStepRunModel) -> dict[str, Any]:
    return {
        "id": step.id,
        "workflow_run_id": step.workflow_run_id,
        "step_id": step.step_id,
        "name": step.name,
        "adapter_key": step.adapter_key,
        "status": step.status,
        "attempt": int(step.attempt or 0),
        "max_attempts": int(step.max_attempts or 1),
        "input": step.get_input(),
        "output": step.get_output(),
        "error": step.get_error(),
        "external_ref": step.external_ref,
        "idempotency_key": step.idempotency_key,
        "next_run_at": serialize_datetime(step.next_run_at),
        "timeout_at": serialize_datetime(step.timeout_at),
        "started_at": serialize_datetime(step.started_at),
        "finished_at": serialize_datetime(step.finished_at),
        "created_at": serialize_datetime(step.created_at),
        "updated_at": serialize_datetime(step.updated_at),
    }


def _serialize_run(
    run: WorkflowRunModel,
    *,
    steps: list[WorkflowStepRunModel] | None = None,
    include_definition: bool = True,
) -> dict[str, Any]:
    data = {
        "id": run.id,
        "definition_key": run.definition_key,
        "definition_version": int(run.definition_version or 1),
        "name": run.name,
        "status": run.status,
        "terminal": run.status in RUN_TERMINAL,
        "input": run.get_input(),
        "context": run.get_context(),
        "output": run.get_output(),
        "current_step_id": run.current_step_id,
        "error": run.error,
        "cancellation_requested_at": serialize_datetime(run.cancellation_requested_at),
        "started_at": serialize_datetime(run.started_at),
        "finished_at": serialize_datetime(run.finished_at),
        "created_at": serialize_datetime(run.created_at),
        "updated_at": serialize_datetime(run.updated_at),
    }
    if include_definition:
        data["definition"] = run.get_definition()
    if steps is not None:
        data["steps"] = [_serialize_step(item) for item in steps]
    return data


def _append_event_in_session(
    session: Session,
    run_id: str,
    message: str,
    *,
    step_id: str = "",
    event_type: str = "log",
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> None:
    event = WorkflowEventModel(
        workflow_run_id=run_id,
        step_id=step_id,
        type=event_type,
        level=level,
        message=message,
        detail_json=_dump_json(detail or {}),
    )
    session.add(event)


def _steps_for_run(session: Session, run_id: str) -> list[WorkflowStepRunModel]:
    return session.exec(
        select(WorkflowStepRunModel).where(WorkflowStepRunModel.workflow_run_id == run_id)
    ).all()


def _step_defs(definition: dict[str, Any]) -> list[dict[str, Any]]:
    steps = definition.get("steps") if isinstance(definition.get("steps"), list) else []
    return [item for item in steps if isinstance(item, dict)]


def _step_def_map(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in _step_defs(definition)}


def _step_order(definition: dict[str, Any]) -> dict[str, int]:
    return {str(item.get("id") or ""): index for index, item in enumerate(_step_defs(definition))}


def _build_context(run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    context = run.get_context()
    if not isinstance(context, dict):
        context = {}
    workflow_context = context.get("workflow") if isinstance(context.get("workflow"), dict) else {}
    workflow_context["inputs"] = run.get_input()
    context["workflow"] = workflow_context
    context["steps"] = {
        step.step_id: {
            "status": step.status,
            "input": step.get_input(),
            "output": step.get_output(),
            "error": step.get_error(),
            "external_ref": step.external_ref,
            "attempt": int(step.attempt or 0),
        }
        for step in steps
    }
    return context


def _run_output_from_steps(steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    return {
        "steps": {
            step.step_id: {
                "status": step.status,
                "output": step.get_output(),
                "external_ref": step.external_ref,
            }
            for step in steps
        }
    }


def validate_workflow_definition(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("key") or "").strip()
    version = max(int(definition.get("version") or 1), 1)
    if not key:
        raise ValueError("工作流定义 key 不能为空")
    steps = _step_defs(definition)
    if not steps:
        raise ValueError("工作流至少需要一个步骤")

    known_adapters = set(list_step_adapters())
    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in steps:
        step = dict(raw)
        step_id = str(step.get("id") or "").strip()
        adapter_key = str(step.get("uses") or "").strip()
        if not step_id:
            raise ValueError("工作流步骤 id 不能为空")
        if step_id in seen:
            raise ValueError(f"工作流步骤 id 重复: {step_id}")
        if not adapter_key:
            raise ValueError(f"步骤 {step_id} 缺少 uses")
        if adapter_key not in known_adapters:
            raise ValueError(f"步骤 {step_id} 使用了未注册 adapter: {adapter_key}")
        needs = [str(item).strip() for item in step.get("needs") or [] if str(item).strip()]
        step.update(
            {
                "id": step_id,
                "name": str(step.get("name") or step_id),
                "uses": adapter_key,
                "needs": needs,
                "input": step.get("input") if isinstance(step.get("input"), dict) else {},
                "max_attempts": max(int(step.get("max_attempts") or 1), 1),
                "retry_delay": step.get("retry_delay", "30s"),
                "timeout": step.get("timeout", ""),
            }
        )
        if "if" in step and not isinstance(step.get("if"), (dict, bool)):
            raise ValueError(f"步骤 {step_id} 的 if 条件必须是对象或布尔值")
        parse_duration_seconds(step.get("retry_delay"), default=30)
        parse_duration_seconds(step.get("timeout"), default=0)
        normalized_steps.append(step)
        seen.add(step_id)

    for step in normalized_steps:
        for need in step.get("needs") or []:
            if need not in seen:
                raise ValueError(f"步骤 {step['id']} 依赖不存在: {need}")

    graph = {step["id"]: list(step.get("needs") or []) for step in normalized_steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"工作流存在循环依赖: {node}")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)

    return {
        **definition,
        "key": key,
        "version": version,
        "name": str(definition.get("name") or key),
        "description": str(definition.get("description") or ""),
        "enabled": bool(definition.get("enabled", True)),
        "steps": normalized_steps,
    }


def sync_registered_workflow_definitions() -> None:
    with Session(engine) as session:
        for raw in registered_workflow_definitions():
            definition = validate_workflow_definition(raw)
            model = session.exec(
                select(WorkflowDefinitionModel)
                .where(WorkflowDefinitionModel.key == definition["key"])
                .where(WorkflowDefinitionModel.version == int(definition["version"]))
            ).first()
            if not model:
                model = WorkflowDefinitionModel(
                    key=definition["key"],
                    version=int(definition["version"]),
                    name=definition["name"],
                    description=definition.get("description", ""),
                    enabled=bool(definition.get("enabled", True)),
                )
            model.name = definition["name"]
            model.description = str(definition.get("description") or "")
            model.enabled = bool(definition.get("enabled", True))
            model.definition_json = _dump_json(definition)
            model.updated_at = _utcnow()
            session.add(model)
        session.commit()


def list_workflow_definitions(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    with Session(engine) as session:
        query = select(WorkflowDefinitionModel)
        if not include_disabled:
            query = query.where(WorkflowDefinitionModel.enabled == True)  # noqa: E712
        items = session.exec(query.order_by(WorkflowDefinitionModel.key, WorkflowDefinitionModel.version)).all()
        return [_serialize_definition(item) for item in items]


def create_or_update_workflow_definition(definition: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_workflow_definition(definition)
    with Session(engine) as session:
        model = session.exec(
            select(WorkflowDefinitionModel)
            .where(WorkflowDefinitionModel.key == normalized["key"])
            .where(WorkflowDefinitionModel.version == int(normalized["version"]))
        ).first()
        if not model:
            model = WorkflowDefinitionModel(
                key=normalized["key"],
                version=int(normalized["version"]),
                name=normalized["name"],
            )
        model.name = normalized["name"]
        model.description = str(normalized.get("description") or "")
        model.enabled = bool(normalized.get("enabled", True))
        model.definition_json = _dump_json(normalized)
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()
        session.refresh(model)
        return _serialize_definition(model)


def _get_definition_model(
    session: Session,
    definition_key: str,
    *,
    version: int = 0,
    require_enabled: bool = True,
) -> WorkflowDefinitionModel | None:
    query = select(WorkflowDefinitionModel).where(WorkflowDefinitionModel.key == definition_key)
    if require_enabled:
        query = query.where(WorkflowDefinitionModel.enabled == True)  # noqa: E712
    if version > 0:
        return session.exec(query.where(WorkflowDefinitionModel.version == version)).first()
    return session.exec(query.order_by(WorkflowDefinitionModel.version.desc())).first()


def _set_run_context(session: Session, run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> None:
    run.context_json = _dump_json(_build_context(run, steps))
    session.add(run)


def _mark_step_skipped(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
    *,
    message: str,
) -> None:
    now = _utcnow()
    step.status = STEP_SKIPPED
    step.output_json = _dump_json({"reason": "condition_false"})
    step.error_json = "{}"
    step.next_run_at = None
    step.timeout_at = None
    step.finished_at = now
    step.updated_at = now
    session.add(step)
    _append_event_in_session(session, run.id, message, step_id=step.step_id, event_type="state")


def _promote_ready_steps(session: Session, run: WorkflowRunModel) -> bool:
    changed = False
    definition = run.get_definition()
    step_defs = _step_def_map(definition)
    while True:
        loop_changed = False
        steps = _steps_for_run(session, run.id)
        by_id = {step.step_id: step for step in steps}
        context = _build_context(run, steps)

        for step in steps:
            if step.status != STEP_PENDING:
                continue
            spec = step_defs.get(step.step_id) or {}
            needs = [str(item) for item in spec.get("needs") or []]
            dependency_steps = [by_id.get(item) for item in needs]
            if any(item is None or item.status not in STEP_TERMINAL for item in dependency_steps):
                continue
            if any(item and item.status in {STEP_FAILED, STEP_CANCELLED} for item in dependency_steps):
                continue
            try:
                should_run = evaluate_condition(spec.get("if"), context)
            except Exception as exc:
                step.status = STEP_NEEDS_ATTENTION
                step.error_json = _dump_json({"code": "condition_invalid", "message": str(exc)})
                step.updated_at = _utcnow()
                session.add(step)
                _append_event_in_session(
                    session,
                    run.id,
                    f"步骤条件无法判断: {exc}",
                    step_id=step.step_id,
                    event_type="state",
                    level="warning",
                )
                loop_changed = True
                continue
            if not should_run:
                _mark_step_skipped(session, run, step, message="步骤条件未满足，已跳过")
                loop_changed = True
                continue
            step.input_json = _dump_json(resolve_value(spec.get("input") or {}, context))
            step.status = STEP_READY
            step.next_run_at = _utcnow()
            step.updated_at = _utcnow()
            session.add(step)
            _append_event_in_session(session, run.id, "步骤已就绪", step_id=step.step_id, event_type="state")
            loop_changed = True

        if not loop_changed:
            break
        changed = True
    steps = _steps_for_run(session, run.id)
    _set_run_context(session, run, steps)
    return changed


def _finish_run(
    session: Session,
    run: WorkflowRunModel,
    *,
    status: str,
    error: str = "",
    steps: list[WorkflowStepRunModel] | None = None,
) -> None:
    now = _utcnow()
    run.status = status
    run.error = error
    run.finished_at = now
    run.updated_at = now
    if steps is None:
        steps = _steps_for_run(session, run.id)
    run.output_json = _dump_json(_run_output_from_steps(steps))
    _set_run_context(session, run, steps)
    session.add(run)


def _refresh_run_status(session: Session, run: WorkflowRunModel) -> None:
    steps = _steps_for_run(session, run.id)
    statuses = [step.status for step in steps]
    now = _utcnow()

    if run.cancellation_requested_at:
        for step in steps:
            if step.status == STEP_RUNNING:
                run.status = RUN_CANCEL_REQUESTED
                run.updated_at = now
                _set_run_context(session, run, steps)
                return
            if step.status not in STEP_TERMINAL:
                step.status = STEP_CANCELLED
                step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
                step.next_run_at = None
                step.timeout_at = None
                step.finished_at = now
                step.updated_at = now
                session.add(step)
        _finish_run(session, run, status=RUN_CANCELLED, error="工作流已取消", steps=steps)
        return

    failed = next((step for step in steps if step.status == STEP_FAILED), None)
    if failed:
        error = str((failed.get_error() or {}).get("message") or "工作流步骤失败")
        for step in steps:
            if step.status not in STEP_TERMINAL:
                step.status = STEP_CANCELLED
                step.error_json = _dump_json({"code": "dependency_failed", "message": "依赖步骤失败"})
                step.next_run_at = None
                step.timeout_at = None
                step.finished_at = now
                step.updated_at = now
                session.add(step)
        _finish_run(session, run, status=RUN_FAILED, error=error, steps=steps)
        return

    if all(status in {STEP_SUCCEEDED, STEP_SKIPPED} for status in statuses):
        _finish_run(session, run, status=RUN_SUCCEEDED, steps=steps)
        return

    if any(status == STEP_NEEDS_ATTENTION for status in statuses):
        run.status = RUN_NEEDS_ATTENTION
    elif any(status == STEP_RUNNING or status == STEP_READY for status in statuses):
        run.status = RUN_RUNNING
    elif any(status == STEP_RETRY_SCHEDULED for status in statuses):
        run.status = RUN_RETRY_SCHEDULED
    elif any(status == STEP_WAITING_EXTERNAL for status in statuses):
        run.status = RUN_WAITING_EXTERNAL
    else:
        run.status = RUN_PENDING
    run.started_at = run.started_at or now
    run.updated_at = now
    _set_run_context(session, run, steps)
    session.add(run)


def create_workflow_run(
    *,
    definition_key: str,
    inputs: dict[str, Any] | None = None,
    version: int = 0,
    name: str = "",
) -> dict[str, Any] | None:
    with Session(engine) as session:
        definition_model = _get_definition_model(session, definition_key, version=version)
        if not definition_model:
            return None
        definition = definition_model.get_definition()
        run_id = f"wf_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        run = WorkflowRunModel(
            id=run_id,
            definition_key=definition_model.key,
            definition_version=int(definition_model.version or 1),
            name=str(name or definition_model.name),
            status=RUN_PENDING,
            input_json=_dump_json(inputs or {}),
            context_json=_dump_json({"workflow": {"inputs": inputs or {}}, "steps": {}}),
            output_json="{}",
            definition_json=_dump_json(definition),
            started_at=_utcnow(),
        )
        session.add(run)
        session.flush()
        for spec in _step_defs(definition):
            step = WorkflowStepRunModel(
                id=f"{run_id}_{spec['id']}",
                workflow_run_id=run_id,
                step_id=spec["id"],
                name=str(spec.get("name") or spec["id"]),
                adapter_key=str(spec.get("uses") or ""),
                status=STEP_PENDING,
                max_attempts=max(int(spec.get("max_attempts") or 1), 1),
                idempotency_key=f"{run_id}_{spec['id']}",
            )
            session.add(step)
        session.flush()
        _append_event_in_session(
            session,
            run.id,
            f"工作流已创建: {run.name}",
            event_type="state",
            detail={"definition_key": run.definition_key, "definition_version": run.definition_version},
        )
        _promote_ready_steps(session, run)
        _refresh_run_status(session, run)
        session.commit()
        steps = sorted(_steps_for_run(session, run.id), key=lambda item: _step_order(definition).get(item.step_id, 0))
        session.refresh(run)
        return _serialize_run(run, steps=steps)


def list_workflow_runs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    definition_key: str = "",
) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 1), 100)
    offset = max(int(offset or 0), 0)
    with Session(engine) as session:
        query = select(WorkflowRunModel)
        count_query = select(func.count()).select_from(WorkflowRunModel)
        if status:
            query = query.where(WorkflowRunModel.status == status)
            count_query = count_query.where(WorkflowRunModel.status == status)
        if definition_key:
            query = query.where(WorkflowRunModel.definition_key == definition_key)
            count_query = count_query.where(WorkflowRunModel.definition_key == definition_key)
        items = session.exec(
            query.order_by(WorkflowRunModel.created_at.desc(), WorkflowRunModel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(session.exec(count_query).one() or 0)
        running = int(
            session.exec(
                select(func.count())
                .select_from(WorkflowRunModel)
                .where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
            ).one()
            or 0
        )
        return {
            "items": [_serialize_run(item, include_definition=False) for item in items],
            "total": total,
            "running": running,
            "limit": limit,
            "offset": offset,
        }


def get_workflow_run(run_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return None
        definition = run.get_definition()
        order = _step_order(definition)
        steps = sorted(_steps_for_run(session, run.id), key=lambda item: order.get(item.step_id, 0))
        return _serialize_run(run, steps=steps)


def list_workflow_events(
    run_id: str,
    *,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
) -> dict[str, Any] | None:
    limit = min(max(int(limit or 200), 1), 1000)
    since = max(int(since or 0), 0)
    before = max(int(before or 0), 0)
    with Session(engine) as session:
        if not session.get(WorkflowRunModel, run_id):
            return None
        if before:
            query = (
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .where(WorkflowEventModel.id < before)
                .order_by(WorkflowEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(query).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        elif latest:
            query = (
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .order_by(WorkflowEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(query).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        else:
            items = session.exec(
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .where(WorkflowEventModel.id > since)
                .order_by(WorkflowEventModel.id)
                .limit(limit)
            ).all()
            has_more_before = False
        serialized = [_serialize_event(item) for item in items]
        return {
            "items": serialized,
            "cursor": max([int(item["id"] or 0) for item in serialized] or [since]),
            "before": min([int(item["id"] or 0) for item in serialized] or [before]),
            "has_more_before": has_more_before,
        }


def _claim_next_due_step() -> dict[str, Any] | None:
    now = _utcnow()
    with Session(engine) as session:
        active_runs = session.exec(
            select(WorkflowRunModel).where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
        ).all()
        for run in active_runs:
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        session.commit()

    with Session(engine) as session:
        candidates = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.status.in_([STEP_READY, STEP_RETRY_SCHEDULED, STEP_WAITING_EXTERNAL]))
            .order_by(WorkflowStepRunModel.next_run_at, WorkflowStepRunModel.created_at)
        ).all()
        for step in candidates:
            if not _due(step.next_run_at, now):
                continue
            run = session.get(WorkflowRunModel, step.workflow_run_id)
            if not run or run.status in RUN_TERMINAL:
                continue
            if run.cancellation_requested_at:
                _refresh_run_status(session, run)
                session.commit()
                return {"cancelled": True}
            if step.timeout_at and ensure_utc_datetime(step.timeout_at) and ensure_utc_datetime(step.timeout_at) <= now:
                transition = StepTransition.failed("步骤执行超时", code="step_timeout", retryable=True)
                _apply_step_transition_in_session(session, run, step, transition)
                session.commit()
                return {"timed_out": True}

            previous_status = step.status
            external_ref = step.external_ref
            if previous_status in {STEP_READY, STEP_RETRY_SCHEDULED}:
                if previous_status == STEP_RETRY_SCHEDULED or int(step.attempt or 0) <= 0:
                    step.attempt = int(step.attempt or 0) + 1
                if previous_status == STEP_RETRY_SCHEDULED:
                    external_ref = ""
                    step.external_ref = ""
                if not _load_json(step.input_json):
                    definition = run.get_definition()
                    spec = _step_def_map(definition).get(step.step_id) or {}
                    context = _build_context(run, _steps_for_run(session, run.id))
                    step.input_json = _dump_json(resolve_value(spec.get("input") or {}, context))
                timeout_seconds = parse_duration_seconds(
                    (_step_def_map(run.get_definition()).get(step.step_id) or {}).get("timeout"),
                    default=0,
                )
                step.timeout_at = now + timedelta(seconds=timeout_seconds) if timeout_seconds else None
            step.status = STEP_RUNNING
            step.next_run_at = None
            step.started_at = step.started_at or now
            step.updated_at = now
            run.status = RUN_RUNNING
            run.current_step_id = step.step_id
            run.started_at = run.started_at or now
            run.finished_at = None
            run.updated_at = now
            session.add(step)
            session.add(run)
            session.commit()
            return {
                "run_id": run.id,
                "step_id": step.step_id,
                "adapter_key": step.adapter_key,
                "input": step.get_input(),
                "external_ref": external_ref,
                "idempotency_key": step.idempotency_key,
                "attempt": int(step.attempt or 1),
            }
    return None


def _apply_step_transition_in_session(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
    transition: StepTransition,
) -> None:
    now = _utcnow()
    previous_status = step.status
    message = transition.message
    if run.cancellation_requested_at:
        step.status = STEP_CANCELLED
        step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
        step.next_run_at = None
        step.timeout_at = None
        step.finished_at = now
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            "步骤已取消",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_WAITING_EXTERNAL:
        step.status = STEP_WAITING_EXTERNAL
        step.output_json = _dump_json(transition.output)
        step.error_json = "{}"
        step.external_ref = transition.external_ref or step.external_ref
        step.next_run_at = transition.next_run_at or (now + timedelta(seconds=1))
        step.updated_at = now
        session.add(step)
        if previous_status != STEP_WAITING_EXTERNAL:
            _append_event_in_session(
                session,
                run.id,
                message or "步骤等待外部结果",
                step_id=step.step_id,
                event_type="state",
                detail={"external_ref": step.external_ref},
            )
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_FAILED:
        step.error_json = _dump_json(transition.error)
        step.output_json = _dump_json(transition.output)
        step.next_run_at = None
        step.timeout_at = None
        step.updated_at = now
        if transition.retryable and int(step.attempt or 0) < int(step.max_attempts or 1):
            delay_seconds = parse_duration_seconds(
                (_step_def_map(run.get_definition()).get(step.step_id) or {}).get("retry_delay"),
                default=30,
            )
            step.status = STEP_RETRY_SCHEDULED
            step.next_run_at = now + timedelta(seconds=delay_seconds)
            _append_event_in_session(
                session,
                run.id,
                message or "步骤失败，将自动重试",
                step_id=step.step_id,
                event_type="state",
                level="warning",
                detail={"retry_at": serialize_datetime(step.next_run_at), "attempt": step.attempt},
            )
        else:
            step.status = STEP_FAILED
            step.finished_at = now
            _append_event_in_session(
                session,
                run.id,
                message or "步骤失败",
                step_id=step.step_id,
                event_type="state",
                level="error",
                detail=transition.error,
            )
        session.add(step)
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_NEEDS_ATTENTION:
        step.status = STEP_NEEDS_ATTENTION
        step.error_json = _dump_json(transition.error)
        step.output_json = _dump_json(transition.output)
        step.next_run_at = None
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            message or "步骤需要人工处理",
            step_id=step.step_id,
            event_type="state",
            level="warning",
            detail=transition.error,
        )
        _refresh_run_status(session, run)
        return

    if transition.status in {STEP_SUCCEEDED, STEP_SKIPPED, STEP_CANCELLED}:
        step.status = transition.status
        step.output_json = _dump_json(transition.output)
        step.error_json = _dump_json(transition.error)
        step.external_ref = transition.external_ref or step.external_ref
        step.next_run_at = None
        step.timeout_at = None
        step.finished_at = now
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            message or ("步骤已跳过" if transition.status == STEP_SKIPPED else "步骤已完成"),
            step_id=step.step_id,
            event_type="state",
            level="warning" if transition.status == STEP_CANCELLED else "info",
            detail=transition.output,
        )
        _promote_ready_steps(session, run)
        _refresh_run_status(session, run)
        return

    step.status = STEP_FAILED
    step.error_json = _dump_json({"code": "invalid_transition", "message": f"未知步骤状态: {transition.status}"})
    step.finished_at = now
    step.updated_at = now
    session.add(step)
    _refresh_run_status(session, run)


def apply_step_transition(run_id: str, step_id: str, transition: StepTransition) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        _apply_step_transition_in_session(session, run, step, transition)
        session.commit()
        session.refresh(run)
        return get_workflow_run(run.id)


def run_due_workflow_once() -> bool:
    claim = _claim_next_due_step()
    if not claim:
        return False
    if claim.get("cancelled") or claim.get("timed_out"):
        return True

    try:
        adapter = get_step_adapter(str(claim["adapter_key"]))
        if claim.get("external_ref"):
            transition = adapter.resume(
                inputs=dict(claim.get("input") or {}),
                external_ref=str(claim.get("external_ref") or ""),
                attempt=int(claim.get("attempt") or 1),
            )
        else:
            transition = adapter.start(
                inputs=dict(claim.get("input") or {}),
                idempotency_key=str(claim.get("idempotency_key") or ""),
                attempt=int(claim.get("attempt") or 1),
            )
    except Exception as exc:  # noqa: BLE001 - adapter failures become workflow state.
        transition = StepTransition.failed(str(exc), code="adapter_exception")
    apply_step_transition(str(claim["run_id"]), str(claim["step_id"]), transition)
    return True


def recover_incomplete_workflow_runs() -> None:
    now = _utcnow()
    with Session(engine) as session:
        runs = session.exec(
            select(WorkflowRunModel).where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
        ).all()
        for run in runs:
            run.finished_at = None
            for step in _steps_for_run(session, run.id):
                if step.status == STEP_RUNNING:
                    step.status = STEP_WAITING_EXTERNAL if step.external_ref else STEP_READY
                    step.next_run_at = now
                    step.updated_at = now
                    session.add(step)
                    _append_event_in_session(
                        session,
                        run.id,
                        "步骤在服务重启后恢复调度",
                        step_id=step.step_id,
                        event_type="state",
                        level="warning",
                    )
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        session.commit()


def cancel_workflow_run(run_id: str) -> dict[str, Any] | None:
    cancel_calls: list[tuple[str, dict[str, Any], str]] = []
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return None
        if run.status in RUN_TERMINAL:
            return _serialize_run(run, steps=_steps_for_run(session, run.id))
        now = _utcnow()
        run.status = RUN_CANCEL_REQUESTED
        run.cancellation_requested_at = run.cancellation_requested_at or now
        run.updated_at = now
        for step in _steps_for_run(session, run.id):
            if step.status == STEP_WAITING_EXTERNAL and step.external_ref:
                cancel_calls.append((step.adapter_key, step.get_input(), step.external_ref))
            if step.status in STEP_TERMINAL or step.status == STEP_RUNNING:
                continue
            step.status = STEP_CANCELLED
            step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
            step.next_run_at = None
            step.timeout_at = None
            step.finished_at = now
            step.updated_at = now
            session.add(step)
        session.add(run)
        _append_event_in_session(session, run.id, "已请求取消工作流", event_type="state", level="warning")
        _refresh_run_status(session, run)
        session.commit()

    for adapter_key, inputs, external_ref in cancel_calls:
        try:
            get_step_adapter(adapter_key).cancel(inputs=inputs, external_ref=external_ref)
        except Exception:
            pass
    return get_workflow_run(run_id)


def _descendant_step_ids(definition: dict[str, Any], step_id: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for spec in _step_defs(definition):
        for need in spec.get("needs") or []:
            children.setdefault(str(need), []).append(str(spec.get("id") or ""))
    result: set[str] = set()
    stack = list(children.get(step_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    return result


def retry_workflow_step(run_id: str, step_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        if step.status not in {STEP_FAILED, STEP_NEEDS_ATTENTION, STEP_CANCELLED}:
            raise ValueError("只有失败、已取消或需人工处理的步骤可以人工重试")
        now = _utcnow()
        descendants = _descendant_step_ids(run.get_definition(), step_id)
        for item in _steps_for_run(session, run.id):
            if item.step_id not in descendants:
                continue
            item.status = STEP_PENDING
            item.attempt = 0
            item.input_json = "{}"
            item.output_json = "{}"
            item.error_json = "{}"
            item.external_ref = ""
            item.next_run_at = None
            item.timeout_at = None
            item.started_at = None
            item.finished_at = None
            item.updated_at = now
            session.add(item)
        step.status = STEP_RETRY_SCHEDULED
        step.max_attempts = max(int(step.max_attempts or 1), int(step.attempt or 0) + 1)
        step.error_json = "{}"
        step.output_json = "{}"
        step.external_ref = ""
        step.next_run_at = now
        step.timeout_at = None
        step.finished_at = None
        step.updated_at = now
        run.status = RUN_RETRY_SCHEDULED
        run.error = ""
        run.finished_at = None
        run.cancellation_requested_at = None
        run.updated_at = now
        session.add(step)
        session.add(run)
        _append_event_in_session(
            session,
            run.id,
            "步骤已安排人工重试",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _refresh_run_status(session, run)
        session.commit()
        return get_workflow_run(run.id)


def update_workflow_step_input(run_id: str, step_id: str, inputs: dict[str, Any]) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        if step.status in {STEP_RUNNING, STEP_WAITING_EXTERNAL}:
            raise ValueError("运行中或等待外部结果的步骤不能直接修改输入")
        step.input_json = _dump_json(inputs)
        step.updated_at = _utcnow()
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            "步骤输入已更新",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _set_run_context(session, run, _steps_for_run(session, run.id))
        session.commit()
        return get_workflow_run(run.id)
