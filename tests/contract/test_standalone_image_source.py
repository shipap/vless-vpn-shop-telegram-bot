"""Имитирует COPY реального Dockerfile и сверяет runtime manifest с файлами image."""

import importlib.util
import shlex
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_standalone_image_copies_every_runtime_source_record_to_the_exact_verified_path(tmp_path):
    spec = importlib.util.spec_from_file_location("scripts.install_production_instance", ROOT / "scripts/install_production_instance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This simulation covers COPY/source identity only. It does not claim that
    # image build, Linux permissions, dependencies or real PostgreSQL ran.
    copied = set()
    for line in (ROOT / "infra/Dockerfile.standalone").read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY "):
            continue
        *sources, destination = shlex.split(line)[1:]
        for source in sources:
            source_path = ROOT / source
            target = tmp_path / destination.removeprefix("./")
            if source_path.is_dir():
                shutil.copytree(source_path, target, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "node_modules"))
            else:
                if destination.endswith("/") or len(sources) > 1:
                    target /= source_path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
            copied.add(source)
    assert "scripts/render_production_runtime.py" in copied
    records = module.source_records(ROOT)
    assert records
    for record in records:
        actual = tmp_path / record["path"]
        assert actual.is_file(), record["path"]
        assert module.digest(actual.read_bytes()) == record["sha256"], record["path"]
