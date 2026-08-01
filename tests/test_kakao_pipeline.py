from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.base_platform import Account
from core.db import KakaoPipelineModel, save_account
from features.kakao_pipeline.client import CustomerApiClient, CustomerApiProblem
from features.kakao_pipeline.service import KakaoPipelineService
from features.kakao_pipeline.workstation_client import WorkstationScannerClient


def _create_account(email: str = "kakao@test.com") -> int:
    created = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={"access_token": "access-token-for-kakao-test"},
        )
    )
    return int(created.id)


def _create_session_account(email: str = "kakao-session@test.com") -> int:
    created = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={
                "access_token": "access-token-for-kakao-test",
                "session_token": "session-cookie-for-kakao-test",
            },
        )
    )
    return int(created.id)


def test_plus_confirmation_uses_initial_ladder_then_60_to_120_seconds(monkeypatch):
    from features.kakao_pipeline import service as kakao_service

    requested_bounds: list[tuple[int, int]] = []

    def fake_randint(lower: int, upper: int) -> int:
        requested_bounds.append((lower, upper))
        return 60

    monkeypatch.setattr(kakao_service.random, "randint", fake_randint)

    assert [kakao_service._next_plus_delay_seconds(count) for count in range(1, 6)] == [5, 10, 30, 30, 30]
    assert kakao_service._next_plus_delay_seconds(6) == 60
    assert kakao_service._next_plus_delay_seconds(20) == 60
    assert requested_bounds == [(60, 120), (60, 120)]


def test_pipeline_latest_event_time_is_not_the_poll_updated_time():
    pipeline = KakaoPipelineModel(
        account_id=123,
        state="supplier_processing",
        updated_at=datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc),
    )
    pipeline.set_events(
        [
            {
                "time": "2026-08-01T12:00:00+00:00",
                "level": "info",
                "message": "供应商状态: PENDING",
            }
        ]
    )

    serialized = KakaoPipelineService._serialize_pipeline(pipeline)

    assert serialized["updated_at"] == "2026-08-01T12:05:00+00:00"
    assert serialized["latest_event_at"] == "2026-08-01T12:00:00+00:00"


def test_customer_api_client_builds_extract_and_ready_link_requests(monkeypatch):
    calls = []

    class Response:
        status_code = 201

        def json(self):
            return {"ok": True, "data": {"order": {"id": "order-1"}}}

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", fake_request)
    client = CustomerApiClient("https://supplier.example", "cdk-secret")
    client.create_extraction("at-secret", payment_method="kakao_pay")
    client.create_scanner("at-secret", "https://pay.nicepay.co.kr/v1/checkout/pay/abc")
    client.create_scanner(
        "at-secret",
        "https://pay.nicepay.co.kr/v1/checkout/pay/session",
        session_cookie="session-cookie-secret",
    )

    assert calls[0][0] == "POST"
    assert calls[0][2]["headers"]["X-CDK-Key"] == "cdk-secret"
    assert calls[0][2]["json"]["mode"] == "EXTRACT"
    assert calls[1][2]["json"]["mode"] == "READY_LINK"
    assert calls[1][2]["json"]["payment_url"].startswith("https://pay.nicepay.co.kr/")
    assert calls[1][2]["allow_redirects"] is False
    assert calls[2][2]["json"]["session_cookie"] == "session-cookie-secret"
    assert "access_token" not in calls[2][2]["json"]


def test_customer_api_client_rejects_cross_origin_poll_url(monkeypatch):
    def should_not_request(*args, **kwargs):
        raise AssertionError("跨域 pollUrl 不应发起请求")

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", should_not_request)
    client = CustomerApiClient("https://supplier.example", "cdk-secret")

    try:
        client.get_order("https://attacker.example/orders/1", "customer-token")
    except ValueError as exc:
        assert "同源" in str(exc)
    else:
        raise AssertionError("跨域 pollUrl 应被拒绝")


