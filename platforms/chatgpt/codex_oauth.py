from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from core.network_retry import retry_network_call

from .._browser_backend import BrowserBackendConfig, keep_browser_context_open, open_browser_backend
from .browser_register import (
    EMAIL_INPUT_SELECTORS,
    EMAIL_SUBMIT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    PASSWORDLESS_LOGIN_SELECTORS,
    Camoufox,
    _apply_camoufox_visible_window_limit,
    _build_proxy_config,
    _click_first,
    _click_first_no_wait,
    _derive_registration_state_from_page,
    _extract_auth_error_text,
    _fill_input_like_user,
    _goto_with_retry,
    _press_enter_on_input,
    _submit_otp_via_page,
    _submit_oauth_password_direct,
    _wait_for_any_selector,
)


CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"
CODEX_USER_AGENT = "codex_cli_rs/0.144.1 (Windows 10.0.0; x86_64)"
DEFAULT_CODEX_AUTH_DIR = Path("data") / "codex_auths"

PHONE_COUNTRY_CODE_MAP = {
    "1": "United States",
    "7": "Russia",
    "20": "Egypt",
    "27": "South Africa",
    "30": "Greece",
    "31": "Netherlands",
    "32": "Belgium",
    "33": "France",
    "34": "Spain",
    "36": "Hungary",
    "39": "Italy",
    "40": "Romania",
    "44": "United Kingdom",
    "45": "Denmark",
    "46": "Sweden",
    "47": "Norway",
    "48": "Poland",
    "49": "Germany",
    "51": "Peru",
    "52": "Mexico",
    "53": "Cuba",
    "54": "Argentina",
    "55": "Brazil",
    "56": "Chile",
    "57": "Colombia",
    "58": "Venezuela",
    "60": "Malaysia",
    "61": "Australia",
    "62": "Indonesia",
    "63": "Philippines",
    "64": "New Zealand",
    "65": "Singapore",
    "66": "Thailand",
    "81": "Japan",
    "82": "South Korea",
    "84": "Vietnam",
    "86": "China",
    "90": "Turkey",
    "91": "India",
    "92": "Pakistan",
    "93": "Afghanistan",
    "94": "Sri Lanka",
    "95": "Myanmar",
    "98": "Iran",
    "212": "Morocco",
    "213": "Algeria",
    "216": "Tunisia",
    "218": "Libya",
    "220": "Gambia",
    "221": "Senegal",
    "234": "Nigeria",
    "254": "Kenya",
    "255": "Tanzania",
    "256": "Uganda",
    "260": "Zambia",
    "263": "Zimbabwe",
    "351": "Portugal",
    "353": "Ireland",
    "354": "Iceland",
    "358": "Finland",
    "370": "Lithuania",
    "371": "Latvia",
    "372": "Estonia",
    "374": "Armenia",
    "375": "Belarus",
    "380": "Ukraine",
    "381": "Serbia",
    "385": "Croatia",
    "420": "Czech Republic",
    "421": "Slovakia",
    "855": "Cambodia",
    "856": "Laos",
    "880": "Bangladesh",
    "886": "Taiwan",
    "960": "Maldives",
    "966": "Saudi Arabia",
    "971": "United Arab Emirates",
    "972": "Israel",
    "977": "Nepal",
    "992": "Tajikistan",
    "993": "Turkmenistan",
    "994": "Azerbaijan",
    "995": "Georgia",
    "996": "Kyrgyzstan",
    "998": "Uzbekistan",
}

PHONE_DIAL_TO_ISO = {
    "1": "US",
    "7": "RU",
    "20": "EG",
    "27": "ZA",
    "30": "GR",
    "31": "NL",
    "32": "BE",
    "33": "FR",
    "34": "ES",
    "36": "HU",
    "39": "IT",
    "40": "RO",
    "44": "GB",
    "45": "DK",
    "46": "SE",
    "47": "NO",
    "48": "PL",
    "49": "DE",
    "51": "PE",
    "52": "MX",
    "53": "CU",
    "54": "AR",
    "55": "BR",
    "56": "CL",
    "57": "CO",
    "58": "VE",
    "60": "MY",
    "61": "AU",
    "62": "ID",
    "63": "PH",
    "64": "NZ",
    "65": "SG",
    "66": "TH",
    "81": "JP",
    "82": "KR",
    "84": "VN",
    "86": "CN",
    "90": "TR",
    "91": "IN",
    "92": "PK",
    "93": "AF",
    "94": "LK",
    "95": "MM",
    "98": "IR",
    "212": "MA",
    "213": "DZ",
    "216": "TN",
    "218": "LY",
    "220": "GM",
    "221": "SN",
    "234": "NG",
    "254": "KE",
    "255": "TZ",
    "256": "UG",
    "260": "ZM",
    "263": "ZW",
    "351": "PT",
    "353": "IE",
    "354": "IS",
    "358": "FI",
    "370": "LT",
    "371": "LV",
    "372": "EE",
    "374": "AM",
    "375": "BY",
    "380": "UA",
    "381": "RS",
    "385": "HR",
    "420": "CZ",
    "421": "SK",
    "855": "KH",
    "856": "LA",
    "880": "BD",
    "886": "TW",
    "960": "MV",
    "966": "SA",
    "971": "AE",
    "972": "IL",
    "977": "NP",
    "992": "TJ",
    "993": "TM",
    "994": "AZ",
    "995": "GE",
    "996": "KG",
    "998": "UZ",
}

PHONE_INPUT_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="phone_number"]',
    'input[name="phoneNumber"]',
    'input[id*="phone" i]',
    'input[placeholder*="phone" i]',
    'input[autocomplete="tel"]',
    'input[autocomplete="tel-national"]',
]

PHONE_SEND_SELECTORS = [
    'button:has-text("Send code via SMS")',
    'button:has-text("Send code")',
    'button:has-text("Send via SMS")',
    'button:has-text("Send link via SMS")',
    'button:has-text("Send")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("发送")',
    'input[type="submit"]',
]

PHONE_STRUCTURAL_SUBMIT_SELECTOR = (
    'button[type="submit"], input[type="submit"], '
    'form button:not([type]), button[form]:not([type])'
)

PHONE_TEXT_MESSAGE_SELECTORS = [
    'label:has-text("Text Message")',
    'button:has-text("Text Message")',
    '[role="radio"]:has-text("Text Message")',
    '[role="option"]:has-text("Text Message")',
    'label:has-text("SMS")',
    'button:has-text("SMS")',
    '[role="radio"]:has-text("SMS")',
    '[role="option"]:has-text("SMS")',
    'label:has-text("短信")',
    'button:has-text("短信")',
    '[role="radio"]:has-text("短信")',
    '[role="option"]:has-text("短信")',
]

PHONE_WHATSAPP_SELECTORS = [
    'label:has-text("WhatsApp")',
    'button:has-text("WhatsApp")',
    '[role="radio"]:has-text("WhatsApp")',
    '[role="option"]:has-text("WhatsApp")',
]

ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS = 15
# A real proxied login took about 35 seconds to leave the email page. Keep the
# first submission in observation mode long enough to avoid sending it twice.
EMAIL_SUBMIT_GRACE_SECONDS = 45
PASSWORDLESS_LOGIN_GRACE_SECONDS = 20
CODEX_BROWSER_LOGIN_MAX_ATTEMPTS = 2
CODEX_CONSENT_STALL_RECOVERY_THRESHOLD = 3
CODEX_CONSENT_STALL_MAX_RECOVERIES = 2


@dataclass(slots=True)
class PKCECodes:
    code_verifier: str
    code_challenge: str


@dataclass(slots=True)
class _CodexOAuthAttempt:
    pkce: PKCECodes
    state: str
    auth_url: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mask_secret(value: str) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return "***" if value else ""
    return f"{value[:6]}...{value[-4:]}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce_codes() -> PKCECodes:
    verifier = _b64url(secrets.token_bytes(96))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCECodes(code_verifier=verifier, code_challenge=challenge)


def _new_codex_oauth_attempt() -> _CodexOAuthAttempt:
    pkce = generate_pkce_codes()
    state = secrets.token_urlsafe(32)
    return _CodexOAuthAttempt(pkce=pkce, state=state, auth_url=build_codex_authorize_url(state, pkce))


def build_codex_authorize_url(state: str, pkce: PKCECodes) -> str:
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{CODEX_AUTH_URL}?{urlencode(params)}"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _token_identity(id_token: str) -> dict[str, str]:
    claims = _decode_jwt_payload(id_token)
    auth = claims.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    profile = claims.get("https://api.openai.com/profile")
    if not isinstance(profile, dict):
        profile = {}
    return {
        "email": str(claims.get("email") or profile.get("email") or "").strip(),
        "account_id": str(auth.get("chatgpt_account_id") or "").strip(),
        "chatgpt_user_id": str(
            auth.get("chatgpt_user_id")
            or auth.get("chatgpt_account_user_id")
            or auth.get("user_id")
            or ""
        ).strip(),
        "plan_type": str(auth.get("chatgpt_plan_type") or "unknown").strip() or "unknown",
    }


