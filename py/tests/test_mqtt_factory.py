"""Tests for the MQTT client factory (make_client) and config TLS/auth fields."""
from snr_sweep.config import SweepConfig, load_config
from snr_sweep.mqtt import make_client


def test_make_client_plain():
    c = make_client("t-plain")
    # No TLS, no credentials: connecting would be plain TCP.
    assert c is not None


def test_make_client_tls_auth():
    c = make_client("t-tls", username="meshcore", password="secret", tls=True)
    # Username/password applied to the paho client.
    assert c._username is not None
    assert c._username.decode() == "meshcore"
    assert c._password is not None
    assert c._password.decode() == "secret"
    # TLS set: paho stores _ssl=True and a real ssl.SSLContext.
    assert c._ssl is True
    assert c._ssl_context is not None


def test_config_fields_default_off():
    cfg = SweepConfig()
    assert cfg.mqtt_tls is False
    assert cfg.mqtt_user is None
    assert cfg.mqtt_pass is None


def test_load_config_reads_tls_and_creds(tmp_path):
    p = tmp_path / "pub.toml"
    p.write_text(
        "mqtt_host = \"mqtt.cisien.com\"\n"
        "mqtt_port = 8883\n"
        "mqtt_user = \"meshcore\"\n"
        "mqtt_pass = \"pw\"\n"
        "mqtt_tls = true\n"
    )
    cfg = load_config(str(p))
    assert cfg.mqtt_host == "mqtt.cisien.com"
    assert cfg.mqtt_port == 8883
    assert cfg.mqtt_user == "meshcore"
    assert cfg.mqtt_pass == "pw"
    assert cfg.mqtt_tls is True
