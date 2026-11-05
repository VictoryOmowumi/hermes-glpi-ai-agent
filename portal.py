import os
import html
import re
import time
import base64
from datetime import datetime
import streamlit as st

from app import (
    category_mapping,
    category_list_text,
    TicketAnalysis,
    analyze_ticket,
    get_glpi_session,
    fetch_knowledge_base,
    glpi_write,
    group_list_text,
    pick_technician_for,
    route_group,
    add_routing_note,
    PERSONA,
    fetch_ticket,
    glpi_get,
    format_reply_html,
    REPLY_HEADER,
    SOLUTION_HEADER,
    SELF_SERVICE_SOURCE_ID,
    find_user_by_email,
    GENERAL_CATEGORY,
)
from hermes_engine import ticket_title
from portal_store import require_user, sign_out, is_admin, save_conversation, list_conversations, load_conversation

# Urgency at or above this level (4=High, 5=Very High) is logged immediately without asking the user first
AUTO_LOG_URGENCY = 4
# How many rounds of follow-up questions / suggestions the AI gets before the technician choice is offered
MAX_TRIAGE_ROUNDS = 2
# GLPI ticket statuses used by the self-service flow
ASSIGNED, PENDING, SOLVED, CLOSED = 2, 4, 5, 6

# =====================================================================
# 💬 FRONT-END WEB CHAT ASSISTANT INTERFACE (Streamlit Web App)
# Launch with: streamlit run portal.py
# =====================================================================
def build_prompt(messages, kb_context, ticket_id, triage_rounds_left=0, escalated=False):
    """Feeds the whole conversation to the AI so follow-up answers are read in context"""
    allowed_cats = category_list_text()
    transcript = "\n".join(f"{'USER' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}" for m in messages)

    no_technician = "No technician is involved yet. In 'draft_reply' you must NOT write that anything was escalated, forwarded or passed to a team or technician, because it has not been. Do not ask whether to involve a technician: the portal shows that choice itself."

    if ticket_id and escalated:
        ticket_note = f"Ticket #{ticket_id} is with a technician. Tell the user their new details have been added to ticket #{ticket_id}."
    elif triage_rounds_left > 0:
        ticket_note = f"""DIAGNOSIS STAGE. {no_technician}
    If no Knowledge Base article solves the issue, your 'draft_reply' must help narrow the problem down before any escalation:
    - Ask ONE or TWO short, specific questions a technician would need answered (for example: the exact error text, when it started, which device or network, whether colleagues are affected, what changed recently). Never repeat a question already answered above.
    - You MAY add one or two quick generic first checks that are safe for any employee (for example: re-type the password and check Caps Lock, restart the app or device, try another browser, check the cable or Wi-Fi). Present them as things to try, not as the official fix.
    - Keep it brief and friendly, and end on your question."""
    else:
        ticket_note = f"WRAP-UP STAGE. {no_technician} You have already asked your follow-up questions, so do NOT ask any more. If there is no Knowledge Base fix, summarise in one or two sentences what you now understand about the issue and say it needs a technician to look at it."

    return f"""
    {PERSONA} You are chatting live with an employee.

    CONVERSATION SO FAR (the last USER line is the newest message):
    {transcript}

    ---
    INTERNAL COMPANY KNOWLEDGE BASE ARTICLES (RAG Context):
    {kb_context}
    ---

    TICKET STATE: {ticket_note}

    YOUR CORE ACTIONS:
    1. Treat the conversation as ONE ongoing issue. Use every earlier message as context and never ask for details the user already gave.
    2. Categorize the issue using exactly one of these values (separated by semicolons): {allowed_cats}. If no specific category fits, also propose a name for a new category in 'suggested_new_category'.
    3. Set 'assigned_group' to the support group that should own it, using exactly one of these names (with what each handles, where known):
{group_list_text()}
    4. Compute Urgency and Impact on GLPI's scale (1=Very Low, 2=Low, 3=Medium, 4=High, 5=Very High).
    5. Cross-reference the Knowledge Base. If an article accurately solves the issue, provide the guide steps in 'draft_reply' and flag 'has_confident_solution' as True. If no documentation matches, toggle it to False.
    6. If the issue is still too vague to categorize, flag 'needs_more_info' as True.
    7. If the user's newest message explicitly asks for a ticket, a technician or a human, flag 'user_requested_ticket' as True.
    8. Write 'draft_reply' as your next chat message to the user, replying to their newest message and following the TICKET STATE instructions above. Never invent company-specific tools, portals or procedures that are not in the Knowledge Base.
    """

