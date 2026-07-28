from __future__ import annotations

import json
import sys
import types

from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_API_ENDPOINTS, SENTINEL_REQ_URL
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_register import (
    ChatGPTProtocolRegister,
    OpenAISentinelClient,
    _SentinelBrowserRuntime,
)
from platforms.chatgpt.browser_register import (
    OTP_SUBMIT_SELECTORS,
    _browser_registration_flow,
    _click_first,
    _click_otp_submit_button,
    _extract_auth_error_text,
    _is_login_password_url,
    _press_enter_on_input,
    _probe_password_registration_page,
    _start_browser_signup_via_authorize,
    _submit_password_via_page,
    _submit_otp_via_page,
)


class _FakeCookies:
    def get(self, key):
        return "device-from-cookie" if key == "oai-did" else None

    def get_dict(self):
        return {"oai-did": "device-from-cookie"}


def test_login_password_url_recognizes_password_form_on_log_in_root():
    assert _is_login_password_url("https://auth.openai.com/log-in") is True
    assert _is_login_password_url("https://auth.openai.com/log-in/password") is True
    assert _is_login_password_url("https://auth.openai.com/create-account/password") is False


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.password_body = {}
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == f"{CHATGPT_APP}/api/auth/csrf":
            return _FakeResponse(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.openai.com/authorize-start":
            return _FakeResponse(headers={"location": "/email-verification"})
        if url == f"{CHATGPT_APP}/api/auth/session":
            return _FakeResponse(
                payload={
                    "accessToken": "header.payload.signature",
                    "sessionToken": "session-token",
                    "expires": "2026-08-01T00:00:00Z",
                    "account": {"id": "account-123", "planType": "free"},
                }
            )
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
            return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            assert kwargs["json"] == {"code": "123456"}
            return _FakeResponse(payload={"continue_url": "/create-account/password"})
        if url == SENTINEL_REQ_URL:
            request_payload = json.loads(kwargs["data"])
            return _FakeResponse(
                payload={
                    "token": "challenge-token",
                    "proofofwork": {"required": False},
                    "flow": request_payload["flow"],
                }
            )
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            self.create_headers = kwargs["headers"]
            return _FakeResponse(
                payload={
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok&state=test"
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(payload={"continue_url": "/about-you"})
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def test_browser_registration_retries_about_you_after_no_navigation(monkeypatch):
    logs = []
    responses = iter(
        [
            {"ok": False, "status": 0, "url": "https://auth.openai.com/about-you", "text": "about_you 提交后未跳转"},
            {"ok": True, "status": 200, "url": "https://chatgpt.com/", "text": ""},
        ]
    )

    class Page:
        url = "https://auth.openai.com/about-you"

        def __init__(self):
            self.reloads = 0

        def reload(self, **kwargs):
            self.reloads += 1

    page = Page()

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: {"page_type": "about_you", "current_url": "https://auth.openai.com/about-you"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._submit_about_you_via_page", lambda page, log: next(responses))
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_flow_state", lambda data, url: {"page_type": "chatgpt_home", "current_url": url})
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)

    result = _browser_registration_flow(page, "user@example.com", "Secret123!", lambda: "123456", logs.append)

    assert result["page_type"] == "chatgpt_home"
    assert page.reloads == 1
    assert any("about_you 提交后观察 20 秒仍确认停留当前页" in item for item in logs)


def test_browser_registration_falls_back_to_visible_page_after_nextauth_failure(monkeypatch):
    calls = []
    logs = []

    class Page:
        url = "https://chatgpt.com/"

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing authorize URL")),
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._derive_registration_state_from_page", lambda page: {})
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_page",
        lambda page, email, log: calls.append(("visible", email)) or {"page_type": "chatgpt_home"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._extract_flow_state",
        lambda data, url: {"page_type": "chatgpt_home", "current_url": url},
    )

    result = _browser_registration_flow(Page(), "user@example.com", "Secret123!", lambda: "123456", logs.append)

    assert result["page_type"] == "chatgpt_home"
    assert calls == [("visible", "user@example.com")]
    assert any("改用可见 OpenAI 注册页面" in item for item in logs)


def test_probe_password_registration_page_uses_active_signup_session(monkeypatch):
    calls = []
    logs = []

    class Page:
        url = "https://auth.openai.com/email-verification"

    page = Page()

    def fake_goto(page, url, **kwargs):
        calls.append(url)
        page.url = url

    monkeypatch.setattr("platforms.chatgpt.browser_register._goto_with_retry", fake_goto)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "create_account_password", "current_url": page.url},
    )

    result = _probe_password_registration_page(
        page,
        {"page_type": "email_otp_verification", "current_url": page.url},
        logs.append,
    )

    assert result["page_type"] == "create_account_password"
    assert calls == ["https://auth.openai.com/create-account/password"]
    assert any("主动密码注册探测成功" in item for item in logs)


def test_signup_authorize_retries_csrf_without_repeating_email_submission(monkeypatch):
    csrf_values = iter(["", "", "csrf-token"])
    calls = []
    logs = []

    class Page:
        url = "https://chatgpt.com/"

    monkeypatch.setattr("platforms.chatgpt.browser_register._goto_with_retry", lambda *args, **kwargs: calls.append("goto"))
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: next(csrf_values))
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signin",
        lambda page, email, device_id, csrf: calls.append(("signin", email, csrf)) or "https://auth.openai.com/authorize",
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._browser_authorize",
        lambda page, url, log: calls.append(("authorize", url)) or "https://auth.openai.com/email-verification",
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "email_otp_verification"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: calls.append(("sleep", seconds)))

    result = _start_browser_signup_via_authorize(Page(), "user@example.com", "device-id", logs.append)

    assert result["page_type"] == "email_otp_verification"
    assert [item for item in calls if isinstance(item, tuple) and item[0] == "signin"] == [
        ("signin", "user@example.com", "csrf-token")
    ]
    assert [item for item in calls if isinstance(item, tuple) and item[0] == "sleep"] == [
        ("sleep", 1.5),
        ("sleep", 3.0),
    ]
    assert sum("CSRF token 瞬时获取失败" in item for item in logs) == 2


