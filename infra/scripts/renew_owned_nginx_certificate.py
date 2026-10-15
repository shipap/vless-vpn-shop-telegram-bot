"""Certbot deploy hook: проверенные bind mounts обновляются без замены inode.

Запуск: python renew_owned_nginx_certificate.py --config /absolute/config.json --apply.
Без --apply выполняется только проверка. Конфигурацию и регистрацию hook готовит владелец.
JSON v1 содержит approved_lineage, archive_dir, exact_sans, minimum_valid_seconds,
docker, openssl, private_state_dir, nginx {name, id, config} и files [certificate, key].
config: host_path, container_path, sha256, dev, ino, mode.
files: alias_path, canonical_path, container_path, alias_dev, alias_ino, dev, ino, mode.
Секретные байты допустимы только в закрытых резервных PEM-файлах, никогда в JSON.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4


class HookError(Exception):
    def __init__(self, code, report=None):
        super().__init__(code)
        self.report = report or {
            "ok": False,
            "phase": "preflight",
            "reason": code,
            "applied": False,
            "nginx_reload_requested": False,
        }


def private_path(path, directory=False):
    info = os.lstat(path)
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise HookError("private_permissions_denied")


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise HookError("configuration_fields_denied")


def absolute_path(value):
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or not Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise HookError("configuration_path_denied")
    return Path(value)


def container_path(value):
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or not PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
    ):
        raise HookError("configuration_container_path_denied")


def validate_config(config):
    keys(
        config,
        "version approved_lineage archive_dir exact_sans minimum_valid_seconds "
        "docker openssl private_state_dir nginx files",
    )
    if type(config["version"]) is not int or config["version"] != 1:
        raise HookError("configuration_version_denied")
    for name in ("approved_lineage", "archive_dir", "docker", "openssl", "private_state_dir"):
        absolute_path(config[name])
    sans = config["exact_sans"]
    if (
        not isinstance(sans, list)
        or not sans
        or len(set(sans)) != len(sans)
        or any(
            not isinstance(name, str)
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name
            )
            for name in sans
        )
    ):
        raise HookError("configuration_sans_denied")
    if type(config["minimum_valid_seconds"]) is not int or config["minimum_valid_seconds"] < 1:
        raise HookError("configuration_expiry_denied")
    nginx = config["nginx"]
    keys(nginx, "name id config")
    if (
        not isinstance(nginx["name"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", nginx["name"])
        or not isinstance(nginx["id"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", nginx["id"])
    ):
        raise HookError("configuration_container_denied")
    row = nginx["config"]
    keys(row, "host_path container_path sha256 dev ino mode")
    if not isinstance(row["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
        raise HookError("configuration_hash_denied")
    absolute_path(row["host_path"])
    container_path(row["container_path"])
    identity_fields(row)
    if not isinstance(config["files"], list) or len(config["files"]) != 2:
        raise HookError("configuration_files_denied")
    used = {str(absolute_path(row["host_path"])), str(absolute_path(config["private_state_dir"]))}
    destinations = {row["container_path"]}
    for row in config["files"]:
        keys(row, "alias_path canonical_path container_path alias_dev alias_ino dev ino mode")
        identity_fields(row)
        for name in ("alias_dev", "alias_ino"):
            if type(row[name]) is not int or row[name] < 0:
                raise HookError("configuration_identity_denied")
        alias, target = absolute_path(row["alias_path"]), absolute_path(row["canonical_path"])
        container_path(row["container_path"])
        if alias.parent != target.parent or str(alias) in used or str(target) in used or alias == target:
            raise HookError("configuration_destination_denied")
        if row["container_path"] in destinations:
            raise HookError("configuration_mount_denied")
        used.update((str(alias), str(target)))
        destinations.add(row["container_path"])


def identity_fields(row):
    if (
        any(type(row[name]) is not int or row[name] < 0 for name in ("dev", "ino", "mode"))
        or row["mode"] > 0o777
    ):
        raise HookError("configuration_identity_denied")


def load_config(path):
    path = absolute_path(str(path))
    before = os.lstat(path)
    private_path(path)

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HookError("configuration_duplicate_key")
            result[key] = value
        return result

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        held = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != 0
            or stat.S_IMODE(held.st_mode) & 0o077
            or (held.st_dev, held.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise HookError("configuration_identity_denied")
        config = json.load(handle, object_pairs_hook=unique_pairs)
    validate_config(config)
    return config


@contextmanager
def exclusive_lock(path):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise HookError("lock_identity_denied")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HookError("renewal_already_running") from None
        yield
    finally:
        os.close(fd)


def run_command(arguments, data=None):
    try:
        result = subprocess.run(arguments, input=data, capture_output=True, check=False, timeout=30)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        raise HookError("command_failed") from None
    if result.returncode:
        # stderr может содержать приватные пути; наружу выходят только статические коды.
        raise HookError("command_failed")
    return result.stdout


def link_identity(alias, target, dev=None, ino=None):
    info = os.lstat(alias)
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
        raise HookError("alias_identity_denied")
    raw = Path(os.readlink(alias))
    resolved = (raw if raw.is_absolute() else alias.parent / raw).resolve(strict=True)
    if resolved != target or (dev is not None and (info.st_dev, info.st_ino) != (dev, ino)):
        raise HookError("alias_target_denied")
    return info.st_dev, info.st_ino


def regular_identity(path, expected=None):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or path.resolve(strict=True) != path:
        raise HookError("regular_target_denied")
    identity = info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)
    if expected is not None and identity != (expected["dev"], expected["ino"], expected["mode"]):
        raise HookError("target_identity_denied")
    return identity


def open_regular(stack, path, identity, writable=False):
    fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0))
    handle = stack.enter_context(os.fdopen(fd, "r+b" if writable else "rb"))
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)) != identity
    ):
        raise HookError("opened_identity_denied")
    return handle


def source_pair(config, stack):
    lineage, archive = Path(config["approved_lineage"]), Path(config["archive_dir"])
    for directory in (lineage, archive):
        if (
            directory.resolve(strict=True) != directory
            or not directory.is_dir()
            or directory.stat().st_uid != 0
        ):
            raise HookError("lineage_directory_denied")
    values, generations = [], []
    for stem in ("fullchain", "privkey"):
        alias = lineage / (stem + ".pem")
        raw = Path(os.readlink(alias))
        target = (raw if raw.is_absolute() else alias.parent / raw).resolve(strict=True)
        match = re.fullmatch(stem + r"([1-9][0-9]*)\.pem", target.name)
        if target.parent != archive or match is None:
            raise HookError("archive_family_denied")
        link_identity(alias, target)
        handle = open_regular(stack, target, regular_identity(target))
        values.append(handle.read())
        generations.append(match[1])
    if generations[0] != generations[1]:
        raise HookError("archive_generation_denied")
    return values


def write_bytes(handle, value):
    handle.seek(0)
    if handle.write(value) != len(value):
        raise HookError("short_write")
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())
    handle.seek(0)
    if handle.read() != value:
        raise HookError("written_bytes_denied")


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        if handle.write(value) != len(value):
            raise HookError("backup_short_write")
        handle.flush()
        os.fsync(handle.fileno())
    private_path(path)


def sync_directory(path):
    private_path(path, directory=True)
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        held = os.fstat(fd)
        if (
            not stat.S_ISDIR(held.st_mode)
            or held.st_uid != 0
            or stat.S_IMODE(held.st_mode) & 0o077
            or (held.st_dev, held.st_ino, held.st_mode) != (before.st_dev, before.st_ino, before.st_mode)
        ):
            raise HookError("backup_directory_identity_denied")
        os.fsync(fd)
    finally:
        os.close(fd)


def scope(config, command, values):
    docker, nginx = config["docker"], config["nginx"]
    observed = json.loads(command([docker, "inspect", nginx["id"]]))
    if (
        len(observed) != 1
        or observed[0]["Id"] != nginx["id"]
        or observed[0]["Name"] != "/" + nginx["name"]
        or observed[0]["State"]["Status"] != "running"
    ):
        raise HookError("container_identity_denied")
    mounts = observed[0]["Mounts"]
    config_row = nginx["config"]
    path = Path(config_row["host_path"])
    regular_identity(path, config_row)
    host = path.read_bytes()
    if hashlib.sha256(host).hexdigest() != config_row["sha256"]:
        raise HookError("host_configuration_denied")
    checks = [(config_row["container_path"], str(path), host)]
    for row, value in zip(config["files"], values, strict=True):
        target = Path(row["canonical_path"])
        regular_identity(target, row)
        link_identity(Path(row["alias_path"]), target, row["alias_dev"], row["alias_ino"])
        checks.append((row["container_path"], row["alias_path"], value))
    for destination, source, value in checks:
        matching = [item for item in mounts if item["Destination"] == destination]
        if (
            len(matching) != 1
            or matching[0]["Type"] != "bind"
            or matching[0]["Source"] != source
            or command([docker, "exec", nginx["id"], "cat", destination]) != value
        ):
            raise HookError("mounted_scope_denied")


def validate_pair(config, command, values):
    openssl = config["openssl"]
    certificate = command([openssl, "x509", "-pubkey", "-noout"], values[0])
    key = command([openssl, "pkey", "-pubout"], values[1])
    extension = command([openssl, "x509", "-noout", "-ext", "subjectAltName"], values[0]).decode("ascii")
    body = re.sub(r"^X509v3 Subject Alternative Name:[^\n]*\n", "", extension.strip()).strip()
    entries = [entry.strip() for entry in body.split(",")]
    names = {entry[4:] for entry in entries if entry.startswith("DNS:")}
    if (
        certificate != key
        or not certificate
        or names != set(config["exact_sans"])
        or any(not re.fullmatch(r"DNS:[^,\s]+", entry) for entry in entries)
    ):
        raise HookError("replacement_pair_denied")
    command([openssl, "x509", "-checkend", str(config["minimum_valid_seconds"]), "-noout"], values[0])


def renew(config, renewed_lineage, *, apply=False, command=run_command):
    validate_config(config)
    report = {"ok": True, "phase": "unrelated_lineage", "applied": False, "nginx_reload_requested": False}
    if renewed_lineage != config["approved_lineage"]:
        return report
    report["phase"] = "preflight"
    backup = None
    state_dir = Path(config["private_state_dir"])
    try:
        private_path(state_dir, directory=True)
        if state_dir.resolve(strict=True) != state_dir:
            raise HookError("state_directory_denied")
        with exclusive_lock(state_dir / "renewal.lock"), ExitStack() as stack:
            handles = [
                open_regular(
                    stack,
                    Path(row["canonical_path"]),
                    regular_identity(Path(row["canonical_path"]), row),
                    writable=apply,
                )
                for row in config["files"]
            ]
            originals = [handle.read() for handle in handles]
            replacements = source_pair(config, stack)
            scope(config, command, originals)
            validate_pair(config, command, replacements)
            scope(config, command, originals)
            if not apply:
                report["phase"] = "validated_dry_run"
                return report
            fresh_backup = state_dir / ("renewal-" + uuid4().hex)
            fresh_backup.mkdir(mode=0o700)
            private_path(fresh_backup, directory=True)
            backup = fresh_backup
            for filename, value in zip(
                ("original-certificate.pem", "original-key.pem"), originals, strict=True
            ):
                private_write(backup / filename, value)
            report["phase"] = "backup_prepared"
            private_write(backup / "before-write.json", json.dumps(report, ensure_ascii=False).encode())
            sync_directory(backup)
            sync_directory(state_dir)
            report.update(phase="writing", applied=None)
            touched = []
            try:
                for index in (1, 0):
                    handles[index].seek(0)
                    if handles[index].read() != originals[index]:
                        raise HookError("original_bytes_changed")
                    touched.append(index)
                    write_bytes(handles[index], replacements[index])
                scope(config, command, replacements)
                command([config["docker"], "exec", config["nginx"]["id"], "nginx", "-t"])
                scope(config, command, replacements)
            except BaseException:
                recovered = True
                for index in reversed(touched):
                    try:
                        write_bytes(handles[index], originals[index])
                    except BaseException:  # noqa: BLE001 -- attempt both original FD recoveries even after a signal
                        recovered = False
                try:
                    scope(config, command, originals)
                except BaseException:  # noqa: BLE001 -- never claim verified recovery after any scope failure
                    recovered = False
                report.update(
                    phase="restored_before_reload" if recovered else "restoration_incomplete",
                    applied=False if recovered else None,
                )
                raise
            report.update(phase="reload_requested", applied=True, nginx_reload_requested=True)
            command([config["docker"], "exec", config["nginx"]["id"], "nginx", "-s", "reload"])
            report["phase"] = "reload_accepted"
            private_write(backup / "result.json", json.dumps(report, ensure_ascii=False).encode())
            sync_directory(backup)
            sync_directory(state_dir)
            return report
    except BaseException:  # noqa: BLE001 -- redact all failures and preserve the actual mutation/reload stage
        report.update(ok=False, reason="owned_renewal_failed")
        if backup is not None and not (backup / "result.json").exists():
            try:
                private_write(backup / "result.json", json.dumps(report, ensure_ascii=False).encode())
                sync_directory(backup)
                sync_directory(state_dir)
            except (OSError, HookError):
                pass
        raise HookError("owned_renewal_failed", report) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверенное обновление сертификатных bind mounts nginx.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise HookError("linux_root_required")
        config = load_config(Path(args.config))
        report = renew(config, os.environ.get("RENEWED_LINEAGE"), apply=args.apply)
    except (HookError, OSError, ValueError, TypeError, KeyError) as error:
        report = (
            error.report
            if isinstance(error, HookError)
            else {
                "ok": False,
                "phase": "preflight",
                "reason": "configuration_or_scope_denied",
                "applied": False,
                "nginx_reload_requested": False,
            }
        )
        print(json.dumps(report))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise HookError("renewal_interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    raise SystemExit(main())
