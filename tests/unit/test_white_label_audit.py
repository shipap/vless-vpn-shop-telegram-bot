"""Run the real audit CLI against synthetic trees, never instance credentials."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_white_label.py"


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def run_audit(root, *, bundles=()):
    output = root / ".artifacts" / "v7-audit-test"
    command = [sys.executable, str(SCRIPT), "--root", str(root), "--output", str(output)]
    for bundle in bundles:
        command.extend(["--bundle-root", str(bundle)])
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    return json.loads(output.with_suffix(".json").read_text(encoding="utf-8")), result


def test_white_label_audit_reports_every_location_without_ignoring_namespaces(tmp_path):
    write(tmp_path, "apps/card.tsx", 'const title = "sunrise SHOP sunrise SHOP";\nconst price = 99;\n')
    write(tmp_path, "packages/module.py", "from sunrise_core import pricing\n")
    report, _ = run_audit(tmp_path)
    brands = [item for item in report["matches"] if item["category"] == "brand"]
    assert [(item["path"], item["line"], item["column"]) for item in brands] == [
        ("apps/card.tsx", 1, 16),
        ("apps/card.tsx", 1, 29),
        ("packages/module.py", 1, 6),
    ]
    assert brands[0]["disposition"] == "instance_config_required"
    assert brands[2]["disposition"] == "technical_namespace"
    assert all("category" in item and "disposition" in item for item in report["matches"])


def test_white_label_audit_never_serializes_sensitive_source_literals(tmp_path):
    credential = "synthetic-private-value-not-for-report"
    url = "postgresql://operator:synthetic-pass@private.example.test/database"
    email = "private-person@operator.example.test"
    token = "123456789:abcdefghijklmnopqrstuvwxyz_1234567"  # noqa: S105 - synthetic scanner fixture
    write(
        tmp_path,
        "config/bad.py",
        f'TOKEN = "{credential}"\nDB = "{url}"\nMAIL = "{email}"\nBOT = "{token}"\n',
    )
    report, process = run_audit(tmp_path)
    emitted = json.dumps(report) + process.stdout + process.stderr
    assert all(value not in emitted for value in (credential, url, email, token, "synthetic-pass"))
    assert any(item["category"] == "credential_candidate" for item in report["matches"])
    assert any(item["category"] == "email" for item in report["matches"])
    assert (tmp_path / ".artifacts/v7-audit-test.csv").exists()


def test_white_label_audit_prunes_private_runtime_and_dependency_regions(tmp_path):
    for relative in (
        ".private/raw.py",
        "private-audit/customer.py",
        ".env",
        ".env.local",
        "config/instance.yaml",
        "node_modules/library.py",
        ".venv/lib.py",
    ):
        write(tmp_path, relative, "sunrise secret_private_marker\n")
    write(tmp_path, ".env.example", "APP_NAME=VPN Demo\n")
    write(tmp_path, "apps/main.py", "name = 'sunrise SHOP'\n")
    report, _ = run_audit(tmp_path)
    assert {item["path"] for item in report["files"] if item["status"] == "scanned_text"} == {
        ".env.example",
        "apps/main.py",
    }
    excluded = {item["path"] for item in report["excluded"]}
    assert {
        ".private",
        "private-audit",
        ".env",
        ".env.local",
        "config/instance.yaml",
        "node_modules",
        ".venv",
    } <= excluded
    assert not any("secret_private_marker" in str(item) for item in report["matches"])


def test_white_label_audit_explicit_bundle_scan_remains_inside_approved_tree(tmp_path):
    bundle = tmp_path / ".artifacts/web/dashboard"
    write(tmp_path, ".artifacts/web/dashboard/assets/main.js", 'const name="sunrise SHOP";')
    write(tmp_path, ".artifacts/raw.py", "private sunrise\n")
    report, _ = run_audit(tmp_path, bundles=(bundle,))
    assert [item["path"] for item in report["matches"] if item["category"] == "brand"] == [
        ".artifacts/web/dashboard/assets/main.js"
    ]
    assert report["matches"][0]["region"] == "bundle"


def test_white_label_audit_scans_separate_bounded_instance_bundles(tmp_path):
    roots = []
    for instance in ("neutral", "private-preview"):
        root = tmp_path / ".artifacts/web" / instance / "dashboard"
        write(tmp_path, str(root.relative_to(tmp_path) / "main.js"), 'const title="sunrise SHOP";')
        roots.append(root)
    report, _ = run_audit(tmp_path, bundles=roots)
    assert len([item for item in report["matches"] if item["category"] == "brand"]) == 2
    assert all(item["region"] == "bundle" for item in report["files"])


def test_white_label_audit_refuses_private_or_external_bundle_roots(tmp_path):
    outside = tmp_path.parent / "foreign-bundle"
    output = tmp_path / ".artifacts/v7-audit-test"
    write(tmp_path, ".artifacts/raw-private/customers.py", "sunrise private")
    write(tmp_path, ".artifacts/web/INVALID_NAME/dashboard/raw.py", "sunrise private")
    for bundle in (
        tmp_path / ".private",
        outside,
        tmp_path / ".artifacts/raw-private",
        tmp_path / ".artifacts/web/INVALID_NAME/dashboard",
    ):
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(tmp_path),
                "--output",
                str(output),
                "--bundle-root",
                str(bundle),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "bundle_root_denied" in result.stderr


def test_white_label_audit_exclusions_are_case_insensitive_on_windows(tmp_path):
    for relative in (
        ".ENV.LOCAL",
        "config/INSTANCE.YAML",
        ".PRIVATE/customer.py",
        "NODE_MODULES/index.js",
        "unsafe.LOG",
    ):
        write(tmp_path, relative, "sunrise private")
    report, _ = run_audit(tmp_path)
    assert not report["files"]
    assert len(report["excluded"]) == 5


def test_white_label_audit_does_not_read_generated_property_test_cache(tmp_path):
    write(tmp_path, ".hypothesis/constants/cache", "sunrise generated_private_value")
    report, _ = run_audit(tmp_path)
    assert not report["files"]
    assert report["excluded"] == [{"path": ".hypothesis", "reason": "dependency_or_cache_not_read"}]


def test_white_label_audit_does_not_read_private_instance_assets(tmp_path):
    write(tmp_path, "instance-assets/brand/private.svg", "sunrise private")
    report, _ = run_audit(tmp_path)
    assert not report["files"]
    assert report["excluded"] == [{"path": "instance-assets", "reason": "private_or_agent_region_not_read"}]


def test_white_label_audit_keeps_example_but_excludes_runtime_json_and_asset_variants(tmp_path):
    write(tmp_path, "config/instance.json", "private")
    write(tmp_path, "config/instance.demo.json", "private")
    write(tmp_path, "instance-assets.demo/logo.svg", "private")
    write(tmp_path, "config/instance.example.json", '{"app_name":"VPN Shop"}')
    report, _ = run_audit(tmp_path)
    assert [row["path"] for row in report["files"]] == ["config/instance.example.json"]
    assert len(report["excluded"]) == 3


def test_white_label_audit_output_cannot_overwrite_a_product_file(tmp_path):
    target = write(tmp_path, "apps/v7-audit-product.json", "untouched")
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--output", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr.strip() == "output_path_denied"
    assert target.read_text(encoding="utf-8") == "untouched"


def test_white_label_audit_differentiates_business_policy_from_css_and_seed(tmp_path):
    write(tmp_path, "apps/checkout.py", "price = 249\ndevices = 4\n")
    write(tmp_path, "packages/bootstrap.py", "price = 99\n")
    write(tmp_path, "apps/card.css", ".card { width: 99px; }\n")
    write(tmp_path, "tests/legacy.py", "price = 249\n")
    report, _ = run_audit(tmp_path)
    numbers = {item["path"]: item for item in report["matches"] if item["category"] == "numeric_literal"}
    assert numbers["apps/checkout.py"]["disposition"] == "business_semantics_review"
    assert numbers["packages/bootstrap.py"]["disposition"] == "bootstrap_default_review"
    assert numbers["apps/card.css"]["disposition"] == "presentation_constant"
    assert numbers["tests/legacy.py"]["disposition"] == "synthetic_test_fixture_review"


def test_white_label_audit_does_not_truncate_large_minified_bundles(tmp_path):
    text = '"sunrise SHOP";' + "x" * (2 * 1024 * 1024) + ';"sunrise SHOP";'
    write(tmp_path, "dist/main.js", text)
    report, _ = run_audit(tmp_path)
    brands = [item for item in report["matches"] if item["category"] == "brand"]
    assert len(brands) == 2
    assert brands[1]["column"] > 2 * 1024 * 1024


def test_white_label_audit_binary_assets_have_explicit_unverified_visual_status(tmp_path):
    path = tmp_path / "assets/brand/mascot.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\x00\x01synthetic")
    report, _ = run_audit(tmp_path)
    assert report["files"][0]["status"] == "binary_metadata_only"
    assert any(item["category"] == "brand_asset" and item["line"] == 0 for item in report["matches"])
    assert "binary_visual_identity_not_verified" in report["limitations"]


def test_white_label_audit_does_not_follow_escape_symlinks(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-private-target")
    outside.mkdir()
    write(outside, "record.py", "sunrise private outside")
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows does not permit creation of this synthetic symlink")
    report, _ = run_audit(tmp_path)
    assert not report["matches"]
    assert any(
        item["path"] == "linked" and item["reason"] == "link_not_followed" for item in report["excluded"]
    )


def scanner_module():
    spec = importlib.util.spec_from_file_location("white_label_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "relative", [".private", ".private/nested", "node_modules", ".artifacts", "instance-assets.demo"]
)
def test_white_label_audit_rejects_excluded_root_before_reading(tmp_path, monkeypatch, relative):
    root = tmp_path / relative
    write(root, "source.py", 'TOKEN = "synthetic-review-only-value"')
    module = scanner_module()

    def forbidden_read(path):
        raise AssertionError("Excluded root was read")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    with pytest.raises(ValueError, match="root_path_denied"):
        module.audit_tree(root)


def test_white_label_audit_marks_unreadable_directory_incomplete_and_cli_fails(tmp_path, monkeypatch, capsys):
    module = scanner_module()
    blocked = tmp_path / "blocked"
    write(tmp_path, "apps/visible.py", "devices = 4")
    write(blocked, "source.py", "price = 99")
    original = module.os.scandir

    def restricted_scandir(path):
        if Path(path) == blocked:
            raise PermissionError(13, "synthetic-private-error-not-for-output", str(blocked))
        return original(path)

    monkeypatch.setattr(module.os, "scandir", restricted_scandir)
    report = module.audit_tree(tmp_path)
    assert report["complete"] is False
    assert report["errors"] == [{"path": "blocked", "reason": "directory_enumeration_failed"}]
    assert "synthetic-private-error" not in json.dumps(report)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--root", str(tmp_path)])
    assert module.main() == 2
    assert "synthetic-private-error" not in capsys.readouterr().err


@pytest.mark.parametrize("region", ["tests", "migrations"])
def test_white_label_audit_prioritizes_secret_candidates_in_all_regions(tmp_path, region):
    write(tmp_path, f"{region}/leak.py", 'TOKEN = "synthetic-review-only-value"')
    report, _ = run_audit(tmp_path)
    credentials = [item for item in report["matches"] if item["category"] == "credential_candidate"]
    assert credentials
    assert all(item["severity"] == "Critical-candidate" for item in credentials)
    assert all(item["disposition"] == "redacted_candidate_requires_secret_review" for item in credentials)


@pytest.mark.parametrize(
    "url",
    [
        "https://telegram.org.attacker.net",
        "https://attacker.net/telegram.org",
        "https://attacker.net/?github.com",
    ],
)
def test_white_label_audit_does_not_trust_provider_substrings(tmp_path, url):
    write(tmp_path, "apps/links.py", f'URL = "{url}"')
    report, _ = run_audit(tmp_path)
    found = next(item for item in report["matches"] if item["category"] == "url")
    assert found["disposition"] == "deployment_identity_config_required"


@pytest.mark.parametrize(
    "url", ["https://telegram.org", "https://api.telegram.org/docs", "https://docs.rollypay.io/api"]
)
def test_white_label_audit_preserves_exact_provider_and_subdomain_references(tmp_path, url):
    write(tmp_path, "apps/links.py", f'URL = "{url}"')
    report, _ = run_audit(tmp_path)
    found = next(item for item in report["matches"] if item["category"] == "url")
    assert found["disposition"] == "provider_or_primary_reference_review"


@pytest.mark.parametrize(
    "literal", ["0x4", "0x_FF", "0b100", "0o4", "4_0", "5e1", "5E-1", "1_000.25", ".5", "4n"]
)
def test_white_label_audit_inventories_nondecimal_business_numeric_forms(tmp_path, literal):
    write(tmp_path, "apps/policy.py", f"included_devices = {literal}\n")
    report, _ = run_audit(tmp_path)
    found = [item for item in report["matches"] if item["category"] == "numeric_literal"]
    assert [(item["line"], item["column"], item["disposition"]) for item in found] == [
        (1, 20, "business_semantics_review")
    ]


def test_white_label_audit_bounds_business_analysis_per_line_without_changing_rows(tmp_path, monkeypatch):
    module = scanner_module()
    original = module.BUSINESS
    calls = []

    class CountedBusiness:
        def search(self, line):
            calls.append(None)
            return original.search(line)

    monkeypatch.setattr(module, "BUSINESS", CountedBusiness())
    numbers = ",".join(["123"] * 250)
    write(tmp_path, "apps/minified.js", f"const items=[{numbers}];\nconst price=[{numbers}];\n")
    report = module.audit_tree(tmp_path)
    found = [item for item in report["matches"] if item["category"] == "numeric_literal"]
    assert len(found) == 500
    assert all(item["disposition"] == "technical_numeric_review" for item in found[:250])
    assert all(item["disposition"] == "business_semantics_review" for item in found[250:])
    assert len(calls) == 2, "Full-line business analysis must not repeat for every literal"


def test_white_label_audit_bounds_namespace_analysis_per_line_without_changing_rows(tmp_path, monkeypatch):
    module = scanner_module()
    original = module.TECH_NAMESPACE
    calls = []

    class CountedNamespace:
        def finditer(self, line):
            calls.append(None)
            return original.finditer(line)

    monkeypatch.setattr(module, "TECH_NAMESPACE", CountedNamespace())
    write(tmp_path, "apps/minified.js", 'const text="' + "sunrise_core " * 150 + 'sunrise SHOP";')
    report = module.audit_tree(tmp_path)
    found = [item for item in report["matches"] if item["category"] == "brand"]
    assert len(found) == 151
    assert all(item["disposition"] == "technical_namespace" for item in found[:150])
    assert found[-1]["disposition"] == "instance_config_required"
    assert len(calls) == 1, "Full-line namespace parsing must not repeat for every brand"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("https://operator.xyz/?note=example.test", "url"),
        ("https://operatorlocalhost.attacker.net", "url"),
        ("https://127.0.0.1.attacker.net", "url"),
        ("https://attacker.net/example.dev", "url"),
        ("example@attacker.net", "email"),
    ],
)
def test_white_label_audit_synthetic_markers_do_not_hide_external_hosts(tmp_path, value, category):
    write(tmp_path, "apps/links.js", f'const value = "{value}";')
    report, _ = run_audit(tmp_path)
    found = next(item for item in report["matches"] if item["category"] == category)
    assert found["disposition"] == "deployment_identity_config_required"
    assert found["severity"] == "Important"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("https://localhost:3200/", "url"),
        ("https://127.9.8.7:3200/", "url"),
        ("https://docs.example.com/", "url"),
        ("customer@demo.test", "email"),
        ("https://operator.invalid/", "url"),
    ],
)
def test_white_label_audit_keeps_reserved_hosts_and_mailbox_domains_synthetic(tmp_path, value, category):
    write(tmp_path, "apps/links.js", f'const value = "{value}";')
    report, _ = run_audit(tmp_path)
    found = next(item for item in report["matches"] if item["category"] == category)
    assert found["disposition"] == "synthetic_or_loopback_example_review"
