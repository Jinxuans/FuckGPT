from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from application.tasks import TASK_STATUS_SUCCEEDED, TASK_TYPE_CODEX_OAUTH_BATCH, create_task
from application.workflow_adapters import CodexAuthorizeAdapter
from application.workflow_registry import register_step_adapter
from application.workflows import (
    cancel_workflow_run,
    create_or_update_workflow_definition,
    create_workflow_run,
    get_workflow_run,
    recover_incomplete_workflow_runs,
    retry_workflow_step,
    run_due_workflow_once,
    update_workflow_step_input,
    validate_workflow_definition,
)
from core.db import TaskModel, WorkflowRunModel, WorkflowStepRunModel, engine
from domain.workflows import (
    RUN_CANCELLED,
    RUN_NEEDS_ATTENTION,
    RUN_RETRY_SCHEDULED,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_NEEDS_ATTENTION,
    STEP_READY,
    STEP_RETRY_SCHEDULED,
    STEP_RUNNING,
    STEP_SUCCEEDED,
    STEP_WAITING_EXTERNAL,
    StepAdapter,
    StepTransition,
    evaluate_condition,
    utcnow,
)


class _ImmediateAdapter(StepAdapter):
    key = "test.immediate"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition.succeeded(
            {"inputs": inputs, "attempt": attempt, "idempotency_key": idempotency_key}
        )

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _WaitingAdapter(StepAdapter):
    key = "test.wait"
    cancelled_refs: list[str] = []

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition(
            STEP_WAITING_EXTERNAL,
            external_ref=f"external-{idempotency_key}",
            output={"started": True},
            next_run_at=utcnow(),
            message="等待外部确认",
        )

    def resume(self, *, inputs, external_ref, attempt):
        return StepTransition.succeeded({"external_ref": external_ref, "attempt": attempt})

    def cancel(self, *, inputs, external_ref):
        self.cancelled_refs.append(external_ref)


class _FlakyAdapter(StepAdapter):
    key = "test.flaky"

    def start(self, *, inputs, idempotency_key, attempt):
        if attempt == 1:
            return StepTransition.failed("temporary", code="temporary", retryable=True)
        return StepTransition.succeeded({"attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _AttentionAdapter(StepAdapter):
    key = "test.attention"

    def start(self, *, inputs, idempotency_key, attempt):
        if not inputs.get("value"):
            return StepTransition.needs_attention("缺少 value", code="value_missing")
        return StepTransition.succeeded({"value": inputs["value"], "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


def _register_test_adapters() -> None:
    for adapter in (_ImmediateAdapter(), _WaitingAdapter(), _FlakyAdapter(), _AttentionAdapter()):
        register_step_adapter(adapter)


def _definition(key: str, steps: list[dict]) -> dict:
    return {
        "key": key,
        "version": 1,
        "name": key,
        "steps": steps,
    }


def test_condition_dsl_supports_safe_ops():
    context = {"workflow": {"inputs": {"enabled": True, "mode": "plus", "count": 2}}}

    assert evaluate_condition({"path": "workflow.inputs.enabled", "op": "truthy"}, context)
    assert evaluate_condition({"path": "workflow.inputs.mode", "op": "eq", "value": "plus"}, context)
    assert evaluate_condition({"path": "workflow.inputs.mode", "op": "in", "value": ["plus"]}, context)
    assert evaluate_condition(
        {
            "all": [
                {"path": "workflow.inputs.enabled", "op": "truthy"},
                {"not": {"path": "workflow.inputs.count", "op": "eq", "value": 3}},
            ]
        },
        context,
    )


def test_definition_validation_rejects_cycles_and_unknown_adapters():
    _register_test_adapters()

    with pytest.raises(ValueError, match="循环依赖"):
        validate_workflow_definition(
            _definition(
                "test_cycle",
                [
                    {"id": "a", "uses": "test.immediate", "needs": ["b"]},
                    {"id": "b", "uses": "test.immediate", "needs": ["a"]},
                ],
            )
        )

    with pytest.raises(ValueError, match="未注册 adapter"):
        validate_workflow_definition(
            _definition("test_unknown", [{"id": "a", "uses": "test.missing"}])
        )


def test_run_executes_steps_and_skips_false_condition():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_skip",
            [
                {
                    "id": "first",
                    "uses": "test.immediate",
                    "input": {"value": {"$path": "workflow.inputs.value"}},
                },
                {
                    "id": "second",
                    "uses": "test.immediate",
                    "needs": ["first"],
                    "if": {"path": "workflow.inputs.run_second", "op": "eq", "value": True},
                },
            ],
        )
    )

    run = create_workflow_run(definition_key="test_skip", inputs={"value": 7, "run_second": False})
    assert run is not None
    assert run_due_workflow_once()

    updated = get_workflow_run(run["id"])
    assert updated["status"] == RUN_SUCCEEDED
    assert updated["steps"][0]["status"] == STEP_SUCCEEDED
    assert updated["steps"][1]["status"] == "skipped"
    assert updated["steps"][0]["output"]["inputs"]["value"] == 7


def test_waiting_external_resumes_without_occupying_a_worker():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_waiting", [{"id": "wait", "uses": "test.wait", "timeout": "1m"}])
    )
    run = create_workflow_run(definition_key="test_waiting", inputs={})

    assert run_due_workflow_once()
    waiting = get_workflow_run(run["id"])
    assert waiting["status"] == "waiting_external"
    assert waiting["steps"][0]["status"] == STEP_WAITING_EXTERNAL

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["external_ref"].startswith("external-")


def test_retryable_failure_schedules_and_uses_next_attempt():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_retry",
            [{"id": "flaky", "uses": "test.flaky", "max_attempts": 2, "retry_delay": "0s"}],
        )
    )
    run = create_workflow_run(definition_key="test_retry", inputs={})

    assert run_due_workflow_once()
    retrying = get_workflow_run(run["id"])
    assert retrying["status"] == RUN_RETRY_SCHEDULED
    assert retrying["steps"][0]["status"] == STEP_RETRY_SCHEDULED

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["attempt"] == 2