def test_password_submit_observes_slow_proxy_transition_without_resubmitting(monkeypatch):
    now = {"value": 1000.0}
    calls = []
    logs = []

    class Page:
        url = "https://auth.openai.com/create-account/password"

    monkeypatch.setattr("platforms.chatgpt.browser_register._recover_signup_password_page", lambda *args: False)
    monkeypatch.setattr("platforms.chatgpt.browser_register._wait_for_any_selector", lambda *args, **kwargs: 'input[type="password"]')
    monkeypatch.setattr("platforms.chatgpt.browser_register._fill_input_like_user", lambda *args: calls.append("fill") or True)
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_first", lambda *args, **kwargs: calls.append("click") or 'button[type="submit"]')
    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_pause", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_auth_error_text", lambda page: "")
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "email_otp_verification" if now["value"] >= 1025.0 else "create_account_password"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    result = _submit_password_via_page(Page(), "Secret123!", logs.append)

    assert result["ok"] is True
    assert calls == ["fill", "click"]
    assert sum("密码提交后 20 秒仍无跳转" in item for item in logs) == 1


def test_click_first_skips_hidden_and_disabled_matches():
    clicked = []

    class Target:
        def __init__(self, *, visible=True, enabled=True, name=""):
            self.visible = visible
            self.enabled = enabled
            self.name = name

        def is_visible(self, **kwargs):
            return self.visible

        def is_enabled(self, **kwargs):
            return self.enabled

        def click(self, **kwargs):
            clicked.append(self.name)

    class Locator:
        def __init__(self, targets):
            self.targets = targets

        def count(self):
            return len(self.targets)

        def nth(self, index):
            return self.targets[index]

    class Page:
        def locator(self, selector):
            assert selector == 'button[type="submit"]'
            return Locator(
                [
                    Target(visible=False, name="hidden"),
                    Target(enabled=False, name="disabled"),
                    Target(name="enabled"),
                ]
            )

    selector = _click_first(Page(), ['button[type="submit"]'], timeout=1)

    assert selector == 'button[type="submit"] nth=2'
    assert clicked == ["enabled"]


def test_press_enter_on_input_uses_real_locator_keyboard_event():
    actions = []

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            actions.append("click")

        def press(self, key, **kwargs):
            actions.append(("press", key))

    class Locator:
        first = Target()

    class Page:
        def locator(self, selector):
            assert selector == 'input[type="email"]'
            return Locator()

    assert _press_enter_on_input(Page(), 'input[type="email"]') is True
    assert actions == ["click", ("press", "Enter")]


def test_press_enter_on_input_focuses_when_pointer_click_is_intercepted():
    actions = []

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            raise RuntimeError("pointer intercepted")

        def focus(self, **kwargs):
            actions.append("focus")

        def press(self, key, **kwargs):
            actions.append(("press", key))

    class Locator:
        first = Target()

    class Page:
        def locator(self, selector):
            return Locator()

    assert _press_enter_on_input(Page(), "input[autocomplete='one-time-code']") is True
    assert actions == ["focus", ("press", "Enter")]


def test_otp_submit_selectors_prioritize_japanese_continue_over_generic_submit():
    japanese_continue = 'button:text-is("続行")'

    assert japanese_continue in OTP_SUBMIT_SELECTORS
    assert OTP_SUBMIT_SELECTORS.index(japanese_continue) < OTP_SUBMIT_SELECTORS.index('button[type="submit"]')