# =====================================================================
# 🎟️ AI SELF-SERVICE TICKETS (F1): every conversation is recorded in GLPI once Hermes understands the issue.
# The ticket stays Pending while Hermes and the user talk, becomes Solved when Hermes offers a fix, Closed when the
# user confirms (or after 24 hours with no reply, see app.py), and is assigned to a technician when needed.
# =====================================================================
def private_note(session_token, ticket_id, text):
    glpi_write("POST", "ITILFollowup", session_token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": text, "is_private": 1}})

def public_note(session_token, ticket_id, text):
    glpi_write("POST", "ITILFollowup", session_token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": text, "is_private": 0}})

def set_status(session_token, ticket_id, status):
    glpi_write("PUT", f"Ticket/{ticket_id}", session_token, {"input": {"id": ticket_id, "status": status}})

def open_self_service_ticket(session_token, analysis, linked_to=None):
    """Records the conversation in GLPI as an AI self-service ticket, Pending and not yet assigned to anyone"""
    messages = st.session_state.messages
    first_issue = next(m["content"] for m in messages if m["role"] == "user")
    user = st.session_state.user
    # Earlier turns go in the description; the current turn is added as a follow-up straight after
    earlier = messages[:-1]
    transcript_html = "<br>".join(f"<b>{'User' if m['role'] == 'user' else 'Hermes'}:</b> {html.escape(m['content'])}" for m in earlier) or "<i>The conversation starts in the follow-ups below.</i>"
    raised_by_html = f"<b>Raised via the portal by:</b> {html.escape(user['name'])} &lt;{html.escape(user['email'])}&gt;<br><br>"
    ticket_payload = {
        "input": {
            "name": f"Portal Request: {ticket_title(first_issue)}",
            "content": f"{html.escape(analysis.summary)}<br><br>{raised_by_html}<b>Portal chat transcript</b><br>{transcript_html}",
            "itilcategories_id": category_mapping().get(analysis.category, 0),
            "urgency": analysis.urgency,
            "impact": analysis.impact,
            "requesttypes_id": SELF_SERVICE_SOURCE_ID,
            "status": PENDING,
        }
    }
    # The signed-in user is the requester when they exist in GLPI (matched by email); otherwise their name and
    # email are still recorded in the description above
    requester_id = find_user_by_email(session_token, user["email"])
    if requester_id:
        ticket_payload["input"]["_users_id_requester"] = requester_id
    ticket_id = glpi_write("POST", "Ticket", session_token, ticket_payload).json()["id"]
    st.session_state.ticket_id = ticket_id
    st.session_state.escalated = False
    st.session_state.outcome = "self_service"
    # A closed ticket cannot be reopened, so a follow-up conversation gets a new ticket linked to the old one
    if linked_to:
        try:
            glpi_write("POST", "Ticket_Ticket", session_token, {"input": {"tickets_id_1": ticket_id, "tickets_id_2": linked_to, "link": 1}})
        except Exception:
            pass
    details = (f"<b>Category:</b> {html.escape(analysis.category)} · <b>Urgency:</b> {analysis.urgency} · <b>Impact:</b> {analysis.impact}"
               f"<br><b>Summary:</b> {html.escape(analysis.summary)}")
    follow_on = f" Follows on from closed ticket #{linked_to}." if linked_to else ""
    if not requester_id:
        follow_on += f" Requester {html.escape(user['email'])} has no GLPI account, so no requester is set on this ticket."
    private_note(session_token, ticket_id, f"🤖 Portal chat: Hermes opened this as an AI self-service request. No technician assigned yet.{follow_on}<br>{details}")
    return f"Recorded as self-service request #{ticket_id}."

def record_turn(session_token, user_text, reply_text=None):
    """Adds the latest exchange to the ticket's timeline, so the GLPI record holds the whole conversation"""
    content = f"💬 <b>User (portal chat)</b><br>{html.escape(user_text)}"
    if reply_text:
        content += f"<br><br>{REPLY_HEADER}<br>{format_reply_html(reply_text)}"
    public_note(session_token, st.session_state.ticket_id, content)

def ensure_ticket(session_token, analysis):
    if not st.session_state.ticket_id:
        open_self_service_ticket(session_token, analysis)

def offer_solution(session_token, analysis):
    """Posts Hermes' knowledge base fix as the ticket's solution; GLPI marks the ticket Solved and the 24-hour clock starts"""
    solution = {"input": {"itemtype": "Ticket", "items_id": st.session_state.ticket_id, "content": f"{SOLUTION_HEADER}<br><br>{format_reply_html(analysis.draft_reply)}"}}
    glpi_write("POST", "ITILSolution", session_token, solution)

def escalate(session_token, analysis, reason=None):
    """Hands the conversation's ticket to the least busy technician in the right group"""
    ensure_ticket(session_token, analysis)
    ticket_id = st.session_state.ticket_id
    group_id = route_group(analysis)
    if reason:
        public_note(session_token, ticket_id, f"💬 <b>User (portal chat)</b><br>{html.escape(reason)}")
    glpi_write("POST", "Group_Ticket", session_token, {"input": {"tickets_id": ticket_id, "groups_id": group_id, "type": 2}})
    try:
        technician = pick_technician_for(session_token, analysis, group_id)
    except Exception:
        technician = None
    if technician:
        glpi_write("POST", "Ticket_User", session_token, {"input": {"tickets_id": ticket_id, "users_id": technician["id"], "type": 2}})
    set_status(session_token, ticket_id, ASSIGNED)
    add_routing_note(session_token, ticket_id, analysis, group_id, technician, prefix="Portal chat: ")
    st.session_state.escalated = True
    st.session_state.outcome = "ticket"
    group_name = glpi_get(session_token, f"Group/{group_id}").get("name", analysis.assigned_group)
    assignee = f", assigned to {technician['name']}" if technician else ""
    return f"Ticket #{ticket_id} passed to the {group_name}{assignee}."

def add_message(role, content, notice=None):
    """Adds a chat message stamped with the time it was sent"""
    st.session_state.messages.append({"role": role, "content": content, "notice": notice, "time": datetime.now().isoformat(timespec="seconds")})

def time_label(stamp):
    """'14:05' for today, 'Oct 14, 14:05' for older messages"""
    sent = datetime.fromisoformat(stamp)
    return sent.strftime("%H:%M") if sent.date() == datetime.now().date() else sent.strftime("%b %d, %H:%M").replace(" 0", " ")

def pending_analysis():
    # Conversations saved before newer fields were added still load
    return TicketAnalysis(**{"suggested_new_category": "", **st.session_state.pending["analysis"]})

def run_action(action):
    """Runs a button's GLPI action and turns any failure into a chat message instead of a crash"""
    try:
        action(get_glpi_session())
    except Exception as e:
        add_message("assistant", f"⚠️ I couldn't update the ticket: {e}")
    st.session_state.pending = None
    persist()
    st.rerun()

def render_pending_buttons():
    """Shows the choice that follows the latest AI reply, inside the assistant's message bubble"""
    pending = st.session_state.pending
    if not pending:
        return

    # Still gathering details: no question to answer yet, just a quiet way to go straight to a technician
    if pending["kind"] == "triage":
        if st.button("Skip the questions and talk to a technician", key="skip_triage", type="tertiary", icon=":material/support_agent:"):
            def action(token):
                notice = escalate(token, pending_analysis(), reason="The user asked to skip the questions and speak to a technician.")
                add_message("assistant", "Done — I've passed this to a technician. Anything else you add here goes onto the same ticket.", notice)
            run_action(action)
        return

    with st.container(key="ticket_actions"):
        st.caption("Is your issue resolved?" if pending["kind"] == "solved_check" else "Do you need a technician to look at this?")
        left, right = st.columns(2)

        if pending["kind"] == "solved_check":
            if left.button("Yes, it's resolved", key="action_fixed", icon=":material/check_circle:", use_container_width=True):
                def action(token):
                    ticket_id = st.session_state.ticket_id
                    public_note(token, ticket_id, "💬 <b>User (portal chat)</b><br>Confirmed the solution resolved the issue.")
                    set_status(token, ticket_id, CLOSED)
                    st.session_state.outcome = "self_solved"
                    add_message("assistant", "Great, glad that sorted it! I've closed the request. Start a new conversation if anything else comes up.", f"Ticket #{ticket_id} closed as resolved.")
                run_action(action)
            if right.button("No, I need a technician", key="action_log_after_fix", icon=":material/support_agent:", type="primary", use_container_width=True):
                def action(token):
                    notice = escalate(token, pending_analysis(), reason="The suggested fix did not resolve the issue.")
                    add_message("assistant", "Sorry that didn't work. I've passed this to a technician, who will follow up with you. Anything else you add here goes onto the same ticket.", notice)
                run_action(action)

        elif pending["kind"] == "offer_ticket":
            if left.button("Yes, get a technician", key="action_log", icon=":material/support_agent:", type="primary", use_container_width=True):
                def action(token):
                    notice = escalate(token, pending_analysis())
                    add_message("assistant", "Done — I've passed this to a technician, who will follow up with you. Anything else you add here goes onto the same ticket.", notice)
                run_action(action)
            if right.button("No, thanks", key="action_decline", icon=":material/close:", use_container_width=True):
                def action(token):
                    analysis = pending_analysis()
                    ensure_ticket(token, analysis)
                    ticket_id = st.session_state.ticket_id
                    glpi_write("POST", "ITILSolution", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id,
                               "content": f"{SOLUTION_HEADER.replace('from the Knowledge Base', 'without a technician')}<br><br>The requester chose not to involve a technician."}})
                    st.session_state.outcome = "declined"
                    add_message("assistant", "No problem. I've recorded this as resolved without a technician. If you change your mind within 24 hours, just reply here.", f"Ticket #{ticket_id} marked as resolved; it closes automatically after 24 hours without a reply.")
                run_action(action)

