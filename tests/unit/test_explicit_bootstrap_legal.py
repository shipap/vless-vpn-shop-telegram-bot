"""Первичная настройка документов явная, сохраняет правки и не создаёт согласий."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure import bootstrap_config
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Base, Database, LegalConsent, Product, Setting


def configuration(**overrides):
    return SimpleNamespace(**({
        "legal_offer_url": "https://example.test/offer", "legal_offer_version": "offer-1",
        "legal_privacy_url": "https://example.test/privacy", "legal_privacy_version": "privacy-1",
    } | overrides))


@pytest.fixture
def database():
    result = Database("sqlite:///:memory:")

    @event.listens_for(result.engine, "connect")
    def synthetic_advisory_function(connection, _record):
        connection.create_function("pg_advisory_xact_lock", 1, lambda _key: None)

    Base.metadata.create_all(result.engine)
    try:
        yield result
    finally:
        result.engine.dispose()


def explicit_bootstrap(database, settings):
    assert hasattr(bootstrap_config, "bootstrap_instance"), "Явная инициализация документов отсутствует"
    return bootstrap_config.bootstrap_instance(database, settings)


def test_fresh_explicit_bootstrap_configures_legal_without_fabricating_consent(database):
    explicit_bootstrap(database, configuration())
    current = Commerce(database).legal.public()
    assert current["configured"] is True
    assert current["documents"] == [
        {"document_type": "offer", "url": "https://example.test/offer", "version": "offer-1"},
        {"document_type": "privacy", "url": "https://example.test/privacy", "version": "privacy-1"},
    ]
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(LegalConsent)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) > 0


def test_repeated_bootstrap_preserves_existing_owner_documents_and_catalog(database):
    explicit_bootstrap(database, configuration())
    with database.transaction() as session:
        session.get(Setting, "legal").value = {"documents": [
            {"document_type": "offer", "url": "https://owner.example.test/offer", "version": "owner-2"},
            {"document_type": "privacy", "url": "https://owner.example.test/privacy", "version": "owner-2"},
        ]}
        product = session.scalar(select(Product).order_by(Product.id))
        product.name = "Изменено владельцем"
        product_id = product.id
    explicit_bootstrap(database, configuration(legal_offer_version="new-env-version"))
    assert Commerce(database).legal.public()["documents"][0]["version"] == "owner-2"
    with database.transaction() as session:
        assert session.get(Product, product_id).name == "Изменено владельцем"


@pytest.mark.parametrize("override", [
    {"legal_privacy_version": ""}, {"legal_offer_url": "http://example.test/offer"},
    {"legal_offer_url": None}, {"legal_privacy_url": "https://user:secret@example.test/privacy"},
])
def test_partial_or_invalid_documents_fail_before_any_seed_write(database, override):
    with pytest.raises(DomainError):
        explicit_bootstrap(database, configuration(**override))
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 0
        assert session.scalar(select(func.count()).select_from(Setting)) == 0


def test_all_missing_documents_leave_safe_unconfigured_state(database):
    explicit_bootstrap(database, configuration(
        legal_offer_url=None, legal_offer_version="", legal_privacy_url=None, legal_privacy_version=""
    ))
    assert Commerce(database).legal.public()["configured"] is False
    with database.transaction() as session:
        assert session.get(Setting, "legal") is None