def test_customer_api_client_checks_exact_cdk_quota(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "data": {
                    "productType": "KAKAO_AT",
                    "totalCount": 10,
                    "usedCount": 2,
                    "frozenCount": 1,
                    "availableCount": 7,
                    "status": "ACTIVE",
                },
            }

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", fake_request)
    result = CustomerApiClient("https://upi.example", "visible-cdk").check_cdk()

    assert result["availableCount"] == 7
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://upi.example/api/customer/cdk/check"
    assert calls[0][2]["json"] == {"code": "visible-cdk"}
    assert "X-CDK-Key" not in calls[0][2]["headers"]


def test_workstation_scanner_client_uses_submission_protocol(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/payment-submissions/batch"):
            return Response({"submissions": [{"id": "submission-1", "state": "pending"}]})
        if url.endswith("/api/payment-submissions/status"):
            return Response({"ok": True, "items": [{"id": "submission-1", "state": "completed"}]})
        return Response({"unlimited": False, "remaining": 3, "limit": 10})

    monkeypatch.setattr("features.kakao_pipeline.workstation_client.requests.request", fake_request)
    client = WorkstationScannerClient("https://kakao.example", "payment-cdk")
    quota = client.check_cdk()
    submitted = client.submit_payment("https://pay.nicepay.co.kr/v1/checkout/pay/workstation")
    status = client.get_submission("submission-1")

    assert quota["remaining"] == 3
    assert submitted["submissions"][0]["id"] == "submission-1"
    assert status["data"]["state"] == "completed"
    assert calls[1][2]["json"] == {
        "payment_urls": ["https://pay.nicepay.co.kr/v1/checkout/pay/workstation"],
        "submit_cdk": "payment-cdk",
    }
    assert calls[2][2]["json"] == {"ids": ["submission-1"]}


def test_kakao_pipeline_supplier_to_scanner_flow(monkeypatch):
    account_id = _create_session_account()
    service = KakaoPipelineService()

    class FakeClient:
        def __init__(self, base_url, cdk_key):
            assert base_url
            assert cdk_key

        def create_extraction(self, access_token, *, payment_method):
            assert access_token == "access-token-for-kakao-test"
            return {
                "data": {
                    "order": {"id": "supplier-order", "status": "PENDING"},
                    "customerToken": "supplier-token",
                    "pollUrl": "/api/v1/customer/orders/supplier-order",
                }
            }

        def create_scanner(self, access_token, payment_url, *, payment_method, session_cookie=""):
            assert access_token == "access-token-for-kakao-test"
            assert session_cookie == "session-cookie-for-kakao-test"
            assert payment_url.startswith("https://pay.nicepay.co.kr/")
            return {
                "data": {
                    "order": {"id": "scanner-order", "status": "PENDING"},
                    "customerToken": "scanner-token",
                    "pollUrl": "/api/v1/customer/orders/scanner-order",
                }
            }

        def get_order(self, poll_url, customer_token):
            if "supplier" in poll_url:
                return {
                    "data": {
                        "status": "READY",
                        "qualification": {
                            "status": "QUALIFIED",
                            "zeroVerified": True,
                            "postPromoAmountKrw": 0,
                            "postTaxAmountKrw": 0,
                        },
                        "extraction": {
                            "status": "READY",
                            "paymentUrl": "https://pay.nicepay.co.kr/v1/checkout/pay/ready",
                            "stage": 9,
                            "stageTotal": 9,
                            "stageName": "完成",
                        },
                    }
                }
            return {
                "data": {
                    "status": "SUCCEEDED",
                    "subscription": {"status": "PLUS"},
                    "payment": {"qrUrl": "https://scanner.example/qr/1"},
                }
            }

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeClient)
    monkeypatch.setenv("KAKAO_SUPPLIER_CDK_KEY", "supplier-cdk")
    monkeypatch.setenv("KAKAO_SCANNER_CDK_KEY", "scanner-cdk")

    created = service.start_extraction(account_id)
    assert created["state"] == "supplier_processing"
    assert created["latest_event_at"] == created["events"][-1]["time"]
    ready = service.poll_supplier(account_id)
    assert ready["state"] == "link_ready"
    assert ready["payment_url"].startswith("https://pay.nicepay.co.kr/")
    submitted = service.submit_scanner(account_id)
    assert submitted["state"] == "scanner_processing"
    scanned = service.poll_scanner(account_id)
    assert scanned["state"] == "scanner_succeeded"
    assert scanned["scan_url"] == "https://scanner.example/qr/1"

    detail = service.get_account_pipeline(account_id)
    encoded = str(detail)
    assert "supplier-token" not in encoded
    assert "scanner-token" not in encoded
    assert "customerToken" not in detail["supplier_response"]["data"]


