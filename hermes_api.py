"""Hermes API: what the intranet (and any other front end) calls.

Callers are trusted back ends, never browsers. Each request must carry:
  X-Hermes-Key:  the shared service key (HERMES_API_KEY in .env)
  X-User-Email:  the email of the employee the back end has already signed in
  X-User-Name:   that employee's display name
The intranet's .NET back end is the caller in production; the sandbox stand-in plays that role during the proof of concept.
"""
import hmac
import html
import os
import re
import threading
from urllib.parse import unquote

import requests
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import hermes_engine as engine
import hermes_guard
import hermes_store as store
from app import (APP_TOKEN, GLPI_BASE_URL, clean_html, find_user_by_email, get_glpi_session, glpi_get, glpi_parallel, glpi_write,
                 open_surveys_for, search_values)

HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
STATUS_LABELS = {1: "New", 2: "In progress", 3: "Planned", 4: "Waiting for you", 5: "Solved", 6: "Closed"}
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._+\-']+@[A-Za-z0-9.\-]+")

router = APIRouter(prefix="/v1")
_conversation_locks = {}
_locks_guard = threading.Lock()

# =====================================================================
# Caller authentication
# =====================================================================
def caller(x_hermes_key: str = Header(default=""), x_user_email: str = Header(default=""), x_user_name: str = Header(default="")):
    if not HERMES_API_KEY:
        raise HTTPException(503, "The Hermes API is not configured (HERMES_API_KEY is missing)")
    if not hmac.compare_digest(x_hermes_key.encode(), HERMES_API_KEY.encode()):
        raise HTTPException(401, "Invalid service key")
    email = x_user_email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(400, "A valid X-User-Email header is required")
    # Callers URL-encode the name so non-ASCII names survive HTTP headers
    return {"email": email, "name": (unquote(x_user_name).strip() or email)[:120]}

def service_caller(x_hermes_key: str = Header(default="")):
    """For back-end jobs that act for the whole service rather than one employee (no user headers)"""
    if not HERMES_API_KEY:
        raise HTTPException(503, "The Hermes API is not configured (HERMES_API_KEY is missing)")
    if not hmac.compare_digest(x_hermes_key.encode(), HERMES_API_KEY.encode()):
        raise HTTPException(401, "Invalid service key")
    return True

def working_caller(user=Depends(caller)):
    """For anything that would change GLPI: refused while Hermes is paused or read-only (hermes_guard)"""
    if not hermes_guard.accepting_work():
        raise HTTPException(503, "IT Support chat is paused for maintenance. Please contact the IT Service Desk by email for now.")
    return user

def _lock_for(conversation_id):
    with _locks_guard:
        return _conversation_locks.setdefault(conversation_id, threading.Lock())

# =====================================================================
# Conversations
# =====================================================================
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=engine.MAX_MESSAGE_LENGTH)

class ActionIn(BaseModel):
    action: str = Field(max_length=40)

def _view(conversation_id, state):
    state = engine.normalise_state(state)
    return {
        "id": conversation_id,
        "messages": [{k: m.get(k) for k in ("role", "content", "notice", "time")} for m in state["messages"]],
        "ticket": {"id": state["ticket_id"], "escalated": state["escalated"]} if state["ticket_id"] else None,
        "choice": engine.available_actions(state),
    }

def _load(conversation_id, user):
    saved = store.load_conversation(conversation_id, user)
    if not saved:
        raise HTTPException(404, "Conversation not found")
    return engine.normalise_state(saved[0])

@router.get("/conversations")
def list_conversations(user=Depends(caller)):
    return [{"id": c["id"], "title": c["title"], "ticket_id": c["ticket_id"], "outcome": c["outcome"], "updated_at": c["updated_at"]}
            for c in store.list_conversations(user, limit=50)]

@router.post("/conversations")
def create_conversation(user=Depends(working_caller)):
    state = engine.new_state()
    conversation_id = store.save_conversation(None, user, state, state["outcome"])
    return _view(conversation_id, state)

@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, user=Depends(caller)):
    return _view(conversation_id, _load(conversation_id, user))

@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: int, body: MessageIn, user=Depends(working_caller)):
    with _lock_for(conversation_id):
        state = engine.handle_message(_load(conversation_id, user), user, body.text)
        store.save_conversation(conversation_id, user, state, state["outcome"])
    return _view(conversation_id, state)

