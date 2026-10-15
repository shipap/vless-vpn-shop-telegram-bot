"""Готовит собственный HTTPS Compose-bundle без запуска Docker, БД или провайдеров."""

import ipaddress
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy.engine import make_url
from sunrise_infrastructure.instance_assets import read_asset

if __package__:
    from . import install_production_instance as install
else:
    import install_production_instance as install

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ".private/production/runtime-bundle"
UID = 10001
CONTAINER_TMPFS = ["/tmp"]  # noqa: S108 -- isolated per-container tmpfs, no host path


def external_host(host):
    if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("runtime_bridge_loopback_denied")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host) or ".." in host:
            raise ValueError("runtime_host_invalid") from None
    else:
        if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError("runtime_bridge_loopback_denied")
    return host


def https_host(origin):
    parsed = urlparse(origin or "")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("runtime_https_origin_required")
    host = external_host(parsed.hostname)
    if ":" in host:
        raise ValueError("runtime_dns_host_required")
    return host


def runtime_hosts(settings):
    for host in (make_url(settings.database_url).host, urlparse(settings.redis_url).hostname):
        external_host(host)
    landing, dashboard = https_host(settings.landing_url), https_host(settings.dashboard_url)
    if landing == dashboard:
        raise ValueError("runtime_distinct_hosts_required")
    if not {landing, dashboard} <= set(settings.allowed_hosts):
        raise ValueError("runtime_backend_hosts_required")
    if https_host(settings.webhook_base_url) != dashboard:
        raise ValueError("runtime_callback_dashboard_origin_required")
    return landing, dashboard


def validate_tls(cert_file, key_file, hosts):
    certificate = x509.load_pem_x509_certificate(Path(cert_file).read_bytes())
    key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
    public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if certificate.public_key().public_bytes(*public_format) != key.public_key().public_bytes(*public_format):
        raise ValueError("runtime_tls_key_mismatch")
    now = datetime.now(UTC)
    if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
        raise ValueError("runtime_tls_dates_invalid")
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    for host in hosts:
        if not any(host == name.lower() or name.startswith("*.") and host.count(".") == name.count(".")
                   and host.endswith(name[1:].lower()) for name in names):
            raise ValueError("runtime_tls_san_mismatch")
    return {"validation": "local_key_dates_san_only", "public_trust_or_live_https_verified": False,
            "certificate_sha256": install.digest(Path(cert_file).read_bytes()),
            "private_key_sha256": install.digest(Path(key_file).read_bytes())}