def test_extract_auth_error_text_recognizes_japanese_expired_code():
    class Target:
        def __init__(self, text=""):
            self.text = text

        @property
        def first(self):
            return self

        def text_content(self, **kwargs):
            return self.text

        def inner_text(self, **kwargs):
            return self.text

    class Page:
        def locator(self, selector):
            if selector == "body":
                return Target("コードの有効期限が切れています。メールを再送信してください。")
            return Target("")

    assert _extract_auth_error_text(Page()) == "コードの有効期限が切れ"


def test_click_otp_submit_button_skips_hidden_submit():
    clicks = []

    class Button:
        def __init__(self, *, visible, enabled=True):
            self.visible = visible
            self.enabled = enabled

        def is_visible(self, **kwargs):
            return self.visible

        def is_enabled(self, **kwargs):
            return self.enabled

        def click(self, **kwargs):
            clicks.append(kwargs.get("timeout"))

    class Locator:
        def __init__(self, buttons):
            self.buttons = buttons

        def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

    class Page:
        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button(visible=False), Button(visible=True)])
            return Locator([])

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=1'
    assert clicks == [3000]


def test_click_otp_submit_button_skips_oauth_provider_submit():
    clicks = []

    class Button:
        def __init__(self, text):
            self.text = text

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def evaluate(self, script):
            return f"BUTTON|||submit|{self.text}"

        def click(self, **kwargs):
            clicks.append(self.text)

    class Locator:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button("Google で続行"), Button("続行")])
            return Locator([])

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=1'
    assert clicks == ["続行"]


def test_click_otp_submit_button_uses_keyboard_when_pointer_click_is_intercepted(monkeypatch):
    now = {"value": 1000.0}
    actions = []

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            raise RuntimeError("pointer intercepted")

        def focus(self, **kwargs):
            actions.append("focus")

        def press(self, key, **kwargs):
            actions.append(("press", key))
            self.page.url = "https://auth.openai.com/about-you"

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            return Button(self.page)

    class Page:
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            return Locator(self, selector == 'button:text-is("続行")')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button:text-is("続行") nth=0 keyboard Enter'
    assert actions == ["focus", ("press", "Enter")]


def test_click_otp_submit_button_uses_real_mouse_after_confirmed_hit_test(monkeypatch):
    now = {"value": 1000.0}
    actions = []

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def evaluate(self, script):
            if "join('|')" in script:
                return "BUTTON|||continue|続行"
            return True

        def bounding_box(self):
            return {"x": 10, "y": 20, "width": 100, "height": 40}

        def click(self, **kwargs):
            raise RuntimeError("pointer actionability timeout")

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            return Button(self.page)

    class Mouse:
        def __init__(self, page):
            self.page = page

        def move(self, x, y, **kwargs):
            actions.append(("move", x, y))

        def click(self, x, y):
            actions.append(("click", x, y))
            self.page.url = "https://auth.openai.com/about-you"

    class Page:
        url = "https://auth.openai.com/email-verification"

        def __init__(self):
            self.mouse = Mouse(self)

        def locator(self, selector):
            return Locator(self, selector == 'button:text-is("続行")')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button:text-is("続行") nth=0 real mouse'
    assert actions == [("move", 60.0, 40.0), ("click", 60.0, 40.0)]


def test_click_otp_submit_button_treats_timeout_then_navigation_as_progress(monkeypatch):
    now = {"value": 1000.0}

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            self.page.url = "https://auth.openai.com/about-you"
            raise RuntimeError("Locator.click: Timeout 3000ms exceeded")

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            return Button(self.page)

    class Page:
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            return Locator(self, selector == 'button[type="submit"]')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=0 delayed'


def test_submit_otp_keyboard_refill_after_dom_fallback_enables_submit(monkeypatch):
    logs = []
    now = {"value": 1000.0}

    class Input:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            return None

        def click(self, **kwargs):
            return None

        def fill(self, value):
            self.page.otp_value = value

        def type(self, value, **kwargs):
            raise RuntimeError("force DOM fallback")

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def input_value(self):
            return self.page.otp_value if self.page.dom_fallback_started else ""

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return self.page.keyboard_refilled

        def click(self, **kwargs):
            self.page.url = "https://auth.openai.com/about-you"

    class Locator:
        def __init__(self, items):
            self.items = items

        @property
        def first(self):
            return self.nth(0)

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Keyboard:
        def __init__(self, page):
            self.page = page

        def press(self, key):
            if key == "Backspace":
                self.page.otp_value = ""

        def type(self, value, **kwargs):
            self.page.otp_value = value
            self.page.keyboard_refilled = True

    class Page:
        url = "https://auth.openai.com/email-verification"

        def __init__(self):
            self.otp_value = ""
            self.dom_fallback_started = False
            self.keyboard_refilled = False
            self.keyboard = Keyboard(self)

        def wait_for_load_state(self, **kwargs):
            return None

        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button(self)])
            if selector.startswith("input") or "input[" in selector:
                return Locator([Input(self)])
            return Locator([])

        def get_by_label(self, *args, **kwargs):
            return Locator([Input(self)])

        def get_by_role(self, *args, **kwargs):
            return Locator([Input(self)])

        def evaluate(self, script, *args):
            if args:
                self.dom_fallback_started = True
                self.otp_value = str(args[0])
                return {"ok": True, "selector": 'input[autocomplete="one-time-code"]', "value": self.otp_value}
            return {"buttons": [{"text": "Continue", "visible": True, "disabled": True}], "inputs": [{"visible": True, "disabled": False, "readOnly": False, "valueLength": len(self.otp_value)}]}

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_pause", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_auth_error_text", lambda page: "")

    result = _submit_otp_via_page(Page(), "112450", logs.append)

    assert result["ok"] is True
    assert result["status"] == 200
    assert any("DOM fallback 后提交按钮不可点击" in item for item in logs)
    assert any("键盘 fallback 重新填写输入框" in item for item in logs)


