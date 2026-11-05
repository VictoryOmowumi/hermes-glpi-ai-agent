"""Weekly service desk report: the figures the service desk presents every week, worked out from GLPI.

Definitions (they differ from the hand-made spreadsheet on purpose; see docs/weekly-report.md):
  Week               Monday 00:00 to the next Monday 00:00.
  New                Opened during the week.
  Brought forward    Opened before the week and not yet resolved when it started.
  Handled            New + brought forward.
  Resolved           Solved (or closed, when no solve date) during the week. Measured by GLPI's solve date, never
                     by "last update", which also moves when GLPI's scheduled job closes solved tickets.
  Open at week end   Handled but not resolved by the end of the week (works for past weeks too).
  SLA                Each ticket's own "Time to resolve" deadline from GLPI. A resolved ticket met it if it was
                     solved by the deadline. An open ticket is "overdue" once the deadline has passed. Tickets
                     without an SLA are counted separately, never as met.
  Handling time      Opening to resolution, in calendar hours. Median shown next to the average, and tickets
                     opened this week are kept apart from backlog cleared this week (old backlog skews the average).
Every technician on a ticket gets credit for it. GLPI's own "glpi" admin account counts as no technician.
"""
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import requests

import hermes_store as store
from app import APP_TOKEN, GLPI_BASE_URL, clean_html, get_glpi_session, glpi_list_all, glpi_parallel, glpi_search_all, search_values

# GLPI's own status names, as the service desk sees them in GLPI
STATUS_LABELS = {1: "New", 2: "Processing (assigned)", 3: "Processing (planned)", 4: "Pending", 5: "Solved", 6: "Closed"}
PRIORITY_LABELS = {1: "Very low", 2: "Low", 3: "Medium", 4: "High", 5: "Very high", 6: "Major"}
TYPE_LABELS = {1: "Incident", 2: "Request"}
FMT = "%Y-%m-%d %H:%M:%S"
# Search fields: 2 ID, 1 title, 12 status, 3 priority, 14 type, 15 opened, 17 solved, 16 closed, 18 time to resolve,
# 30 SLA name, 7 category, 5 technicians, 83 location
FIELDS = (2, 1, 12, 3, 14, 15, 17, 16, 18, 30, 7, 5, 83)
NO_TECHNICIAN = "No technician"
ADMIN_ACCOUNTS = {"glpi"}  # GLPI's built-in admin login: a ticket "assigned" to it has nobody working on it

def week_bounds(any_day):
    start = any_day - timedelta(days=any_day.weekday())
    return datetime.combine(start, datetime.min.time()), datetime.combine(start + timedelta(days=7), datetime.min.time())

def _when(value):
    try:
        return datetime.strptime(str(value)[:19], FMT) if value else None
    except ValueError:
        return None

def _hours(start, end):
    return round((end - start).total_seconds() / 3600, 2)

def _load(start, end):
    token = get_glpi_session()
    try:
        s, e = start.strftime(FMT), end.strftime(FMT)
        read = glpi_parallel({
            # Still open today and opened before the week ended
            "open": lambda: glpi_search_all(token, "Ticket", [
                {"field": 12, "searchtype": "equals", "value": "notold"},
                {"link": "AND", "field": 15, "searchtype": "lessthan", "value": e}], fields=FIELDS),
            # Resolved on or after the week started, and opened before it ended
            "solved": lambda: glpi_search_all(token, "Ticket", [
                {"field": 17, "searchtype": "morethan", "value": s},
                {"link": "AND", "field": 15, "searchtype": "lessthan", "value": e}], fields=FIELDS),
            # Closed without a solve date (rare), same window
            "closed": lambda: glpi_search_all(token, "Ticket", [
                {"field": 16, "searchtype": "morethan", "value": s},
                {"link": "AND", "field": 15, "searchtype": "lessthan", "value": e}], fields=FIELDS),
            "users": lambda: glpi_list_all(token, "User"),
        })
    finally:
        requests.get(f"{GLPI_BASE_URL}/killSession", headers={"App-Token": APP_TOKEN, "Session-Token": token})
    rows = {}
    for row in read["open"] + read["solved"] + read["closed"]:
        rows[int(row["2"])] = row
    return rows, read["users"]