def test_needs_attention_can_patch_input_and_retry():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_attention",
            [{"id": "attention", "uses": "test.attention", "input": {}}],
        )
    )
    run = create_workflow_run(definition_key="test_attention", inputs={})

    assert run_due_workflow_once()
    attention = get_workflow_run(run["id"])
    assert attention["status"] == RUN_NEEDS_ATTENTION
    assert attention["steps"][0]["status"] == STEP_NEEDS_ATTENTION

    update_workflow_step_input(run["id"], "attention", {"value": 42})
    retry_workflow_step(run["id"], "attention")
    assert run_due_workflow_once()

    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["value"] == 42


def test_cancel_waiting_workflow_cancels_external_ref():
    _register_test_adapters()
    _WaitingAdapter.cancelled_refs.clear()
    create_or_update_workflow_definition(
        _definition("test_cancel", [{"id": "wait", "uses": "test.wait", "timeout": "1m"}])
    )
    run = create_workflow_run(definition_key="test_cancel", inputs={})

    assert run_due_workflow_once()
    cancelled = cancel_workflow_run(run["id"])

    assert cancelled["status"] == RUN_CANCELLED
    assert cancelled["steps"][0]["status"] == "cancelled"
    assert _WaitingAdapter.cancelled_refs


def test_recovery_requeues_running_step_without_incrementing_attempt():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_recover", [{"id": "first", "uses": "test.immediate"}])
    )
    run = create_workflow_run(definition_key="test_recover", inputs={})
    with Session(engine) as session:
        model = session.get(WorkflowRunModel, run["id"])
        step = session.exec(select(WorkflowStepRunModel)).one()
        step.status = STEP_RUNNING
        step.attempt = 1
        step.input_json = "{}"
        step.next_run_at = None
        step.timeout_at = utcnow() + timedelta(minutes=1)
        model.status = "running"
        session.add(model)
        session.add(step)
        session.commit()

    recover_incomplete_workflow_runs()
    recovered = get_workflow_run(run["id"])
    assert recovered["steps"][0]["status"] == STEP_READY

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["attempt"] == 1


def test_deterministic_child_task_id_is_reused():
    first = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [1]},
        task_id="task_wf_same_1",
    )
    second = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [2]},
        task_id="task_wf_same_1",
    )

    assert second["id"] == first["id"]
    with Session(engine) as session:
        assert len(session.exec(select(TaskModel)).all()) == 1
        assert session.exec(select(TaskModel)).one().get_payload()["account_ids"] == [1]


def test_codex_adapter_checks_account_level_result_and_disables_auto_push():
    task = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [9]},
        result_seed={"data": {"accounts": [{"account_id": 9, "ok": False, "error": "oauth failed"}]}},
    )
    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        model.status = TASK_STATUS_SUCCEEDED
        session.add(model)
        session.commit()

    adapter = CodexAuthorizeAdapter()
    transition = adapter.task_succeeded({**task, "status": TASK_STATUS_SUCCEEDED}, inputs={"account_id": 9})
    assert transition.status == STEP_FAILED
    assert transition.error["code"] == "codex_account_failed"

    created = adapter.create_task(inputs={"account_id": 9, "params": {}}, task_id="task_wf_codex_1")
    with Session(engine) as session:
        model = session.get(TaskModel, created["id"])
        assert model.get_payload()["auto_push_after_oauth"] is False
