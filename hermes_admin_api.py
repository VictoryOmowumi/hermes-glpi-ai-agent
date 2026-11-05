"""Hermes admin API: the service desk admin section of the intranet (analysis, category suggestions, audit log).

Same callers and headers as hermes_api. The intranet back end only forwards these routes for employees with an
admin role, so Hermes does not check roles itself; the signed-in employee in X-User-Email is recorded on every
decision in the audit log. Analysis is read live from GLPI; decisions are stored in Hermes' own data store.
Approving a suggestion creates the category in GLPI and moves its tickets, using the category account
(GLPI_CATEGORY_USER_TOKEN; Hermes' own account during the proof of concept).
"""
import html
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import hermes_guard
import hermes_routing as routing
import hermes_store as store
from app import (ALL_TIME, APP_TOKEN, GENERAL_CATEGORY, GLPI_BASE_URL, SELF_SERVICE_SOURCE_ID, category_mapping, clean_html,
                 get_category_session, get_glpi_session, glpi_get, glpi_list_all, glpi_parallel, glpi_search_all, glpi_write,
                 reset_category_cache, search_values)
from hermes_api import EMAIL_PATTERN, STATUS_LABELS, caller

router = APIRouter(prefix="/v1/admin")

# =====================================================================
# Analysis: how tickets were handled, read from GLPI
# =====================================================================
RESOLVED, ESCALATED, AUTO_CLOSED, IN_PROGRESS, NOT_HANDLED = "resolved", "escalated", "auto_closed", "in_progress", "not_handled"
AUTO_CLOSE_TEXT = "Closed automatically"
CORRECTION_TEXT = "Category corrected:"
CACHE_SECONDS = 60
_cache = {}  # period in days -> (data, loaded at)
_cache_lock = threading.Lock()

