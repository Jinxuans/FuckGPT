from __future__ import annotations

import time

from application.workflow_registry import register_step_adapter
from application.workflows import run_due_workflow_once
from domain.workflows import StepAdapter, StepTransition


class _ApiImmediateAdapter(StepAdapter):
    key = "test.api.immediate"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition.succeeded({"inputs": inputs, "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _ApiNeedsInputAdapter(StepAdapter):
    key = "test.api.needs_input"

    def start(self, *, inputs, idempotency_key, attempt):
        if not inputs.get("target"):
            return StepTransition.needs_attention("缺少 target", code="target_missing")
        return StepTransition.succeeded({"target": inputs["target"], "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


def _wait_run_status(client, run_id: str, expected: str, *, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        run_due_workflow_once()
        response = client.get(f"/api/workflows/runs/{run_id}")
        last = response.json()
        if last.get("status") == expected:
            return last
        time.sleep(0.05)
    return last


def test_workflow_definition_and_run_api(client):
    register_step_adapter(_ApiImmediateAdapter())
    response = client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_run",
                "version": 1,
                "name": "API run",
                "steps": [
                    {
                        "id": "first",
                        "uses": "test.api.immediate",
                        "input": {"value": {"$path": "workflow.inputs.value"}},
                    }
                ],
            }
        },
    )
    assert response.status_code == 200

    listing = client.get("/api/workflows/definitions")
    assert listing.status_code == 200
    assert any(item["key"] == "test_api_run" for item in listing.json()["items"])

    created = client.post(
        "/api/workflows/runs",
        json={"definition_key": "test_api_run", "input": {"value": "ok"}},
    )
    assert created.status_code == 200
    run_id = created.json()["id"]

    detail = _wait_run_status(client, run_id, "succeeded")
    assert detail["status"] == "succeeded"
    assert detail["steps"][0]["output"]["inputs"]["value"] == "ok"

    events = client.get(f"/api/workflows/runs/{run_id}/events")
    assert events.status_code == 200
    assert events.json()["items"]


def test_workflow_step_input_patch_and_retry_api(client):
    register_step_adapter(_ApiNeedsInputAdapter())
    client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_attention",
                "version": 1,
                "name": "API attention",
                "steps": [{"id": "needs", "uses": "test.api.needs_input", "input": {}}],
            }
        },
    )
    created = client.post(
        "/api/workflows/runs",
        json={"definition_key": "test_api_attention", "input": {}},
    )
    run_id = created.json()["id"]
    assert _wait_run_status(client, run_id, "needs_attention")["status"] == "needs_attention"

    patched = client.patch(
        f"/api/workflows/runs/{run_id}/steps/needs/input",
        json={"input": {"target": "nvtokens"}},
    )
    assert patched.status_code == 200
    retried = client.post(f"/api/workflows/runs/{run_id}/steps/needs/retry")
    assert retried.status_code == 200

    detail = _wait_run_status(client, run_id, "succeeded")
    assert detail["status"] == "succeeded"
    assert detail["steps"][0]["output"]["target"] == "nvtokens"
