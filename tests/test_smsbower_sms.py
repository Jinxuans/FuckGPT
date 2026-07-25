from __future__ import annotations

import json

import pytest

from core.smsbower_sms import SMSBowerClient, SMSBowerError
from infrastructure.provider_definitions_repository import (
    ProviderDefinitionsRepository,
    SUPPORTED_SMS_PROVIDER_KEYS,
)
from providers.registry import create_provider, list_registered, load_all


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure url=https://smsbower.page/stubs/handler_api.php?api_key=secret-key")


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.payloads:
            raise RuntimeError("no fake payloads left")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def test_smsbower_get_number_parses_activation_and_sends_documented_params():
    logs = []
    session = FakeSession(["ACCESS_NUMBER:12345:+15551234567"])
    client = SMSBowerClient(api_key="secret-key", session=session, log_fn=logs.append)

    activation = client.get_number(
        "go",
        "0",
        max_price="0.5",
        min_price="0.1",
        provider_ids="1,2",
        except_provider_ids="3",
        phone_exception="+1555000",
        user_id="user-1",
    )

    assert activation.activation_id == "12345"
    assert activation.phone_number == "+15551234567"
    assert activation.provider == "smsbower"
    assert activation.service == "go"
    assert activation.country == "0"
    assert session.calls[0][0] == "https://smsbower.page/stubs/handler_api.php"
    assert session.calls[0][1]["params"] == {
        "api_key": "secret-key",
        "action": "getNumber",
        "service": "go",
        "country": "0",
        "maxPrice": "0.5",
        "minPrice": "0.1",
        "providerIds": "1,2",
        "exceptProviderIds": "3",
        "phoneException": "+1555000",
        "ref": "531498",
        "userID": "user-1",
    }
    assert any("买号成功" in item for item in logs)
    assert not any("secret-key" in item for item in logs)


def test_smsbower_get_number_v2_parses_json_activation():
    payload = json.dumps(
        {
            "activationId": "777",
            "phoneNumber": 15551234567,
            "activationCost": "0.34",
            "countryCode": "0",
            "activationOperator": "any",
        }
    )
    session = FakeSession([payload])
    client = SMSBowerClient(api_key="secret-key", session=session)

    activation = client.get_number_v2("go", "0")

    assert activation.activation_id == "777"
    assert activation.phone_number == "15551234567"
    assert activation.metadata["activationCost"] == "0.34"
    assert session.calls[0][1]["params"]["action"] == "getNumberV2"


def test_smsbower_configured_price_bounds_are_used_and_can_be_overridden():
    session = FakeSession(["ACCESS_NUMBER:12345:+15551234567", "ACCESS_NUMBER:67890:+15557654321"])
    client = SMSBowerClient.from_config(
        {
            "smsbower_api_key": "secret-key",
            "smsbower_default_service": "go",
            "smsbower_default_country": "0",
            "smsbower_max_price": "0.50",
            "smsbower_min_price": "0.10",
        }
    )
    client.session = session

    client.get_number()
    client.get_number(max_price="0.40", min_price="0.20")

    assert session.calls[0][1]["params"]["maxPrice"] == "0.50"
    assert session.calls[0][1]["params"]["minPrice"] == "0.10"
    assert session.calls[0][1]["params"]["ref"] == "531498"
    assert session.calls[1][1]["params"]["maxPrice"] == "0.40"
    assert session.calls[1][1]["params"]["minPrice"] == "0.20"
    assert session.calls[1][1]["params"]["ref"] == "531498"


def test_smsbower_status_and_set_status_responses_are_normalized():
    session = FakeSession(
        [
            "STATUS_WAIT_CODE",
            "STATUS_WAIT_RETRY:111111",
            "STATUS_OK:654321",
            "ACCESS_CANCEL",
            "ACCESS_ACTIVATION",
        ]
    )
    client = SMSBowerClient(api_key="secret-key", session=session)

    assert client.get_status("123").status == "waiting"

    retry = client.get_status("123")
    assert retry.status == "waiting_retry"
    assert retry.last_code == "111111"

    ok = client.get_status("123")
    assert ok.status == "ok"
    assert ok.code == "654321"

    assert client.cancel("123") == "ACCESS_CANCEL"
    assert client.finish("123") == "ACCESS_ACTIVATION"
    assert session.calls[-2][1]["params"] == {
        "api_key": "secret-key",
        "action": "setStatus",
        "id": "123",
        "status": 8,
    }
    assert session.calls[-1][1]["params"]["status"] == 6


def test_smsbower_wait_for_code_polls_until_status_ok():
    session = FakeSession(["STATUS_WAIT_CODE", "STATUS_OK:222333"])
    client = SMSBowerClient(api_key="secret-key", session=session, poll_interval=0)

    assert client.wait_for_code("123", timeout=1) == "222333"
    assert len(session.calls) == 2


def test_smsbower_balance_supports_plain_and_prefixed_response():
    plain = SMSBowerClient(api_key="secret-key", session=FakeSession(["12.34"]))
    prefixed = SMSBowerClient(api_key="secret-key", session=FakeSession(["ACCESS_BALANCE:56.78"]))

    assert plain.get_balance() == 12.34
    assert prefixed.get_balance() == 56.78


def test_smsbower_errors_redact_api_key():
    session = FakeSession(["BAD_KEY"])
    client = SMSBowerClient(api_key="secret-key", session=session)

    with pytest.raises(SMSBowerError) as excinfo:
        client.get_number("go", "0")

    assert "BAD_KEY" in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)

    http_session = FakeSession([])
    http_session.payloads = [FakeResponse("ignored", status_code=500)]
    client = SMSBowerClient(api_key="secret-key", session=http_session)

    with pytest.raises(SMSBowerError) as http_excinfo:
        client.get_status("123")

    assert "secret-key" not in str(http_excinfo.value)
    assert "api_key=***" in str(http_excinfo.value)


def test_smsbower_is_exposed_in_provider_catalog_and_registry():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()
    load_all()

    definitions = repository.list_by_type("sms", enabled_only=True)
    drivers = repository.list_driver_templates("sms")

    assert {item.provider_key for item in definitions} == set(SUPPORTED_SMS_PROVIDER_KEYS)
    assert {item["driver_type"] for item in drivers} == {"smsbower"}
    assert "smsbower" in list_registered("sms")
    provider = create_provider("sms", "smsbower", {"smsbower_api_key": "secret-key"})
    assert isinstance(provider, SMSBowerClient)
