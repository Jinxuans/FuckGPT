import threading

from application.tasks import (
    TASK_TYPE_ACCOUNT_CHECK_ALL,
    TASK_TYPE_ACCOUNT_PUSH,
    TASK_TYPE_CODEX_OAUTH_BATCH,
    TASK_TYPE_PLATFORM_ACTION,
    TASK_TYPE_REGISTER,
    TASK_STATUS_INTERRUPTED,
    TaskLogger,
    claim_next_runnable_task,
    create_task,
    get_task,
    request_cancel,
    _mutate_task,
)
from services.task_runtime import TaskRuntime, TaskWorkerState


def test_claim_allows_different_task_types_on_same_platform():
    first_register = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    blocked_register = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    account_check = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="chatgpt",
        payload={"limit": 1},
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first_register["id"]

    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        max_parallel_per_scope=1,
    )

    assert next_claimed["id"] == account_check["id"]
    assert next_claimed["id"] != blocked_register["id"]
    assert next_claimed["scope"] == "chatgpt:account_check_all"


def test_claim_allows_codex_oauth_actions_for_different_accounts():
    first = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 101, "action_id": "codex_oauth_authorize"},
        progress_total=1,
    )
    second = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 102, "action_id": "codex_oauth_authorize"},
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first["id"]
    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        busy_account_keys=set(claimed["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert next_claimed["id"] == second["id"]
    assert next_claimed["scope"] != claimed["scope"]


def test_codex_oauth_auto_push_can_start_before_batch_finishes():
    batch = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [101, 102]},
        progress_total=2,
    )
    push = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        },
        progress_total=1,
    )
    second_push = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [102],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        },
        progress_total=1,
    )

    claimed_batch = claim_next_runnable_task()
    assert claimed_batch["id"] == batch["id"]
    assert "account:101" in claimed_batch["account_keys"]

    claimed_push = claim_next_runnable_task(
        running_scope_counts={claimed_batch["scope"]: 1},
        busy_account_keys=set(claimed_batch["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert claimed_push["id"] == push["id"]
    assert claimed_push["account_keys"] == []
    assert claimed_push["scope"] == "chatgpt:account_push:nvtokens:101"

    second_claimed_push = claim_next_runnable_task(
        running_scope_counts={
            claimed_batch["scope"]: 1,
            claimed_push["scope"]: 1,
        },
        busy_account_keys=set(claimed_batch["account_keys"]),
        max_parallel_per_scope=1,
    )
    assert second_claimed_push["id"] == second_push["id"]
    assert second_claimed_push["scope"] == "chatgpt:account_push:nvtokens:102"


def test_cancel_requested_worker_does_not_block_runtime_slots():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    claimed = claim_next_runnable_task()
    request_cancel(task["id"])

    runtime = TaskRuntime(max_parallel_tasks=1, max_parallel_per_scope=1)
    runtime._workers[task["id"]] = TaskWorkerState(
        thread=threading.Thread(),
        platform="chatgpt",
        task_type=TASK_TYPE_REGISTER,
        scope=claimed["scope"],
    )

    available_slots, running_scope_counts, busy_account_keys = runtime._accounting_snapshot()

    assert available_slots == 1
    assert running_scope_counts == {}
    assert busy_account_keys == set()


def test_interrupted_task_is_seen_as_stop_requested():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_INTERRUPTED))

    assert TaskLogger(task["id"]).is_cancel_requested() is True


def test_terminal_task_is_not_overwritten_by_late_worker_finish():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_INTERRUPTED))

    TaskLogger(task["id"]).finish("succeeded")

    assert get_task(task["id"])["status"] == TASK_STATUS_INTERRUPTED