def test_kakao_settings_support_visible_multiline_cdk_pool(client):
    response = client.put(
        "/api/kakao-pipeline/settings/supplier",
        json={
            "display_name": "临时供应商",
            "base_url": "http://127.0.0.1:8788",
            "cdk_keys": "supplier-cdk-one\nsupplier-cdk-two\nsupplier-cdk-one",
        },
    )
    assert response.status_code == 200
    payload = response.json()["item"]
    assert payload["display_name"] == "临时供应商"
    assert payload["has_cdk"] is True
    assert payload["cdk_count"] == 2
    assert payload["cdk_keys"] == ["supplier-cdk-one", "supplier-cdk-two"]

    listed = client.get("/api/kakao-pipeline/settings")
    assert listed.status_code == 200
    assert listed.json()["supplier"]["cdk_keys"] == ["supplier-cdk-one", "supplier-cdk-two"]
    assert listed.json()["scanner"]["base_url"] == "https://customer.i7wap.xyz"

    migrated = client.put(
        "/api/kakao-pipeline/settings/scanner",
        json={"display_name": "扫码平台", "base_url": "https://upi.i7wap.xyz", "cdk_keys": "scanner-cdk"},
    )
    assert migrated.status_code == 200
    assert migrated.json()["item"]["base_url"] == "https://customer.i7wap.xyz"

    selected = client.put(
        "/api/kakao-pipeline/settings/default-scanner/select",
        json={"scanner_kind": "scanner_546789"},
    )
    assert selected.status_code == 200
    assert selected.json()["default_scanner_kind"] == "scanner_546789"
    assert client.get("/api/kakao-pipeline/settings").json()["default_scanner_kind"] == "scanner_546789"

    automatic = client.put(
        "/api/kakao-pipeline/settings/options/auto-upload",
        json={"enabled": True},
    )
    assert automatic.status_code == 200
    assert automatic.json()["auto_upload_after_extract"] is True
    assert client.get("/api/kakao-pipeline/settings").json()["auto_upload_after_extract"] is True

    cleared = client.put(
        "/api/kakao-pipeline/settings/supplier",
        json={"display_name": "临时供应商", "base_url": "http://127.0.0.1:8788", "cdk_keys": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["item"]["cdk_keys"] == []


def test_depleted_scanner_cdk_is_removed_and_upload_can_retry(monkeypatch):
    account_id = _create_account("retry-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner",
        {
            "display_name": "扫码平台",
            "base_url": "https://scanner.example",
            "cdk_keys": "used-cdk\nnext-cdk",
        },
    )

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        pipeline = KakaoPipelineModel(
            account_id=account_id,
            state="link_ready",
            payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/retry",
        )
        session.add(pipeline)
        session.commit()

    used_keys = []

    class FakeClient:
        def __init__(self, base_url, cdk_key):
            used_keys.append(cdk_key)
            self.cdk_key = cdk_key

        def create_scanner(self, access_token, payment_url, *, payment_method, session_cookie=""):
            if self.cdk_key == "used-cdk":
                raise CustomerApiProblem(403, "insufficient_cdk_uses", "CDK 可用次数不足 / Insufficient CDK uses")
            return {
                "data": {
                    "order": {"id": "scanner-retry", "status": "PENDING"},
                    "customerToken": "scanner-token",
                    "pollUrl": "/api/v1/customer/orders/scanner-retry",
                }
            }

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeClient)

    try:
        service.submit_scanner(account_id)
    except CustomerApiProblem:
        pass
    else:
        raise AssertionError("第一条已用完的 CDK 应导致提交失败")

    failed = service.get_account_pipeline(account_id)
    assert failed["state"] == "scanner_failed"
    assert "已删除用完的 CDK" in failed["last_error_message"]
    assert service.list_settings()["scanner"]["cdk_keys"] == ["next-cdk"]

    retried = service.submit_scanner(account_id)
    assert retried["state"] == "scanner_processing"
    assert used_keys == ["used-cdk", "next-cdk"]


def test_546789_scanner_flow_uses_independent_driver(monkeypatch):
    account_id = _create_account("workstation-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789 扫码",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/workstation-flow",
            )
        )
        session.commit()

    class FakeWorkstationClient:
        def __init__(self, base_url, cdk_key=""):
            assert base_url == "https://kakao.example"
            if cdk_key:
                assert cdk_key == "workstation-cdk"

        def submit_payment(self, payment_url):
            assert payment_url.endswith("/workstation-flow")
            return {"submissions": [{"id": "submission-flow", "state": "pending"}]}

        def get_submission(self, submission_id):
            assert submission_id == "submission-flow"
            return {"ok": True, "data": {"id": submission_id, "state": "completed"}}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)

    submitted = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )
    assert submitted["state"] == "scanner_processing"
    assert submitted["scanner_driver"] == "payment_submission"
    assert submitted["scanner_order_id"] == "submission-flow"
    assert submitted["scan_url"].endswith("/submission-flow/qr.png")

    completed = service.poll_scanner(account_id)
    assert completed["state"] == "scanner_succeeded"
    assert completed["scanner_status"] == "COMPLETED"


