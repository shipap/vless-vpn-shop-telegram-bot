# VLESS VPN Shop application instructions

This is a standalone, neutral application edition. Historical deployment authority,
credentials and private material from another installation do not apply here.
The installation owner must explicitly authorize production deployment, external
publication and real charges. Do not initialize or publish a remote repository by default.

Current work is limited to this public project. Do not read or modify another
installation's private sources, dependencies, configuration, customer data or
deployment resources. Keep disposable verification tools and evidence under this
project's ignored .artifacts directory; never use them as distribution content.

Build distribution ZIPs only with scripts/package_public_release.py and a reviewed
local Gitleaks executable. Do not archive the working directory recursively.
The packer uses an explicit source allowlist and scans the extracted final contents;
keep required notices, fixtures and lockfiles in that allowlist when adding formats.
Changes to its pinned Gitleaks configuration require an independent security review
and a coordinated checksum update. Never bypass scanning or overwrite an existing ZIP.

The default public display name is VLESS VPN Shop. Public author contacts are
intentionally work@bububi.icu, https://t.me/o3amfeels and https://github.com/shipap;
they are documentation contacts, not instance support defaults. Preserve the exact
owner-approved RollyPay onboarding referral URL in both README files. Keep the
security reporting channel distinct from merchant onboarding.

Use one shared business backend/database for website and Telegram bot. Preserve
identities, paid access, purchase history, UUIDs and relationships. Keep money in
Decimal/NUMERIC and BIGINT identities as strings. Never run DDL or providers on import.
Do not create a separate billing or identity implementation for either interface.

Branding belongs to the typed InstanceConfig boundary. Start with
config/instance.example.json: neutral profile, optional images absent. Catalog,
prices and admin-managed content remain in the database. The sunrise_* package names,
SUNRISE_* environment prefix and compatibility keys are technical identifiers;
do not mass-rename them or replace migration/test fixture values indiscriminately.

Keep secrets in ignored local configuration. Never copy customer dumps, usable
auth/payment/VPN links, logs, private assets or screenshots into distributed files.
Use synthetic local test resources and explicit recipient/resource allowlists;
empty allowlists deny. Never weaken ownership checks, sessions, CSRF, MFA or consent.
New recurring enrollment OFF must preserve existing agreement handling/cancellation.
Unknown financial fees are not zero. Preserve technical and financial history.

Product copy, setup comments and owner-facing reports are Russian; agent instructions
and conventional code/API identifiers are English. Use a supported Sol model where
available; do not use Astra. Keep one owner for migrations, billing and lockfiles,
and independently review high-risk changes. Do not revert another contributor's work.
Apply relevant installed debugging, security, billing, migration and verification
skills. Reproduce defects and run appropriate checks; report PASS/FAIL/NOT RUN honestly.

This folder is maintained separately from any private installation. Mirror fixes
between editions only after explicit owner approval of the affected changes.
Preserve licenses and third-party notices. The owner selected AGPL-3.0-only for
code they own, with author attribution shipap. Third-party licenses remain intact.
The current task explicitly authorizes preparing Russian and English README files,
installation guides and release metadata, and mirroring applicable approved fixes.
It does not authorize publication, a Git push, remote repository creation, production
changes or live integration calls. Keep all test data synthetic and providers mocked.

Reviewed documentation skills are project-local at .agents/skills/create-readme/
and .agents/skills/humanizer/. Read their SKILL.md files when writing public docs.
Record upstream revisions and distinguish tested commands from pending verification.

Before claiming release readiness, read docs/RELEASE_STATUS.md,
docs/FINAL_PUBLICATION_CHECK.md and inspect any
current local verification receipts. Recheck current sources;
historical green reports are not evidence for a later revision. Verify the base
interpreter of a fresh environment and keep integration providers mocked unless
the installation owner explicitly authorizes their use.