def _since(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00") if days else ALL_TIME

def _load_glpi_data(days):
    """Tickets opened in the period and Hermes' activity on them, read in full (page by page) but only as far back
    as the period goes, and cached per period for a minute so the page can be refreshed freely"""
    with _cache_lock:
        cached = _cache.get(days)
        if cached and time.time() - cached[1] < CACHE_SECONDS:
            return cached[0]
        since = _since(days)
        token = get_glpi_session()
        try:
            data = glpi_parallel({
                "hermes_id": lambda: int(glpi_get(token, "getFullSession")["session"]["glpiID"]),
                "tickets": lambda: glpi_list_all(token, "Ticket", newer_than=since),
                # A reply or solution is never older than its ticket, so the same cut-off applies
                "solutions": lambda: glpi_list_all(token, "ITILSolution", newer_than=since),
                "followups": lambda: glpi_list_all(token, "ITILFollowup", newer_than=since),
                # Technicians come from GLPI's search (field 5), filtered to the period by GLPI (field 15, opening date)
                "technician_rows": lambda: glpi_search_all(token, "Ticket", [{"field": 15, "searchtype": "morethan", "value": since}], fields=(2, 5)),
                "users": lambda: glpi_list_all(token, "User"),
            })
            data["technicians"] = {int(r["2"]): [int(v) for v in search_values(r.get("5")) if v.isdigit()]
                                   for r in data.pop("technician_rows")}
            data["categories"] = {cid: name for name, cid in category_mapping().items() if cid}
        finally:
            requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
        loaded_at = time.time()
        _cache[days] = (data, loaded_at)
        return data

def _classify(data):
    """One row per ticket with how it was handled. A ticket a technician worked on counts as escalated, even if
    Hermes first offered a fix: a person had to step in."""
    hermes_id = data["hermes_id"]
    names = {u["id"]: (f"{u.get('firstname') or ''} {u.get('realname') or ''}".strip() or u.get("name")) for u in data["users"]}
    categories = data["categories"]
    technicians = {tid: [names.get(uid, "Unknown") for uid in uids] for tid, uids in data["technicians"].items()}
    hermes_items = defaultdict(list)
    for item in data["solutions"] + data["followups"]:
        if item.get("itemtype") == "Ticket" and item.get("users_id") == hermes_id:
            hermes_items[item["items_id"]].append(item.get("content") or "")

    rows = []
    for t in data["tickets"]:
        if int(t.get("is_deleted") or 0):
            continue
        ticket_id, status = t["id"], int(t.get("status") or 0)
        from_chat = bool(SELF_SERVICE_SOURCE_ID) and int(t.get("requesttypes_id") or 0) == SELF_SERVICE_SOURCE_ID
        activity = [clean_html(c) for c in hermes_items.get(ticket_id, [])]
        if not (activity or from_chat):
            outcome = NOT_HANDLED
        elif status == 6 and any(AUTO_CLOSE_TEXT in text for text in activity):
            outcome = AUTO_CLOSED
        elif technicians.get(ticket_id) or status in (2, 3):
            outcome = ESCALATED
        elif status in (5, 6):
            outcome = RESOLVED
        else:
            outcome = IN_PROGRESS
        rows.append({
            "id": ticket_id,
            "title": clean_html(t.get("name") or ""),
            "created": t.get("date_creation"),
            "source": "IT Support chat" if from_chat else "Email / GLPI",
            "category": categories.get(t.get("itilcategories_id")) or "Uncategorised",
            "outcome": outcome,
            "status": STATUS_LABELS.get(status, "Unknown"),
            "technician": ", ".join(technicians.get(ticket_id, [])) or None,
            "category_corrected": any(CORRECTION_TEXT in text for text in activity),
        })
    return rows

@router.get("/analytics")
def analytics(days: int = Query(30, ge=0, le=365), user=Depends(caller)):
    """Headline numbers, daily and per-category breakdowns, and the tickets behind them. days=0 means all time."""
    rows = _classify(_load_glpi_data(days))
    cutoff = _since(days) if days else ""
    rows = [r for r in rows if (r["created"] or "") >= cutoff]
    handled = [r for r in rows if r["outcome"] != NOT_HANDLED]
    counts = Counter(r["outcome"] for r in rows)
    decided = counts[RESOLVED] + counts[ESCALATED]

    # Daily series covers every day in the period, so quiet days show as zero rather than disappearing
    start = datetime.now().date() - timedelta(days=days - 1) if days else min(
        (datetime.fromisoformat(r["created"][:10]).date() for r in handled if r["created"]), default=datetime.now().date())
    start = max(start, datetime.now().date() - timedelta(days=364))
    daily = {(start + timedelta(days=i)).isoformat(): Counter() for i in range((datetime.now().date() - start).days + 1)}
    for r in handled:
        day = (r["created"] or "")[:10]
        if day in daily:
            daily[day][r["outcome"]] += 1

    by_category = defaultdict(Counter)
    for r in handled:
        by_category[r["category"]][r["outcome"]] += 1
    by_technician = Counter(r["technician"] or "Team queue (no technician)" for r in handled if r["outcome"] == ESCALATED)

    conversations = [c for c in store.all_conversation_outcomes() if (c["created_at"] or "") >= cutoff]
    return {
        "period_days": days,
        "generated_at": datetime.fromtimestamp(_cache[days][1]).isoformat(timespec="seconds"),
        "totals": {
            "tickets": len(rows),
            "handled": len(handled),
            RESOLVED: counts[RESOLVED],
            ESCALATED: counts[ESCALATED],
            AUTO_CLOSED: counts[AUTO_CLOSED],
            IN_PROGRESS: counts[IN_PROGRESS],
            NOT_HANDLED: counts[NOT_HANDLED],
            # Share of finished, Hermes-handled tickets that needed no technician
            "resolution_rate": round(counts[RESOLVED] / decided, 3) if decided else None,
            "category_corrections": sum(1 for r in handled if r["category_corrected"]),
            "uncategorised": sum(1 for r in handled if r["category"] == "Uncategorised"),
            "conversations": len(conversations),
            "solved_in_chat_without_ticket": sum(1 for c in conversations if c["outcome"] == "self_solved"),
            "pending_suggestions": sum(g["ticket_count"] for g in store.category_suggestion_groups("pending")),
        },
        "daily": [{"day": day, **{k: c[k] for k in (RESOLVED, ESCALATED, AUTO_CLOSED, IN_PROGRESS)}} for day, c in daily.items()],
        "by_category": sorted(({"category": name, "total": sum(c.values()), **{k: c[k] for k in (RESOLVED, ESCALATED, AUTO_CLOSED, IN_PROGRESS)}}
                               for name, c in by_category.items()), key=lambda x: -x["total"]),
        "by_technician": [{"technician": name, "tickets": n} for name, n in by_technician.most_common()],
        "tickets": rows[:300],
    }

# =====================================================================
# Weekly report: the service desk's weekly figures (see hermes_report.py for the definitions)
# =====================================================================
_report_cache = {}  # week start -> (report, loaded at)
_report_lock = threading.Lock()

@router.get("/weekly-report")
def weekly_report(week: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"), user=Depends(caller)):
    """The week containing 'week' (YYYY-MM-DD, default this week), Monday to Sunday"""
    from hermes_report import week_bounds, weekly_report as build
    try:
        day = datetime.strptime(week, "%Y-%m-%d").date() if week else datetime.now().date()
    except ValueError:
        raise HTTPException(400, "Unknown date")
    if day > datetime.now().date() or day < datetime.now().date() - timedelta(days=730):
        raise HTTPException(400, "Choose a week in the last two years")
    key = week_bounds(day)[0].date().isoformat()
    with _report_lock:
        cached = _report_cache.get(key)
        if cached and time.time() - cached[1] < CACHE_SECONDS:
            return cached[0]
        report = build(day)
        _report_cache[key] = (report, time.time())
        return report

# =====================================================================
# Category suggestions: review queue and decisions
# =====================================================================
def tidy_name(name):
    """'Network>VPN  access' -> 'Network > VPN access': one spelling for GLPI's 'Parent > Child' names"""
    return " > ".join(" ".join(part.split()) for part in (name or "").split(">") if part.strip())

def _glpi_categories():
    """GLPI's real categories by full name (without Hermes' 'General / Unknown' placeholder)"""
    return [name for name in category_mapping() if name != GENERAL_CATEGORY]

def _proposed_name(group):
    """Where the new category should go: under the broad category its tickets were filed in, unless that was
    'General / Unknown' (then it is a new top-level category)"""
    filed = Counter(t["filed_under"] for t in group["tickets"] if t["filed_under"] and t["filed_under"] != GENERAL_CATEGORY)
    label = tidy_name(group["label"])
    if not filed:
        return label
    parent = filed.most_common(1)[0][0]
    # The AI sometimes includes the parent in the suggestion already
    return label if label.lower().startswith(parent.lower() + " >") else f"{parent} > {label}"

def _already_exists(name, categories):
    """True when GLPI already has this category, or another category with the same final name"""
    full = {c.lower() for c in categories}
    leaves = {c.split(">")[-1].strip().lower() for c in categories}
    return name.lower() in full or name.split(">")[-1].strip().lower() in leaves

@router.get("/category-suggestions")
def category_suggestions(status: str = Query("pending", pattern="^(pending|approved|declined)$"), user=Depends(caller)):
    """Suggested new categories grouped into one proposal per name, with the tickets behind each, plus GLPI's
    current categories so the review screen can show where a new category will be placed"""
    try:
        categories = _glpi_categories()
    except Exception:
        categories = None  # GLPI is down: suggestions can still be read, but not approved
    groups = store.category_suggestion_groups(status)
    for group in groups:
        group["proposed_name"] = group["approved_label"] or _proposed_name(group)
        group["already_exists"] = None if categories is None else _already_exists(group["proposed_name"], categories)
    return {"proposals": groups, "categories": categories}

class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    keys: list[str] = Field(min_length=1, max_length=50)
    note: str = Field(default="", max_length=500)
    # The full name to approve per proposal, including any parent ("vpn access" -> "Network > VPN access")
    labels: dict[str, str] = Field(default_factory=dict, max_length=50)

class CategoryChangeRefused(Exception):
    """GLPI would not create the category (usually a missing right on the account's profile)"""

def _apply_in_glpi(full_name, proposal, user):
    """Creates the approved category in GLPI (or reuses it if it already exists) and moves the proposal's tickets
    into it. A ticket is only moved while it is still in the category Hermes filed it under, so a category a
    technician has set since is never overwritten. Returns what was done, for the audit log."""
    token = get_category_session()
    try:
        categories = {clean_html(c["completename"]): c for c in glpi_list_all(token, "ITILCategory")}
        parent_name, _, leaf = full_name.rpartition(" > ")
        if full_name in categories:
            category_id, created = categories[full_name]["id"], False
        else:
            parent = categories.get(parent_name) if parent_name else None
            if parent_name and not parent:
                raise HTTPException(400, f"There is no category called '{parent_name}' in GLPI")
            new_category = {"name": leaf, "itilcategories_id": parent["id"] if parent else 0,
                            # Same visibility as the parent; top-level categories are shared from the root entity
                            "entities_id": parent["entities_id"] if parent else 0, "is_recursive": parent["is_recursive"] if parent else 1,
                            "is_helpdeskvisible": 1, "is_incident": 1, "is_request": 1,
                            "comment": f"Created from Hermes' category review, approved by {user['name']}"}
            try:
                response = glpi_write("POST", "ITILCategory", token, {"input": new_category})
            except Exception as e:
                print(f"⚠️ GLPI refused to create category '{full_name}': {e}")
                raise CategoryChangeRefused(
                    "GLPI refused to create the category. The GLPI account Hermes uses for categories needs the Create right "
                    "on ITIL categories (Administration > Profiles > its profile > Setup tab).")
            category_id, created = int(response.json()["id"]), True

        moved, skipped = [], []
        for ticket in proposal["tickets"]:
            ticket_id = ticket["ticket_id"]
            try:
                current = int(glpi_get(token, f"Ticket/{ticket_id}").get("itilcategories_id") or 0)
                filed_id = categories.get(ticket["filed_under"], {}).get("id", 0)
                if current not in (filed_id, category_id):
                    skipped.append({"ticket_id": ticket_id, "reason": "its category was changed since Hermes filed it"})
                    continue
                if current != category_id:
                    glpi_write("PUT", f"Ticket/{ticket_id}", token, {"input": {"id": ticket_id, "itilcategories_id": category_id}})
                moved.append(ticket_id)
                try:
                    glpi_write("POST", "ITILFollowup", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "is_private": 1,
                               "content": f"🗂️ Category set to <b>{html.escape(full_name)}</b> after the category review "
                                          f"(approved by {html.escape(user['name'])})."}})
                except Exception:
                    pass  # The note is a courtesy for technicians; the move itself has succeeded
            except Exception as e:
                print(f"⚠️ Could not move Ticket #{ticket_id} to '{full_name}': {e}")
                skipped.append({"ticket_id": ticket_id, "reason": "GLPI refused the change (closed tickets may be locked)"})
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
    # Hermes starts using the new category for incoming tickets straight away
    reset_category_cache()
    return {"category_id": category_id, "category_created": created, "moved": moved, "skipped": skipped}

