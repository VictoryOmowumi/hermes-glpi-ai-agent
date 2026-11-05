"""Routing suggestions for the service desk admin section: which team and technician should own tickets.

Four kinds, all worked out from facts in GLPI (no AI, so nothing is sent to an AI provider):
  group_in_charge  Technicians keep moving a category's tickets from the group Hermes chose to another group:
                   suggest making that group the category's "Group in charge" (a lasting routing rule).
  regroup          An open ticket sits in a group other than its category's Group in charge: suggest moving it
                   there, with a technician from that group.
  assign           An open ticket is in a group's queue with no technician: suggest the least busy active member.
  reassign         An open ticket's technician is inactive, on leave, or not a member of the ticket's group: suggest
                   the least busy available member of the group instead.
Pending suggestions are computed live each time; decided ones are kept in hermes_store and never proposed again.
"""
import html
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import requests

import hermes_store as store
from app import (APP_TOKEN, GLPI_BASE_URL, category_mapping, category_owner, clean_html, get_category_session,
                 get_glpi_session, glpi_get, glpi_list_all, glpi_parallel, glpi_search_all, glpi_write, reset_category_cache,
                 search_values)

# How many moved tickets it takes before Hermes suggests a new Group in charge for a category
MIN_MOVED_TICKETS = 2
# How far back routing rules look for tickets technicians moved (open tickets are always checked, whatever their age)
EVIDENCE_DAYS = 90
OPEN_STATUSES = (1, 2, 3, 4)
ROUTING_NOTE = re.compile(r"routed via \S+ AI Engine to (.+?)(?: \(the category's group in charge\))?(?:, technician: |, no technician available|\.$|\.\s)")

class SuggestionOutdated(Exception):
    """The ticket or category changed since the suggestion was worked out"""

def _person(user):
    return f"{user.get('firstname') or ''} {user.get('realname') or ''}".strip() or user.get("name") or f"User {user.get('id')}"

