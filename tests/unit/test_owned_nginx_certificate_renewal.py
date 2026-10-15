"""Синтетические проверки hook: никаких Docker, OpenSSL или сетевых вызовов."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def hook():
    path = Path(__file__).resolve().parents[2] / "infra/scripts/renew_owned_nginx_certificate.py"
    assert path.is_file(), "generic renewal hook is missing"
    spec = importlib.util.spec_from_file_location("owned_tls_renewal", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation(tmp_path, monkeypatch, hook):
    lineage, archive, state_dir = (tmp_path / name for name in ("live", "archive", "state"))
    for path in (lineage, archive, state_dir):
        path.mkdir()
    config_file = tmp_path / "nginx.conf"
    config_file.write_bytes(b"synthetic reviewed nginx configuration")
    targets = [tmp_path / "certificate.pem", tmp_path / "key.pem"]
    aliases = [tmp_path / "certificate-alias.pem", tmp_path / "key-alias.pem"]
    old, new = [b"old-certificate", b"old-private-key"], [b"new-certificate", b"new-private-key"]
    for path, value in zip(targets, old, strict=True):
        path.write_bytes(value)
    sources = [archive / "fullchain3.pem", archive / "privkey3.pem"]
    for path, value in zip(sources, new, strict=True):
        path.write_bytes(value)
    links = dict(zip(aliases, targets, strict=True))
    links.update({lineage / "fullchain.pem": sources[0], lineage / "privkey.pem": sources[1]})
    real_lstat, real_readlink = os.lstat, os.readlink

    def synthetic_lstat(path, *args, **kwargs):
        if Path(path) in links:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_dev=7, st_ino=100 + list(links).index(Path(path))
            )
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(hook.os, "lstat", synthetic_lstat)
    monkeypatch.setattr(
        hook.os,
        "readlink",
        lambda path, *a, **kw: (
            str(links[Path(path)]) if Path(path) in links else real_readlink(path, *a, **kw)
        ),
    )
    # Windows cannot express Linux root-only permission bits; exercise that guard separately.
    monkeypatch.setattr(hook, "private_path", lambda path, directory=False: None)
    monkeypatch.setattr(hook, "exclusive_lock", lambda path: nullcontext())
    # Native directory descriptors/fsync are exercised separately with synthetic Linux metadata.
    monkeypatch.setattr(hook, "sync_directory", lambda path: None, raising=False)

    def identity(path):
        info = path.stat()
        return {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}

    config = {
        "version": 1,
        "approved_lineage": str(lineage),
        "archive_dir": str(archive),
        "exact_sans": ["shop.example.test", "pay.example.test"],
        "minimum_valid_seconds": 86400,
        "docker": str(tmp_path / "docker"),
        "openssl": str(tmp_path / "openssl"),
        "private_state_dir": str(state_dir),
        "nginx": {
            "name": "synthetic-nginx",
            "id": "a" * 64,
            "config": {
                "host_path": str(config_file),
                "container_path": "/etc/nginx/owned.conf",
                "sha256": hashlib.sha256(config_file.read_bytes()).hexdigest(),
                **identity(config_file),
            },
        },
        "files": [],
    }
    for index, (alias, target) in enumerate(zip(aliases, targets, strict=True)):
        config["files"].append(
            {
                "alias_path": str(alias),
                "canonical_path": str(target),
                "container_path": ["/tls/cert.pem", "/tls/key.pem"][index],
                "alias_dev": 7,
                "alias_ino": 100 + index,
                **identity(target),
            }
        )
    calls, behavior = [], {}

    def command(arguments, data=None):
        calls.append(arguments)
        if arguments[1:2] == ["inspect"]:
            mounts = [{"Type": "bind", "Source": str(config_file), "Destination": "/etc/nginx/owned.conf"}]
            mounts.extend(
                {"Type": "bind", "Source": str(alias), "Destination": item["container_path"]}
                for alias, item in zip(aliases, config["files"], strict=True)
            )
            if behavior.get("foreign_mount"):
                mounts[1]["Source"] = str(tmp_path / "foreign.pem")
            return json.dumps(
                [
                    {
                        "Name": "/synthetic-nginx",
                        "Id": "a" * 64,
                        "State": {"Status": "running"},
                        "Mounts": mounts,
                    }
                ]
            ).encode()
        if arguments[1:4] == ["exec", "a" * 64, "cat"]:
            if arguments[4] == "/etc/nginx/owned.conf":
                return b"wrong-config" if behavior.get("wrong_active_config") else config_file.read_bytes()
            return targets[["/tls/cert.pem", "/tls/key.pem"].index(arguments[4])].read_bytes()
        if "subjectAltName" in arguments:
            return behavior.get("sans", b"DNS:shop.example.test, DNS:pay.example.test")
        if "-checkend" in arguments:
            if behavior.get("expired"):
                raise hook.HookError("command_failed")
            return b""
        if "-pubkey" in arguments or "-pubout" in arguments:
            return (
                b"wrong-public-key"
                if "-pubout" in arguments and behavior.get("wrong_pair")
                else b"public-key"
            )
        if arguments[-2:] == ["nginx", "-t"]:
            if behavior.get("nginx_failure"):
                raise hook.HookError("command_failed")
            return b""
        if arguments[-3:] == ["nginx", "-s", "reload"]:
            if behavior.get("reload_failure"):
                raise hook.HookError("command_failed")
            return b""
        raise AssertionError("unapproved synthetic command")

    return SimpleNamespace(
        hook=hook,
        config=config,
        lineage=lineage,
        archive=archive,
        targets=targets,
        aliases=aliases,
        old=old,
        new=new,
        links=links,
        calls=calls,
        behavior=behavior,
        command=command,
        state_dir=state_dir,
    )


def invoke(item, apply=True):
    return item.hook.renew(item.config, str(item.lineage), apply=apply, command=item.command)


def test_other_lineage_is_noop_without_commands_or_backups(installation):
    item = installation
    result = item.hook.renew(
        item.config, str(item.lineage.parent / "other"), apply=True, command=item.command
    )
    assert result["phase"] == "unrelated_lineage"
    assert item.calls == []
    assert list(item.state_dir.iterdir()) == []
    assert [p.read_bytes() for p in item.targets] == item.old


def test_dry_run_validates_without_writing_or_reload(installation):
    item = installation
    result = invoke(item, apply=False)
    assert result["phase"] == "validated_dry_run"
    assert [p.read_bytes() for p in item.targets] == item.old
    assert list(item.state_dir.iterdir()) == []
    assert not any("reload" in call for call in item.calls)


def test_install_preserves_bound_inode_mode_and_keeps_private_backup(installation):
    item = installation
    before = [(p.stat().st_dev, p.stat().st_ino, p.stat().st_mode) for p in item.targets]
    result = invoke(item)
    assert result["phase"] == "reload_accepted"
    assert [p.read_bytes() for p in item.targets] == item.new
    assert [(p.stat().st_dev, p.stat().st_ino, p.stat().st_mode) for p in item.targets] == before
    backup = next(item.state_dir.glob("renewal-*"))
    assert (backup / "original-certificate.pem").read_bytes() == item.old[0]
    assert (backup / "original-key.pem").read_bytes() == item.old[1]
    report = (backup / "result.json").read_text()
    assert "private-key" not in report
    test_index = next(i for i, call in enumerate(item.calls) if call[-2:] == ["nginx", "-t"])
    reload_index = next(i for i, call in enumerate(item.calls) if call[-3:] == ["nginx", "-s", "reload"])
    assert test_index < reload_index


@pytest.mark.parametrize("denial", ["foreign_mount", "wrong_active_config", "wrong_pair", "expired"])
def test_preflight_denial_never_writes_or_reloads(installation, denial):
    item = installation
    item.behavior[denial] = True
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert not any("reload" in call for call in item.calls)


def test_extra_san_is_denied(installation):
    item = installation
    item.behavior["sans"] = b"DNS:shop.example.test, DNS:pay.example.test, DNS:foreign.example.test"
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old


def test_nginx_validation_failure_restores_both_files_without_reload(installation):
    item = installation
    item.behavior["nginx_failure"] = True
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert failure.value.report["phase"] == "restored_before_reload"
    assert not any("reload" in call for call in item.calls)


def test_partial_write_recovers_current_and_previously_completed_files(installation, monkeypatch):
    item = installation
    original_write = item.hook.write_bytes
    failed = False

    def partial(handle, value):
        nonlocal failed
        if value == item.new[0] and not failed:
            failed = True
            handle.seek(0)
            handle.write(value[:3])
            handle.flush()
            raise OSError("synthetic interrupted certificate write")
        return original_write(handle, value)

    monkeypatch.setattr(item.hook, "write_bytes", partial)
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert not any("reload" in call for call in item.calls)


def test_persistent_recovery_error_never_claims_restored(installation, monkeypatch):
    item = installation
    original_fsync = item.hook.os.fsync
    calls = 0

    def broken_after_backups(fd):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise OSError("synthetic I/O failure")
        return original_fsync(fd)

    monkeypatch.setattr(item.hook.os, "fsync", broken_after_backups)
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert failure.value.report["phase"] == "restoration_incomplete"
    assert failure.value.report["applied"] is None
    assert not any("reload" in call for call in item.calls)


def test_reload_failure_retains_validated_bytes_and_reports_uncertainty(installation):
    item = installation
    item.behavior["reload_failure"] = True
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.new
    assert failure.value.report["phase"] == "reload_requested"
    assert failure.value.report["applied"] is True
    assert failure.value.report["nginx_reload_requested"] is True


def test_source_generation_can_advance_without_changing_destination_identity(installation):
    item = installation
    for source, value, name in zip(
        (item.lineage / "fullchain.pem", item.lineage / "privkey.pem"),
        item.new,
        ("fullchain4.pem", "privkey4.pem"),
        strict=True,
    ):
        path = item.archive / name
        path.write_bytes(value)
        item.links[source] = path
    assert invoke(item)["phase"] == "reload_accepted"


def test_source_outside_approved_archive_is_denied(installation):
    item = installation
    item.links[item.lineage / "privkey.pem"] = item.targets[1]
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old


def test_destination_generation_changed_is_denied(installation):
    item = installation
    item.config["files"][1]["ino"] += 1
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old


def test_destination_alias_changed_is_denied(installation):
    item = installation
    item.links[item.aliases[1]] = item.targets[0]
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old


def test_duplicate_config_keys_are_denied(hook, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"version":1,"version":1}')
    monkeypatch.setattr(hook, "private_path", lambda path, directory=False: None)
    original_fstat = hook.os.fstat

    def root_private(fd):
        info = original_fstat(fd)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(hook.os, "fstat", root_private)
    with pytest.raises(hook.HookError, match="configuration_duplicate_key"):
        hook.load_config(path)


def test_opened_config_must_still_be_root_private(installation, monkeypatch):
    item = installation
    path = item.state_dir / "configuration.json"
    path.write_text(json.dumps(item.config))
    original_fstat = item.hook.os.fstat

    def foreign_owner(fd):
        info = original_fstat(fd)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1, st_dev=info.st_dev, st_ino=info.st_ino)

    monkeypatch.setattr(item.hook.os, "fstat", foreign_owner)
    with pytest.raises(item.hook.HookError):
        item.hook.load_config(path)


def test_destination_replaced_during_preflight_denies_before_inode_mutation(installation, monkeypatch):
    item = installation
    original_command = item.command
    original_lstat = item.hook.os.lstat
    changed = False

    def replaced_generation(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if changed and Path(path) == item.targets[1]:
            return SimpleNamespace(
                st_mode=info.st_mode, st_uid=info.st_uid, st_dev=info.st_dev, st_ino=info.st_ino + 1
            )
        return info

    monkeypatch.setattr(item.hook.os, "lstat", replaced_generation)

    def replace_after_validation(arguments, data=None):
        nonlocal changed
        result = original_command(arguments, data)
        if "-checkend" in arguments:
            # Windows denies renaming an open FD; model the Linux pathname generation boundary.
            changed = True
        return result

    item.command = replace_after_validation
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert failure.value.report["phase"] == "preflight"
    assert list(item.state_dir.glob("renewal-*")) == []
    assert not any("reload" in call for call in item.calls)


def test_config_change_during_nginx_test_restores_without_reload(installation):
    item = installation
    original_command = item.command

    def change_after_nginx_test(arguments, data=None):
        result = original_command(arguments, data)
        if arguments[-2:] == ["nginx", "-t"]:
            Path(item.config["nginx"]["config"]["host_path"]).write_bytes(b"concurrent-config-change")
        return result

    item.command = change_after_nginx_test
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert failure.value.report["phase"] == "restoration_incomplete"
    assert not any("reload" in call for call in item.calls)


def test_changed_original_is_not_overwritten_and_owned_key_is_recovered(installation):
    item = installation
    original_command = item.command
    inspections = 0

    def competing_writer(arguments, data=None):
        nonlocal inspections
        result = original_command(arguments, data)
        if arguments[1:2] == ["inspect"]:
            inspections += 1
        if inspections == 2 and arguments[-2:] == ["cat", "/tls/key.pem"]:
            item.targets[0].write_bytes(b"other-writer-certificate")
        return result

    item.command = competing_writer
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert item.targets[0].read_bytes() == b"other-writer-certificate"
    assert item.targets[1].read_bytes() == item.old[1]
    assert failure.value.report["phase"] == "restoration_incomplete"
    assert not any("reload" in call for call in item.calls)


def test_signal_during_certificate_write_restores_both_files(installation, monkeypatch):
    item = installation
    original_write = item.hook.write_bytes

    def interrupted(handle, value):
        if value == item.new[0]:
            handle.seek(0)
            handle.write(value[:3])
            handle.flush()
            raise KeyboardInterrupt
        return original_write(handle, value)

    monkeypatch.setattr(item.hook, "write_bytes", interrupted)
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert failure.value.report["phase"] == "restored_before_reload"
    assert not any("reload" in call for call in item.calls)


@pytest.mark.parametrize(
    "sans",
    [
        b"DNS:shop.example.test, DNS:pay.example.test, IP Address:127.0.0.1",
        b"DNS:shop.example.test, DNS:pay.example.test, URI:https://other.example.test",
    ],
)
def test_non_dns_extra_san_is_denied(installation, sans):
    item = installation
    item.behavior["sans"] = sans
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old


def test_each_matching_invocation_retains_a_fresh_backup(installation):
    item = installation
    invoke(item)
    invoke(item)
    backups = list(item.state_dir.glob("renewal-*"))
    assert len(backups) == 2
    assert {p.joinpath("original-key.pem").read_bytes() for p in backups} == {item.old[1], item.new[1]}


def test_backup_files_and_directory_names_are_synced_before_first_destination_write(
    installation, monkeypatch
):
    item = installation
    events = []
    original_private_write, original_write = item.hook.private_write, item.hook.write_bytes
    first_write = True

    def private_file(path, value):
        original_private_write(path, value)
        events.append(("file", path.name))

    def directory_synced(path):
        events.append(("directory", path))

    def destination_write(handle, value):
        nonlocal first_write
        if first_write:
            first_write = False
            backup = next(item.state_dir.glob("renewal-*"))
            assert events == [
                ("file", "original-certificate.pem"),
                ("file", "original-key.pem"),
                ("file", "before-write.json"),
                ("directory", backup),
                ("directory", item.state_dir),
            ]
            assert [p.read_bytes() for p in item.targets] == item.old
            before_write = json.loads((backup / "before-write.json").read_text())
            assert before_write["applied"] is False
            assert "private-key" not in json.dumps(before_write)
        events.append(("destination", value))
        return original_write(handle, value)

    monkeypatch.setattr(item.hook, "private_write", private_file)
    monkeypatch.setattr(item.hook, "write_bytes", destination_write)
    monkeypatch.setattr(item.hook, "sync_directory", directory_synced)
    assert invoke(item)["phase"] == "reload_accepted"
    backup = next(item.state_dir.glob("renewal-*"))
    assert events[-3:] == [("file", "result.json"), ("directory", backup), ("directory", item.state_dir)]


def test_failed_backup_directory_sync_never_mutates_destination_or_reloads(installation, monkeypatch):
    item = installation

    def failed_sync(path):
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(item.hook, "sync_directory", failed_sync)
    with pytest.raises(item.hook.HookError) as failure:
        invoke(item)
    assert [p.read_bytes() for p in item.targets] == item.old
    assert failure.value.report["applied"] is False
    assert not any("reload" in call for call in item.calls)


@pytest.mark.parametrize("changed", [False, True])
def test_native_directory_fsync_requires_same_protected_directory_descriptor(
    hook, tmp_path, monkeypatch, changed
):
    before = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_dev=9, st_ino=17)
    held = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_dev=9, st_ino=18 if changed else 17)
    events = []
    monkeypatch.setattr(hook.os, "lstat", lambda path: before)
    monkeypatch.setattr(hook.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setattr(hook.os, "O_NOFOLLOW", 0x20000, raising=False)

    def open_directory(path, flags):
        assert flags & hook.os.O_DIRECTORY and flags & hook.os.O_NOFOLLOW
        return 123456

    monkeypatch.setattr(hook.os, "open", open_directory)
    monkeypatch.setattr(hook.os, "fstat", lambda fd: held)
    monkeypatch.setattr(hook.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(hook.os, "close", lambda fd: events.append(("close", fd)))
    if changed:
        with pytest.raises(hook.HookError, match="backup_directory_identity_denied"):
            hook.sync_directory(tmp_path)
        assert events == [("close", 123456)]
    else:
        hook.sync_directory(tmp_path)
        assert events == [("fsync", 123456), ("close", 123456)]


def test_existing_backup_directory_is_never_modified(installation, monkeypatch):
    item = installation
    existing = item.state_dir / ("renewal-" + "b" * 32)
    existing.mkdir()
    (existing / "preserved.txt").write_bytes(b"previous-owned-evidence")
    monkeypatch.setattr(item.hook, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    with pytest.raises(item.hook.HookError):
        invoke(item)
    assert {p.name for p in existing.iterdir()} == {"preserved.txt"}
    assert [p.read_bytes() for p in item.targets] == item.old


def test_external_command_failure_does_not_disclose_stderr_or_key(hook, monkeypatch):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"synthetic-secret-private-key-content"
        ),
    )
    with pytest.raises(hook.HookError) as failure:
        hook.run_command(["/synthetic/tool", "owned-command"], b"synthetic-private-key-input")
    assert str(failure.value) == "command_failed"
    assert "private-key" not in json.dumps(failure.value.report)


def test_competing_invocation_cannot_acquire_existing_private_lock(hook, tmp_path, monkeypatch):
    original_fstat = hook.os.fstat

    def root_private(fd):
        info = original_fstat(fd)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_dev=info.st_dev, st_ino=info.st_ino)

    def lock_busy(fd, operation):
        raise BlockingIOError("synthetic held advisory lock")

    monkeypatch.setattr(hook.os, "fstat", root_private)
    monkeypatch.setattr(hook.os, "O_NOFOLLOW", getattr(hook.os, "O_NOFOLLOW", 0), raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=lock_busy, LOCK_EX=1, LOCK_NB=4))
    with (
        pytest.raises(hook.HookError, match="renewal_already_running"),
        hook.exclusive_lock(tmp_path / "lock"),
    ):
        pytest.fail("a competing invocation acquired the lock")


@pytest.mark.parametrize(
    "mode,uid", [(stat.S_IFREG | 0o644, 0), (stat.S_IFREG | 0o600, 1), (stat.S_IFLNK | 0o777, 0)]
)
def test_config_permission_or_link_is_denied(hook, tmp_path, monkeypatch, mode, uid):
    monkeypatch.setattr(hook.os, "lstat", lambda path: SimpleNamespace(st_mode=mode, st_uid=uid))
    with pytest.raises(hook.HookError):
        hook.private_path(tmp_path / "config.json")
