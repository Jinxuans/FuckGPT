from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from core.base_sms import SmsActivation
from core.base_platform import Account, RegisterConfig
from infrastructure.platform_runtime import (
    PERSISTED_ACTION_DATA_KEYS,
    _safe_action_result_data,
)
from core.account_graph import PLATFORM_CREDENTIAL_TYPES
from platforms.chatgpt.codex_oauth import (
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    PKCECodes,
    build_codex_authorize_url,
    _drive_codex_oauth_page,
    _phone_input_contains,
    _select_text_message_delivery,
)
from platforms.chatgpt.plugin import ChatGPTPlatform, _CodexSmsPhoneCallback


def test_codex_oauth_authorize_url_matches_cli_proxy_api_flow():
    url = build_codex_authorize_url(
        "state-test",
        PKCECodes(code_verifier="verifier-test", code_challenge="challenge-test"),
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == [CODEX_CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [CODEX_REDIRECT_URI]
    assert query["scope"] == [CODEX_SCOPE]
    assert query["state"] == ["state-test"]
    assert query["code_challenge"] == ["challenge-test"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["login"]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]


def test_chatgpt_codex_oauth_action_uses_browser_login_flow(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {
            "message": "Codex OAuth 授权完成",
            "codex_auth_path": "data/codex_auths/codex-test.json",
            "codex_access_token": "access-token",
            "codex_refresh_token": "refresh-token",
            "codex_id_token": "id-token",
        }

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {},
    )

    assert result["ok"] is True
    assert captured["email"] == "user@example.com"
    assert captured["password"] == "Secret123!"
    assert captured["headless"] is False
    assert "phone_callback" in captured
    assert result["data"]["codex_access_token"] == "access-token"


def test_chatgpt_codex_oauth_action_can_run_headless(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"codex_access_token": "access-token"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(platform="chatgpt", email="user@example.com", password="Secret123!"),
        {"browser_mode": "headless"},
    )

    assert result["ok"] is True
    assert captured["headless"] is True


def test_codex_oauth_add_phone_uses_phone_callback(monkeypatch):
    calls = []

    class Event:
        def is_set(self):
            return any(item[0] == "phone" for item in calls)

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/add-phone"

    def fake_goto(page, url, **kwargs):
        calls.append(("goto", url))

    states = iter(
        [
            {"page_type": "add_phone"},
            {"page_type": "oauth_callback"},
        ]
    )

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", fake_goto)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: next(states))
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._handle_add_phone_challenge",
        lambda page, phone_callback, **kwargs: calls.append(("phone", phone_callback())),
    )

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=None,
        phone_callback=lambda: "+15555550123",
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert calls == [
        ("goto", "https://auth.openai.com/oauth/authorize?state=state-test"),
        ("phone", "+15555550123"),
    ]


def test_phone_input_contains_accepts_formatted_local_number():
    class Page:
        def evaluate(self, script, selector):
            assert selector == 'input[type="tel"]'
            return "+56 9 7123 4527"

    ok, actual = _phone_input_contains(Page(), 'input[type="tel"]', "971234527")

    assert ok is True
    assert actual == "+56 9 7123 4527"


def test_select_text_message_delivery_prefers_sms_over_whatsapp(monkeypatch):
    clicked = []

    class Page:
        def evaluate(self, script):
            clicked.append("Text Message")
            return True

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", lambda *args, **kwargs: None)

    assert _select_text_message_delivery(Page(), lambda message: None) is True
    assert clicked == ["Text Message"]


def test_codex_sms_phone_callback_retries_when_no_numbers(monkeypatch):
    sleeps = []

    class Provider:
        request_timeout = 1
        poll_interval = 0

        def __init__(self):
            self.calls = 0

        def get_number(self, service="", country=""):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("SMSBower getNumber失败: 当前服务/国家暂无号码 (NO_NUMBERS)")
            return SmsActivation(
                activation_id="activation-1",
                phone_number="+15555550123",
                provider="smsbower",
                service=service,
                country=country,
            )

    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: sleeps.append(seconds))
    logs = []
    provider = Provider()
    callback = _CodexSmsPhoneCallback(
        provider,
        service="go",
        country="0",
        log_fn=logs.append,
        buy_max_attempts=5,
        buy_retry_interval=2,
    )

    assert callback() == "+15555550123"
    assert provider.calls == 3
    assert sleeps == [2, 2]
    assert any("接码暂无号码" in item for item in logs)


def test_codex_phone_callback_uses_smsbower_retry_settings(monkeypatch):
    class Repo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smsbower"

        def resolve_runtime_settings(self, provider_type, provider_key, extra):
            assert provider_type == "sms"
            assert provider_key == "smsbower"
            return {
                "smsbower_api_key": "secret-key",
                "smsbower_default_service": "go",
                "smsbower_default_country": "0",
                "smsbower_buy_max_attempts": "7",
                "smsbower_buy_retry_interval": "1.5",
            }

    class Client:
        api_key = "secret-key"
        default_service = "go"
        default_country = "0"

    monkeypatch.setattr("infrastructure.provider_settings_repository.ProviderSettingsRepository", Repo)
    monkeypatch.setattr("core.smsbower_sms.SMSBowerClient.from_config", classmethod(lambda cls, config: Client()))

    callback = ChatGPTPlatform(config=RegisterConfig())._build_codex_phone_callback(proxy=None)

    assert isinstance(callback, _CodexSmsPhoneCallback)
    assert callback.buy_max_attempts == 7
    assert callback.buy_retry_interval == 1.5


def test_codex_oauth_result_is_persistable_but_returned_safely():
    assert "codex_access_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_refresh_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_id_token" in PERSISTED_ACTION_DATA_KEYS
    assert PLATFORM_CREDENTIAL_TYPES["codex_access_token"] == "token"
    assert PLATFORM_CREDENTIAL_TYPES["codex_refresh_token"] == "token"
    assert PLATFORM_CREDENTIAL_TYPES["codex_id_token"] == "token"

    safe = _safe_action_result_data(
        "codex_oauth_authorize",
        {
            "codex_access_token": "access-token-very-secret",
            "codex_refresh_token": "refresh-token-very-secret",
            "codex_id_token": "id-token-very-secret",
            "codex_auth_path": "data/codex_auths/codex-test.json",
        },
    )

    assert "codex_access_token" not in safe
    assert "codex_refresh_token" not in safe
    assert "codex_id_token" not in safe
    assert safe["codex_access_token_preview"] == "access...cret"
    assert safe["codex_auth_path"] == "data/codex_auths/codex-test.json"


def test_chatgpt_registration_result_keeps_post_codex_oauth_data():
    mapped = ChatGPTPlatform(config=RegisterConfig())._map_chatgpt_result(
        {
            "email": "user@example.com",
            "password": "Secret123!",
            "access_token": "chatgpt-access",
            "codex_access_token": "codex-access",
            "codex_refresh_token": "codex-refresh",
            "codex_id_token": "codex-id",
            "codex_auth_path": "data/codex_auths/codex-test.json",
            "post_codex_oauth": {"ok": True},
        }
    )

    assert mapped.extra["access_token"] == "chatgpt-access"
    assert mapped.extra["codex_access_token"] == "codex-access"
    assert mapped.extra["codex_refresh_token"] == "codex-refresh"
    assert mapped.extra["codex_id_token"] == "codex-id"
    assert mapped.extra["codex_auth_path"] == "data/codex_auths/codex-test.json"
    assert mapped.extra["post_codex_oauth"] == {"ok": True}