def _load(token):
    """Everything the suggestions need, read fresh from GLPI (decisions must be based on the current state).
    Every read is complete (paged), and GLPI's search does the filtering, so this holds up with thousands of tickets."""
    reset_category_cache()
    since = (datetime.now() - timedelta(days=EVIDENCE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    # Independent reads, side by side. The ticket search returns open tickets of any age (for team and technician
    # fixes) plus every ticket opened in the evidence window (for routing rules).
    # Search fields: 2 ID, 1 title, 12 status, 7 category, 5 technicians, 8 technician groups.
    read = glpi_parallel({
        "categories": category_mapping,
        "groups": lambda: glpi_list_all(token, "Group"),
        "users": lambda: glpi_list_all(token, "User"),
        "members": lambda: glpi_list_all(token, "Group_User"),
        "rows": lambda: glpi_search_all(token, "Ticket", [
            {"field": 12, "searchtype": "equals", "value": "notold"},
            {"link": "OR", "field": 15, "searchtype": "morethan", "value": since},
        ], fields=(2, 1, 12, 7, 5, 8)),
        "hermes_id": lambda: int(glpi_get(token, "getFullSession")["session"]["glpiID"]),
        "followups": lambda: glpi_list_all(token, "ITILFollowup", newer_than=since),
    })
    category_ids = {name: cid for name, cid in read["categories"].items() if cid}
    categories = {cid: name for name, cid in category_ids.items()}
    groups = {g["id"]: clean_html(g.get("completename") or g.get("name") or "") for g in read["groups"]}
    group_ids_by_name = {}
    for gid, full in groups.items():
        group_ids_by_name.setdefault(full.lower(), gid)
        group_ids_by_name.setdefault(full.split(">")[-1].strip().lower(), gid)  # older routing notes used the short name
    users = {u["id"]: u for u in read["users"]}
    members = defaultdict(set)
    for link in read["members"]:
        members[link["groups_id"]].add(link["users_id"])

    rows = read["rows"]
    tickets, ticket_groups, ticket_techs = {}, defaultdict(list), defaultdict(list)
    for row in rows:
        ticket_id = int(row["2"])
        category = clean_html(str(row.get("7") or ""))
        tickets[ticket_id] = {"id": ticket_id, "name": row.get("1") or "", "status": int(row.get("12") or 0),
                              "itilcategories_id": category_ids.get(category, 0)}
        ticket_groups[ticket_id] = [group_ids_by_name[g.lower()] for g in search_values(row.get("8")) if g.lower() in group_ids_by_name]
        ticket_techs[ticket_id] = [int(v) for v in search_values(row.get("5")) if v.isdigit()]

    hermes_id = read["hermes_id"]
    # The group Hermes chose for each ticket, from its private routing note (the latest one wins)
    hermes_group = {}
    for f in sorted(read["followups"], key=lambda f: f["id"]):
        if f.get("itemtype") == "Ticket" and f.get("users_id") == hermes_id:
            match = ROUTING_NOTE.search(clean_html(f.get("content") or ""))
            if match and match.group(1).strip().lower() in group_ids_by_name:
                hermes_group[f["items_id"]] = group_ids_by_name[match.group(1).strip().lower()]
    open_load = Counter(uid for tid, uids in ticket_techs.items() if int(tickets[tid].get("status") or 0) in OPEN_STATUSES for uid in uids)
    return {"categories": categories, "groups": groups, "users": users, "members": members, "tickets": tickets,
            "ticket_groups": ticket_groups, "ticket_techs": ticket_techs, "hermes_group": hermes_group, "open_load": open_load,
            "on_leave": store.absent_technicians()}

def _active(data, user_id):
    return user_id in data["users"] and int(data["users"][user_id].get("is_active") or 0)

def _available(data, user_id):
    """Active in GLPI and not marked on leave in the service desk console"""
    return _active(data, user_id) and user_id not in data["on_leave"]

def _best_technician(data, group_id, category_name=None):
    """The category's technician in charge if available, else the available group member with the fewest open tickets"""
    _, owner = category_owner(category_name) if category_name else (0, 0)
    if owner and _available(data, owner):
        return owner
    candidates = [uid for uid in data["members"].get(group_id, ()) if _available(data, uid)]
    return min(candidates, key=lambda uid: (data["open_load"][uid], uid)) if candidates else None

def _ticket_ref(data, ticket_id):
    return {"ticket_id": ticket_id, "ticket_title": clean_html(data["tickets"][ticket_id].get("name") or "").replace("Portal Request: ", "")}

def compute_suggestions(token):
    data = _load(token)
    decided = store.routing_decided_fingerprints()
    names = lambda uid: _person(data["users"][uid]) if uid in data["users"] else None
    suggestions = []

    # 1. Group in charge: tickets Hermes routed that a person then moved to another group, grouped by category
    moves = defaultdict(list)
    for ticket_id, chosen in data["hermes_group"].items():
        ticket = data["tickets"].get(ticket_id)
        current = data["ticket_groups"].get(ticket_id, [])
        category_id = int((ticket or {}).get("itilcategories_id") or 0)
        if ticket and category_id and current and chosen not in current:
            moves[(category_id, current[0])].append((ticket_id, chosen))
    for (category_id, group_id), moved in moves.items():
        category = data["categories"].get(category_id)
        if not category or len(moved) < MIN_MOVED_TICKETS or category_owner(category)[0] == group_id:
            continue
        fingerprint = f"group_in_charge:{category_id}:{group_id}"
        if fingerprint in decided:
            continue
        current_owner = category_owner(category)[0]
        suggestions.append({
            "id": fingerprint, "kind": "group_in_charge",
            "title": f"Send {category} tickets to {data['groups'][group_id]}",
            "category": category, "category_id": category_id,
            "group": data["groups"][group_id], "group_id": group_id,
            "current_group": data["groups"].get(current_owner) if current_owner else None,
            "reason": f"Technicians moved {len(moved)} tickets in this category from the group Hermes chose to {data['groups'][group_id]}.",
            "tickets": [{**_ticket_ref(data, tid), "from_group": data["groups"].get(src)} for tid, src in moved],
        })

    # 2 and 3. Open tickets in the wrong group, or with a technician who cannot work on them
    for ticket_id, ticket in data["tickets"].items():
        if int(ticket.get("status") or 0) not in OPEN_STATUSES:
            continue
        category = data["categories"].get(int(ticket.get("itilcategories_id") or 0))
        groups, techs = data["ticket_groups"].get(ticket_id, []), data["ticket_techs"].get(ticket_id, [])
        target = category_owner(category)[0] if category else 0
        # Also a ticket with more than one group (for example two parts of Hermes both assigned it): keep only the
        # group in charge
        shared = target in groups and len(set(groups)) > 1
        if target and groups and (target not in groups or shared) and target in data["groups"]:
            fingerprint = f"regroup:{ticket_id}:{target}"
            if fingerprint not in decided:
                # Keep a technician already on the ticket when they belong to the group in charge
                keep = [t for t in techs if t in data["members"].get(target, ()) and _available(data, t)]
                technician = keep[0] if keep else _best_technician(data, target, category)
                group_name = data["groups"][target]
                suggestions.append({
                    "id": fingerprint, "kind": "regroup",
                    "title": f"Keep ticket #{ticket_id} with {group_name} only" if shared else f"Move ticket #{ticket_id} to {group_name}",
                    **_ticket_ref(data, ticket_id), "category": category,
                    "from_group": ", ".join(data["groups"].get(g, "?") for g in groups), "group": group_name, "group_id": target,
                    "from_technician": ", ".join(filter(None, map(names, techs))) or None,
                    "technician": names(technician), "technician_id": technician,
                    "reason": (f"The ticket is with {len(set(groups))} groups at once; {group_name} is the group in charge of {category}."
                               if shared else f"{group_name} is the group in charge of {category}."),
                })
            continue  # moving the ticket also replaces its technician, so no separate reassignment
        if groups and not techs and data["groups"].get(groups[0]):
            # In a team's queue with nobody working on it (for example after a person moved it to another team)
            fingerprint = f"assign:{ticket_id}:{groups[0]}"
            technician = _best_technician(data, groups[0], category)
            if technician and fingerprint not in decided:
                suggestions.append({
                    "id": fingerprint, "kind": "assign",
                    "title": f"Assign ticket #{ticket_id} to {names(technician)}",
                    **_ticket_ref(data, ticket_id), "category": category, "group": data["groups"][groups[0]], "group_id": groups[0],
                    "from_technician": None, "technician": names(technician), "technician_id": technician,
                    "reason": f"Waiting in the {data['groups'][groups[0]]} queue with no technician.",
                })
            continue
        for tech in techs:
            group_id = groups[0] if groups else 0
            if _available(data, tech) and (not group_id or tech in data["members"].get(group_id, ())):
                continue
            fingerprint = f"reassign:{ticket_id}:{tech}"
            replacement = _best_technician(data, group_id, category) if group_id else None
            if fingerprint in decided or not replacement or replacement == tech:
                continue
            if not _active(data, tech):
                why = "is no longer active in GLPI"
            elif tech in data["on_leave"]:
                why = f"is on leave until {data['on_leave'][tech]}"
            else:
                why = f"is not a member of {data['groups'].get(group_id, 'its group')}"
            suggestions.append({
                "id": fingerprint, "kind": "reassign",
                "title": f"Reassign ticket #{ticket_id} to {names(replacement)}",
                **_ticket_ref(data, ticket_id), "category": category, "group": data["groups"].get(group_id), "group_id": group_id,
                "from_technician": names(tech), "from_technician_id": tech,
                "technician": names(replacement), "technician_id": replacement,
                "reason": f"{names(tech)} {why}.",
            })
    order = {"group_in_charge": 0, "regroup": 1, "assign": 2, "reassign": 3}
    return sorted(suggestions, key=lambda s: (order[s["kind"]], s.get("ticket_id") or 0))

def _note(token, ticket_id, text):
    try:
        glpi_write("POST", "ITILFollowup", token, {"input": {"itemtype": "Ticket", "items_id": ticket_id, "is_private": 1, "content": text}})
    except Exception:
        pass  # The note is for technicians; the change itself has been made

def apply_suggestion(token, suggestion, user):
    """Makes the approved change in GLPI after checking nothing has changed since it was suggested. Category rules
    use the category account; ticket changes use Hermes' own account."""
    who = html.escape(user["name"])
    if suggestion["kind"] == "group_in_charge":
        category_token = get_category_session()
        try:
            glpi_write("PUT", f"ITILCategory/{suggestion['category_id']}", category_token,
                       {"input": {"id": suggestion["category_id"], "groups_id": suggestion["group_id"]}})
        finally:
            requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": category_token})
        reset_category_cache()
        return {"category_id": suggestion["category_id"], "group_in_charge": suggestion["group_id"]}

    ticket_id = suggestion["ticket_id"]
    if int(glpi_get(token, f"Ticket/{ticket_id}").get("status") or 0) not in OPEN_STATUSES:
        raise SuggestionOutdated(f"Ticket #{ticket_id} is no longer open")
    group_links = [g for g in glpi_get(token, f"Ticket/{ticket_id}/Group_Ticket") if g.get("type") == 2]
    tech_links = [u for u in glpi_get(token, f"Ticket/{ticket_id}/Ticket_User") if u.get("type") == 2]

    if suggestion["kind"] == "regroup":
        if group_links and all(g["groups_id"] == suggestion["group_id"] for g in group_links):
            raise SuggestionOutdated(f"Ticket #{ticket_id} is already with {suggestion['group']} only")
        for link in group_links:
            if link["groups_id"] != suggestion["group_id"]:
                glpi_write("DELETE", f"Group_Ticket/{link['id']}", token, None)
        if not any(g["groups_id"] == suggestion["group_id"] for g in group_links):
            glpi_write("POST", "Group_Ticket", token, {"input": {"tickets_id": ticket_id, "groups_id": suggestion["group_id"], "type": 2}})
        for link in tech_links:
            if link["users_id"] != suggestion.get("technician_id"):
                glpi_write("DELETE", f"Ticket_User/{link['id']}", token, None)
        if suggestion.get("technician_id") and not any(l["users_id"] == suggestion["technician_id"] for l in tech_links):
            glpi_write("POST", "Ticket_User", token, {"input": {"tickets_id": ticket_id, "users_id": suggestion["technician_id"], "type": 2}})
        assignee = f", technician {html.escape(suggestion['technician'])}" if suggestion.get("technician") else ""
        _note(token, ticket_id, f"🔀 Moved to <b>{html.escape(suggestion['group'])}</b>{assignee} after the routing review (approved by {who}).")
        return {"ticket_id": ticket_id, "group_id": suggestion["group_id"], "technician_id": suggestion.get("technician_id")}

    if suggestion["kind"] == "assign":
        if tech_links:
            raise SuggestionOutdated(f"Ticket #{ticket_id} already has a technician")
        glpi_write("POST", "Ticket_User", token, {"input": {"tickets_id": ticket_id, "users_id": suggestion["technician_id"], "type": 2}})
        _note(token, ticket_id, f"🔀 Assigned to <b>{html.escape(suggestion['technician'])}</b> from {html.escape(suggestion['group'])} "
                                f"after the routing review (approved by {who}).")
        return {"ticket_id": ticket_id, "technician_id": suggestion["technician_id"]}

    # reassign
    old = [l for l in tech_links if l["users_id"] == suggestion["from_technician_id"]]
    if not old:
        raise SuggestionOutdated(f"{suggestion['from_technician']} is no longer assigned to ticket #{ticket_id}")
    for link in old:
        glpi_write("DELETE", f"Ticket_User/{link['id']}", token, None)
    if not any(l["users_id"] == suggestion["technician_id"] for l in tech_links):
        glpi_write("POST", "Ticket_User", token, {"input": {"tickets_id": ticket_id, "users_id": suggestion["technician_id"], "type": 2}})
    _note(token, ticket_id, f"🔀 Reassigned from {html.escape(suggestion['from_technician'])} to <b>{html.escape(suggestion['technician'])}</b> "
                            f"after the routing review (approved by {who}).")
    return {"ticket_id": ticket_id, "technician_id": suggestion["technician_id"]}

def pending_suggestions():
    token = get_glpi_session()
    try:
        return compute_suggestions(token)
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})

