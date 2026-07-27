"""Persistent task runtime for single-process execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from application.tasks import (
    TASK_STATUS_CANCEL_REQUESTED,
    TERMINAL_TASK_STATUSES,
    claim_next_runnable_task,
    execute_task,
    get_task,
    mark_incomplete_tasks_interrupted,
)


@dataclass(slots=True)
class TaskWorkerState:
    thread: threading.Thread
    platform: str = ""
    task_type: str = ""
    scope: str = ""
    account_keys: set[str] = field(default_factory=set)


class TaskRuntime:
    def __init__(self, *, max_parallel_tasks: int = 3, max_parallel_per_scope: int = 1, poll_interval: float = 0.5):
        self.max_parallel_tasks = max_parallel_tasks
        self.max_parallel_per_scope = max_parallel_per_scope
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher: threading.Thread | None = None
        self._workers: dict[str, TaskWorkerState] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            mark_incomplete_tasks_interrupted()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="task-runtime")
            self._dispatcher.start()
            print("[TaskRuntime] 已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        print("[TaskRuntime] 停止中")

    def wake_up(self) -> None:
        # Polling loop wakes quickly already; this method exists as an explicit runtime hook.
        return

    def _loop(self) -> None:
        while self._running:
            self._reap_workers()
            available_slots, running_scope_counts, busy_account_keys = self._accounting_snapshot()
            while available_slots > 0 and self._running:
                task_info = claim_next_runnable_task(
                    running_scope_counts=running_scope_counts,
                    busy_account_keys=busy_account_keys,
                    max_parallel_per_scope=self.max_parallel_per_scope,
                )
                if not task_info:
                    break
                task_id = task_info["id"]
                worker = threading.Thread(
                    target=self._run_task,
                    args=(task_id,),
                    daemon=True,
                    name=f"task-worker-{task_id}",
                )
                with self._lock:
                    self._workers[task_id] = TaskWorkerState(
                        thread=worker,
                        platform=str(task_info.get("platform", "") or ""),
                        task_type=str(task_info.get("type", "") or ""),
                        scope=str(task_info.get("scope", "") or ""),
                        account_keys=set(task_info.get("account_keys") or []),
                    )
                    if task_info.get("scope"):
                        scope = str(task_info["scope"])
                        running_scope_counts[scope] = running_scope_counts.get(scope, 0) + 1
                    busy_account_keys.update(set(task_info.get("account_keys") or []))
                worker.start()
                available_slots -= 1
            time.sleep(self.poll_interval)
        self._reap_workers()

    def _accounting_snapshot(self) -> tuple[int, dict[str, int], set[str]]:
        with self._lock:
            workers = list(self._workers.items())

        accounted = 0
        running_scope_counts: dict[str, int] = {}
        busy_account_keys: set[str] = set()
        for task_id, state in workers:
            task = get_task(task_id)
            status = str((task or {}).get("status", "") or "")
            if status == TASK_STATUS_CANCEL_REQUESTED or status in TERMINAL_TASK_STATUSES:
                continue
            accounted += 1
            if state.scope:
                running_scope_counts[state.scope] = running_scope_counts.get(state.scope, 0) + 1
            busy_account_keys.update(state.account_keys)
        return self.max_parallel_tasks - accounted, running_scope_counts, busy_account_keys

    def _run_task(self, task_id: str) -> None:
        try:
            execute_task(task_id)
        finally:
            with self._lock:
                self._workers.pop(task_id, None)

    def _reap_workers(self) -> None:
        with self._lock:
            finished = [task_id for task_id, worker in self._workers.items() if not worker.thread.is_alive()]
            for task_id in finished:
                self._workers.pop(task_id, None)


task_runtime = TaskRuntime()
