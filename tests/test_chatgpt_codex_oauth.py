from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from core.base_platform import Account, RegisterConfig
from infrastructure.platform_runtime import (
    PERSISTED_ACTION_DATA_KEYS,
    _safe_action_result_data,
)
from platforms.chatgpt.codex_oauth import (
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    PKCECodes,
    build_codex_authorize_url,
)
from platforms.chatgpt.plugin import ChatGPTPlatform


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
    assert captured["headless"] is True
    assert result["data"]["codex_access_token"] == "access-token"


def test_codex_oauth_result_is_persistable_but_returned_safely():
    assert "codex_access_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_refresh_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_id_token" in PERSISTED_ACTION_DATA_KEYS

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