@router.post("/conversations/{conversation_id}/actions")
def take_action(conversation_id: int, body: ActionIn, user=Depends(working_caller)):
    with _lock_for(conversation_id):
        try:
            state = engine.handle_action(_load(conversation_id, user), user, body.action)
        except ValueError as e:
            raise HTTPException(409, str(e))
        store.save_conversation(conversation_id, user, state, state["outcome"])
    return _view(conversation_id, state)

# =====================================================================
# Ticket update events (service-level: read by the intranet back end's notification worker, never by browsers)
# =====================================================================
class AckIn(BaseModel):
    ids: list[int] = Field(max_length=500)

@router.get("/events")
def pending_events(_=Depends(service_caller)):
    """Ticket updates not yet delivered: each has the requester's email, ticket number, title and message"""
    return store.pending_ticket_events(limit=100)

@router.post("/events/ack")
def acknowledge_events(body: AckIn, _=Depends(service_caller)):
    """Marks events as delivered so they are never sent twice"""
    return {"delivered": store.mark_ticket_events_delivered(body.ids)}

# =====================================================================
# My tickets (only tickets where the signed-in employee is the requester; public information only)
# =====================================================================
def _clean_list(value):
    return ", ".join(search_values(value)) or None

@router.get("/tickets")
def my_tickets(user=Depends(caller)):
    token = get_glpi_session()
    try:
        glpi_user = find_user_by_email(token, user["email"])
        if not glpi_user:
            return []
        headers = {"App-Token": APP_TOKEN, "Session-Token": token}
        # Search fields: 4 requester, 1 title, 2 ID, 12 status, 15 opened, 19 last update, 7 category, 8 group, 5 technician
        params = {"criteria[0][field]": 4, "criteria[0][searchtype]": "equals", "criteria[0][value]": glpi_user,
                  "sort": 19, "order": "DESC", "range": "0-49"}
        for i, field in enumerate((1, 2, 12, 15, 19, 7, 8, 5)):
            params[f"forcedisplay[{i}]"] = field
        response = requests.get(f"{GLPI_BASE_URL}/search/Ticket", headers=headers, params=params)
        rows = response.json().get("data", []) if response.status_code in (200, 206) else []
        # GLPI's search returns technicians as user IDs, so turn them into display names
        names = {}
        def technician_names(value):
            parts = search_values(value)
            for part in parts:
                if part.isdigit() and part not in names:
                    u = glpi_get(token, f"User/{part}")
                    names[part] = f"{u.get('firstname') or ''} {u.get('realname') or ''}".strip() or u.get("name")
            return ", ".join(names.get(p, p) for p in parts) or None
        surveys = store.user_surveys(user["email"])
        return [{"id": int(r["2"]), "title": clean_html(r.get("1") or ""), "status": STATUS_LABELS.get(int(r.get("12") or 0), "Unknown"),
                 "opened": r.get("15"), "updated": r.get("19"), "category": _clean_list(r.get("7")),
                 "team": _clean_list(r.get("8")), "technician": technician_names(r.get("5")),
                 # Solved tickets wait for the employee to accept or refuse the solution
                 "needs_confirmation": int(r.get("12") or 0) == 5,
                 # Closed tickets: {'status': 'pending' | 'answered' | 'expired', 'rating'}
                 "survey": surveys.get(int(r["2"]))} for r in rows]
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})

# =====================================================================
# Satisfaction surveys (one per closed ticket; only the person who raised it can answer)
# =====================================================================
class SurveyAnswerIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    reason: str = Field(default="", max_length=40)
    comment: str = Field(default="", max_length=1000)

@router.get("/surveys")
def my_surveys(user=Depends(caller)):
    """Surveys waiting for this person. 'popup' is False once they closed the floating card for that ticket."""
    return [{"ticket_id": s["ticket_id"], "title": s["title"], "closed_at": s["closed_at"], "expires_at": s["expires_at"],
             "popup": not s["dismissed_at"]} for s in store.pending_surveys(user["email"])]

