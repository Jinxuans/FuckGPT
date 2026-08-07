from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from platforms._browser_backend import (
    BrowserBackendConfig,
    keep_browser_context_open,
    open_browser_backend,
    parse_checkout_mode,
)

from .constants import CHATGPT_APP


BROWSER_STATE_SCHEMA = 1
BROWSER_STATE_DIR = Path("data") / "browser_states" / "chatgpt"
STATE_COOKIE_URLS = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://auth.openai.com",
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_state_stem(*, email: str = "", account_id: str = "") -> str:
    identity = f"{account_id or ''}:{email or ''}".strip(":") or "unknown"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    prefix_source = str(account_id or email.split("@")[0] or "account")
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix_source).strip("._-")[:48]
    return f"{prefix or 'account'}_{digest}"


def _state_file_path(*, email: str = "", account_id: str = "") -> Path:
    return (BROWSER_STATE_DIR / f"{_safe_state_stem(email=email, account_id=account_id)}.storage.json").resolve()


def _context_storage_state(context: Any) -> dict[str, Any]:
    storage_state = getattr(context, "storage_state", None)
    if callable(storage_state):
        try:
            state = storage_state(indexed_db=True)
        except TypeError:
            state = storage_state()
        if isinstance(state, dict):
            return state
    return {}