def _normalize_filename_part(value: str, fallback: str = "unknown") -> str:
    cleaned = []
    prev_dash = False
    for char in str(value or "").strip().lower():
        if char.isalnum():
            cleaned.append(char)
            prev_dash = False
        elif not prev_dash:
            cleaned.append("-")
            prev_dash = True
    return "".join(cleaned).strip("-") or fallback


def codex_credential_filename(email: str, plan_type: str, account_id: str) -> str:
    account_hash = ""
    if account_id:
        account_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:8]
    email_part = _normalize_filename_part(email, "account")
    plan_part = _normalize_filename_part(plan_type, "")
    if account_hash and plan_part:
        return f"codex-{account_hash}-{email_part}-{plan_part}.json"
    if account_hash:
        return f"codex-{account_hash}-{email_part}.json"
    if plan_part:
        return f"codex-{email_part}-{plan_part}.json"
    return f"codex-{email_part}.json"


class _OAuthCallbackServer:
    def __init__(
        self,
        *,
        port: int = CODEX_CALLBACK_PORT,
        state: str = "",
        log: Callable[[str], None] | None = None,
    ):
        self.port = int(port)
        self.event = threading.Event()
        self.result: dict[str, str] = {}
        self.state = str(state or "")
        self.log = log or (lambda _message: None)

    def __enter__(self) -> "_OAuthCallbackServer":
        _oauth_callback_broker.register(self)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        _oauth_callback_broker.unregister(self)

    def wait(self, timeout: int) -> dict[str, str]:
        if not self.event.wait(max(int(timeout), 1)):
            raise RuntimeError("等待 Codex OAuth 回调超时")
        return dict(self.result)


class _OAuthCallbackBroker:
    def __init__(self, *, port: int = CODEX_CALLBACK_PORT):
        self.port = int(port)
        self._lock = threading.RLock()
        self._waiters: dict[str, _OAuthCallbackServer] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def register(self, waiter: _OAuthCallbackServer) -> None:
        if not waiter.state:
            waiter.state = secrets.token_urlsafe(16)
        with self._lock:
            self._ensure_started()
            self._waiters[waiter.state] = waiter

    def unregister(self, waiter: _OAuthCallbackServer) -> None:
        with self._lock:
            if self._waiters.get(waiter.state) is waiter:
                self._waiters.pop(waiter.state, None)

    def deliver(self, result: dict[str, str]) -> bool:
        state = str(result.get("state") or "")
        log: Callable[[str], None] | None = None
        with self._lock:
            waiter = self._waiters.get(state)
            if not waiter and len(self._waiters) == 1:
                waiter = next(iter(self._waiters.values()))
            if not waiter:
                return False
            first_delivery = not waiter.event.is_set()
            waiter.result = dict(result)
            waiter.event.set()
            if first_delivery:
                log = waiter.log
        if log:
            try:
                log("Codex OAuth 本地回调已到达")
            except Exception:
                pass
        return True

    def _ensure_started(self) -> None:
        if self._httpd and self._thread and self._thread.is_alive():
            return
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlparse(self.path)
                if parsed.path == "/success":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h1>Codex OAuth complete</h1><p>You can close this window.</p></body></html>"
                    )
                    return
                if parsed.path != "/auth/callback":
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                result = {
                    "code": (query.get("code") or [""])[0],
                    "state": (query.get("state") or [""])[0],
                    "error": (query.get("error") or [""])[0],
                    "error_description": (query.get("error_description") or [""])[0],
                }
                broker.deliver(result)
                self.send_response(302)
                self.send_header("Location", "/success")
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="codex-oauth-callback")
        self._thread.start()


_oauth_callback_broker = _OAuthCallbackBroker(port=CODEX_CALLBACK_PORT)