def test_supplier_ready_poll_does_not_regress_an_existing_scanner_state(monkeypatch):
    account_id = _create_account("supplier-stale-ready@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                supplier_order_id="supplier-order",
                supplier_poll_url="/api/v1/customer/orders/supplier-order",
                supplier_customer_token="supplier-token",
                scanner_order_id="scanner-order",
            )
        )
        session.commit()

    class ShouldNotPoll:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("过期的供应商轮询不应再访问上游")

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", ShouldNotPoll)

    result = service.poll_supplier(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "scanner-order"


def test_546789_unknown_immediately_compensates_and_tracks_new_submission(monkeypatch):
    account_id = _create_account("workstation-compensate@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/compensate",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="missing-submission",
                scanner_status="PENDING",
                scanner_submit_attempts=1,
            )
        )
        session.commit()

    calls: list[str] = []

    class FakeWorkstationClient:
        def __init__(self, _base_url, cdk_key=""):
            self.cdk_key = cdk_key

        def get_submission(self, submission_id):
            assert submission_id == "missing-submission"
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, payment_url):
            calls.append(payment_url)
            assert self.cdk_key == "workstation-cdk"
            return {"submissions": [{"id": "replacement-submission", "state": "queued"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "replacement-submission"
    assert result["scanner_status"] == "QUEUED"
    assert result["scanner_submit_attempts"] == 2
    assert result["scanner_compensation_attempted"] is True
    assert len(calls) == 1


def test_546789_duplicate_compensation_switches_to_untracked_plus_confirmation(monkeypatch):
    account_id = _create_account("workstation-duplicate@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/duplicate",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="unknown-submission",
                scanner_status="PENDING",
                scanner_submit_attempts=1,
                scan_url="https://kakao.example/old-qr.png",
            )
        )
        session.commit()

    class DuplicateWorkstationClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, _payment_url):
            raise CustomerApiProblem(409, "http_409", "支付链接已经提交，请勿重复点击")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", DuplicateWorkstationClient)

    accepted = service.poll_scanner(account_id)

    assert accepted["state"] == "scanner_accepted_untracked"
    assert accepted["scanner_status"] == "DUPLICATE_ACCEPTED"
    assert accepted["scanner_order_id"] == ""
    assert accepted["scan_url"] == ""
    assert accepted["scanner_submit_attempts"] == 2
    assert accepted["scanner_compensation_attempted"] is True
    recovery_started = datetime.fromisoformat(accepted["scanner_recovery_started_at"])
    recovery_next = datetime.fromisoformat(accepted["scanner_recovery_next_check_at"])
    recovery_deadline = datetime.fromisoformat(accepted["scanner_recovery_deadline_at"])
    assert timedelta(seconds=29) <= recovery_next - recovery_started <= timedelta(seconds=31)
    assert timedelta(minutes=29, seconds=59) <= recovery_deadline - recovery_started <= timedelta(minutes=30, seconds=1)

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    completed = service.check_untracked_plus(account_id)

    assert completed["state"] == "completed"
    assert completed["completion_source"] == "duplicate_submission_untracked"
    assert completed["scanner_recovery_check_count"] == 1
    assert completed["scanner_recovery_next_check_at"] is None


def test_untracked_plus_confirmation_stops_only_at_persisted_30_minute_deadline(monkeypatch):
    account_id = _create_account("untracked-plus-deadline@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_accepted_untracked",
                scanner_status="DUPLICATE_ACCEPTED",
                scanner_recovery_started_at=started_at,
                scanner_recovery_next_check_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                scanner_recovery_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("到期后不应再次查询 Plus")),
    )

    result = service.advance_background(account_id, expected_state="scanner_accepted_untracked")

    assert result["state"] == "scanner_recovery_unconfirmed"
    assert result["last_error_code"] == "untracked_plus_unconfirmed"
    assert result["scanner_recovery_next_check_at"] is None
    assert "30 分钟" in result["last_error_message"]


