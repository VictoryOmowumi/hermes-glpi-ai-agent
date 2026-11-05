"""Hermes conversation engine: the chat rules, independent of any screen.

Every front end (the intranet, the Streamlit test portal, later Google Chat) calls handle_message and handle_action
and renders the returned state. Nothing here depends on Streamlit or on how the user signed in.
"""
import html
from datetime import datetime

import requests

import hermes_guard
from app import (
    APP_TOKEN,
    GLPI_BASE_URL,
    PERSONA,
    REPLY_HEADER,
    SAFETY_RULES,
    SELF_SERVICE_SOURCE_ID,
    SOLUTION_HEADER,
    TicketAnalysis,
    add_routing_note,
    analyze_ticket,
    category_list_text,
    category_mapping,
    fetch_knowledge_base,
    fetch_ticket,
    find_user_by_email,
    format_reply_html,
    get_glpi_session,
    glpi_get,
    glpi_write,
    group_list_text,
    is_broad_category,
    pick_technician_for,
    open_surveys_for,
    record_category_suggestion,
    route_group,
    ticket_type_id,
)

# GLPI ticket statuses used by the self-service flow
ASSIGNED, PENDING, SOLVED, CLOSED = 2, 4, 5, 6
# Urgency at or above this level (4=High, 5=Very High) goes to a technician straight away when there is no fix
AUTO_ESCALATE_URGENCY = 4
# Rounds of follow-up questions before Hermes asks whether a technician is needed
MAX_TRIAGE_ROUNDS = 2
# Longest message accepted from a user
MAX_MESSAGE_LENGTH = 4000

# =====================================================================
# Conversation state
# =====================================================================
def new_state():
    return {"messages": [], "ticket_id": None, "escalated": False, "pending": None,
            "triage_rounds": 0, "declined_technician": False, "outcome": "open"}

def normalise_state(state):
    """Fills in fields that conversations saved by older versions do not have"""
    base = new_state()
    base.update(state or {})
    # Before self-service tickets existed, a conversation only had a ticket once it was escalated
    if "escalated" not in (state or {}):
        base["escalated"] = bool(base["ticket_id"])
    return base

def add_message(state, role, content, notice=None):
    state["messages"].append({"role": role, "content": content, "notice": notice,
                              "time": datetime.now().isoformat(timespec="seconds")})

def available_actions(state):
    """The choice to show under the latest reply, as data any front end can render:
    {"prompt": text or None, "actions": [{"id", "label", "style"}]}. Styles: success, primary, neutral, link."""
    pending = (state.get("pending") or {}).get("kind")
    if pending == "solved_check":
        return {"prompt": "Is your issue resolved?", "actions": [
            {"id": "resolved", "label": "Yes, it's resolved", "style": "success"},
            {"id": "not_resolved", "label": "No, it's not resolved", "style": "neutral"}]}
    if pending in ("not_resolved_offer", "offer_technician"):
        prompt = "Shall I assign this to a technician?" if pending == "not_resolved_offer" else "Do you need a technician to look at this?"
        return {"prompt": prompt, "actions": [
            {"id": "technician_yes", "label": "Yes, assign a technician", "style": "primary"},
            {"id": "technician_no", "label": "No, let's keep trying", "style": "neutral"}]}
    if state["messages"] and not state["escalated"]:
        label = "Skip the questions and talk to a technician" if pending == "triage" else "Talk to a technician"
        return {"prompt": None, "actions": [{"id": "request_technician", "label": label, "style": "link"}]}
    return {"prompt": None, "actions": []}