def _exchange_code_for_tokens(
    code: str,
    pkce: PKCECodes,
    *,
    proxy: str | None = None,
    timeout: int = 60,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def request_tokens():
        response = requests.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CODEX_CLIENT_ID,
                "code": code,
                "redirect_uri": CODEX_REDIRECT_URI,
                "code_verifier": pkce.code_verifier,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": CODEX_USER_AGENT,
            },
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=timeout,
        )
        if int(getattr(response, "status_code", 0) or 0) in {408, 425, 429, 500, 502, 503, 504}:
            error = requests.exceptions.HTTPError(
                f"Codex OAuth token exchange transient HTTP {response.status_code}",
                response=response,
            )
            raise error
        return response

    response = retry_network_call(
        request_tokens,
        max_attempts=4,
        base_delay=1,
        max_delay=8,
        on_retry=lambda attempt, attempts, delay, exc: (log or (lambda _message: None))(
            f"Codex OAuth token 交换遇到瞬时网络/代理异常，{delay:g}s 后自动重试 "
            f"({attempt}/{attempts}): {exc}"
        ),
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Codex OAuth token exchange 失败 HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("Codex OAuth token exchange 未返回 access_token")
    return payload


def _save_codex_auth_file(token_record: dict[str, Any], *, auth_dir: str | os.PathLike[str] | None = None) -> Path:
    identity = _token_identity(str(token_record.get("id_token") or ""))
    target_dir = Path(auth_dir or os.environ.get("CODEX_AUTH_DIR") or DEFAULT_CODEX_AUTH_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = codex_credential_filename(
        identity.get("email") or str(token_record.get("email") or ""),
        identity.get("plan_type") or "unknown",
        identity.get("account_id") or str(token_record.get("account_id") or ""),
    )
    path = target_dir / filename
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(token_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _click_continue_like_button(page, log: Callable[[str], None], context: str) -> bool:
    selector = _click_first(
        page,
        [
            'button[type="submit"]',
            'button[data-testid="continue-button"]',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Allow")',
            'button:has-text("allow")',
            'button:has-text("Authorize")',
            'button:has-text("authorize")',
            'button:has-text("Approve")',
            'button:has-text("approve")',
            'button:has-text("确认")',
            'button:has-text("继续")',
            'button:has-text("同意")',
            'button:has-text("許可")',
            'button:has-text("続ける")',
        ],
        timeout=6,
    )
    if selector:
        log(f"{context}: 已点击 {selector}")
        return True
    return False


def _normalize_email_for_compare(value: str) -> str:
    return str(value or "").strip().lower()


def _account_chooser_submission_pending(page, email: str) -> bool:
    expected_email = _normalize_email_for_compare(email)
    if not expected_email:
        return False
    try:
        return bool(
            page.evaluate(
                """
                (expectedEmail) => {
                  const extractEmail = (text) => {
                    const match = String(text || '').match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                    return match ? match[0].trim().toLowerCase() : '';
                  };
                  return Array.from(document.querySelectorAll('button[name="session_id"]')).some((button) => {
                    const email = extractEmail(button.innerText || button.textContent || '');
                    return email === expectedEmail && (
                      button.disabled
                      || button.getAttribute('aria-busy') === 'true'
                      || button.getAttribute('data-state') === 'loading'
                    );
                  });
                }
                """,
                expected_email,
            )
        )
    except Exception:
        return False


def _detect_codex_next_step_from_dom(page) -> str:
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const hasVisible = (selector) => Array.from(document.querySelectorAll(selector)).some(visible);
              if (hasVisible('input[type="tel"], input[name="phone"], input[name="phone_number"], input[name="phoneNumber"], input[id*="phone" i], input[placeholder*="phone" i], input[autocomplete="tel"], input[autocomplete="tel-national"]')) {
                return 'add_phone';
              }
              if (hasVisible('input[type="password"], input[name="password"], input[autocomplete="new-password"]')) {
                return 'login_password';
              }
              if (hasVisible("input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='number'], input[name*='code' i], input[id*='code' i]")) {
                return 'email_otp_verification';
              }
              return '';
            }
            """
        )
        return str(result or "").strip()
    except Exception:
        return ""


def _handle_account_chooser(page, email: str, log: Callable[[str], None]) -> bool:
    expected_email = _normalize_email_for_compare(email)
    if not expected_email:
        return False
    try:
        result = page.evaluate(
            """
            (expectedEmail) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const extractEmail = (text) => {
                const match = String(text || '').match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
                return match ? match[0].trim() : '';
              };
              const buttons = Array.from(document.querySelectorAll('button[name="session_id"]'));
              const accounts = buttons.map((button) => ({
                email: extractEmail(button.innerText || button.textContent || ''),
                visible: visible(button),
              }));
              for (let i = 0; i < buttons.length; i += 1) {
                const account = accounts[i];
                if (!account.visible || account.email.toLowerCase() !== expectedEmail) continue;
                return { action: 'select', index: i, email: account.email };
              }
              const switchLink = document.querySelector('a[href="/log-in-or-create-account"], a[href*="/log-in-or-create-account"]');
              if (visible(switchLink)) {
                return { action: 'switch', accounts: accounts.map((item) => item.email).filter(Boolean) };
              }
              return { action: 'none', accounts: accounts.map((item) => item.email).filter(Boolean) };
            }
            """,
            expected_email,
        )
    except Exception as exc:
        log(f"Codex OAuth 账号选择页处理失败: {exc}")
        return False

    if not isinstance(result, dict):
        return False
    action = str(result.get("action") or "")
    if action == "select":
        selected_email = str(result.get("email") or "").strip()
        index = int(result.get("index") or 0)
        target = page.locator('button[name="session_id"]').nth(index)
        try:
            if not target.is_visible(timeout=500) or not target.is_enabled(timeout=500):
                log(f"Codex OAuth 账号选择页: 匹配账号当前不可点击 {selected_email or email}")
                return False
            target.click(timeout=3000, no_wait_after=True)
        except Exception as exc:
            summary = str(exc or "").splitlines()[0][:160]
            if "timeout" in summary.lower():
                log(
                    f"Codex OAuth 账号选择页: 匹配账号点击等待超时，"
                    f"已进入异步跳转观察期 {selected_email or email}"
                )
                return True
            log(f"Codex OAuth 账号选择页: 匹配账号点击失败 {selected_email or email}: {summary}")
            return False
        log(f"Codex OAuth 账号选择页: 已选择匹配账号 {selected_email or email}")
        return True
    if action == "switch":
        accounts = [str(item) for item in (result.get("accounts") or []) if str(item or "").strip()]
        suffix = f"；页面已有账号: {', '.join(accounts)}" if accounts else ""
        try:
            page.locator('a[href="/log-in-or-create-account"], a[href*="/log-in-or-create-account"]').first.click(timeout=5000)
        except Exception as exc:
            log(f"Codex OAuth 账号选择页: 切换账号点击失败: {exc}")
            return False
        log(f"Codex OAuth 账号选择页: 未匹配预期邮箱 {email}，改为登录另一个帐户{suffix}")
        return True
    log(f"Codex OAuth 账号选择页: 未找到可用账号或切换入口，预期邮箱 {email}")
    return False


def _get_invalid_session_error_page(page) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const retry = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
                .find((el) => visible(el) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(String(el.innerText || el.textContent || el.value || '')));
              return {
                invalidSession: /invalid\\s+session\\s+id/i.test(text),
                retryVisible: Boolean(retry),
                text,
              };
            }
            """
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _click_invalid_session_try_again(page) -> bool:
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden'
                      && rect.width > 0 && rect.height > 0;
                  };
                  const retry = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
                    .find((el) => visible(el) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(String(el.innerText || el.textContent || el.value || '')));
                  if (!retry) return false;
                  retry.click();
                  return true;
                }
                """
            )
        )
        return clicked
    except Exception:
        return False


def _mask_phone_number(phone_number: str) -> str:
    text = str(phone_number or "").strip()
    if len(text) <= 4:
        return text
    if len(text) <= 8:
        return f"{text[:2]}****{text[-2:]}"
    return f"{text[:4]}****{text[-2:]}"


def _parse_phone_country_and_local(phone_number: str) -> tuple[str, str, str]:
    num = str(phone_number or "").lstrip("+").strip().replace(" ", "").replace("-", "")
    for length in (3, 2, 1):
        if length > len(num):
            continue
        prefix = num[:length]
        if prefix in PHONE_COUNTRY_CODE_MAP:
            return prefix, num[length:], PHONE_COUNTRY_CODE_MAP[prefix]
    return "", num, ""


def _digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone_input_contains(page, selector: str, expected: str) -> tuple[bool, str]:
    try:
        actual = str(page.evaluate("(sel) => document.querySelector(sel)?.value || ''", selector) or "")
    except Exception:
        actual = ""
    actual_digits = _digits_only(actual)
    expected_digits = _digits_only(expected)
    if not expected_digits:
        return False, actual
    return expected_digits in actual_digits, actual


def _select_phone_country_ui(page, dial_code: str, country_name: str, log: Callable[[str], None]) -> bool:
    if not dial_code and not country_name:
        log("Codex OAuth add_phone: 无法识别国家码，跳过国家选择")
        return False
    iso_code = PHONE_DIAL_TO_ISO.get(dial_code, "")
    dial_pattern = f"(+{dial_code})"

    try:
        already = page.evaluate(
            """
            (dialPattern) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              for (const el of Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], select, div, span'))) {
                if (!visible(el)) continue;
                const text = String(el.innerText || el.textContent || '').trim();
                if (text.includes(dialPattern) && text.length < 100) return true;
              }
              return false;
            }
            """,
            dial_pattern,
        )
        if already:
            return True
    except Exception:
        pass

    if iso_code:
        try:
            selected = page.evaluate(
                """
                (isoCode) => {
                  const selects = Array.from(document.querySelectorAll('select'));
                  for (const select of selects) {
                    const option = Array.from(select.options || []).find((item) => {
                      const value = String(item.value || '').toUpperCase();
                      const text = String(item.textContent || '').toUpperCase();
                      return value === isoCode || text.includes(isoCode);
                    });
                    if (!option) continue;
                    select.value = option.value;
                    select.dispatchEvent(new Event('input', { bubbles: true }));
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                  }
                  return false;
                }
                """,
                iso_code,
            )
            if selected:
                log(f"Codex OAuth add_phone: 已通过 select 选择国家 {country_name or iso_code}")
                return True
        except Exception:
            pass

    for trigger in (
        '[role="combobox"]',
        'button[aria-haspopup="listbox"]',
        'button:has-text("+")',
        'button',
    ):
        try:
            if not page.locator(trigger).first.is_visible(timeout=500):
                continue
            page.locator(trigger).first.click(timeout=1500)
            time.sleep(0.5)
            for option in (
                f'[role="option"]:has-text("{dial_pattern}")',
                f'[role="option"]:has-text("{country_name}")',
                f'li:has-text("{dial_pattern}")',
                f'li:has-text("{country_name}")',
                f'button:has-text("{dial_pattern}")',
            ):
                try:
                    if page.locator(option).first.is_visible(timeout=700):
                        page.locator(option).first.click(timeout=1500)
                        log(f"Codex OAuth add_phone: 已选择国家 {country_name or dial_pattern}")
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    log(f"Codex OAuth add_phone: 未能选择国家 {country_name or dial_pattern}，将尝试填写完整号码")
    return False


def _delivery_option_state(page, keyword: str) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """
            (keyword) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const matches = (text, metadata) => {
                const lowered = normalize(text).toLowerCase();
                const stable = normalize(metadata).toLowerCase();
                if (keyword === 'sms') {
                  return (
                    /(^|[^a-z])(sms|text[-_ ]?message)([^a-z]|$)/i.test(stable)
                    || lowered.includes('text message')
                    || lowered === 'sms'
                    || lowered.includes('短信')
                    || lowered.includes('문자 메시지')
                  ) && !stable.includes('whatsapp') && !lowered.includes('whatsapp');
                }
                return stable.includes('whatsapp') || lowered.includes('whatsapp');
              };
              const candidates = [];
              const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
              const radioIndices = [];
              for (const el of Array.from(document.querySelectorAll('label, button, [role="radio"], [role="option"], input[type="radio"]'))) {
                let text = normalize(el.innerText || el.textContent || el.value || '');
                let input = null;
                if (el.matches('input[type="radio"]')) {
                  input = el;
                  const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${el.id}"]`) : null);
                  text = normalize(label?.innerText || label?.textContent || text);
                } else if (el.matches('label')) {
                  input = el.querySelector('input[type="radio"]') || (el.htmlFor ? document.getElementById(el.htmlFor) : null);
                }
                const metadata = [el, input].filter(Boolean).flatMap((node) => [
                  node.getAttribute('value'),
                  node.getAttribute('name'),
                  node.getAttribute('id'),
                  node.getAttribute('data-testid'),
                  node.getAttribute('data-value'),
                  node.getAttribute('aria-label'),
                ]).filter(Boolean).join(' ');
                if (!matches(text, metadata)) continue;
                const radioIndex = input ? radios.indexOf(input) : -1;
                if (radioIndex >= 0 && !radioIndices.includes(radioIndex)) radioIndices.push(radioIndex);
                candidates.push({
                  tag: el.tagName.toLowerCase(),
                  text: text.slice(0, 60),
                  visible: visible(el),
                  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                  selected: Boolean(
                    input?.checked
                    || el.getAttribute('aria-checked') === 'true'
                    || el.getAttribute('aria-selected') === 'true'
                    || el.getAttribute('data-state') === 'checked'
                  ),
                });
              }
              return { selected: candidates.some((item) => item.visible && item.selected), candidates, radioIndices };
            }
            """,
            keyword,
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _redact_add_phone_capture_text(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"https?://[^\s]+", "[url]", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)\+?\d[\d\s().-]{6,}\d(?!\d)", "[phone]", text)
    text = re.sub(r"\b\d{4,}\b", "[digits]", text)
    return text[:240]


def _inspect_add_phone_submission(page, phone_selector: str) -> dict[str, Any]:
    """Return an allowlisted, value-free description of the phone form."""
    try:
        result = page.evaluate(
            r"""
            ({ phoneSelector, submitSelector }) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const disabled = (el) => Boolean(
                el.disabled || el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true'
              );
              const redact = (value) => String(value || '')
                .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[email]')
                .replace(/\+?\d[\d\s().-]{6,}\d/g, '[phone]')
                .replace(/\b\d{4,}\b/g, '[digits]')
                .replace(/\s+/g, ' ')
                .trim()
                .slice(0, 100);
              const phone = document.querySelector(phoneSelector);
              const phoneForm = phone?.form || phone?.closest('form') || null;
              const submitters = Array.from(document.querySelectorAll(submitSelector));
              const submits = submitters.map((el, index) => {
                const owner = el.form || el.closest('form') || null;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const centerX = rect.left + (rect.width / 2);
                const centerY = rect.top + (rect.height / 2);
                const hit = rect.width > 0 && rect.height > 0
                  ? document.elementFromPoint(centerX, centerY)
                  : null;
                return {
                  index,
                  tag: el.tagName.toLowerCase(),
                  type: String(el.getAttribute('type') || '').slice(0, 30),
                  name: String(el.getAttribute('name') || '').slice(0, 80),
                  id: String(el.id || '').slice(0, 80),
                  role: String(el.getAttribute('role') || '').slice(0, 40),
                  testId: String(el.getAttribute('data-testid') || '').slice(0, 80),
                  text: redact(el.innerText || el.textContent || el.value),
                  visible: visible(el),
                  disabled: disabled(el),
                  busy: el.getAttribute('aria-busy') === 'true'
                    || el.getAttribute('data-loading') === 'true'
                    || el.getAttribute('data-state') === 'loading',
                  pointerEvents: String(style.pointerEvents || ''),
                  hasForm: Boolean(owner),
                  sameForm: Boolean(phoneForm && owner === phoneForm),
                  hitMatches: Boolean(hit && (hit === el || el.contains(hit))),
                  hitTag: hit ? hit.tagName.toLowerCase() : '',
                };
              });
              const eligible = submits.filter((item) => item.visible && !item.disabled);
              const target = phoneForm
                ? eligible.find((item) => item.sameForm)
                : (eligible.length === 1 ? eligible[0] : null);
              const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="listbox"]'))
                .filter(visible)
                .slice(0, 5)
                .map((el) => ({
                  role: String(el.getAttribute('role') || ''),
                  text: redact(el.innerText || el.textContent),
                }));
              return {
                page: {
                  url: `${location.origin}${location.pathname}`,
                  lang: String(document.documentElement.lang || '').slice(0, 30),
                  title: redact(document.title),
                },
                phone: {
                  present: Boolean(phone),
                  type: String(phone?.getAttribute('type') || '').slice(0, 30),
                  name: String(phone?.getAttribute('name') || '').slice(0, 80),
                  id: String(phone?.id || '').slice(0, 80),
                  autocomplete: String(phone?.getAttribute('autocomplete') || '').slice(0, 50),
                  visible: visible(phone),
                  disabled: phone ? disabled(phone) : false,
                  readOnly: Boolean(phone?.readOnly),
                  hasForm: Boolean(phoneForm),
                  formId: String(phoneForm?.id || '').slice(0, 80),
                },
                submits,
                targetIndex: target ? target.index : null,
                dialogs,
              };
            }
            """,
            {"phoneSelector": phone_selector, "submitSelector": PHONE_STRUCTURAL_SUBMIT_SELECTOR},
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _capture_add_phone_structure(
    page,
    phone_selector: str,
    *,
    stage: str,
    click_error: Any = "",
) -> str:
    """Persist only allowlisted DOM metadata when explicitly enabled."""
    capture_root = str(os.environ.get("CODEX_OAUTH_SAFE_CAPTURE_DIR") or "").strip()
    if not capture_root:
        return ""
    try:
        payload = _inspect_add_phone_submission(page, phone_selector)
        payload["capturedAt"] = datetime.now(timezone.utc).isoformat()
        payload["stage"] = re.sub(r"[^a-z0-9_-]+", "-", str(stage or "state").lower()).strip("-")[:50]
        if click_error:
            payload["clickError"] = _redact_add_phone_capture_text(click_error)
        root = Path(capture_root)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = root / f"codex_add_phone_{stamp}_{os.getpid()}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path.resolve())
    except Exception:
        return ""


def _add_phone_submit_progress_state(page, phone_selector: str) -> dict[str, Any]:
    try:
        result = page.evaluate(
            r"""
            ({ phoneSelector, submitSelector }) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const phone = document.querySelector(phoneSelector);
              const codeInputs = Array.from(document.querySelectorAll(
                'input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[data-testid*="otp" i]'
              )).filter((el) => el !== phone && visible(el));
              const phoneForm = phone?.form || phone?.closest('form') || null;
              const submitters = Array.from(document.querySelectorAll(submitSelector));
              const relevant = submitters.filter((el) => {
                const owner = el.form || el.closest('form') || null;
                return phoneForm ? owner === phoneForm : visible(el);
              });
              const busy = relevant.some((el) => Boolean(
                el.disabled || el.matches(':disabled')
                || el.getAttribute('aria-disabled') === 'true'
                || el.getAttribute('aria-busy') === 'true'
                || el.getAttribute('data-loading') === 'true'
                || el.getAttribute('data-state') === 'loading'
              ));
              const alerts = Array.from(document.querySelectorAll('[role="alert"], [aria-live="assertive"]'))
                .filter((el) => visible(el) && String(el.innerText || el.textContent || '').trim());
              return {
                url: `${location.origin}${location.pathname}${location.hash}`,
                otpVisible: codeInputs.length > 0,
                phoneVisible: visible(phone),
                phoneLocked: Boolean(phone && (phone.disabled || phone.readOnly)),
                busy,
                alertCount: alerts.length,
                alertLength: alerts.reduce((total, el) => total + String(el.innerText || el.textContent || '').length, 0),
              };
            }
            """,
            {"phoneSelector": phone_selector, "submitSelector": PHONE_STRUCTURAL_SUBMIT_SELECTOR},
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _add_phone_submission_progressed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not after:
        return False
    return bool(
        (after.get("url") and after.get("url") != before.get("url"))
        or (after.get("otpVisible") and not before.get("otpVisible"))
        or (not after.get("phoneVisible", True) and before.get("phoneVisible", True))
        or (after.get("phoneLocked") and not before.get("phoneLocked"))
        or (after.get("busy") and not before.get("busy"))
        or int(after.get("alertCount") or 0) > int(before.get("alertCount") or 0)
        or int(after.get("alertLength") or 0) > int(before.get("alertLength") or 0)
    )


def _request_submit_add_phone_form(page, phone_selector: str) -> dict[str, Any]:
    try:
        result = page.evaluate(
            r"""
            ({ phoneSelector, submitSelector }) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const enabled = (el) => Boolean(
                el && !el.disabled && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true'
              );
              const phone = document.querySelector(phoneSelector);
              const form = phone?.form || phone?.closest('form') || null;
              const submitters = Array.from(document.querySelectorAll(submitSelector));
              const eligible = submitters.filter((el) => visible(el) && enabled(el));
              const submitter = form
                ? eligible.find((el) => (el.form || el.closest('form') || null) === form)
                : (eligible.length === 1 ? eligible[0] : null);
              if (!submitter) return { ok: false, reason: 'no-structural-submitter' };
              if (!form) {
                submitter.click();
                return { ok: true, method: 'element.click' };
              }
              if ((submitter.form || submitter.closest('form') || null) !== form) {
                return { ok: false, reason: 'form-owner-mismatch' };
              }
              if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
                return {
                  ok: false,
                  reason: 'form-invalid',
                  invalidCount: Array.from(form.elements || []).filter((el) => el.willValidate && !el.validity.valid).length,
                };
              }
              if (typeof form.requestSubmit !== 'function') {
                return { ok: false, reason: 'request-submit-unavailable' };
              }
              form.requestSubmit(submitter);
              return { ok: true, method: 'form.requestSubmit' };
            }
            """,
            {"phoneSelector": phone_selector, "submitSelector": PHONE_STRUCTURAL_SUBMIT_SELECTOR},
        )
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        return {"ok": False, "reason": _redact_add_phone_capture_text(exc)}


def _submit_add_phone_number(
    page,
    phone_selector: str,
    *,
    log: Callable[[str], None],
    timeout: int = 8,
    stage: str = "send",
) -> str | None:
    structure = _inspect_add_phone_submission(page, phone_selector)
    capture_path = _capture_add_phone_structure(page, phone_selector, stage=f"{stage}-before")
    if capture_path:
        log(f"Codex OAuth add_phone: 已保存脱敏结构快照 {capture_path}")

    target_index = structure.get("targetIndex")
    if isinstance(target_index, int) and not isinstance(target_index, bool):
        before = _add_phone_submit_progress_state(page, phone_selector)
        click_error: Exception | None = None
        try:
            target = page.locator(PHONE_STRUCTURAL_SUBMIT_SELECTOR).nth(target_index)
            target.click(timeout=min(max(int(timeout * 1000), 1000), 4000), no_wait_after=True)
            return f"{PHONE_STRUCTURAL_SUBMIT_SELECTOR} nth={target_index}"
        except Exception as exc:
            click_error = exc
            log(
                "Codex OAuth add_phone: 结构化 submit 常规点击失败，观察页面状态后尝试表单兜底: "
                f"{_redact_add_phone_capture_text(exc)}"
            )
        if click_error is not None:
            time.sleep(0.8)
            after = _add_phone_submit_progress_state(page, phone_selector)
            if _add_phone_submission_progressed(before, after):
                log("Codex OAuth add_phone: submit 点击虽返回异常，但页面已进入下一状态，不重复提交")
                return f"{PHONE_STRUCTURAL_SUBMIT_SELECTOR} progress-after-click"
            capture_path = _capture_add_phone_structure(
                page,
                phone_selector,
                stage=f"{stage}-click-failed",
                click_error=click_error,
            )
            if capture_path:
                log(f"Codex OAuth add_phone: 已保存点击失败脱敏结构快照 {capture_path}")
    else:
        legacy_selector = _click_first_no_wait(page, PHONE_SEND_SELECTORS, timeout=timeout)
        if legacy_selector:
            return legacy_selector

    before_enter = _add_phone_submit_progress_state(page, phone_selector)
    try:
        phone_input = page.locator(phone_selector).first
        if phone_input.is_visible(timeout=500) and phone_input.is_enabled(timeout=500):
            phone_input.focus(timeout=1000)
            phone_input.press("Enter", timeout=2000)
            time.sleep(0.8)
            after_enter = _add_phone_submit_progress_state(page, phone_selector)
            if _add_phone_submission_progressed(before_enter, after_enter):
                log("Codex OAuth add_phone: 已通过手机号输入框 Enter 提交所属表单")
                return f"{phone_selector} press=Enter"
    except Exception as exc:
        log(f"Codex OAuth add_phone: Enter 提交失败: {_redact_add_phone_capture_text(exc)}")

    request_result = _request_submit_add_phone_form(page, phone_selector)
    if request_result.get("ok"):
        method = str(request_result.get("method") or "requestSubmit")
        log(f"Codex OAuth add_phone: 已通过 {method} 提交手机号表单")
        return method
    reason = str(request_result.get("reason") or "unknown")
    log(f"Codex OAuth add_phone: 表单结构化提交失败: {reason[:160]}")
    return None


def _select_delivery_radio_with_keyboard(page, state: dict[str, Any], keyword: str) -> bool:
    for raw_index in state.get("radioIndices") or []:
        try:
            index = int(raw_index)
            target = page.locator('input[type="radio"]').nth(index)
            if not target.is_visible(timeout=500) or not target.is_enabled(timeout=500):
                continue
            target.focus(timeout=1000)
            target.press("Space", timeout=1500)
            time.sleep(0.2)
            if _delivery_option_state(page, keyword).get("selected"):
                return True
        except Exception:
            continue
    return False


def _select_text_message_delivery(page, log: Callable[[str], None]) -> bool:
    selector = _click_first_no_wait(page, PHONE_TEXT_MESSAGE_SELECTORS, timeout=3)
    if selector:
        log(f"Codex OAuth add_phone: 已选择短信方式 {selector}")
        return True
    state = _delivery_option_state(page, "sms")
    if state.get("selected"):
        log("Codex OAuth add_phone: Text Message 已处于选中状态")
        return True
    if _select_delivery_radio_with_keyboard(page, state, "sms"):
        log("Codex OAuth add_phone: 已使用键盘 Space 选择 Text Message")
        return True
    log(f"Codex OAuth add_phone: 未找到可点击的 Text Message 选项，候选={(state.get('candidates') or [])[:4]}")
    return False


def _is_whatsapp_fallback_prompt(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        "whatsapp" in lowered
        and (
            "switched to whatsapp" in lowered
            or "continue to send" in lowered
            or "couldn't send a text message" in lowered
            or "could not send a text message" in lowered
            or "whatsapp に切り替えました" in lowered
            or "whatsappに切り替えました" in lowered
            or "whatsapp で認証コード" in lowered
            or "whatsappで認証コード" in lowered
        )
    )


def _select_whatsapp_delivery(page, log: Callable[[str], None]) -> bool:
    selector = _click_first_no_wait(page, PHONE_WHATSAPP_SELECTORS, timeout=3)
    if selector:
        log(f"Codex OAuth add_phone: 已选择 WhatsApp 方式 {selector}")
        return True
    state = _delivery_option_state(page, "whatsapp")
    if state.get("selected"):
        log("Codex OAuth add_phone: WhatsApp 已处于选中状态")
        return True
    if _select_delivery_radio_with_keyboard(page, state, "whatsapp"):
        log("Codex OAuth add_phone: 已使用键盘 Space 选择 WhatsApp")
        return True
    log(f"Codex OAuth add_phone: 未找到可点击的 WhatsApp 选项，候选={(state.get('candidates') or [])[:4]}")
    return False


def _summarize_add_phone_controls(page) -> str:
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
                .filter(visible)
                .slice(0, 10)
                .map((el) => ({
                  text: normalize(el.innerText || el.textContent || el.value).slice(0, 50),
                  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                  type: String(el.getAttribute('type') || el.getAttribute('role') || el.tagName).slice(0, 20),
                }));
              const options = Array.from(document.querySelectorAll('label, [role="radio"], [role="option"]'))
                .filter(visible)
                .map((el) => normalize(el.innerText || el.textContent))
                .filter((text) => /text message|sms|whatsapp|短信/i.test(text))
                .slice(0, 8);
              return { buttons, options };
            }
            """
        )
        return json.dumps(result, ensure_ascii=False)[:700]
    except Exception as exc:
        return f"unavailable:{str(exc or '').splitlines()[0][:120]}"


