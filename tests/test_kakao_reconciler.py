from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import features.kakao_pipeline.reconciler as reconciler_module
from sqlmodel import Session, select

from core.base_platform import Account
from core.db import KakaoPipelineModel, engine, save_account
from features.kakao_pipeline.reconciler import KakaoPipelineReconciler
from features.kakao_pipeline.service import KakaoPipelineService


def _create_account(email: str) -> int:
    account = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={"access_token": "background-test-token"},
        )
    )
    return int(account.id)


def test_background_work_is_discovered_from_all_persisted_pipelines():
    states = [
        "supplier_processing",
        "scanner_processing",
        "scanner_succeeded",
        "plus_checking",
        "plus_pending",
        "link_ready",
        "completed",
    ]
    account_ids = [_create_account(f"background-{index}@test.com") for index in range(len(states))]
    with Session(engine) as session:
        for account_id, state in zip(account_ids, states, strict=True):
            session.add(KakaoPipelineModel(account_id=account_id, state=state))
        session.commit()

    work = KakaoPipelineService().list_background_work()

    assert {(item["account_id"], item["state"]) for item in work} == {
        (account_ids[0], "supplier_processing"),
        (account_ids[1], "scanner_processing"),
        (account_ids[2], "scanner_succeeded"),
        (account_ids[3], "plus_checking"),
        (account_ids[4], "plus_pending"),
    }


def test_background_advance_skips_stale_selected_state(monkeypatch):
    account_id = _create_account("background-stale@test.com")
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="link_ready"))
        session.commit()

    service = KakaoPipelineService()
    monkeypatch.setattr(
        service,
        "poll_supplier",
        lambda _account_id: (_ for _ in ()).throw(AssertionError("stale work must not run")),
    )

    result = service.advance_background(account_id, expected_state="supplier_processing")

    assert result["state"] == "link_ready"


def test_background_scanner_success_runs_pipeline_plus_check(monkeypatch):
    account_id = _create_account("background-scanner-success@test.com")
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_processing"))
        session.commit()

    service = KakaoPipelineService()
    calls: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(service, "poll_scanner", lambda _account_id: {"state": "scanner_succeeded"})

    def check_plus(current_account_id: int, *, advance_pipeline: bool = False):
        calls.append(("plus", current_account_id, advance_pipeline))
        return {"state": "plus_pending"}

    monkeypatch.setattr(service, "check_plus", check_plus)

    result = service.advance_background(account_id, expected_state="scanner_processing")

    assert result["state"] == "plus_pending"
    assert calls == [("plus", account_id, True)]


def test_reconciler_processes_all_work_with_bounded_concurrency():
    class FakeService:
        def __init__(self):
            self.guard = threading.Lock()
            self.states = {
                1: "supplier_processing",
                2: "scanner_processing",
                3: "scanner_succeeded",
                4: "plus_pending",
                5: "supplier_processing",
            }
            self.calls: list[int] = []
            self.active = 0
            self.max_active = 0

        def list_background_work(self, *, limit: int):
            with self.guard:
                return [
                    {"account_id": account_id, "state": state}
                    for account_id, state in list(self.states.items())[:limit]
                ]

        def advance_background(self, account_id: int, *, expected_state: str):
            with self.guard:
                assert self.states[account_id] == expected_state
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append(account_id)
            time.sleep(0.03)
            with self.guard:
                self.active -= 1
                self.states.pop(account_id, None)
            return {"state": "completed"}

    service = FakeService()
    reconciler = KakaoPipelineReconciler(
        service=service,  # type: ignore[arg-type]
        scan_interval=0.01,
        max_workers=2,
    )
    reconciler.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            with service.guard:
                if not service.states and service.active == 0:
                    break
            time.sleep(0.01)
    finally:
        reconciler.stop()

    assert sorted(service.calls) == [1, 2, 3, 4, 5]
    assert service.max_active == 2
    assert reconciler.running is False


def test_reconciler_retries_one_failure_without_stopping(monkeypatch):
    monkeypatch.setattr(reconciler_module, "_ERROR_RETRY_DELAYS_SECONDS", (0.01,))

    class FlakyService:
        def __init__(self):
            self.guard = threading.Lock()
            self.pending = True
            self.calls = 0

        def list_background_work(self, *, limit: int):
            with self.guard:
                return [{"account_id": 7, "state": "supplier_processing"}] if self.pending else []

        def advance_background(self, account_id: int, *, expected_state: str):
            assert account_id == 7
            assert expected_state == "supplier_processing"
            with self.guard:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary supplier failure")
                self.pending = False
            return {"state": "completed"}

    service = FlakyService()
    reconciler = KakaoPipelineReconciler(
        service=service,  # type: ignore[arg-type]
        scan_interval=0.01,
        max_workers=1,
    )
    reconciler.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            with service.guard:
                if service.calls >= 2 and not service.pending:
                    break
            time.sleep(0.01)
    finally:
        reconciler.stop()

    assert service.calls == 2


def test_plus_pending_is_discovered_only_when_persisted_next_check_is_due():
    account_id = _create_account("plus-next-check@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                plus_next_check_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    assert service.list_background_work() == []

    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        pipeline.plus_next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(pipeline)
        session.commit()

    assert service.list_background_work() == [{"account_id": account_id, "state": "plus_pending"}]


def test_untracked_plus_is_discovered_only_when_persisted_next_check_is_due():
    account_id = _create_account("untracked-next-check@test.com")
    started_at = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_accepted_untracked",
                scanner_recovery_started_at=started_at,
                scanner_recovery_next_check_at=started_at + timedelta(minutes=5),
                scanner_recovery_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    assert service.list_background_work() == []

    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        pipeline.scanner_recovery_next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(pipeline)
        session.commit()

    assert service.list_background_work() == [
        {"account_id": account_id, "state": "scanner_accepted_untracked"}
    ]
