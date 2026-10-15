"""Готовит SQL из фактических expand-миграций без подключения к какой-либо БД."""

import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def render(versions: Path, *, since_revision: str | None = None) -> tuple[str, dict]:
    modules = []
    files = []
    previous = None
    for path in sorted(versions.glob("[0-9]*.py")):
        if path.is_symlink() or not path.resolve().is_relative_to(versions.resolve()):
            raise ValueError("migration chain path denied")
        spec = importlib.util.spec_from_file_location("closed_expand_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module.down_revision != previous or module.revision in [m.revision for m in modules]:
            raise ValueError("migration chain incomplete or ambiguous")
        if getattr(module, "depends_on", None) or getattr(module, "branch_labels", None):
            raise ValueError("migration chain has unsupported branches")
        modules.append(module)
        files.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        previous = module.revision
    if not modules:
        raise ValueError("migration chain empty")
    if since_revision is not None:
        revisions = [module.revision for module in modules]
        if since_revision not in revisions or since_revision == revisions[-1]:
            raise ValueError("migration starting revision unknown or already current")
        start = revisions.index(since_revision) + 1
        modules, files = modules[start:], files[start:]
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "literal_binds": True, "output_buffer": output}
    )
    output.write("BEGIN;\n")
    with Operations.context(context):
        if since_revision is None:
            Operations(context).create_table(
                "v5_alembic_version", sa.Column("version_num", sa.String(32), primary_key=True)
            )
        for index, module in enumerate(modules):
            module.upgrade()
            version = sa.table("v5_alembic_version", sa.column("version_num", sa.String(32)))
            statement = (
                sa.insert(version).values(version_num=module.revision)
                if index == 0 and since_revision is None
                else sa.update(version)
                .where(version.c.version_num == (since_revision if index == 0 else modules[index - 1].revision))
                .values(version_num=module.revision)
            )
            Operations(context).execute(statement)
    output.write("COMMIT;\n")
    # SQLAlchemy stores anonymous table indexes in a set. Sort only consecutive
    # independent CREATE INDEX statements; migration/data statement order stays intact.
    sql = re.sub(
        r"(?:CREATE (?:UNIQUE )?INDEX [^\n]+\n\s*){2,}",
        lambda match: "\n\n".join(sorted(match.group().strip().split("\n\n"))) + "\n\n",
        output.getvalue(),
    )
    return sql, {
        "mode": "offline_render_only",
        "from_revision": since_revision,
        "revisions": [module.revision for module in modules],
        "files": files,
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
    }


def main():
    root = Path(__file__).resolve().parents[1]
    sql, proof = render(root / "migrations" / "versions")
    destination = root / ".private" / "deployment" / "closed-expand.sql"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sql, encoding="utf-8", newline="\n")
    destination.with_suffix(".proof.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"mode": proof["mode"], "revisions": len(proof["revisions"]), "sql_sha256": proof["sql_sha256"]}))


if __name__ == "__main__":
    main()
