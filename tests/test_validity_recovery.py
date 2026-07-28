from __future__ import annotations

import json

from sqlmodel import Session, select

from application.tasks import _run_single_account_check
from core.account_graph import patch_account_graph
from core.base_platform import RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.lifecycle import check_accounts_validity
from core.proxy_pool import proxy_pool
from platforms.chatgpt import subscription
from platforms.chatgpt.plugin import ChatGPTPlatform


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _overview(account_id: int):
    with Session(engine) as session:
        return session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).one()


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"
    assert overview.checked_at


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "invalid"
    assert overview.checked_at


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = subscription.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_subscription_status_prefers_wham_usage_plan(monkeypatch):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(data={"plan_type": "free", "orgs": {"data": []}})
        return _Resp(data={"plan_type": "plus"})

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {"access_token": "token", "cookies": "", "id_token": "", "extra": {}},
    )()

    details = subscription.fetch_subscription_status_details(account)

    assert details["status"] == "plus"
    assert details["usage"]["plan_type"] == "plus"


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}


def test_chatgpt_check_valid_uses_usage_plan_and_masks_phone(monkeypatch):
    def _fake_status(account, proxy=None):
        return {
            "status": "plus",
            "source": "backend-api/me",
            "me": {"email": "phone@test.com", "phone_number": "+56996830313"},
            "usage": {"plan_type": "plus"},
        }

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {"access_token": "token", "id_token": "", "cookies": ""},
        },
    )()

    assert plugin.check_valid(account) is True
    overview = plugin.get_last_check_overview()
    assert overview["plan"] == "plus"
    assert overview["plan_state"] == "subscribed"
    assert overview["phone_bound"] is True
    assert overview["phone_number_masked"] == "+569****0313"
    assert "已绑手机" in overview["chips"]


def test_chatgpt_query_state_uses_account_id_for_subscription(monkeypatch):
    captured: dict[str, str] = {}

    def _fake_profile(access_token, proxy=None):
        return True, {"email": "state@test.com"}

    def _fake_status(account, proxy=None):
        captured["account_id"] = getattr(account, "chatgpt_account_id", "")
        return {
            "status": "plus",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "plus"},
        }

    monkeypatch.setattr("platforms.chatgpt.switch._fetch_profile", _fake_profile)
    monkeypatch.setattr("platforms.chatgpt.switch.read_current_codex_account", lambda: {})
    monkeypatch.setattr("platforms.chatgpt.switch.get_codex_desktop_state", lambda: {"available": True})
    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "user_id": "fallback-account-id",
            "extra": {
                "access_token": "token",
                "account_id": "acct-real",
                "id_token": "id-token",
                "session_token": "session-token",
                "cookies": "",
            },
        },
    )()

    result = plugin._handle_query_state(account, {})

    assert result["ok"] is True
    assert captured["account_id"] == "acct-real"
    assert result["data"]["subscription_status"] == "plus"
    assert result["data"]["chatgpt_usage"]["plan_type"] == "plus"
