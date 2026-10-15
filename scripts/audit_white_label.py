"""Офлайн-инвентарь переносимости: только координаты, без исходных значений/секретов.

Это консервативный поиск кандидатов, а не замена Gitleaks или проверка готовности.
Бинарные картинки учитываются метаданными: надписи/визуальный бренд требуют просмотра.
"""

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import sys
from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urlsplit

PRIVATE_DIRS = {".private", "private-audit", "instance-assets", ".local", ".git", ".codex", ".agents"}
DEPENDENCY_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
}
EVIDENCE_DIRS = {".artifacts", "test-results", "playwright-report"}
SENSITIVE_SUFFIXES = {
    ".dump",
    ".backup",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".zip",
    ".gz",
}
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".pdf",
}
RULES = (
    ("brand", re.compile(r"sunrise|bububi", re.I)),
    ("url", re.compile(r"\b(?:https?|postgresql(?:\+\w+)?|redis|smtp)://[^\s\"'<>`]+", re.I)),
    (
        "domain",
        re.compile(
            r"\b(?:[a-z0-9][a-z0-9-]*\.)+(?:icu|ru|com|io|org|net|dev|test|example|local|app|cloud|ph|co|vpn)\b",
            re.I,
        ),
    ),
    ("email", re.compile(r"\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.I)),
    ("telegram_username", re.compile(r"(?:https?://)?t\.me/[a-z][a-z0-9_]{3,}", re.I)),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("tariff_name", re.compile(r"\b(?:standard|universal)\b|Стандарт\w*|Универсальн\w*", re.I)),
    (
        "numeric_literal",
        re.compile(
            r"(?<![\w.])(?:0[xX]_?[0-9a-fA-F](?:_?[0-9a-fA-F])*"
            r"|0[bB]_?[01](?:_?[01])*|0[oO]_?[0-7](?:_?[0-7])*"
            r"|(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)"
            r"(?:[eE][+-]?\d(?:_?\d)*)?)(?:n)?"
            r"(?=(?:px|rem|em|vh|vw|vmin|vmax|ms|s|deg)\b|[^\w.]|$)"
        ),
    ),
    (
        "incomplete_marker",
        re.compile(
            r"\b(?:TODO|FIXME|XXX|HACK|NotImplementedError|placeholder|dummy|mock|stub|fake|pass|local_artifact_only)\b|заглуш\w*|не реализован\w*|в разработке",
            re.I,
        ),
    ),
    ("deployment_path", re.compile(r"[A-Z]:[\\/](?:Users|home)[\\/][^\s\"'<>]+", re.I)),
    (
        "credential_candidate",
        re.compile(
            r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|\b\w+://[^\s/@:'\"]+:[^\s/@'\"]+@",
            re.I,
        ),
    ),
    (
        "credential_candidate",
        re.compile(
            r"\b[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)[A-Za-z_]*[\"']?\s*[:=]\s*[\"'][^\"'\r\n]+[\"']",
            re.I,
        ),
    ),
)
BUSINESS = re.compile(
    r"price|amount|discount|bonus|devices|period|days|refund|traffic|loyalty|referral|цена|скидк|устройств|тариф",
    re.I,
)
TECH_NAMESPACE = re.compile(r"sunrise_(?:core|infrastructure|api|worker|bot)\b|sunrise-shop\b", re.I)
PROVIDER_HOSTS = (
    "telegram.org",
    "telegram.me",
    "rollypay.io",
    "cloudflare.com",
    "remnawave.com",
    "github.com",
    "githubusercontent.com",
    "npmjs.org",
    "python.org",
    "w3.org",
    "reactbits.dev",
)


def linked(path):
    return path.is_symlink() or path.is_junction()


def excluded_reason(path, root):
    relative = path.relative_to(root)
    name = path.name.casefold()
    if linked(path):
        return "link_not_followed"
    if name in PRIVATE_DIRS or re.fullmatch(r"instance-assets(?:\.[a-z0-9_-]{1,40})?", name):
        return "private_or_agent_region_not_read"
    if name in DEPENDENCY_DIRS or name.endswith(".egg-info"):
        return "dependency_or_cache_not_read"
    if name in EVIDENCE_DIRS:
        return "private_evidence_except_explicit_bundles"
    if name.startswith(".env") and name != ".env.example":
        return "runtime_environment_not_read"
    if name in {"instance.yaml", "instance.yml", "instance.json"} or (
        name.startswith("instance.") and name.endswith(".json") and name != "instance.example.json"
    ):
        return "runtime_instance_not_read"
    if (
        path.suffix.lower() in SENSITIVE_SUFFIXES
        or name.startswith("manual_backup_")
        or path.suffix.lower() == ".log"
    ):
        return "secret_backup_or_log_not_read"
    parts = {part.casefold() for part in relative.parts}
    if "playwright" in parts and ".auth" in parts:
        return "browser_auth_not_read"
    return None


def region_for(relative, bundle=False):
    if bundle or relative.parts[0] in {"dist", "build"}:
        return "bundle"
    if relative.parts[0] == "tests" or ".test." in relative.name or ".spec." in relative.name:
        return "test"
    if relative.parts[0] == "docs" or relative.suffix.lower() == ".md":
        return "documentation"
    if relative.parts[0] == "migrations":
        return "migration"
    if relative.parts[0] == "scripts":
        return "operational_script"
    if relative.parts[0] in {"config", "infra", "contracts"} or relative.name.startswith(".env"):
        return "configuration"
    return "runtime_source"


def disposition(category, line, token, relative, region, start, *, business_line=None, namespace_ranges=None):
    if category == "credential_candidate":
        return "redacted_candidate_requires_secret_review"
    if category == "brand":
        ranges = (
            [(found.start(), found.end()) for found in TECH_NAMESPACE.finditer(line)]
            if namespace_ranges is None
            else namespace_ranges
        )
        index = bisect_right(ranges, start, key=lambda item: item[0]) - 1
        if index >= 0 and start < ranges[index][1]:
            return "technical_namespace"
    if region == "test":
        return "synthetic_test_fixture_review"
    if region == "migration":
        return "immutable_migration_compatibility_review"
    if category == "brand_asset":
        return "private_asset_publication_review"
    if category == "numeric_literal":
        if relative.suffix.lower() in {".css", ".svg"}:
            return "presentation_constant"
        if "bootstrap" in relative.name:
            return "bootstrap_default_review"
        business = bool(BUSINESS.search(line)) if business_line is None else business_line
        return "business_semantics_review" if business else "technical_numeric_review"
    if category in {"url", "domain", "email", "telegram_username"}:
        try:
            if category in {"url", "telegram_username"}:
                hostname = urlsplit(token if "://" in token else "https://" + token).hostname
            elif category == "email":
                hostname = token.rpartition("@")[2].casefold()
            else:
                hostname = token.casefold()
        except ValueError:
            hostname = None
        if synthetic_hostname(hostname):
            return "synthetic_or_loopback_example_review"
        if category in {"url", "domain"}:
            if hostname and any(hostname == host or hostname.endswith("." + host) for host in PROVIDER_HOSTS):
                return "provider_or_primary_reference_review"
        return "deployment_identity_config_required"
    if category == "uuid":
        return "instance_resource_mapping_review"
    if category == "incomplete_marker":
        return "behavior_or_explicit_mock_gate_review"
    if category == "deployment_path":
        return "operator_path_portability_review"
    if region == "documentation":
        return "documentation_deployment_review"
    if category == "tariff_name":
        return "catalog_semantic_key_or_display_review"
    return "instance_config_required"


def synthetic_hostname(hostname):
    if not hostname:
        return False
    if hostname == "localhost" or hostname in {"example", "test", "invalid"}:
        return True
    if hostname.endswith((".test", ".invalid", ".example")) or any(
        hostname == host or hostname.endswith("." + host)
        for host in ("example.com", "example.org", "example.net")
    ):
        return True
    try:
        return IPv4Address(hostname).is_loopback
    except ValueError:
        return False


def severity(disposition_value, region):
    if disposition_value in {
        "technical_namespace",
        "presentation_constant",
        "synthetic_test_fixture_review",
        "immutable_migration_compatibility_review",
        "provider_or_primary_reference_review",
        "synthetic_or_loopback_example_review",
    }:
        return "Info"
    if disposition_value == "redacted_candidate_requires_secret_review":
        return "Critical-candidate"
    if disposition_value in {
        "instance_config_required",
        "private_asset_publication_review",
        "deployment_identity_config_required",
    }:
        return "Important"
    return "Review" if region != "bundle" else "Bundle-review"


def python_symbols(content):
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return [], "parse_failed_fallback_regex"
    symbols = [
        (node.lineno, node.end_lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    return sorted(symbols, key=lambda item: item[1] - item[0]), "python_ast_and_regex"


def inspect_file(path, root, report, *, bundle=False):
    relative = path.relative_to(root)
    region = region_for(relative, bundle)
    entry = {"path": relative.as_posix(), "region": region}
    try:
        entry["bytes"] = path.stat().st_size
    except OSError:
        entry["status"] = "stat_failed"
        report["files"].append(entry)
        record_error(report, relative, "file_stat_failed")
        return
    if path.suffix.lower() in BINARY_SUFFIXES:
        entry["status"] = "binary_metadata_only"
        report["files"].append(entry)
        if "brand" in relative.parts or "mascot" in path.name.lower():
            add_match(report, relative, region, 0, 0, "brand_asset", "private_asset_publication_review")
        return
    try:
        raw = path.read_bytes()
    except OSError:
        entry["status"] = "read_failed"
        report["files"].append(entry)
        record_error(report, relative, "file_read_failed")
        return
    if b"\x00" in raw:
        entry["status"] = "binary_metadata_only"
        report["files"].append(entry)
        return
    try:
        content = raw.decode("utf-8-sig")
        entry["encoding"] = "utf-8"
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
        entry["encoding"] = "utf-8_replacement_requires_review"
    entry["status"] = "scanned_text"
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    symbols, entry["analysis"] = python_symbols(content) if path.suffix == ".py" else ([], "regex")
    report["files"].append(entry)
    for number, line in enumerate(content.splitlines(), 1):
        symbol = next((name for first, last, name in symbols if first <= number <= last), None)
        business_line = bool(BUSINESS.search(line))
        namespace_ranges = [(found.start(), found.end()) for found in TECH_NAMESPACE.finditer(line)]
        for category, pattern in RULES:
            for match in pattern.finditer(line):
                decision = disposition(
                    category,
                    line,
                    match.group(),
                    relative,
                    region,
                    match.start(),
                    business_line=business_line,
                    namespace_ranges=namespace_ranges,
                )
                add_match(report, relative, region, number, match.start() + 1, category, decision, symbol)


def add_match(report, relative, region, line, column, category, decision, symbol=None):
    report["matches"].append(
        {
            "path": relative.as_posix(),
            "line": line,
            "column": column,
            "category": category,
            "disposition": decision,
            "severity": severity(decision, region),
            "region": region,
            "symbol": symbol,
            "evidence": "<redacted-match>",
        }
    )


def record_error(report, relative, reason):
    report["complete"] = False
    report["errors"].append({"path": relative.as_posix(), "reason": reason})


def walk_tree(start, root, report, *, bundle=False):
    def enumeration_failed(error):
        try:
            relative = Path(error.filename).relative_to(root)
        except (TypeError, ValueError):
            relative = Path(".")
        record_error(report, relative, "directory_enumeration_failed")

    for directory, dirs, files in os.walk(start, followlinks=False, onerror=enumeration_failed):
        current = Path(directory)
        for name in sorted(dirs[:]):
            child = current / name
            reason = excluded_reason(child, root)
            if reason:
                dirs.remove(name)
                report["excluded"].append({"path": child.relative_to(root).as_posix(), "reason": reason})
        dirs.sort()
        for name in sorted(files):
            child = current / name
            reason = excluded_reason(child, root)
            if reason:
                report["excluded"].append({"path": child.relative_to(root).as_posix(), "reason": reason})
            else:
                inspect_file(child, root, report, bundle=bundle)


def audit_tree(root, bundle_roots=()):
    root = Path(root).absolute()
    # Validate the supplied path before resolution and the canonical path afterwards.
    # An excluded region must never become an allowed root merely by changing --root.
    for candidate_root in (root, root.resolve(strict=True)):
        if not candidate_root.is_dir() or any(
            excluded_reason(part, part.parent) for part in (candidate_root, *candidate_root.parents)
        ):
            raise ValueError("root_path_denied")
    root = root.resolve(strict=True)
    approved = []
    for supplied in bundle_roots:
        candidate = Path(supplied).absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            raise ValueError("bundle_root_denied") from None
        # Never allow an explicit bundle option to override private/runtime exclusions.
        if not relative.parts or any(
            part.casefold() in PRIVATE_DIRS | DEPENDENCY_DIRS for part in relative.parts
        ):
            raise ValueError("bundle_root_denied")
        if relative.parts[0] not in {".artifacts", "dist", "build"} or candidate.resolve() != candidate:
            raise ValueError("bundle_root_denied")
        # Only known web build output, never an arbitrary private artifact directory.
        if relative.parts[0] == ".artifacts":
            legacy = relative.parts in {(".artifacts", "web", "dashboard"), (".artifacts", "web", "landing")}
            instance = (
                len(relative.parts) == 4
                and relative.parts[1] == "web"
                and re.fullmatch(r"[a-z0-9-]{1,48}", relative.parts[2]) is not None
                and relative.parts[3] in {"dashboard", "landing"}
            )
            if not legacy and not instance:
                raise ValueError("bundle_root_denied")
        if any(linked(root.joinpath(*relative.parts[:i])) for i in range(1, len(relative.parts) + 1)):
            raise ValueError("bundle_root_denied")
        if not candidate.is_dir() or candidate == root / ".artifacts":
            raise ValueError("bundle_root_denied")
        approved.append(candidate)
    report = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "files": [],
        "matches": [],
        "excluded": [],
        "complete": True,
        "errors": [],
        "limitations": [
            "binary_visual_identity_not_verified",
            "regex_candidates_not_confirmed_secrets",
            "excluded_private_contents_not_read",
            "git_history_and_tracking_not_verified",
            "dual_instance_runtime_not_run_by_this_scanner",
            "business_numeric_semantics_require_source_review",
        ],
    }
    walk_tree(root, root, report)
    for candidate in sorted(set(approved)):
        # dist/build already belong to the ordinary scan.
        if candidate.relative_to(root).parts[0] == ".artifacts":
            walk_tree(candidate, root, report, bundle=True)
    report["files"].sort(key=lambda item: item["path"])
    report["matches"].sort(key=lambda item: (item["path"], item["line"], item["column"], item["category"]))
    report["errors"].sort(key=lambda item: (item["path"], item["reason"]))
    report["counts"] = {
        "files": len(report["files"]),
        "matches": len(report["matches"]),
        "categories": dict(sorted(Counter(item["category"] for item in report["matches"]).items())),
        "dispositions": dict(sorted(Counter(item["disposition"] for item in report["matches"]).items())),
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bundle-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        root = args.root.absolute()
        output = (args.output or root / ".artifacts/v7-audit-inventory").absolute()
        if (
            output.parent != root / ".artifacts"
            or not output.name.startswith("v7-audit-")
            or linked(output.parent)
        ):
            raise ValueError("output_path_denied")
        report = audit_tree(root, args.bundle_root)
        output.parent.mkdir(exist_ok=True)
        json_path, csv_path = output.with_suffix(".json"), output.with_suffix(".csv")
        if any(linked(path) for path in (json_path, csv_path)):
            raise ValueError("output_path_denied")
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "path",
                    "line",
                    "column",
                    "category",
                    "disposition",
                    "severity",
                    "region",
                    "symbol",
                    "evidence",
                ],
            )
            writer.writeheader()
            writer.writerows(report["matches"])
    except (OSError, ValueError) as failure:
        # No original exception text: filesystem/provider-derived strings may be private.
        message = (
            str(failure)
            if str(failure) in {"output_path_denied", "bundle_root_denied", "root_path_denied"}
            else "audit_failed"
        )
        print(message, file=sys.stderr)
        return 2
    print(json.dumps(report["counts"], ensure_ascii=False))
    if not report["complete"]:
        print("audit_incomplete", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
