"""Поиск потерянного ответа создания счёта допускает только чтение рабочего терминала."""

from types import SimpleNamespace

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.providers import RollyPay


def provider(environment, handler):
    policy = SimpleNamespace(
        production=environment == "live",
        scoped_payments=environment == "scoped_live",
        scope=SimpleNamespace(
            rollypay=SimpleNamespace(origin="https://bank.synthetic.test", terminal="synthetic-terminal")
        ),
        require=lambda *args: None,
    )
    return RollyPay(
        "https://bank.synthetic.test",
        "synthetic",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(handler),
        environment=environment,
        runtime_policy=policy,
    )


def test_unknown_invoice_search_reads_only_terminal_collection():
    requests = []
    client = provider(
        "live",
        lambda request: (
            requests.append(request)
            or httpx.Response(
                200, json={"items": [{"payment_id": "synthetic", "order_id": "own-order"}], "total": 1}
            )
        ),
    )
    result = client.list_payments("own-order")
    assert result == {"items": [{"payment_id": "synthetic", "order_id": "own-order"}], "total": 1}
    assert [(item.method, item.url.path, dict(item.url.params)) for item in requests] == [
        (
            "GET",
            "/api/v1/payments",
            {"terminal_id": "synthetic-terminal", "order_id": "own-order", "limit": "100"},
        )
    ]


@pytest.mark.parametrize("environment", ["sandbox", "scoped_live"])
def test_terminal_collection_read_does_not_expand_scoped_or_sandbox_permissions(environment):
    requests = []
    client = provider(environment, lambda request: requests.append(request) or httpx.Response(200, json={}))
    with pytest.raises(DomainError):
        client.list_payments("own-order")
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        {"items": [], "total": True},
        {"items": {}, "total": 0},
        {"items": [None], "total": 1},
        {"items": [], "total": -1},
        {"items": [], "total": 2},
        {},
        {"items": [{"order_id": "foreign"}], "total": 1},
    ],
)
def test_unknown_invoice_search_rejects_incomplete_or_malformed_collection(response):
    client = provider("live", lambda _: httpx.Response(200, json=response))
    with pytest.raises(DomainError):
        client.list_payments("own-order")
