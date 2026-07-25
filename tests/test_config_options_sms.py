from __future__ import annotations


def test_config_options_exposes_smsbower_catalog(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    data = resp.json()

    assert "sms_providers" in data
    assert "sms_settings" in data
    assert "sms_drivers" in data
    smsbower = next(item for item in data["sms_providers"] if item["value"] == "smsbower")
    field_keys = {item["key"] for item in smsbower["fields"]}
    fields = {item["key"]: item for item in smsbower["fields"]}

    assert "smsbower_max_price" in field_keys
    assert "smsbower_min_price" in field_keys
    assert "smsbower_buy_max_attempts" in field_keys
    assert "smsbower_buy_retry_interval" in field_keys
    assert "smsbower_ref" not in field_keys
    assert fields["smsbower_default_country"]["label"] == "默认国家 ID"
    assert fields["smsbower_buy_max_attempts"]["default_value"] == "20"
    assert fields["smsbower_buy_retry_interval"]["default_value"] == "3"


def test_sms_provider_test_fetches_balance_without_purchase(client, monkeypatch):
    from core.smsbower_sms import SMSBowerClient

    calls = []

    def fake_get_balance(self):
        calls.append(self.api_key)
        return 12.34

    monkeypatch.setattr(SMSBowerClient, "get_balance", fake_get_balance)

    resp = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "sms",
            "provider_key": "smsbower",
            "config": {},
            "auth": {"smsbower_api_key": "secret-key"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["balance"] == 12.34
    assert "账户余额：12.34" in data["message"]
    assert calls == ["secret-key"]