# =====================================================================
# The AI prompt
# =====================================================================
def build_prompt(state, kb_context, user=None):
    transcript = "\n".join(f"{'USER' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}" for m in state["messages"])
    no_technician = ("No technician is involved yet. In 'draft_reply' you must NOT write that anything was escalated, forwarded "
                     "or passed to a team or technician, because it has not been. Do not ask whether to involve a technician: "
                     "the screen shows that choice itself.")
    if state["ticket_id"] and state["escalated"]:
        stage = f"Ticket #{state['ticket_id']} is with a technician. Tell the user their new details have been added to ticket #{state['ticket_id']}."
    elif state["declined_technician"]:
        stage = (f"CONTINUE TROUBLESHOOTING. {no_technician} The user chose to keep trying rather than involve a technician. "
                 "Suggest further safe checks, or ask what they see now. Never repeat a question already answered.")
    elif state["triage_rounds"] < MAX_TRIAGE_ROUNDS:
        stage = f"""DIAGNOSIS STAGE. {no_technician}
    If no Knowledge Base article solves the issue, your 'draft_reply' must help narrow the problem down before any escalation:
    - Ask ONE or TWO short, specific questions a technician would need answered (for example: the exact error text, when it started, which device or network, whether colleagues are affected, what changed recently). Never repeat a question already answered above.
    - You MAY add one or two quick generic first checks that are safe for any employee (for example: re-type the password and check Caps Lock, restart the app or device, try another browser, check the cable or Wi-Fi). Present them as things to try, not as the official fix.
    - Keep it brief and friendly, and end on your question."""
    else:
        stage = (f"WRAP-UP STAGE. {no_technician} You have already asked your follow-up questions, so do NOT ask any more. "
                 "If there is no Knowledge Base fix, summarise in one or two sentences what you now understand about the issue "
                 "and say it needs a technician to look at it.")
    # A live chat reads like a conversation: no letter-style greeting on every reply, and never a sign-off
    if any(m["role"] == "assistant" for m in state["messages"]):
        reply_style = "This is a continuing live chat. Do NOT greet the user again and do NOT add any closing or sign-off. Start directly with the substance."
    else:
        reply_style = "This is your first reply in a live chat. You may open with one short friendly line, but do NOT add any closing or sign-off."
    return f"""
    {PERSONA} You are chatting live with an employee{f" named {user['name']}" if user and user.get('name') else ""}.
    Other people they mention (for example a new staff member they are requesting something for) are NOT the person you are talking to; never address the user by someone else's name.

    CONVERSATION SO FAR (the last USER line is the newest message):
    {transcript}

    ---
    INTERNAL COMPANY KNOWLEDGE BASE ARTICLES (RAG Context):
    {kb_context}
    ---

    TICKET STATE: {stage}

    YOUR CORE ACTIONS:
    1. Treat the conversation as ONE ongoing issue. Use every earlier message as context and never ask for details the user already gave.
    2. Categorize the issue using exactly one of these values (separated by semicolons): {category_list_text()}. If no specific category fits, also propose a name for a new category in 'suggested_new_category'.
    3. Set 'assigned_group' to the support group that should own it, using exactly one of these names (with what each handles, where known):
{group_list_text()}
    4. Compute Urgency and Impact on GLPI's scale (1=Very Low, 2=Low, 3=Medium, 4=High, 5=Very High).
    5. Cross-reference the Knowledge Base. If an article accurately solves the issue, provide the guide steps in 'draft_reply', flag 'has_confident_solution' as True and put the article's title in 'kb_article'. If no documentation matches, toggle it to False.
    6. If the issue is still too vague to categorize, flag 'needs_more_info' as True.
    7. If the user's newest message explicitly asks for a ticket, a technician or a human, flag 'user_requested_ticket' as True.
    8. Write 'draft_reply' as your next chat message to the user, replying to their newest message and following the TICKET STATE instructions above. Never invent company-specific tools, portals or procedures that are not in the Knowledge Base.
    9. Set 'ticket_type': 'request' if the user is asking for something new or a change (new or upgraded hardware, software, access, an account), 'incident' if something is broken. For a request, do NOT troubleshoot or suggest fixes; ask only for the details needed to fulfil it (for example which device, what exactly is needed, the business reason, and by when).

    REPLY STYLE: {reply_style}

    {SAFETY_RULES}
    """

# Shown instead of an AI reply that fails the safety checks (hermes_guard)
SAFE_CHAT_REPLY = "Sorry, I can't help with that here. If it's an IT problem, I can pass it to a technician, who will follow up with you."

