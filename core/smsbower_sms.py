"""SMSBower 短信接码服务封装。

文档：https://smsbower.app/cn/api?page=client
API 兼容 sms-activate 风格的 ``handler_api.php``。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from core.base_sms import BaseSmsProvider, SmsActivation, SmsStatus


DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
DEFAULT_REF = "531498"

SMSBOWER_STATUS_SMS_SENT = 1
SMSBOWER_STATUS_RETRY = 3
SMSBOWER_STATUS_FINISH = 6
SMSBOWER_STATUS_CANCEL = 8

_ERROR_CODES = {
    "BAD_KEY": "API Key 无效",
    "BAD_ACTION": "接口 action 无效",
    "NO_ACTIVATION": "激活单不存在",
    "NO_BALANCE": "余额不足",
    "NO_NUMBERS": "当前服务/国家暂无号码",
    "BAD_SERVICE": "服务代码无效",
    "BAD_STATUS": "状态码无效",
    "BANNED": "账号受限",
}


class SMSBowerError(RuntimeError):
    pass


def _string(value: object) -> str:
    return str(value or "").strip()


def _float_value(value: object, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _int_value(value: object, default: int, *, minimum: int = 0) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


class SMSBowerClient(BaseSmsProvider):
    """SMSBower API 客户端。

    核心能力：

    * ``get_number`` / ``get_number_v2``：购买手机号；
    * ``get_status``：取短信验证码；
    * ``set_status`` 及便捷方法：取消、完成、重试等状态流转。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_service: str = "",
        default_country: str | int = "",
        default_max_price: str | float = "",
        default_min_price: str | float = "",
        request_timeout: float | str = 15,
        poll_interval: float | str = 3,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.api_key = _string(api_key)
        self.base_url = _string(base_url) or DEFAULT_BASE_URL
        self.default_service = _string(default_service)
        self.default_country = _string(default_country)
        self.default_max_price = _string(default_max_price)
        self.default_min_price = _string(default_min_price)
        self.request_timeout = _float_value(request_timeout, 15, minimum=0.5)
        self.poll_interval = _float_value(poll_interval, 3, minimum=0)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._log_fn = log_fn if callable(log_fn) else None

    @classmethod
    def from_config(cls, config: dict) -> "SMSBowerClient":
        return cls(
            api_key=config.get("smsbower_api_key", ""),
            base_url=config.get("smsbower_base_url", DEFAULT_BASE_URL),
            default_service=config.get("smsbower_default_service", ""),
            default_country=config.get("smsbower_default_country", ""),
            default_max_price=config.get("smsbower_max_price", ""),
            default_min_price=config.get("smsbower_min_price", ""),
            request_timeout=config.get("smsbower_request_timeout", 15),
            poll_interval=config.get("smsbower_poll_interval", 3),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def set_logger(self, log_fn) -> None:
        self._log_fn = log_fn if callable(log_fn) else None

    def _log(self, message: str) -> None:
        if not self._log_fn:
            return
        try:
            self._log_fn(self._redact(message))
        except Exception:
            pass

    def _redact(self, text: object) -> str:
        value = str(text or "")
        if self.api_key:
            value = value.replace(self.api_key, "***")
        value = re.sub(r"(api_key=)[^&\s]+", r"\1***", value, flags=re.IGNORECASE)
        return value

    def _raise_api_error(self, raw: str, *, action: str) -> None:
        code = raw.split(":", 1)[0].strip()
        message = _ERROR_CODES.get(code, raw or "unknown")
        raise SMSBowerError(f"SMSBower {action}失败: {message} ({code})")

    def _request(self, action: str, params: dict[str, Any] | None = None) -> str:
        if not self.api_key:
            raise SMSBowerError("SMSBower API Key 未配置")
        query = {"api_key": self.api_key, "action": action}
        for key, value in dict(params or {}).items():
            if value not in (None, ""):
                query[key] = value
        try:
            response = self.session.get(
                self.base_url,
                params=query,
                headers={"Accept": "application/json, text/plain, */*", "User-Agent": "aBaiAutoplus/smsbower"},
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            raw = str(response.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise SMSBowerError(self._redact(f"SMSBower 请求失败: {exc}")) from exc
        if not raw:
            raise SMSBowerError(f"SMSBower {action} 返回空响应")
        if raw.split(":", 1)[0].strip() in _ERROR_CODES:
            self._raise_api_error(raw, action=action)
        return raw

    @staticmethod
    def _activation_from_text(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        parts = raw.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise SMSBowerError(f"SMSBower 买号响应格式无效: {raw}")
        return SmsActivation(
            activation_id=parts[1].strip(),
            phone_number=parts[2].strip(),
            provider="smsbower",
            service=service,
            country=country,
            raw=raw,
        )

    @staticmethod
    def _activation_from_json(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SMSBowerError(f"SMSBower JSON 买号响应格式无效: {raw}") from exc
        if not isinstance(payload, dict):
            raise SMSBowerError("SMSBower JSON 买号响应不是对象")
        activation_id = _string(payload.get("activationId") or payload.get("id"))
        phone_number = _string(payload.get("phoneNumber") or payload.get("number"))
        if not activation_id or not phone_number:
            raise SMSBowerError(f"SMSBower JSON 买号响应缺少 activationId/phoneNumber: {raw}")
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            provider="smsbower",
            service=service,
            country=_string(payload.get("countryCode")) or country,
            raw=raw,
            metadata=payload,
        )

    @staticmethod
    def _status_from_text(raw: str) -> SmsStatus:
        if raw == "STATUS_WAIT_CODE":
            return SmsStatus(status="waiting", raw=raw)
        if raw == "STATUS_CANCEL":
            return SmsStatus(status="cancelled", raw=raw)
        if raw.startswith("STATUS_WAIT_RETRY:"):
            return SmsStatus(status="waiting_retry", last_code=raw.split(":", 1)[1].strip(), raw=raw)
        if raw.startswith("STATUS_OK:"):
            return SmsStatus(status="ok", code=raw.split(":", 1)[1].strip(), raw=raw)
        raise SMSBowerError(f"SMSBower 查码响应格式无效: {raw}")

    @staticmethod
    def _set_status_result(raw: str) -> str:
        ok = {"ACCESS_READY", "ACCESS_RETRY_GET", "ACCESS_ACTIVATION", "ACCESS_CANCEL"}
        if raw in ok:
            return raw
        if raw.split(":", 1)[0] in _ERROR_CODES:
            raise SMSBowerError(f"SMSBower 改状态失败: {_ERROR_CODES.get(raw, raw)} ({raw})")
        raise SMSBowerError(f"SMSBower 改状态响应格式无效: {raw}")

    def get_balance(self) -> float:
        raw = self._request("getBalance")
        value = raw.split(":", 1)[1] if raw.startswith("ACCESS_BALANCE:") else raw
        try:
            return float(value)
        except ValueError as exc:
            raise SMSBowerError(f"SMSBower 余额响应格式无效: {raw}") from exc

    def get_number(
        self,
        service: str = "",
        country: str = "",
        **options,
    ) -> SmsActivation:
        service = _string(service) or self.default_service
        country = _string(country) or self.default_country
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        if not country:
            raise SMSBowerError("SMSBower country 未配置")

        params = self._number_params(service, country, options)
        self._log(f"SMSBower 开始购买手机号：service={service}, country={country}")
        raw = self._request("getNumber", params)
        activation = self._activation_from_text(raw, service=service, country=country)
        self._log(
            f"SMSBower 买号成功：activationId={activation.activation_id}, phone={activation.phone_number}"
        )
        return activation

    def get_number_v2(
        self,
        service: str = "",
        country: str = "",
        **options,
    ) -> SmsActivation:
        service = _string(service) or self.default_service
        country = _string(country) or self.default_country
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        if not country:
            raise SMSBowerError("SMSBower country 未配置")

        params = self._number_params(service, country, options)
        self._log(f"SMSBower 开始购买手机号 V2：service={service}, country={country}")
        raw = self._request("getNumberV2", params)
        if raw.startswith("{"):
            activation = self._activation_from_json(raw, service=service, country=country)
        else:
            activation = self._activation_from_text(raw, service=service, country=country)
        self._log(
            f"SMSBower 买号成功：activationId={activation.activation_id}, phone={activation.phone_number}"
        )
        return activation

    def _number_params(self, service: str, country: str, options: dict[str, Any]) -> dict[str, Any]:
        option_map = {
            "max_price": "maxPrice",
            "min_price": "minPrice",
            "provider_ids": "providerIds",
            "except_provider_ids": "exceptProviderIds",
            "phone_exception": "phoneException",
            "user_id": "userID",
        }
        params: dict[str, Any] = {"service": service, "country": country, "ref": DEFAULT_REF}
        if self.default_max_price:
            params["maxPrice"] = self.default_max_price
        if self.default_min_price:
            params["minPrice"] = self.default_min_price
        for source_key, target_key in option_map.items():
            value = options.get(source_key, options.get(target_key))
            if value not in (None, ""):
                params[target_key] = value
        return params

    def get_status(self, activation_id: str) -> SmsStatus:
        activation_id = _string(activation_id)
        if not activation_id:
            raise SMSBowerError("SMSBower activation_id 不能为空")
        raw = self._request("getStatus", {"id": activation_id})
        status = self._status_from_text(raw)
        if status.code:
            self._log(f"SMSBower 收到验证码：activationId={activation_id}, code={status.code}")
        else:
            self._log(f"SMSBower 查码状态：activationId={activation_id}, status={status.raw}")
        return status

    def set_status(self, activation_id: str, status: int | str) -> str:
        activation_id = _string(activation_id)
        status_value = _int_value(status, -1, minimum=-1)
        if not activation_id:
            raise SMSBowerError("SMSBower activation_id 不能为空")
        if status_value < 0:
            raise SMSBowerError("SMSBower status 不能为空")
        raw = self._request("setStatus", {"id": activation_id, "status": status_value})
        result = self._set_status_result(raw)
        self._log(f"SMSBower 改状态成功：activationId={activation_id}, status={status_value}, result={result}")
        return result

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int | float = 120,
        poll_interval: int | float | None = None,
    ) -> str:
        interval = self.poll_interval if poll_interval is None else poll_interval
        self._log(f"SMSBower 开始等待短信验证码：activationId={activation_id}")
        code = super().wait_for_code(activation_id, timeout=timeout, poll_interval=interval)
        self._log(f"SMSBower 等待验证码完成：activationId={activation_id}, code={code}")
        return code

    def get_prices(self, service: str = "", country: str = "") -> str:
        params = {}
        if service:
            params["service"] = service
        if country:
            params["country"] = country
        return self._request("getPrices", params)

    def get_services_list(self) -> str:
        return self._request("getServicesList")

    def get_countries(self) -> str:
        return self._request("getCountries")

    def get_top_countries_by_service(self, service: str = "") -> str:
        service = _string(service) or self.default_service
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        return self._request("getTopCountriesByService", {"service": service})