@router.post("/category-suggestions/decisions")
def decide(body: DecisionIn, user=Depends(caller)):
    note = body.note.strip()
    if body.decision == "decline" and not note:
        raise HTTPException(400, "Please give a reason for declining")
    keys = [store.suggestion_key(k) for k in body.keys if store.suggestion_key(k)]
    labels = {}
    if body.decision == "approve":
        # The approved name is exactly what will be created later, so a parent must be a real GLPI category
        categories = {c.lower(): c for c in _glpi_categories()}
        for key, label in body.labels.items():
            label = tidy_name(label)
            if not 2 <= len(label) <= 80:
                raise HTTPException(400, "Category names must be between 2 and 80 characters")
            parent, _, leaf = label.rpartition(" > ")
            if parent and parent.lower() not in categories:
                raise HTTPException(400, f"There is no category called '{parent}' in GLPI to place '{label}' under")
            # Store the parent with GLPI's own spelling ("infrastructure services" -> "Infrastructure Services")
            labels[store.suggestion_key(key)] = f"{categories[parent.lower()]} > {leaf}" if parent else label

    if body.decision == "decline":
        decided = store.decide_category_suggestions(keys, "decline", user, note, {})
        if not decided:
            raise HTTPException(409, "These suggestions have already been decided")
        return {"decided": decided, "results": []}

    # Approvals are applied in GLPI one proposal at a time, and each is recorded only once GLPI has accepted it,
    # so a failure part-way leaves the remaining proposals pending rather than approved on paper only
    pending = {g["key"]: g for g in store.category_suggestion_groups("pending")}
    results = []
    for key in dict.fromkeys(keys):
        proposal = pending.get(key)
        if not proposal:
            continue
        full_name = labels.get(key) or _proposed_name(proposal)
        try:
            outcome = _apply_in_glpi(full_name, proposal, user)
        except CategoryChangeRefused as e:
            if results:
                raise HTTPException(502, f"{len(results)} approved, then stopped at '{full_name}': {e}")
            raise HTTPException(502, str(e))
        store.decide_category_suggestions([key], "approve", user, note or None, {key: full_name}, outcome)
        results.append({"key": key, "name": full_name, **outcome})
    if not results:
        raise HTTPException(409, "These suggestions have already been decided")
    return {"decided": len(results), "results": results}

