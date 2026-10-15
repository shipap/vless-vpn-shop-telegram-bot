import pytest

from tests import isolated_database as module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://synthetic@127.0.0.1:5432/sunrise_test_backend",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        "postgresql+psycopg://synthetic@production.example.test:55439/sunrise_test_backend",
    ],
)
def test_temporary_schema_rejects_unapproved_database_before_any_connection(monkeypatch, url):
    monkeypatch.setenv("SUNRISE_TEST_DATABASE_URL", url)
    monkeypatch.setattr(module, "Database", lambda *_: pytest.fail("Unapproved connection attempted"))
    with pytest.raises(AssertionError), module.isolated_database("boundary_test"):
        pytest.fail("Unapproved database yielded")
