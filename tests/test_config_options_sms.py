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


def test_config_options_exposes_proxy_catalog(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    data = resp.json()

    assert "proxy_providers" in data
    assert "proxy_settings" in data
    assert "proxy_drivers" in data
    provider_keys = {item["value"] for item in data["proxy_providers"]}
    assert {"api_extract", "rotating_gateway"}.issubset(provider_keys)


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


def test_proxy_provider_test_masks_credentials_and_checks_origin(client, monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"origin": "203.0.113.10"}

    calls = []

    def fake_get(url, *, proxies, timeout):
        calls.append({"url": url, "proxies": proxies, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    resp = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "proxy",
            "provider_key": "rotating_gateway",
            "config": {
                "proxy_gateway_url": "socks5://user:pass@gate.example.com:1080",
            },
            "auth": {},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["origin"] == "203.0.113.10"
    assert data["proxy"] == "socks5://***:***@gate.example.com:1080"
    assert "user:pass" not in data["message"]
    assert calls == [
        {
            "url": "https://httpbin.org/ip",
            "proxies": {
                "http": "socks5://user:pass@gate.example.com:1080",
                "https": "socks5://user:pass@gate.example.com:1080",
            },
            "timeout": 12,
        }
    ]