# =====================================================================
# Routing suggestions: group in charge, regroup, reassign (worked out in hermes_routing)
# =====================================================================
@router.get("/routing-suggestions")
def routing_suggestions(status: str = Query("pending", pattern="^(pending|approved|declined)$"), user=Depends(caller)):
    return routing.pending_suggestions() if status == "pending" else store.list_routing_decisions(status)

class RoutingDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    ids: list[str] = Field(min_length=1, max_length=50)
    note: str = Field(default="", max_length=500)

@router.post("/routing-suggestions/decisions")
def decide_routing(body: RoutingDecisionIn, user=Depends(caller)):
    note = body.note.strip()
    if body.decision == "decline" and not note:
        raise HTTPException(400, "Please give a reason for declining")
    results, error, status_code = routing.decide([i[:120] for i in body.ids], body.decision, note or None, user)
    if error:
        # Earlier suggestions in the same request may already be applied, so say how many
        raise HTTPException(status_code, f"{len(results)} done, then stopped: {error}" if results else error)
    if not results:
        raise HTTPException(409, "These suggestions have already been decided or no longer apply. Refresh the list.")
    return {"decided": len(results), "results": results}

# =====================================================================
# Technician availability: leave periods, so Hermes does not assign tickets to someone who is away
# =====================================================================
def _person_name(user):
    return f"{user.get('firstname') or ''} {user.get('realname') or ''}".strip() or user.get("name") or f"User {user.get('id')}"

