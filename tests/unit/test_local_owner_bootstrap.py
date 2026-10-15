"""Local-target validation and CLI failures never expose credentials."""

import importlib
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError


def implementation():
    try:
        return importlib.import_module("scripts.bootstrap_local_owner")
    except ModuleNotFoundError:
        pytest.fail("Explicit offline first-owner setup is missing")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://localhost:55439/sunrise_test",
        "postgresql+psycopg://127.0.0.1:55439/sunrise_test_backend?options=-csearch_path=demo_instance_"
        + "a" * 32,
    ],
)
def test_owner_target_accepts_only_fixed_local_test_names_and_single_safe_schema(url):
    schema = implementation().validate_target(SimpleNamespace(local_test=True, database_url=url))
    assert schema == ("demo_instance_" + "a" * 32 if "options" in url else "public")


@pytest.mark.parametrize(
    "url,local",
    [
        ("postgresql+psycopg://localhost:55439/sunrise_test", False),
        ("postgresql+psycopg://remote.example.test:55439/sunrise_test", True),
        ("postgresql+psycopg://localhost:5432/sunrise_test", True),
        ("postgresql+psycopg://localhost:55439/production", True),
        ("sqlite:///local.db", True),
        ("postgresql+psycopg://localhost:55439/sunrise_test?options=-csearch_path=other", True),
        ("postgresql+psycopg://localhost:55439/sunrise_test?options=-csearch_path=public,other", True),
        ("postgresql+psycopg://localhost:55439/sunrise_test?sslmode=disable", True),
        ("invalid-dsn", True),
    ],
)
def test_owner_target_rejects_unsafe_targets_before_database_access(url, local):
    with pytest.raises(DomainError) as failure:
        implementation().validate_target(SimpleNamespace(local_test=local, database_url=url))
    assert failure.value.code == "owner_target_denied"


def test_cli_rejects_missing_confirmation_and_redacts_unexpected_errors(monkeypatch, capsys):
    module = implementation()
    called = []
    monkeypatch.setattr(module, "load_settings", lambda profile: called.append(profile))
    assert (
        module.main(
            ["--account-id", "51", "--email", "owner@example.test", "--reason", "Начальная настройка"]
        )
        == 2
    )
    assert not called

    def fail(profile):
        raise RuntimeError("postgresql://secret-user:secret-password@private.invalid/live")

    monkeypatch.setattr(module, "load_settings", fail)
    assert (
        module.main(
            [
                "--account-id",
                "51",
                "--email",
                "owner@example.test",
                "--confirm-email",
                "owner@example.test",
                "--confirm-first-owner",
                "--reason",
                "Начальная настройка",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "secret-password" not in output.err + output.out
    assert "private.invalid" not in output.err + output.out


def test_cli_parse_failure_never_echoes_unrecognized_secret_arguments(capsys):
    assert (
        implementation().main(["--dsn", "postgresql://synthetic-user:synthetic-secret@private.invalid/live"])
        == 2
    )
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.err + output.out
    assert "private.invalid" not in output.err + output.out
