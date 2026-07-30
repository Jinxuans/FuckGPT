from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

from application.accounts import AccountsService
from core.db import AccountModel, KakaoPipelineModel, ProviderSettingModel, engine
from core.platform_accounts import build_platform_account
from domain.accounts import AccountQuery
from domain.actions import ActionExecutionCommand
from infrastructure.platform_runtime import PlatformRuntime
from infrastructure.provider_settings_repository import ProviderSettingsRepository

from .client import CustomerApiClient, CustomerApiProblem, normalize_base_url
from .workstation_client import WorkstationScannerClient


KAKAO_PROVIDER_TYPE = "kakao_pipeline"
SCANNER_KINDS = ("scanner", "scanner_546789")
SCANNER_BASE_URL = "https://customer.i7wap.xyz"
LEGACY_SCANNER_BASE_URLS = {"https://upi.i7wap.xyz"}
SETTING_DEFAULTS = {
    "supplier": {
        "display_name": "Kakao 提链供应商",
        "base_url": "http://127.0.0.1:8788",
        "env_url": "KAKAO_SUPPLIER_BASE_URL",
        "env_key": "KAKAO_SUPPLIER_CDK_KEY",
    },
    "scanner": {
        "display_name": "I7wap 扫码平台",
        "base_url": SCANNER_BASE_URL,
        "env_url": "KAKAO_SCANNER_BASE_URL",
        "env_key": "KAKAO_SCANNER_CDK_KEY",
        "driver_type": "customer_api",
    },
    "scanner_546789": {
        "display_name": "546789 扫码平台",
        "base_url": "https://kakao.546789.shop",
        "env_url": "KAKAO_546789_BASE_URL",
        "env_key": "KAKAO_546789_CDK_KEY",
        "driver_type": "payment_submission",
    },
}

ACTIVE_STATES = {
    "supplier_submitting",
    "supplier_processing",
    "scanner_submitting",
    "scanner_processing",
    "plus_checking",
}

TERMINAL_REMOTE_FAILURES = {"FAILED", "CANCELLED", "EXPIRED", "REJECTED"}
REMOTE_SUCCESS_STATUSES = {"SUCCESS", "SUCCEEDED", "COMPLETED", "CONFIRMED", "PLUS", "ACTIVE"}

_account_locks: dict[int, threading.RLock] = {}
_account_locks_guard = threading.Lock()
_cdk_pool_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _account_lock(account_id: int) -> threading.RLock:
    with _account_locks_guard:
        return _account_locks.setdefault(int(account_id), threading.RLock())


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}{'*' * min(len(text) - 6, 12)}{text[-3:]}"


