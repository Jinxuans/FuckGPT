"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, TaskEventModel, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_FOLLOW_PLATFORM,
    PROXY_MODE_MANUAL,
    PROXY_MODE_PROXY_SERVICE,
    mask_proxy_url,
    normalize_proxy_mode,
    resolve_proxy_by_mode,
)
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_CODEX_OAUTH_BATCH = "codex_oauth_batch"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

# A single registration task may run many workers.  Twenty is the UI and
# backend contract; anything larger is deliberately bounded to avoid an
# accidental unbounded thread pool.
MAX_REGISTER_CONCURRENCY = 20
MAX_CODEX_OAUTH_BATCH_CONCURRENCY = 5

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}
STOP_REQUEST_TASK_STATUSES = TERMINAL_TASK_STATUSES | {TASK_STATUS_CANCEL_REQUESTED}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()

# Sub2API Admin keys are intentionally volatile.  The browser submits them
# when creating a task, but they are never put in task payloads, the SQLite
# database, result data, or task logs.  The shared lock also makes attaching
# the config atomic with task creation, before a worker may claim the task.
_register_sub2api_upload_configs: dict[str, dict[str, str]] = {}
_register_sub2api_upload_configs_guard = threading.RLock()


def register_sub2api_upload_configs_guard() -> threading.RLock:
    return _register_sub2api_upload_configs_guard


def set_register_sub2api_upload_config(
    task_id: str,
    *,
    sub2api_url: str,
    api_key: str,
) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs[task_id] = {
            "sub2api_url": str(sub2api_url or "").strip(),
            "api_key": str(api_key or "").strip(),
        }


def get_register_sub2api_upload_config(task_id: str) -> dict[str, str] | None:
    with _register_sub2api_upload_configs_guard:
        config = _register_sub2api_upload_configs.get(task_id)
        return dict(config) if config else None


def clear_register_sub2api_upload_config(task_id: str) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs.pop(task_id, None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type == TASK_TYPE_PLATFORM_ACTION:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    if task_type == TASK_TYPE_CODEX_OAUTH_BATCH:
        return [
            f"account:{int(item)}"
            for item in payload.get("account_ids", [])
            if int(item or 0) > 0
        ]
    return []


def _task_scope(task_type: str, platform: str, payload: dict[str, Any]) -> str:
    if task_type == TASK_TYPE_PLATFORM_ACTION and str(payload.get("action_id") or "") == "codex_oauth_authorize":
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return f"{platform}:{task_type}:codex_oauth_authorize:{account_id}"
    return f"{platform}:{task_type}"


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    payload = {**payload, "platform": "chatgpt"}
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload=payload,
        progress_total=count,
    )


def create_account_check_all_task(
    platform: str = "",
    limit: int = 50,
    account_ids: list[int] | None = None,
    platform_proxy_mode: str = "",
    platform_proxy_value: str = "",
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    payload: dict[str, Any] = {"platform": platform, "limit": int(limit or 50)}
    if platform_proxy_mode:
        payload["platform_proxy_mode"] = platform_proxy_mode
    if platform_proxy_value:
        payload["platform_proxy_value"] = platform_proxy_value
    progress_total = max(int(limit or 50), 1)
    if account_ids is not None:
        payload["account_ids"] = normalized_ids
        progress_total = len(normalized_ids)
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload=payload,
        progress_total=progress_total,
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def create_codex_oauth_batch_task(
    *,
    platform: str,
    account_ids: list[int],
    params: dict[str, Any] | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    return create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform=platform or "chatgpt",
        payload={
            "platform": platform or "chatgpt",
            "account_ids": normalized_ids,
            "action_id": "codex_oauth_authorize",
            "params": dict(params or {}),
            "concurrency": int(concurrency or 1),
        },
        progress_total=len(normalized_ids),
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    platform: str = "",
    task_type: str = "",
) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 1), 100)
    offset = max(int(offset or 0), 0)
    status = str(status or "").strip()
    platform = str(platform or "").strip()
    task_type = str(task_type or "").strip()

    conditions = []
    if status:
        conditions.append(TaskModel.status == status)
    if platform:
        conditions.append(TaskModel.platform == platform)
    if task_type:
        conditions.append(TaskModel.type == task_type)

    with Session(engine) as session:
        q = select(TaskModel)
        count_q = select(func.count()).select_from(TaskModel)
        for condition in conditions:
            q = q.where(condition)
            count_q = count_q.where(condition)
        items = session.exec(
            q.order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(session.exec(count_q).one() or 0)
        running = int(
            session.exec(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.status.in_(list(ACTIVE_TASK_STATUSES)))
            ).one()
            or 0
        )

    return {
        "items": [serialize_task(item) for item in items],
        "total": total,
        "running": running,
        "limit": limit,
        "offset": offset,
    }


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    return list_task_events_page(task_id, since=since, limit=limit)["items"]


