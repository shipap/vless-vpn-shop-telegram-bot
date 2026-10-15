"""Real read-only Git checks in synthetic standalone and instruction workspaces."""

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import check_architecture as architecture
from tests.architecture import test_boundaries as boundaries

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_DOCUMENTS = (
    "AGENTS.md",
    "SUNRISE-CODEX-PROMPT-EN.md",
    "SUNRISE-CODEX-PROMPT-V6-EN.md",
    "SUNRISE-CODEX-PROMPT-V7-EN.md",
    "WORKSPACE-ARCHITECTURE.md",
    "AGENT-ORCHESTRATION.md",
)
SKILLS = (
    "legacy-db-safe-migration",
    "vpn-billing-integrity",
    "release-readiness-audit",
    "white-label-hardcode-audit",
)


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def repository(root, *, origin=False, git=True):
    root.mkdir(parents=True)
    if git:
        subprocess.run(  # noqa: S603 -- new synthetic local repository, no remote access
            [shutil.which("git") or "git", "init", "--quiet", str(root)], check=True, capture_output=True
        )
    if origin:
        with (root / ".git/config").open("a", encoding="utf-8") as config:
            config.write('[remote "origin"]\nurl = https://example.test/portable/repository.git\n')
    for name in (".gitignore", ".dockerignore"):
        write(root, name, (ROOT / name).read_text(encoding="utf-8"))
    write(root, "AGENTS.md", "Synthetic repository instructions.\n")
    write(root, "packages/core/src/core/pricing.py", "from decimal import Decimal\n")
    write(root, "migrations/alembic.ini", "[alembic]\nscript_location = migrations\n")
    return root


@pytest.fixture
def standalone(tmp_path):
    write(tmp_path, "unrelated.py", "unrelated = True\n")
    write(tmp_path, "AGENTS.md", "Unrelated parent instructions.\n")
    write(tmp_path, "WORKSPACE-ARCHITECTURE.md", "An unrelated architecture document.\n")
    return repository(tmp_path / "portable-service", origin=True)


@pytest.fixture
def workspace(tmp_path):
    project = repository(tmp_path / "sunrise")
    write(project, "AGENTS.md", "Read the enclosing owner instructions at ../AGENTS.md.\n")
    for document in WORKSPACE_DOCUMENTS:
        write(tmp_path, document, "Synthetic instruction workspace marker.\n")
    for skill in SKILLS:
        write(project, f".agents/skills/{skill}/SKILL.md", "Synthetic local skill.\n")
    return project


