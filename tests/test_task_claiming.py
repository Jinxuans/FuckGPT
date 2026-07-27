import threading

from application.tasks import (
    TASK_TYPE_ACCOUNT_CHECK_ALL,
    TASK_TYPE_REGISTER,
    claim_next_runnable_task,
    create_task,
    request_cancel,
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