def list_task_events_page(
    task_id: str,
    *,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    since = max(int(since or 0), 0)
    before = max(int(before or 0), 0)
    with Session(engine) as session:
        if before:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id < before)
                .order_by(TaskEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(q).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        elif latest:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .order_by(TaskEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(q).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        else:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id > since)
                .order_by(TaskEventModel.id)
                .limit(limit)
            )
            items = session.exec(q).all()
            has_more_before = False
        serialized = [serialize_event(item) for item in items]
    return {
        "items": serialized,
        "cursor": max([int(item["id"] or 0) for item in serialized] or [since]),
        "before": min([int(item["id"] or 0) for item in serialized] or [before]),
        "has_more_before": has_more_before,
    }


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
    else:
        task.status = TASK_STATUS_CANCEL_REQUESTED


def claim_next_runnable_task(
    *,
    running_scope_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_scope: int = 1,
) -> Optional[dict[str, Any]]:
    running_scope_counts = dict(running_scope_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            scope = _task_scope(task.type, platform, payload)
            if scope and running_scope_counts.get(scope, 0) >= max_parallel_per_scope:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "type": task.type, "scope": scope, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        if event_type != "state" and self.is_cancel_requested():
            return
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {message}")

    def mark_running(self) -> bool:
        started = {"ok": False}

        def _update(task: TaskModel) -> None:
            if task.status in STOP_REQUEST_TASK_STATUSES:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()
            started["ok"] = True

        _mutate_task(self.task_id, _update)
        if started["ok"]:
            self.log("任务已开始执行", event_type="state")
        return started["ok"]

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool((not task) or task.status in STOP_REQUEST_TASK_STATUSES)

    def _status(self) -> str:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return str(task.status or "") if task else ""

    def _is_terminal(self) -> bool:
        return self._status() in TERMINAL_TASK_STATUSES

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        if self.is_cancel_requested():
            return
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        current_status = self._status()
        if current_status in TERMINAL_TASK_STATUSES and current_status != status:
            return
        def _update(task: TaskModel) -> None:
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _build_platform_instance(
    platform_name: str,
    payload: dict[str, Any],
    logger: TaskLogger,
    platform_proxy: str | None = None,
    mailbox_proxy: str | None = None,
    shared_mailbox=None,
    task_id: str = "",
    subtask_id: str = "",
):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "headless") or "headless")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    extra["_log_fn"] = logger.log
    extra["mailbox_proxy"] = mailbox_proxy or ""
    extra["_task_id"] = str(task_id or "")
    extra["_subtask_id"] = str(subtask_id or "")
    extra["_registration_attempt_id"] = (
        f"{task_id}:{subtask_id}" if task_id and subtask_id else ""
    )
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=platform_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=mailbox_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    if hasattr(platform, "set_cancel_checker"):
        platform.set_cancel_checker(logger.is_cancel_requested)
    elif hasattr(platform, "_cancel_check_fn"):
        platform._cancel_check_fn = logger.is_cancel_requested
    return platform