def _technicians(token):
    """Members of the groups that can be assigned tickets, with their teams and open ticket counts"""
    read = glpi_parallel({
        "groups": lambda: glpi_list_all(token, "Group"),
        "members": lambda: glpi_list_all(token, "Group_User"),
        "users": lambda: glpi_list_all(token, "User"),
        # Open tickets with their technicians (search fields: 2 ID, 5 technicians)
        "open": lambda: glpi_search_all(token, "Ticket", [{"field": 12, "searchtype": "equals", "value": "notold"}], fields=(2, 5)),
    })
    teams = {g["id"]: clean_html(g.get("completename") or g.get("name") or "") for g in read["groups"] if int(g.get("is_assign") or 0)}
    users = {u["id"]: u for u in read["users"]}
    load = Counter(int(v) for row in read["open"] for v in search_values(row.get("5")) if v.isdigit())
    team_of = defaultdict(list)
    for link in read["members"]:
        if link["groups_id"] in teams:
            team_of[link["users_id"]].append(teams[link["groups_id"]])
    return {uid: {"id": uid, "name": _person_name(users.get(uid, {"id": uid})), "teams": sorted(names),
                  "active": bool(int(users.get(uid, {}).get("is_active") or 0)), "open_tickets": load[uid]}
            for uid, names in team_of.items()}

