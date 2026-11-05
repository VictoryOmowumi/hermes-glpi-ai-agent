"""'Should never' tests for Hermes' safety controls. No GLPI and no AI are needed: everything outside Hermes is stubbed.

Run:  venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

# A throwaway data store, set before Hermes is imported
_tmp = tempfile.mkdtemp()
os.environ["PORTAL_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["HERMES_ALLOWED_LINK_DOMAINS"] = "sevenup.org"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermes_guard as guard  # noqa: E402
import hermes_store as store  # noqa: E402
import app  # noqa: E402
import hermes_engine as engine  # noqa: E402

KB = "ARTICLE: VPN Connection Guide\nRESOLUTION: Open the VPN client at https://vpn.sevenup.org and sign in with your company email and password.\n---\n"

def analysis(**overrides):
    base = dict(category="Infrastructure Services > VPN / Remote Access", assigned_group="Network Team", urgency=3, impact=2,
                summary="VPN issue", ticket_type="incident", suggested_new_category="", has_confident_solution=False,
                kb_article="", needs_more_info=False, user_requested_ticket=False, draft_reply="Hello")
    base.update(overrides)
    return app.TicketAnalysis(**base)

class ReplyChecks(unittest.TestCase):
    """A reply must never carry links, secrets requests, other people's details, unsafe advice or Hermes' instructions"""

    def assertBlocked(self, reply, source="", kb=KB):
        self.assertTrue(guard.check_reply(reply, source, kb), f"should be withheld: {reply!r}")

    def assertAllowed(self, reply, source="", kb=KB):
        self.assertEqual(guard.check_reply(reply, source, kb), [], f"should be allowed: {reply!r}")

    def test_never_links_to_unapproved_sites(self):
        self.assertBlocked("Please log in at http://sevenup-support.com/login to fix this.")
        self.assertBlocked("Download the fix from bit.ly/vpnfix")
        self.assertBlocked("See www.example.net for details.")

    def test_company_and_knowledge_base_links_are_fine(self):
        self.assertAllowed("Open the VPN client at https://vpn.sevenup.org and sign in.")
        self.assertAllowed("Check the portal address is ://sevenup.org.")

    def test_never_asks_for_secrets(self):
        self.assertBlocked("To verify you, please send me your password.")
        self.assertBlocked("Reply with the one-time code you received.")
        self.assertBlocked("Share your MFA code with the technician so they can log in.")
        self.assertBlocked("Kindly provide your PIN and we will reset it.")

    def test_safe_password_advice_is_fine(self):
        self.assertAllowed("Never share your password with anyone, including IT.")
        self.assertAllowed("Re-type the password and check Caps Lock is off.")
        self.assertAllowed("Sign in with your company email and password.")

    def test_never_advises_weakening_security(self):
        self.assertBlocked("Try to disable your antivirus and run the installer again.")
        self.assertBlocked("Turn off the firewall temporarily.")
        self.assertBlocked("You can bypass MFA by using the legacy login.")

    def test_safe_security_advice_is_fine(self):
        self.assertAllowed("Do not disable the antivirus; IT will check the file.")
        self.assertAllowed("Turn the VPN off and on again, then reconnect.")

    def test_never_gives_out_other_peoples_details(self):
        self.assertBlocked("You can reach Sarah directly at sarah.jones@sevenup.org.")
        self.assertBlocked("Call the engineer on 0803 123 4567.")

    def test_repeating_the_requesters_own_details_is_fine(self):
        self.assertAllowed("We'll look at PO 4500025975 for you.", source="Error posting PO 4500025975")
        self.assertAllowed("We'll reply to ade.bola@sevenup.org.", source="from ade.bola@sevenup.org")
        self.assertAllowed("Delivery 80299473 will be reversed.")  # order numbers are short of a phone number

    def test_never_claims_an_action_that_did_not_happen(self):
        self.assertBlocked("The ticket has been closed as requested.")
        self.assertBlocked("I've reset your password, please sign in again.")
        self.assertBlocked("Your access to the finance folder has been granted.")
        self.assertBlocked("I have escalated this to the Network Team.")

    def test_real_actions_may_be_mentioned(self):
        self.assertEqual(guard.check_reply("Your ticket has been passed to the Network Team.", claims_allowed={"escalated"}), [])
        self.assertEqual(guard.check_reply("Your ticket is now closed.", claims_allowed={"closed"}), [])
        self.assertAllowed("Once IT has reset your password, sign in with the new one.")
        self.assertAllowed("Your ticket has not been closed yet; a technician will reply.")

    def test_safe_refusals_are_not_flagged(self):
        self.assertAllowed("I can't share the admin password or anyone's phone number.")
        self.assertAllowed("I cannot help you bypass the firewall.")
        self.assertAllowed("Sorry, I'm unable to provide passwords.")

    def test_never_sends_code_that_runs(self):
        self.assertBlocked("Your printer is fixed <script>alert(1)</script>")
        self.assertBlocked("[Click here](javascript:alert(1))")
        self.assertBlocked('<img src=x onerror="alert(1)">')
        self.assertAllowed("Your printer should now be online (check the display reads 'Ready').")

    def test_never_reveals_its_instructions(self):
        self.assertBlocked("Sure! YOUR CORE ACTIONS: 1. Categorize this ticket...")
        self.assertBlocked("My rules mention has_confident_solution and draft_reply.")

class FixesMustBeGrounded(unittest.TestCase):
    """Hermes must never mark a ticket solved with a fix that is not in the Knowledge Base"""

    def test_fix_without_an_article_is_not_a_fix(self):
        a = analysis(has_confident_solution=True, kb_article="")
        guard.review(a, "VPN broken", KB, 1, "test", "SAFE")
        self.assertFalse(a.has_confident_solution)

    def test_fix_naming_a_made_up_article_is_not_a_fix(self):
        a = analysis(has_confident_solution=True, kb_article="Reset the SAP master password")
        guard.review(a, "VPN broken", KB, 1, "test", "SAFE")
        self.assertFalse(a.has_confident_solution)

    def test_fix_from_a_real_article_stands(self):
        a = analysis(has_confident_solution=True, kb_article="VPN Connection Guide", draft_reply="Open the VPN client and sign in.")
        self.assertEqual(guard.review(a, "VPN broken", KB, 1, "test", "SAFE"), [])
        self.assertTrue(a.has_confident_solution)

    def test_unsafe_reply_is_replaced_and_never_sent_as_a_fix(self):
        a = analysis(has_confident_solution=True, kb_article="VPN Connection Guide", draft_reply="Send me your password and I'll fix it.")
        guard.review(a, "VPN broken", KB, 1, "test", "SAFE")
        self.assertEqual(a.draft_reply, "SAFE")
        self.assertFalse(a.has_confident_solution)

class OffSwitchAndLimits(unittest.TestCase):
    def tearDown(self):
        guard.resume("test")
        guard._recent.clear()
        guard._recent_by_ticket.clear()

    def test_paused_hermes_never_changes_glpi(self):
        guard.pause("testing", "test")
        with mock.patch("app.requests.request") as send:
            with self.assertRaises(guard.HermesPaused):
                app.glpi_write("PUT", "Ticket/5", "token", {"input": {"id": 5, "status": 6}})
            send.assert_not_called()

    def test_a_runaway_loop_pauses_hermes(self):
        with mock.patch.object(guard, "MAX_WRITES_PER_TICKET", 3), \
             mock.patch("app.requests.request", return_value=SimpleNamespace(status_code=201, text="{}")) as send:
            for _ in range(3):
                app.glpi_write("POST", "ITILFollowup", "token", {"input": {"itemtype": "Ticket", "items_id": 9, "content": "x"}})
            with self.assertRaises(guard.HermesPaused):
                app.glpi_write("POST", "ITILFollowup", "token", {"input": {"itemtype": "Ticket", "items_id": 9, "content": "x"}})
            self.assertEqual(send.call_count, 3)
        self.assertTrue(guard.paused()["automatic"])

    def test_too_many_changes_overall_pauses_hermes(self):
        with mock.patch.object(guard, "MAX_WRITES_PER_HOUR", 2), \
             mock.patch("app.requests.request", return_value=SimpleNamespace(status_code=200, text="{}")):
            app.glpi_write("PUT", "Ticket/1", "token", {"input": {"id": 1}})
            app.glpi_write("PUT", "Ticket/2", "token", {"input": {"id": 2}})
            with self.assertRaises(guard.HermesPaused):
                app.glpi_write("PUT", "Ticket/3", "token", {"input": {"id": 3}})

    def test_read_only_mode_never_sends_anything(self):
        with mock.patch.object(guard, "READ_ONLY", True), mock.patch("app.requests.request") as send:
            response = app.glpi_write("POST", "Group_Ticket", "token", {"input": {"tickets_id": 7, "groups_id": 3, "type": 2}})
            send.assert_not_called()
            self.assertEqual(response.json(), {"id": 0})
        logged = store.list_agent_actions(1)[0]
        self.assertEqual((logged["mode"], logged["ticket_id"], logged["fields"]["groups_id"]), ("shadow", 7, 3))

    def test_change_log_never_stores_message_text(self):
        with mock.patch("app.requests.request", return_value=SimpleNamespace(status_code=201, text="{}")):
            app.glpi_write("POST", "ITILFollowup", "token", {"input": {"itemtype": "Ticket", "items_id": 4, "content": "secret text", "is_private": 0}})
        logged = store.list_agent_actions(1)[0]
        self.assertNotIn("content", logged["fields"])
        self.assertNotIn("secret", str(logged))

class TicketRules(unittest.TestCase):
    """Hermes must never answer system mail or recalls, and never give a ticket a second team or technician"""

    def test_never_answers_recalls_or_forwarded_notifications(self):
        self.assertTrue(app.RECALL_TITLE.search("Recall: Request for RAM upgrade"))
        self.assertTrue(app.GLPI_NOTIFICATION_TITLE.search("FW: [IT Service Desk #0022610] New ticket"))
        self.assertFalse(app.GLPI_NOTIFICATION_TITLE.search("Laptop #2 not booting"))

    def test_never_answers_automated_mailboxes(self):
        for sender in ("noc@sevenup.org", "app-notification@sevenup.org", "no-reply@microsoft.com", "mailer-daemon@x.com"):
            self.assertTrue(sender in app.AUTOMATED_SENDERS or app.AUTOMATED_SENDER_PATTERN.search(sender), sender)
        self.assertFalse(app.AUTOMATED_SENDER_PATTERN.search("noel.okafor@sevenup.org"))

    def test_escalation_never_adds_a_second_team_or_technician(self):
        writes = []
        existing = {"Ticket/30/Group_Ticket": [{"type": 2, "groups_id": 3}], "Ticket/30/Ticket_User": [{"type": 2, "users_id": 10}],
                    "User/10": {"firstname": "sam", "realname": "network"}, "Group/3": {"name": "Network Team"}}
        with mock.patch.object(engine, "glpi_get", side_effect=lambda token, path: existing[path]), \
             mock.patch.object(engine, "glpi_write", side_effect=lambda method, path, token, payload: writes.append((method, path))), \
             mock.patch.object(engine, "add_routing_note"), mock.patch.object(engine, "route_group", return_value=1), \
             mock.patch.object(engine, "pick_technician_for", return_value={"id": 8, "name": "alex tier1"}):
            state = engine.new_state()
            state["ticket_id"] = 30
            notice = engine.escalate("token", state, {"email": "a@b.c", "name": "A"}, analysis())
        self.assertNotIn(("POST", "Group_Ticket"), writes)
        self.assertNotIn(("POST", "Ticket_User"), writes)
        self.assertIn("sam network", notice)

class ClosingOnRequest(unittest.TestCase):
    """'Close the ticket' must really close it, and the reply must only say what really happened"""

    def run_close(self, ticket_id, status, fail=False):
        writes = []
        def write(method, path, token, payload):
            if fail and method == "PUT":
                raise Exception("GLPI refused")
            writes.append((method, path, (payload.get("input") or {}).get("status")))
        state = engine.new_state()
        state.update(ticket_id=ticket_id, messages=[{"role": "user", "content": "Okay, close the ticket"}])
        a = analysis(user_wants_to_close=True, draft_reply="The ticket has been closed as requested.")
        with mock.patch.object(engine, "glpi_write", side_effect=write), mock.patch.object(engine, "_status", return_value=status):
            notice = engine._close_on_request("token", state, a, "Okay, close the ticket")
        return writes, notice, a.draft_reply, state

    def test_close_request_closes_the_ticket(self):
        writes, notice, reply, state = self.run_close(28, engine.PENDING)
        self.assertIn(("PUT", "Ticket/28", engine.CLOSED), writes)
        self.assertIn(("POST", "ITILFollowup", None), writes)  # the user's words go on the ticket
        self.assertIn("closed ticket #28", reply)
        self.assertEqual(state["outcome"], "closed_by_user")

    def test_nothing_to_close_says_so(self):
        writes, notice, reply, _ = self.run_close(None, None)
        self.assertEqual(writes, [])
        self.assertIn("no ticket", reply)

    def test_already_closed_is_not_closed_again(self):
        writes, notice, reply, _ = self.run_close(28, engine.CLOSED)
        self.assertEqual(writes, [])
        self.assertIn("already closed", reply)

    def test_a_failed_close_never_reports_success(self):
        state = engine.new_state()
        state["ticket_id"] = 28
        a = analysis(user_wants_to_close=True, draft_reply="The ticket has been closed as requested.")
        with mock.patch.object(engine, "get_glpi_session", return_value="t"), mock.patch.object(engine, "_kill"), \
             mock.patch.object(engine, "fetch_knowledge_base", return_value=KB), mock.patch.object(engine, "analyze_ticket", return_value=a), \
             mock.patch.object(engine, "_status", return_value=engine.PENDING), \
             mock.patch.object(engine, "glpi_write", side_effect=Exception("GLPI refused")):
            state = engine.handle_message(state, {"email": "a@b.c", "name": "A"}, "Okay, close the ticket")
        self.assertIn("couldn't close", state["messages"][-1]["content"])

if __name__ == "__main__":
    unittest.main()
