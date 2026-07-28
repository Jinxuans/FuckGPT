from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import ProviderDefinitionModel, ProviderSettingModel, engine

logger = logging.getLogger(__name__)

SUPPORTED_MAILBOX_PROVIDER_KEYS = ("local_ms_pool", "api_mailbox", "hotmail007")
SUPPORTED_SMS_PROVIDER_KEYS = ("smsbower", "herosms", "smspool", "fivesim", "smstome")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_BUILTIN_DEFINITIONS: list[dict] = [
    # ── mailbox ──────────────────────────────────────────────────────
    {
        "provider_type": "mailbox",
        "provider_key": "local_ms_pool",
        "label": "本地微软邮箱池",
        "description": "导入 Hotmail/Outlook 邮箱池，支持 GuJumpgate 四列格式，优先使用 Client Id + 刷新令牌通过 Microsoft Graph 收验证码",
        "driver_type": "local_ms_pool",
        "default_auth_mode": "pool",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "pool", "label": "账号池"}],
        "fields": [
            {
                "key": "local_ms_pool_file",
                "label": "账号池文件路径",
                "placeholder": "/Users/you/ms-mail-pool.txt",
                "category": "connection",
                "hint": "可选；每行一条 Hotmail 四列格式：账号----密码----ID----Token。也兼容旧通用格式。配置文件路径后无需把账号明文粘贴到设置页。",
            },
            {
                "key": "local_ms_pool_text",
                "label": "账号池文本",
                "type": "textarea",
                "category": "auth",
                "hint": "可选；直接粘贴 Hotmail 四列格式：账号----密码----ID----Token。也兼容旧通用格式。支持逗号、中文逗号、TAB、---- 分隔。",
            },
            {
                "key": "local_ms_graph_scope",
                "label": "Graph Scope",
                "placeholder": "https://graph.microsoft.com/Mail.Read offline_access",
                "category": "connection",
            },
            {
                "key": "local_ms_pool_state_file",
                "label": "占用状态文件",
                "placeholder": "默认 data/.local_ms_mailbox_pool_state.json",
                "category": "connection",
                "hint": "用于避免同一个邮箱被重复分配；清空该文件可重置账号池占用状态。",
            },
            {
                "key": "local_ms_pool_allow_reuse",
                "label": "允许重复使用邮箱",
                "type": "toggle",
                "category": "connection",
                "hint": "测试时可开启；批量注册建议关闭。",
            },
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "api_mailbox",
        "label": "API 邮箱",
        "description": "使用固定邮箱及其专属 API 地址轮询获取验证码，支持通用 API URL 和 flysms 取件链接",
        "driver_type": "api_mailbox",
        "default_auth_mode": "pool",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "pool", "label": "邮箱 API 池"}],
        "fields": [
            {
                "key": "api_mailbox_pool_text",
                "label": "邮箱 API 池",
                "type": "textarea",
                "secret": True,
                "category": "auth",
                "placeholder": "user@example.com----https://mail.example.com/api/code?email=...&token=...\nuser@icloud.com---tok_xxx---https://flysms.top/icloud/pickup#email=...&key=...",
                "hint": "每行一组。通用格式：邮箱----完整 API URL；flysms 格式：邮箱---token---取件 URL。其他商家的 token 三段格式不会自动套用 flysms 接口。",
            },
            {
                "key": "api_mailbox_poll_interval",
                "label": "轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
            },
            {
                "key": "api_mailbox_request_timeout",
                "label": "单次请求超时秒",
                "placeholder": "15",
                "default_value": "15",
                "category": "connection",
            },
            {
                "key": "api_mailbox_state_file",
                "label": "占用状态文件",
                "placeholder": "默认 data/.api_mailbox_pool_state.json",
                "category": "connection",
                "hint": "用于避免同一个邮箱被重复分配；删除该文件可重置占用状态。",
            },
            {
                "key": "api_mailbox_allow_reuse",
                "label": "允许重复使用邮箱",
                "type": "toggle",
                "category": "connection",
                "hint": "测试时可开启；批量注册建议关闭。",
            },
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "hotmail007",
        "label": "Hotmail007",
        "description": "通过 Hotmail007 API 循环购买邮箱账号，并用最新邮件接口获取验证码；购买循环无固定延迟，适合抢短暂库存",
        "driver_type": "hotmail007",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {
                "key": "hotmail007_client_key",
                "label": "Client Key",
                "secret": True,
                "category": "auth",
                "hint": "Hotmail007 用户中心的 API Key；请求会作为 clientKey query 参数传入。",
            },
            {
                "key": "hotmail007_product_id",
                "label": "商品 ID",
                "placeholder": "例如 11,12",
                "category": "connection",
                "hint": "Hotmail007 商品卡片上的 productId。支持用英文/中文逗号或空白填写多个，每次购买尝试会随机选择一个。",
            },
            {
                "key": "hotmail007_base_url",
                "label": "API 基址",
                "placeholder": "https://hotmail007.com/api",
                "default_value": "https://hotmail007.com/api",
                "category": "connection",
            },
            {
                "key": "hotmail007_buy_quantity",
                "label": "每次购买数量",
                "placeholder": "1",
                "default_value": "1",
                "category": "connection",
                "hint": "核心注册流程通常填 1；大于 1 时多买到的账号会在当前进程内缓存给后续注册使用。",
            },
            {
                "key": "hotmail007_buy_concurrency",
                "label": "批量买号并发",
                "placeholder": "跟随注册并发",
                "default_value": "",
                "category": "connection",
                "hint": "批量注册时生效；留空时等于注册并发，且不会超过注册并发。",
            },
            {
                "key": "hotmail007_prefetch_queue_max",
                "label": "预取队列上限",
                "placeholder": "注册并发 x 2",
                "default_value": "",
                "category": "connection",
                "hint": "批量注册时最多预取并缓存的邮箱数；实际总购买量不会超过本批注册数量。",
            },
            {
                "key": "hotmail007_buy_max_attempts",
                "label": "购买最大尝试次数",
                "placeholder": "200",
                "default_value": "200",
                "category": "connection",
                "hint": "购买循环没有固定延迟；库存不足、瞬时失败会立即重试，直到成功、达到次数或超时。",
            },
            {
                "key": "hotmail007_buy_timeout_seconds",
                "label": "购买超时秒",
                "placeholder": "30",
                "default_value": "30",
                "category": "connection",
            },
            {
                "key": "hotmail007_request_timeout",
                "label": "单次请求超时秒",
                "placeholder": "8",
                "default_value": "8",
                "category": "connection",
            },
            {
                "key": "hotmail007_folders",
                "label": "取件文件夹",
                "placeholder": "inbox,junkemail",
                "default_value": "inbox,junkemail",
                "category": "connection",
                "hint": "仅支持 inbox 和 junkemail。会依次查询这些文件夹的最新邮件。",
            },
            {
                "key": "hotmail007_include_junk",
                "label": "同时查询垃圾邮件",
                "type": "toggle",
                "default_value": "true",
                "category": "connection",
            },
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "yescaptcha_api",
        "label": "YesCaptcha",
        "description": "YesCaptcha 云端验证码识别服务，支持 Turnstile 等类型",
        "driver_type": "yescaptcha_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "yescaptcha_key", "label": "Client Key", "secret": True},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "twocaptcha_api",
        "label": "2Captcha",
        "description": "2Captcha 云端验证码识别服务，支持 Turnstile 等类型",
        "driver_type": "twocaptcha_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "twocaptcha_key", "label": "API Key", "secret": True},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "local_solver",
        "label": "本地验证码求解器",
        "description": "调用本地 api_solver 服务（Camoufox/patchright）解 Turnstile 验证码",
        "driver_type": "local_solver",
        "default_auth_mode": "",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "solver_url", "label": "Solver 地址", "placeholder": "http://localhost:8889"},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "manual",
        "label": "人工打码",
        "description": "阻塞等待用户手动输入验证码，适用于调试场景",
        "driver_type": "manual",
        "default_auth_mode": "",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [],
    },
    # ── sms ──────────────────────────────────────────────────────────
    {
        "provider_type": "sms",
        "provider_key": "smsbower",
        "label": "SMSBower",
        "description": "通过 SMSBower API 购买手机号、查询短信验证码并更新激活状态，用于后续自动绑定手机号",
        "driver_type": "smsbower",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {
                "key": "smsbower_api_key",
                "label": "API Key",
                "secret": True,
                "category": "auth",
                "hint": "SMSBower 用户中心的 API Key；请求会作为 api_key query 参数传入，日志与异常会脱敏。",
            },
            {
                "key": "smsbower_default_service",
                "label": "默认接码服务",
                "placeholder": "填写 API Key 后加载服务",
                "default_value": "dr",
                "category": "connection",
                "type": "async-select",
                "asyncUrl": "/provider-settings/options",
                "hint": "默认选择 OpenAI (ChatGPT)，也可以从 SMSBower 实时服务目录中搜索并切换。",
            },
            {
                "key": "smsbower_default_country",
                "label": "默认国家/地区",
                "placeholder": "填写 API Key 后加载国家/地区",
                "category": "connection",
                "type": "async-select",
                "asyncUrl": "/provider-settings/options",
                "hint": "选项值使用 SMSBower 的 activate.org 国家代码，列表显示国家/地区名称与对应代码。",
            },
            {
                "key": "smsbower_number_api",
                "label": "买号接口",
                "type": "select",
                "options": [
                    {"value": "getNumber", "label": "getNumber（兼容模式）"},
                    {"value": "getNumberV2", "label": "getNumberV2（返回价格与运营商信息）"},
                ],
                "default_value": "getNumber",
                "category": "connection",
                "hint": "V2 会保留 activationCost、activationOperator 等扩展信息；遇到兼容问题时使用 getNumber。",
            },
            {
                "key": "smsbower_max_price",
                "label": "最高价格",
                "placeholder": "例如 0.5",
                "category": "connection",
                "hint": "可选；购买手机号时作为 maxPrice 参数传入，用于限制最高可接受价格。",
            },
            {
                "key": "smsbower_min_price",
                "label": "最低价格",
                "placeholder": "例如 0.1",
                "category": "connection",
                "hint": "可选；购买手机号时作为 minPrice 参数传入，用于限制最低可接受价格。",
            },
            {
                "key": "smsbower_provider_ids",
                "label": "指定供应商 ID",
                "placeholder": "例如 1,2,3",
                "category": "advanced",
                "hint": "可选；仅从这些 SMSBower 供应商购买，多个 ID 用英文逗号分隔。",
            },
            {
                "key": "smsbower_except_provider_ids",
                "label": "排除供应商 ID",
                "placeholder": "例如 4,5",
                "category": "advanced",
                "hint": "可选；购买时排除这些 SMSBower 供应商，多个 ID 用英文逗号分隔。",
            },
            {
                "key": "smsbower_phone_exception",
                "label": "排除号码前缀",
                "placeholder": "例如 7918,7900111",
                "category": "advanced",
                "hint": "可选；排除国家代码加 3 到 6 位号码掩码，多个前缀用英文逗号分隔。",
            },
            {
                "key": "smsbower_user_id",
                "label": "经销商 User ID",
                "placeholder": "可选",
                "category": "advanced",
                "hint": "SMSBower 经销商参数；普通账户留空。",
            },
            {
                "key": "smsbower_base_url",
                "label": "API 地址",
                "placeholder": "https://smsbower.page/stubs/handler_api.php",
                "default_value": "https://smsbower.page/stubs/handler_api.php",
                "category": "connection",
            },
            {
                "key": "smsbower_request_timeout",
                "label": "单次请求超时秒",
                "placeholder": "15",
                "default_value": "15",
                "category": "connection",
            },
            {
                "key": "smsbower_poll_interval",
                "label": "查码轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
                "hint": "等待短信验证码时使用；可在后续流程中按任务覆盖。",
            },
            {
                "key": "smsbower_otp_timeout_seconds",
                "label": "短信验证码等待秒数",
                "placeholder": "120",
                "default_value": "120",
                "category": "connection",
                "hint": "手机号提交成功后等待短信验证码的最长时间。",
            },
            {
                "key": "smsbower_buy_max_attempts",
                "label": "买号重试次数",
                "placeholder": "20",
                "default_value": "20",
                "category": "connection",
                "hint": "当 SMSBower 返回 NO_NUMBERS / 暂无号码时继续重试买号，直到成功或达到次数上限。",
            },
            {
                "key": "smsbower_buy_retry_interval",
                "label": "买号重试间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
                "hint": "买号暂无库存时两次重试之间的等待时间。",
            },
        ],
    },
    {
        "provider_type": "sms",
        "provider_key": "herosms",
        "label": "HeroSMS",
        "description": "SMS-Activate 兼容接码服务，支持实时选择服务和国家、价格限制以及完整订单状态流转",
        "driver_type": "herosms",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "herosms_api_key", "label": "API Key", "secret": True, "category": "auth", "hint": "HeroSMS 用户中心的 API Key，日志与异常会自动脱敏。"},
            {"key": "herosms_default_service", "label": "默认接码服务", "type": "async-select", "asyncUrl": "/provider-settings/options", "placeholder": "填写 API Key 后加载服务", "default_value": "dr", "category": "connection", "hint": "默认使用 OpenAI 服务代码 dr，可从 HeroSMS 实时目录搜索切换。"},
            {"key": "herosms_default_country", "label": "默认国家/地区", "type": "async-select", "asyncUrl": "/provider-settings/options", "placeholder": "填写 API Key 后加载国家/地区", "category": "connection"},
            {"key": "herosms_number_api", "label": "买号接口", "type": "select", "options": [{"value": "getNumberV2", "label": "getNumberV2（推荐）"}, {"value": "getNumber", "label": "getNumber（兼容模式）"}], "default_value": "getNumberV2", "category": "connection"},
            {"key": "herosms_max_price", "label": "最高价格", "placeholder": "例如 0.5", "category": "connection", "hint": "可选，作为 maxPrice 参数限制单次买号价格。"},
            {"key": "herosms_operator", "label": "运营商", "placeholder": "例如 any 或 tele2,beeline", "category": "connection", "hint": "可选；多个运营商代码用英文逗号分隔。"},
            {"key": "herosms_fixed_price", "label": "固定价格", "type": "toggle", "default_value": "false", "category": "advanced", "hint": "开启后向 HeroSMS 传入 fixedPrice=true。"},
            {"key": "herosms_phone_exception", "label": "排除号码前缀", "placeholder": "例如 7918,7900111", "category": "advanced"},
            {"key": "herosms_ref", "label": "推荐 ID", "placeholder": "可选", "category": "advanced"},
            {"key": "herosms_base_url", "label": "API 地址", "default_value": "https://hero-sms.com/stubs/handler_api.php", "category": "connection"},
            {"key": "herosms_request_timeout", "label": "单次请求超时秒", "default_value": "15", "category": "connection"},
            {"key": "herosms_poll_interval", "label": "查码轮询间隔秒", "default_value": "5", "category": "connection"},
            {"key": "herosms_otp_timeout_seconds", "label": "短信验证码等待秒数", "default_value": "120", "category": "connection"},
            {"key": "herosms_buy_max_attempts", "label": "买号重试次数", "default_value": "20", "category": "connection"},
            {"key": "herosms_buy_retry_interval", "label": "买号重试间隔秒", "default_value": "3", "category": "connection"},
        ],
    },
    {
        "provider_type": "sms",
        "provider_key": "smspool",
        "label": "SMSPool",
        "description": "通过 SMSPool REST API 购买号码、查询验证码和取消订单，国家与服务目录实时加载",
        "driver_type": "smspool",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "smspool_api_key", "label": "API Key", "secret": True, "category": "auth", "hint": "SMSPool 账户 API Key，只在服务端请求中使用。"},
            {"key": "smspool_default_service", "label": "默认接码服务", "type": "async-select", "asyncUrl": "/provider-settings/options", "placeholder": "加载服务目录", "default_value": "671", "category": "connection", "hint": "当前 OpenAI / ChatGPT 服务 ID 默认为 671，目录变化时可直接重新选择。"},
            {"key": "smspool_default_country", "label": "默认国家/地区", "type": "async-select", "asyncUrl": "/provider-settings/options", "placeholder": "加载国家/地区", "default_value": "9", "category": "connection"},
            {"key": "smspool_max_price", "label": "最高价格", "placeholder": "例如 0.20", "category": "connection"},
            {"key": "smspool_pricing_option", "label": "定价选项", "placeholder": "0", "default_value": "0", "category": "advanced", "hint": "传给 SMSPool purchase/sms 的 pricing_option，通常保持 0。"},
            {"key": "smspool_base_url", "label": "API 地址", "default_value": "https://api.smspool.net", "category": "connection"},
            {"key": "smspool_request_timeout", "label": "单次请求超时秒", "default_value": "15", "category": "connection"},
            {"key": "smspool_poll_interval", "label": "查码轮询间隔秒", "default_value": "5", "category": "connection"},
            {"key": "smspool_otp_timeout_seconds", "label": "短信验证码等待秒数", "default_value": "120", "category": "connection"},
            {"key": "smspool_buy_max_attempts", "label": "买号重试次数", "default_value": "20", "category": "connection"},
            {"key": "smspool_buy_retry_interval", "label": "买号重试间隔秒", "default_value": "3", "category": "connection"},
        ],
    },
    {
        "provider_type": "sms",
        "provider_key": "fivesim",
        "label": "5sim",
        "description": "通过 5sim Activation API 获取 OpenAI 号码，支持运营商、最高价格、订单完成和取消",
        "driver_type": "fivesim",
        "default_auth_mode": "token",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "token", "label": "Bearer Token"}],
        "fields": [
            {"key": "fivesim_api_key", "label": "API Token", "secret": True, "category": "auth", "hint": "5sim 用户中心生成的 API Token，将通过 Bearer 认证发送。"},
            {"key": "fivesim_default_service", "label": "默认接码服务", "type": "async-select", "asyncUrl": "/provider-settings/options", "default_value": "openai", "category": "connection"},
            {"key": "fivesim_default_country", "label": "默认国家/地区", "type": "async-select", "asyncUrl": "/provider-settings/options", "placeholder": "加载 5sim 国家目录", "default_value": "vietnam", "category": "connection"},
            {"key": "fivesim_operator", "label": "运营商", "placeholder": "any", "default_value": "any", "category": "connection", "hint": "通常使用 any；也可填写 5sim 对应国家支持的运营商代码。"},
            {"key": "fivesim_max_price", "label": "最高价格", "placeholder": "可选", "category": "connection", "hint": "作为 maxPrice 参数传入；设置时建议运营商保持 any。"},
            {"key": "fivesim_base_url", "label": "API 地址", "default_value": "https://5sim.net", "category": "connection"},
            {"key": "fivesim_request_timeout", "label": "单次请求超时秒", "default_value": "15", "category": "connection"},
            {"key": "fivesim_poll_interval", "label": "查码轮询间隔秒", "default_value": "5", "category": "connection"},
            {"key": "fivesim_otp_timeout_seconds", "label": "短信验证码等待秒数", "default_value": "180", "category": "connection"},
            {"key": "fivesim_buy_max_attempts", "label": "买号重试次数", "default_value": "20", "category": "connection"},
            {"key": "fivesim_buy_retry_interval", "label": "买号重试间隔秒", "default_value": "3", "category": "connection"},
        ],
    },
    {
        "provider_type": "sms",
        "provider_key": "smstome",
        "label": "SMSToMe",
        "description": "从 SMSToMe 公共号码池抓取可用号码并轮询新短信，保留已使用号码状态以避免重复分配",
        "driver_type": "smstome",
        "default_auth_mode": "cookie",
        "enabled": True,
        "category": "free",
        "auth_modes": [{"value": "cookie", "label": "Cookie（可选）"}],
        "fields": [
            {"key": "smstome_cookie", "label": "Cookie", "secret": True, "category": "auth", "hint": "可选；遇到 Cloudflare 校验时粘贴浏览器 Cookie，正常可访问时留空。"},
            {"key": "smstome_country_slugs", "label": "默认国家/地区", "type": "async-select", "asyncUrl": "/provider-settings/options", "default_value": "poland", "category": "connection", "hint": "使用 SMSToMe 国家 slug，运行时会从对应国家的公共号码池分配号码。"},
            {"key": "smstome_state_file", "label": "已使用号码状态文件", "default_value": "data/.smstome_phone_state.json", "category": "advanced", "hint": "记录已分配号码，避免不同注册任务重复使用。"},
            {"key": "smstome_sync_max_pages_per_country", "label": "每个国家最多抓取页数", "default_value": "5", "category": "connection"},
            {"key": "smstome_base_url", "label": "站点地址", "default_value": "https://smstome.com", "category": "connection"},
            {"key": "smstome_request_timeout", "label": "单次请求超时秒", "default_value": "20", "category": "connection"},
            {"key": "smstome_poll_interval_seconds", "label": "查码轮询间隔秒", "default_value": "5", "category": "connection"},
            {"key": "smstome_otp_timeout_seconds", "label": "短信验证码等待秒数", "default_value": "120", "category": "connection"},
            {"key": "smstome_phone_attempts", "label": "取号重试次数", "default_value": "3", "category": "connection"},
            {"key": "smstome_buy_retry_interval", "label": "取号重试间隔秒", "default_value": "2", "category": "connection"},
        ],
    },
    # ── proxy ────────────────────────────────────────────────────────
    {
        "provider_type": "proxy",
        "provider_key": "api_extract",
        "label": "API 提取代理",
        "description": "通过 HTTP API 动态提取代理 IP 列表，适用于大多数代理商的 API 提取接口",
        "driver_type": "api_extract",
        "default_auth_mode": "",
        "enabled": False,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "proxy_api_url", "label": "API 地址", "placeholder": "https://provider.com/api/get_proxy?key=xxx"},
            {"key": "proxy_protocol", "label": "协议", "placeholder": "http / socks5"},
            {"key": "proxy_username", "label": "用户名 (可选)"},
            {"key": "proxy_password", "label": "密码 (可选)", "secret": True},
        ],
    },
    {
        "provider_type": "proxy",
        "provider_key": "rotating_gateway",
        "label": "旋转网关代理",
        "description": "固定入口地址，每次请求自动分配不同出口 IP，适用于 BrightData / Oxylabs / IPRoyal 等",
        "driver_type": "rotating_gateway",
        "default_auth_mode": "",
        "enabled": False,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "proxy_gateway_url", "label": "网关地址", "placeholder": "http://user:pass@gate.example.com:7777"},
        ],
    },
]