def test_546789_compensation_is_never_submitted_more_than_once(monkeypatch):
    account_id = _create_account("workstation-one-compensation@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/no-third-submit",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="still-missing",
                scanner_status="PENDING",
                scanner_submit_attempts=2,
                scanner_compensation_attempted=True,
            )
        )
        session.commit()

    class NoThirdSubmitClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, _payment_url):
            raise AssertionError("补偿已使用后不得第三次提交")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", NoThirdSubmitClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_recovery_deadline_at"] is not None
    assert result["scanner_submit_attempts"] == 2


def test_546789_ambiguous_first_submit_immediately_uses_one_compensation(monkeypatch):
    account_id = _create_account("workstation-first-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/first-timeout",
            )
        )
        session.commit()

    calls = 0

    class FirstTimeoutClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def submit_payment(self, _payment_url):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CustomerApiProblem(503, "network_error", "连接超时")
            return {"submissions": [{"id": "compensated-submission", "state": "queued"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FirstTimeoutClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )

    assert calls == 2
    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "compensated-submission"
    assert result["scanner_submit_attempts"] == 2
    assert result["scanner_compensation_attempted"] is True


def test_546789_two_ambiguous_submits_enter_30_minute_observation(monkeypatch):
    account_id = _create_account("workstation-double-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/double-timeout",
            )
        )
        session.commit()

    calls = 0

    class DoubleTimeoutClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def submit_payment(self, _payment_url):
            nonlocal calls
            calls += 1
            raise CustomerApiProblem(503, "network_error", "连接超时")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", DoubleTimeoutClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )

    assert calls == 2
    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_submit_attempts"] == 2
    started = datetime.fromisoformat(result["scanner_recovery_started_at"])
    deadline = datetime.fromisoformat(result["scanner_recovery_deadline_at"])
    assert timedelta(minutes=29, seconds=59) <= deadline - started <= timedelta(minutes=30, seconds=1)


def test_i7_ambiguous_submit_enters_observation_and_cannot_resubmit(monkeypatch):
    account_id = _create_account("i7-submit-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner",
        {
            "display_name": "I7wap",
            "base_url": "https://scanner.example",
            "cdk_keys": "scanner-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/i7-timeout",
            )
        )
        session.commit()

    class TimeoutCustomerClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_scanner(self, *_args, **_kwargs):
            raise CustomerApiProblem(503, "network_error", "连接超时")

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", TimeoutCustomerClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner",
    )

    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_submit_attempts"] == 1

    try:
        service.submit_scanner(account_id, scanner_setting_id=setting["id"], scanner_kind="scanner")
    except ValueError as exc:
        assert "必须先成功提取" in str(exc)
    else:
        raise AssertionError("结果不确定后不应允许再次上传")