def decide_next_step(session_token, analysis, user_prompt):
    """Applies the F1 rules after each reply and returns the notice line to show under it (or None)"""
    st.session_state.pending = None
    notices = []

    # A closed ticket cannot be reopened: this message starts a new ticket linked to the old one
    linked_to = None
    if st.session_state.ticket_id and int(fetch_ticket(session_token, st.session_state.ticket_id).get("status", 0)) == CLOSED:
        linked_to = st.session_state.ticket_id
        st.session_state.ticket_id = None
        st.session_state.escalated = False
        st.session_state.triage_rounds = 0

    # Already with a technician: Hermes keeps helping, and every exchange is added to the ticket
    if st.session_state.ticket_id and st.session_state.escalated:
        record_turn(session_token, user_prompt, analysis.draft_reply)
        return f"Added to ticket #{st.session_state.ticket_id}."

    # The user replied after a solution was offered: the conversation continues, so the ticket goes back to Pending
    if st.session_state.ticket_id and int(fetch_ticket(session_token, st.session_state.ticket_id).get("status", 0)) == SOLVED:
        set_status(session_token, st.session_state.ticket_id, PENDING)

    # Record the conversation as soon as Hermes understands the issue (greetings and vague openers are not recorded)
    triage_done = st.session_state.triage_rounds >= MAX_TRIAGE_ROUNDS
    # "Understood" means Hermes could place the issue in a real category, even if it still has questions to ask
    understood = (analysis.category != GENERAL_CATEGORY or not analysis.needs_more_info or analysis.user_requested_ticket
                  or analysis.has_confident_solution or triage_done)
    if not st.session_state.ticket_id and understood:
        notices.append(open_self_service_ticket(session_token, analysis, linked_to))
    if st.session_state.ticket_id:
        # A knowledge base fix is posted as the solution itself, so only the user's message is added here
        record_turn(session_token, user_prompt, None if analysis.has_confident_solution else analysis.draft_reply)

    if analysis.user_requested_ticket:
        notices.append(escalate(session_token, analysis))
    elif analysis.urgency >= AUTO_LOG_URGENCY and not analysis.has_confident_solution:
        notices.append(escalate(session_token, analysis) + " Escalated automatically because this looks urgent.")
    elif analysis.has_confident_solution:
        offer_solution(session_token, analysis)
        st.session_state.pending = {"kind": "solved_check", "analysis": analysis.model_dump()}
    elif not triage_done:
        st.session_state.triage_rounds += 1
        st.session_state.pending = {"kind": "triage", "analysis": analysis.model_dump()}
    else:
        st.session_state.pending = {"kind": "offer_ticket", "analysis": analysis.model_dump()}
    return " ".join(notices) or None

