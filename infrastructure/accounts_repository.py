from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from core.account_display import build_account_display_summary
from core.db import AccountModel, engine
from core.account_graph import (
    build_account_view,
    compute_account_stats,
    load_account_graphs,
    matches_status_filter,
    patch_account_graph,
    purge_account_graph,
    sync_account_graph,
)
from core.platform_accounts import resolve_primary_token
from domain.accounts import (
    AccountExportSelection,
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)


def _build_summary_updates(
    overview: dict | None,
    *,
    cashier_url: str | None = None,
    region: str | None = None,
    trial_end_time: int | None = None,
) -> dict | None:
    summary = dict(overview or {})
    if cashier_url is not None:
        summary["cashier_url"] = cashier_url
    if region is not None:
        summary["region"] = region
    if trial_end_time is not None:
        summary["trial_end_time"] = int(trial_end_time or 0)
    return summary or None


def _build_credential_updates(
    credentials: dict | None,
) -> dict | None:
    return dict(credentials or {}) or None


def _to_record(model: AccountModel, graph: dict | None = None) -> AccountRecord:
    graph = graph or {}
    overview = graph.get("overview") or {}
    lifecycle_status = graph.get("lifecycle_status") or "registered"
    validity_status = graph.get("validity_status") or "unknown"
    plan_state = graph.get("plan_state") or "unknown"
    plan_name = graph.get("plan_name") or ""
    display_status = graph.get("display_status") or "registered"
    provider_resources = list(graph.get("provider_resources") or [])
    return AccountRecord(
        id=int(model.id or 0),
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        primary_token=resolve_primary_token(model, graph),
        trial_end_time=int(overview.get("trial_end_time") or 0),
        cashier_url=str(overview.get("cashier_url") or ""),
        lifecycle_status=lifecycle_status,
        validity_status=validity_status,
        plan_state=plan_state,
        plan_name=plan_name,
        display_status=display_status,
        overview=overview,
        display_summary=build_account_display_summary(
            platform=model.platform,
            email=model.email,
            lifecycle_status=lifecycle_status,
            validity_status=validity_status,
            plan_state=plan_state,
            plan_name=plan_name,
            display_status=display_status,
            overview=overview,
            provider_resources=provider_resources,
        ),
        account_view=build_account_view(model, graph),
        credentials=list(graph.get("credentials") or []),
        provider_accounts=list(graph.get("provider_accounts") or []),
        provider_resources=provider_resources,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AccountsRepository:
    @staticmethod
    def _load_records(session: Session, models: list[AccountModel]) -> list[AccountRecord]:
        account_ids = [int(model.id or 0) for model in models if model.id]
        graphs = load_account_graphs(session, account_ids)
        missing = [
            model
            for model in models
            if not (graphs.get(int(model.id or 0)) or {}).get("status")
        ]
        if missing:
            for model in missing:
                sync_account_graph(session, model)
            session.commit()
            graphs = load_account_graphs(session, account_ids)
        return [_to_record(model, graphs.get(int(model.id or 0), {})) for model in models]

    def list(self, query: AccountQuery) -> tuple[int, list[AccountRecord]]:
        page = max(query.page, 1)
        page_size = max(query.page_size, 1)
        with Session(engine) as session:
            statement = select(AccountModel)
            if query.platform:
                statement = statement.where(AccountModel.platform == query.platform)
            if query.email:
                statement = statement.where(AccountModel.email.contains(query.email))
            statement = statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            models = session.exec(statement).all()
            records = self._load_records(session, models)
            if query.status:
                records = [item for item in records if matches_status_filter({
                    "display_status": item.display_status,
                    "lifecycle_status": item.lifecycle_status,
                    "plan_state": item.plan_state,
                    "validity_status": item.validity_status,
                }, query.status)]
        total = len(records)
        start = (page - 1) * page_size
        end = start + page_size
        return total, records[start:end]

    def get(self, account_id: int) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            records = self._load_records(session, [model])
            return records[0] if records else None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        if not selection.select_all and not selection.ids:
            return []
        with Session(engine) as session:
            statement = select(AccountModel)
            if selection.platform:
                statement = statement.where(AccountModel.platform == selection.platform)
            if selection.search_filter:
                statement = statement.where(AccountModel.email.contains(selection.search_filter))
            if not selection.select_all and selection.ids:
                statement = statement.where(AccountModel.id.in_(selection.ids))
            statement = statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            models = session.exec(statement).all()
            records = self._load_records(session, models)
        if selection.status_filter:
            records = [item for item in records if matches_status_filter({
                "display_status": item.display_status,
                "lifecycle_status": item.lifecycle_status,
                "plan_state": item.plan_state,
                "validity_status": item.validity_status,
            }, selection.status_filter)]
        return records

    def update(self, account_id: int, command: AccountUpdateCommand) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            if command.password is not None:
                model.password = command.password
            if command.user_id is not None:
                model.user_id = command.user_id
            model.updated_at = datetime.now(timezone.utc)
            patch_account_graph(
                session,
                model,
                lifecycle_status=command.lifecycle_status,
                primary_token=command.primary_token,
                cashier_url=command.cashier_url,
                region=command.region,
                trial_end_time=command.trial_end_time,
                summary_updates=_build_summary_updates(
                    command.overview,
                    cashier_url=command.cashier_url,
                    region=command.region,
                    trial_end_time=command.trial_end_time,
                ),
                credential_updates=_build_credential_updates(command.credentials),
                provider_accounts=command.provider_accounts,
                provider_resources=command.provider_resources,
                replace_provider_accounts=command.replace_provider_accounts,
                replace_provider_resources=command.replace_provider_resources,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            return self._load_records(session, [model])[0]

    def delete(self, account_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return False
            from core.mailbox_lifecycle import MailboxAllocationLifecycle

            MailboxAllocationLifecycle().archive_account_mailbox(account_id)
            purge_account_graph(session, account_id)
            session.delete(model)
            session.commit()
            return True

    def import_lines(self, platform: str, lines: list[AccountImportLine]) -> int:
        created = 0
        with Session(engine) as session:
            models_by_email: dict[str, AccountModel] = {}
            for line in lines:
                model = models_by_email.get(line.email)
                if model is None:
                    model = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == platform)
                        .where(AccountModel.email == line.email)
                    ).first()
                is_new = model is None
                if model is None:
                    model = AccountModel(
                        platform=platform,
                        email=line.email,
                        password=line.password,
                    )
                    session.add(model)
                    session.flush()
                    created += 1
                else:
                    model.password = line.password
                    model.updated_at = datetime.now(timezone.utc)
                    session.add(model)
                models_by_email[line.email] = model

                extra = dict(line.extra or {})
                summary_updates = dict(extra.get("overview") or extra.get("summary") or {})
                for key in (
                    "valid",
                    "validity_status",
                    "checked_at",
                    "check_source",
                    "subscription_source",
                    "remote_email",
                    "region",
                    "plan",
                    "plan_name",
                    "plan_type",
                    "plan_state",
                    "trial_end_time",
                    "cashier_url",
                    "phone_bound",
                    "phone_number_masked",
                    "mfa_enabled",
                    "amr",
                    "chatgpt_usage",
                    "wham_usage",
                    "usage",
                    "usage_summary",
                    "subscription",
                    "security",
                    "profile",
                    "registration_auth_mode",
                ):
                    if key in extra and key not in summary_updates:
                        summary_updates[key] = extra[key]
                credential_updates = dict(extra.get("credentials") or {})
                for key in (
                    "access_token",
                    "refresh_token",
                    "session_token",
                    "id_token",
                    "accessToken",
                    "refreshToken",
                    "sessionToken",
                    "idToken",
                    "cookies",
                    "cookie",
                    "api_key",
                    "wos_session",
                    "sso",
                    "sso_rw",
                    "codex_access_token",
                    "codex_refresh_token",
                    "codex_id_token",
                    "codex_account_id",
                    "codex_email",
                    "codex_plan_type",
                    "codex_expires_at",
                    "codex_last_refresh",
                    "codex_auth_path",
                ):
                    if key in extra and key not in credential_updates:
                        credential_updates[key] = extra[key]
                primary_token = extra.get("primary_token")
                if primary_token in (None, ""):
                    primary_token = extra.get("token")
                lifecycle_status = extra.get("lifecycle_status")
                if lifecycle_status in (None, ""):
                    lifecycle_status = extra.get("status")
                if lifecycle_status in (None, "") and is_new:
                    lifecycle_status = "registered"
                patch_account_graph(
                    session,
                    model,
                    lifecycle_status=str(lifecycle_status or "") or None,
                    primary_token=str(primary_token or "") or None,
                    cashier_url=str(extra.get("cashier_url") or "") or None,
                    summary_updates=summary_updates or None,
                    credential_updates=credential_updates or None,
                    provider_accounts=list(extra.get("provider_accounts") or []) or None,
                    provider_resources=list(extra.get("provider_resources") or []) or None,
                    replace_provider_accounts=bool(extra.get("provider_accounts")),
                    replace_provider_resources=bool(extra.get("provider_resources")),
                )
            session.commit()
        return created

    def stats(self) -> AccountStats:
        with Session(engine) as session:
            accounts = session.exec(select(AccountModel).order_by(AccountModel.created_at.desc(), AccountModel.id.desc())).all()
            records = self._load_records(session, accounts)
        stats = compute_account_stats(
            [
                {
                    "lifecycle_status": item.lifecycle_status,
                    "plan_state": item.plan_state,
                    "validity_status": item.validity_status,
                    "display_status": item.display_status,
                }
                for item in records
            ],
            [item.platform for item in records],
        )
        return AccountStats(
            total=len(records),
            by_platform=stats["by_platform"],
            by_status=stats["by_display_status"],
            by_lifecycle_status=stats["by_lifecycle_status"],
            by_plan_state=stats["by_plan_state"],
            by_validity_status=stats["by_validity_status"],
            by_display_status=stats["by_display_status"],
        )
