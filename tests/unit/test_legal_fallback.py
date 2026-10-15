from types import SimpleNamespace

import pytest
from sunrise_infrastructure.db import Database, Setting
from sunrise_infrastructure.legal import Legal

DOCUMENTS = [
    {"document_type": "offer", "version": "owner-v1", "url": "https://example.test/offer"},
    {"document_type": "privacy", "version": "owner-v1", "url": "https://example.test/privacy"},
]


@pytest.fixture
def legal():
    db = Database("sqlite+pysqlite:///:memory:")
    Setting.__table__.create(db.engine)
    yield Legal(SimpleNamespace(db=db))
    db.engine.dispose()


def assert_neutral_unconfigured(state):
    assert state["configured"] is False
    assert state["documents"] == [
        {"document_type": "offer", "version": "", "url": ""},
        {"document_type": "privacy", "version": "", "url": ""},
    ]
    assert isinstance(state["revision"], str) and state["revision"]


def test_missing_database_documents_return_neutral_state_without_writing(legal):
    state = legal.public()
    assert_neutral_unconfigured(state)
    state["documents"][0]["url"] = "https://example.test/response-only"
    assert_neutral_unconfigured(legal.public())
    with legal.db.transaction() as session:
        assert session.get(Setting, "legal") is None


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        42,
        "private-string",
        {},
        {"documents": None},
        {"documents": [DOCUMENTS[0]]},
        {"documents": [DOCUMENTS[0], DOCUMENTS[1] | {"version": ""}]},
        {"documents": [DOCUMENTS[0], DOCUMENTS[1] | {"url": "https://example.test/\nprivate"}]},
        {"documents": [DOCUMENTS[0], DOCUMENTS[1] | {"version": "v1\nprivate"}]},
    ],
)
def test_malformed_stored_row_is_neutral_and_preserved_without_read_repair(legal, stored):
    with legal.db.transaction() as session:
        session.add(Setting(key="legal", value=stored))
    assert_neutral_unconfigured(legal.public())
    with legal.db.transaction() as session:
        assert session.get(Setting, "legal").value == stored


def test_valid_database_documents_remain_authoritative_and_detached(legal):
    stored = {"documents": list(reversed(DOCUMENTS)), "owner_metadata": {"preserve": True}}
    with legal.db.transaction() as session:
        session.add(Setting(key="legal", value=stored))
    state = legal.public()
    assert state["configured"] is True
    assert state["documents"] == DOCUMENTS
    state["documents"][0]["version"] = "response-only"
    assert legal.public()["documents"] == DOCUMENTS
    with legal.db.transaction() as session:
        assert session.get(Setting, "legal").value == stored
