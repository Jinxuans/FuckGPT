"""数据库模型 - SQLite via SQLModel"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, Session, create_engine, select


def _utcnow():
    return datetime.now(timezone.utc)


def _default_database_url() -> str:
    database_path = Path(__file__).resolve().parent.parent / "account_manager.db"
    return f"sqlite:///{database_path}"


DATABASE_URL = os.getenv("ACCOUNT_MANAGER_DATABASE_URL", _default_database_url())


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


engine = create_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "email", name="uq_accounts_platform_email"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AccountAuthCredentialModel(SQLModel, table=True):
    __tablename__ = "account_auth_credentials"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "scope",
            "provider_name",
            "key",
            name="uq_account_auth_credentials_key",
        ),
        Index("ix_account_auth_credentials_account_scope", "account_id", "scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    scope: str = Field(default="platform", index=True)
    provider_name: str = Field(default="", index=True)
    credential_type: str = Field(default="secret", index=True)
    key: str = Field(default="", index=True)
    value: str = ""
    is_primary: bool = False
    source: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class AccountStatusModel(SQLModel, table=True):
    __tablename__ = "account_status"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    lifecycle_status: str = Field(default="registered", index=True)
    validity_status: str = Field(default="unknown", index=True)
    display_status: str = Field(default="registered", index=True)
    remote_email: str = ""
    region: str = ""
    checked_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AccountSubscriptionModel(SQLModel, table=True):
    __tablename__ = "account_subscription"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    plan_type: str = Field(default="", index=True)
    plan_state: str = Field(default="unknown", index=True)
    source: str = ""
    trial_end_time: int = 0
    cashier_url: str = ""
    raw_json: str = "{}"
    checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_raw(self) -> dict:
        return json.loads(self.raw_json or "{}")

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountSecurityProfileModel(SQLModel, table=True):
    __tablename__ = "account_security_profile"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    phone_bound: bool = Field(default=False, index=True)
    phone_number_masked: str = ""
    mfa_enabled: bool = Field(default=False, index=True)
    amr_json: str = "[]"
    raw_json: str = "{}"
    checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_amr(self) -> list:
        data = json.loads(self.amr_json or "[]")
        return data if isinstance(data, list) else []

    def set_amr(self, data: list):
        self.amr_json = json.dumps(data or [], ensure_ascii=False, default=str)

    def get_raw(self) -> dict:
        data = json.loads(self.raw_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountUsageSnapshotModel(SQLModel, table=True):
    __tablename__ = "account_usage_snapshot"
    __table_args__ = (
        Index(
            "ix_account_usage_snapshot_account_provider_checked",
            "account_id",
            "provider",
            "checked_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider: str = Field(default="", index=True)
    plan_type: str = Field(default="", index=True)
    used_percent: Optional[float] = None
    limit_reached: bool = Field(default=False, index=True)
    reset_at: int = 0
    credits_json: str = "{}"
    raw_json: str = "{}"
    checked_at: datetime = Field(default_factory=_utcnow, index=True)
    created_at: datetime = Field(default_factory=_utcnow)

    def get_credits(self) -> dict:
        data = json.loads(self.credits_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_credits(self, data: dict):
        self.credits_json = json.dumps(data or {}, ensure_ascii=False, default=str)

    def get_raw(self) -> dict:
        data = json.loads(self.raw_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountCodexAuthModel(SQLModel, table=True):
    __tablename__ = "account_codex_auth"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    codex_email: str = Field(default="", index=True)
    codex_account_id: str = Field(default="", index=True)
    codex_plan_type: str = Field(default="", index=True)
    auth_path: str = ""
    expires_at: Optional[datetime] = None
    last_refresh: Optional[datetime] = None
    has_access_token: bool = Field(default=False, index=True)
    has_refresh_token: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProviderAccountModel(SQLModel, table=True):
    __tablename__ = "provider_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    login_identifier: str = Field(default="", index=True)
    display_name: str = ""
    credentials_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_credentials(self) -> dict:
        return json.loads(self.credentials_json or "{}")

    def set_credentials(self, data: dict):
        self.credentials_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderResourceModel(SQLModel, table=True):
    __tablename__ = "provider_resources"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    resource_type: str = Field(default="resource", index=True)
    resource_identifier: str = Field(default="", index=True)
    handle: str = ""
    display_name: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxProviderAccountModel(SQLModel, table=True):
    """Provider login credentials shared by one or more mailbox addresses."""

    __tablename__ = "mailbox_provider_accounts"
    __table_args__ = (
        UniqueConstraint("provider_name", "login_identifier", name="uq_mailbox_provider_accounts_login"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_name: str = Field(index=True)
    login_identifier: str = Field(index=True)
    display_name: str = ""
    credentials_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_credentials(self) -> dict:
        return json.loads(self.credentials_json or "{}")

    def set_credentials(self, data: dict):
        self.credentials_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxResourceModel(SQLModel, table=True):
    """Canonical mailbox resource, independent from any GPT account."""

    __tablename__ = "mailbox_resources"
    __table_args__ = (
        UniqueConstraint("provider_name", "resource_identifier", name="uq_mailbox_resources_provider_identifier"),
        UniqueConstraint("provider_name", "address", name="uq_mailbox_resources_provider_address"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_account_id: int = Field(index=True, foreign_key="mailbox_provider_accounts.id")
    provider_name: str = Field(index=True)
    resource_identifier: str = Field(index=True)
    address: str = Field(index=True)
    parent_address: str = ""
    status: str = Field(default="available", index=True)
    provider_resource_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_provider_resource(self) -> dict:
        return json.loads(self.provider_resource_json or "{}")

    def set_provider_resource(self, data: dict):
        self.provider_resource_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxAllocationModel(SQLModel, table=True):
    """One registration attempt's claim of a mailbox resource."""

    __tablename__ = "mailbox_allocations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_mailbox_allocations_attempt"),
        Index(
            "uq_mailbox_allocations_active_resource",
            "resource_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: str = Field(primary_key=True)
    resource_id: int = Field(index=True, foreign_key="mailbox_resources.id")
    attempt_id: str = Field(index=True)
    task_id: str = Field(default="", index=True)
    subtask_id: str = ""
    platform: str = Field(default="chatgpt", index=True)
    status: str = Field(default="active", index=True)
    reason: str = ""
    account_id: Optional[int] = Field(default=None, index=True, foreign_key="accounts.id")
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class MailboxAccountLinkModel(SQLModel, table=True):
    """One-to-one successful GPT account to primary verification mailbox link."""

    __tablename__ = "mailbox_account_links"
    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_mailbox_account_links_resource"),
        UniqueConstraint("allocation_id", name="uq_mailbox_account_links_allocation"),
        UniqueConstraint("account_id", name="uq_mailbox_account_links_account"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(index=True, foreign_key="mailbox_resources.id")
    allocation_id: str = Field(index=True, foreign_key="mailbox_allocations.id")
    account_id: Optional[int] = Field(default=None, index=True, foreign_key="accounts.id")
    account_id_snapshot: int = Field(index=True)
    account_email: str = ""
    platform: str = Field(default="chatgpt", index=True)
    linked_at: datetime = Field(default_factory=_utcnow)
    archived_at: Optional[datetime] = None


class DataMigrationModel(SQLModel, table=True):
    __tablename__ = "data_migrations"

    key: str = Field(primary_key=True)
    completed_at: datetime = Field(default_factory=_utcnow)
    detail_json: str = "{}"

    def set_detail(self, data: dict):
        self.detail_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderDefinitionModel(SQLModel, table=True):
    __tablename__ = "provider_definitions"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_definitions_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    label: str = ""
    description: str = ""
    driver_type: str = ""
    default_auth_mode: str = ""
    enabled: bool = True
    is_builtin: bool = False
    category: str = ""  # "free" | "selfhost" | "custom"
    auth_modes_json: str = "[]"
    fields_json: str = "[]"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_auth_modes(self) -> list[dict]:
        return json.loads(self.auth_modes_json or "[]")

    def set_auth_modes(self, data: list[dict]):
        self.auth_modes_json = json.dumps(data or [], ensure_ascii=False)

    def get_fields(self) -> list[dict]:
        return json.loads(self.fields_json or "[]")

    def set_fields(self, data: list[dict]):
        self.fields_json = json.dumps(data or [], ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderSettingModel(SQLModel, table=True):
    __tablename__ = "provider_settings"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_settings_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config_json: str = "{}"
    auth_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_config(self) -> dict:
        return json.loads(self.config_json or "{}")

    def set_config(self, data: dict):
        self.config_json = json.dumps(data or {}, ensure_ascii=False)

    def get_auth(self) -> dict:
        return json.loads(self.auth_json or "{}")

    def set_auth(self, data: dict):
        self.auth_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class TaskModel(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    type: str = Field(index=True)
    platform: str = Field(default="", index=True)
    status: str = Field(default="pending", index=True)
    payload_json: str = "{}"
    result_json: str = "{}"
    progress_current: int = 0
    progress_total: int = 0
    success_count: int = 0
    error_count: int = 0
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_payload(self) -> dict:
        return json.loads(self.payload_json or "{}")

    def set_payload(self, data: dict):
        self.payload_json = json.dumps(data or {}, ensure_ascii=False)

    def get_result(self) -> dict:
        return json.loads(self.result_json or "{}")

    def set_result(self, data: dict):
        self.result_json = json.dumps(data or {}, ensure_ascii=False)


class TaskEventModel(SQLModel, table=True):
    __tablename__ = "task_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    type: str = Field(default="log", index=True)
    level: str = "info"
    message: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)

    def get_detail(self) -> dict:
        return json.loads(self.detail_json or "{}")

    def set_detail(self, data: dict):
        self.detail_json = json.dumps(data or {}, ensure_ascii=False)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


def _save_account_in_session(session: Session, account) -> 'AccountModel':
    from core.account_graph import sync_platform_account_graph

    existing = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == account.platform)
        .where(AccountModel.email == account.email)
    ).first()
    if existing:
        existing.password = account.password
        existing.user_id = account.user_id or ""
        existing.updated_at = _utcnow()
        session.add(existing)
        session.flush()
        sync_platform_account_graph(session, existing, account)
        session.flush()
        return existing
    model = AccountModel(
        platform=account.platform,
        email=account.email,
        password=account.password,
        user_id=account.user_id or "",
    )
    session.add(model)
    session.flush()
    sync_platform_account_graph(session, model, account)
    session.flush()
    return model


def save_account(account, *, session: Session | None = None, commit: bool = True) -> 'AccountModel':
    """Persist an account; optionally join a caller-owned transaction."""

    if session is not None:
        model = _save_account_in_session(session, account)
        if commit:
            session.commit()
            session.refresh(model)
        return model

    with Session(engine) as owned_session:
        model = _save_account_in_session(owned_session, account)
        owned_session.commit()
        owned_session.refresh(model)
        return model


def init_db():
    SQLModel.metadata.create_all(engine)
    from core.account_graph import sync_all_account_graphs
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    _ensure_column("provider_definitions", "category", "TEXT DEFAULT ''")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        ProviderDefinitionsRepository().ensure_seeded()
        _migrate_legacy_provider_keys()
        _cleanup_non_real_providers()
        _cleanup_empty_provider_settings()
        sync_all_account_graphs(session)
        session.commit()

    # Any active allocation that survived a process restart has no live
    # registration worker. Preserve the attempt as interrupted and return its
    # mailbox immediately, as required by the domain policy.
    from core.mailbox_lifecycle import MailboxAllocationLifecycle

    mailbox_lifecycle = MailboxAllocationLifecycle()
    if not os.getenv("PYTEST_CURRENT_TEST"):
        mailbox_lifecycle.migrate_legacy_json_once()
    mailbox_lifecycle.interrupt_active()


def _ensure_column(table: str, column: str, col_type: str):
    """给已有表安全地加一列（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if table not in tables:
        return
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    print(f"[DB] 已添加列 {table}.{column}")


def _cleanup_empty_provider_settings():
    """清理 v1.0.7/v1.0.8 中 PR #42 自动创建的空 ProviderSetting。

    判定条件：config / auth / metadata 三个字段都为空 dict 时认为
    用户从未编辑过，可以安全删除。被删后用户能从前端"新增"按钮
    重新选择对应的 provider。"""
    with Session(engine) as session:
        items = session.exec(select(ProviderSettingModel)).all()
        removed = 0
        for item in items:
            config = item.get_config() or {}
            auth = item.get_auth() or {}
            metadata = item.get_metadata() or {}
            if not config and not auth and not metadata:
                session.delete(item)
                removed += 1
        if removed:
            session.commit()


# 旧版 provider_key → 新版 provider_key 映射
_LEGACY_PROVIDER_KEY_MAP: dict[tuple[str, str], str] = {
    # captcha
    ("captcha", "yescaptcha"): "yescaptcha_api",
    ("captcha", "twocaptcha"): "twocaptcha_api",
}

# 旧版 auth_mode 值 → 新版 auth_mode 值映射
_LEGACY_AUTH_MODE_MAP: dict[str, str] = {
    "endpoint_only": "password",
    "manual_login": "password",
    "bearer_token": "bearer",
    "jwt_token": "token",
    "admin_token": "token",
    "api_key": "apikey",
}


def _migrate_legacy_provider_keys():
    """将旧版 provider_key 和 auth_mode 迁移到新版命名。

    同时迁移 provider_settings 和 provider_definitions 两张表。
    如果新 key 已存在则删除旧记录（避免唯一约束冲突）。
    迁移后还会修正 auth_mode 值，使其匹配新版 definition 的有效值。
    """
    with Session(engine) as session:
        migrated = 0

        # 1. 迁移 provider_key
        for (ptype, old_key), new_key in _LEGACY_PROVIDER_KEY_MAP.items():
            # --- provider_settings ---
            old_setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == ptype)
                .where(ProviderSettingModel.provider_key == old_key)
            ).first()
            if old_setting:
                new_setting = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == ptype)
                    .where(ProviderSettingModel.provider_key == new_key)
                ).first()
                if new_setting:
                    session.delete(old_setting)
                else:
                    old_setting.provider_key = new_key
                    session.add(old_setting)
                migrated += 1

            # --- provider_definitions ---
            old_defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == ptype)
                .where(ProviderDefinitionModel.provider_key == old_key)
            ).first()
            if old_defn:
                new_defn = session.exec(
                    select(ProviderDefinitionModel)
                    .where(ProviderDefinitionModel.provider_type == ptype)
                    .where(ProviderDefinitionModel.provider_key == new_key)
                ).first()
                if new_defn:
                    session.delete(old_defn)
                else:
                    old_defn.provider_key = new_key
                    session.add(old_defn)
                migrated += 1

        if migrated:
            session.commit()
            print(f"[DB] 已迁移 {migrated} 条旧版 provider key")

        # 2. 修正 auth_mode 值
        fixed = 0
        all_settings = session.exec(select(ProviderSettingModel)).all()
        for item in all_settings:
            old_mode = item.auth_mode or ""
            if not old_mode:
                continue
            # 查找对应的 definition
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == item.provider_type)
                .where(ProviderDefinitionModel.provider_key == item.provider_key)
            ).first()
            if not defn:
                continue
            valid_modes = {m.get("value") for m in defn.get_auth_modes()}
            if not valid_modes or old_mode in valid_modes:
                # 当前值已经有效，跳过
                continue
            # 尝试映射
            new_mode = _LEGACY_AUTH_MODE_MAP.get(old_mode)
            if new_mode and new_mode in valid_modes:
                item.auth_mode = new_mode
            elif defn.default_auth_mode:
                item.auth_mode = defn.default_auth_mode
            else:
                continue
            session.add(item)
            fixed += 1

        if fixed:
            session.commit()
            print(f"[DB] 已修正 {fixed} 条旧版 auth_mode")


def _cleanup_non_real_providers():
    """generic_http 不是真实邮箱，从 DB 中清除其 definition 和空 setting。"""
    remove_keys = [("mailbox", "generic_http")]
    with Session(engine) as session:
        for pt, pk in remove_keys:
            setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == pt)
                .where(ProviderSettingModel.provider_key == pk)
            ).first()
            if setting:
                config = setting.get_config() or {}
                auth = setting.get_auth() or {}
                if not config and not auth:
                    session.delete(setting)
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == pt)
                .where(ProviderDefinitionModel.provider_key == pk)
            ).first()
            if defn:
                remaining = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == pt)
                    .where(ProviderSettingModel.provider_key == pk)
                ).first()
                if not remaining:
                    session.delete(defn)
        session.commit()


def get_session():
    with Session(engine) as session:
        yield session