class _FixedMailbox:
    def __init__(self, mailbox, mailbox_account):
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account

    def get_email(self):
        return self._mailbox_account

    def set_cancel_checker(self, checker):
        if hasattr(self._mailbox, "set_cancel_checker"):
            self._mailbox.set_cancel_checker(checker)

    def get_current_ids(self, account):
        return self._mailbox.get_current_ids(account)

    def wait_for_code(self, account, **kwargs):
        return self._mailbox.wait_for_code(account, **kwargs)

    def wait_for_link(self, account, **kwargs):
        return self._mailbox.wait_for_link(account, **kwargs)


def _run_single_account_check(
    account_id: int,
    logger: TaskLogger | None = None,
    *,
    proxy: str | None = None,
    disable_proxy_pool: bool = False,
) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(
            config=RegisterConfig(
                proxy=proxy,
                extra={"disable_proxy_pool": bool(disable_proxy_pool)},
            )
        )
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}
            if hasattr(plugin, "get_last_check_overview"):
                summary_updates.update(plugin.get_last_check_overview() or {})
            lifecycle_status = None
            if valid:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    if not logger.mark_running():
        if logger._status() == TASK_STATUS_CANCEL_REQUESTED:
            logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
        TASK_TYPE_CODEX_OAUTH_BATCH: _execute_codex_oauth_batch_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    try:
        handler(payload, logger)
    finally:
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
) -> str | None:
    normalized_explicit_proxy = str(explicit_proxy or "").strip() or None
    if str(platform_name or "").strip().lower() == "chatgpt":
        # ChatGPT 只使用本次任务显式传入的动态 IP；留空时固定本地直连，
        # 不从全局代理池回退。
        return normalized_explicit_proxy
    return normalized_explicit_proxy or proxy_getter()


