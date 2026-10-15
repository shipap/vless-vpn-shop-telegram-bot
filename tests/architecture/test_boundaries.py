"""Cross-component safeguards; these must run against the actual assembled project."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_architecture import ALLOWED_OUTER, git_command, instruction_workspace

ROOT = Path(__file__).resolve().parents[2]


def require_instruction_workspace():
    outer = instruction_workspace(ROOT)
    if outer is None:
        pytest.skip("Private owner runtime evidence requires an enclosing instruction workspace")
    return outer


def test_importing_all_composition_modules_does_not_open_network_or_start_workers():
    source = """
import socket
def denied(*args, **kwargs):
    raise AssertionError('Import attempted network access')
socket.socket.connect = denied
socket.create_connection = denied
import sunrise_core.pricing
import sunrise_infrastructure.db
import sunrise_infrastructure.settings
import sunrise_infrastructure.accounts
import sunrise_infrastructure.commerce
import sunrise_infrastructure.providers
import sunrise_api.main
import sunrise_bot.runtime
import sunrise_worker.main
"""
    result = subprocess.run(  # noqa: S603 -- fixed local interpreter/Git argv; test-owned code, no shell input
        [sys.executable, "-c", source], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_actual_core_does_not_import_transports_or_concrete_infrastructure():
    banned = {
        "fastapi",
        "aiogram",
        "sqlalchemy",
        "httpx",
        "redis",
        "sunrise_infrastructure",
        "sunrise_api",
        "sunrise_bot",
        "sunrise_worker",
    }
    for path in (ROOT / "packages/core/src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            assert not any(name.split(".")[0] in banned for name in names), str(path.relative_to(ROOT))


def test_private_resources_never_enter_git_or_image_context():
    with git_command(ROOT) as command:
        for path in [
            ".env",
            ".private/local-resources.json",
            ".private/owner-access.json",
            "private-audit/DB-FINDINGS.md",
            ".artifacts/screenshots/test.png",
            "instance-assets/logo.svg",
            "config/instance.json",
            ".env.demo",
        ]:
            result = subprocess.run(  # noqa: S603 -- resolved Git, read-only argv, no shell
                [*command, "check-ignore", "--quiet", path], cwd=ROOT, capture_output=True
            )
            assert result.returncode == 0, path
        tracked = subprocess.run(  # noqa: S603 -- resolved local Git executable, fixed read-only argv
            [*command, "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    assert not any(
        path.startswith((".private/", "private-audit/", ".artifacts/")) or path == ".env" for path in tracked
    )
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    for path in [
        ".private",
        ".artifacts",
        "private-audit",
        ".env",
        "instance-assets",
        "config/instance.json",
    ]:
        assert path in patterns


def test_outer_workspace_contains_only_instructions_and_agent_config():
    outer = require_instruction_workspace()
    assert {path.name for path in outer.iterdir()} <= ALLOWED_OUTER


def test_skills_discovered_by_real_client_in_each_launch_directory():
    import json

    require_instruction_workspace()
    evidence = json.loads((ROOT / ".artifacts/codex-capabilities.json").read_text(encoding="utf-8"))
    assert evidence["initialize"] == "PASS"
    assert not evidence["limitations"]
    required = {
        "legacy-db-safe-migration",
        "vpn-billing-integrity",
        "security-best-practices",
        "security-threat-model",
    }
    assert len(evidence["skills"]) == 4
    for entry in evidence["skills"]:
        assert not entry["errors"]
        assert required <= {item["name"] for item in entry["skills"] if item["enabled"]}


def test_routing_configuration_uses_real_supported_model_effort_values():
    import json
    import tomllib

    outer = require_instruction_workspace()
    evidence = json.loads((ROOT / ".artifacts/codex-capabilities.json").read_text(encoding="utf-8"))
    models = {
        row["model"]: {effort["reasoningEffort"] for effort in row["supportedReasoningEfforts"]}
        for row in evidence["models"]
    }
    for path in (outer / ".codex/agents").glob("*.toml"):
        role = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        assert role["model"] in models, path.name
        assert role["model_reasoning_effort"] in models[role["model"]], path.name
        assert role.get("sandbox_mode") in {None, "read-only", "workspace-write"}