@router.get("/technicians")
def technicians(user=Depends(caller)):
    token = get_glpi_session()
    try:
        people = _technicians(token)
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
    on_leave = store.absent_technicians()
    for person in people.values():
        person["on_leave_until"] = on_leave.get(person["id"])
    return {"technicians": sorted(people.values(), key=lambda p: p["name"].lower()), "absences": store.list_absences()}

class LeaveIn(BaseModel):
    starts_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ends_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str = Field(default="", max_length=200)

@router.post("/technicians/{user_id}/leave")
def add_leave(user_id: int, body: LeaveIn, user=Depends(caller)):
    try:
        start, end = datetime.strptime(body.starts_on, "%Y-%m-%d").date(), datetime.strptime(body.ends_on, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Enter valid dates")
    if end < start:
        raise HTTPException(400, "The last day of leave must be on or after the first day")
    if end < datetime.now().date():
        raise HTTPException(400, "This leave has already ended")
    if (end - start).days > 365:
        raise HTTPException(400, "Leave longer than a year should be handled by deactivating the account in GLPI")
    token = get_glpi_session()
    try:
        person = _technicians(token).get(user_id)
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
    if not person:
        raise HTTPException(404, "That person is not a technician in any support team")
    absence_id = store.add_absence(user_id, person["name"], body.starts_on, body.ends_on, body.note.strip() or None, user)
    return {"id": absence_id}

@router.delete("/technicians/leave/{absence_id}")
def cancel_leave(absence_id: int, user=Depends(caller)):
    if not store.remove_absence(absence_id, user):
        raise HTTPException(404, "That leave no longer exists")
    return {"removed": absence_id}

# =====================================================================
# Data protection: retention, and one person's data on request (Nigeria Data Protection Act)
# Transcripts are never browsable here: an admin only sees counts for one exact email, and an export or
# deletion needs a request reference. Every export and deletion is in the audit log.
# =====================================================================
class PersonIn(BaseModel):
    email: str = Field(max_length=254)

class PersonRequestIn(PersonIn):
    # The data protection request this is for (ticket number, letter reference...)
    reference: str = Field(min_length=3, max_length=200)

def _subject(email):
    email = email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(400, "Enter the person's full email address")
    return email

@router.get("/data-protection")
def data_protection_summary(user=Depends(caller)):
    return store.retention_summary()

@router.post("/data-protection/lookup")
def data_protection_lookup(body: PersonIn, user=Depends(caller)):
    """How much Hermes holds about one person: counts and dates only, never content"""
    return store.person_data_counts(_subject(body.email))

@router.post("/data-protection/export")
def data_protection_export(body: PersonRequestIn, user=Depends(caller)):
    return store.export_person_data(_subject(body.email), user, body.reference.strip())

@router.post("/data-protection/delete")
def data_protection_delete(body: PersonRequestIn, user=Depends(caller)):
    email = _subject(body.email)
    if email == user["email"]:
        # Not a security boundary (the data is the admin's own), but a deletion request should go through someone else
        raise HTTPException(400, "Ask another administrator to handle a request for your own data")
    return store.delete_person_data(email, user, body.reference.strip())

# =====================================================================
# Safety: off switch, limits, change log, withheld replies and read-only results (hermes_guard)
# =====================================================================
class PauseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)

@router.get("/safety")
def safety(user=Depends(caller)):
    return {**hermes_guard.status(),
            "actions": store.list_agent_actions(200),
            "events": store.list_guard_events(100),
            "shadow": store.list_shadow_tickets(300) if hermes_guard.READ_ONLY or store.list_shadow_tickets(1) else []}