def _resume_oauth_after_add_phone_success(
    page,
    *,
    resume_url: str,
    log: Callable[[str], None],
    observe_seconds: float = 5,
) -> None:
    deadline = time.time() + max(float(observe_seconds), 0)
    last_url = str(getattr(page, "url", "") or "")
    while time.time() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        if current_url:
            last_url = current_url
        if (
            "localhost:1455/auth/callback" in current_url
            or "localhost:1455/success" in current_url
            or "code=" in current_url
        ):
            log("Codex OAuth add_phone: 手机验证后已进入回调流程")
            return
        if current_url and "add-phone" not in current_url:
            log(f"Codex OAuth add_phone: 手机验证后页面已自然跳转 {current_url[:110]}")
            return
        try:
            page_type = str((_derive_registration_state_from_page(page) or {}).get("page_type") or "")
        except Exception:
            page_type = ""
        if page_type in {"consent", "workspace_selection", "organization_selection", "oauth_callback", "chatgpt_home"}:
            log(f"Codex OAuth add_phone: 手机验证后已进入 {page_type}")
            return
        time.sleep(0.5)

    if resume_url:
        log(f"Codex OAuth add_phone: 手机验证后仍停留 add_phone，重新打开授权链接 {last_url[:110]}")
        _goto_with_retry(page, resume_url, wait_until="domcontentloaded", timeout=30000, log=log)


