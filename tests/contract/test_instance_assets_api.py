import os
import shutil
import subprocess

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure import instance_config

from tests.e2e.local_auth_server import SyntheticSettings

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="#ffaa00"/></svg>'


@pytest.fixture
def asset_api(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    assets = tmp_path / "instance-assets"
    assets.mkdir()
    (assets / "logo.svg").write_text(SVG, encoding="utf-8")
    settings = SyntheticSettings(
        _env_file=None,
        instance_config_path=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-instance-asset-key-" * 2,
        local_test=True,
        allowed_hosts=["testserver"],
        logo_asset="logo.svg",
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    try:
        with TestClient(app) as client:
            yield client, settings, assets
    finally:
        app.state.commerce.db.engine.dispose()


def test_instance_asset_get_returns_only_configured_safe_role(asset_api):
    client, _, _ = asset_api

    response = client.get("/api/v1/instance/assets/logo")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.decode() == SVG
    assert client.post("/api/v1/instance/assets/logo").status_code == 405
    assert client.get("/api/v1/instance/assets/favicon").status_code == 404
    data = client.get("/api/v1/public-config").json()["instance"]
    assert data["logo_url"] == "/api/v1/instance/assets/logo"
    assert data["favicon_url"] is None
    assert "instance-assets" not in str(data)


@pytest.mark.parametrize(
    "path",
    ["unknown", "logo.svg", "../config/instance.json", "%2e%2e%2fconfig%2finstance.json", "logo/extra"],
)
def test_instance_asset_endpoint_cannot_select_arbitrary_path(asset_api, path):
    client, _, _ = asset_api
    assert client.get("/api/v1/instance/assets/" + path).status_code == 404


@pytest.mark.parametrize(
    "content",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///private.txt">]><svg>&x;</svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.test/track"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="u\\72l(//example.test/paint.svg#x)"/></svg>',
        "invalid-image-content",
    ],
)
def test_unsafe_or_malformed_svg_is_unavailable_and_not_advertised(asset_api, content):
    client, _, assets = asset_api
    (assets / "logo.svg").write_text(content, encoding="utf-8")
    assert client.get("/api/v1/instance/assets/logo").status_code == 404
    assert client.get("/api/v1/public-config").json()["instance"]["logo_url"] is None


def test_missing_optional_asset_has_null_projection(asset_api):
    client, _, assets = asset_api
    (assets / "logo.svg").unlink()
    assert client.get("/api/v1/public-config").json()["instance"]["logo_url"] is None
    assert client.get("/api/v1/instance/assets/logo").status_code == 404


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32", "utf-32-le"])
@pytest.mark.parametrize(
    "prefix",
    [
        '<?xml-stylesheet href="https://example.test/track.css"?>',
        '<!DOCTYPE svg [<!ENTITY unused "hidden">]>',
    ],
)
def test_encoded_svg_processing_instructions_and_dtd_are_never_served(asset_api, encoding, prefix):
    client, _, assets = asset_api
    (assets / "logo.svg").write_bytes((prefix + SVG).encode(encoding))
    assert client.get("/api/v1/instance/assets/logo").status_code == 404
    assert client.get("/api/v1/public-config").json()["instance"]["logo_url"] is None


def test_real_windows_junction_cannot_expose_even_in_root_asset_directory(asset_api, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction behavior")
    client, settings, assets = asset_api
    original = assets / "original"
    original.mkdir()
    (original / "logo.svg").write_text(SVG, encoding="utf-8")
    link = assets / "linked"
    assert original.resolve().is_relative_to(tmp_path.resolve())
    assert link.absolute().is_relative_to(tmp_path.resolve())
    result = subprocess.run(  # noqa: S603 -- fixed local junction command; both paths inside owned temporary fixture
        [shutil.which("cmd.exe"), "/c", "mklink", "/J", str(link), str(original)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, "Synthetic local junction creation failed"
    try:
        assert link.is_junction()
        object.__setattr__(settings.instance, "logo_asset", "linked/logo.svg")
        assert client.get("/api/v1/instance/assets/logo").status_code == 404
        assert client.get("/api/v1/public-config").json()["instance"]["logo_url"] is None
        assert (original / "logo.svg").read_text(encoding="utf-8") == SVG
    finally:
        link.rmdir()