def _parse_cdk_keys(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[\r\n,]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = _text(item)
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _is_cdk_depleted(code: str, message: str) -> bool:
    text = f"{code} {message}".lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
    return (
        any(token in text for token in (
            "insufficient_cdk",
            "cdk_insufficient",
            "cdk_quota_exhausted",
            "cdk_uses_exhausted",
        ))
        or ("cdk" in compact and any(token in compact for token in ("insufficient", "nouses", "exhausted", "quota")))
        or any(token in compact for token in ("cdk可用次数不足", "cdk次数不足", "cdk额度不足", "cdk已用完"))
    )


def _is_workstation_cdk_depleted(code: str, message: str) -> bool:
    if _is_cdk_depleted(code, message):
        return True
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", f"{code} {message}".lower())
    return any(token in compact for token in ("额度不足", "余额不足", "次数不足", "quotaexhausted", "insufficientquota"))


def _normalized_key(value: object) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    key = _normalized_key(value)
    compact = key.replace("_", "")
    if compact in {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "customertoken",
        "cdkkey",
        "authorization",
        "password",
        "cookie",
        "cookies",
        "secret",
    }:
        return True
    return compact.endswith(("accesstoken", "customertoken", "cdkkey", "password", "secret"))


def sanitize_remote(value: Any, *, key: object = "") -> Any:
    if _is_secret_key(key):
        return "***"
    if isinstance(value, dict):
        return {str(child_key): sanitize_remote(child_value, key=child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_remote(item, key=key) for item in value]
    return value


def _data(payload: dict) -> dict:
    value = payload.get("data") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _nested_text(payload: dict, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        text = _text(current)
        if text:
            return text
    return ""


def _append_event(model: KakaoPipelineModel, message: str, *, level: str = "info", detail: dict | None = None) -> None:
    events = model.get_events()
    events.append(
        {
            "time": _utcnow_iso(),
            "level": level,
            "message": str(message or "")[:1000],
            "detail": sanitize_remote(detail or {}),
        }
    )
    model.set_events(events[-200:])


def _set_error(model: KakaoPipelineModel, state: str, code: str, message: str) -> None:
    model.state = state
    model.last_error_code = str(code or "unknown_error")[:120]
    model.last_error_message = str(message or "未知错误")[:1000]
    model.updated_at = _utcnow()
    _append_event(model, model.last_error_message, level="error", detail={"code": model.last_error_code})


def _problem_from_payload(data: dict, default_code: str, default_message: str) -> tuple[str, str]:
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    return (
        _text(error.get("code") or data.get("problemCode") or default_code),
        _text(error.get("message") or data.get("problemReason") or default_message),
    )


def _valid_payment_url(value: str) -> str:
    text = _text(value)
    parsed = urlsplit(text)
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or not (host == "nicepay.co.kr" or host.endswith(".nicepay.co.kr")):
        raise ValueError("供应商返回的不是受支持的 NicePay HTTPS 长链")
    return text


def _find_scan_value(value: Any, wanted: set[str]) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in wanted and _text(child):
                return _text(child)
        for child in value.values():
            found = _find_scan_value(child, wanted)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_scan_value(child, wanted)
            if found:
                return found
    return ""


def _safe_scan_url(value: str) -> str:
    text = _text(value)
    parsed = urlsplit(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _scanner_outcome(data: dict) -> str:
    main_status = _text(data.get("status")).upper()
    subscription = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    subscription_status = _text(subscription.get("status") or subscription.get("plan") or subscription.get("planType")).upper()
    if main_status in TERMINAL_REMOTE_FAILURES or subscription_status in TERMINAL_REMOTE_FAILURES:
        return "failed"
    if main_status in REMOTE_SUCCESS_STATUSES:
        return "success"
    if subscription_status in REMOTE_SUCCESS_STATUSES or any(token in subscription_status for token in ("PLUS", "PRO", "PAID")):
        return "success"
    return "processing"


class KakaoPipelineService:
    def __init__(self):
        self.settings = ProviderSettingsRepository()
        self.accounts = AccountsService()

    def _setting_payload(self, kind: str) -> tuple[ProviderSettingModel | None, dict]:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        defaults = SETTING_DEFAULTS[kind]
        item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
        config = item.get_config() if item else {}
        auth = item.get_auth() if item else {}
        base_url = _text(
            config.get("base_url")
            or os.getenv(str(defaults["env_url"]), "")
            or defaults["base_url"]
        )
        if kind == "scanner" and base_url.rstrip("/") in LEGACY_SCANNER_BASE_URLS:
            base_url = SCANNER_BASE_URL
        has_saved_pool = "cdk_keys" in auth
        cdk_keys = _parse_cdk_keys(auth.get("cdk_keys"))
        if not has_saved_pool and not cdk_keys:
            cdk_keys = _parse_cdk_keys(auth.get("cdk_key") or os.getenv(str(defaults["env_key"]), ""))
        return item, {
            "base_url": base_url,
            "cdk_key": cdk_keys[0] if cdk_keys else "",
            "cdk_keys": cdk_keys,
            "display_name": _text(item.display_name if item else defaults["display_name"]),
            "driver_type": _text(defaults.get("driver_type") or "customer_api"),
        }

    def list_settings(self) -> dict:
        result = {}
        for kind in ("supplier", "scanner", "scanner_546789"):
            item, payload = self._setting_payload(kind)
            result[kind] = {
                "id": int(item.id or 0) if item else None,
                "kind": kind,
                "display_name": payload["display_name"],
                "base_url": payload["base_url"],
                "has_cdk": bool(payload["cdk_keys"]),
                "cdk_count": len(payload["cdk_keys"]),
                "cdk_keys": payload["cdk_keys"],
                "cdk_preview": _mask_secret(payload["cdk_key"]),
                "driver_type": payload["driver_type"],
            }
        result["default_scanner_kind"] = self._default_scanner_kind()
        result["auto_upload_after_extract"] = self._auto_upload_after_extract()
        return result

    def _default_scanner_kind(self) -> str:
        for kind in SCANNER_KINDS:
            item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
            if item and item.get_metadata().get("pipeline_default") is True:
                return kind
        return "scanner"

    def set_default_scanner(self, kind: str) -> dict:
        selected = _text(kind)
        if selected not in SCANNER_KINDS:
            raise ValueError("未知扫码供应商")
        for scanner_kind in SCANNER_KINDS:
            item, current = self._setting_payload(scanner_kind)
            if item is None and scanner_kind != selected:
                continue
            metadata = item.get_metadata() if item else {}
            metadata.update({"temporary_feature": True, "pipeline_default": scanner_kind == selected})
            self.settings.save(
                setting_id=int(item.id or 0) if item else None,
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=scanner_kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": current["cdk_keys"]},
                metadata=metadata,
            )
        return {"ok": True, "default_scanner_kind": selected, "settings": self.list_settings()}

    def _auto_upload_after_extract(self) -> bool:
        for kind in SCANNER_KINDS:
            item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
            if item and item.get_metadata().get("auto_upload_after_extract") is True:
                return True
        return False

    def set_auto_upload(self, enabled: bool) -> dict:
        selected = self._default_scanner_kind()
        for kind in SCANNER_KINDS:
            item, current = self._setting_payload(kind)
            if item is None and kind != selected:
                continue
            metadata = item.get_metadata() if item else {}
            metadata.update({"temporary_feature": True, "auto_upload_after_extract": bool(enabled)})
            self.settings.save(
                setting_id=int(item.id or 0) if item else None,
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": current["cdk_keys"]},
                metadata=metadata,
            )
        return {"ok": True, "auto_upload_after_extract": bool(enabled), "settings": self.list_settings()}

    def _maybe_auto_submit_scanner(self, account_id: int, result: dict) -> dict:
        if result.get("state") != "link_ready" or not self._auto_upload_after_extract():
            return result
        try:
            return self.submit_scanner(account_id)
        except Exception as exc:  # noqa: BLE001
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if pipeline.state == "link_ready":
                    pipeline.last_error_code = "auto_upload_failed"
                    pipeline.last_error_message = f"自动上传扫码失败: {exc}"[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, pipeline.last_error_message, level="warning")
                    session.add(pipeline)
                    session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    def save_setting(self, kind: str, payload: dict) -> dict:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        current, current_payload = self._setting_payload(kind)
        base_url = normalize_base_url(_text(payload.get("base_url") or current_payload["base_url"]))
        cdk_keys = _parse_cdk_keys(payload.get("cdk_keys"))
        if not cdk_keys and _text(payload.get("cdk_key")):
            cdk_keys = _parse_cdk_keys(payload.get("cdk_key"))
        metadata = current.get_metadata() if current else {}
        metadata.update({"temporary_feature": True})
        item = self.settings.save(
            setting_id=int(current.id or 0) if current else None,
            provider_type=KAKAO_PROVIDER_TYPE,
            provider_key=kind,
            display_name=_text(payload.get("display_name") or current_payload["display_name"]),
            auth_mode="apikey",
            enabled=True,
            is_default=True,
            config={"base_url": base_url},
            auth={"cdk_keys": cdk_keys},
            metadata=metadata,
        )
        return {"ok": True, "item": self.list_settings()[kind], "id": int(item.id or 0)}

    def test_setting(self, kind: str, payload: dict | None = None) -> dict:
        _, current = self._setting_payload(kind)
        overrides = dict(payload or {})
        base_url = _text(overrides.get("base_url") or current["base_url"])
        cdk_keys = _parse_cdk_keys(overrides.get("cdk_keys")) or _parse_cdk_keys(overrides.get("cdk_key")) or current["cdk_keys"]
        cdk_key = cdk_keys[0] if cdk_keys else ""
        if kind == "scanner_546789":
            WorkstationScannerClient(base_url, cdk_key).test_connection()
        else:
            CustomerApiClient(base_url, cdk_key).test_connection()
        return {"ok": True, "message": f"{SETTING_DEFAULTS[kind]['display_name']}连接成功"}

    def check_cdks(self, kind: str, payload: dict | None = None) -> dict:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        _, current = self._setting_payload(kind)
        overrides = dict(payload or {})
        base_url = _text(overrides.get("base_url") or current["base_url"])
        cdk_keys = _parse_cdk_keys(overrides.get("cdk_keys")) or _parse_cdk_keys(overrides.get("cdk_key")) or current["cdk_keys"]
        if not cdk_keys:
            raise ValueError("没有可校验的 CDK")
        results = []
        depleted = []
        for key in cdk_keys:
            try:
                client = CustomerApiClient(base_url, key)
                if kind == "scanner":
                    quota = client.check_cdk()
                    total_count = int(quota.get("totalCount") or 0)
                    used_count = int(quota.get("usedCount") or 0)
                    frozen_count = int(quota.get("frozenCount") or 0)
                    available_count = int(quota.get("availableCount") or 0)
                    cdk_status = _text(quota.get("status")).upper() or "UNKNOWN"
                    exhausted = available_count <= 0 or cdk_status in {"EXHAUSTED", "USED", "DEPLETED"}
                    status = "depleted" if exhausted else ("valid" if cdk_status == "ACTIVE" else "invalid")
                    if exhausted:
                        depleted.append(key)
                    results.append(
                        {
                            "cdk_key": key,
                            "status": status,
                            "message": f"可用 {available_count} / 总计 {total_count}，已用 {used_count}，冻结 {frozen_count}",
                            "product_type": _text(quota.get("productType")),
                            "cdk_status": cdk_status,
                            "total_count": total_count,
                            "used_count": used_count,
                            "frozen_count": frozen_count,
                            "available_count": available_count,
                            "expires_at": quota.get("expiresAt"),
                            "remark": _text(quota.get("remark")),
                        }
                    )
                elif kind == "scanner_546789":
                    quota = WorkstationScannerClient(base_url, key).check_cdk()
                    unlimited = quota.get("unlimited") is True
                    remaining = int(quota.get("remaining") or 0)
                    limit = int(quota.get("limit") or 0)
                    exhausted = not unlimited and remaining <= 0
                    status = "depleted" if exhausted else "valid"
                    if exhausted:
                        depleted.append(key)
                    results.append(
                        {
                            "cdk_key": key,
                            "status": status,
                            "message": "无限额度" if unlimited else f"剩余 {remaining} / {limit}",
                            "cdk_status": "UNLIMITED" if unlimited else ("DEPLETED" if exhausted else "ACTIVE"),
                            "total_count": limit,
                            "used_count": max(0, limit - remaining),
                            "frozen_count": 0,
                            "available_count": remaining,
                            "unlimited": unlimited,
                        }
                    )
                else:
                    client.test_connection()
                    results.append({"cdk_key": key, "status": "valid", "message": "鉴权有效；提链接口未提供精确剩余额度"})
            except CustomerApiProblem as exc:
                exhausted = _is_cdk_depleted(exc.code, exc.message)
                results.append({"cdk_key": key, "status": "depleted" if exhausted else "invalid", "message": exc.message})
                if exhausted:
                    depleted.append(key)
            except Exception as exc:  # noqa: BLE001
                results.append({"cdk_key": key, "status": "invalid", "message": str(exc)})
        if depleted:
            self._remove_cdks(kind, depleted)
        return {"ok": True, "items": results, "removed": depleted, "quota_supported": kind in {"scanner", "scanner_546789"}}

    def _remove_cdks(self, kind: str, keys: list[str]) -> list[str]:
        remove = set(_parse_cdk_keys(keys))
        if not remove:
            return []
        with _cdk_pool_lock:
            item, current = self._setting_payload(kind)
            if item is None:
                return []
            remaining = [key for key in current["cdk_keys"] if key not in remove]
            removed = [key for key in current["cdk_keys"] if key in remove]
            if not removed:
                return []
            metadata = item.get_metadata()
            metadata.update({"temporary_feature": True})
            self.settings.save(
                setting_id=int(item.id or 0),
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": remaining},
                metadata=metadata,
            )
            return removed

    @staticmethod
    def _pipeline_for_account(session: Session, account_id: int, *, create: bool = False) -> KakaoPipelineModel | None:
        model = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == int(account_id))
        ).first()
        if model is None and create:
            model = KakaoPipelineModel(account_id=int(account_id))
            session.add(model)
            session.flush()
        return model

    @staticmethod
    def _account_credentials(account_id: int) -> tuple[AccountModel, str, str]:
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model or model.platform != "chatgpt":
                raise ValueError("ChatGPT 账号不存在")
            account = build_platform_account(session, model)
            extra = account.extra or {}
            token = _text(account.token or extra.get("access_token") or extra.get("accessToken"))
            session_cookie = _text(extra.get("session_cookie") or extra.get("session_token") or extra.get("sessionToken"))
            if not session_cookie and extra.get("cookies"):
                cookies = extra.get("cookies")
                session_cookie = cookies if isinstance(cookies, str) else json.dumps(cookies, ensure_ascii=False)
            if not token and not session_cookie:
                raise ValueError("账号缺少 session_cookie 和 access_token")
            session.expunge(model)
            return model, token, session_cookie

    @staticmethod
    def _serialize_pipeline(model: KakaoPipelineModel | None, *, detail: bool = False) -> dict:
        if model is None:
            return {
                "state": "idle",
                "supplier_status": "",
                "scanner_status": "",
                "plus_status": "",
                "final_result": "",
                "last_error_code": "",
                "last_error_message": "",
            }
        supplier = model.get_supplier_response()
        scanner = model.get_scanner_response()
        supplier_data = _data(supplier)
        scanner_data = _data(scanner)
        extraction = supplier_data.get("extraction") if isinstance(supplier_data.get("extraction"), dict) else {}
        subscription = scanner_data.get("subscription") if isinstance(scanner_data.get("subscription"), dict) else {}
        payload = {
            "id": int(model.id or 0),
            "account_id": int(model.account_id),
            "state": model.state,
            "payment_method": model.payment_method,
            "supplier_name": model.supplier_name,
            "supplier_status": model.supplier_status,
            "supplier_order_id": model.supplier_order_id,
            "supplier_stage": int(extraction.get("stage") or 0),
            "supplier_stage_total": int(extraction.get("stageTotal") or 0),
            "supplier_stage_name": _text(extraction.get("stageName")),
            "payment_url": model.payment_url,
            "scanner_driver": model.scanner_driver or "customer_api",
            "scanner_name": model.scanner_name,
            "scanner_status": model.scanner_status,
            "scanner_order_id": model.scanner_order_id,
            "scanner_subscription_status": _text(subscription.get("status")),
            "scan_url": model.scan_url,
            "scan_expires_at": model.scan_expires_at,
            "plus_status": model.plus_status,
            "final_result": model.final_result,
            "last_error_code": model.last_error_code,
            "last_error_message": model.last_error_message,
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None,
            "completed_at": model.completed_at.isoformat() if model.completed_at else None,
        }
        if detail:
            payload.update(
                {
                    "events": model.get_events(),
                    "supplier_response": sanitize_remote(supplier),
                    "scanner_response": sanitize_remote(scanner),
                }
            )
        return payload

    def list_accounts(self, *, search: str = "", page: int = 1, page_size: int = 20) -> dict:
        result = self.accounts.list_accounts(
            AccountQuery(platform="chatgpt", email=_text(search), page=max(1, page), page_size=min(max(1, page_size), 100))
        )
        account_ids = [int(item.get("id") or 0) for item in result.get("items", [])]
        pipelines: dict[int, KakaoPipelineModel] = {}
        if account_ids:
            with Session(engine) as session:
                rows = session.exec(
                    select(KakaoPipelineModel).where(KakaoPipelineModel.account_id.in_(account_ids))
                ).all()
                pipelines = {int(item.account_id): item for item in rows}
        items = []
        for account in result.get("items", []):
            account_id = int(account.get("id") or 0)
            view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
            identity = view.get("identity") if isinstance(view.get("identity"), dict) else {}
            subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
            status = view.get("status") if isinstance(view.get("status"), dict) else {}
            items.append(
                {
                    "id": account_id,
                    "email": _text(identity.get("email") or account.get("email")),
                    "plan": _text(subscription.get("plan") or account.get("plan_name") or "unknown"),
                    "plan_state": _text(subscription.get("state") or account.get("plan_state") or "unknown"),
                    "validity": _text(status.get("validity") or account.get("validity_status") or "unknown"),
                    "checked_at": status.get("checked_at"),
                    "pipeline": self._serialize_pipeline(pipelines.get(account_id)),
                }
            )
        return {**result, "items": items}

    def get_account_pipeline(self, account_id: int) -> dict:
        with Session(engine) as session:
            model = self._pipeline_for_account(session, account_id)
            if model is None:
                raise ValueError("账号还没有 Kakao 操作记录")
            return self._serialize_pipeline(model, detail=True)

    def start_extraction(self, account_id: int, supplier_setting_id: int | None = None, payment_method: str = "kakao_pay") -> dict:
        with _account_lock(account_id):
            _, access_token, _ = self._account_credentials(account_id)
            if not access_token:
                raise ValueError("提链供应商仍需要账号 access_token")
            setting, config = self._setting_payload("supplier")
            if supplier_setting_id and (not setting or int(setting.id or 0) != int(supplier_setting_id)):
                raise ValueError("选择的供应商配置不存在")
            method = _text(payment_method).lower() or "kakao_pay"
            if method not in {"kakao_pay", "naver_pay"}:
                raise ValueError("不支持的支付方式")
            client = CustomerApiClient(config["base_url"], config["cdk_key"])
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id, create=True)
                assert pipeline is not None
                if pipeline.state in {"scanner_submitting", "scanner_processing", "plus_checking"}:
                    raise ValueError("当前账号已有扫码任务，不能重新提链")
                pipeline.state = "supplier_submitting"
                pipeline.payment_method = method
                pipeline.supplier_setting_id = int(setting.id or 0) if setting else None
                pipeline.supplier_name = config["display_name"]
                pipeline.supplier_base_url = config["base_url"]
                pipeline.supplier_cdk_key = config["cdk_key"]
                pipeline.supplier_order_id = ""
                pipeline.supplier_customer_token = ""
                pipeline.supplier_poll_url = ""
                pipeline.supplier_status = "SUBMITTING"
                pipeline.set_supplier_response({})
                pipeline.payment_url = ""
                pipeline.scanner_setting_id = None
                pipeline.scanner_driver = "customer_api"
                pipeline.scanner_name = ""
                pipeline.scanner_base_url = ""
                pipeline.scanner_cdk_key = ""
                pipeline.scanner_order_id = ""
                pipeline.scanner_customer_token = ""
                pipeline.scanner_poll_url = ""
                pipeline.scanner_status = ""
                pipeline.set_scanner_response({})
                pipeline.scan_url = ""
                pipeline.scan_expires_at = ""
                pipeline.plus_status = ""
                pipeline.final_result = ""
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                pipeline.completed_at = None
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, f"已向 {config['display_name']} 提交提链请求")
                session.add(pipeline)
                session.commit()
            try:
                payload = client.create_extraction(access_token, payment_method=method)
                data = _data(payload)
                order = data.get("order") if isinstance(data.get("order"), dict) else {}
                order_id = _text(order.get("id"))
                customer_token = _text(data.get("customerToken"))
                poll_url = _text(data.get("pollUrl"))
                if not order_id or not customer_token or not poll_url:
                    raise ValueError("供应商创建订单响应缺少 order/customerToken/pollUrl")
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.supplier_order_id = order_id
                    pipeline.supplier_customer_token = customer_token
                    pipeline.supplier_poll_url = poll_url
                    pipeline.supplier_status = _text(order.get("status") or "PENDING").upper()
                    pipeline.state = "supplier_processing"
                    pipeline.set_supplier_response(payload)
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"供应商订单已创建: {order_id}")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, CustomerApiProblem) else "supplier_submit_failed"
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    _set_error(pipeline, "supplier_failed", code, str(exc))
                    pipeline.supplier_status = "FAILED"
                    session.add(pipeline)
                    session.commit()
                raise

    def poll_supplier(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or not pipeline.supplier_order_id:
                    raise ValueError("当前账号没有供应商提链订单")
                client = CustomerApiClient(pipeline.supplier_base_url, pipeline.supplier_cdk_key)
                poll_url = pipeline.supplier_poll_url
                customer_token = pipeline.supplier_customer_token
            try:
                payload = client.get_order(poll_url, customer_token)
            except Exception as exc:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.last_error_code = exc.code if isinstance(exc, CustomerApiProblem) else "supplier_poll_failed"
                    pipeline.last_error_message = str(exc)[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"刷新供应商状态失败: {exc}", level="warning")
                    session.add(pipeline)
                    session.commit()
                raise

            data = _data(payload)
            status = _text(data.get("status") or "PENDING").upper()
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                previous = pipeline.supplier_status
                pipeline.supplier_status = status
                pipeline.set_supplier_response(payload)
                pipeline.updated_at = _utcnow()
                if previous != status:
                    _append_event(pipeline, f"供应商状态: {status}")
                if status == "READY":
                    qualification = data.get("qualification") if isinstance(data.get("qualification"), dict) else {}
                    extraction = data.get("extraction") if isinstance(data.get("extraction"), dict) else {}
                    zero_verified = qualification.get("zeroVerified") is True
                    promo = qualification.get("postPromoAmountKrw")
                    tax = qualification.get("postTaxAmountKrw")
                    amounts_ok = (promo in (None, 0)) and (tax in (None, 0))
                    try:
                        payment_url = _valid_payment_url(_text(extraction.get("paymentUrl")))
                    except ValueError as exc:
                        _set_error(pipeline, "supplier_failed", "invalid_payment_url", str(exc))
                    else:
                        if not zero_verified or not amounts_ok:
                            _set_error(pipeline, "supplier_failed", "zero_amount_not_verified", "供应商未完成 0 KRW 双重校验")
                        else:
                            pipeline.payment_url = payment_url
                            pipeline.state = "link_ready"
                            pipeline.last_error_code = ""
                            pipeline.last_error_message = ""
                            _append_event(
                                pipeline,
                                "Kakao 长链提取成功，准备自动上传扫码"
                                if self._auto_upload_after_extract()
                                else "Kakao 长链提取成功，等待人工上传扫码",
                            )
                elif status in TERMINAL_REMOTE_FAILURES:
                    code, message = _problem_from_payload(data, "supplier_failed", "供应商提链失败")
                    _set_error(pipeline, "supplier_failed", code, message)
                else:
                    pipeline.state = "supplier_processing"
                session.add(pipeline)
                session.commit()
                result = self._serialize_pipeline(pipeline, detail=True)
                return self._maybe_auto_submit_scanner(account_id, result)

    def submit_scanner(
        self,
        account_id: int,
        scanner_setting_id: int | None = None,
        scanner_kind: str = "",
    ) -> dict:
        with _account_lock(account_id):
            kind = _text(scanner_kind) or self._default_scanner_kind()
            if kind not in {"scanner", "scanner_546789"}:
                raise ValueError("未知扫码供应商")
            setting, config = self._setting_payload(kind)
            if scanner_setting_id and (not setting or int(setting.id or 0) != int(scanner_setting_id)):
                raise ValueError("选择的扫码配置不存在")
            if not config["cdk_key"]:
                raise ValueError(f"{config['display_name']} 没有可用 CDK")
            access_token = ""
            session_cookie = ""
            if config["driver_type"] == "customer_api":
                _, access_token, session_cookie = self._account_credentials(account_id)
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or pipeline.state not in {"link_ready", "scanner_failed"} or not pipeline.payment_url:
                    raise ValueError("必须先成功提取 Kakao 长链")
                payment_url = pipeline.payment_url
                payment_method = pipeline.payment_method
                pipeline.state = "scanner_submitting"
                pipeline.scanner_setting_id = int(setting.id or 0) if setting else None
                pipeline.scanner_driver = config["driver_type"]
                pipeline.scanner_name = config["display_name"]
                pipeline.scanner_base_url = config["base_url"]
                pipeline.scanner_cdk_key = config["cdk_key"]
                pipeline.scanner_status = "SUBMITTING"
                pipeline.scanner_order_id = ""
                pipeline.scanner_customer_token = ""
                pipeline.scanner_poll_url = ""
                pipeline.set_scanner_response({})
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, f"已向 {config['display_name']} 上传扫码任务")
                session.add(pipeline)
                session.commit()
            try:
                if config["driver_type"] == "payment_submission":
                    client = WorkstationScannerClient(config["base_url"], config["cdk_key"])
                    payload = client.submit_payment(payment_url)
                    submissions = payload.get("submissions") if isinstance(payload.get("submissions"), list) else []
                    order = submissions[0] if submissions and isinstance(submissions[0], dict) else {}
                    order_id = _text(order.get("id"))
                    customer_token = ""
                    poll_url = ""
                    if not order_id:
                        raise ValueError("546789 扫码接口响应缺少 submission ID")
                    initial_status = _text(order.get("state") or "PENDING").upper()
                    scan_url = client.qr_url(order_id)
                else:
                    client = CustomerApiClient(config["base_url"], config["cdk_key"])
                    payload = client.create_scanner(
                        access_token,
                        payment_url,
                        payment_method=payment_method,
                        session_cookie=session_cookie,
                    )
                    data = _data(payload)
                    order = data.get("order") if isinstance(data.get("order"), dict) else {}
                    order_id = _text(order.get("id"))
                    customer_token = _text(data.get("customerToken"))
                    poll_url = _text(data.get("pollUrl"))
                    if not order_id or not customer_token or not poll_url:
                        raise ValueError("扫码接口响应缺少 order/customerToken/pollUrl")
                    initial_status = _text(order.get("status") or "PENDING").upper()
                    scan_url = ""
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.scanner_order_id = order_id
                    pipeline.scanner_customer_token = customer_token
                    pipeline.scanner_poll_url = poll_url
                    pipeline.scanner_status = initial_status
                    pipeline.state = "scanner_processing"
                    pipeline.set_scanner_response(payload)
                    pipeline.scan_url = scan_url
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"扫码订单已创建: {order_id}")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, CustomerApiProblem) else "scanner_submit_failed"
                depleted = _is_workstation_cdk_depleted(code, str(exc)) if kind == "scanner_546789" else _is_cdk_depleted(code, str(exc))
                if depleted:
                    self._remove_cdks(kind, [config["cdk_key"]])
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    message = str(exc)
                    if depleted:
                        message = f"{message}；已删除用完的 CDK，可使用池中下一条重新上传"
                    _set_error(pipeline, "scanner_failed", code, message)
                    pipeline.scanner_status = "FAILED"
                    session.add(pipeline)
                    session.commit()
                raise

    def poll_scanner(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or not pipeline.scanner_order_id:
                    raise ValueError("当前账号没有扫码订单")
                scanner_driver = pipeline.scanner_driver or "customer_api"
                poll_url = pipeline.scanner_poll_url
                customer_token = pipeline.scanner_customer_token
            try:
                if scanner_driver == "payment_submission":
                    payload = WorkstationScannerClient(pipeline.scanner_base_url).get_submission(pipeline.scanner_order_id)
                else:
                    payload = CustomerApiClient(pipeline.scanner_base_url, pipeline.scanner_cdk_key).get_order(poll_url, customer_token)
            except Exception as exc:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.last_error_code = exc.code if isinstance(exc, CustomerApiProblem) else "scanner_poll_failed"
                    pipeline.last_error_message = str(exc)[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"刷新扫码状态失败: {exc}", level="warning")
                    session.add(pipeline)
                    session.commit()
                raise

            data = _data(payload)
            if scanner_driver == "payment_submission":
                status = _text(data.get("state") or "PENDING").upper()
                outcome = "success" if status == "COMPLETED" else ("failed" if status in {"EXPIRED", "UNKNOWN", "FAILED"} else "processing")
            else:
                status = _text(data.get("status") or "PENDING").upper()
                outcome = _scanner_outcome(data)
            scan_url = _safe_scan_url(
                _find_scan_value(data.get("payment") or data, {"qr_url", "qrcode_url", "qr_code", "scan_url"})
            )
            expires_at = _find_scan_value(data, {"expires_at", "expire_at", "expired_at"})
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                previous = pipeline.scanner_status
                pipeline.scanner_status = status
                pipeline.set_scanner_response(payload)
                pipeline.scan_url = scan_url or pipeline.scan_url
                pipeline.scan_expires_at = expires_at or pipeline.scan_expires_at
                pipeline.updated_at = _utcnow()
                if previous != status:
                    _append_event(pipeline, f"扫码状态: {status}")
                if outcome == "success":
                    pipeline.state = "scanner_succeeded"
                    pipeline.last_error_code = ""
                    pipeline.last_error_message = ""
                    _append_event(pipeline, "扫码平台返回成功，等待本地 Plus 复检")
                elif outcome == "failed":
                    if scanner_driver == "payment_submission":
                        code = "payment_link_expired" if status == "EXPIRED" else "scanner_failed"
                        message = "支付链接已失效" if status == "EXPIRED" else f"546789 扫码任务失败: {status}"
                    else:
                        code, message = _problem_from_payload(data, "scanner_failed", f"扫码任务失败: {status}")
                    if scanner_driver == "customer_api" and _is_cdk_depleted(code, message):
                        self._remove_cdks("scanner", [pipeline.scanner_cdk_key])
                        message = f"{message}；已删除用完的 CDK，可使用池中下一条重新上传"
                    _set_error(pipeline, "scanner_failed", code, message)
                else:
                    pipeline.state = "scanner_processing"
                session.add(pipeline)
                session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    def check_plus(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id, create=True)
                assert pipeline is not None
                pipeline.state = "plus_checking"
                pipeline.plus_status = "checking"
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, "开始从本地账号复检 Plus")
                session.add(pipeline)
                session.commit()
            result = PlatformRuntime().execute_action(
                ActionExecutionCommand(
                    platform="chatgpt",
                    account_id=int(account_id),
                    action_id="query_state",
                    params={"platform_proxy_mode": "direct"},
                )
            )
            if not result.ok:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.plus_status = "error"
                    _set_error(pipeline, "plus_check_failed", "plus_check_failed", result.error or "Plus 检测失败")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)

            account = self.accounts.get_account(int(account_id)) or {}
            view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
            subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
            plan = _text(subscription.get("plan") or account.get("plan_name")).lower()
            plan_state = _text(subscription.get("state") or account.get("plan_state")).lower()
            is_plus = plan_state == "subscribed" or any(token in plan for token in ("plus", "pro", "team", "business", "enterprise"))
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                pipeline.plus_status = "plus" if is_plus else (plan or plan_state or "free")
                pipeline.final_result = "plus" if is_plus else "not_plus"
                pipeline.state = "completed" if is_plus else "plus_pending"
                pipeline.completed_at = _utcnow() if is_plus else None
                pipeline.updated_at = _utcnow()
                _append_event(
                    pipeline,
                    "账号已确认升级为 Plus" if is_plus else f"本地复检尚未发现 Plus（{plan or plan_state or 'unknown'}）",
                    level="info" if is_plus else "warning",
                )
                session.add(pipeline)
                session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    def reset(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None:
                    return self._serialize_pipeline(None)
                if pipeline.state in ACTIVE_STATES:
                    raise ValueError("任务进行中，不能重置")
                session.delete(pipeline)
                session.commit()
                return self._serialize_pipeline(None)
