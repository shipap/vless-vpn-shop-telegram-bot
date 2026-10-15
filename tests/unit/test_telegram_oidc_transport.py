import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.telegram_auth import TelegramOIDCTransport


def test_oidc_jwks_oversized_body_is_rejected_during_streaming_before_unbounded_download(monkeypatch):
    class CountingStream(httpx.SyncByteStream):
        def __init__(self):
            self.emitted = 0

        def __iter__(self):
            for _ in range(100):
                self.emitted += 1
                yield b" " * 16384

    stream = CountingStream()
    original = httpx.Client
    handler = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        "sunrise_infrastructure.telegram_auth.httpx.Client",
        lambda **kwargs: original(transport=handler, **kwargs),
    )
    with pytest.raises(DomainError) as error:
        TelegramOIDCTransport().keys()
    assert error.value.code == "telegram_auth_unavailable"
    assert stream.emitted <= 5
