"""Satisfaction survey rules, through the Hermes API. No GLPI or AI needed.

Run:  venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_tmp = tempfile.mkdtemp()
os.environ["PORTAL_DB_PATH"] = os.path.join(_tmp, "surveys.db")
os.environ.setdefault("HERMES_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings  # noqa: E402
warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient  # noqa: E402

import hermes_api  # noqa: E402
import hermes_store as store  # noqa: E402
import server  # noqa: E402

client = TestClient(server.app.api_app)

def headers(email):
    return {"X-Hermes-Key": hermes_api.HERMES_API_KEY, "X-User-Email": email, "X-User-Name": "Test"}

ADA, BEN = "ada@sevenup.org", "ben@sevenup.org"
DETAILS = {"title": "VPN broken", "channel": "IT Support chat", "category": "VPN", "team": "Network Team", "technicians": "sam network"}

class Surveys(unittest.TestCase):
    def setUp(self):
        with store.connect() as connection:
            connection.execute("DELETE FROM surveys")
        store.open_survey(101, ADA, DETAILS)

    def test_a_closed_ticket_gets_one_survey(self):
        self.assertFalse(store.open_survey(101, ADA, DETAILS))  # closing again never asks twice
        surveys = client.get("/v1/surveys", headers=headers(ADA)).json()
        self.assertEqual([(s["ticket_id"], s["popup"]) for s in surveys], [(101, True)])

    def test_only_the_requester_can_answer(self):
        self.assertEqual(client.get("/v1/surveys", headers=headers(BEN)).json(), [])
        r = client.post("/v1/surveys/101", json={"rating": 1}, headers=headers(BEN))
        self.assertEqual(r.status_code, 404)

    def test_answer_is_recorded_once(self):
        r = client.post("/v1/surveys/101", json={"rating": 2, "reason": "Too slow", "comment": "Took two days"}, headers=headers(ADA))
        self.assertEqual(r.status_code, 200)
        again = client.post("/v1/surveys/101", json={"rating": 5}, headers=headers(ADA))
        self.assertEqual(again.status_code, 409)
        saved = store.get_survey(101, ADA)
        self.assertEqual((saved["rating"], saved["reason"], saved["comment"]), (2, "Too slow", "Took two days"))
        self.assertEqual(client.get("/v1/surveys", headers=headers(ADA)).json(), [])

    def test_rating_must_be_one_to_five(self):
        for bad in (0, 6, -1):
            self.assertEqual(client.post("/v1/surveys/101", json={"rating": bad}, headers=headers(ADA)).status_code, 422)

    def test_reason_only_kept_for_low_ratings(self):
        client.post("/v1/surveys/101", json={"rating": 5, "reason": "Too slow"}, headers=headers(ADA))
        self.assertIsNone(store.get_survey(101, ADA)["reason"])

    def test_unknown_reason_is_refused(self):
        r = client.post("/v1/surveys/101", json={"rating": 1, "reason": "<script>"}, headers=headers(ADA))
        self.assertEqual(r.status_code, 400)

    def test_closing_the_card_stops_the_popup_but_keeps_the_survey(self):
        client.post("/v1/surveys/101/dismiss", headers=headers(ADA))
        surveys = client.get("/v1/surveys", headers=headers(ADA)).json()
        self.assertEqual([(s["ticket_id"], s["popup"]) for s in surveys], [(101, False)])
        self.assertEqual(client.post("/v1/surveys/101", json={"rating": 4}, headers=headers(ADA)).status_code, 200)

    def test_expired_survey_cannot_be_answered(self):
        with store.connect() as connection:
            connection.execute("UPDATE surveys SET expires_at = ?", ((datetime.now() - timedelta(days=1)).isoformat(),))
        self.assertEqual(client.get("/v1/surveys", headers=headers(ADA)).json(), [])
        self.assertEqual(client.post("/v1/surveys/101", json={"rating": 4}, headers=headers(ADA)).status_code, 409)

    def test_erasure_request_anonymises_the_answer(self):
        client.post("/v1/surveys/101", json={"rating": 3, "comment": "fine"}, headers=headers(ADA))
        store.delete_person_data(ADA, {"email": "admin@sevenup.org", "name": "Admin"}, "DSR-1")
        with store.connect() as connection:
            row = dict(connection.execute("SELECT user_email, comment, rating FROM surveys WHERE ticket_id = 101").fetchone())
        self.assertNotIn("ada", row["user_email"])
        self.assertIsNone(row["comment"])
        self.assertEqual(row["rating"], 3)  # the rating still counts in the figures

    def test_console_figures(self):
        store.open_survey(102, BEN, {**DETAILS, "technicians": "alex tier1"})
        client.post("/v1/surveys/101", json={"rating": 5}, headers=headers(ADA))
        client.post("/v1/surveys/102", json={"rating": 2, "reason": "Not fixed"}, headers=headers(BEN))
        data = client.get("/v1/admin/satisfaction?days=7", headers=headers("admin@sevenup.org")).json()
        self.assertEqual((data["responses"], data["average"], data["satisfied"]), (2, 3.5, 0.5))
        self.assertEqual({r["name"]: r["average"] for r in data["by_technician"]}, {"sam network": 5.0, "alex tier1": 2.0})
        follow = client.post("/v1/admin/satisfaction/follow-up", json={"ticket_id": 102, "user_email": BEN, "note": "Called Ben, fixed now"},
                             headers=headers("admin@sevenup.org"))
        self.assertEqual(follow.status_code, 200)

if __name__ == "__main__":
    unittest.main()