def _handle_add_phone_challenge(
    page,
    phone_callback: Callable[[], str],
    *,
    log: Callable[[str], None],
    resume_url: str,
    max_phone_attempts: int | None = None,
) -> None:
    if not callable(phone_callback):
        raise RuntimeError("Codex OAuth 遇到 add_phone，但未配置接码服务")

    configured_attempts = (
        max_phone_attempts
        if max_phone_attempts is not None
        else getattr(phone_callback, "phone_max_attempts", 3)
    )
    try:
        attempt_limit = max(int(configured_attempts or 1), 1)
    except (TypeError, ValueError):
        attempt_limit = 3

    last_error: Exception | None = None
    for attempt in range(attempt_limit):
        if attempt:
            log(f"Codex OAuth add_phone: 换号重试 {attempt + 1}/{attempt_limit}")
            try:
                _goto_with_retry(
                    page,
                    "https://auth.openai.com/add-phone",
                    wait_until="domcontentloaded",
                    timeout=15000,
                    log=log,
                )
                time.sleep(1)
            except Exception:
                pass
        try:
            _do_add_phone_attempt(page, phone_callback, log=log, resume_url=resume_url)
            return
        except Exception as exc:
            last_error = exc
            message = str(exc)
            retryable = (
                "未获取到短信验证码" in message
                or "等待短信验证码超时" in message
                or "NO_NUMBERS" in message
                or "暂无号码" in message
                or "无号码" in message
                or "已被使用" in message
                or "其他电话号码" in message
                or "电话号码无效" in message
                or "手机号无效" in message
                or "電話番号が無効" in message
                or "電話番号は無効" in message
                or "すでに使用" in message
                or "最近使用された" in message
                or "別の電話番号" in message
                or "上限数のアカウントに関連付けられ" in message
                or "phone_number_in_use" in message
                or "invalid phone" in message.lower()
                or "invalid number" in message.lower()
                or "no numbers" in message.lower()
                or "no_number" in message.lower()
                or "no number" in message.lower()
                or "already" in message.lower()
                or "in use" in message.lower()
                or "recently used" in message.lower()
                or "最近使用" in message
            )
            if hasattr(phone_callback, "cleanup"):
                try:
                    phone_callback.cleanup()
                except Exception:
                    pass
            if not retryable:
                raise
            if attempt + 1 >= attempt_limit:
                break
            if hasattr(phone_callback, "reset"):
                try:
                    phone_callback.reset()
                except Exception:
                    pass
            log(f"Codex OAuth add_phone: {message}，准备换号")
    raise RuntimeError(f"Codex OAuth add_phone 手机验证失败: {last_error}")


