"""Exact complete group identity is required before routes can be refreshed."""

from types import SimpleNamespace

import httpx
import pytest
from sunrise_infrastructure import panel_event_routes
from sunrise_infrastructure.panel import PanelReview, RemnawaveTest
from sunrise_infrastructure.panel_event_routes import accessible_node_ids

NODE = "11111111-1111-4111-8111-111111111111"
SQUAD = "22222222-2222-4222-8222-222222222222"
PROFILE = "33333333-3333-4333-8333-333333333333"
INBOUND = "44444444-4444-4444-8444-444444444444"
FOREIGN = "55555555-5555-4555-8555-555555555555"


def host_inputs():
    accessible = {SQUAD: {"squadUuid": SQUAD, "accessibleNodes": [{
        "uuid": NODE, "configProfileUuid": PROFILE, "activeInbounds": ["entry-tag"],
    }]}}
    details = {SQUAD: {"uuid": SQUAD, "info": {"inboundsCount": 1}, "inbounds": [{
        "uuid": INBOUND, "profileUuid": PROFILE, "tag": "entry-tag",
    }]}}
    host = {
        "uuid": "66666666-6666-4666-8666-666666666666", "nodes": [NODE], "remark": "Финляндия",
        "isDisabled": False, "isHidden": False,
        "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": INBOUND},
        "internalSquads": {"mode": "EXCLUDE", "squads": []}, "address": "private.synthetic.invalid",
    }
    return accessible, details, host


def test_host_label_requires_exact_owned_node_profile_inbound_and_product_squad():
    accessible, details, host = host_inputs()
    assert panel_event_routes.host_node_labels(accessible, details, [host], {NODE}) == {NODE: "Финляндия"}
    for delta in (
        {"nodes": []}, {"nodes": [FOREIGN]}, {"isDisabled": True}, {"isHidden": True},
        {"inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": FOREIGN}},
        {"inbound": {"configProfileUuid": FOREIGN, "configProfileInboundUuid": INBOUND}},
        {"internalSquads": {"mode": "EXCLUDE", "squads": [SQUAD]}},
        {"internalSquads": {"mode": "ALLOW_ONLY", "squads": [FOREIGN]}},
    ):
        assert panel_event_routes.host_node_labels(accessible, details, [host | delta], {NODE}) == {}
    assert panel_event_routes.host_node_labels(accessible, details, [host | {"internalSquads": {
        "mode": "ALLOW_ONLY", "squads": [SQUAD],
    }}], {NODE}) == {NODE: "Финляндия"}


def test_host_labels_keep_all_safe_captions_and_reject_any_unsafe_candidate():
    accessible, details, host = host_inputs()
    second = host | {"uuid": FOREIGN, "remark": "Германия"}
    assert panel_event_routes.host_node_labels(accessible, details, [host, second], {NODE}) == {NODE: "Германия — Финляндия"}
    for remark in ("https://private.synthetic.invalid", "{USERNAME}", FOREIGN):
        second = host | {"uuid": FOREIGN, "remark": remark}
        assert panel_event_routes.host_node_labels(accessible, details, [host, second], {NODE}) == {}
    assert panel_event_routes.host_node_labels(accessible, details, [host, host | {"uuid": FOREIGN}], {NODE}) == {NODE: "Финляндия"}
    assert panel_event_routes.host_node_labels(accessible, details, [host | {"remark": "А" * 45}, host | {"uuid": FOREIGN, "remark": "Б" * 45}], {NODE}) == {}


def test_host_label_requires_eligibility_for_every_product_squad_on_node():
    accessible, details, host = host_inputs()
    accessible[FOREIGN] = accessible[SQUAD] | {"squadUuid": FOREIGN}
    details[FOREIGN] = details[SQUAD] | {"uuid": FOREIGN}
    host["internalSquads"] = {"mode": "ALLOW_ONLY", "squads": [SQUAD]}
    assert panel_event_routes.host_node_labels(accessible, details, [host], {NODE}) == {}


def test_host_caption_preserves_country_flag_and_ordinal_without_relaxing_private_data_guards():
    accessible, details, host = host_inputs()
    caption = "🇫🇮 Финляндия #1"
    assert panel_event_routes.host_node_labels(accessible, details, [host | {"remark": caption}], {NODE}) == {NODE: caption}
    for caption in ("🇫 Финляндия", "🇫🇮 #USERNAME", "🇫🇮 https://private.synthetic.invalid #1", "🇫🇮 {USERNAME} #1", "🇫🇮 " + FOREIGN):
        assert panel_event_routes.host_node_labels(accessible, details, [host | {"remark": caption}], {NODE}) == {}


def test_host_caption_never_displays_configured_address_even_when_plain_word():
    accessible, details, host = host_inputs()
    assert panel_event_routes.host_node_labels(accessible, details, [host | {"remark": "privatehost", "address": "privatehost"}], {NODE}) == {}


def test_array_envelope_is_supported_only_for_exact_hosts_get():
    with httpx.Client(base_url="https://panel.synthetic.invalid/api/", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": []})
    )) as client:
        panel = SimpleNamespace(client=client)
        assert RemnawaveTest._request(panel, "GET", "hosts") == []
        for method, path in (("GET", "nodes/" + NODE), ("GET", "hosts/" + NODE), ("POST", "hosts")):
            with pytest.raises(PanelReview):
                RemnawaveTest._request(panel, method, path)


def test_accessible_nodes_requires_exact_complete_group_identity():
    node = "11111111-1111-4111-8111-111111111111"
    squad = "22222222-2222-4222-8222-222222222222"
    assert accessible_node_ids({"squadUuid": squad, "accessibleNodes": [{"uuid": node}]}, squad) == {node}
    assert accessible_node_ids({"squadUuid": squad, "accessibleNodes": []}, squad) == set()


def test_accessible_nodes_rejects_partial_or_ambiguous_result():
    node = "11111111-1111-4111-8111-111111111111"
    squad = "22222222-2222-4222-8222-222222222222"
    for response in (
        {"squadUuid": squad},
        {"squadUuid": "33333333-3333-4333-8333-333333333333", "accessibleNodes": []},
        {"squadUuid": squad, "accessibleNodes": [{"uuid": node}, {"uuid": node}]},
        {"squadUuid": squad, "accessibleNodes": [{"uuid": "not-a-uuid"}]},
    ):
        try:
            accessible_node_ids(response, squad)
        except ValueError:
            pass
        else:
            raise AssertionError("untrusted group response was accepted")
