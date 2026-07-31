from __future__ import annotations

from core.base_platform import Account
from core.db import save_account
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
    assert "access-token-for-kakao-test" not in response.text