def persist():
    """Saves the current conversation to the chat history store after every change"""
    state = {
        "messages": st.session_state.messages,
        "ticket_id": st.session_state.ticket_id,
        "escalated": st.session_state.escalated,
        "pending": st.session_state.pending,
        "triage_rounds": st.session_state.triage_rounds,
    }
    st.session_state.conversation_id = save_conversation(st.session_state.conversation_id, st.session_state.user, state, st.session_state.outcome)

def start_conversation(conversation_id=None):
    """Resets the chat to a blank conversation, or reopens a saved one from the history"""
    saved = load_conversation(conversation_id, st.session_state.user) if conversation_id else None
    state, outcome = saved if saved else ({}, "open")
    st.session_state.conversation_id = conversation_id if saved else None
    st.session_state.messages = state.get("messages", [])
    st.session_state.ticket_id = state.get("ticket_id")
    # Conversations saved before F1 only had a ticket when it was escalated
    st.session_state.escalated = state.get("escalated", bool(state.get("ticket_id")))
    st.session_state.pending = state.get("pending")
    st.session_state.triage_rounds = state.get("triage_rounds", 0)
    st.session_state.outcome = outcome

# =====================================================================
# 🎨 LOOK AND FEEL (colors come from .streamlit/config.toml; this CSS adds shape, spacing and hover effects)
# =====================================================================
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")
ASSISTANT_AVATAR = ":material/support_agent:"
SUGGESTIONS = [
    (":material/vpn_lock:", "My VPN won't connect from home"),
    (":material/wifi_off:", "Wi-Fi keeps dropping on my floor"),
    (":material/lock_person:", "I'm locked out of my account"),
]
NOTICE_ICONS = {"ticket": ":material/confirmation_number:", "update": ":material/attach_file:"}

