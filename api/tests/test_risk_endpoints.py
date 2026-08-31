"""The fraud rails, as the dashboard sees them."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from api.main import app


def test_the_rails_score_the_live_session():
    """The endpoints are only useful if the rails are actually attached.

    A summary of zeroes would pass a shape assertion while telling nobody that
    nothing is being scored, so this asserts the counters move.
    """
    with TestClient(app) as client:
        # The session steps on its own loop; give it a moment to transact.
        deadline = time.time() + 15
        summary = {}
        while time.time() < deadline:
            summary = client.get("/api/risk/summary").json()
            if summary.get("assessed", 0) > 0:
                break
            time.sleep(0.5)

        assert summary["enabled"] is True
        assert summary["assessed"] > 0, "the rails saw no traffic in fifteen seconds"
        assert summary["scored"] <= summary["assessed"]
        assert "card_model_loaded" in summary, (
            "a card rail that allows everything and one that finds nothing "
            "produce identical counts - the dashboard has to be told which")


def test_cases_carry_their_evidence():
    with TestClient(app) as client:
        body = client.get("/api/risk/cases").json()
        assert "cases" in body
        for case in body["cases"]:
            assert case["rail"] in {"card", "wire"}
            assert 0.0 <= case["score"] <= 1.0
            assert case["reason"], "a case with no stated reason is not reviewable"


def test_the_console_renders():
    with TestClient(app) as client:
        page = client.get("/api/risk/console")
        assert page.status_code == 200
        assert "Fraud review" in page.text


def test_the_console_is_honest_about_an_untrained_card_rail():
    """Without a model the page must say so, not just show an empty queue."""
    with TestClient(app) as client:
        loaded = client.get("/api/risk/summary").json().get("card_model_loaded")
        page = client.get("/api/risk/console").text
        if not loaded:
            assert "no trained model" in page.lower()