@router.post("/safety/pause")
def pause_hermes(body: PauseIn, user=Depends(caller)):
    record = hermes_guard.pause(body.reason.strip(), f"{user['name']} <{user['email']}>")
    store.audit(user, "safety.pause", "Hermes", {"reason": record["reason"]})
    return hermes_guard.status()

@router.post("/safety/resume")
def resume_hermes(user=Depends(caller)):
    previous = hermes_guard.paused()
    if not previous:
        raise HTTPException(409, "Hermes is not paused")
    hermes_guard.resume(f"{user['name']} <{user['email']}>")
    store.audit(user, "safety.resume", "Hermes", {"was_paused_for": previous.get("reason")})
    return hermes_guard.status()

# =====================================================================
# Satisfaction: ratings from closed tickets (hermes_store surveys)
# =====================================================================
def _rating_rows(surveys, key):
    groups = defaultdict(list)
    for s in surveys:
        for name in [n.strip() for n in (s.get(key) or "").split(",") if n.strip()] or ["(none)"]:
            groups[name].append(s["rating"])
    return sorted(({"name": name, "responses": len(r), "average": round(sum(r) / len(r), 2),
                    "satisfied": round(sum(1 for x in r if x >= 4) / len(r), 4)} for name, r in groups.items()),
                  key=lambda row: (-row["responses"], row["name"]))

def satisfaction_summary(surveys):
    answered = [s for s in surveys if s["status"] == "answered"]
    ratings = [s["rating"] for s in answered]
    return {
        "surveys": len(surveys), "responses": len(answered),
        "response_rate": round(len(answered) / len(surveys), 4) if surveys else None,
        "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "satisfied": round(sum(1 for r in ratings if r >= 4) / len(ratings), 4) if ratings else None,
        "distribution": [{"stars": n, "responses": ratings.count(n)} for n in range(5, 0, -1)],
    }

@router.get("/satisfaction")
def satisfaction(days: int = Query(30, ge=1, le=365), user=Depends(caller)):
    """Ratings for tickets closed in the last 'days' days. Comments are shown to service desk admins here, and to
    technicians on the ticket in GLPI (copied to GLPI's satisfaction survey)."""
    surveys = store.list_surveys(_since(days))
    answered = [s for s in surveys if s["status"] == "answered"]
    return {
        "period_days": days,
        **satisfaction_summary(surveys),
        "copied_to_glpi": sum(1 for s in answered if s["glpi_synced"]),
        "reasons": [{"name": r, "responses": sum(1 for s in answered if s["reason"] == r)} for r in store.SURVEY_REASONS],
        "by_technician": _rating_rows(answered, "technicians"),
        "by_team": _rating_rows(answered, "team"),
        "by_category": _rating_rows(answered, "category"),
        "by_channel": _rating_rows(answered, "channel"),
        "responses_list": [{k: s[k] for k in ("ticket_id", "user_email", "title", "channel", "category", "team", "technicians", "closed_at",
                                               "answered_at", "rating", "reason", "comment", "followed_up_by", "followed_up_at", "follow_up_note")}
                           for s in sorted(answered, key=lambda s: s["answered_at"], reverse=True)],
    }

class FollowUpIn(BaseModel):
    ticket_id: int
    user_email: str = Field(max_length=200)
    note: str = Field(min_length=3, max_length=500)

@router.post("/satisfaction/follow-up")
def follow_up(body: FollowUpIn, user=Depends(caller)):
    """Records that someone followed up a low rating with the requester"""
    if not store.follow_up_survey(body.ticket_id, body.user_email, user, body.note.strip()):
        raise HTTPException(404, "That response was not found")
    return {"ticket_id": body.ticket_id}

# =====================================================================
# Audit log
# =====================================================================
@router.get("/audit-log")
def audit_log(limit: int = Query(200, ge=1, le=500), user=Depends(caller)):
    return store.list_audit_log(limit)
