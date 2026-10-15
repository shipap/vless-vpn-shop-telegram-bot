"""Генератор не запускает сервисы и выпускает только привязанный HTTPS runtime."""

import importlib.util
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from test_explicit_bootstrap_legal import database  # noqa: F401 -- nested shared fixture
from test_first_activation import staged_installation  # noqa: F401 -- shared real ORM synthetic fixture
from test_production_installer import installation  # noqa: F401 -- nested shared fixture

ROOT = Path(__file__).resolve().parents[2]


def renderer():
    source = ROOT / "scripts/render_production_runtime.py"
    assert source.is_file(), "Нет воспроизводимого HTTPS runtime-генератора"
    spec = importlib.util.spec_from_file_location("scripts.render_production_runtime", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "service.localhost", "0.0.0.0"])  # noqa: S104 -- denial fixture
def test_bridge_runtime_rejects_host_loopback_resources(host):
    with pytest.raises(ValueError):
        renderer().external_host(host)


@pytest.mark.parametrize("url", ["http://shop.example.test", "https://shop.example.test/path",
                                 "https://shop.example.test:8443", "https://user:pass@shop.example.test"])
def test_https_origins_cannot_inject_paths_credentials_or_unpublished_ports(url):
    with pytest.raises(ValueError):
        renderer().https_host(url)


def test_public_ingress_hosts_must_already_be_admitted_by_backend_configuration():
    module = renderer()
    settings = SimpleNamespace(database_url="postgresql+psycopg://app:synthetic@postgres/shop",
                               redis_url="redis://redis/0", landing_url="https://shop.example.test",
                               dashboard_url="https://cabinet.example.test", allowed_hosts=["shop.example.test"],
                               webhook_base_url="https://cabinet.example.test")
    with pytest.raises(ValueError, match="runtime_backend_hosts_required"):
        module.runtime_hosts(settings)
    settings.allowed_hosts += ["cabinet.example.test", "extra.example.test"]
    assert module.runtime_hosts(settings) == ("shop.example.test", "cabinet.example.test")


@pytest.mark.parametrize("origin", ["https://third.example.test", "https://shop.example.test",
                                    "http://cabinet.example.test", "https://cabinet.example.test/path"])
def test_callback_origin_must_be_served_by_the_dashboard_api(origin):
    settings = SimpleNamespace(database_url="postgresql+psycopg://app:synthetic@postgres/shop",
                               redis_url="redis://redis/0", landing_url="https://shop.example.test",
                               dashboard_url="https://cabinet.example.test",
                               allowed_hosts=["shop.example.test", "cabinet.example.test"],
                               webhook_base_url=origin)
    with pytest.raises(ValueError):
        renderer().runtime_hosts(settings)


def certificate_pair(tmp_path, *, names=("shop.example.test", "cabinet.example.test")):
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in names]), critical=False)
        .sign(key, algorithm=None)
    )
    cert_file, key_file = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key_file.chmod(0o600)
    return cert_file, key_file


def test_tls_validation_requires_matching_key_and_both_real_configured_hosts(tmp_path):
    module = renderer()
    cert, key = certificate_pair(tmp_path)
    proof = module.validate_tls(cert, key, ["shop.example.test", "cabinet.example.test"])
    assert proof["validation"] == "local_key_dates_san_only"
    assert proof["public_trust_or_live_https_verified"] is False
    with pytest.raises(ValueError):
        module.validate_tls(cert, key, ["foreign.example.test"])
    other = tmp_path / "other"
    other.mkdir()
    _, wrong_key = certificate_pair(other)
    with pytest.raises(ValueError):
        module.validate_tls(cert, wrong_key, ["shop.example.test"])


def test_nginx_configuration_encodes_host_api_admin_and_secret_route_boundaries():
    module = renderer()
    rendered = module.nginx_config("shop.example.test", "cabinet.example.test")
    assert "server_name shop.example.test;" in rendered
    assert "server_name cabinet.example.test;" in rendered
    assert "auth_request /_internal_admin_check;" in rendered
    assert "proxy_pass_request_body off;" in rendered
    assert "error_page 404 =403 /_internal_admin_denied;" in rendered
    assert "error_page 401 403 =404 /404.html;" in rendered
    assert 'proxy_set_header X-Forwarded-For $remote_addr;' in rendered
    assert 'proxy_set_header Forwarded "";' in rendered
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for" not in rendered
    assert "location ^~ /api/v1/internal/ { return 404; }" in rendered
    assert 'include /etc/nginx/landing-proxy.conf;' in rendered
    assert 'proxy_set_header Cookie "";' in module.LANDING_PROXY
    assert "proxy_hide_header Set-Cookie;" in module.LANDING_PROXY
    assert "owner-access\\.json" in rendered
    assert "local-resources\\.json" in rendered


