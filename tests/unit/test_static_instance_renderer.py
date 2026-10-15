"""Настоящий Node-renderer не публикует секреты и не заменяет чужой output."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node 24+ must be installed and available on PATH")


def run_renderer(tmp_path, instance, license_files=None):
    source = tmp_path / "built"
    for profile in ("landing", "dashboard"):
        target = source / profile
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text('<html><head><title>Neutral</title></head><body></body></html>', encoding="utf-8")
        (target / "assets").mkdir(exist_ok=True)
        (target / "assets/app.js").write_text("window.fixture=true;", encoding="utf-8")
        for name, body in (license_files or {}).items():
            destination = target / "licenses" / name
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(body, encoding="utf-8")
    config = tmp_path / "public-instance.json"
    config.write_text(json.dumps(instance), encoding="utf-8")
    return subprocess.run([NODE, str(ROOT / "scripts/render_static_instance.ts"), str(source),  # noqa: S603 -- fixed script, synthetic paths
                           str(tmp_path / "output"), str(config)], capture_output=True, check=False)


def test_node_renderer_uses_allowlisted_public_values_and_hashes_inline_scripts(tmp_path):
    value = {"app_name": "Example </script> shop", "visual_profile": "neutral", "secret_key": "never-publish-this"}
    result = run_renderer(tmp_path, value)
    assert result.returncode == 0, result.stderr.decode()
    html = (tmp_path / "output/dashboard/index.html").read_text(encoding="utf-8")
    assert "never-publish-this" not in html
    assert "\\u003c/script\\u003e" in html
    assert html.count("window.__PUBLIC_INSTANCE__=") == 1
    csp = (tmp_path / "output/dashboard/csp.conf").read_text()
    assert "'sha256-" in csp and "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    before = (tmp_path / "output/receipt.json").read_bytes()
    assert run_renderer(tmp_path, value).returncode == 0
    assert (tmp_path / "output/receipt.json").read_bytes() == before
    assert run_renderer(tmp_path, dict(value, app_name="Changed shop")).returncode != 0
    assert (tmp_path / "output/dashboard/index.html").read_text(encoding="utf-8") == html


def test_node_renderer_refuses_existing_unowned_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "unrelated.txt").write_text("preserve", encoding="utf-8")
    assert run_renderer(tmp_path, {"app_name": "Example", "visual_profile": "neutral"}).returncode != 0
    assert (output / "unrelated.txt").read_text() == "preserve"


def test_node_renderer_preserves_only_the_named_distribution_notices(tmp_path):
    documents = {"AGPL-3.0.txt": "License text", "NOTICE.txt": "Copyright shipap",
                 "THIRD_PARTY_NOTICES.txt": "Third-party terms"}
    value = {"app_name": "Example", "visual_profile": "neutral"}
    assert run_renderer(tmp_path, value, documents).returncode == 0
    for profile in ("landing", "dashboard"):
        for name, body in documents.items():
            assert (tmp_path / "output" / profile / "licenses" / name).read_text(encoding="utf-8") == body
    assert run_renderer(tmp_path, value, {"customer-data.txt": "must not publish"}).returncode != 0
    assert not (tmp_path / "output/dashboard/licenses/customer-data.txt").exists()


def test_static_build_configuration_has_no_backend_fetch_plugin_or_public_environment():
    script = '''import configure from './infra/vite.static.config.ts';
const config=await configure({command:'build', mode:'dashboard'});
console.log(JSON.stringify({plugins:config.plugins.map(p=>p.name),prefix:config.envPrefix,output:config.build.outDir}));'''
    result = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, check=False)  # noqa: S603 -- fixed config evaluation
    assert result.returncode == 0, result.stderr.decode()
    config = json.loads(result.stdout)
    assert "instance-public-metadata" not in config["plugins"]
    assert config["prefix"] == []
    assert config["output"].endswith(".artifacts/web/production-static/dashboard")
