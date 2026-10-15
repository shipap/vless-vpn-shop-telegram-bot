"""Собрать проверенный публичный ZIP без локальных данных и зависимостей."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT_FILES = (
    ".dockerignore", ".env.example", ".gitignore", ".gitleaks.toml", "AGENTS.md",
    "CONTRIBUTING.md", "LICENSE", "NOTICE", "README.md", "README.en.md", "SECURITY.md",
    "package.json", "package-lock.json", "pyproject.toml", "requirements.lock",
    "tsconfig.json", "tsconfig.runtime.json", "vitest.config.ts",
)
DOCS = tuple(f"docs/{name}.md" for name in (
    "ARCHITECTURE", "BACKUP_RESTORE", "CONFIGURATION", "DEPLOYMENT", "FEATURES",
    "GITHUB_PUBLICATION", "INSTALL_EN", "INSTALL_RU", "RELEASE_STATUS",
    "THIRD_PARTY_NOTICES", "TROUBLESHOOTING", "UPDATING", "FINAL_PUBLICATION_CHECK",
))
EXAMPLES = tuple(f"config/{name}.json" for name in (
    "bootstrap.demo", "bootstrap.example", "instance.example", "production-scope.example",
))
SKILLS = (
    ".agents/skills/create-readme/SKILL.md", ".agents/skills/humanizer/SKILL.md",
    ".agents/skills/PROVENANCE.md",
)
PUBLIC_METADATA = (
    ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml", "docs/assets/social-preview.png",
)
ENTRYPOINTS = (
    "apps/api/src/sunrise_api/main.py", "apps/bot/src/sunrise_bot/runtime.py",
    "apps/worker/src/sunrise_worker/main.py", "apps/web/dashboard/index.html",
    "apps/web/landing/index.html",
)
TREES = ("apps", "packages", "scripts", "migrations", "tests", "infra", "contracts")
SKIP_PARTS = {
    ".artifacts", ".private", "private-audit", "DELETE-ME", ".git", ".venv", "venv",
    "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".hypothesis",
    ".next", ".cache", "dist", "build", "coverage", "test-results", "playwright-report",
    "customer-data", "customer_data", "backups", "dumps", "secrets", "credentials", "runtime",
}
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".toml", ".ini", ".conf", ".sh"}
BINARY_EXTENSIONS = {".png", ".woff2", ".mp4"}
DATA_FILES = {"contracts/openapi.json", "infra/compose.standalone.json", "infra/compose.local.yml"}
DATA_FILES.add("packages/web-ui/practical-faq.example.json")
# Reviewed narrow config; a rule change requires a new security review of this pin.
SCAN_CONFIG_SHA256 = "67ec3d933002de1dbe44cb511cfed106560c7d50b7af66f30b445ab237f3fc97"
MAX_FILE_BYTES = 16 * 1024 * 1024
PREFIX = "vless-vpn-shop-telegram-bot/"


class PackageError(ValueError):
    """Безопасная ошибка без содержимого файлов и секретов."""


def check_name(relative):
    if relative.is_absolute() or any(
        p in {".", ".."} or p.endswith((" ", "."))
        or any(c in p for c in '\\:<>"|?*') or any(ord(c)<32 for c in p)
        or p.split(".")[0].casefold() in {"con", "prn", "aux", "nul"}
        or re.fullmatch(r"(?:com|lpt)[1-9]", p.split(".")[0], flags=re.IGNORECASE)
        for p in relative.parts
    ):
        raise PackageError("public_package_unsafe_path")


def ignored(parts):
    return any(p.casefold() in {name.casefold() for name in SKIP_PARTS}
               or p.startswith(".") or p.casefold().endswith(".egg-info") for p in parts)


def checked_file(root, relative):
    check_name(relative)
    path = root/relative
    cursor = path
    while cursor != root:
        details = cursor.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 1024:
            raise PackageError("public_package_alias_rejected")
        cursor = cursor.parent
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise PackageError("public_package_alias_rejected")
    if details.st_size > MAX_FILE_BYTES:
        raise PackageError("public_package_file_too_large")
    return details


def source_path(relative):
    parts = relative.parts
    if ignored(parts):
        return False
    suffix = relative.suffix.lower()
    return (
        suffix in SOURCE_EXTENSIONS | BINARY_EXTENSIONS
        or relative.as_posix() in DATA_FILES
        or relative.name in {"LICENSE.md", "NOTICE.md", "README.md"} or relative.name.endswith(".LICENSE.md")
        or parts[0] == "infra" and relative.name.startswith("Dockerfile")
        or parts[:2] == ("tests", "fixtures") and suffix in {".sql", ".json", ".md"}
        or parts[0] == "migrations" and suffix == ".sql"
    )


def select_files(root):
    root = Path(root).resolve()
    required = ROOT_FILES + DOCS + EXAMPLES + SKILLS + ENTRYPOINTS + PUBLIC_METADATA
    selected = set()
    for name in required:
        if not (root/name).exists():
            raise PackageError("public_package_required_missing: " + name)
        checked_file(root, Path(name))
        selected.add(Path(name))
    for name in TREES:
        if not (root/name).is_dir():
            raise PackageError("public_package_required_tree_missing: " + name)
        stack = [Path(name)]
        while stack:
            relative = stack.pop()
            details = (root/relative).lstat()
            if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 1024:
                raise PackageError("public_package_alias_rejected")
            with os.scandir(root/relative) as entries:
                for entry in entries:
                    child = relative/entry.name
                    if ignored(child.parts):
                        continue
                    details = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 1024:
                        raise PackageError("public_package_alias_rejected")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(child)
                    elif source_path(child):
                        checked_file(root, child)
                        selected.add(child)
    screenshots = root/"docs/assets/screenshots"
    if screenshots.is_dir():
        for path in screenshots.glob("*.png"):
            relative = path.relative_to(root)
            checked_file(root, relative)
            selected.add(relative)
    names = [p.as_posix().casefold() for p in selected]
    if len(names) != len(set(names)):
        raise PackageError("public_package_case_collision")
    return sorted(selected, key=lambda p: p.as_posix())


def scan(stage, scanner, config, work):
    normalized_config = config.read_text(encoding="utf-8-sig").encode("utf-8")
    if hashlib.sha256(normalized_config).hexdigest() != SCAN_CONFIG_SHA256:
        raise PackageError("public_package_scan_config_changed")
    report = work/(stage.name + "-gitleaks.json")
    ignore = work/"empty.gitleaksignore"
    ignore.write_text("", encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("GITLEAKS_")}
    command = [
        str(scanner), "dir", ".", "--config", str(config), "--redact", "--no-banner",
        "--ignore-gitleaks-allow", "--gitleaks-ignore-path", str(ignore),
        "--max-target-megabytes", "0", "--exit-code", "1", "--timeout", "120", "--log-level", "error",
        "--report-format", "json", "--report-path", str(report),
    ]
    try:
        result = subprocess.run(command, cwd=stage, env=environment, capture_output=True, timeout=150)  # noqa: S603 -- explicit local scanner, no shell
        if result.returncode != 0 or result.stderr.strip() or not report.is_file() or json.loads(report.read_text(encoding="utf-8")) != []:
            raise PackageError("public_package_secret_scan_failed")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise PackageError("public_package_scanner_unavailable") from error


def package_release(root, output, scanner):
    root = Path(root).resolve()
    output = Path(output).absolute()
    scanner = Path(scanner).absolute()
    if output.exists():
        raise PackageError("public_package_output_exists")
    if not scanner.is_file():
        raise PackageError("public_package_scanner_required")
    if not output.parent.is_dir():
        raise PackageError("public_package_output_parent_missing")
    paths = select_files(root)
    scratch = root/".artifacts"
    if scratch.is_symlink() or scratch.is_junction():
        raise PackageError("public_package_alias_rejected")
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="public-package-", dir=scratch) as temporary:
        work = Path(temporary)
        archive_path = work/"candidate.zip"
        digests = {}
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative in paths:
                before = checked_file(root, relative)
                data = (root/relative).read_bytes()
                after = checked_file(root, relative)
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise PackageError("public_package_source_changed")
                if relative.suffix.lower() not in BINARY_EXTENSIONS:
                    try:
                        data.decode("utf-8-sig")
                    except UnicodeDecodeError as error:
                        raise PackageError("public_package_unexpected_binary") from error
                    if b"\x00" in data:
                        raise PackageError("public_package_unexpected_binary")
                name = relative.as_posix()
                digests[name] = hashlib.sha256(data).hexdigest()
                info = zipfile.ZipInfo(PREFIX + name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if relative.suffix == ".sh" else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compresslevel=9)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        extracted = work/"verified"
        binary_text = work/"binary-text"
        extracted.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None or archive.namelist() != [PREFIX+p.as_posix() for p in paths]:
                raise PackageError("public_package_integrity_failed")
            for relative in paths:
                name = relative.as_posix()
                data = archive.read(PREFIX + name)
                if hashlib.sha256(data).hexdigest() != digests[name]:
                    raise PackageError("public_package_integrity_failed")
                target = extracted/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if relative.suffix.lower() in BINARY_EXTENSIONS:
                    projection = binary_text/(name + ".txt")
                    projection.parent.mkdir(parents=True, exist_ok=True)
                    projection.write_bytes(b"\n".join(re.findall(rb"[\x20-\x7e]{4,}", data)))
        config = extracted/".gitleaks.toml"
        scan(extracted, scanner, config, work)
        if binary_text.exists():
            scan(binary_text, scanner, config, work)
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != digest:
            raise PackageError("public_package_integrity_failed")
        # Exclusive creation preserves an existing release even if it appears during scanning.
        owned = None
        try:
            with output.open("xb") as destination, archive_path.open("rb") as source:
                owned = os.fstat(destination.fileno())
                shutil.copyfileobj(source, destination)
            with output.open("rb") as published:
                if hashlib.file_digest(published, "sha256").hexdigest() != digest:
                    raise PackageError("public_package_integrity_failed")
        except BaseException:
            # Remove only the incomplete file exclusively created by this invocation.
            if owned is not None:
                try:
                    current = output.lstat()
                    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        output.unlink()
                except FileNotFoundError:
                    pass
            raise
    return {"output":str(output), "files":len(paths), "bytes":output.stat().st_size,
            "sha256":digest, "secret_scan":"PASS"}


def main():
    parser = argparse.ArgumentParser(description="Публичный ZIP: allowlist, проверка целостности и обязательный Gitleaks.")
    parser.add_argument("--output", type=Path, required=True, help="Новый ZIP; существующий файл не перезаписывается")
    parser.add_argument("--gitleaks", type=Path, help="Проверенный локальный executable Gitleaks 8.x")
    args = parser.parse_args()
    scanner = args.gitleaks or shutil.which("gitleaks")
    if not scanner:
        parser.exit(1, "public_package_scanner_required\n")
    try:
        result = package_release(Path(__file__).resolve().parents[1], args.output, scanner)
    except (PackageError, OSError) as error:
        # Scanner stdout/stderr and source contents are deliberately not printed.
        message = str(error) if isinstance(error, PackageError) else type(error).__name__
        parser.exit(1, message + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
