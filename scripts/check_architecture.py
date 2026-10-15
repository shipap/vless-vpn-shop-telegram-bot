"""Executable dependency, workspace, confidentiality and language checks."""

import ast
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUTER = PROJECT.parent
ALLOWED_OUTER = {
    "AGENTS.md",
    "SUNRISE-CODEX-PROMPT-EN.md",
    "SUNRISE-CODEX-PROMPT-V6-EN.md",
    "SUNRISE-CODEX-PROMPT-V7-EN.md",
    "WORKSPACE-ARCHITECTURE.md",
    "AGENT-ORCHESTRATION.md",
    "START-CODEX.txt",
    "START-HERE.md",
    "CURRENT-STATE.md",
    "NEXT-STEPS.md",
    "OWNER-DECISIONS.md",
    "VERIFICATION.md",
    "README.md",
    ".agents",
    ".codex",
    "sunrise",
    "vless-vpn-shop-telegram-bot",  # Owner-approved sanitized sibling edition.
    ".gitignore",
    ".git",
}
WORKSPACE_DOCUMENTS = (
    "AGENTS.md",
    "SUNRISE-CODEX-PROMPT-EN.md",
    "SUNRISE-CODEX-PROMPT-V6-EN.md",
    "SUNRISE-CODEX-PROMPT-V7-EN.md",
    "WORKSPACE-ARCHITECTURE.md",
    "AGENT-ORCHESTRATION.md",
)
WORKSPACE_SKILLS = (
    "legacy-db-safe-migration",
    "vpn-billing-integrity",
    "release-readiness-audit",
    "white-label-hardcode-audit",
)


def instruction_workspace(project=PROJECT):
    """Recognize owner instruction layout without treating any parent AGENTS as it."""
    project = Path(project).resolve()
    bootstrap = project / "AGENTS.md"
    if not bootstrap.is_file() or "../AGENTS.md" not in bootstrap.read_text(encoding="utf-8-sig"):
        return None
    outer = project.parent
    distinctive_prompt = any((outer / name).is_file() for name in WORKSPACE_DOCUMENTS[1:4])
    architecture_markers = all((outer / name).is_file() for name in WORKSPACE_DOCUMENTS[4:])
    # A partially damaged recognized workspace must still fail its required checks.
    return outer if distinctive_prompt or architecture_markers else None


@contextmanager
def git_command(project):
    """Check source archives with real Git without creating metadata in their tree."""
    project = Path(project).resolve()
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("Git не найден: установите Git и добавьте его в PATH")
    command = [git, "-c", f"core.excludesFile={os.devnull}"]
    if (project / ".git").exists():
        yield command
        return
    with tempfile.TemporaryDirectory(prefix="vpn-shop-architecture-") as metadata:
        subprocess.run(  # noqa: S603 -- resolved Git, isolated temporary metadata, no shell or remote
            [git, "init", "--bare", "--quiet", metadata], capture_output=True, check=True
        )
        yield [*command, f"--git-dir={metadata}", f"--work-tree={project}"]


def check(project=PROJECT):
    project = Path(project).resolve()
    errors = []
    outer = instruction_workspace(project)
    required = ["AGENTS.md"]
    if outer is not None:
        for path in outer.iterdir():
            if path.name not in ALLOWED_OUTER:
                errors.append(f"Прикладной файл во внешней папке: {path.name}")
        required.extend("../" + name for name in WORKSPACE_DOCUMENTS)
        required.extend(f".agents/skills/{skill}/SKILL.md" for skill in WORKSPACE_SKILLS)
    for relative in required:
        if not (project / relative).is_file():
            errors.append(f"Не обнаружены инструкции/skills: {relative}")
    core = project / "packages/core/src"
    banned = {
        "fastapi",
        "aiogram",
        "sqlalchemy",
        "redis",
        "httpx",
        "sunrise_api",
        "sunrise_bot",
        "sunrise_worker",
        "sunrise_infrastructure",
    }
    for path in core.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            imports = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            for imported in imports:
                if imported.split(".")[0] in banned:
                    errors.append(
                        f"Core нарушает зависимость: {path.relative_to(project)}:{node.lineno} {imported}"
                    )
    migration_roots = [
        path
        for path in project.glob("**/alembic.ini")
        if path.relative_to(project).parts[0] != ".artifacts"
        and not any(
            part in {".private", ".venv", "node_modules", ".agents", ".codex"}
            for part in path.relative_to(project).parts
        )
    ]
    if len(migration_roots) != 1:
        errors.append(f"Историй Alembic должно быть ровно одна, найдено {len(migration_roots)}")
    for root in [project / "apps/api", project / "apps/bot"]:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8-sig")
            if re.search(r"\b(?:create_all|drop_all)\s*\(", text):
                errors.append(f"Startup DDL в адаптере: {path.relative_to(project)}")
            for _found in re.finditer(
                r'(?:\.execute\s*\(\s*[f]?\s*[\'"]\s*(?:UPDATE|INSERT|DELETE)|SELECT\s+MAX\(.+\)\s*\+\s*1)',
                text,
                re.IGNORECASE,
            ):
                errors.append(f"Прямая SQL-мутация в адаптере: {path.relative_to(project)}")
    for path in (project / "apps/web").rglob("*"):
        if path.suffix not in {".tsx", ".ts"} or "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig")
        if "VITE_" in text and re.search(r"VITE_\w*(?:SECRET|PASSWORD|TOKEN|API_KEY)", text):
            errors.append(f"Публичная переменная секрета: {path.relative_to(project)}")
        if re.search(r"(?:Number|parseInt)\s*\(\s*(?:\w+\.)?(?:user_id|telegram_id|account_id)", text):
            errors.append(f"Потеря точности identity: {path.relative_to(project)}")
    try:
        with git_command(project) as command:
            for name in [".env", ".private/local-resources.json", "private-audit/DB-FINDINGS.md"]:
                result = subprocess.run(  # noqa: S603 -- resolved Git, fixed read-only argv, no shell
                    [*command, "check-ignore", "--quiet", name],
                    cwd=project,
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    errors.append(f"Приватный материал не исключён Git: {name}")
            result = subprocess.run(  # noqa: S603 -- resolved Git, fixed read-only argv, no shell
                [*command, "remote"], cwd=project, capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                errors.append("Не удалось проверить Git remotes")
            elif outer is not None and result.stdout.strip():
                errors.append("Remote не разрешён для этой локальной реализации")
    except (OSError, subprocess.CalledProcessError) as error:
        errors.append(f"Не удалось выполнить проверку Git: {error}")
    return errors


if __name__ == "__main__":
    errors = check()
    for error in errors:
        print("FAIL:", error)
    if not errors:
        print("PASS: границы зависимостей, инструкции репозитория, единые миграции, приватность")
        if instruction_workspace() is not None:
            print("PASS: внешний instruction workspace, инструкции/skills, запрет remote")
        else:
            print("NOT APPLICABLE: внешний instruction workspace и его частные runtime evidence")
    raise SystemExit(bool(errors))