def _current_origin(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _local_storage_snapshot(page: Any) -> list[dict[str, Any]]:
    try:
        items = page.evaluate(
            """
            () => Object.keys(window.localStorage || {}).map((name) => ({
              name,
              value: window.localStorage.getItem(name) || "",
            }))
            """
        )
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        result.append({"name": name, "value": str(item.get("value") or "")})
    return result


def _fallback_storage_state(page: Any) -> dict[str, Any]:
    context = getattr(page, "context", None)
    cookies: list[dict[str, Any]] = []
    try:
        raw_cookies = context.cookies() if context is not None else []
        cookies = [dict(item) for item in raw_cookies if isinstance(item, dict)]
    except Exception:
        cookies = []

    origins: list[dict[str, Any]] = []
    origin = _current_origin(str(getattr(page, "url", "") or ""))
    local_storage = _local_storage_snapshot(page)
    if origin and local_storage:
        origins.append({"origin": origin, "localStorage": local_storage})
    return {"cookies": cookies, "origins": origins}


def normalize_storage_state(state: dict[str, Any]) -> dict[str, Any]:
    cookies = state.get("cookies") if isinstance(state.get("cookies"), list) else []
    origins = state.get("origins") if isinstance(state.get("origins"), list) else []
    normalized: dict[str, Any] = {
        "cookies": [dict(item) for item in cookies if isinstance(item, dict)],
        "origins": [dict(item) for item in origins if isinstance(item, dict)],
    }
    # Preserve future Playwright fields, including indexedDB snapshots, while
    # keeping the top-level shape valid for browser.new_context(storage_state=...).
    for key, value in state.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def save_browser_state(
    page: Any,
    *,
    email: str = "",
    account_id: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    context = getattr(page, "context", None)
    state = _context_storage_state(context) if context is not None else {}
    if not state:
        state = _fallback_storage_state(page)
    state = normalize_storage_state(state)
    cookies = state.get("cookies") if isinstance(state.get("cookies"), list) else []
    origins = state.get("origins") if isinstance(state.get("origins"), list) else []
    if not cookies and not origins:
        raise RuntimeError("浏览器上下文没有可保存的 cookies/localStorage")

    path = _state_file_path(email=email, account_id=account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(state)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, path)
    digest = hashlib.sha256(payload).hexdigest()
    saved_at = utcnow_iso()
    _log(log, f"ChatGPT 浏览器状态已保存: {path}")
    return {
        "browser_state_path": str(path),
        "browser_state_saved_at": saved_at,
        "browser_state_schema": BROWSER_STATE_SCHEMA,
        "browser_state_sha256": digest,
        "browser_state_cookie_count": len(cookies),
        "browser_state_origin_count": len(origins),
    }


def resolve_browser_state_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except Exception:
        path = path.absolute()
    return path if path.exists() and path.is_file() else None


def load_browser_state(path: str | os.PathLike[str]) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("浏览器状态文件不是 JSON 对象")
    return normalize_storage_state(data)


def _parse_cookie_header(cookies: Any) -> dict[str, str]:
    if isinstance(cookies, dict):
        return {
            str(name).strip(): str(value)
            for name, value in cookies.items()
            if str(name or "").strip() and value not in (None, "")
        }
    if isinstance(cookies, list):
        parsed: dict[str, str] = {}
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = item.get("value")
            if name and value not in (None, ""):
                parsed[name] = str(value)
        return parsed
    text = str(cookies or "").strip()
    if not text:
        return {}
    if text[:1] in {"{", "["}:
        try:
            return _parse_cookie_header(json.loads(text))
        except Exception:
            pass
    parsed: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            parsed[name] = value.strip()
    return parsed


def _cookie_entries_from_account(extra: dict[str, Any]) -> list[dict[str, Any]]:
    cookie_map = _parse_cookie_header(extra.get("cookies"))
    session_token = str(extra.get("session_token") or "").strip()
    if session_token:
        cookie_map["__Secure-next-auth.session-token"] = session_token
    entries: list[dict[str, Any]] = []
    for name, value in cookie_map.items():
        if not name or value in (None, ""):
            continue
        if name in {"oai-did", "oaicom-stable-id"}:
            urls = STATE_COOKIE_URLS
        elif name == "__Secure-next-auth.session-token":
            urls = ("https://chatgpt.com", "https://chat.openai.com")
        else:
            urls = ("https://chatgpt.com",)
        for url in urls:
            entries.append({"name": name, "value": str(value), "url": url, "secure": url.startswith("https://")})
    return entries


def seed_browser_state(page: Any, account_extra: dict[str, Any], *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    extra = dict(account_extra or {})
    state_path = resolve_browser_state_path(extra.get("browser_state_path"))
    injected_cookie_count = 0
    restored_origins = 0
    if state_path:
        state = load_browser_state(state_path)
        cookies = state.get("cookies") if isinstance(state.get("cookies"), list) else []
        if cookies:
            page.context.add_cookies(cookies)
            injected_cookie_count += len(cookies)
        origins = state.get("origins") if isinstance(state.get("origins"), list) else []
        for origin_record in origins:
            if not isinstance(origin_record, dict):
                continue
            origin = str(origin_record.get("origin") or "").strip()
            local_storage = origin_record.get("localStorage")
            if not origin.startswith("http") or not isinstance(local_storage, list) or not local_storage:
                continue
            try:
                page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
                page.evaluate(
                    """
                    (items) => {
                      for (const item of items || []) {
                        if (!item || !item.name) continue;
                        window.localStorage.setItem(String(item.name), String(item.value || ""));
                      }
                    }
                    """,
                    local_storage,
                )
                restored_origins += 1
            except Exception as exc:
                _log(log, f"恢复 localStorage 失败 origin={origin}: {exc}")
        _log(log, f"已从浏览器状态文件注入登录状态: cookies={injected_cookie_count}, origins={restored_origins}")
        return {
            "seeded": True,
            "source": "browser_state",
            "browser_state_path": str(state_path),
            "injected_cookie_count": injected_cookie_count,
            "restored_origin_count": restored_origins,
        }

    fallback_cookies = _cookie_entries_from_account(extra)
    if fallback_cookies:
        page.context.add_cookies(fallback_cookies)
        _log(log, f"未找到完整浏览器状态文件，已回退注入账号 Cookie: {len(fallback_cookies)} 项")
        return {
            "seeded": True,
            "source": "account_cookies",
            "browser_state_path": "",
            "injected_cookie_count": len(fallback_cookies),
            "restored_origin_count": 0,
        }
    return {"seeded": False, "source": "none", "browser_state_path": "", "injected_cookie_count": 0, "restored_origin_count": 0}


def verify_chatgpt_session(page: Any, *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    session_url = f"{CHATGPT_APP}/api/auth/session"
    try:
        if "chatgpt.com" not in str(getattr(page, "url", "") or ""):
            page.goto(CHATGPT_APP, wait_until="domcontentloaded", timeout=30_000)
        payload = page.evaluate(
            """
            async (sessionUrl) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return { status: response.status, url: response.url, text: await response.text() };
            }
            """,
            session_url,
        )
    except Exception as exc:
        _log(log, f"ChatGPT 浏览器登录态验证失败: {exc}")
        return {"ok": False, "status": 0, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "status": 0, "error": "session API 未返回对象"}
    status = int(payload.get("status") or 0)
    text = str(payload.get("text") or "")
    access_token = ""
    profile: dict[str, Any] = {}
    if status == 200 and text:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
                profile = data.get("user") if isinstance(data.get("user"), dict) else {}
        except Exception as exc:
            return {"ok": False, "status": status, "error": f"session JSON 解析失败: {exc}"}
    return {
        "ok": bool(access_token),
        "status": status,
        "session_url": str(payload.get("url") or session_url),
        "access_token_present": bool(access_token),
        "remote_email": str(profile.get("email") or ""),
        "profile": profile,
    }


def build_browser_backend_config(
    browser_mode: str,
    *,
    bit_profile_id: str = "",
    bit_api_url: str = "",
    bit_api_token: str = "",
) -> BrowserBackendConfig:
    mode = str(browser_mode or "camoufox_headed").strip().lower()
    if mode in {"headed", "browser", "visible", ""}:
        mode = "camoufox_headed"
    if mode == "headless":
        mode = "camoufox_headless"
    return parse_checkout_mode(
        mode,
        bit_profile_id=bit_profile_id,
        bit_api_url=bit_api_url,
        bit_api_token=bit_api_token,
    )


def launch_browser_with_state(
    *,
    account_email: str,
    account_id: str,
    account_extra: dict[str, Any],
    proxy: str | None = None,
    browser_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    bit_api_url: str = "",
    bit_api_token: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from .browser_register import Camoufox, _apply_camoufox_visible_window_limit, _build_proxy_config

    config = build_browser_backend_config(
        browser_mode,
        bit_profile_id=bit_profile_id,
        bit_api_url=bit_api_url,
        bit_api_token=bit_api_token,
    )
    launch_opts: dict[str, Any] = {"headless": config.is_headless}
    if config.is_camoufox:
        proxy_config = _build_proxy_config(proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config
            launch_opts["geoip"] = True
    _apply_camoufox_visible_window_limit(launch_opts, config)

    browser_context = open_browser_backend(
        launch_opts=launch_opts,
        config=config,
        camoufox_class=Camoufox,
        log=log or print,
    )
    browser = browser_context.__enter__()
    kept = False
    try:
        state_path = resolve_browser_state_path((account_extra or {}).get("browser_state_path"))
        page = None
        used_context_storage = False
        if config.is_camoufox and state_path:
            try:
                context = browser.new_context(storage_state=str(state_path))
                page = context.new_page()
                used_context_storage = True
                _log(log, f"启动浏览器已通过 storage_state 加载状态: {state_path}")
            except Exception as exc:
                _log(log, f"storage_state 加载失败，改用运行期注入: {exc}")
        if page is None:
            page = browser.new_page()
            seed_result = seed_browser_state(page, account_extra, log=log)
        else:
            seed_result = {
                "seeded": True,
                "source": "storage_state_context",
                "browser_state_path": str(state_path),
                "injected_cookie_count": 0,
                "restored_origin_count": 0,
            }

        page.goto(CHATGPT_APP, wait_until="domcontentloaded", timeout=30_000)
        verification = verify_chatgpt_session(page, log=log)
        keep_browser_context_open(browser_context, browser, label=f"chatgpt-session:{account_email or account_id}")
        kept = True
        return {
            "message": "浏览器已启动并注入账号登录状态",
            "browser_mode": str(browser_mode or "camoufox_headed"),
            "backend": config.backend,
            "window_mode": config.window_mode,
            "url": str(getattr(page, "url", "") or CHATGPT_APP),
            "browser_kept_open": True,
            "used_context_storage": used_context_storage,
            "state_seed": seed_result,
            "session_restored": bool(verification.get("ok")),
            "session_verification": verification,
        }
    finally:
        if not kept:
            browser_context.__exit__(*sys.exc_info())