def _ticket(row, start, end, names, admin_ids):
    opened = _when(row.get("15"))
    resolved = _when(row.get("17")) or _when(row.get("16"))
    deadline = _when(row.get("18"))
    tech_ids = [int(v) for v in search_values(row.get("5")) if v.isdigit() and int(v) not in admin_ids]
    resolved_in_week = bool(resolved and start <= resolved < end)
    if resolved_in_week:
        sla = "no_sla" if not deadline else ("met" if resolved <= deadline else "breached")
    elif not deadline:
        sla = "no_sla"
    else:
        sla = "overdue" if deadline < end else "on_track"
    category = clean_html(str(row.get("7") or "")) or "Uncategorised"
    return {
        "id": int(row["2"]),
        "title": clean_html(str(row.get("1") or "")),
        "status_now": STATUS_LABELS.get(int(row.get("12") or 0), "Unknown"),
        "priority": PRIORITY_LABELS.get(int(row.get("3") or 0), "Unknown"),
        "type": TYPE_LABELS.get(int(row.get("14") or 0), "Unknown"),
        "category": category,
        "category_group": category.split(" > ")[0],
        "location": clean_html(str(row.get("83") or "")) or "No location",
        "opened": opened.strftime(FMT) if opened else None,
        "resolved": resolved.strftime(FMT) if resolved else None,
        "sla_name": clean_html(str(row.get("30") or "")) or None,
        "sla_deadline": deadline.strftime(FMT) if deadline else None,
        "sla": sla,
        "new": bool(opened and opened >= start),
        "resolved_in_week": resolved_in_week,
        "handling_hours": _hours(opened, resolved) if resolved_in_week and opened else None,
        "technician_ids": tech_ids,
        "technicians": [names.get(uid, f"User {uid}") for uid in tech_ids] or [NO_TECHNICIAN],
    }

def _time_stats(tickets):
    hours = [t["handling_hours"] for t in tickets if t["handling_hours"] is not None]
    return {"count": len(hours), "average": round(statistics.mean(hours), 1) if hours else None,
            "median": round(statistics.median(hours), 1) if hours else None}

def _sla_stats(tickets):
    counts = Counter(t["sla"] for t in tickets)
    judged = counts["met"] + counts["breached"]
    return {"met": counts["met"], "breached": counts["breached"], "no_sla": sum(1 for t in tickets if t["sla"] == "no_sla"),
            "compliance": round(counts["met"] / judged, 4) if judged else None}

def _share(counter, total):
    return [{"name": name, "tickets": count, "share": round(count / total, 4) if total else 0} for name, count in counter.most_common()]