def _do_add_phone_attempt(
    page,
    phone_callback: Callable[[], str],
    *,
    log: Callable[[str], None],
    resume_url: str,
) -> None:
    log("Codex OAuth add_phone: 开始获取手机号")
    phone_number = str(phone_callback() or "").strip()
    if not phone_number:
        raise RuntimeError("未获取到手机号")
    log(f"Codex OAuth add_phone: 提交手机号 {_mask_phone_number(phone_number)}")

    current_url = str(getattr(page, "url", "") or "")
    if "add-phone" not in current_url:
        _goto_with_retry(
            page,
            "https://auth.openai.com/add-phone",
            wait_until="domcontentloaded",
            timeout=30000,
            log=log,
        )
    time.sleep(1)

    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
    phone_selector = _wait_for_any_selector(page, PHONE_INPUT_SELECTORS, timeout=10)
    if not phone_selector:
        raise RuntimeError("未找到手机号输入框")

    fill_value = local_number if country_selected and local_number else phone_number
    filled = _fill_input_like_user(page, phone_selector, fill_value)
    if not filled:
        filled, actual_value = _phone_input_contains(page, phone_selector, fill_value)
        if filled:
            log(f"Codex OAuth add_phone: 手机号已填写并被页面格式化 value={actual_value[:16]}...")
    if not filled:
        log("Codex OAuth add_phone: 常规填写失败，尝试键盘 fallback")
        try:
            page.click(phone_selector)
            time.sleep(0.2)
            for shortcut in ("Control+A", "Meta+A"):
                try:
                    page.keyboard.press(shortcut)
                    time.sleep(0.1)
                    page.keyboard.press("Backspace")
                    time.sleep(0.1)
                except Exception:
                    pass
            page.keyboard.type(fill_value, delay=40)
            time.sleep(0.3)
            filled, actual_value = _phone_input_contains(page, phone_selector, fill_value)
            if filled:
                log(f"Codex OAuth add_phone: 键盘 fallback 成功 value={actual_value[:16]}...")
        except Exception as exc:
            log(f"Codex OAuth add_phone: 键盘 fallback 失败: {exc}")
            filled = False
    if not filled:
        log("Codex OAuth add_phone: 键盘 fallback 失败，尝试 JS setValue")
        try:
            filled = bool(
                page.evaluate(
                    """
                    ({ selector, value }) => {
                      const input = document.querySelector(selector);
                      if (!input) return false;
                      input.focus();
                      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                      if (setter) setter.call(input, value);
                      else input.value = value;
                      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                      input.dispatchEvent(new Event('change', { bubbles: true }));
                      input.dispatchEvent(new Event('blur', { bubbles: true }));
                      const digits = (raw) => String(raw || '').replace(/\\D/g, '');
                      return digits(input.value).includes(digits(value));
                    }
                    """,
                    {"selector": phone_selector, "value": fill_value},
                )
            )
            if filled:
                log("Codex OAuth add_phone: JS setValue fallback 成功")
        except Exception as exc:
            log(f"Codex OAuth add_phone: JS setValue fallback 失败: {exc}")
    if not filled:
        _, actual_value = _phone_input_contains(page, phone_selector, fill_value)
        raise RuntimeError(f"手机号输入框填写失败: {phone_selector} value={actual_value[:40]}")
    log(f"Codex OAuth add_phone: 手机号输入框已填写 {phone_selector}")

    _select_text_message_delivery(page, log)
    time.sleep(0.5)

    send_selector = _submit_add_phone_number(
        page,
        phone_selector,
        log=log,
        timeout=8,
        stage="sms-send",
    )
    if not send_selector:
        controls = _summarize_add_phone_controls(page)
        log(f"Codex OAuth add_phone: 未找到可见且可点击的发送按钮，页面控件={controls}")
        raise RuntimeError("未找到可见且可点击的发送验证码按钮")
    log(f"Codex OAuth add_phone: 已点击发送按钮 {send_selector}")
    time.sleep(2)

    error_text = _extract_auth_error_text(page)
    if _is_whatsapp_fallback_prompt(error_text):
        log(f"Codex OAuth add_phone: 短信发送失败，切换 WhatsApp 重试: {error_text[:160]}")
        if not _select_whatsapp_delivery(page, log):
            raise RuntimeError(f"手机号提交失败，且未找到 WhatsApp 选项: {error_text[:200]}")
        time.sleep(0.5)
        whatsapp_send_selector = _submit_add_phone_number(
            page,
            phone_selector,
            log=log,
            timeout=8,
            stage="whatsapp-send",
        )
        if not whatsapp_send_selector:
            raise RuntimeError("切换 WhatsApp 后未找到发送验证码按钮")
        log(f"Codex OAuth add_phone: 已点击 WhatsApp 发送按钮 {whatsapp_send_selector}")
        time.sleep(2)
        error_text = _extract_auth_error_text(page)
        if _is_whatsapp_fallback_prompt(error_text):
            error_text = ""
    if error_text:
        if hasattr(phone_callback, "mark_send_failed"):
            phone_callback.mark_send_failed(error_text)
        raise RuntimeError(f"手机号提交失败: {error_text[:200]}")
    if hasattr(phone_callback, "mark_send_succeeded"):
        phone_callback.mark_send_succeeded()

    for code_attempt in range(3):
        sms_code = str(phone_callback() or "").strip()
        if not sms_code:
            raise RuntimeError("未获取到短信验证码")
        otp_result = _submit_otp_via_page(page, sms_code, log)
        log(f"Codex OAuth add_phone: 短信验证码提交状态 {otp_result.get('status', 0)}")
        if otp_result.get("ok"):
            if hasattr(phone_callback, "report_success"):
                phone_callback.report_success()
            _resume_oauth_after_add_phone_success(page, resume_url=resume_url, log=log)
            return
        page_error = _extract_auth_error_text(page)
        if page_error and any(token in page_error.lower() for token in ("invalid", "incorrect", "wrong", "expired")):
            if hasattr(phone_callback, "mark_code_failed"):
                phone_callback.mark_code_failed(page_error)
            log(f"Codex OAuth add_phone: 短信验证码无效，继续等待下一条: {page_error[:100]}")
            continue
        if hasattr(phone_callback, "mark_code_failed"):
            phone_callback.mark_code_failed(page_error or str(otp_result.get("status") or "failed"))
        raise RuntimeError(f"短信验证码校验失败: {page_error[:200] if page_error else otp_result.get('status')}")
    raise RuntimeError("短信验证码校验失败: 多次验证码均未通过")


def _try_skip_add_phone(page, *, auth_url: str, callback_server: _OAuthCallbackServer, log: Callable[[str], None]) -> bool:
    log("Codex OAuth add_phone: 未配置接码，尝试重新访问授权链接跳过")
    try:
        _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=15000, log=log)
        for _ in range(8):
            if callback_server.event.is_set():
                return True
            current_url = str(getattr(page, "url", "") or "")
            if "code=" in current_url or "localhost:1455/auth/callback" in current_url:
                return True
            state = _derive_registration_state_from_page(page)
            if str(state.get("page_type") or "") in {"consent", "workspace_selection", "organization_selection"}:
                return False
            time.sleep(1)
    except Exception as exc:
        log(f"Codex OAuth add_phone: 跳过尝试异常: {exc}")
    return False


def _sleep_until_callback(callback_server: _OAuthCallbackServer, seconds: float) -> bool:
    wait_seconds = max(float(seconds), 0.05)
    event_wait = getattr(callback_server.event, "wait", None)
    if callable(event_wait):
        return bool(event_wait(wait_seconds))
    time.sleep(wait_seconds)
    return bool(callback_server.event.is_set())


def _observe_callback_request_on_page(
    page,
    callback_server: _OAuthCallbackServer,
    log: Callable[[str], None],
) -> None:
    on = getattr(page, "on", None)
    if not callable(on):
        return

    def _capture(request) -> None:
        try:
            parsed = urlparse(str(getattr(request, "url", "") or ""))
            if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port != CODEX_CALLBACK_PORT:
                return
            if parsed.path != "/auth/callback":
                return
            query = parse_qs(parsed.query)
            result = {
                "code": (query.get("code") or [""])[0],
                "state": (query.get("state") or [""])[0],
                "error": (query.get("error") or [""])[0],
                "error_description": (query.get("error_description") or [""])[0],
            }
            if result["state"] != callback_server.state:
                return
            first_delivery = not callback_server.event.is_set()
            callback_server.result = result
            callback_server.event.set()
            if first_delivery:
                log("Codex OAuth 已从浏览器本地回调请求捕获结果")
        except Exception:
            return

    on("request", _capture)