def test_supplier_processing_timeout_preserves_original_order(monkeypatch):
    account_id = _create_account("supplier-processing-timeout@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="supplier_processing",
                supplier_order_id="existing-supplier-order",
                supplier_processing_started_at=started_at,
                supplier_deadline_at=started_at + timedelta(minutes=15),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.CustomerApiClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("后台到期后不应请求上游")),
    )

    result = service.advance_background(account_id, expected_state="supplier_processing")

    assert result["state"] == "supplier_poll_failed"
    assert result["supplier_order_id"] == "existing-supplier-order"
    assert result["last_error_code"] == "supplier_processing_timeout"


def test_force_reset_can_clear_stuck_active_state():
    account_id = _create_account("force-reset@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/stuck-task",
            )
        )
        session.commit()

    reset = service.reset(account_id, force=True)

    assert reset["state"] == "idle"
    listed = service.list_accounts(search="force-reset@test.com")
    assert listed["items"][0]["pipeline"]["state"] == "idle"


def test_manual_plus_check_preserves_pipeline_stage(monkeypatch):
    account_id = _create_account("manual-plus-check@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/manual-check",
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )

    checked = service.check_plus(account_id, advance_pipeline=False)

    assert checked["state"] == "link_ready"
    assert checked["payment_url"].endswith("/manual-check")