@router.post("/surveys/{ticket_id}")
def answer_survey(ticket_id: int, body: SurveyAnswerIn, user=Depends(caller)):
    survey = store.get_survey(ticket_id, user["email"])
    if not survey:
        raise HTTPException(404, "There is no survey for this ticket")
    reason = body.reason.strip()
    if reason and reason not in store.SURVEY_REASONS:
        raise HTTPException(400, "Unknown reason")
    if body.rating > 2:
        reason = ""  # the 'what went wrong' choice is only asked for low ratings
    if not store.answer_survey(ticket_id, user["email"], body.rating, reason or None, body.comment.strip() or None):
        raise HTTPException(409, "This survey has already been answered or has expired")
    # Copied to GLPI's survey on the ticket by the change tracker within a minute (app.sync_survey_answers)
    return {"ticket_id": ticket_id, "rating": body.rating}

@router.post("/surveys/{ticket_id}/dismiss")
def dismiss_survey(ticket_id: int, user=Depends(caller)):
    if not store.get_survey(ticket_id, user["email"]):
        raise HTTPException(404, "There is no survey for this ticket")
    store.dismiss_survey(ticket_id, user["email"])
    return {"ticket_id": ticket_id}

class SolutionDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject)$")
    comment: str = Field(default="", max_length=2000)

@router.post("/tickets/{ticket_id}/solution")
def decide_solution(ticket_id: int, body: SolutionDecisionIn, user=Depends(working_caller)):
    """The requester accepts a solution (the ticket closes) or refuses it with a reason (it goes back to the technician)"""
    token = get_glpi_session()
    try:
        # The reads are independent, so they run side by side; nothing is changed until the requester check passes
        read = glpi_parallel({
            "user": lambda: find_user_by_email(token, user["email"]),
            "actors": lambda: glpi_get(token, f"Ticket/{ticket_id}/Ticket_User"),
            "ticket": lambda: glpi_get(token, f"Ticket/{ticket_id}"),
            "solutions": lambda: glpi_get(token, f"Ticket/{ticket_id}/ITILSolution"),
        })
        glpi_user = read["user"]
        if not glpi_user or not any(a.get("type") == 1 and a.get("users_id") == glpi_user for a in read["actors"]):
            raise HTTPException(404, "Ticket not found")
        ticket = read["ticket"]
        if int(ticket.get("status") or 0) != 5:
            raise HTTPException(409, "This ticket has no solution waiting for your confirmation")
        # Technicians sometimes mark a ticket Solved without writing a solution, so the record is optional
        waiting = [s for s in read["solutions"] if int(s.get("status") or 0) == 2]
        solution = max(waiting, key=lambda s: s["id"]) if waiting else None
        comment = body.comment.strip()
        if body.decision == "reject" and not comment:
            raise HTTPException(400, "Please say why the solution did not fix the issue")

        if body.decision == "accept":
            text = f"💬 <b>Requester confirmed on the intranet that the solution resolved the issue.</b>"
            if comment:
                text += f"<br>{html.escape(comment)}"
            glpi_write("POST", "ITILFollowup", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": text, "is_private": 0}})
            if solution:
                glpi_write("PUT", f"ITILSolution/{solution['id']}", token, {"input": {"id": solution["id"], "status": 3}})
            glpi_write("PUT", f"Ticket/{ticket_id}", token, {"input": {"id": ticket_id, "status": 6}})
            try:
                open_surveys_for(token, ticket_id, [user["email"]])  # the rating card appears straight away
            except Exception as e:
                print(f"⚠️ Could not open the satisfaction survey for ticket #{ticket_id}: {e}")
            return {"ticket_id": ticket_id, "status": STATUS_LABELS[6]}

        # Refused: record the reason, mark the solution refused and send the ticket back for more work.
        # If Hermes wrote the solution and no technician is assigned, Hermes' poller re-triages and assigns one.
        glpi_write("POST", "ITILFollowup", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "is_private": 0,
                   "content": f"💬 <b>Requester says the solution did not resolve the issue:</b><br>{html.escape(comment)}"}})
        if solution:
            glpi_write("PUT", f"ITILSolution/{solution['id']}", token, {"input": {"id": solution["id"], "status": 4}})
        glpi_write("PUT", f"Ticket/{ticket_id}", token, {"input": {"id": ticket_id, "status": 2}})
        return {"ticket_id": ticket_id, "status": STATUS_LABELS[2]}
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})

