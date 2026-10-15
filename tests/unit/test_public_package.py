"""Regression checks for source-only ZIP packaging; no application/providers needed."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/package_public_release.py"


def load_packer():
    spec = importlib.util.spec_from_file_location("public_package", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(root):
    # Public release contract: metadata, operating guides, examples and entrypoints.
    names = [
        ".dockerignore", ".env.example", ".gitignore", ".gitleaks.toml", "AGENTS.md",
        "CONTRIBUTING.md", "LICENSE", "NOTICE", "README.md", "README.en.md", "SECURITY.md",
        "package.json", "package-lock.json", "pyproject.toml", "requirements.lock",
        "tsconfig.json", "tsconfig.runtime.json", "vitest.config.ts",
        "config/bootstrap.demo.json", "config/bootstrap.example.json",
        "config/instance.example.json", "config/production-scope.example.json",
        "docs/ARCHITECTURE.md", "docs/BACKUP_RESTORE.md", "docs/CONFIGURATION.md",
        "docs/DEPLOYMENT.md", "docs/FEATURES.md", "docs/GITHUB_PUBLICATION.md",
        "docs/INSTALL_EN.md", "docs/INSTALL_RU.md", "docs/RELEASE_STATUS.md",
        "docs/THIRD_PARTY_NOTICES.md", "docs/TROUBLESHOOTING.md", "docs/UPDATING.md",
        "docs/FINAL_PUBLICATION_CHECK.md",
        ".agents/skills/create-readme/SKILL.md", ".agents/skills/humanizer/SKILL.md",
        ".agents/skills/PROVENANCE.md", "apps/api/src/sunrise_api/main.py",
        "apps/bot/src/sunrise_bot/runtime.py", "apps/worker/src/sunrise_worker/main.py",
        "apps/web/dashboard/index.html", "apps/web/landing/index.html",
        "packages/core/src/sunrise_core/pricing.py", "migrations/versions/example.sql",
        "tests/fixtures/schema.sql", "scripts/local.py", "infra/Dockerfile.standalone",
        "contracts/openapi.json",
        ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml", "docs/assets/social-preview.png",
    ]
    for name in names:
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe\n", encoding="utf-8")
    (root/".gitleaks.toml").write_bytes((ROOT/".gitleaks.toml").read_bytes())


class PublicPackageTests(unittest.TestCase):
    def test_reviewed_community_files_and_social_preview_are_included(self):
        module = load_packer()
        names = (
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            "docs/assets/social-preview.png",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            for name in names:
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe\n", encoding="utf-8")
            unreviewed = root/".github/workflows/unused.yml"
            unreviewed.parent.mkdir(parents=True)
            unreviewed.write_text("not reviewed\n", encoding="utf-8")
            selected = {p.as_posix() for p in module.select_files(root)}
            self.assertTrue(set(names).issubset(selected))
            self.assertNotIn(".github/workflows/unused.yml", selected)

    def test_cli_is_available_without_application_dependencies(self):
        result = subprocess.run(  # noqa: S603 -- fixed repository script; no shell or external input
            [sys.executable, str(SCRIPT), "--help"], capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dirty_tree_excludes_private_material_and_keeps_source_fixtures(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            forbidden = [
                ".env.docker", ".artifacts/frozen/runtime.env", "DELETE-ME/apps/old.py",
                "apps/api/.env", "apps/api/key.pem", "apps/api/server.log",
                "apps/api/customer-data/export.json", "config/instance.local.json",
                "tests/.artifacts/copy.py", "tests/__pycache__/old.pyc",
                "docs/OLD_PROGRESS.md", ".git/config", ".gitleaksignore",
                "tests/fixtures/Backups/customer.json", "apps/api/delete-me/customer.py",
                "apps/api/Customer-Data/export.py",
            ]
            for name in forbidden:
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not for publication\n", encoding="utf-8")
            selected = {p.as_posix() for p in module.select_files(root)}
            self.assertTrue(selected.isdisjoint(forbidden))
            self.assertTrue({".env.example", "requirements.lock", "package-lock.json",
                             "migrations/versions/example.sql", "tests/fixtures/schema.sql"} <= selected)

    def test_legal_notices_and_runtime_example_are_preserved(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            expected = {"packages/web-ui/fonts/TwemojiCountryFlags.LICENSE.md",
                        "packages/web-ui/reactbits/LICENSE.md", "packages/web-ui/practical-faq.example.json"}
            for name in expected:
                p = root/name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("safe\n", encoding="utf-8")
            self.assertTrue(expected <= {p.as_posix() for p in module.select_files(root)})

    def test_unsafe_archive_names_are_rejected_before_opening(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("apps/../escape.py", "apps/NUL.py", "apps/trailing. /safe.py", "apps/x:y.py"):
                with self.subTest(name=name), self.assertRaisesRegex(module.PackageError, "unsafe_path"):
                    module.checked_file(root, Path(name))

    def test_scanner_error_log_is_not_a_success(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            work = root/".artifacts/check"
            work.mkdir(parents=True)

            def failed_read(*args, **kwargs):
                (work/(root.name+"-gitleaks.json")).write_text("[]", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, b"", b"ERR failed reading file")

            with patch.object(module.subprocess, "run", side_effect=failed_read):
                with self.assertRaisesRegex(module.PackageError, "scan_failed"):
                    module.scan(root, Path(sys.executable), root/".gitleaks.toml", work)

    def test_changed_scanner_allowlist_is_rejected(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            p = root/".gitleaks.toml"
            p.write_text(p.read_text(encoding="utf-8")+'\n[allowlist]\npaths = [".*"]\n', encoding="utf-8")
            with self.assertRaisesRegex(module.PackageError, "scan_config"):
                module.scan(root, Path(sys.executable), p, root)

    def test_missing_required_file_refuses_incomplete_package(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            (root/"LICENSE").unlink()
            with self.assertRaisesRegex(module.PackageError, "required"):
                module.select_files(root)

    def test_hardlink_alias_is_rejected_before_reading(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            secret = root/".env.docker"
            secret.write_text("private fixture", encoding="utf-8")
            os.link(secret, root/"apps/api/alias.py")
            with self.assertRaisesRegex(module.PackageError, "alias"):
                module.select_files(root)

    def test_failed_scanner_leaves_no_archive(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            output = root/"release.zip"
            with patch.object(module, "scan", side_effect=module.PackageError("scan_failed")):
                with self.assertRaisesRegex(module.PackageError, "scan_failed"):
                    module.package_release(root, output, Path(sys.executable))
            self.assertFalse(output.exists())

    def test_existing_output_is_preserved(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            output = root/"release.zip"
            output.write_bytes(b"existing release")
            with self.assertRaisesRegex(module.PackageError, "exists"):
                module.package_release(root, output, Path(sys.executable))
            self.assertEqual(output.read_bytes(), b"existing release")

    def test_copy_error_removes_only_own_incomplete_output(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            output = root/"release.zip"
            with patch.object(module, "scan"), patch.object(module.shutil, "copyfileobj", side_effect=OSError):
                with self.assertRaises(OSError):
                    module.package_release(root, output, Path(sys.executable))
            self.assertFalse(output.exists())

    def test_output_created_during_scan_is_preserved(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            output = root/"release.zip"
            with patch.object(module, "scan", side_effect=lambda *args: output.write_bytes(b"foreign release")):
                with self.assertRaises(FileExistsError):
                    module.package_release(root, output, Path(sys.executable))
            self.assertEqual(output.read_bytes(), b"foreign release")

    def test_archive_is_verified_and_scanned_before_final_output(self):
        module = load_packer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            output = root/"release.zip"
            scanned = []

            def check_scan(stage, scanner, config, work):
                self.assertFalse(output.exists())
                if stage.name == "verified":
                    self.assertTrue((stage/"LICENSE").is_file())
                else:
                    self.assertEqual(stage.name, "binary-text")
                    self.assertTrue((stage/"docs/assets/social-preview.png.txt").is_file())
                scanned.append(stage)

            with patch.object(module, "scan", side_effect=check_scan):
                result = module.package_release(root, output, Path(sys.executable))
            self.assertEqual([stage.name for stage in scanned], ["verified", "binary-text"])
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("vless-vpn-shop-telegram-bot/LICENSE", archive.namelist())
            self.assertEqual(result["secret_scan"], "PASS")


if __name__ == "__main__":
    unittest.main()