def weekly_report(any_day):
    start, end = week_bounds(any_day)
    rows, users = _load(start, end)
    names = {u["id"]: (f"{u.get('firstname') or ''} {u.get('realname') or ''}".strip() or u.get("name")) for u in users}
    admin_ids = {u["id"] for u in users if (u.get("name") or "").lower() in ADMIN_ACCOUNTS}
    tickets = []
    for row in rows.values():
        t = _ticket(row, start, end, names, admin_ids)
        opened = _when(t["opened"])
        resolved = _when(t["resolved"])
        # Handled: opened before the week ended, and not already resolved before it started
        if opened and opened < end and not (resolved and resolved < start):
            tickets.append(t)
    tickets.sort(key=lambda t: t["id"])

    new = [t for t in tickets if t["new"]]
    resolved = [t for t in tickets if t["resolved_in_week"]]
    open_end = [t for t in tickets if not t["resolved_in_week"]]
    total = len(tickets)

    daily = Counter((t["opened"] or "")[:10] for t in new)
    days = [(start + timedelta(days=i)).date() for i in range(7)]

    per_tech = defaultdict(list)
    for t in tickets:
        for name in t["technicians"]:
            per_tech[name].append(t)
    technicians = []
    for name, items in per_tech.items():
        done = [t for t in items if t["resolved_in_week"]]
        technicians.append({
            "technician": name, "assigned": len(items), "resolved": len(done), "open_at_end": len(items) - len(done),
            **{f"handling_{k}": v for k, v in _time_stats(done).items() if k != "count"},
            "sla_met": sum(t["sla"] == "met" for t in done), "sla_breached": sum(t["sla"] == "breached" for t in done),
            "overdue_open": sum(t["sla"] == "overdue" for t in items if not t["resolved_in_week"]),
        })
    technicians.sort(key=lambda r: (r["technician"] == NO_TECHNICIAN, -r["assigned"], r["technician"]))

    breach_by_priority = Counter(t["priority"] for t in resolved if t["sla"] == "breached")

    # Satisfaction: surveys sent for tickets closed this week, and answers given this week
    s_iso, e_iso = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    recent = store.list_surveys((start - timedelta(days=store.SURVEY_DAYS)).isoformat(timespec="seconds"))
    sent = [s for s in recent if s_iso <= s["closed_at"] < e_iso]
    answers = [s for s in recent if s["status"] == "answered" and s_iso <= (s["answered_at"] or "") < e_iso]
    ratings = [s["rating"] for s in answers]
    tech_ratings = defaultdict(list)
    for s in answers:
        for name in [n.strip() for n in (s["technicians"] or "").split(",") if n.strip()] or [NO_TECHNICIAN]:
            tech_ratings[name].append(s["rating"])
    for row in technicians:
        r = tech_ratings.get(row["technician"], [])
        row["rating_average"] = round(sum(r) / len(r), 2) if r else None
        row["ratings"] = len(r)
    satisfaction = {
        "sent": len(sent), "responses": len(answers),
        "response_rate": round(sum(1 for s in sent if s["status"] == "answered") / len(sent), 4) if sent else None,
        "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "satisfied": round(sum(1 for r in ratings if r >= 4) / len(ratings), 4) if ratings else None,
        "low_ratings": sum(1 for r in ratings if r <= 2),
    }
    return {
        "week_start": start.date().isoformat(), "week_end": (end - timedelta(days=1)).date().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "handled": total, "new": len(new), "brought_forward": total - len(new),
            "resolved": len(resolved), "resolved_new": sum(t["new"] for t in resolved),
            "resolved_backlog": sum(not t["new"] for t in resolved),
            "open_at_end": len(open_end), "new_still_open": sum(t["new"] for t in open_end),
            "resolution_rate": round(len(resolved) / total, 4) if total else None,
            "overdue_open": sum(t["sla"] == "overdue" for t in open_end),
            "high_priority_open": sum(t["priority"] in ("High", "Very high", "Major") for t in open_end),
        },
        "sla": {"resolved": _sla_stats(resolved), "resolved_new": _sla_stats([t for t in resolved if t["new"]]),
                "resolved_backlog": _sla_stats([t for t in resolved if not t["new"]]),
                "breached_by_priority": _share(breach_by_priority, sum(breach_by_priority.values()))},
        "satisfaction": satisfaction,
        "handling_time": {"all": _time_stats(resolved), "new": _time_stats([t for t in resolved if t["new"]]),
                          "backlog": _time_stats([t for t in resolved if not t["new"]])},
        "daily_new": [{"date": d.isoformat(), "day": d.strftime("%A"), "tickets": daily.get(d.isoformat(), 0)} for d in days],
        "by_type": _share(Counter(t["type"] for t in tickets), total),
        "by_priority": _share(Counter(t["priority"] for t in tickets), total),
        "by_location": _share(Counter(t["location"] for t in tickets), total),
        "by_category": _share(Counter(t["category_group"] for t in tickets), total),
        "open_by_status": _share(Counter(t["status_now"] for t in open_end), len(open_end)),
        "technicians": technicians,
        "tickets": [{k: v for k, v in t.items() if k != "technician_ids"} for t in tickets],
    }

if __name__ == "__main__":
    import json, sys
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    report = weekly_report(day)
    print(json.dumps({k: v for k, v in report.items() if k != "tickets"}, indent=2))
