import json

import httpx
import pytest
from test_test_integrations import BINDING, TARGET, panel, user

EXTERNAL = "90000000-0000-4000-8000-000000000009"


def test_external_squad_is_separately_allowlisted_and_observed():
    calls = []

    def handler(request):
        if request.method == "PATCH":
            calls.append(json.loads(request.content))
        return httpx.Response(200, json={"response": user(externalSquadUuid=EXTERNAL)})

    denied = panel(lambda _: pytest.fail("External mapping cannot bypass explicit allowlist"))
    with pytest.raises(ValueError):
        denied.apply(BINDING, TARGET | {"external_squad_uuid": EXTERNAL}, create=False)
    provider = panel(handler, allowed_external_squads={EXTERNAL})
    provider.apply(BINDING, TARGET | {"external_squad_uuid": EXTERNAL, "project_prefix": "sunrise_test_"})
    assert calls[0]["externalSquadUuid"] == EXTERNAL
    with pytest.raises(ValueError):
        provider.apply(BINDING, TARGET | {"project_prefix": "other_"})
