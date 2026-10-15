"""Локальная настройка не перезаписывает секреты и не подключает провайдеров."""

import importlib.util
import os
import re
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


def installer():
    source = ROOT / "scripts/setup_local_docker.py"
    assert source.is_file(), "Нет безопасной настройки локального Docker-профиля"
    spec = importlib.util.spec_from_file_location("setup_local_docker", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_environment(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


class SetupLocalDockerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in (".gitignore", ".dockerignore"):
            (self.root / name).write_text(".env\n.env.*\n!.env.example\n", encoding="utf-8")

    def test_new_installation_generates_distinct_secrets_and_only_synthetic_targets(self):
        result = installer().prepare(self.root)
        self.assertTrue(result["created"])
        values = read_environment(self.root / ".env.docker")
        self.assertEqual(values["APP_NAME"], "VLESS VPN Shop")
        secrets = [values[key] for key in ("LOCAL_POSTGRES_PASSWORD", "LOCAL_REDIS_PASSWORD", "SUNRISE_SECRET_KEY")]
        self.assertEqual(len(set(secrets)), 3)
        self.assertTrue(all(re.fullmatch(r"[a-f0-9]{64}", value) for value in secrets))
        database = urlparse(values["SUNRISE_DATABASE_URL"])
        self.assertEqual((database.hostname, database.port, database.path), ("127.0.0.1", 55439, "/sunrise_test"))
        self.assertEqual(database.password, values["LOCAL_POSTGRES_PASSWORD"])
        redis = urlparse(values["SUNRISE_REDIS_URL"])
        self.assertEqual((redis.hostname, redis.port), ("127.0.0.1", 56379))
        self.assertEqual(redis.password, values["LOCAL_REDIS_PASSWORD"])
        self.assertEqual(values["SUNRISE_RUNTIME_MODE"], "isolated")
        self.assertEqual(values["SUNRISE_LOCAL_TEST"], "true")
        self.assertEqual(values["SUNRISE_EMAIL_ALLOWLIST"], "[]")
        self.assertEqual(values["SUNRISE_TEST_TELEGRAM_RECIPIENTS"], "")
        self.assertEqual(values["INSTANCE_CONFIG_PATH"], "")
        self.assertNotEqual(values["LOCAL_LANDING_PORT"], values["LOCAL_DASHBOARD_PORT"])
        for name, value in values.items():
            if name.endswith("_TEST_AUTHORIZED"):
                self.assertEqual(value, "false", name)
        if os.name != "nt":
            self.assertEqual((self.root / ".env.docker").stat().st_mode & 0o777, 0o600)

    def test_existing_owner_configuration_is_never_overwritten(self):
        target = self.root / ".env.docker"
        original = b"OWNER_CUSTOM_VALUE=keep-this-value\n"
        target.write_bytes(original)
        self.assertFalse(installer().prepare(self.root)["created"])
        self.assertEqual(target.read_bytes(), original)

    def test_shipped_ignore_files_allow_setup_without_unignoring_secrets(self):
        for name in (".gitignore", ".dockerignore"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        self.assertTrue(installer().prepare(self.root)["created"])

    def test_concurrent_setup_cannot_replace_the_first_secret_file(self):
        module = installer()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: module.prepare(self.root), range(2)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        values = read_environment(self.root / ".env.docker")
        self.assertEqual(len(values["SUNRISE_SECRET_KEY"]), 64)

    def test_missing_ignore_or_later_exception_denies_before_secret_write(self):
        for content in (
            ".env\n", ".env.*\n!.env.docker\n", ".env.*\n!.env.*\n",
            ".env.*\n!.env.docker \n", ".env.*\n!**/.env.docker\n",
        ):
            with self.subTest(content=content):
                (self.root / ".dockerignore").write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    installer().prepare(self.root)
                self.assertFalse((self.root / ".env.docker").exists())

    def test_directory_at_secret_path_is_rejected(self):
        (self.root / ".env.docker").mkdir()
        with self.assertRaises(ValueError):
            installer().prepare(self.root)

    def test_linked_secret_path_is_rejected_without_touching_its_target(self):
        original = self.root / "preserve.txt"
        original.write_text("preserve", encoding="utf-8")
        try:
            (self.root / ".env.docker").symlink_to(original)
        except OSError:
            self.skipTest("ОС не разрешает создание тестовых symbolic links")
        with self.assertRaises(ValueError):
            installer().prepare(self.root)
        self.assertEqual(original.read_text(encoding="utf-8"), "preserve")

    def test_invalid_or_identical_requested_ports_never_write_configuration(self):
        for ports in ((80, 3200), (3100, 65536), (3100, 3100)):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                installer().prepare(self.root, landing_port=ports[0], dashboard_port=ports[1])
            self.assertFalse((self.root / ".env.docker").exists())

    def test_an_occupied_explicit_port_is_rejected_before_secret_write(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(ValueError):
                installer().prepare(self.root, landing_port=listener.getsockname()[1])
        self.assertFalse((self.root / ".env.docker").exists())


if __name__ == "__main__":
    unittest.main()