def _recover_stalled_consent_page(
    page,
    *,
    auth_url: str,
    recovery_index: int,
    log: Callable[[str], None],
) -> None:
    if recovery_index <= 1:
        log("Codex OAuth consent 页面停滞，刷新页面重试")
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
            return
        except Exception as exc:
            log(f"Codex OAuth consent 页面刷新失败，改为重新打开授权链接: {exc}")
    log("Codex OAuth consent 页面仍停滞，重新打开授权链接重试")
    _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)


def _is_incorrect_password_error(text: str) -> bool:
    value = str(text or "").lower()
    return any(
        token in value
        for token in (
            "incorrect email address or password",
            "incorrect password",
            "invalid email or password",
            "invalid credentials",
            "wrong password",
            "邮箱地址或密码不正确",
            "邮箱或密码不正确",
            "密码不正确",
            "メールアドレスまたはパスワードが正しくありません",
            "パスワードが正しくありません",
        )
    )


def _request_oauth_email_otp(page, log: Callable[[str], None], *, reason: str) -> bool:
    selector = _click_first_no_wait(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=5)
    if not selector:
        return False
    log(f"Codex OAuth 密码页已选择邮箱验证码登录 ({reason}): {selector}")
    return True


def _drive_codex_oauth_page(
    page,
    *,
    auth_url: str,
    email: str,
    password: str,
    callback_server: _OAuthCallbackServer,
    log: Callable[[str], None],
    otp_callback: Callable[[], str] | None,
    phone_callback: Callable[[], str] | None,
    timeout: int,
    registration_auth_mode: str = "",
) -> dict[str, str]:
    _observe_callback_request_on_page(page, callback_server, log)
    _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=45000, log=log)
    deadline = time.time() + max(int(timeout), 30)
    last_signature = ""
    repeated = 0
    invalid_session_retries = 0
    account_chooser_grace_until = 0.0
    email_submit_grace_until = 0.0
    passwordless_login_grace_until = 0.0
    passwordless_login_requested = False
    consent_stall_recoveries = 0
    prefer_email_otp = str(registration_auth_mode or "").strip().lower() == "email_otp"

    while time.time() < deadline:
        if callback_server.event.is_set():
            return callback_server.wait(1)

        current_url = str(getattr(page, "url", "") or "")
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        dom_page_type = ""
        if page_type == "account_chooser":
            dom_page_type = _detect_codex_next_step_from_dom(page)
            if dom_page_type:
                page_type = dom_page_type
                state["page_type"] = dom_page_type
        signature = f"{page_type}|{current_url}"
        signature_changed = signature != last_signature
        if not signature_changed:
            repeated += 1
        else:
            repeated = 0
            last_signature = signature
            log(f"Codex OAuth 页面变化: page={page_type or '-'} url={current_url[:110]}")

        if "localhost:1455/auth/callback" in current_url or "localhost:1455/success" in current_url:
            return callback_server.wait(10)

        consent_like_page = page_type in {"consent", "workspace_selection", "organization_selection"} or any(
            token in current_url for token in ("sign-in-with-chatgpt", "workspace", "organization", "consent")
        )
        if (
            consent_like_page
            and repeated >= CODEX_CONSENT_STALL_RECOVERY_THRESHOLD
            and consent_stall_recoveries < CODEX_CONSENT_STALL_MAX_RECOVERIES
        ):
            consent_stall_recoveries += 1
            _recover_stalled_consent_page(
                page,
                auth_url=auth_url,
                recovery_index=consent_stall_recoveries,
                log=log,
            )
            repeated = 0
            if _sleep_until_callback(callback_server, 1):
                return callback_server.wait(1)
            continue

        invalid_session = _get_invalid_session_error_page(page)
        if invalid_session.get("invalidSession"):
            invalid_session_retries += 1
            account_chooser_grace_until = 0.0
            if invalid_session_retries <= 3:
                log(f"Codex OAuth 缓存账号 session 失效，点击 Try again 后重选账号 ({invalid_session_retries}/3)")
                if _click_invalid_session_try_again(page):
                    if _sleep_until_callback(callback_server, 1):
                        return callback_server.wait(1)
                    continue
                log("Codex OAuth Invalid session ID 页面未找到 Try again，重新打开授权链接")
                _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            log("Codex OAuth 缓存账号 session 连续失效，重新打开授权链接")
            _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
            if _sleep_until_callback(callback_server, 1):
                return callback_server.wait(1)
            continue

        if page_type == "account_chooser":
            if account_chooser_grace_until and time.time() < account_chooser_grace_until:
                if repeated <= 1:
                    log("Codex OAuth 账号选择页: 已提交，等待页面异步跳转")
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            if _account_chooser_submission_pending(page, email):
                log("Codex OAuth 账号选择页: 账号选择已提交，等待页面跳转")
                account_chooser_grace_until = max(
                    account_chooser_grace_until,
                    time.time() + ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS,
                )
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            if _handle_account_chooser(page, email, log):
                account_chooser_grace_until = time.time() + ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            raise RuntimeError("Codex OAuth 账号选择页未找到匹配账号或切换入口")

        email_selector = None
        try:
            email_selector = next(
                selector
                for selector in EMAIL_INPUT_SELECTORS
                if page.locator(selector).first.is_visible(timeout=500)
            )
        except Exception:
            email_selector = None
        if email_selector:
            if email_submit_grace_until and time.time() < email_submit_grace_until:
                if repeated <= 1:
                    log("Codex OAuth 邮箱页: 已提交，等待页面异步跳转")
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            if not _fill_input_like_user(page, email_selector, email):
                raise RuntimeError("Codex OAuth 邮箱页填写失败")
            log(f"Codex OAuth 邮箱页输入框: {email_selector}")
            email_submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
            if not email_submit_selector and _press_enter_on_input(page, email_selector):
                email_submit_selector = f"{email_selector} keyboard Enter"
                log("Codex OAuth 邮箱页按钮不可点击，已在邮箱输入框按 Enter")
            if not email_submit_selector:
                raise RuntimeError("Codex OAuth 邮箱页未找到 Continue 按钮")
            email_submit_grace_until = time.time() + EMAIL_SUBMIT_GRACE_SECONDS
            log(f"Codex OAuth 邮箱页已点击 Continue: {email_submit_selector}")
            if _sleep_until_callback(callback_server, 1):
                return callback_server.wait(1)
            continue

        password_visible = False
        try:
            password_visible = any(
                page.locator(selector).first.is_visible(timeout=500)
                for selector in PASSWORD_INPUT_SELECTORS
            )
        except Exception:
            password_visible = False
        if password_visible or page_type == "login_password":
            if passwordless_login_requested:
                if time.time() < passwordless_login_grace_until:
                    if _sleep_until_callback(callback_server, 1):
                        return callback_server.wait(1)
                    continue
                raise RuntimeError("Codex OAuth 已选择邮箱验证码登录，但页面未进入验证码阶段")
            if prefer_email_otp and callable(otp_callback):
                log("Codex OAuth 账号由邮箱验证码注册，跳过未在远端设置的本地生成密码")
                if not _request_oauth_email_otp(page, log, reason="registration_auth_mode=email_otp"):
                    raise RuntimeError("Codex OAuth 邮箱验证码账号未找到可见且可点击的验证码登录按钮")
                passwordless_login_requested = True
                passwordless_login_grace_until = time.time() + PASSWORDLESS_LOGIN_GRACE_SECONDS
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            password_result = _submit_oauth_password_direct(page, password, log)
            if not password_result.get("ok"):
                password_error = str(password_result.get("text") or password_result.get("url") or "")
                if (
                    callable(otp_callback)
                    and not passwordless_login_requested
                    and _is_incorrect_password_error(password_error)
                    and _request_oauth_email_otp(page, log, reason="密码校验失败，切换恢复")
                ):
                    passwordless_login_requested = True
                    passwordless_login_grace_until = time.time() + PASSWORDLESS_LOGIN_GRACE_SECONDS
                    if _sleep_until_callback(callback_server, 1):
                        return callback_server.wait(1)
                    continue
                raise RuntimeError(f"Codex OAuth 密码页失败: {password_error}")
            if _sleep_until_callback(callback_server, 1):
                return callback_server.wait(1)
            continue

        if page_type == "email_otp_verification":
            if not callable(otp_callback):
                raise RuntimeError("Codex OAuth 登录触发邮箱验证码，但该账号未绑定可用验证邮箱")
            log("Codex OAuth 需要邮箱验证码，正在读取绑定邮箱")
            code = str(otp_callback() or "").strip()
            otp_result = _submit_otp_via_page(page, code, log, otp_callback=otp_callback)
            if not otp_result.get("ok"):
                raise RuntimeError(f"Codex OAuth 验证码提交失败: {otp_result.get('text') or otp_result.get('url')}")
            if _sleep_until_callback(callback_server, 1):
                return callback_server.wait(1)
            continue

        if page_type == "add_phone":
            if callable(phone_callback):
                log("Codex OAuth 检测到 add_phone，开始短信验证")
                _handle_add_phone_challenge(
                    page,
                    phone_callback,
                    log=log,
                    resume_url=auth_url,
                )
                if _sleep_until_callback(callback_server, 1):
                    return callback_server.wait(1)
                continue
            if _try_skip_add_phone(page, auth_url=auth_url, callback_server=callback_server, log=log):
                return callback_server.wait(10)
            raise RuntimeError("Codex OAuth 登录触发 add_phone，但未配置可用接码服务")

        if consent_like_page:
            if _click_continue_like_button(page, log, page_type or "Codex OAuth 授权确认"):
                callback_wait = 3 if page_type == "consent" or "consent" in current_url else 1
                if _sleep_until_callback(callback_server, callback_wait):
                    return callback_server.wait(1)
                continue

        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"Codex OAuth 页面错误: {error_text}")

        if repeated > 12:
            raise RuntimeError(f"Codex OAuth 页面停滞: page={page_type or '-'} url={current_url[:160]}")
        if _sleep_until_callback(callback_server, 1):
            return callback_server.wait(1)

    raise RuntimeError("Codex OAuth 浏览器登录超时")


