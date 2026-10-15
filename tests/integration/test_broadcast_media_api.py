"""The actual raw-body route retains session, CSRF, MFA and bounded private storage."""

from io import BytesIO

import pyotp
from PIL import Image
from sunrise_infrastructure.broadcast_media import BroadcastMediaStore

from tests.integration.test_broadcast_media_registry import png
from tests.integration.test_v10_admin_mfa import enroll
from tests.integration.test_v10_admin_mfa import mfa_context as mfa_context


def test_raw_media_upload_requires_verified_staff_csrf_mfa_and_exact_bounds(
    mfa_context, tmp_path, monkeypatch
):
    _, _, client, _, _, _, _ = mfa_context
    store = BroadcastMediaStore(tmp_path / "private-media", signing_key=b"s" * 32)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: store
    )
    route = "/api/v1/admin/broadcasts/media"
    assert client.post(route, content=png(), headers={"Content-Type": "image/png"}).status_code == 404
    auth, headers, pending = enroll(mfa_context)
    assert (
        client.post(route, content=png(), headers={**headers, "Content-Type": "image/png"}).status_code == 403
    )
    verified = client.post(
        "/api/v1/admin/mfa/enroll/confirm",
        headers=headers,
        json={"code": pyotp.TOTP(pending["secret"]).now()},
    )
    assert verified.status_code == 200
    assert client.post(route, content=png(), headers={"Content-Type": "image/png"}).status_code == 403
    result = client.post(route, content=png(), headers={**headers, "Content-Type": "image/png"})
    assert result.status_code == 200
    reference = result.json()
    assert set(reference) == {"asset_id", "sha256", "mime", "size_bytes", "proof"}
    assert client.get(route + "/" + reference["asset_id"]).json() == reference
    assert client.get(route + "/" + "0" * 32).status_code == 404
    image = BytesIO()
    Image.new("RGB", (256, 256), "red").save(image, format="PNG", compress_level=0)
    large_png = image.getvalue()
    assert 65_536 < len(large_png) < 2_000_000
    large_result = client.post(route, content=large_png, headers={**headers, "Content-Type": "image/png"})
    assert large_result.status_code == 200
    assert 0 < large_result.json()["size_bytes"] <= len(large_png)
    assert (
        client.post(
            "/api/v1/admin/broadcasts/estimate",
            content=large_png,
            headers={**headers, "Content-Type": "image/png"},
        ).status_code
        == 413
    )
    assert (
        client.post(route, content=b"bad", headers={**headers, "Content-Type": "image/png"}).status_code
        == 400
    )
    assert (
        client.post(
            route, content=b"x" * 2_000_001, headers={**headers, "Content-Type": "image/png"}
        ).status_code
        == 413
    )
    assert (
        client.post(route, content=png(), headers={**headers, "Content-Type": "text/html"}).status_code == 400
    )
