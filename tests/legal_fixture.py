"""Явная подготовка согласия только для синтетических сценариев до V6."""


def current_test_legal(commerce):
    assert commerce.db.engine.url.database == "sunrise_test_backend"
    documents = [
        {"document_type": "offer", "version": "fixture-v6", "url": "https://example.test/offer"},
        {"document_type": "privacy", "version": "fixture-v6", "url": "https://example.test/privacy"},
    ]
    commerce.legal.configure(None, documents, "Явная подготовка синтетического теста", only_if_missing=True)
    return commerce.legal.public()["documents"]


def accept_test_legal(commerce, account_id):
    commerce.accept_legal(account_id, current_test_legal(commerce), "website", "checkout")