def remote_names(project):
    result = subprocess.run(  # noqa: S603 -- fixed read-only Git argv, synthetic repository cwd
        [shutil.which("git") or "git", "remote"], cwd=project, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_standalone_clone_accepts_origin_unrelated_parent_and_no_private_evidence(standalone):
    assert not (standalone / ".artifacts/codex-capabilities.json").exists()
    assert remote_names(standalone) == "origin"
    assert architecture.check(standalone) == []
    assert remote_names(standalone) == "origin"


def test_generic_sibling_does_not_inherit_private_workspace_proof_requirements(standalone, monkeypatch):
    for document in WORKSPACE_DOCUMENTS:
        write(standalone.parent, document, "Synthetic enclosing owner instructions.\n")
    assert architecture.check(standalone) == []
    monkeypatch.setattr(boundaries, "ROOT", standalone)
    with pytest.raises(pytest.skip.Exception, match="instruction workspace"):
        boundaries.test_skills_discovered_by_real_client_in_each_launch_directory()


def test_source_archive_checks_real_ignore_rules_without_creating_target_git(tmp_path, monkeypatch):
    archive = repository(tmp_path / "source-archive", git=False)
    assert architecture.check(archive) == []
    monkeypatch.setattr(boundaries, "ROOT", archive)
    boundaries.test_private_resources_never_enter_git_or_image_context()
    assert not (archive / ".git").exists()
    ignore = archive / ".gitignore"
    ignore.write_text(ignore.read_text().replace(".env\n", ""), encoding="utf-8")
    assert any(".env" in error for error in architecture.check(archive))
    with pytest.raises(AssertionError, match=".env"):
        boundaries.test_private_resources_never_enter_git_or_image_context()
    assert not (archive / ".git").exists()


def test_missing_git_reports_an_actionable_check_failure(standalone, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert any("Git" in error for error in architecture.check(standalone))


def test_tracked_private_material_is_rejected_in_a_real_clone(standalone, monkeypatch):
    write(standalone, ".private/synthetic.txt", "Synthetic privacy regression fixture.\n")
    subprocess.run(  # noqa: S603 -- synthetic fixture index only; no commit, remote or real private data
        [shutil.which("git") or "git", "add", "--force", "--", ".private/synthetic.txt"],
        cwd=standalone,
        capture_output=True,
        check=True,
    )
    monkeypatch.setattr(boundaries, "ROOT", standalone)
    with pytest.raises(AssertionError):
        boundaries.test_private_resources_never_enter_git_or_image_context()


def test_archive_does_not_inherit_an_unrelated_parent_git_repository(tmp_path):
    parent = repository(tmp_path / "parent")
    archive = repository(parent / "export", git=False)
    (parent / ".gitignore").write_text(".env\n", encoding="utf-8")
    ignore = archive / ".gitignore"
    ignore.write_text(ignore.read_text().replace(".env\n", ""), encoding="utf-8")
    assert any(".env" in error for error in architecture.check(archive))
    assert not (archive / ".git").exists()


def test_generated_release_snapshot_is_not_a_second_migration_owner(standalone):
    write(standalone, ".artifacts/release-verification/migrations/alembic.ini", "[alembic]\n")
    assert architecture.check(standalone) == []
    write(standalone, "another-source/alembic.ini", "[alembic]\n")
    assert any("Историй Alembic" in error for error in architecture.check(standalone))


@pytest.mark.parametrize(
    "test_name",
    [
        "test_outer_workspace_contains_only_instructions_and_agent_config",
        "test_skills_discovered_by_real_client_in_each_launch_directory",
        "test_routing_configuration_uses_real_supported_model_effort_values",
    ],
)
def test_standalone_owner_runtime_checks_are_explicitly_not_applicable(standalone, monkeypatch, test_name):
    monkeypatch.setattr(boundaries, "ROOT", standalone)
    with pytest.raises(pytest.skip.Exception, match="instruction workspace"):
        getattr(boundaries, test_name)()


def test_standalone_privacy_checks_remain_active(standalone, monkeypatch):
    monkeypatch.setattr(boundaries, "ROOT", standalone)
    boundaries.test_private_resources_never_enter_git_or_image_context()
    ignore = standalone / ".gitignore"
    ignore.write_text(ignore.read_text().replace(".env\n", ""), encoding="utf-8")
    assert any(".env" in error for error in architecture.check(standalone))


@pytest.mark.parametrize(
    "path,content,expected",
    [
        ("packages/core/src/core/pricing.py", "import fastapi\n", "Core нарушает зависимость"),
        ("apps/api/src/app.py", "metadata.create_all(engine)\n", "Startup DDL"),
        (
            "apps/web/dashboard/config.ts",
            'const token = "VITE_PAYMENT_SECRET";',
            "Публичная переменная секрета",
        ),
        ("other/alembic.ini", "[alembic]\n", "Историй Alembic"),
    ],
)
def test_standalone_repository_boundaries_are_still_enforced(standalone, path, content, expected):
    write(standalone, path, content)
    assert any(expected in error for error in architecture.check(standalone))


def test_current_instruction_workspace_contract_remains_enforced(workspace):
    assert architecture.check(workspace) == []
    write(workspace.parent, "unexpected-app.py", "value = 1\n")
    assert any("внешней папке" in error for error in architecture.check(workspace))


def test_instruction_workspace_remote_remains_denied_without_modification(workspace):
    with (workspace / ".git/config").open("a", encoding="utf-8") as config:
        config.write('[remote "origin"]\nurl = https://example.test/owner/repository.git\n')
    assert any("Remote" in error for error in architecture.check(workspace))
    assert remote_names(workspace) == "origin"


@pytest.mark.parametrize("document", WORKSPACE_DOCUMENTS)
def test_partial_instruction_workspace_does_not_silently_become_standalone(workspace, document):
    (workspace.parent / document).unlink()
    assert any(document in error for error in architecture.check(workspace))


def test_instruction_workspace_missing_skill_still_fails(workspace):
    (workspace / ".agents/skills/white-label-hardcode-audit/SKILL.md").unlink()
    assert any("white-label-hardcode-audit" in error for error in architecture.check(workspace))


def test_instruction_workspace_missing_private_runtime_evidence_still_fails(workspace, monkeypatch):
    monkeypatch.setattr(boundaries, "ROOT", workspace)
    with pytest.raises(FileNotFoundError):
        boundaries.test_skills_discovered_by_real_client_in_each_launch_directory()


def test_actual_workspace_still_runs_current_instruction_rules():
    assert architecture.check(ROOT) == []
