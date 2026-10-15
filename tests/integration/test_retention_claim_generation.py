"""A late response cannot replace reconciliation owned by a later cleanup claim."""

from datetime import timedelta

import httpx
from sunrise_infrastructure.db import Job, Setting

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement
from tests.integration.test_subscription_retention import due_retention as due_retention


def test_stale_cleanup_error_preserves_reclaimed_unknown_outcome(due_retention, monkeypatch):
    context, moment = due_retention
    db, panel, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    clock = {"now": moment, "reclaimed": False, "expected": None}
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: clock["now"])
    original = panel.client._transport

    def handler(request):
        if request.method == "GET" and not clock["reclaimed"]:
            clock["reclaimed"] = True
            clock["now"] += timedelta(minutes=3)
            assert commerce.retention.run_jobs() == 0
            with db.transaction() as session:
                marker = session.get(Setting, "retention:" + sid).value
                job = session.get(Job, marker["job_id"])
                assert marker["phase"] == "unknown" and marker["lease"] is None
                assert marker["attempted_at"] and job.attempts == 1
                clock["expected"] = (job.state, job.last_error, job.available_at, dict(marker))
            # The older GET completes malformed after a newer DELETE timed out.
            return httpx.Response(200, json={"response": {"invalid": "late snapshot"}})
        if request.method == "DELETE":
            state["calls"].append(("DELETE", request.url.path))
            raise httpx.ReadTimeout("synthetic delayed cleanup outcome")
        return original.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    assert commerce.retention.run_jobs() == 0
    with db.transaction() as session:
        marker = session.get(Setting, "retention:" + sid).value
        job = session.get(Job, marker["job_id"])
        assert (job.state, job.last_error, job.available_at, marker) == clock["expected"]
    state["user"] = None
    clock["now"] += timedelta(seconds=31)
    assert commerce.retention.run_jobs() == 1
    assert len([method for method, _ in state["calls"] if method == "DELETE"]) == 1
    with db.transaction() as session:
        marker = session.get(Setting, "retention:" + sid).value
        assert marker["phase"] == "archived"
        assert session.get(Job, marker["job_id"]).state == "done"