class ProviderDefinitionsRepository:

    def ensure_seeded(self) -> None:
        """将内置 provider definition 种子数据写入数据库。

        新增的插入，已存在的更新字段定义（label、description、fields 等），
        确保代码升级后内置 provider 的元数据能同步到数据库。
        """
        with Session(engine) as session:
            existing: dict[str, ProviderDefinitionModel] = {}
            for row in session.exec(select(ProviderDefinitionModel)).all():
                key = f"{row.provider_type}::{row.provider_key}"
                existing[key] = row

            changed = False
            for seed in _BUILTIN_DEFINITIONS:
                key = f"{seed['provider_type']}::{seed['provider_key']}"
                item = existing.get(key)

                if item is None:
                    # 新增
                    item = ProviderDefinitionModel(
                        provider_type=seed["provider_type"],
                        provider_key=seed["provider_key"],
                        created_at=_utcnow(),
                    )
                    logger.info("种子数据: 新增 %s/%s", seed["provider_type"], seed["provider_key"])

                # 更新元数据（每次启动都同步，确保代码变更生效）
                item.label = seed.get("label", seed["provider_key"])
                item.description = seed.get("description", "")
                item.driver_type = seed.get("driver_type", seed["provider_key"])
                item.default_auth_mode = seed.get("default_auth_mode", "")
                item.enabled = (
                    seed["provider_key"] in SUPPORTED_MAILBOX_PROVIDER_KEYS
                    if seed["provider_type"] == "mailbox"
                    else seed["provider_key"] in SUPPORTED_SMS_PROVIDER_KEYS
                    if seed["provider_type"] == "sms"
                    else seed.get("enabled", True)
                )
                item.is_builtin = True
                item.category = seed.get("category", "")
                item.set_auth_modes(list(seed.get("auth_modes") or []))
                item.set_fields(list(seed.get("fields") or []))
                if not item.get_metadata():
                    # 只在 metadata 为空时写入种子值，避免覆盖用户自定义的 pipeline
                    item.set_metadata(dict(seed.get("metadata") or {}))
                item.updated_at = _utcnow()
                session.add(item)
                changed = True

            # Keep historical/custom mailbox definitions in the database so
            # upgrades are non-destructive, but remove them from active use.
            for item in existing.values():
                if (
                    item.provider_type == "mailbox"
                    and item.provider_key not in SUPPORTED_MAILBOX_PROVIDER_KEYS
                    and item.enabled
                ):
                    item.enabled = False
                    item.updated_at = _utcnow()
                    session.add(item)
                    changed = True
                if (
                    item.provider_type == "sms"
                    and item.provider_key not in SUPPORTED_SMS_PROVIDER_KEYS
                    and item.enabled
                ):
                    item.enabled = False
                    item.updated_at = _utcnow()
                    session.add(item)
                    changed = True

            if changed:
                session.commit()

    # ── 查询（全部从 DB） ────────────────────────────────────────────

    def list_by_type(self, provider_type: str, *, enabled_only: bool = False) -> list[ProviderDefinitionModel]:
        with Session(engine) as session:
            query = select(ProviderDefinitionModel).where(ProviderDefinitionModel.provider_type == provider_type)
            if enabled_only:
                query = query.where(ProviderDefinitionModel.enabled == True)  # noqa: E712
            items = session.exec(query.order_by(ProviderDefinitionModel.id)).all()
            if provider_type == "mailbox":
                items = [item for item in items if item.provider_key in SUPPORTED_MAILBOX_PROVIDER_KEYS]
            if provider_type == "sms":
                items = [item for item in items if item.provider_key in SUPPORTED_SMS_PROVIDER_KEYS]
            return items

    def get_by_key(self, provider_type: str, provider_key: str) -> ProviderDefinitionModel | None:
        with Session(engine) as session:
            return session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .where(ProviderDefinitionModel.provider_key == provider_key)
            ).first()

    def list_driver_templates(self, provider_type: str) -> list[dict]:
        """从 DB 读取：按 driver_type 去重，返回可用驱动模板列表。"""
        with Session(engine) as session:
            definitions = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .order_by(ProviderDefinitionModel.is_builtin.desc(), ProviderDefinitionModel.id)
            ).all()
        seen: dict[str, dict] = {}
        for d in definitions:
            if provider_type == "mailbox" and d.provider_key not in SUPPORTED_MAILBOX_PROVIDER_KEYS:
                continue
            if provider_type == "sms" and d.provider_key not in SUPPORTED_SMS_PROVIDER_KEYS:
                continue
            dt = d.driver_type or ""
            if dt and dt not in seen:
                seen[dt] = {
                    "provider_type": d.provider_type,
                    "provider_key": d.provider_key,
                    "driver_type": dt,
                    "label": d.label,
                    "description": d.description,
                    "default_auth_mode": d.default_auth_mode,
                    "auth_modes": d.get_auth_modes(),
                    "fields": d.get_fields(),
                }
        return list(seen.values())

    def _get_driver_defaults(self, provider_type: str, driver_type: str) -> dict | None:
        """从 DB 中查找同 driver_type 的已有 definition 作为模板。"""
        with Session(engine) as session:
            ref = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .where(ProviderDefinitionModel.driver_type == driver_type)
                .order_by(ProviderDefinitionModel.is_builtin.desc(), ProviderDefinitionModel.id)
            ).first()
            if not ref:
                return None
            return {
                "default_auth_mode": ref.default_auth_mode,
                "auth_modes": ref.get_auth_modes(),
                "fields": ref.get_fields(),
            }

    # ── 写入 ────────────────────────────────────────────────────────

    def save(
        self,
        *,
        definition_id: int | None,
        provider_type: str,
        provider_key: str,
        label: str,
        description: str,
        driver_type: str,
        enabled: bool,
        default_auth_mode: str = "",
        metadata: dict | None = None,
    ) -> ProviderDefinitionModel:
        defaults = self._get_driver_defaults(provider_type, driver_type)

        with Session(engine) as session:
            if definition_id:
                item = session.get(ProviderDefinitionModel, definition_id)
                if not item:
                    raise ValueError("provider definition 不存在")
            else:
                item = session.exec(
                    select(ProviderDefinitionModel)
                    .where(ProviderDefinitionModel.provider_type == provider_type)
                    .where(ProviderDefinitionModel.provider_key == provider_key)
                ).first()
                if not item:
                    item = ProviderDefinitionModel(
                        provider_type=provider_type,
                        provider_key=provider_key,
                    )
                    item.created_at = _utcnow()

            item.provider_type = provider_type
            item.provider_key = provider_key
            item.label = label or provider_key
            item.description = description or ""
            item.driver_type = driver_type
            item.default_auth_mode = default_auth_mode or item.default_auth_mode or (defaults.get("default_auth_mode", "") if defaults else "")
            item.enabled = bool(enabled)
            if not item.get_auth_modes() and defaults:
                item.set_auth_modes(list(defaults.get("auth_modes") or []))
            if not item.get_fields() and defaults:
                item.set_fields(list(defaults.get("fields") or []))
            item.set_metadata(dict(metadata or {}))
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def delete(self, definition_id: int) -> bool:
        with Session(engine) as session:
            item = session.get(ProviderDefinitionModel, definition_id)
            if not item:
                return False
            has_settings = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == item.provider_type)
                .where(ProviderSettingModel.provider_key == item.provider_key)
            ).first()
            if has_settings:
                raise ValueError("请先删除对应 provider 配置，再删除 definition")
            session.delete(item)
            session.commit()
            return True
