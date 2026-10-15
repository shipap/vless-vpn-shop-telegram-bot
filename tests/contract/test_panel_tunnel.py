import ssl

import httpx
import pytest


def transport(port=7443, delegate=None, verify=True):
    from sunrise_infrastructure.panel_tunnel import PanelTunnelTransport

    return PanelTunnelTransport("panel.example.test", port, verify=verify, delegate=delegate)


def test_panel_tunnel_preserves_tls_identity_and_request():
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"response": {"healthy": True}})

    with httpx.Client(transport=transport(delegate=httpx.MockTransport(receive)), trust_env=False) as client:
        response = client.get(
            "https://panel.example.test/api/system/health", headers={"Authorization": "Bearer synthetic"}
        )
    assert response.status_code == 200
    request = seen[0]
    assert request.url.host == "127.0.0.1" and request.url.port == 7443
    assert request.headers["host"] == "panel.example.test"
    assert request.extensions["sni_hostname"] == "panel.example.test"
    assert request.headers["authorization"] == "Bearer synthetic"
    assert request.url.path == "/api/system/health"


@pytest.mark.parametrize(
    "url",
    [
        "http://panel.example.test/api/users",
        "https://other.example.test/api/users",
        "https://panel.example.test:444/api/users",
        "https://user:password@panel.example.test/api/users",
    ],
)
def test_panel_tunnel_rejects_other_targets_without_sending(url):
    def forbidden(request):
        pytest.fail("A denied target reached the transport")

    with httpx.Client(
        transport=transport(delegate=httpx.MockTransport(forbidden)), trust_env=False
    ) as client:
        with pytest.raises(ValueError, match="panel_tunnel_target_denied"):
            client.get(url)


@pytest.mark.parametrize("port", [0, 443, 65536, True])
def test_panel_tunnel_rejects_invalid_local_port(port):
    with pytest.raises(ValueError, match="panel_tunnel_configuration_denied"):
        transport(port)


def test_panel_tunnel_never_disables_certificate_verification():
    with pytest.raises(ValueError, match="panel_tunnel_tls_required"):
        transport(verify=False)
    context = ssl.create_default_context()
    context.check_hostname = False
    with pytest.raises(ValueError, match="panel_tunnel_tls_required"):
        transport(verify=context)


def test_empty_optional_tunnel_setting_keeps_direct_transport_default():
    from sunrise_infrastructure.settings import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql://synthetic@127.0.0.1:55439/sunrise_test",
        secret_key="s" * 32,
        panel_tunnel_port="",
    )
    assert settings.panel_tunnel_port is None