# =====================================================================
# GLPI actions
# =====================================================================
def _note(token, ticket_id, text, private):
    glpi_write("POST", "ITILFollowup", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": text, "is_private": 1 if private else 0}})

def _set_status(token, ticket_id, status):
    glpi_write("PUT", f"Ticket/{ticket_id}", token, {"input": {"id": ticket_id, "status": status}})

def _status(token, ticket_id):
    return int(fetch_ticket(token, ticket_id).get("status", 0))

def _technician(token, user_id):
    """{'id', 'name'} for a GLPI user already assigned to a ticket"""
    user = glpi_get(token, f"User/{user_id}")
    name = f"{user.get('firstname') or ''} {user.get('realname') or ''}".strip() or user.get("name") or f"user #{user_id}"
    return {"id": user_id, "name": name}

def _user_says(text):
    return f"💬 <b>User (portal chat)</b><br>{html.escape(text, quote=False)}"

def ticket_title(text, limit=80):
    """The first non-empty line of the user's first message, cut at a word boundary: messages pasted from an email
    usually start with a subject line, and a title must never carry on into the next line"""
    first_line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "IT support request")
    first_line = " ".join(first_line.split())
    if len(first_line) <= limit:
        return first_line
    return first_line[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"

def open_self_service_ticket(token, state, user, analysis, linked_to=None):
    """Records the conversation in GLPI as an AI self-service ticket: Pending, no technician yet"""
    messages = state["messages"]
    first_issue = next(m["content"] for m in messages if m["role"] == "user")
    # Earlier turns go in the description; the current turn is added as a follow-up straight after
    transcript = "<br>".join(f"<b>{'User' if m['role'] == 'user' else 'Hermes'}:</b> {html.escape(m['content'], quote=False)}" for m in messages[:-1])
    transcript = transcript or "<i>The conversation starts in the follow-ups below.</i>"
    payload = {"input": {
        "name": f"Portal Request: {ticket_title(first_issue)}",
        "content": (f"{html.escape(analysis.summary, quote=False)}<br><br><b>Raised via the portal by:</b> {html.escape(user['name'], quote=False)} "
                    f"&lt;{html.escape(user['email'], quote=False)}&gt;<br><br><b>Portal chat transcript</b><br>{transcript}"),
        "itilcategories_id": category_mapping().get(analysis.category, 0),
        "urgency": analysis.urgency,
        "impact": analysis.impact,
        "type": ticket_type_id(analysis),
        "requesttypes_id": SELF_SERVICE_SOURCE_ID,
        "status": PENDING,
    }}
    # The signed-in user is the requester when they have a GLPI account (matched by email)
    requester_id = find_user_by_email(token, user["email"])
    if requester_id:
        payload["input"]["_users_id_requester"] = requester_id
    ticket_id = glpi_write("POST", "Ticket", token, payload).json()["id"]
    state.update(ticket_id=ticket_id, escalated=False, outcome="self_service")

    # A closed ticket cannot be reopened, so a returning conversation gets a new ticket linked to the old one
    if linked_to:
        try:
            glpi_write("POST", "Ticket_Ticket", token, {"input": {"tickets_id_1": ticket_id, "tickets_id_2": linked_to, "link": 1}})
        except Exception:
            pass
    extra = f" Follows on from closed ticket #{linked_to}." if linked_to else ""
    if not requester_id:
        extra += f" Requester {html.escape(user['email'], quote=False)} has no GLPI account, so no requester is set on this ticket."
    if is_broad_category(analysis.category):
        extra += " No specific category fits yet."
    details = (f"<b>Type:</b> {'Request' if ticket_type_id(analysis) == 2 else 'Incident'} · <b>Category:</b> {html.escape(analysis.category, quote=False)}"
               f" · <b>Urgency:</b> {analysis.urgency} · <b>Impact:</b> {analysis.impact}<br><b>Summary:</b> {html.escape(analysis.summary, quote=False)}")
    _note(token, ticket_id, f"🤖 Portal chat: Hermes opened this as an AI self-service request. No technician assigned yet.{extra}<br>{details}", private=True)
    record_category_suggestion(ticket_id, analysis, source="portal")
    return f"Recorded as self-service request #{ticket_id}."

def record_turn(token, state, user_text, reply_text=None):
    """Adds the latest exchange to the ticket's timeline, so the GLPI record holds the whole conversation"""
    content = _user_says(user_text)
    if reply_text:
        content += f"<br><br>{REPLY_HEADER}<br>{format_reply_html(reply_text)}"
    _note(token, state["ticket_id"], content, private=False)

def escalate(token, state, user, analysis, reason=None):
    """Hands the conversation's ticket to the least busy technician in the right group"""
    if not state["ticket_id"]:
        open_self_service_ticket(token, state, user, analysis)
    ticket_id = state["ticket_id"]
    if reason:
        _note(token, ticket_id, _user_says(reason), private=False)

    # Keep whatever group and technician the ticket already has (a technician may have picked it up, or this is a
    # repeat), so a ticket never ends up with two of each
    groups = [g for g in glpi_get(token, f"Ticket/{ticket_id}/Group_Ticket") if g.get("type") == 2]
    if groups:
        group_id = int(groups[0]["groups_id"])
    else:
        group_id = route_group(analysis)
        glpi_write("POST", "Group_Ticket", token, {"input": {"tickets_id": ticket_id, "groups_id": group_id, "type": 2}})

    assigned = [a for a in glpi_get(token, f"Ticket/{ticket_id}/Ticket_User") if a.get("type") == 2 and a.get("users_id")]
    if assigned:
        technician = _technician(token, int(assigned[0]["users_id"]))
    else:
        try:
            technician = pick_technician_for(token, analysis, group_id)
        except Exception:
            technician = None
        if technician:
            glpi_write("POST", "Ticket_User", token, {"input": {"tickets_id": ticket_id, "users_id": technician["id"], "type": 2}})
    _set_status(token, ticket_id, ASSIGNED)
    # This is a hand-over to a technician, so the note must say "routed", even when the analysis had a fix that failed
    add_routing_note(token, ticket_id, analysis.model_copy(update={"has_confident_solution": False}), group_id, technician, prefix="Portal chat: ")
    state.update(escalated=True, outcome="ticket", pending=None)
    group_name = glpi_get(token, f"Group/{group_id}").get("name", analysis.assigned_group)
    assignee = f", assigned to {technician['name']}" if technician else ""
    return f"Ticket #{ticket_id} passed to the {group_name}{assignee}."

# =====================================================================
# Entry points
# =====================================================================
def _kill(token):
    try:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
    except Exception:
        pass

def _open_survey_now(token, ticket_id, user):
    """Closed from the chat: open the satisfaction survey at once, so the rating card appears straight away (the
    change tracker would also open it, a minute later). Never blocks the close itself."""
    if not user:
        return
    try:
        open_surveys_for(token, ticket_id, [user["email"]])
    except Exception as e:
        print(f"⚠️ Could not open the satisfaction survey for ticket #{ticket_id}: {e}")

def _close_on_request(token, state, analysis, user_text, user=None):
    """Closes the conversation's ticket because the user asked to. The reply always comes from here, so it only ever
    says what really happened. The user's own words go on the ticket, so the technician (if any) sees why."""
    ticket_id = state["ticket_id"]
    if not ticket_id:
        analysis.draft_reply = "There's no ticket from this conversation to close, so nothing has been logged with IT. Start a new message any time you need help."
        return None
    if _status(token, ticket_id) == CLOSED:
        analysis.draft_reply = f"Ticket #{ticket_id} is already closed. If the issue comes back, just tell me here and I'll open a new one."
        return None
    _note(token, ticket_id, _user_says(f"{user_text}\n(The requester closed this ticket from IT Support chat.)"), private=False)
    _set_status(token, ticket_id, CLOSED)
    state.update(outcome="closed_by_user", pending=None)
    _open_survey_now(token, ticket_id, user)
    analysis.draft_reply = f"Done, I've closed ticket #{ticket_id} as you asked. If the issue comes back, just tell me here and I'll open a new one."
    return f"Ticket #{ticket_id} closed at your request."

def _decide_next_step(token, state, user, analysis, user_text):
    """Applies the self-service rules after each AI reply; returns the notice line for the reply (or None)"""
    state["pending"] = None
    notices = []

    # The user asked to close their ticket: Hermes' code closes it and writes the reply, never the AI
    if analysis.user_wants_to_close:
        return _close_on_request(token, state, analysis, user_text, user)

    # A closed ticket cannot be reopened: this message starts a new ticket linked to the old one
    linked_to = None
    if state["ticket_id"] and _status(token, state["ticket_id"]) == CLOSED:
        linked_to = state["ticket_id"]
        state.update(ticket_id=None, escalated=False, triage_rounds=0, declined_technician=False)

    # Already with a technician: Hermes keeps helping, and every exchange is added to the ticket
    if state["ticket_id"] and state["escalated"]:
        record_turn(token, state, user_text, analysis.draft_reply)
        return f"Added to ticket #{state['ticket_id']}."

    # The user wrote back after a solution instead of using the buttons: the conversation continues
    if state["ticket_id"] and _status(token, state["ticket_id"]) == SOLVED:
        _set_status(token, state["ticket_id"], PENDING)

    # Record the conversation as soon as Hermes can place the issue (greetings and vague openers are not recorded)
    triage_done = state["triage_rounds"] >= MAX_TRIAGE_ROUNDS
    understood = (analysis.category != "General / Unknown" or not analysis.needs_more_info
                  or analysis.user_requested_ticket or analysis.has_confident_solution or triage_done)
    if not state["ticket_id"] and understood:
        notices.append(open_self_service_ticket(token, state, user, analysis, linked_to))
    if state["ticket_id"]:
        # A knowledge base fix is posted as the solution itself, so only the user's message is added here
        record_turn(token, state, user_text, None if analysis.has_confident_solution else analysis.draft_reply)

    snapshot = {"kind": None, "analysis": analysis.model_dump()}
    if analysis.user_requested_ticket:
        notices.append(escalate(token, state, user, analysis))
    elif analysis.urgency >= AUTO_ESCALATE_URGENCY and not analysis.has_confident_solution:
        notices.append(escalate(token, state, user, analysis) + " Escalated automatically because this looks urgent.")
    elif analysis.has_confident_solution:
        glpi_write("POST", "ITILSolution", token, {"input": {"itemtype": "Ticket", "items_id": state["ticket_id"],
                   "content": f"{SOLUTION_HEADER}<br><br>{format_reply_html(analysis.draft_reply)}"}})
        state["pending"] = {**snapshot, "kind": "solved_check"}
    elif state["declined_technician"]:
        # The user chose to keep trying: keep helping without asking again (the technician link stays available)
        state["pending"] = {**snapshot, "kind": "continue"}
    elif not triage_done:
        state["triage_rounds"] += 1
        state["pending"] = {**snapshot, "kind": "triage"}
    else:
        state["pending"] = {**snapshot, "kind": "offer_technician"}
    return " ".join(notices) or None

def handle_message(state, user, text):
    """Processes one user message and returns the updated state. 'user' is {'email', 'name'}, verified by the caller."""
    state = normalise_state(state)
    text = (text or "").strip()[:MAX_MESSAGE_LENGTH]
    if not text:
        return state
    add_message(state, "user", text)
    token = get_glpi_session()
    try:
        try:
            kb_context = fetch_knowledge_base(token)
            analysis = analyze_ticket(build_prompt(state, kb_context, user))
            # Safety checks before the user sees anything: the user's own messages count as known details
            said = "\n".join(m["content"] for m in state["messages"] if m["role"] == "user")
            # Actions Hermes' own code really takes this turn, which the reply may therefore mention
            claims = set()
            if state["escalated"] or analysis.user_requested_ticket or analysis.urgency >= AUTO_ESCALATE_URGENCY:
                claims.add("escalated")
            if analysis.user_wants_to_close:
                claims.add("closed")  # the reply is replaced by Hermes' own confirmation below anyway
            hermes_guard.review(analysis, said, kb_context, state.get("ticket_id"), "chat", SAFE_CHAT_REPLY, claims)
        except Exception as e:
            # Details go to the server log only; the user gets a plain message
            print(f"❌ Hermes could not get an AI reply for {user['email']}: {e}")
            add_message(state, "assistant", "Sorry, I couldn't process that just now. Please try again in a moment.")
            return state
        try:
            notice = _decide_next_step(token, state, user, analysis, text)
        except Exception as e:
            # The answer is still useful even if GLPI refused an update; the choice buttons are withheld
            print(f"❌ Hermes could not update GLPI for {user['email']}: {e}")
            state["pending"] = None
            notice = "I couldn't update your ticket just now; the IT team has not been notified of this message."
            if analysis.user_wants_to_close:
                # Never let a reply say it was closed when it was not
                analysis.draft_reply = "Sorry, I couldn't close the ticket just now. Please try again in a moment."
                notice = None
        add_message(state, "assistant", analysis.draft_reply, notice)
    finally:
        _kill(token)
    return state

def handle_action(state, user, action):
    """Applies a button choice ('resolved', 'not_resolved', 'technician_yes', 'technician_no', 'request_technician')"""
    state = normalise_state(state)
    allowed = {a["id"] for a in available_actions(state)["actions"]}
    if action not in allowed:
        raise ValueError(f"Action '{action}' is not available in this conversation right now")
    pending = state.get("pending") or {}
    analysis = TicketAnalysis(**{"suggested_new_category": "", **pending["analysis"]}) if pending.get("analysis") else None
    token = get_glpi_session()
    try:
        ticket_id = state["ticket_id"]
        if action == "resolved":
            _note(token, ticket_id, _user_says("Confirmed the solution resolved the issue."), private=False)
            _set_status(token, ticket_id, CLOSED)
            state.update(outcome="self_solved", pending=None)
            _open_survey_now(token, ticket_id, user)
            add_message(state, "assistant", "Great, glad that sorted it! I've closed the request. Start a new conversation if anything else comes up.",
                        f"Ticket #{ticket_id} closed as resolved.")
        elif action == "not_resolved":
            _note(token, ticket_id, _user_says("The suggested fix did not resolve the issue."), private=False)
            _set_status(token, ticket_id, PENDING)
            state["pending"] = {**pending, "kind": "not_resolved_offer"}
            add_message(state, "assistant", "Sorry that didn't help. Would you like me to assign this to a technician, or shall we keep trying together?")
        elif action == "technician_yes":
            reason = "The suggested fix did not resolve the issue; a technician was requested." if pending.get("kind") == "not_resolved_offer" else None
            notice = escalate(token, state, user, analysis, reason=reason)
            add_message(state, "assistant", "Done — I've passed this to a technician, who will follow up with you. Anything else you add here goes onto the same ticket.", notice)
        elif action == "technician_no":
            state.update(declined_technician=True, pending=None)
            add_message(state, "assistant", "No problem, let's keep working on it. Tell me what you've tried or what you're seeing now. "
                        "You can ask for a technician at any time.")
        elif action == "request_technician":
            if analysis is None:
                # No AI analysis yet (for example right after a greeting): route with neutral defaults
                analysis = TicketAnalysis(category="General / Unknown", assigned_group="", urgency=3, impact=3,
                                          summary="User asked to speak to a technician from the portal chat.", suggested_new_category="",
                                          has_confident_solution=False, needs_more_info=True, user_requested_ticket=True, draft_reply="")
            notice = escalate(token, state, user, analysis, reason="The user asked to speak to a technician.")
            add_message(state, "assistant", "Done — I've passed this to a technician. Anything else you add here goes onto the same ticket.", notice)
    except Exception as e:
        print(f"❌ Hermes action '{action}' failed for {user['email']}: {e}")
        add_message(state, "assistant", "Sorry, I couldn't update your request just now. Please try again in a moment.")
    finally:
        _kill(token)
    return state