def test_pipeline_plus_check_advances_after_scanner_success(monkeypatch):
    account_id = _create_account("pipeline-plus-check@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    checked = service.check_plus(account_id, advance_pipeline=True)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 1
    assert checked["plus_check_started_at"] is not None
    assert checked["plus_next_check_at"] is not None
    started = datetime.fromisoformat(checked["plus_check_started_at"])
    next_check = datetime.fromisoformat(checked["plus_next_check_at"])
    deadline = datetime.fromisoformat(checked["plus_check_deadline_at"])
    assert timedelta(seconds=4) <= next_check - started <= timedelta(seconds=6)
    assert timedelta(minutes=9, seconds=59) <= deadline - started <= timedelta(minutes=10, seconds=1)
    assert next_check <= deadline


def test_pipeline_plus_query_failure_uses_same_persisted_ladder(monkeypatch):
    account_id = _create_account("failed-plus-query-interval@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="temporary query failure"),
    )
    checked = service.check_plus(account_id, advance_pipeline=True)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 1
    started = datetime.fromisoformat(checked["plus_check_started_at"])
    next_check = datetime.fromisoformat(checked["plus_next_check_at"])
    deadline = datetime.fromisoformat(checked["plus_check_deadline_at"])
    assert timedelta(seconds=4) <= next_check - started <= timedelta(seconds=6)
    assert next_check <= deadline
    assert service.list_background_work() == []


def test_pipeline_plus_check_uses_random_interval_from_sixth_check(monkeypatch):
    account_id = _create_account("sixth-plus-check-random@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                plus_check_count=5,
                plus_check_started_at=now - timedelta(minutes=2),
                plus_check_deadline_at=now + timedelta(minutes=28),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        "features.kakao_pipeline.service.random.randint",
        lambda lower, upper: 75 if (lower, upper) == (60, 120) else lower,
    )

    check_started = datetime.now(timezone.utc)
    checked = service.check_plus(account_id, advance_pipeline=True)
    next_check = datetime.fromisoformat(checked["plus_next_check_at"]).replace(tzinfo=timezone.utc)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 6
    assert timedelta(seconds=74) <= next_check - check_started <= timedelta(seconds=76)


def test_normal_plus_confirmation_completes_only_with_fresh_query_result(monkeypatch):
    account_id = _create_account("fresh-normal-plus@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded", scanner_status="COMPLETED"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert result["state"] == "completed"
    assert result["completion_source"] == "normal_scanner"
    assert result["plus_next_check_at"] is None


def test_completed_pipeline_cannot_regress_during_account_refresh(monkeypatch):
    account_id = _create_account("completed-no-regression@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    completed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completion_source="normal_scanner",
                completed_at=completed_at,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "free", "state": "free"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert result["state"] == "completed"
    assert result["plus_status"] == "plus"
    assert result["final_result"] == "plus"
    assert result["completion_source"] == "normal_scanner"
    assert datetime.fromisoformat(result["completed_at"]) == completed_at.replace(tzinfo=None)


def test_scanner_processing_timeout_preserves_order_for_manual_poll(monkeypatch):
    account_id = _create_account("scanner-processing-timeout@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                scanner_driver="customer_api",
                scanner_order_id="existing-order",
                scanner_processing_started_at=started_at,
                scanner_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.CustomerApiClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("后台到期后不应请求上游")),
    )

    result = service.advance_background(account_id, expected_state="scanner_processing")

    assert result["state"] == "scanner_poll_failed"
    assert result["scanner_order_id"] == "existing-order"
    assert result["last_error_code"] == "scanner_processing_timeout"


def test_manual_scanner_poll_reuses_existing_order_after_timeout(monkeypatch):
    account_id = _create_account("manual-existing-scanner-order@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_poll_failed",
                scanner_driver="customer_api",
                scanner_base_url="https://scanner.example",
                scanner_cdk_key="cdk",
                scanner_order_id="existing-order",
                scanner_poll_url="/api/v1/customer/orders/existing-order",
                scanner_customer_token="customer-token",
                scanner_processing_started_at=datetime.now(timezone.utc) - timedelta(minutes=31),
                scanner_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        session.commit()

    class ExistingOrderClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_order(self, poll_url, customer_token):
            assert poll_url.endswith("existing-order")
            assert customer_token == "customer-token"
            return {"data": {"status": "COMPLETED", "subscription": {"status": "PLUS"}}}

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", ExistingOrderClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_succeeded"
    assert result["scanner_order_id"] == "existing-order"


def test_legacy_plus_pending_older_than_10_minutes_is_persistently_paused(monkeypatch):
    account_id = _create_account("legacy-plus-pending@test.com")
    service = KakaoPipelineService()

    from datetime import timedelta
    from sqlmodel import Session

    from core.db import engine

    old_time = datetime.now(timezone.utc) - timedelta(minutes=11)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                scanner_status="COMPLETED",
                updated_at=old_time,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("过期窗口不应再查询 Plus")),
    )

    result = service.advance_background(account_id, expected_state="plus_pending")

    assert result["state"] == "plus_unconfirmed"
    assert result["last_error_code"] == "plus_unconfirmed"
    assert result["plus_next_check_at"] is None
    assert result["plus_check_paused_at"] is not None
    assert account_id not in {item["account_id"] for item in service.list_background_work()}


def test_manual_plus_check_does_not_restart_paused_10_minute_window(monkeypatch):
    account_id = _create_account("manual-paused-plus@test.com")
    service = KakaoPipelineService()

    from datetime import timedelta
    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    legacy_deadline_at = started_at + timedelta(minutes=30)
    expected_deadline_at = started_at + timedelta(minutes=10)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_unconfirmed",
                scanner_status="COMPLETED",
                plus_check_count=8,
                plus_check_started_at=started_at,
                plus_check_deadline_at=legacy_deadline_at,
                plus_check_paused_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "free", "state": "free"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert result["state"] == "plus_unconfirmed"
    assert datetime.fromisoformat(result["plus_check_started_at"]) == started_at.replace(tzinfo=None)
    assert datetime.fromisoformat(result["plus_check_deadline_at"]) == expected_deadline_at.replace(tzinfo=None)
    assert result["plus_next_check_at"] is None


def test_546789_expired_keeps_link_and_quota_removal_is_isolated(monkeypatch):
    account_id = _create_account("expired-workstation@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner",
        {"display_name": "I7wap", "base_url": "https://customer.example", "cdk_keys": "i7wap-cdk"},
    )
    setting = service.save_setting(
        "scanner_546789",
        {"display_name": "546789", "base_url": "https://kakao.example", "cdk_keys": "empty-cdk\nusable-cdk"},
    )

    class QuotaClient:
        def __init__(self, base_url, cdk_key=""):
            self.cdk_key = cdk_key

        def check_cdk(self):
            remaining = 0 if self.cdk_key == "empty-cdk" else 2
            return {"unlimited": False, "remaining": remaining, "limit": 5}

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", QuotaClient)
    checked = service.check_cdks("scanner_546789")
    assert checked["removed"] == ["empty-cdk"]
    assert service.list_settings()["scanner_546789"]["cdk_keys"] == ["usable-cdk"]
    assert service.list_settings()["scanner"]["cdk_keys"] == ["i7wap-cdk"]

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    payment_url = "https://pay.nicepay.co.kr/v1/checkout/pay/expired-workstation"
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="link_ready", payment_url=payment_url))
        session.commit()

    class ExpiredClient:
        def __init__(self, base_url, cdk_key=""):
            pass

        def submit_payment(self, payment_url):
            return {"submissions": [{"id": "expired-submission", "state": "pending"}]}

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "expired"}}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", ExpiredClient)
    service.submit_scanner(account_id, scanner_setting_id=setting["id"], scanner_kind="scanner_546789")
    expired = service.poll_scanner(account_id)
    assert expired["state"] == "scanner_failed"
    assert expired["last_error_code"] == "payment_link_expired"
    assert expired["payment_url"] == payment_url