def inject_styles():
    # Neutral rgba tints adapt to whichever theme (light or dark) is active
    st.markdown("""
    <style>
    /* Centered reading column, like ChatGPT / Claude */
    [data-testid="stMainBlockContainer"] { max-width: 800px; padding-top: 2.5rem; padding-bottom: 7rem; }
    [data-testid="stBottomBlockContainer"] { max-width: 800px; }

    /* Chat messages: the user's own messages are a bubble on the right (no avatar); replies are open text */
    [data-testid="stChatMessage"] {
        border-radius: 14px; padding: 0.85rem 1.1rem; margin-bottom: 0.6rem;
        background: transparent; border: 1px solid transparent;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        background: rgba(128, 128, 128, 0.11); border-color: rgba(128, 128, 128, 0.16);
        margin-left: 18%;
    }
    [data-testid="stChatMessageAvatarUser"] { display: none; }
    [data-testid="stChatMessage"] [data-testid="stAlert"] { border-radius: 12px; }

    /* Chat input: a rounded floating box */
    [data-testid="stChatInput"], [data-testid="stChatInput"] > div { border-radius: 18px; }

    /* Escalation buttons and suggestion chips: outline pills */
    .st-key-suggestions button, .st-key-action_decline button { background: transparent; border: 1px solid rgba(128, 128, 128, 0.32); }
    .st-key-suggestions button:hover, .st-key-action_decline button:hover { background: rgba(128, 128, 128, 0.10); }
    /* Action colors: green confirms the fix, blue logs a ticket (primary), grey declines */
    .st-key-action_fixed button { background: #34A853; border: 1px solid #34A853; color: #FFFFFF; }
    .st-key-action_fixed button:hover { background: #2D9147; border-color: #2D9147; color: #FFFFFF; }
    .st-key-action_fixed button p, .st-key-action_fixed button span { color: #FFFFFF; }
    .st-key-ticket_actions button { font-weight: 600; min-height: 2.6rem; }

    /* Message timestamps: small and quiet, right-aligned under the user's own bubble */
    .msg-time { font-size: 0.7rem; opacity: 0.5; margin-top: -0.35rem; }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) .msg-time { text-align: right; }

    /* Empty-state greeting */
    .welcome { text-align: center; margin: 12vh 0 1.75rem; }
    .welcome h1 { font-size: 2rem; font-weight: 600; margin-bottom: 0.35rem; padding: 0; }
    .welcome p { opacity: 0.65; margin: 0; }

    /* ---------- Sidebar ---------- */
    [data-testid="stSidebarHeader"] { height: 2.5rem; min-height: 2.5rem; }
    /* Full-height column so the profile and sign-out sit at the bottom */
    [data-testid="stSidebarUserContent"] { padding-top: 0; padding-bottom: 1rem; min-height: calc(100vh - 3.5rem); display: flex; flex-direction: column; }
    [data-testid="stSidebarUserContent"] > div { flex: 1; display: flex; flex-direction: column; }
    [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] { flex: 1; }
    [data-testid="stLayoutWrapper"]:has(> .st-key-sidebar_footer) { margin-top: auto; }
    .st-key-sidebar_footer { padding-top: 0.75rem; border-top: 1px solid rgba(128, 128, 128, 0.18); gap: 0.35rem; }

    /* Brand row: logo + name */
    .brand { display: flex; align-items: center; gap: 0.7rem; margin: 0 0 1.2rem 0.35rem; }
    .brand img { height: 32px; width: auto; display: block; }
    .brand-mark {
        width: 32px; height: 32px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
        background: linear-gradient(135deg, #1A73E8, #1557B0); color: #FFFFFF; font-weight: 700; font-size: 0.9rem;
    }
    .brand-title { font-weight: 650; font-size: 1rem; line-height: 1.15; letter-spacing: -0.01em; }
    .brand-subtitle { font-size: 0.74rem; opacity: 0.6; line-height: 1.2; }

    /* New conversation: a full-width blue button with a softer corner than the chat pills */
    [data-testid="stSidebar"] button[kind="primary"] { border-radius: 12px; min-height: 2.6rem; }

    .history-label { font-size: 0.72rem; font-weight: 600; opacity: 0.5; text-transform: uppercase; letter-spacing: 0.07em; margin: 1.4rem 0 0.35rem 0.75rem; }

    /* Navigation-style rows: history items and footer links */
    .st-key-history { gap: 0.15rem; max-height: calc(100vh - 360px); overflow-y: auto; overflow-x: hidden; }
    .st-key-history button, .st-key-sidebar_footer button, .st-key-sidebar_footer a {
        width: 100%; border-radius: 10px; padding: 0.5rem 0.75rem; min-height: 2.4rem;
        justify-content: flex-start; transition: background-color 0.15s ease, color 0.15s ease;
    }
    .st-key-history button > div, .st-key-sidebar_footer button > div { justify-content: flex-start; width: 100%; min-width: 0; gap: 0.6rem; }
    .st-key-history [data-testid="stTooltipHoverTarget"] { justify-content: flex-start !important; }
    .st-key-history button:hover, .st-key-sidebar_footer button:hover, .st-key-sidebar_footer a:hover { background: rgba(128, 128, 128, 0.12); }
    /* The open conversation: blue tint and blue text, like a selected nav item */
    .st-key-history button:disabled { background: rgba(26, 115, 232, 0.14) !important; color: #1A73E8; opacity: 1; cursor: default; font-weight: 600; }
    .st-key-history button:disabled p, .st-key-history button:disabled span { color: #1A73E8; }
    @media (prefers-color-scheme: dark) {
        .st-key-history button:disabled { background: rgba(138, 180, 248, 0.16) !important; }
        .st-key-history button:disabled p, .st-key-history button:disabled span { color: #8AB4F8; }
    }
    .st-key-history button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; font-size: 0.88rem; }
    .st-key-history button span, .st-key-history button [data-testid="stMarkdownContainer"] { min-width: 0; overflow: hidden; }

    /* Profile row at the bottom */
    .profile-card { display: flex; align-items: center; gap: 0.7rem; padding: 0.55rem 0.6rem; border-radius: 12px; }
    .profile-badge {
        flex: 0 0 2.25rem; height: 2.25rem; border-radius: 50%; display: flex; align-items: center; justify-content: center;
        background: linear-gradient(135deg, #1A73E8, #1557B0); color: #FFFFFF; font-weight: 650; font-size: 0.85rem;
    }
    .profile-text { min-width: 0; line-height: 1.25; flex: 1; }
    .profile-name { font-weight: 600; font-size: 0.88rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .profile-email { font-size: 0.75rem; opacity: 0.6; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .profile-tag { display: inline-block; margin-top: 0.2rem; font-size: 0.62rem; font-weight: 600; padding: 0.05rem 0.45rem; border-radius: 999px; background: rgba(26, 115, 232, 0.14); color: #1A73E8; }
    </style>
    """, unsafe_allow_html=True)