def test_compose_has_only_nginx_public_ports_and_exact_trusted_proxy_address(tmp_path):
    module = renderer()
    document = module.compose_document(
        source_root=tmp_path, bundle_root=tmp_path / ".private/production/runtime-bundle",
        scope_id="synthetic-shop", source_pin="a" * 64, frontend_pin="b" * 64,
        ingress_subnet="172.30.65.0/28", instance_file="config/instance.json", asset_root=None,
        plan_pin="c" * 64, activation_pin="d" * 64,
    )
    assert set(document["services"]["nginx"]["ports"]) == {"80:8080", "443:8443"}
    for name, service in document["services"].items():
        if name != "nginx":
            assert "ports" not in service
    api = document["services"]["api"]
    trusted_index = api["command"].index("--forwarded-allow-ips") + 1
    assert api["command"][trusted_index] == "172.30.65.3"
    assert document["services"]["web-init"]["network_mode"] == "none"
    assert document["networks"]["ingress"]["internal"] is True
    assert document["services"]["operator"]["profiles"] == ["operator"]
    assert document["services"]["bot"]["profiles"] == ["bot"]
    assert "environment" not in document["services"]["web-init"]
    assert "env_file" not in document["services"]["nginx"]
    assert json.loads(json.dumps(document))["services"]["nginx"]["user"] == "10001:10001"


def test_full_bundle_keeps_original_proofs_and_owner_keys_immutable_and_verifier_uses_original_paths(staged_installation, monkeypatch):  # noqa: F811
    install, root, pin, activation_pin, _store = staged_installation
    install.activate(root, pin, activation_pin, "synthetic-shop", True)
    for directory in ("apps/web", "packages/web-ui", "packages/api-client"):
        shutil.copytree(ROOT / directory, root / directory)
    for name in ("scripts/render_static_instance.ts", "infra/vite.static.config.ts", "infra/Dockerfile.static-web",
                 "tsconfig.json", "tsconfig.runtime.json", "package.json", "package-lock.json",
                 "LICENSE", "NOTICE", "docs/THIRD_PARTY_NOTICES.md"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes((ROOT / name).read_bytes())
    cert, key = certificate_pair(root)
    before = {name: (root / name).read_bytes() for name in (install.PLAN, install.ADMISSION, install.ACTIVE_ENV)}
    original_key = key.read_bytes()
    module = renderer()
    receipt = module.render(root, pin, activation_pin, cert, key, "172.30.65.0/28")
    assert len(receipt) == 64
    bundle = root / module.BUNDLE
    document = json.loads((bundle / "compose.json").read_bytes())
    public = json.loads((bundle / "public-instance.json").read_bytes())
    assert public["app_name"] == "Synthetic Shop"
    assert "secret_key" not in public and "database_url" not in public
    operator = document["services"]["operator"]
    assert operator["command"][:3] == ["python", "scripts/install_production_instance.py", "verify-activation"]
    targets = {mount["target"] for mount in operator["volumes"]}
    assert "/app/.env.production" in targets
    assert "/app/" + install.PLAN in targets
    assert "/app/" + install.ACTIVATION_PLAN in targets
    assert "/app/" + install.ACTIVE_ADMISSION in targets
    assert "/app/" + install.ACTIVE_ENV in targets
    assert key.read_bytes() == original_key
    assert module.render(root, pin, activation_pin, cert, key, "172.30.65.0/28") == receipt
    for name, body in before.items():
        assert (root / name).read_bytes() == body
    with pytest.raises(ValueError, match="runtime_bundle_exists"):
        module.render(root, pin, activation_pin, cert, key, "172.30.66.0/28")
    assert (bundle / "receipt.json").is_file()
    # Reconstruct the operator's actual file mounts and image source; exercising
    # the real verifier catches paths that env_file injection alone cannot supply.
    runtime_root = root / "synthetic-image"
    for record in json.loads((root / install.MANIFEST).read_bytes()):
        target = runtime_root / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / record["path"]).read_bytes())
    for name in (".gitignore", ".dockerignore", "config/bootstrap.example.json"):
        target = runtime_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    for mount in operator["volumes"]:
        target = runtime_root / mount["target"].removeprefix("/app/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(mount["source"]).read_bytes())
        target.chmod(0o600)
    from sunrise_infrastructure import instance_config
    monkeypatch.setattr(instance_config, "PROJECT_ROOT", runtime_root)
    assert install.verify_activation(runtime_root, pin, activation_pin) == activation_pin