# Headings Hermes writes into GLPI follow-ups; on the intranet they become separate "You" and "Hermes" entries
USER_MARKER = "💬 User (portal chat)"
HERMES_MARKERS = ("✨ Hermes · SBC Desk Assistant", "✨ Automated AI Assistant Reply", "🤖 Resolved by Hermes from the Knowledge Base",
                  "🤖 AI Engine auto-resolved this request from the Knowledge Base")
_MARKER_PATTERN = re.compile("(" + "|".join(re.escape(m) for m in (USER_MARKER, *HERMES_MARKERS)) + ")")

def _strip_markers(text):
    return _MARKER_PATTERN.sub("", text).strip()

def _timeline_entries(followup, hermes_id, requester_id, name_of):
    """One GLPI follow-up as timeline entries with role 'you', 'hermes' or 'technician' and Markdown-friendly text"""
    text = clean_html(followup["content"])
    date = followup["date_creation"]
    author_id = followup["users_id"]
    if author_id == requester_id:
        return [{"date": date, "role": "you", "author": "You", "text": text}]
    if author_id != hermes_id:
        return [{"date": date, "role": "technician", "author": name_of(author_id), "text": text}]
    entries, role = [], "hermes"
    for piece in _MARKER_PATTERN.split(text):
        if piece == USER_MARKER:
            role = "you"
        elif piece in HERMES_MARKERS:
            role = "hermes"
        elif piece.strip():
            entries.append({"date": date, "role": role, "author": "You" if role == "you" else "Hermes", "text": piece.strip()})
    return entries

@router.get("/tickets/{ticket_id}")
def ticket_details(ticket_id: int, user=Depends(caller)):
    token = get_glpi_session()
    try:
        glpi_user = find_user_by_email(token, user["email"])
        actors = glpi_get(token, f"Ticket/{ticket_id}/Ticket_User") if glpi_user else []
        # Ownership check: only the ticket's requester may see it
        if not any(a.get("type") == 1 and a.get("users_id") == glpi_user for a in actors):
            raise HTTPException(404, "Ticket not found")
        ticket = glpi_get(token, f"Ticket/{ticket_id}")
        names = {}
        def name_of(user_id):
            if user_id not in names:
                u = glpi_get(token, f"User/{user_id}")
                names[user_id] = f"{u.get('firstname') or ''} {u.get('realname') or ''}".strip() or u.get("name")
            return names[user_id]
        me = int(glpi_get(token, "getFullSession")["session"]["glpiID"])
        category = clean_html(glpi_get(token, f"ITILCategory/{ticket['itilcategories_id']}").get("completename", "")) if ticket.get("itilcategories_id") else None
        groups = [clean_html(glpi_get(token, f"Group/{g['groups_id']}").get("name", "")) for g in glpi_get(token, f"Ticket/{ticket_id}/Group_Ticket") if g.get("type") == 2]
        # Public updates only: private technician notes (including Hermes' routing notes) are never returned
        updates = []
        for f in glpi_get(token, f"Ticket/{ticket_id}/ITILFollowup"):
            if not f.get("is_private"):
                updates.extend(_timeline_entries(f, me, glpi_user, name_of))
        solutions = glpi_get(token, f"Ticket/{ticket_id}/ITILSolution")
        latest = solutions[-1] if solutions else None
        return {
            "id": ticket_id,
            "title": clean_html(ticket.get("name") or ""),
            "status": STATUS_LABELS.get(int(ticket.get("status") or 0), "Unknown"),
            "opened": ticket.get("date_creation"),
            "updated": ticket.get("date_mod"),
            "category": category,
            "teams": groups,
            "technicians": [name_of(a["users_id"]) for a in actors if a.get("type") == 2 and a.get("users_id")],
            "updates": sorted(updates, key=lambda u: u["date"]),
            "solution": {"date": latest["date_creation"], "text": _strip_markers(clean_html(latest["content"]))} if latest else None,
            # True when the employee is expected to accept or refuse the latest solution
            "awaiting_confirmation": int(ticket.get("status") or 0) == 5,
            "survey": store.user_surveys(user["email"]).get(ticket_id),
        }
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