def _registration_platform_proxy(payload: dict[str, Any], proxy_getter: Callable[[], str | None]) -> tuple[str | None, str]:
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    mode = normalize_proxy_mode(
        str(payload.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_MANUAL if explicit_proxy else PROXY_MODE_DIRECT,
    )
    proxy = resolve_proxy_by_mode(
        mode,
        manual_proxy=str(payload.get("platform_proxy_value") or "").strip() or explicit_proxy,
        proxy_getter=proxy_getter,
    )
    return proxy, mode


def _registration_mailbox_proxy(
    payload: dict[str, Any],
    *,
    platform_proxy: str | None,
    proxy_getter: Callable[[], str | None],
) -> tuple[str | None, str]:
    legacy_explicit_proxy = str(payload.get("proxy") or "").strip() or None
    default_mode = PROXY_MODE_FOLLOW_PLATFORM if legacy_explicit_proxy and not payload.get("mailbox_proxy_mode") else PROXY_MODE_DIRECT
    mode = normalize_proxy_mode(str(payload.get("mailbox_proxy_mode") or "").strip(), default=default_mode)
    proxy = resolve_proxy_by_mode(
        mode,
        manual_proxy=str(payload.get("mailbox_proxy_value") or "").strip(),
        follow_proxy=platform_proxy,
        proxy_getter=proxy_getter,
    )
    return proxy, mode


def _check_task_proxy(payload: dict[str, Any], proxy_getter: Callable[[], str | None]) -> tuple[str | None, str, bool]:
    mode = normalize_proxy_mode(
        str(payload.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_PROXY_SERVICE,
    )
    proxy = resolve_proxy_by_mode(
        mode,
        manual_proxy=str(payload.get("platform_proxy_value") or payload.get("proxy") or "").strip(),
        proxy_getter=proxy_getter,
    )
    return proxy, mode, mode == PROXY_MODE_DIRECT


def _registration_concurrency(requested: Any, count: int) -> int:
    return min(
        max(int(requested or 1), 1),
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )


def _codex_oauth_batch_concurrency(requested: Any, count: int) -> int:
    try:
        value = int(requested or 1)
    except Exception:
        value = 1
    return min(
        max(value, 1),
        max(int(count or 1), 1),
        MAX_CODEX_OAUTH_BATCH_CONCURRENCY,
    )


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except Exception:
        result = default
    result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


def _upload_registered_chatgpt_account_to_sub2api(
    account_id: int,
    *,
    sub2api_url: str,
    api_key: str,
) -> dict:
    """Send exactly one newly saved Agent Identity to Sub2API."""
    from application.account_exports import AccountExportsService
    from domain.accounts import AccountExportSelection

    return AccountExportsService().upload_chatgpt_agent_identity_to_sub2api(
        AccountExportSelection(
            platform="chatgpt",
            ids=[int(account_id)],
            select_all=False,
        ),
        sub2api_url=sub2api_url,
        api_key=api_key,
    )


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    count = max(int(payload.get("count", 1) or 1), 1)
    concurrency = _registration_concurrency(payload.get("concurrency", 1), count)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    extra = dict(payload.get("extra") or {})
    extra["_log_fn"] = logger.log
    task_id = str(getattr(logger, "task_id", "") or "")
    sub2api_upload_enabled = bool(extra.get("auto_upload_sub2api_agent_identity"))
    sub2api_upload_config = (
        get_register_sub2api_upload_config(task_id)
        if sub2api_upload_enabled and task_id
        else None
    )
    platform_proxy, platform_proxy_mode = _registration_platform_proxy(payload, proxy_pool.get_next)
    mailbox_proxy, mailbox_proxy_mode = _registration_mailbox_proxy(
        payload,
        platform_proxy=platform_proxy,
        proxy_getter=proxy_pool.get_next,
    )
    extra["mailbox_proxy"] = mailbox_proxy or ""
    payload["extra"] = extra

    logger.set_progress(0, count)
    if sub2api_upload_enabled and not sub2api_upload_config:
        logger.log(
            "已启用 Sub2API 自动上传，但当前任务缺少临时上传凭据；注册账号不会上传",
            level="warning",
        )
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            fixed_mailbox_address_id = str(extra.get("mailbox_address_id") or "").strip()
            if fixed_mailbox_address_id:
                from core.mailbox_store import MailboxStore

                mailbox, mailbox_account, mailbox_context = MailboxStore().resolve_mailbox_for_address(
                    mailbox_address_id=fixed_mailbox_address_id,
                    proxy=mailbox_proxy,
                    extra=extra,
                )
                provider = str(((mailbox_context.get("account") or {}).get("provider")) or "").strip()
                if provider:
                    extra["mail_provider"] = provider
                    payload["extra"] = extra
                fixed_email = str(getattr(mailbox_account, "email", "") or "").strip()
                if fixed_email:
                    payload["email"] = fixed_email
                    email = fixed_email
                shared_mailbox = _FixedMailbox(mailbox, mailbox_account)
                logger.log(f"使用选中邮箱注册: {fixed_email or fixed_mailbox_address_id}")
            else:
                if not extra.get("mail_provider"):
                    from infrastructure.provider_settings_repository import ProviderSettingsRepository

                    extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
                shared_mailbox = create_mailbox(
                    provider=extra.get("mail_provider", ""),
                    extra=extra,
                    proxy=mailbox_proxy,
                )
                if hasattr(shared_mailbox, "set_cancel_checker"):
                    shared_mailbox.set_cancel_checker(logger.is_cancel_requested)
                if (
                    count > 1
                    and str(extra.get("mail_provider") or "").strip() == "hotmail007"
                    and hasattr(shared_mailbox, "configure_prefetch")
                ):
                    buy_concurrency = _bounded_int(
                        extra.get("hotmail007_buy_concurrency"),
                        concurrency,
                        minimum=1,
                        maximum=concurrency,
                    )
                    queue_max = _bounded_int(
                        extra.get("hotmail007_prefetch_queue_max"),
                        concurrency * 2,
                        minimum=1,
                        maximum=count,
                    )
                    shared_mailbox.configure_prefetch(
                        total_needed=count,
                        buy_concurrency=buy_concurrency,
                        queue_max=queue_max,
                    )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    upload_lock = threading.Lock()
    uploaded_count = 0
    upload_failures: list[str] = []

    def _do_one(index: int) -> dict[str, Any] | str:
        nonlocal uploaded_count
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        subtask_id = f"worker_{index + 1}"
        logger.set_subtask(subtask_id, f"Worker {index + 1}")
        platform = None
        allocation_id = ""
        allocation_succeeded = False
        failure_reason = ""
        try:
            platform = _build_platform_instance(
                platform_name,
                payload,
                logger,
                platform_proxy=platform_proxy,
                mailbox_proxy=mailbox_proxy,
                shared_mailbox=shared_mailbox,
                task_id=task_id,
                subtask_id=subtask_id,
            )
            logger.log(f"开始注册第 {index + 1}/{count} 个账号")
            logger.log(
                f"ChatGPT/Codex 代理: {mask_proxy_url(platform_proxy) if platform_proxy else '直连'}"
                f"（{platform_proxy_mode}）"
            )
            logger.log(
                f"邮箱 API 代理: {mask_proxy_url(mailbox_proxy) if mailbox_proxy else '直连'}"
                f"（{mailbox_proxy_mode}）"
            )
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            account = platform.register(email=email, password=password)
            from core.mailbox_lifecycle import MailboxAllocationLifecycle

            allocation_id = MailboxAllocationLifecycle.allocation_id_from_account(account)
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            with Session(engine) as registration_session:
                saved_account = save_account(account, session=registration_session, commit=False)
                saved_account_id = int(saved_account.id)
                if allocation_id:
                    MailboxAllocationLifecycle().succeed_in_session(
                        registration_session,
                        allocation_id,
                        account_id=saved_account_id,
                        account_email=account.email,
                        platform=account.platform,
                    )
                registration_session.commit()
            if allocation_id:
                allocation_succeeded = True
            if platform_proxy and platform_proxy_mode == PROXY_MODE_PROXY_SERVICE:
                proxy_pool.report_success(platform_proxy)
            if mailbox_proxy and mailbox_proxy_mode == PROXY_MODE_PROXY_SERVICE and mailbox_proxy != platform_proxy:
                proxy_pool.report_success(mailbox_proxy)
            post_codex_oauth = dict((account.extra or {}).get("post_codex_oauth") or {})
            auto_codex_oauth_enabled = str(extra.get("auto_codex_oauth_after_register") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "是",
                "开启",
                "启用",
            }
            if auto_codex_oauth_enabled and not post_codex_oauth:
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                logger.log(f"{account.email} 注册后执行 Codex OAuth 授权")
                try:
                    setattr(account, "id", saved_account_id)
                except Exception:
                    pass
                browser_mode = str(extra.get("codex_oauth_browser_mode") or "").strip().lower()
                keep_browser_open = str(extra.get("codex_oauth_keep_browser_open") or "").strip().lower()
                codex_action = platform.execute_action(
                    "codex_oauth_authorize",
                    account,
                    {
                        "browser_mode": browser_mode or ("headed" if str(payload.get("executor_type") or "") == "headed" else "headless"),
                        "keep_browser_open": keep_browser_open,
                    },
                )
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                if codex_action.get("ok") and isinstance(codex_action.get("data"), dict):
                    account.extra = {**dict(account.extra or {}), **codex_action["data"]}
                    save_account(account)
                    post_codex_oauth = {"ok": True}
                else:
                    post_codex_oauth = {"ok": False, "error": str(codex_action.get("error") or codex_action.get("data") or "unknown")}
            if post_codex_oauth:
                if post_codex_oauth.get("ok"):
                    logger.log(f"{account.email} 的 Codex OAuth 授权已完成")
                else:
                    logger.log(
                        f"{account.email} 注册成功，但 Codex OAuth 授权失败: {post_codex_oauth.get('error') or 'unknown'}",
                        level="warning",
                    )
            if sub2api_upload_config:
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                logger.log(f"正在上传 {account.email} 的 Agent Identity 到 Sub2API")
                try:
                    _upload_registered_chatgpt_account_to_sub2api(
                        saved_account_id,
                        sub2api_url=sub2api_upload_config["sub2api_url"],
                        api_key=sub2api_upload_config["api_key"],
                    )
                except Exception as upload_exc:
                    upload_error = str(upload_exc)
                    with upload_lock:
                        upload_failures.append(upload_error)
                    logger.log(
                        f"{account.email} 的 Agent Identity 上传失败：{upload_error}",
                        level="error",
                    )
                else:
                    with upload_lock:
                        uploaded_count += 1
                    logger.log(f"{account.email} 的 Agent Identity 已上传到 Sub2API")
            logger.record_success()
            logger.log(f"注册成功: {account.email}")
            item = {
                "account_id": saved_account_id,
                "email": account.email,
            }
            if auto_codex_oauth_enabled or post_codex_oauth:
                item["codex_oauth"] = post_codex_oauth or {"ok": False, "skipped": True}
            return item
        except Exception as exc:
            failure_reason = str(exc)
            if logger.is_cancel_requested() or str(exc) == "任务已取消":
                return "__cancel_requested__"
            if platform_proxy and platform_proxy_mode == PROXY_MODE_PROXY_SERVICE:
                proxy_pool.report_fail(platform_proxy)
            if mailbox_proxy and mailbox_proxy_mode == PROXY_MODE_PROXY_SERVICE and mailbox_proxy != platform_proxy:
                proxy_pool.report_fail(mailbox_proxy)
            error = str(exc)
            logger.record_error(error)
            logger.log(f"注册失败: {error}", level="error")
            return error
        finally:
            if platform is not None and not allocation_id:
                from core.mailbox_lifecycle import MailboxAllocationLifecycle

                allocation_id = MailboxAllocationLifecycle.allocation_id_from_platform(platform)
            if allocation_id and not allocation_succeeded:
                from core.mailbox_lifecycle import (
                    ALLOCATION_CANCELLED,
                    ALLOCATION_FAILED,
                    MailboxAllocationLifecycle,
                )

                cancelled = logger.is_cancel_requested() or failure_reason == "任务已取消"
                MailboxAllocationLifecycle().release(
                    allocation_id,
                    outcome=ALLOCATION_CANCELLED if cancelled else ALLOCATION_FAILED,
                    reason=failure_reason or ("任务已取消" if cancelled else "注册未成功"),
                )
            logger.clear_subtask()

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    pool: ThreadPoolExecutor | None = None
    cancel_pool = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = set()
        next_index = 0
        while next_index < count and len(pending) < concurrency and not logger.is_cancel_requested():
            pending.add(pool.submit(_do_one, next_index))
            next_index += 1
        while pending:
            if logger.is_cancel_requested():
                cancel_pool = True
                for future in pending:
                    future.cancel()
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                completed += 1
                if isinstance(result, dict):
                    success += 1
                    registered_accounts.append(result)
                elif result != "__cancel_requested__":
                    errors.append(str(result))
                logger.set_progress(completed, count)
                if logger.is_cancel_requested():
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
            while next_index < count and len(pending) < concurrency and not logger.is_cancel_requested():
                pending.add(pool.submit(_do_one, next_index))
                next_index += 1
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    finally:
        if pool is not None:
            pool.shutdown(wait=not cancel_pool, cancel_futures=cancel_pool)
        if hasattr(shared_mailbox, "shutdown_prefetch"):
            try:
                shared_mailbox.shutdown_prefetch()
            except Exception as exc:
                logger.log(f"邮箱预取停止失败: {exc}", level="warning")

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in registered_accounts],
        "accounts": registered_accounts,
        "auto_upload_sub2api_agent_identity": sub2api_upload_enabled,
    }
    if sub2api_upload_enabled:
        result_data["sub2api_agent_identity_upload"] = {
            "submitted": uploaded_count,
            "failed": len(upload_failures),
            "errors": upload_failures,
        }
    logger.set_result_data(result_data)
    logger.log(f"完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
    )
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_codex_oauth_batch_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform") or "chatgpt")
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]
    params = dict(payload.get("params") or {})
    if not params.get("browser_mode"):
        params["browser_mode"] = "headless"
    if not params.get("keep_browser_open"):
        params["keep_browser_open"] = "false"
    concurrency = _codex_oauth_batch_concurrency(payload.get("concurrency", 1), len(account_ids))

    with Session(engine) as session:
        records = session.exec(
            select(AccountModel)
            .where(AccountModel.id.in_(account_ids))
            .where(AccountModel.platform == platform)
        ).all() if account_ids else []
        by_id = {int(item.id or 0): item for item in records}

    accounts = [by_id[item] for item in account_ids if item in by_id]
    total = len(accounts)
    logger.set_progress(0, total)
    logger.log(f"Codex OAuth 批量授权: {total} 个账号，并发 {concurrency}")
    if total == 0:
        logger.set_result_data({"success": 0, "fail": 0, "accounts": []})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    def _do_one(model: AccountModel) -> dict[str, Any]:
        account_id = int(model.id or 0)
        email = str(model.email or "")
        logger.set_subtask(f"account_{account_id}", email or f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"account_id": account_id, "email": email, "cancelled": True}
            logger.log(f"{email}: 开始 Codex OAuth 授权")
            runtime = PlatformRuntime()
            result = runtime.execute_action(
                type("Command", (), {
                    "platform": platform,
                    "account_id": account_id,
                    "action_id": "codex_oauth_authorize",
                    "params": params,
                })(),
                log_fn=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
                return {"account_id": account_id, "email": email, "cancelled": True}
            if not result.ok:
                error = str(result.error or "Codex OAuth 授权失败")
                logger.record_error(error)
                logger.log(f"{email}: Codex OAuth 授权失败: {error}", level="error")
                return {"account_id": account_id, "email": email, "ok": False, "error": error}
            logger.record_success()
            logger.log(f"{email}: Codex OAuth 授权完成")
            return {"account_id": account_id, "email": email, "ok": True, "data": result.data}
        except Exception as exc:
            error = str(exc)
            logger.record_error(error)
            logger.log(f"{email}: Codex OAuth 授权异常: {error}", level="error")
            return {"account_id": account_id, "email": email, "ok": False, "error": error}
        finally:
            logger.clear_subtask()

    completed = 0
    success = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    pool: ThreadPoolExecutor | None = None
    cancel_pool = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = set()
        next_index = 0
        while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
            pending.add(pool.submit(_do_one, accounts[next_index]))
            next_index += 1
        while pending:
            if logger.is_cancel_requested():
                cancel_pool = True
                for future in pending:
                    future.cancel()
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                if result.get("cancelled"):
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
                completed += 1
                if result.get("ok"):
                    success += 1
                else:
                    errors.append(str(result.get("error") or "unknown"))
                results.append(result)
                logger.set_progress(completed, total)
                if logger.is_cancel_requested():
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
            while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
                pending.add(pool.submit(_do_one, accounts[next_index]))
                next_index += 1
    finally:
        if pool is not None:
            pool.shutdown(wait=not cancel_pool, cancel_futures=cancel_pool)

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in results if item.get("ok")],
        "accounts": results,
    }
    logger.set_result_data(result_data)
    logger.log(f"Codex OAuth 批量授权完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]

    with Session(engine) as session:
        q = select(AccountModel)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        elif "account_ids" in payload:
            q = q.where(AccountModel.id == -1)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        if account_ids or "account_ids" in payload:
            accounts = session.exec(q).all()
        else:
            accounts = session.exec(q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    results = {"valid": 0, "invalid": 0, "error": 0}
    completed = 0
    task_proxy, proxy_mode, disable_proxy_pool = _check_task_proxy(payload, proxy_pool.get_next)
    logger.log(
        f"账号检测代理: {mask_proxy_url(task_proxy) if task_proxy else '直连'}"
        f"（{proxy_mode}）"
    )
    for model in accounts:
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, _ = _run_single_account_check(
                int(model.id or 0),
                logger,
                proxy=task_proxy,
                disable_proxy_pool=disable_proxy_pool,
            )
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)
