from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .._browser_backend import BrowserBackendConfig, open_browser_backend
from .browser_register import (
    EMAIL_INPUT_SELECTORS,
    EMAIL_SUBMIT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    Camoufox,
    _apply_camoufox_visible_window_limit,
    _build_proxy_config,
    _click_first,
    _derive_registration_state_from_page,
    _extract_auth_error_text,
    _fill_input_like_user,
    _goto_with_retry,
    _submit_otp_via_page,
    _submit_oauth_password_direct,
)


CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"
CODEX_USER_AGENT = "codex_cli_rs/0.144.1 (Windows 10.0.0; x86_64)"
DEFAULT_CODEX_AUTH_DIR = Path("data") / "codex_auths"


@dataclass(slots=True)
class PKCECodes:
    code_verifier: str
    code_challenge: str


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
    def __init__(self, *, port: int = CODEX_CALLBACK_PORT):
        self.port = int(port)
        self.event = threading.Event()
        self.result: dict[str, str] = {}
        owner = self

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
                owner.result = {
                    "code": (query.get("code") or [""])[0],
                    "state": (query.get("state") or [""])[0],
                    "error": (query.get("error") or [""])[0],
                    "error_description": (query.get("error_description") or [""])[0],
                }
                owner.event.set()
                self.send_response(302)
                self.send_header("Location", "/success")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_OAuthCallbackServer":
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def wait(self, timeout: int) -> dict[str, str]:
        if not self.event.wait(max(int(timeout), 1)):
            raise RuntimeError("等待 Codex OAuth 回调超时")
        return dict(self.result)


def _exchange_code_for_tokens(
    code: str,
    pkce: PKCECodes,
    *,
    proxy: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
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


def _drive_codex_oauth_page(
    page,
    *,
    auth_url: str,
    email: str,
    password: str,
    callback_server: _OAuthCallbackServer,
    log: Callable[[str], None],
    otp_callback: Callable[[], str] | None,
    timeout: int,
) -> dict[str, str]:
    _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=45000, log=log)
    deadline = time.time() + max(int(timeout), 30)
    last_signature = ""
    repeated = 0

    while time.time() < deadline:
        if callback_server.event.is_set():
            return callback_server.wait(1)

        current_url = str(getattr(page, "url", "") or "")
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        signature = f"{page_type}|{current_url}"
        if signature == last_signature:
            repeated += 1
        else:
            repeated = 0
            last_signature = signature
        log(f"Codex OAuth 页面: page={page_type or '-'} url={current_url[:110]}")

        if "localhost:1455/auth/callback" in current_url or "localhost:1455/success" in current_url:
            return callback_server.wait(10)

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
            if not _fill_input_like_user(page, email_selector, email):
                raise RuntimeError("Codex OAuth 邮箱页填写失败")
            log(f"Codex OAuth 邮箱页输入框: {email_selector}")
            if not _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8):
                raise RuntimeError("Codex OAuth 邮箱页未找到 Continue 按钮")
            time.sleep(1)
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
            password_result = _submit_oauth_password_direct(page, password, log)
            if not password_result.get("ok"):
                raise RuntimeError(f"Codex OAuth 密码页失败: {password_result.get('text') or password_result.get('url')}")
            time.sleep(1)
            continue

        if page_type == "email_otp_verification":
            if not callable(otp_callback):
                raise RuntimeError("Codex OAuth 登录触发邮箱验证码，但该账号未绑定可用验证邮箱")
            log("Codex OAuth 需要邮箱验证码，正在读取绑定邮箱")
            code = str(otp_callback() or "").strip()
            otp_result = _submit_otp_via_page(page, code, log)
            if not otp_result.get("ok"):
                raise RuntimeError(f"Codex OAuth 验证码提交失败: {otp_result.get('text') or otp_result.get('url')}")
            time.sleep(1)
            continue

        if page_type in {"consent", "workspace_selection", "organization_selection"} or any(
            token in current_url for token in ("sign-in-with-chatgpt", "workspace", "organization", "consent")
        ):
            if _click_continue_like_button(page, log, page_type or "Codex OAuth 授权确认"):
                time.sleep(1)
                continue

        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"Codex OAuth 页面错误: {error_text}")

        if repeated > 12:
            raise RuntimeError(f"Codex OAuth 页面停滞: page={page_type or '-'} url={current_url[:160]}")
        time.sleep(1)

    raise RuntimeError("Codex OAuth 浏览器登录超时")


def perform_codex_oauth_login(
    *,
    email: str,
    password: str,
    proxy: str | None = None,
    headless: bool = True,
    log_fn: Callable[[str], None] | None = None,
    otp_callback: Callable[[], str] | None = None,
    auth_dir: str | os.PathLike[str] | None = None,
    timeout: int = 300,
    backend_config: BrowserBackendConfig | None = None,
) -> dict[str, Any]:
    log = log_fn or (lambda _message: None)
    email = str(email or "").strip()
    password = str(password or "")
    if not email:
        raise RuntimeError("Codex OAuth 需要账号邮箱")
    if not password:
        raise RuntimeError("Codex OAuth 需要账号密码")

    pkce = generate_pkce_codes()
    state = secrets.token_urlsafe(32)
    auth_url = build_codex_authorize_url(state, pkce)
    log("Codex OAuth 授权链接已生成，启动本地回调服务")

    browser_config = backend_config or BrowserBackendConfig.camoufox(headless=bool(headless))
    launch_opts = {"headless": browser_config.is_headless}
    if browser_config.is_camoufox:
        proxy_config = _build_proxy_config(proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config
            launch_opts["geoip"] = True
    _apply_camoufox_visible_window_limit(launch_opts, browser_config)

    with _OAuthCallbackServer(port=CODEX_CALLBACK_PORT) as callback_server:
        with open_browser_backend(
            launch_opts=launch_opts,
            config=browser_config,
            camoufox_class=Camoufox,
            log=log,
        ) as browser:
            page = browser.new_page()
            callback = _drive_codex_oauth_page(
                page,
                auth_url=auth_url,
                email=email,
                password=password,
                callback_server=callback_server,
                log=log,
                otp_callback=otp_callback,
                timeout=timeout,
            )

    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise RuntimeError(f"Codex OAuth 回调失败: {detail}")
    if callback.get("state") != state:
        raise RuntimeError("Codex OAuth state 校验失败")
    code = str(callback.get("code") or "").strip()
    if not code:
        raise RuntimeError("Codex OAuth 回调缺少 code")

    log("Codex OAuth 回调已收到，正在交换 token")
    token_payload = _exchange_code_for_tokens(code, pkce, proxy=proxy)
    expires_in = int(token_payload.get("expires_in") or 0)
    identity = _token_identity(str(token_payload.get("id_token") or ""))
    expires_at = ""
    if expires_in > 0:
        expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    token_record = {
        "id_token": str(token_payload.get("id_token") or ""),
        "access_token": str(token_payload.get("access_token") or ""),
        "refresh_token": str(token_payload.get("refresh_token") or ""),
        "account_id": identity.get("account_id") or "",
        "last_refresh": _utcnow(),
        "email": identity.get("email") or email,
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