def initials(name):
    parts = [p for p in re.split(r"[\s._@-]+", name) if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"

def logo_html():
    """The company logo from assets/logo.png at 32px high, or a lettered badge if the file is missing"""
    try:
        with open(LOGO_PATH, "rb") as logo_file:
            encoded = base64.b64encode(logo_file.read()).decode()
        return f'<img src="data:image/png;base64,{encoded}" alt="Company logo">'
    except OSError:
        return '<div class="brand-mark">IT</div>'

def type_out(text):
    """Reveals an already-finished reply word by word, for a live typing feel"""
    for piece in re.split(r"(\s+)", text):
        yield piece
        if piece.strip():
            time.sleep(0.012)

def show_notice(notice):
    """Shows the ticket line under a reply with a proper icon in place of the stored emoji"""
    text = re.sub(r"^[^\w#]+", "", notice)
    icon = NOTICE_ICONS["update"] if "update" in text.lower() else NOTICE_ICONS["ticket"]
    st.info(text, icon=icon)

def render_sidebar(user):
    with st.sidebar:
        st.markdown(f"""
        <div class="brand">
            {logo_html()}
            <div>
                <div class="brand-title">Hermes</div>
                <div class="brand-subtitle">SBC IT Service Desk</div>
            </div>
        </div>""", unsafe_allow_html=True)

        # A new conversation starts a fresh issue (and, if needed, a fresh ticket)
        if st.button("New conversation", icon=":material/add:", use_container_width=True, type="primary"):
            start_conversation()
            st.rerun()

        history = list_conversations(user)
        if history:
            st.markdown('<div class="history-label">Recent</div>', unsafe_allow_html=True)
        with st.container(key="history"):
            for item in history:
                if item["ticket_id"]:
                    icon = ":material/confirmation_number:"
                elif item["outcome"] == "self_solved":
                    icon = ":material/check_circle:"
                else:
                    icon = ":material/chat_bubble:"
                is_current = item["id"] == st.session_state.conversation_id
                if st.button(item["title"], key=f"conversation_{item['id']}", icon=icon, type="tertiary", use_container_width=True,
                             disabled=is_current, help=f"Last updated {item['updated_at'].replace('T', ' ')}"):
                    start_conversation(item["id"])
                    st.rerun()

        # Bottom of the sidebar: admin link, who is signed in, sign out
        with st.container(key="sidebar_footer"):
            if is_admin(user):
                st.page_link("pages/Analytics.py", label="Analytics", icon=":material/insights:", use_container_width=True)
            tag = "" if user["sso"] else '<div class="profile-tag" title="SSO is not configured, so this email is not verified">Demo sign-in</div>'
            st.markdown(f"""
            <div class="profile-card" title="{html.escape(user["name"])} &lt;{html.escape(user["email"])}&gt;">
                <div class="profile-badge">{html.escape(initials(user["name"]))}</div>
                <div class="profile-text">
                    <div class="profile-name">{html.escape(user["name"])}</div>
                    <div class="profile-email">{html.escape(user["email"])}</div>
                    {tag}
                </div>
            </div>""", unsafe_allow_html=True)
            if st.button("Sign out", icon=":material/logout:", type="tertiary", use_container_width=True):
                sign_out()

def render_welcome(user):
    first_name = user["name"].split()[0] if user["name"] else "there"
    st.markdown(f"""
    <div class="welcome">
        <h1>Hi {html.escape(first_name)}, I'm Hermes</h1>
        <p>Your SBC desk assistant. How can I help you today?</p>
    </div>""", unsafe_allow_html=True)
    with st.container(key="suggestions"):
        columns = st.columns(len(SUGGESTIONS))
        for column, (icon, suggestion) in zip(columns, SUGGESTIONS):
            if column.button(suggestion, icon=icon, use_container_width=True):
                st.session_state.queued_prompt = suggestion
                st.rerun()

def run_frontend_portal():
    st.set_page_config(page_title="Hermes · SBC IT Service Desk", page_icon=":material/support_agent:")
    inject_styles()
    user = require_user()
    st.session_state.user = user

    if not SELF_SERVICE_SOURCE_ID:
        st.error("Hermes is not fully configured: set HERMES_SELF_SERVICE_SOURCE_ID in .env to the ID of GLPI's "
                 "'AI Self-Service' request source (see docs/glpi-configuration-guide.md, Part C), then restart the portal.")
        st.stop()

    if "messages" not in st.session_state:
        start_conversation()

    render_sidebar(user)

    if not st.session_state.messages:
        render_welcome(user)
    elif st.session_state.ticket_id:
        st.caption(f":material/confirmation_number: This conversation is linked to ticket #{st.session_state.ticket_id}")

    last_index = len(st.session_state.messages) - 1
    for index, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar=ASSISTANT_AVATAR if msg["role"] == "assistant" else None):
            st.markdown(msg["content"])
            if msg.get("time"):
                st.markdown(f'<div class="msg-time">{time_label(msg["time"])}</div>', unsafe_allow_html=True)
            if msg.get("notice"):
                show_notice(msg["notice"])
            # The Yes / No choice lives inside the latest assistant reply
            if index == last_index and msg["role"] == "assistant":
                render_pending_buttons()

    user_prompt = st.chat_input("Message Hermes...") or st.session_state.pop("queued_prompt", None)
    if user_prompt:
        with st.chat_message("user"):
            st.markdown(user_prompt)
        add_message("user", user_prompt)

        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            try:
                with st.spinner("Hermes is checking the knowledge base..."):
                    session_token = get_glpi_session()
                    kb_context = fetch_knowledge_base(session_token)

                    triage_rounds_left = MAX_TRIAGE_ROUNDS - st.session_state.triage_rounds
                    prompt = build_prompt(st.session_state.messages, kb_context, st.session_state.ticket_id, triage_rounds_left, st.session_state.escalated)
                    analysis = analyze_ticket(prompt)
                    notice = decide_next_step(session_token, analysis, user_prompt)

                add_message("assistant", analysis.draft_reply, notice)
                persist()
                st.write_stream(type_out(analysis.draft_reply))
                st.rerun()
            except Exception as e:
                persist()
                st.error(f"Helpdesk Portal Sync Drop: {e}")

run_frontend_portal()
