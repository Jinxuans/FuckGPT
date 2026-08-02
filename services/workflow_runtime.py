"""Durable workflow runtime for single-process orchestration."""
from __future__ import annotations

import threading
from application.workflows import recover_incomplete_workflow_runs, run_due_workflow_once


class WorkflowRuntime:
    def __init__(self, *, poll_interval: float = 0.5):
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher: threading.Thread | None = None
        self._wake_event = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            recover_incomplete_workflow_runs()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="workflow-runtime")
            self._dispatcher.start()
            print("[WorkflowRuntime] 已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._wake_event.set()
        print("[WorkflowRuntime] 停止中")

    def wake_up(self) -> None:
        self._wake_event.set()

    def _loop(self) -> None:
        while self._running:
            progressed = False
            while self._running and run_due_workflow_once():
                progressed = True
            if progressed:
                continue
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()


workflow_runtime = WorkflowRuntime()