def _finalize_codex_oauth_callback(
    callback: dict[str, str],
    *,
    expected_state: str,
    pkce: PKCECodes,
    email: str,
    proxy: str | None,
    auth_dir: str | os.PathLike[str] | None,
    log: Callable[[str], None],
) -> dict[str, Any]:
    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise RuntimeError(f"Codex OAuth 回调失败: {detail}")
    if callback.get("state") != expected_state:
        raise RuntimeError("Codex OAuth state 校验失败")
    code = str(callback.get("code") or "").strip()
    if not code:
        raise RuntimeError("Codex OAuth 回调缺少 code")

    log("Codex OAuth 回调已收到，正在交换 token")
    token_payload = _exchange_code_for_tokens(code, pkce, proxy=proxy, log=log)
    expires_in = int(token_payload.get("expires_in") or 0)
    identity = _token_identity(str(token_payload.get("id_token") or ""))
    identity_email = str(identity.get("email") or "").strip()
    if identity_email and _normalize_email_for_compare(identity_email) != _normalize_email_for_compare(email):
        raise RuntimeError(f"Codex OAuth 返回邮箱不匹配: 预期 {email}，实际 {identity_email}")
    expires_at = ""
    if expires_in > 0:
        expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    token_record = {
        "id_token": str(token_payload.get("id_token") or ""),
        "access_token": str(token_payload.get("access_token") or ""),
        "refresh_token": str(token_payload.get("refresh_token") or ""),
        "account_id": identity.get("account_id") or "",
        "last_refresh": _utcnow(),
        "email": identity_email or email,
        "type": "codex",
        "expired": expires_at,
    }
    auth_path = _save_codex_auth_file(token_record, auth_dir=auth_dir)
    log(f"Codex OAuth 登录数据已保存: {auth_path}")

    return {
        "message": "Codex OAuth 授权完成",
        "codex_auth_path": str(auth_path),
        "codex_email": token_record["email"],
        "codex_account_id": token_record["account_id"],
        "codex_plan_type": identity.get("plan_type") or "unknown",
        "codex_access_token": token_record["access_token"],
        "codex_refresh_token": token_record["refresh_token"],
        "codex_id_token": token_record["id_token"],
        "codex_expires_at": token_record["expired"],
        "codex_last_refresh": token_record["last_refresh"],
        "codex_access_token_preview": _mask_secret(token_record["access_token"]),
        "codex_refresh_token_preview": _mask_secret(token_record["refresh_token"]),
        "codex_id_token_preview": _mask_secret(token_record["id_token"]),
    }


def _is_codex_browser_login_timeout(exc: Exception) -> bool:
    return "Codex OAuth 浏览器登录超时" in str(exc or "")


def perform_codex_oauth_login_on_page(
    page,
    *,
    email: str,
    password: str,
    registration_auth_mode: str = "",
    proxy: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    otp_callback: Callable[[], str] | None = None,
    phone_callback: Callable[[], str] | None = None,
    auth_dir: str | os.PathLike[str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    log = log_fn or (lambda _message: None)
    email = str(email or "").strip()
    password = str(password or "")
    normalized_auth_mode = str(registration_auth_mode or "").strip().lower()
    if not email:
        raise RuntimeError("Codex OAuth 需要账号邮箱")
    if not password and normalized_auth_mode != "email_otp":
        raise RuntimeError("Codex OAuth 需要账号密码")

    last_error: Exception | None = None
    for attempt_index in range(1, CODEX_BROWSER_LOGIN_MAX_ATTEMPTS + 1):
        attempt = _new_codex_oauth_attempt()
        suffix = f" ({attempt_index}/{CODEX_BROWSER_LOGIN_MAX_ATTEMPTS})" if attempt_index > 1 else ""
        log(f"Codex OAuth 授权链接已生成，复用当前浏览器窗口{suffix}")

        try:
            with _OAuthCallbackServer(port=CODEX_CALLBACK_PORT, state=attempt.state, log=log) as callback_server:
                callback = _drive_codex_oauth_page(
                    page,
                    auth_url=attempt.auth_url,
                    email=email,
                    password=password,
                    callback_server=callback_server,
                    log=log,
                    otp_callback=otp_callback,
                    phone_callback=phone_callback,
                    timeout=timeout,
                    registration_auth_mode=registration_auth_mode,
                )
            return _finalize_codex_oauth_callback(
                callback,
                expected_state=attempt.state,
                pkce=attempt.pkce,
                email=email,
                proxy=proxy,
                auth_dir=auth_dir,
                log=log,
            )
        except Exception as exc:
            last_error = exc
            if not _is_codex_browser_login_timeout(exc) or attempt_index >= CODEX_BROWSER_LOGIN_MAX_ATTEMPTS:
                raise
            log("Codex OAuth 浏览器登录超时，重新生成授权链接重试")
    raise RuntimeError(f"Codex OAuth 授权失败: {last_error}")


def perform_codex_oauth_login(
    *,
    email: str,
    password: str,
    registration_auth_mode: str = "",
    proxy: str | None = None,
    headless: bool = True,
    log_fn: Callable[[str], None] | None = None,
    otp_callback: Callable[[], str] | None = None,
    phone_callback: Callable[[], str] | None = None,
    auth_dir: str | os.PathLike[str] | None = None,
    timeout: int = 300,
    backend_config: BrowserBackendConfig | None = None,
    keep_browser_open: bool = False,
) -> dict[str, Any]:
    log = log_fn or (lambda _message: None)
    email = str(email or "").strip()
    password = str(password or "")
    normalized_auth_mode = str(registration_auth_mode or "").strip().lower()
    if not email:
        raise RuntimeError("Codex OAuth 需要账号邮箱")
    if not password and normalized_auth_mode != "email_otp":
        raise RuntimeError("Codex OAuth 需要账号密码")

    browser_config = backend_config or BrowserBackendConfig.camoufox(headless=bool(headless))
    launch_opts = {"headless": browser_config.is_headless}
    if browser_config.is_camoufox:
        proxy_config = _build_proxy_config(proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config
            launch_opts["geoip"] = True
    _apply_camoufox_visible_window_limit(launch_opts, browser_config)

    browser_context = open_browser_backend(
        launch_opts=launch_opts,
        config=browser_config,
        camoufox_class=Camoufox,
        log=log,
    )
    browser = browser_context.__enter__()
    keep_open = bool(keep_browser_open and not browser_config.is_headless)
    try:
        page = browser.new_page()
        last_error: Exception | None = None
        for attempt_index in range(1, CODEX_BROWSER_LOGIN_MAX_ATTEMPTS + 1):
            attempt = _new_codex_oauth_attempt()
            suffix = f" ({attempt_index}/{CODEX_BROWSER_LOGIN_MAX_ATTEMPTS})" if attempt_index > 1 else ""
            log(f"Codex OAuth 授权链接已生成，启动本地回调服务{suffix}")
            try:
                with _OAuthCallbackServer(port=CODEX_CALLBACK_PORT, state=attempt.state, log=log) as callback_server:
                    callback = _drive_codex_oauth_page(
                        page,
                        auth_url=attempt.auth_url,
                        email=email,
                        password=password,
                        callback_server=callback_server,
                        log=log,
                        otp_callback=otp_callback,
                        phone_callback=phone_callback,
                        timeout=timeout,
                        registration_auth_mode=registration_auth_mode,
                    )
                return _finalize_codex_oauth_callback(
                    callback,
                    expected_state=attempt.state,
                    pkce=attempt.pkce,
                    email=email,
                    proxy=proxy,
                    auth_dir=auth_dir,
                    log=log,
                )
            except Exception as exc:
                last_error = exc
                if not _is_codex_browser_login_timeout(exc) or attempt_index >= CODEX_BROWSER_LOGIN_MAX_ATTEMPTS:
                    raise
                log("Codex OAuth 浏览器登录超时，重新生成授权链接重试")
        raise RuntimeError(f"Codex OAuth 授权失败: {last_error}")
    finally:
        if keep_open:
            keep_browser_context_open(browser_context, browser, label=f"codex-oauth:{email}")
            log("Codex OAuth 浏览器窗口已保留，可手动关闭")
        else:
            browser_context.__exit__(*sys.exc_info())