def test_successful_extraction_can_auto_submit_to_default_scanner(monkeypatch):
    account_id = _create_account("auto-upload-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789 自动扫码",
            "base_url": "https://kakao.example",
            "cdk_keys": "auto-cdk",
        },
    )
    service.set_default_scanner("scanner_546789")
    service.set_auto_upload(True)

    class FakeSupplierClient:
        def __init__(self, base_url, cdk_key):
            pass

        def create_extraction(self, access_token, *, payment_method):
            return {
                "data": {
                    "order": {"id": "auto-supplier", "status": "PENDING"},
                    "customerToken": "auto-token",
                    "pollUrl": "/api/v1/customer/orders/auto-supplier",
                }
            }

        def get_order(self, poll_url, customer_token):
            return {
                "data": {
                    "status": "READY",
                    "qualification": {
                        "zeroVerified": True,
                        "postPromoAmountKrw": 0,
                        "postTaxAmountKrw": 0,
                    },
                    "extraction": {
                        "paymentUrl": "https://pay.nicepay.co.kr/v1/checkout/pay/auto-upload",
                    },
                }
            }

    class FakeWorkstationClient:
        def __init__(self, base_url, cdk_key=""):
            assert base_url == "https://kakao.example"

        def submit_payment(self, payment_url):
            assert payment_url.endswith("/auto-upload")
            return {"submissions": [{"id": "auto-submission", "state": "pending"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeSupplierClient)
    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)
    monkeypatch.setenv("KAKAO_SUPPLIER_CDK_KEY", "supplier-cdk")

    service.start_extraction(account_id)
    result = service.poll_supplier(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_driver"] == "payment_submission"
    assert result["scanner_order_id"] == "auto-submission"
    assert result["scanner_name"] == "546789 自动扫码"


def test_kakao_account_list_uses_local_accounts_without_exposing_tokens(client):
    account_id = _create_account("list-kakao@test.com")
    response = client.get("/api/kakao-pipeline/accounts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == account_id
    assert payload["items"][0]["account_view"]["security"]["phone_bound"] is False
    assert payload["items"][0]["pipeline"]["latest_event_at"] is None
    assert "access-token-for-kakao-test" not in response.text


def test_kakao_account_list_exposes_phone_and_codex_status_without_secrets(monkeypatch):
    service = KakaoPipelineService()
    monkeypatch.setattr(
        service.accounts,
        "list_accounts",
        lambda _query: {
            "total": 1,
            "items": [
                {
                    "id": 321,
                    "email": "status@test.com",
                    "account_view": {
                        "identity": {"email": "status@test.com"},
                        "status": {"validity": "valid", "checked_at": "2026-08-01T08:00:00"},
                        "subscription": {"plan": "plus", "state": "subscribed"},
                        "security": {
                            "phone_bound": True,
                            "phone_number_masked": "+86****1234",
                        },
                        "codex": {
                            "authorized": True,
                            "has_access_token": True,
                            "access_token": "must-not-leak",
                        },
                    },
                }
            ],
        },
    )

    payload = service.list_accounts()

    item = payload["items"][0]
    assert item["account_view"]["security"] == {
        "phone_bound": True,
        "phone_number_masked": "+86****1234",
    }
    assert "must-not-leak" not in str(payload)
