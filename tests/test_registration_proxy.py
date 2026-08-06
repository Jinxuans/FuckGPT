from __future__ import annotations

import pytest

from application.tasks import _resolve_registration_proxy_for_platform
from core.base_identity import IdentityMaterial
from core.base_platform import BasePlatform, RegisterConfig
from core.registration import ProtocolMailboxAdapter, RegistrationResult


class _RetryFixturePlatform(BasePlatform):
    name = ""
    display_name = "Retry Fixture"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, identity, *, fail: bool, contexts: list):
        self.fixture_identity = identity
        self.fail = fail
        self.contexts = contexts
        self.resolve_calls = 0
        super().__init__(
            RegisterConfig(
                executor_type="protocol",
                extra={"identity_provider": "mailbox"},
            )
        )

    def _resolve_identity(self, email=None, *, require_email=True):
        self.resolve_calls += 1
        self._last_identity = self.fixture_identity
        return self.fixture_identity

    def _make_random_password(self, length=16, charset=None):
        return "generated-password"

    def build_protocol_mailbox_adapter(self):
        def register_runner(_worker, ctx, _artifacts):
            self.contexts.append(ctx)
            if self.fail:
                raise RuntimeError("CONNECT tunnel failed, response 509")
            return {}

        return ProtocolMailboxAdapter(
            worker_builder=lambda _ctx, _artifacts: object(),
            register_runner=register_runner,
            result_mapper=lambda ctx, _raw: RegistrationResult(
                email=ctx.identity.email,
                password=ctx.password,
            ),
        )

    def check_valid(self, account):
        return True


def test_chatgpt_registration_uses_explicit_proxy_without_proxy_pool():
    calls = []
    proxy = _resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="http://explicit-proxy.example:8080",
        proxy_getter=lambda: calls.append("called") or "http://pool-proxy.example:8080",
    )
    assert proxy == "http://explicit-proxy.example:8080"
    assert calls == []


def test_chatgpt_registration_uses_local_network_when_proxy_is_blank():
    calls = []
    proxy = _resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="  ",
        proxy_getter=lambda: calls.append("called") or "http://pool-proxy.example:8080",
    )
    assert proxy is None
    assert calls == []


def test_proxy_retry_reuses_identity_allocation_and_generated_password():
    contexts = []
    identity = IdentityMaterial(
        email="fixture@example.test",
        mailbox_account=object(),
        metadata={"mailbox_allocation_id": "allocation-1"},
    )
    first = _RetryFixturePlatform(identity, fail=True, contexts=contexts)

    with pytest.raises(RuntimeError, match="CONNECT tunnel failed"):
        first.register()

    retry_state = first.export_registration_retry_state()
    second = _RetryFixturePlatform(identity, fail=False, contexts=contexts)
    second.import_registration_retry_state(retry_state)
    account = second.register()

    assert first.resolve_calls == 1
    assert second.resolve_calls == 0
    assert retry_state["identity"] is identity
    assert account.email == "fixture@example.test"
    assert account.password == "generated-password"
    assert [ctx.password for ctx in contexts] == ["generated-password", "generated-password"]
    assert [ctx.password_provided for ctx in contexts] == [False, False]