def decide(ids, decision, note, user):
    """Approves (applying each in GLPI first) or declines the given pending suggestions, one at a time.
    Returns (results, error, status code): a failure part-way stops there and leaves the rest pending."""
    token = get_glpi_session()
    results = []
    try:
        pending = {s["id"]: s for s in compute_suggestions(token)}
        for suggestion_id in dict.fromkeys(ids):
            suggestion = pending.get(suggestion_id)
            if not suggestion:
                continue
            if decision == "decline":
                if store.record_routing_decision(suggestion, "declined", user, note):
                    results.append({"id": suggestion_id, "title": suggestion["title"]})
                continue
            try:
                outcome = apply_suggestion(token, suggestion, user)
            except SuggestionOutdated as e:
                return results, f"{e}. Refresh the list to see the current suggestions.", 409
            except Exception as e:
                print(f"⚠️ Could not apply routing suggestion {suggestion_id}: {e}")
                return results, (f"GLPI refused the change for '{suggestion['title']}'. Check the rights of the GLPI account Hermes uses "
                                 "(ITIL categories: Update for category rules; tickets: Update and assign for ticket changes)."), 502
            store.record_routing_decision(suggestion, "approved", user, note, outcome)
            results.append({"id": suggestion_id, "title": suggestion["title"], **outcome})
        return results, None, 200
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