def nginx_config(landing, dashboard):
    for host in (landing, dashboard):
        if https_host("https://" + host) != host:
            raise ValueError("runtime_host_invalid")
    template = r'''pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log off;
  server_tokens off;
  client_max_body_size 64m;
  client_body_temp_path /tmp/client_temp;
  proxy_temp_path /tmp/proxy_temp;
  fastcgi_temp_path /tmp/fastcgi_temp;
  uwsgi_temp_path /tmp/uwsgi_temp;
  scgi_temp_path /tmp/scgi_temp;
  map $request_uri $unsafe_raw_path {
    default 0;
    ~*^[^?]*(%2e|%2f|%5c|%25) 1;
    ~^[^?]*/\.\.?(?:/|\?|$) 1;
  }
  upstream app_backend { server api:8100; }
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $remote_addr;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header Forwarded "";
  proxy_read_timeout 60s;
  ssl_certificate /run/tls/fullchain.pem;
  ssl_certificate_key /run/tls/privkey.pem;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_session_tickets off;
  server { listen 8080 default_server; server_name _; return 404; }
  server { listen 8443 ssl default_server; server_name _; return 404; }
  server {
    listen 8080;
    server_name __LANDING__ __DASHBOARD__;
    return 308 https://$host$request_uri;
  }
  __SERVERS__
}
'''
    common = r'''
    if ($unsafe_raw_path) { return 404; }
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    include /usr/share/nginx/html/__PROFILE__/csp.conf;
    location ~* "(?:^|/)(?:\.private|private-audit|\.git|config|brand|instance-assets[^/]*|\.env[^/]*|owner-access\.json|local-resources\.json)(?:/|$)|\.(?:sql|dump|bak|pem|key|conf)$" { return 404; }
    location ~ /\. { return 404; }
    location ^~ /api/v1/internal/ { return 404; }
    location = /index.html { expires -1; }
    location = /site.webmanifest { expires -1; }
    location = /404.html { internal; }
'''
    landing_locations = r'''
    location = /api/v1/catalog { if ($request_method != GET) { return 404; } include /etc/nginx/landing-proxy.conf; }
    location = /api/v1/public-config { if ($request_method != GET) { return 404; } include /etc/nginx/landing-proxy.conf; }
    location ~ "^/api/v1/(?:content/emoji/[0-9]{1,32}|instance/assets/(?:logo|favicon|hero|og))$" {
      if ($request_method != GET) { return 404; }
      include /etc/nginx/landing-proxy.conf;
    }
    location = /api/v1/campaigns/visit { if ($request_method != POST) { return 404; } include /etc/nginx/landing-proxy.conf; }
    location /api/ { return 404; }
    location / { try_files $uri $uri/ /index.html; }
'''
    dashboard_locations = r'''
    location = /_internal_admin_check {
      internal;
      proxy_pass http://app_backend/api/v1/admin/access;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Forwarded "";
      proxy_intercept_errors on;
      error_page 404 =403 /_internal_admin_denied;
    }
    location = /_internal_admin_denied { internal; return 403; }
    location ~ ^/admin(?:/|$) {
      auth_request /_internal_admin_check;
      error_page 401 403 =404 /404.html;
      try_files $uri /index.html;
    }
    location /api/ { proxy_pass http://app_backend; }
    location / { try_files $uri $uri/ /index.html; }
'''
    servers = []
    for host, profile, locations in ((landing, "landing", landing_locations), (dashboard, "dashboard", dashboard_locations)):
        servers.append("server { listen 8443 ssl; server_name " + host + "; root /usr/share/nginx/html/" + profile + ";"
                       + common.replace("__PROFILE__", profile) + locations + "\n}")
    return (template.replace("__LANDING__", landing).replace("__DASHBOARD__", dashboard)
            .replace("__SERVERS__", "\n".join(servers)))


LANDING_PROXY = '''proxy_pass http://app_backend;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Forwarded "";
proxy_set_header Cookie "";
proxy_set_header Authorization "";
proxy_set_header X-CSRF-Token "";
proxy_hide_header Set-Cookie;
'''