def test_protocol_register_completes_email_flow_without_browser():
    session = _FakeSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
        sentinel_runtime=False,
    )

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["email"] == "user@outlook.com"
    assert result["password"] == "StrongPass123!"
    assert result["access_token"] == "header.payload.signature"
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "account-123"
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    assert any("协议注册完成" in line for line in logs)


def test_protocol_registration_accepts_current_chatgpt_otp_subjects():
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()

    # Current messages are titled "Your temporary ChatGPT ... code" and may
    # not contain the old OpenAI brand keyword.
    assert adapter.otp_spec is not None
    assert adapter.otp_spec.keyword == ""


def test_sentinel_headers_include_vm_and_session_observer_tokens():
    class _FakeRuntime:
        def vm_tokens(self, chat_req, cached_proof):
            return {"t": "turnstile-proof", "so": "observer-proof"}

    client = OpenAISentinelClient(
        session=object(),
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    client._browser_runtime = _FakeRuntime()
    client.session = type(
        "NoNetworkSession",
        (),
        {"post": lambda *args, **kwargs: None},
    )()

    # Bypass the network challenge and exercise the header assembly using a
    # deterministic VM result.
    def fake_post(*args, **kwargs):
        return _FakeResponse(
            payload={
                "token": "challenge",
                "proofofwork": {"required": False},
            }
        )

    client.session.post = fake_post
    headers = client.build_headers("device-1", "oauth_create_account")
    assert set(headers) == {
        "openai-sentinel-token",
        "openai-sentinel-so-token",
    }
    token = json.loads(headers["openai-sentinel-token"])
    so_token = json.loads(headers["openai-sentinel-so-token"])
    assert token["t"] == "turnstile-proof"
    assert so_token["so"] == "observer-proof"


def test_sentinel_runtime_uses_camoufox_and_releases_it(monkeypatch):
    events = []

    class _Page:
        def goto(self, *_args, **_kwargs):
            events.append("goto")

        def evaluate(self, expression, *_args):
            if expression == "typeof window.SentinelSDK":
                return "object"
            return None

    class _Browser:
        def new_page(self):
            return _Page()

    class _Camoufox:
        options = None

        def __init__(self, **options):
            type(self).options = options

        def __enter__(self):
            events.append("enter")
            return _Browser()

        def __exit__(self, *_args):
            events.append("exit")

    class _Session:
        def get(self, *_args, **_kwargs):
            return _FakeResponse(text="before t.token=ye,t}({}); after")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )
    monkeypatch.setattr(_SentinelBrowserRuntime, "_sdk_code", None)

    runtime = _SentinelBrowserRuntime.create(
        _Session(),
        user_agent="unused-by-camoufox",
        proxy="http://name:pass@127.0.0.1:8080",
    )
    assert _Camoufox.options["headless"] is True
    assert _Camoufox.options["block_webrtc"] is True
    assert _Camoufox.options["proxy"] == {
        "server": "http://127.0.0.1:8080",
        "username": "name",
        "password": "pass",
    }

    runtime.close()
    runtime.close()
    assert events.count("enter") == 1
    assert events.count("exit") == 1


def test_sentinel_runtime_releases_failed_camoufox_startup(monkeypatch):
    events = []

    class _Camoufox:
        def __init__(self, **_options):
            pass

        def __enter__(self):
            events.append("enter")
            raise RuntimeError("Camoufox startup failed")

        def __exit__(self, *_args):
            events.append("exit")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )

    try:
        _SentinelBrowserRuntime.create(
            object(), user_agent="unused-by-camoufox", proxy=None
        )
    except RuntimeError as exc:
        assert str(exc) == "Camoufox startup failed"
    else:
        raise AssertionError("expected Camoufox startup error")

    assert events == ["enter", "exit"]
