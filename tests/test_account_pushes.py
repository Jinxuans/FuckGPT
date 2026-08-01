from __future__ import annotations

from sqlmodel import Session, select

from core.base_platform import Account
from core.db import AccountPushDeliveryModel, save_account, engine


def _create_pushable_account(*, with_codex: bool = True, email: str = "pushable@example.com", extra_updates: dict | None = None):
    extra = {
        "access_token": "platform-access-secret",
        "refresh_token": "platform-refresh-secret",
    }
    if with_codex:
        extra.update({
            "codex_access_token": "codex-access-secret",
            "codex_refresh_token": "codex-refresh-secret",
        })
    extra.update(extra_updates or {})
    return save_account(Account(
        platform="chatgpt",
        email=email,
        password="Password123!",
        extra=extra,
    ))


def _configure_nvtokens(client):
    response = client.post("/api/provider-settings", json={
        "provider_type": "push",
        "provider_key": "nvtokens",
        "display_name": "NexusVault",
        "auth_mode": "apikey",
        "enabled": True,
        "is_default": True,
        "config": {
            "nvtokens_endpoint": "https://nvtokens.test/api/inventory/cards/import",
            "nvtokens_payload_format": "codex",
            "nvtokens_timeout": "5",
        },
        "auth": {"nvtokens_api_key": "test-api-key"},
    })
    assert response.status_code == 200


def test_push_requires_configured_target(client):
    account = _create_pushable_account()
    response = client.post("/api/accounts/push", json={
        "ids": [account.id],
        "select_all": False,
    })
    assert response.status_code == 400
    assert "推送目标" in response.json()["detail"]


def test_push_codex_payload_and_records_delivery(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account()
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post("/api/accounts/push", json={
        "ids": [account.id],
        "select_all": False,
        "target_key": "nvtokens",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert captured["headers"]["x-api-key"] == "test-api-key"
    assert captured["json"] == {
        "data": {
            "access_token": "codex-access-secret",
            "refresh_token": "codex-refresh-secret",
            "email": "pushable@example.com",
            "type": "codex",
        }
    }

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.account_id == account.id
        assert delivery.target_key == "nvtokens"
        assert delivery.status == "success"
        assert delivery.attempt_count == 1
        assert delivery.http_status == 200
        assert delivery.pushed_at is not None

    listed = client.get("/api/accounts", params={"platform": "chatgpt"}).json()
    status = listed["items"][0]["push_deliveries"][0]
    assert status["target_key"] == "nvtokens"
    assert status["status"] == "success"
    assert status["last_attempt_at"].endswith("Z")
    assert status["pushed_at"].endswith("Z")
    assert "codex-access-secret" not in str(status)


def test_codex_push_never_falls_back_to_platform_tokens(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account(with_codex=False)
    remote_called = False

    def fake_post(*args, **kwargs):
        nonlocal remote_called
        remote_called = True
        raise AssertionError("缺少 Codex 凭据时不应请求远端")

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post("/api/accounts/push", json={"ids": [account.id]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["succeeded"] == 0
    assert payload["failed"] == 1
    assert payload["results"][0]["error"] == "账号缺少 Codex access_token"
    assert remote_called is False

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.status == "failed"
        assert delivery.http_status == 0
        assert delivery.last_error == "账号缺少 Codex access_token"


def test_failed_push_records_safe_http_error(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account()

    class FakeResponse:
        status_code = 401

    monkeypatch.setattr(
        "providers.push.nvtokens.httpx.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    response = client.post("/api/accounts/push", json={"ids": [account.id]})
    assert response.status_code == 200
    assert response.json()["failed"] == 1

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.status == "failed"
        assert delivery.http_status == 401
        assert delivery.last_error == "远端返回 HTTP 401"
        assert "test-api-key" not in delivery.last_error

    listed = client.get("/api/accounts", params={"platform": "chatgpt"}).json()
    status = listed["items"][0]["push_deliveries"][0]
    assert status["last_attempt_at"].endswith("Z")
    assert status["pushed_at"] is None


def test_push_select_all_uses_complete_v2_filter_result(client, monkeypatch):
    _configure_nvtokens(client)
    matched = _create_pushable_account(
        email="push-filter-match@example.com",
        extra_updates={"region": "US", "account_source": "import", "import_method": "csv"},
    )
    _create_pushable_account(
        email="push-filter-ignore@example.com",
        extra_updates={"region": "JP", "account_source": "import", "import_method": "text"},
    )
    pushed_emails = []

    class FakeResponse:
        status_code = 200

    def fake_post(_url, *, json, **_kwargs):
        pushed_emails.append(json["data"]["email"])
        return FakeResponse()

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post(
        "/api/accounts/push",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "target_key": "nvtokens",
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
        },
    )

    assert response.status_code == 200
    assert response.json()["succeeded"] == 1
    assert pushed_emails == [matched.email]
