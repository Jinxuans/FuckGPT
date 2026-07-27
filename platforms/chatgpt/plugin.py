"""ChatGPT / Codex CLI 平台插件"""
import secrets
import time

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register
from core.proxy_pool import proxy_pool


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是", "开启", "启用"}


def _mask_phone_number(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}****{text[-4:]}"


def _int_setting(value, default: int, *, minimum: int = 1) -> int:
    try:
        result = int(float(value if value not in (None, "") else default))
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _float_setting(value, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


class _CodexSmsPhoneCallback:
    def __init__(
        self,
        provider,
        *,
        service: str = "",
        country: str = "",
        log_fn=None,
        buy_max_attempts: int = 20,
        buy_retry_interval: float = 3,
    ):
        self.provider = provider
        self.service = service
        self.country = country
        self.log_fn = log_fn if callable(log_fn) else (lambda _message: None)
        self.buy_max_attempts = max(int(buy_max_attempts or 1), 1)
        self.buy_retry_interval = max(float(buy_retry_interval or 0), 0)
        self.activation = None
        self.completed = False
        self.sent = False

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            pass

    def __call__(self) -> str:
        if self.activation is None:
            self.activation = self._buy_number_with_retry()
            return self.activation.phone_number
        timeout = int(float(getattr(self.provider, "request_timeout", 15) or 15) * 8)
        timeout = max(timeout, 120)
        return self.provider.wait_for_code(
            self.activation.activation_id,
            timeout=timeout,
            poll_interval=getattr(self.provider, "poll_interval", 3),
        )

    @staticmethod
    def _is_retryable_buy_error(exc: Exception) -> bool:
        message = str(exc or "")
        lowered = message.lower()
        return (
            "NO_NUMBERS" in message
            or "暂无号码" in message
            or "无号码" in message
            or "no numbers" in lowered
            or "no_number" in lowered
            or "no number" in lowered
        )

    def _buy_number_with_retry(self):
        last_error: Exception | None = None
        for attempt in range(1, self.buy_max_attempts + 1):
            try:
                activation = self.provider.get_number(service=self.service, country=self.country)
                self._log(f"Codex OAuth 接码买号成功: activation={activation.activation_id}")
                return activation
            except Exception as exc:
                last_error = exc
                if not self._is_retryable_buy_error(exc):
                    raise
                if attempt >= self.buy_max_attempts:
                    break
                self._log(
                    f"Codex OAuth 接码暂无号码，{self.buy_retry_interval:g}s 后重试 "
                    f"({attempt}/{self.buy_max_attempts}): {exc}"
                )
                if self.buy_retry_interval > 0:
                    time.sleep(self.buy_retry_interval)
        raise RuntimeError(f"Codex OAuth 接码买号重试耗尽: {last_error}") from last_error

    def mark_send_succeeded(self) -> None:
        if self.activation is None or self.sent:
            return
        self.provider.mark_sms_sent(self.activation.activation_id)
        self.sent = True

    def mark_send_failed(self, reason: str = "") -> None:
        self._log(f"Codex OAuth 手机号提交失败: {reason}")
        self.cleanup()

    def mark_code_failed(self, reason: str = "") -> None:
        self._log(f"Codex OAuth 短信验证码失败: {reason}")
        if self.activation is not None:
            try:
                self.provider.request_retry(self.activation.activation_id)
            except Exception:
                pass

    def report_success(self) -> None:
        if self.activation is None:
            return
        try:
            self.provider.finish(self.activation.activation_id)
        finally:
            self.completed = True

    def cleanup(self) -> None:
        if self.activation is None or self.completed:
            return
        try:
            self.provider.cancel(self.activation.activation_id)
        except Exception:
            pass

    def reset(self) -> None:
        self.activation = None
        self.sent = False
        self.completed = False


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "switch_desktop",   # Switch to Codex desktop
        "codex_oauth_authorize",  # Create Codex OAuth credentials through browser login
        "upload_cpa",       # Upload to CPA system
        "upload_tm",        # Upload to Team Manager
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        try:
            from platforms.chatgpt.subscription import fetch_subscription_status_details
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            configured_proxy = self.config.proxy if self.config else None
            disable_proxy_pool = _truthy((self.config.extra or {}).get("disable_proxy_pool")) if self.config else False
            proxy_candidates: list[tuple[str | None, bool]] = []
            if configured_proxy:
                proxy_candidates.append((configured_proxy, False))
            elif not disable_proxy_pool:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, True))
            proxy_candidates.append((None, False))

            for proxy, should_report in proxy_candidates:
                try:
                    details = fetch_subscription_status_details(a, proxy=proxy)
                    if should_report and proxy:
                        proxy_pool.report_success(proxy)
                    status = details.get("status")
                    # 把订阅状态同步映射成前端能用的 plan_state / chips
                    # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
                    if status == "plus":
                        plan_state = "subscribed"
                        chips = ["Plus"]
                    elif status == "team":
                        plan_state = "subscribed"
                        chips = ["Team"]
                    elif status == "free":
                        plan_state = "free"
                        chips = ["Free"]
                    elif status in ("expired", "invalid", "banned"):
                        plan_state = "expired"
                        chips = []
                    else:
                        plan_state = "unknown"
                        chips = []
                    overview = {
                        "plan": status,
                        "plan_name": status,
                        "plan_state": plan_state,
                        "chips": chips,
                        "check_source": details.get("source"),
                    }
                    me = details.get("me")
                    if isinstance(me, dict):
                        phone_number = _mask_phone_number(me.get("phone_number"))
                        overview["phone_bound"] = bool(phone_number)
                        if phone_number:
                            overview["phone_number_masked"] = phone_number
                            overview["chips"].append("已绑手机")
                        if me.get("email"):
                            overview["remote_email"] = str(me.get("email") or "")
                    if isinstance(details.get("usage"), dict):
                        overview["chatgpt_usage"] = details["usage"]
                    self._last_check_overview = overview
                    return status not in ("expired", "invalid", "banned", None)
                except Exception:
                    if should_report and proxy:
                        proxy_pool.report_fail(proxy)
                    continue
        except Exception:
            return False
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
    ) -> RegistrationResult:
        extra = {
            "account_id": result.get("account_id", ""),
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "id_token": result.get("id_token", ""),
            "session_token": result.get("session_token", ""),
            "workspace_id": result.get("workspace_id", ""),
            "cookies": result.get("cookies", ""),
            "profile": result.get("profile", {}),
            "expires_at": result.get("expires_at", ""),
        }
        for key in (
            "codex_auth_path",
            "codex_email",
            "codex_account_id",
            "codex_plan_type",
            "codex_access_token",
            "codex_refresh_token",
            "codex_id_token",
            "codex_expires_at",
            "codex_last_refresh",
        ):
            if result.get(key) not in (None, ""):
                extra[key] = result[key]
        if isinstance(result.get("post_codex_oauth"), dict):
            extra["post_codex_oauth"] = result["post_codex_oauth"]
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra=extra,
        )

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister
            post_codex_oauth = _truthy(ctx.extra.get("auto_codex_oauth_after_register"))
            codex_phone_callback = self._build_codex_phone_callback(ctx.proxy) if post_codex_oauth else None
            keep_browser_open = _truthy(ctx.extra.get("codex_oauth_keep_browser_open"))

            return ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                post_codex_oauth=post_codex_oauth,
                codex_phone_callback=codex_phone_callback,
                codex_oauth_timeout=int(ctx.extra.get("codex_oauth_timeout") or 300),
                keep_browser_open=keep_browser_open,
                log_fn=ctx.log,
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
            )

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=600),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_protocol_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

            return ChatGPTProtocolRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
            )

        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                password=ctx.password or "",
            ),
            worker_builder=_build_protocol_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                # ChatGPT's current OTP emails use subjects such as
                # "Your temporary ChatGPT login code" and do not always
                # contain the literal "OpenAI".  The mailbox provider already
                # filters stale messages and extracts a six-digit code, so a
                # sender/brand keyword here only causes valid messages to be
                # discarded.
                keyword="",
                wait_message="等待 Outlook 验证码...",
                timeout=180,
            ),
        )

    def get_platform_actions(self) -> list:
        proxy_params = [
            {"key": "platform_proxy_mode", "label": "ChatGPT/Codex 代理", "type": "select", "options": ["direct", "manual", "proxy_service"]},
            {"key": "platform_proxy_value", "label": "手动代理 URL", "type": "text"},
        ]
        return [
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "codex_oauth_authorize", "label": "Codex OAuth 授权",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select", "options": ["headless", "headed"]},
                 {"key": "keep_browser_open", "label": "完成后保留浏览器窗口", "type": "select", "options": ["false", "true"]},
                 *proxy_params,
             ]},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": proxy_params},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        aliases = {
            "switch_account": "switch_desktop",
            "get_account_state": "query_state",
        }
        resolved_action = aliases.get(action_id, action_id)
        if resolved_action in self.capabilities:
            return self._handle_capability(resolved_action, account, params or {})
        return self._execute_platform_action(resolved_action, account, params or {})

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        mailbox_proxy = str((self.config.extra or {}).get("mailbox_proxy") or "").strip() if self.config else ""
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or ""
        a.account_id = account.user_id or ""

        if action_id == "codex_oauth_authorize":
            from platforms.chatgpt.codex_oauth import perform_codex_oauth_login

            if not account.email:
                return {"ok": False, "error": "Codex OAuth 授权需要账号邮箱"}
            if not account.password:
                return {"ok": False, "error": "Codex OAuth 授权需要账号密码"}
            browser_mode = str(params.get("browser_mode") or "").strip().lower()
            if not browser_mode:
                browser_mode = str((self.config.extra or {}).get("codex_oauth_browser_mode") or "headed").strip().lower()
            headless = browser_mode in {"headless", "true", "1", "yes", "后台", "后台浏览器"}
            keep_browser_open = _truthy(params.get("keep_browser_open") or (self.config.extra or {}).get("codex_oauth_keep_browser_open"))
            otp_callback = None
            try:
                from core.mailbox_store import MailboxStore

                mailbox, mailbox_account, _mailbox_context = MailboxStore().resolve_mailbox_for_account(
                    platform="chatgpt",
                    account_id=int(getattr(account, "id", 0) or 0) if getattr(account, "id", None) else 0,
                    proxy=mailbox_proxy or None,
                    extra=self.config.extra if self.config else {},
                )
                before_ids = mailbox.get_current_ids(mailbox_account)

                def _otp_callback():
                    self.log("等待 Codex OAuth 邮箱验证码...")
                    code = mailbox.wait_for_code(
                        mailbox_account,
                        keyword="",
                        timeout=180,
                        before_ids=before_ids,
                    )
                    if code:
                        self.log(f"Codex OAuth 验证码: {code}")
                    return code

                otp_callback = _otp_callback
            except Exception as exc:
                self.log(f"未绑定可用验证邮箱，Codex OAuth 如触发验证码将失败: {exc}")
            phone_callback = self._build_codex_phone_callback(proxy)
            result = perform_codex_oauth_login(
                email=account.email,
                password=account.password,
                proxy=proxy,
                headless=headless,
                log_fn=self.log,
                otp_callback=otp_callback,
                phone_callback=phone_callback,
                keep_browser_open=keep_browser_open,
            )
            return {"ok": True, "data": result}

        if action_id == "switch_desktop":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "Switch to Codex desktop requires session_token"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "Switch failed")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                cookies=a.cookies,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "Codex credentials written")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": ".".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"Unknown action: {action_id}")

    def _build_codex_phone_callback(self, proxy: str | None):
        try:
            from core.smsbower_sms import SMSBowerClient
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            repo = ProviderSettingsRepository()
            provider_key = repo.get_default_provider_key("sms")
            if not provider_key:
                self.log("Codex OAuth 未配置默认接码服务，add_phone 将尝试跳过")
                return None
            if provider_key != "smsbower":
                self.log(f"Codex OAuth 暂不支持接码 provider: {provider_key}")
                return None
            extra = repo.resolve_runtime_settings("sms", provider_key, self.config.extra if self.config else {})
            if proxy and not extra.get("sms_proxy"):
                extra["proxy"] = proxy
            client = SMSBowerClient.from_config(extra)
            if not client.api_key or not client.default_service or not client.default_country:
                self.log("Codex OAuth 默认接码服务未配置 API Key/service/country，add_phone 将尝试跳过")
                return None
            return _CodexSmsPhoneCallback(
                client,
                service=client.default_service,
                country=client.default_country,
                log_fn=self.log,
                buy_max_attempts=_int_setting(extra.get("smsbower_buy_max_attempts"), 20),
                buy_retry_interval=_float_setting(extra.get("smsbower_buy_retry_interval"), 3),
            )
        except Exception as exc:
            self.log(f"Codex OAuth 初始化接码服务失败，add_phone 将尝试跳过: {exc}")
            return None

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

        data = fetch_chatgpt_account_state(
            access_token=a.access_token,
            session_token=a.session_token,
            cookies=a.cookies,
            proxy=proxy,
        )
        data["local_app_account"] = read_current_codex_account()
        data["desktop_app_state"] = get_codex_desktop_state()
        return {"ok": True, "data": data}