def compose_document(*, source_root, bundle_root, scope_id, source_pin, frontend_pin, ingress_subnet,
                     instance_file, asset_root, plan_pin, activation_pin, operator_files=()):
    network = ipaddress.ip_network(ingress_subnet, strict=True)
    if not isinstance(network, ipaddress.IPv4Network) or not network.is_private or not 24 <= network.prefixlen <= 28:
        raise ValueError("runtime_private_ingress_subnet_required")
    api_ip, nginx_ip = str(network.network_address + 2), str(network.network_address + 3)
    backend_image = f"vpn-shop-{scope_id}-backend:{source_pin[:16]}"
    web_image = f"vpn-shop-{scope_id}-web:{frontend_pin[:16]}"
    def mount(source, target):
        return {"type": "bind", "source": str(bundle_root / source), "target": target, "read_only": True,
                "bind": {"create_host_path": False}}
    protected = [mount("secrets/admission.json", "/app/" + install.ACTIVE_ADMISSION),
                 mount("secrets/runtime.env", "/app/" + install.ACTIVE_ENV),
                 mount("secrets/source-manifest.json", "/app/" + install.MANIFEST),
                 mount("secrets/instance.json", "/app/" + instance_file)]
    if asset_root:
        protected.append(mount("instance-assets", "/app/" + asset_root))
    shared = {"image": backend_image, "user": "10001:10001", "read_only": True,
              "env_file": [str(bundle_root / "secrets/runtime.env")], "tmpfs": CONTAINER_TMPFS,
              "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
              "volumes": protected + ["runtime:/app/.private/runtime", "media:/app/.private/broadcast-media", "mail:/app/.private/mail"]}
    services = {
        "api": shared | {"build": {"context": str(source_root), "dockerfile": "infra/Dockerfile.standalone"},
                         "command": ["python", "-m", "uvicorn", "sunrise_api.main:create_app", "--factory", "--host", "0.0.0.0",  # noqa: S104 -- private bridge only, no published API port
                                     "--port", "8100", "--proxy-headers", "--forwarded-allow-ips", nginx_ip, "--no-access-log"],
                         "networks": {"ingress": {"ipv4_address": api_ip}, "egress": {}}, "restart": "unless-stopped"},
        "worker": shared | {"command": ["python", "-m", "sunrise_worker.main"], "networks": ["egress"],
                            "depends_on": {"api": {"condition": "service_started"}}, "restart": "unless-stopped"},
        "bot": shared | {"command": ["python", "-c", "from sunrise_bot.runtime import main; main()"],
                         "profiles": ["bot"], "networks": ["egress"], "restart": "unless-stopped"},
        "operator": shared | {"profiles": ["operator"], "networks": ["egress"],
                              "volumes": protected + [mount("operator/" + name, "/app/" + name) for name in operator_files],
                              "command": ["python", "scripts/install_production_instance.py", "verify-activation", "--expected-plan-sha256", plan_pin,
                                          "--expected-activation-sha256", activation_pin]},
        "web-init": {"image": web_image, "build": {"context": str(source_root), "dockerfile": "infra/Dockerfile.static-web",
                                                   "args": {"FRONTEND_SOURCE_SHA256": frontend_pin}},
                     "network_mode": "none", "user": "10001:10001", "read_only": True, "tmpfs": CONTAINER_TMPFS,
                     "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                     "volumes": ["web:/output", mount("public-instance.json", "/app/public-instance.json")], "restart": "no"},
        "nginx": {"image": "nginx:1.30.4-alpine", "user": "10001:10001", "entrypoint": ["nginx"],
                  "command": ["-g", "daemon off;", "-c", "/etc/nginx/nginx.conf"], "read_only": True,
                  "tmpfs": CONTAINER_TMPFS, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                  "ports": ["80:8080", "443:8443"], "networks": {"ingress": {"ipv4_address": nginx_ip}, "edge": {}},
                  "volumes": ["web:/usr/share/nginx/html:ro", mount("nginx.conf", "/etc/nginx/nginx.conf"),
                              mount("landing-proxy.conf", "/etc/nginx/landing-proxy.conf"),
                              mount("tls/fullchain.pem", "/run/tls/fullchain.pem"), mount("tls/privkey.pem", "/run/tls/privkey.pem")],
                  "depends_on": {"web-init": {"condition": "service_completed_successfully"}, "api": {"condition": "service_started"}},
                  "restart": "unless-stopped"},
    }
    return {"name": "vpn-shop-" + scope_id + "-" + activation_pin[:8], "services": services,
            "networks": {"ingress": {"internal": True, "ipam": {"config": [{"subnet": str(network)}]}}, "egress": {}, "edge": {}},
            "volumes": {"runtime": {}, "media": {}, "mail": {}, "web": {}}}


def write_bundle(root, relative, body):
    target = install.file_path(root, BUNDLE + "/" + relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    install.file_path(root, BUNDLE + "/" + relative)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if target.read_bytes() != body or os.name != "nt" and (target.stat().st_uid != UID or target.stat().st_mode & 0o077):
            raise ValueError("runtime_bundle_exists_or_permissions_changed") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt" and os.geteuid() == 0:
        os.chown(target, UID, UID)


def render(root, plan_pin, activation_pin, cert_file, key_file, ingress_subnet):
    if os.name != "nt" and os.geteuid() not in {0, UID}:
        raise ValueError("runtime_bundle_requires_root_or_runtime_uid")
    root = install.owned_path(root)
    document, _, activation, _ = install.load_activation(root, plan_pin, activation_pin)
    inputs = install.checked_inputs(root, document)
    active = install.activation_files(inputs, document, activation)
    for relative, body in active.items():
        if install.read_file(root, relative) != body:
            raise ValueError("runtime_active_file_changed")
    settings = install.FileOnlySettings(_env_file=install.file_path(root, install.ACTIVE_ENV))
    landing, dashboard = runtime_hosts(settings)
    cert_file, key_file = Path(cert_file).resolve(strict=True), Path(key_file).resolve(strict=True)
    install.check_secret_permissions(key_file)
    tls = validate_tls(cert_file, key_file, [landing, dashboard])
    bundle_root = root / BUNDLE
    frontend_records = [
        {"path": path.relative_to(root).as_posix(), "sha256": install.digest(install.read_file(root, path.relative_to(root).as_posix()))}
        for base in ("apps/web", "packages/web-ui", "packages/api-client")
        for path in sorted((root / base).rglob("*"))
        if path.is_file() and "node_modules" not in path.parts and "__pycache__" not in path.parts
    ]
    for relative in ("scripts/render_static_instance.ts", "infra/vite.static.config.ts", "infra/Dockerfile.static-web",
                     "tsconfig.json", "tsconfig.runtime.json", "package.json", "package-lock.json",
                     "LICENSE", "NOTICE", "docs/THIRD_PARTY_NOTICES.md"):
        frontend_records.append({"path": relative, "sha256": install.digest(install.read_file(root, relative))})
    frontend_pin = install.digest(install.encoded(frontend_records))
    files = {"secrets/runtime.env": active[install.ACTIVE_ENV], "secrets/admission.json": active[install.ACTIVE_ADMISSION],
             "secrets/source-manifest.json": install.read_file(root, install.MANIFEST),
             "secrets/instance.json": install.read_file(root, settings.instance_config_path),
             "tls/fullchain.pem": cert_file.read_bytes(), "tls/privkey.pem": key_file.read_bytes(),
             "public-instance.json": install.encoded(settings.instance.public(project_root=root)),
             "nginx.conf": nginx_config(landing, dashboard).encode(), "landing-proxy.conf": LANDING_PROXY.encode()}
    # Proof input copies keep original relative paths inside a protected directory.
    for relative in (install.PLAN, install.ACTIVATION_PLAN, install.ADMISSION, install.RUNTIME_ENV,
                     document["env_file"], document["scope_file"], activation["evidence"]["path"]):
        files["operator/" + relative] = install.read_file(root, relative)
    for item in activation["evidence"]["attestation"]["checks"]:
        files["operator/" + item["path"]] = install.read_file(root, item["path"])
    asset_root = None
    for role, field in (("logo", "logo_asset"), ("favicon", "favicon_asset"), ("hero", "hero_asset"), ("og", "og_image_asset")):
        if getattr(settings.instance, field):
            found = read_asset(settings.instance, role, project_root=root)
            if found is not None:
                asset_root = settings.instance.asset_root
                files["instance-assets/" + getattr(settings.instance, field)] = found[0]
    compose = compose_document(source_root=root, bundle_root=bundle_root, scope_id=activation["scope_id"],
                               source_pin=inputs["fingerprints"]["manifest"], frontend_pin=frontend_pin,
                               ingress_subnet=ingress_subnet, instance_file=settings.instance_config_path,
                               asset_root=asset_root, plan_pin=plan_pin, activation_pin=activation_pin,
                               operator_files=[name.removeprefix("operator/") for name in files if name.startswith("operator/")])
    files["compose.json"] = install.encoded(compose)
    receipt = {"version": 1, "install_plan_sha256": plan_pin, "activation_plan_sha256": activation_pin,
               "scope_id": activation["scope_id"], "tls": tls, "frontend_source": frontend_records,
               "network": "general_egress_bridge_with_application_runtime_scopes",
               "database_or_provider_requests": "not_performed_by_renderer",
               "permissions": "windows_acl_not_verified" if os.name == "nt" else "owner_10001_mode_0600",
               "files": {name: install.digest(body) for name, body in files.items()}}
    for relative, body in files.items():
        write_bundle(root, relative, body)
    write_bundle(root, "receipt.json", install.encoded(receipt))
    return install.digest(install.encoded(receipt))


def main(argv=None):
    parser = install.SafeParser(description="Неизменяемый HTTPS runtime-bundle; сервисы не запускаются")
    for option in ("expected-plan-sha256", "expected-activation-sha256", "tls-cert", "tls-key", "ingress-subnet"):
        parser.add_argument("--" + option, required=True)
    try:
        args = parser.parse_args(argv)
        pin = render(ROOT, args.expected_plan_sha256, args.expected_activation_sha256,
                     args.tls_cert, args.tls_key, args.ingress_subnet)
        print(json.dumps({"bundle": BUNDLE, "receipt_sha256": pin, "runtime_verification": "not_run"}))
        return 0
    except Exception:  # noqa: BLE001 -- never disclose TLS keys, runtime credentials or private paths
        print("Генерация остановлена проверкой конфигурации, допуска, TLS или прав файлов.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
