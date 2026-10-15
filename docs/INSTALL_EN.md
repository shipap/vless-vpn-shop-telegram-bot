# Installation

[Русский](INSTALL_RU.md) · [English](INSTALL_EN.md) · [README](../README.en.md)

This guide prepares a separate synthetic localhost environment. Clean backend/web Docker builds, migrations through `v11_0019`, PostgreSQL, Redis and the worker have been checked in isolation with mocked external providers. Windows host access through local Nginx ingress, protected route handling and blocked application egress have passed. The separate fresh-database operator in [deployment (RU)](DEPLOYMENT.md) has passed its preparation and first activation flow on dedicated real PostgreSQL with synthetic data and only authentication mail enabled; no real provider calls were made.

A separate Linux production renderer prepares an HTTPS Compose bundle with static frontend files, operator-owned external PostgreSQL/Redis and existing TLS files. See [runtime preparation and verification (RU)](DEPLOYMENT.md#https-runtime-bundle). Twenty real Nginx/HTTPS scenarios passed with an operator-owned test CA. The check requiring webhook origin to match the dashboard host also passed.

For this bundle, set `SUNRISE_WEBHOOK_BASE_URL` to the dashboard's HTTPS origin,
for example `https://cabinet.example.test`. Configure full provider endpoints:
`https://cabinet.example.test/api/v1/webhooks/rollypay` and
`https://cabinet.example.test/api/v1/webhooks/remnawave`. The bundle does not serve
a separate third webhook host.

## Requirements

Use Python 3.12 or newer and Docker with Compose v2 running Linux containers. Native frontend development needs Node.js 24 and npm. The images use Python 3.12, Node.js 24, PostgreSQL 18.4 and Redis 8.2.3.

On Windows, install Python with the `py` launcher and prepare Docker Desktop with WSL/virtualization support. Complete any required Windows restart before checking the engine. On Linux, install Docker Engine and the Compose plugin through your system's supported procedure.

```text
docker version
docker compose version
```

`docker version` must show both Client and Server. A working CLI alone cannot build containers.

## Source files

Extract the supplied archive into a separate directory, or use the repository URL supplied by its publisher. This guide does not create a remote repository. Run every command from the application directory containing `pyproject.toml` and `package.json`.

## Docker setup

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe scripts/setup_local_docker.py prepare
docker compose --env-file .env.docker -f infra/compose.standalone.json build
docker compose --env-file .env.docker -f infra/compose.standalone.json up -d
docker compose --env-file .env.docker -f infra/compose.standalone.json ps -a
```

Linux/macOS:

```bash
python3.12 -m venv .venv
.venv/bin/python scripts/setup_local_docker.py prepare
docker compose --env-file .env.docker -f infra/compose.standalone.json build
docker compose --env-file .env.docker -f infra/compose.standalone.json up -d
docker compose --env-file .env.docker -f infra/compose.standalone.json ps -a
```

The preparation script needs only the Python standard library. It creates an ignored `.env.docker` with random local secrets and prints the interface URLs without printing secrets. An existing file is preserved. Keep it private.

Default URLs are `http://127.0.0.1:3100` for the landing and `http://127.0.0.1:3200` for the dashboard. If a default port is busy, the script chooses an available port and prints the actual URL. On first preparation, pass `--landing-port 33100 --dashboard-port 33200` to request exact ports. An occupied explicitly requested port is rejected.

Compose runs PostgreSQL, Redis, a one-shot `init`, API, worker, landing, dashboard and a separate Nginx `ingress`. `init` applies guarded migrations followed by the initial catalog bootstrap. Its successful exit code is `0`. The API waits for initialization; the worker and web interfaces wait for the API.

Only Nginx `ingress` exposes web ports, bound to `127.0.0.1`. It runs as UID `101`, receives no secret env files and uses two fixed upstreams. Applications, PostgreSQL and Redis remain on the internal network without external egress; application services share the PostgreSQL container's network namespace. PostgreSQL, Redis and API have no published host ports. Building images and downloading dependencies requires internet access.

The initial profile disables external providers, Telegram polling and recipients. You can inspect the catalog, while real payments and delivery remain unavailable. The empty email allowlist also blocks login codes. Authentication testing requires a separately configured synthetic profile, described below.

Stop while preserving data:

```text
docker compose --env-file .env.docker -f infra/compose.standalone.json stop
```

Use `up -d` to start again. Do not use `down -v`; it deletes data volumes.

## Native development

Prepare separate local PostgreSQL and Redis services. First copy `.env.example` to a new `.env` and `config/instance.example.json` to `config/instance.json`. Preserve existing working files. Set new `LOCAL_POSTGRES_PASSWORD` and `LOCAL_REDIS_PASSWORD` in the protected `.env`. [compose.local.yml](../infra/compose.local.yml) exposes the services on loopback ports `55439` and `56379`. Then run:

```text
docker compose --env-file .env -f infra/compose.local.yml up -d
```

Set the matching database and Redis DSNs, a random `SUNRISE_SECRET_KEY` of at least 32 characters and `SUNRISE_LOCAL_TEST=true`. For login tests, allow only your synthetic `.test` addresses in `SUNRISE_EMAIL_ALLOWLIST`, for example `["developer@example.test"]`, and set `SUNRISE_MAIL_DIR=.private/mail`. Set `SUNRISE_TURNSTILE_TEST_BYPASS=true` and leave external integration permissions off.

Before `bootstrap`, set all four legal fields: `OFFER_URL=https://example.test/offer`, `PRIVACY_URL=https://example.test/privacy`, `OFFER_VERSION=local-v1` and `PRIVACY_VERSION=local-v1`. These are synthetic values; the application does not need to fetch these pages. `run_local.py bootstrap` validates the complete set and saves initial documents when none are configured. Existing documents and consent records remain intact. Partial or invalid input is rejected; an entirely absent set leaves documents unconfigured and login blocked. API startup alone does not perform bootstrap. The full login flow against real PostgreSQL remains unverified. Public installations need their own actual documents. See [configuration (RU)](CONFIGURATION.md).

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install pip==26.2.1 setuptools==83.0.0 wheel==0.46.2
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
npm.cmd ci
.\.venv\Scripts\python.exe scripts/run_local.py migrate
.\.venv\Scripts\python.exe scripts/run_local.py bootstrap
.\.venv\Scripts\python.exe scripts/run_local.py api
```

Run each following command in a separate terminal from the same directory:

```powershell
.\.venv\Scripts\python.exe scripts/run_local.py worker
npm.cmd run dev:landing
npm.cmd run dev:dashboard
```

On Linux/macOS, replace `.\.venv\Scripts\python.exe` with `.venv/bin/python` and `npm.cmd` with `npm`. Explicit interpreter paths avoid reliance on shell activation. The API binds to `127.0.0.1:8100`; its health endpoint is `/api/v1/health`.

The mail sink stores synthetic login messages under `.private/mail`. Read only your own messages locally and keep codes out of reports. External SMTP is unnecessary for this flow.

## Administrator and legal setup

Email login creates a customer account. Administration requires a canonical account with a verified allowed Telegram identity, the configured owner/admin Telegram IDs and MFA. An entered ID or email address alone does not establish administrative identity.

A verified Telegram ID matching `SUNRISE_OWNER_ID` receives owner capabilities without a separate offline role grant. Configure your own Telegram Login client credentials and exact callback redirect URI, complete genuine Telegram login, then enroll and confirm MFA from the fresh session. See [first owner login (RU)](DEPLOYMENT.md#первый-вход-владельца). Real OIDC login was not exercised during this preparation.

`scripts/bootstrap_local_owner.py --help` documents the bounded offline first-owner bootstrap. It requires an existing verified account, the exact allowed local database, confirmation and an audit reason. It does not replace another owner or grant production access. The default Docker profile does not configure Telegram authentication, so an admin login is not ready immediately after startup.

Before purchases, configure your legal document URLs and versions, catalog and Remnawave mappings. Legal documents require explicit versions and HTTPS URLs. The product interface is Russian.

## Checks

Windows:

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
.\.venv\Scripts\python.exe scripts/check_architecture.py
.\.venv\Scripts\python.exe -m pytest tests/unit --tb=short
```

PostgreSQL suites need separate synthetic databases `sunrise_test_backend` and `sunrise_test_bot` and their explicit test DSNs. `scripts/run_isolated_tests.py` validates targets and creates temporary schemas. Serialize PostgreSQL suites sharing advisory locks. Tests skipped because a service is absent do not establish integration success.

[Troubleshooting (RU)](TROUBLESHOOTING.md) · [Updates (RU)](UPDATING.md) · [Backup and restore (RU)](BACKUP_RESTORE.md)
