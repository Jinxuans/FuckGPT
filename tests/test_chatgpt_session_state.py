import json

from platforms.chatgpt import session_state as ss
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.relogin import RELOGIN_CREDENTIAL_KEYS


class _FakeContext:
    def __init__(self, state=None):
        self._state = state or {"cookies": [], "origins": []}
        self.added = []

    def storage_state(self, **kwargs):
        return self._state

    def cookies(self):
        return list(self._state.get("cookies") or [])

    def add_cookies(self, cookies):
        self.added.extend(cookies)


class _FakePage:
    def __init__(self, state=None):
        self.context = _FakeContext(state)
        self.url = "https://chatgpt.com/"
        self.goto_calls = []
        self.local_storage_sets = []

    def goto(self, url, **kwargs):
        self.url = url
        self.goto_calls.append((url, kwargs))
        return None

    def evaluate(self, script, arg=None):
        if "localStorage.setItem" in script:
            self.local_storage_sets.append(arg)
            return None
        if "Object.keys(window.localStorage" in script:
            return [{"name": "feature", "value": "on"}]
        if "fetch(sessionUrl" in script:
            return {
                "status": 200,
                "url": "https://chatgpt.com/api/auth/session",
                "text": json.dumps({"accessToken": "access", "user": {"email": "a@test.com"}}),
            }
        return None


def test_save_browser_state_writes_storage_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "BROWSER_STATE_DIR", tmp_path)
    page = _FakePage(
        {
            "cookies": [{"name": "__Secure-next-auth.session-token", "value": "session", "domain": "chatgpt.com", "path": "/"}],
            "origins": [{"origin": "https://chatgpt.com", "localStorage": [{"name": "k", "value": "v"}]}],
        }
    )

    result = ss.save_browser_state(page, email="user@example.com", account_id="acct-1")

    path = ss.resolve_browser_state_path(result["browser_state_path"])
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cookies"][0]["name"] == "__Secure-next-auth.session-token"
    assert result["browser_state_cookie_count"] == 1
    assert result["browser_state_origin_count"] == 1
    assert len(result["browser_state_sha256"]) == 64


def test_seed_browser_state_restores_cookies_and_local_storage(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "oai-did", "value": "device", "domain": "chatgpt.com", "path": "/"}],
                "origins": [{"origin": "https://chatgpt.com", "localStorage": [{"name": "k", "value": "v"}]}],
            }
        ),
        encoding="utf-8",
    )
    page = _FakePage()

    result = ss.seed_browser_state(page, {"browser_state_path": str(state_path)})

    assert result["seeded"] is True
    assert result["source"] == "browser_state"
    assert page.context.added[0]["name"] == "oai-did"
    assert page.local_storage_sets == [[{"name": "k", "value": "v"}]]


def test_seed_browser_state_falls_back_to_account_cookies():
    page = _FakePage()

    result = ss.seed_browser_state(
        page,
        {
            "session_token": "session",
            "cookies": "oai-did=device; other=value",
        },
    )

    assert result["seeded"] is True
    assert result["source"] == "account_cookies"
    names = {item["name"] for item in page.context.added}
    assert "__Secure-next-auth.session-token" in names
    assert "oai-did" in names


def test_verify_chatgpt_session_reports_restored_session():
    page = _FakePage()

    result = ss.verify_chatgpt_session(page)

    assert result["ok"] is True
    assert result["access_token_present"] is True
    assert result["remote_email"] == "a@test.com"


def test_chatgpt_actions_include_launch_browser():
    actions = ChatGPTPlatform().get_platform_actions()
    launch = next(item for item in actions if item["id"] == "launch_browser")

    assert launch["label"] == "启动浏览器"
    assert launch["sync"] is True
    assert any(param["key"] == "browser_mode" for param in launch["params"])
    assert any(param["key"] == "platform_proxy_mode" for param in launch["params"])


def test_relogin_persists_browser_state_keys():
    assert "browser_state_path" in RELOGIN_CREDENTIAL_KEYS
    assert "browser_state_sha256" in RELOGIN_CREDENTIAL_KEYS
