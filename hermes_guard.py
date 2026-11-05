"""Hermes' safety controls: the limits on what Hermes can do, whatever the AI says or a bug in Hermes does.

1. Off switch      An admin can pause Hermes from the IT Service Desk console. While paused, every change to GLPI
                   is refused and Hermes stops picking up tickets. The setting survives a restart.
2. Limits          At most HERMES_MAX_WRITES_PER_HOUR changes to GLPI an hour, and HERMES_MAX_WRITES_PER_TICKET an hour
                   on any one ticket. Going over pauses Hermes automatically: something is wrong (a loop, a bug).
3. Read-only mode  HERMES_READ_ONLY=true: nothing is ever sent to GLPI. Each change Hermes would have made is logged
                   instead (shadow mode, for testing against production).
4. Reply checks    Every AI reply is checked before anyone sees it (check_reply). A reply that fails is withheld and a
                   standard reply is sent instead; technicians see why in a private note.
5. Change log      Every change Hermes makes in GLPI is logged (field values only, never message text), 90 days.

All GLPI changes go through app.glpi_write, which calls before_write and after_write here.
"""
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import urlparse

import hermes_store as store

READ_ONLY = os.getenv("HERMES_READ_ONLY", "false").strip().lower() == "true"
MAX_WRITES_PER_HOUR = int(os.getenv("HERMES_MAX_WRITES_PER_HOUR", "400") or 400)
MAX_WRITES_PER_TICKET = int(os.getenv("HERMES_MAX_WRITES_PER_TICKET", "30") or 30)
# Links a reply may contain: these domains (and their sub-domains), plus links that appear in the Knowledge Base
ALLOWED_LINK_DOMAINS = [d.strip().lower() for d in os.getenv("HERMES_ALLOWED_LINK_DOMAINS", "sevenup.org").split(",") if d.strip()]
# Contact details a reply may always give (for example the service desk's own mailbox and phone line)
ALLOWED_CONTACTS = [c.strip().lower() for c in os.getenv("HERMES_ALLOWED_CONTACTS", "").split(",") if c.strip()]
MAX_REPLY_LENGTH = 5000
PAUSE_KEY = "paused"

class HermesPaused(Exception):
    """Raised instead of changing GLPI while Hermes is paused (by an admin or by a limit)"""

# =====================================================================
# 1. Off switch
# =====================================================================
_pause_cache = {"value": None, "read_at": 0.0}

def paused():
    """The pause record ({'by', 'reason', 'at', 'automatic'}) or None. Re-read every few seconds so a pause from the
    console reaches every Hermes thread almost at once."""
    if time.time() - _pause_cache["read_at"] > 3:
        _pause_cache.update(value=store.get_guard_state(PAUSE_KEY), read_at=time.time())
    return _pause_cache["value"]

def pause(reason, by, automatic=False):
    record = {"by": by, "reason": reason[:300], "at": datetime.now().isoformat(timespec="seconds"), "automatic": automatic}
    store.set_guard_state(PAUSE_KEY, record)
    _pause_cache.update(value=record, read_at=time.time())
    print(f"⏸️ Hermes paused by {by}: {reason}")
    return record

def resume(by):
    store.set_guard_state(PAUSE_KEY, None)
    _pause_cache.update(value=None, read_at=time.time())
    print(f"▶️ Hermes resumed by {by}")

def accepting_work():
    """False while paused or read-only: no new chats or ticket work that would change GLPI"""
    return not READ_ONLY and not paused()

# =====================================================================
# 2. Limits, 3. read-only mode and 5. the change log (called by app.glpi_write)
# =====================================================================
_lock = threading.Lock()
_recent = deque()                     # times of recent writes
_recent_by_ticket = defaultdict(deque)
# Field values worth recording; free text (replies, notes, descriptions) is never logged
LOGGED_FIELDS = ("id", "status", "itilcategories_id", "urgency", "impact", "type", "groups_id", "users_id", "tickets_id",
                 "tickets_id_1", "tickets_id_2", "link", "itemtype", "items_id", "is_private", "requesttypes_id")

def ticket_of(path, payload):
    """The ticket a write is about, when there is one"""
    match = re.match(r"^Ticket/(\d+)", path)
    if match:
        return int(match.group(1))
    data = (payload or {}).get("input") or {}
    if isinstance(data, dict):
        if data.get("itemtype") == "Ticket" and data.get("items_id"):
            return int(data["items_id"])
        for key in ("tickets_id", "tickets_id_1"):
            if data.get(key):
                return int(data[key])
    return None

def _fields(payload):
    data = (payload or {}).get("input") or {}
    return {k: data[k] for k in LOGGED_FIELDS if isinstance(data, dict) and k in data}

def before_write(method, path, payload):
    """Refuses the write while paused, and pauses Hermes when a limit is passed. Returns the ticket ID."""
    ticket_id = ticket_of(path, payload)
    target = path.split("?")[0]
    if paused():
        store.record_agent_action("blocked", method, target, ticket_id, _fields(payload), "Refused: Hermes is paused")
        raise HermesPaused(f"Hermes is paused ({paused()['reason']}), so it cannot change GLPI")
    if READ_ONLY:
        return ticket_id
    now = time.time()
    with _lock:
        while _recent and now - _recent[0] > 3600:
            _recent.popleft()
        per_ticket = _recent_by_ticket[ticket_id] if ticket_id else None
        while per_ticket and now - per_ticket[0] > 3600:
            per_ticket.popleft()
        reason = None
        if len(_recent) >= MAX_WRITES_PER_HOUR:
            reason = f"more than {MAX_WRITES_PER_HOUR} changes to GLPI in an hour"
        elif per_ticket is not None and len(per_ticket) >= MAX_WRITES_PER_TICKET:
            reason = f"more than {MAX_WRITES_PER_TICKET} changes to ticket #{ticket_id} in an hour"
        if reason:
            pause(f"Paused automatically: {reason}. Check the change log before resuming.", "Hermes safety limit", automatic=True)
            store.record_guard_event("auto_pause", ticket_id, "limit", [reason])
            store.record_agent_action("blocked", method, target, ticket_id, _fields(payload), f"Refused: {reason}")
            raise HermesPaused(f"Hermes paused itself: {reason}")
        _recent.append(now)
        if per_ticket is not None:
            per_ticket.append(now)
    return ticket_id

def after_write(method, path, payload, ticket_id, outcome):
    store.record_agent_action("shadow" if READ_ONLY else "live", method, path.split("?")[0], ticket_id, _fields(payload), outcome)

class ShadowResponse:
    """Stands in for GLPI's answer in read-only mode, so Hermes carries on and logs everything it would have done"""
    status_code = 200
    text = '{"id": 0}'

    @staticmethod
    def json():
        return {"id": 0}

# =====================================================================
# 4. Reply checks
# =====================================================================
URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\"'\]]+|(?<![@\w.])(?:[a-z0-9-]+\.)+(?:com|org|net|io|ng|co|info|biz|xyz|app|dev|ly|me)(?:/[^\s<>()\"'\]]*)?\b")
EMAIL_PATTERN = re.compile(r"(?i)\b[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
# 10 or more digits, allowing spaces, dashes and a leading + (phone numbers); shorter numbers are order or ticket numbers
PHONE_PATTERN = re.compile(r"(?<![\w])\+?\d(?:[\s-]?\d){9,}(?![\w])")
SECRET_WORDS = r"(?:password|passcode|pass\s*word|pin\b|otp\b|one[- ]time (?:code|password)|verification code|mfa code|2fa code|backup codes?|security code|authenticator code)"
# Asking someone to hand over a secret ("send us your password"), not telling them to type it into a sign-in screen
CREDENTIAL_REQUEST = re.compile(
    rf"(?i)\b(?:send|share|provide|give|tell|email (?:me|us|it|them)|reply with|forward|paste)\b[^.\n]{{0,60}}\b{SECRET_WORDS}"
    rf"|\b(?:your|the)\s+{SECRET_WORDS}[^.\n]{{0,40}}\b(?:to (?:me|us|hermes)|in (?:this|the) chat|by (?:email|reply))")
WEAKEN_SECURITY = re.compile(
    r"(?i)\b(?:disable|turn off|switch off|uninstall|bypass|deactivate|whitelist|exclude)\b[^.\n]{0,40}"
    r"\b(?:antivirus|anti-virus|defender|firewall|endpoint protection|crowdstrike|sophos|mfa|multi-factor|two-factor|2fa|bitlocker|encryption|security software|uac|user account control)\b")
# "Never share your password" and "do not disable the antivirus" are good advice, not a problem
NEGATION = re.compile(r"(?i)\b(?:never|not|no one|nobody|avoid|unable|cannot|can't|cant|won't|don't|dont|couldn't|isn't|wasn't|hasn't|haven't)\b|n['’]t\b")
# The AI cannot do anything itself, so a reply must never claim it did. Hermes' own code confirms real actions.
ACTION_CLAIMS = {
    "closed": re.compile(r"(?i)\b(?:ticket|request|case|issue|incident)\b[^.\n]{0,40}\b(?:has been|have been|is now|was|is)\s+(?:closed|cancell?ed|withdrawn)\b"
                         r"|\bI(?:'ve|’ve| have)\s+(?:closed|cancell?ed|withdrawn)\b"),
    "escalated": re.compile(r"(?i)\b(?:has been|have been|is now|was)\s+(?:escalated|forwarded|passed|assigned|routed)\b"
                            r"|\bI(?:'ve|’ve| have)\s+(?:escalated|forwarded|passed|assigned|routed)\b"),
    "done_for_them": re.compile(r"(?i)\b(?:I|we)(?:'ve|’ve| have)\s+(?:reset|unlocked|granted|created|enabled|installed|activated|reactivated|restored|unblocked|changed|added you)\b"
                                r"|\b(?:password|account|access|licen[cs]e|mailbox|profile|permission)s?\b[^.\n]{0,30}\b(?:has been|have been|is now)\s+"
                                r"(?:reset|unlocked|granted|created|enabled|activated|reactivated|restored|unblocked)\b"),
}
CLAIM_LABELS = {"closed": "Says the ticket was closed when it was not",
                "escalated": "Says the ticket was passed to a team when it was not",
                "done_for_them": "Says something was done (reset, granted, created) that Hermes cannot do"}

def _unsafe(pattern, text):
    """The first match of pattern in a sentence that is not negated, or None"""
    for match in pattern.finditer(text):
        sentence_start = max(text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
        if not NEGATION.search(text[sentence_start:match.start()] + match.group(0)):
            return match
    return None
# Replies become HTML in GLPI (and email), so markup that runs code must never get through
ACTIVE_MARKUP = re.compile(r"(?i)<\s*(?:script|iframe|object|embed|form|img|svg|style|link|meta)\b|javascript\s*:|\bon[a-z]+\s*=|data:text/html")
PROMPT_LEAK = re.compile(r"(?i)YOUR CORE ACTIONS|INTERNAL COMPANY KNOWLEDGE BASE|TICKET STATE:|RAG Context|draft_reply|has_confident_solution|suggested_new_category")

def _host(url):
    url = url if re.match(r"(?i)^https?://", url) else f"http://{url}"
    return (urlparse(url).hostname or "").lower()

def _digits(text):
    return re.sub(r"\D", "", text)

def check_reply(reply, source_text="", kb_text="", claims_allowed=()):
    """Reasons to withhold an AI reply, or [] when it is safe to send.
    source_text: what the person wrote (their own details may be repeated back). kb_text: the Knowledge Base given to the AI.
    claims_allowed: actions Hermes really takes alongside this reply ('closed', 'escalated'), which it may mention."""
    reply = reply or ""
    known = f"{source_text}\n{kb_text}".lower()
    known_digits = _digits(f"{source_text} {kb_text}")
    reasons = []
    if len(reply) > MAX_REPLY_LENGTH:
        reasons.append("The reply is unusually long")
    if PROMPT_LEAK.search(reply):
        reasons.append("The reply repeats Hermes' internal instructions")
    if ACTIVE_MARKUP.search(reply):
        reasons.append("The reply contains web page code")
    for url in URL_PATTERN.findall(reply):
        host = _host(url)
        if not host or "@" in url:
            continue
        allowed = any(host == d or host.endswith(f".{d}") for d in ALLOWED_LINK_DOMAINS) or url.lower().rstrip("/.") in kb_text.lower()
        if not allowed:
            reasons.append(f"Link to an unapproved site ({host})")
    for email in EMAIL_PATTERN.findall(reply):
        if email.lower() not in known and email.lower() not in ALLOWED_CONTACTS:
            reasons.append("An email address that was not in the request or the Knowledge Base")
            break
    for phone in PHONE_PATTERN.findall(reply):
        digits = _digits(phone)
        if digits not in known_digits and not any(digits == _digits(c) for c in ALLOWED_CONTACTS):
            reasons.append("A phone number that was not in the request or the Knowledge Base")
            break
    if _unsafe(CREDENTIAL_REQUEST, reply):
        reasons.append("Asks for a password or security code")
    for kind, pattern in ACTION_CLAIMS.items():
        if kind not in claims_allowed and _unsafe(pattern, reply):
            reasons.append(CLAIM_LABELS[kind])
    weaken = _unsafe(WEAKEN_SECURITY, reply)
    if weaken and weaken.group(0).lower() not in kb_text.lower():
        reasons.append("Advises weakening a security control")
    return list(dict.fromkeys(reasons))

def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def kb_titles(kb_text):
    return [line.split(":", 1)[1].strip() for line in (kb_text or "").splitlines() if line.startswith("ARTICLE:")]

def solution_is_grounded(analysis, kb_text):
    """A Knowledge Base fix must name a Knowledge Base article that exists. Otherwise it is not treated as a fix."""
    if not analysis.has_confident_solution:
        return True
    cited = _norm(getattr(analysis, "kb_article", ""))
    return bool(cited) and any(cited == _norm(title) or (len(cited) > 8 and cited in _norm(title)) for title in kb_titles(kb_text))

def review(analysis, source_text, kb_text, ticket_id=None, source="email", safe_reply="", claims_allowed=()):
    """Applies the checks to an AI analysis before Hermes acts on it, in place. Returns the reasons (empty when fine).
    A failed reply is replaced with safe_reply and never offered as a fix; an ungrounded fix becomes a normal reply."""
    reasons = []
    if not solution_is_grounded(analysis, kb_text):
        analysis.has_confident_solution = False
        reasons.append("Claimed a Knowledge Base fix without a matching article")
    reply_reasons = check_reply(analysis.draft_reply, source_text, kb_text, claims_allowed)
    if reply_reasons:
        analysis.has_confident_solution = False
        analysis.draft_reply = safe_reply
        reasons += reply_reasons
    if reasons:
        store.record_guard_event("reply_withheld" if reply_reasons else "fix_not_grounded", ticket_id, source, reasons)
        print(f"🛡️ Hermes safety checks on {source} ticket #{ticket_id or '-'}: {'; '.join(reasons)}")
    return reasons

def status():
    since = datetime.fromtimestamp(time.time() - 3600).isoformat(timespec="seconds")
    counts = store.count_agent_actions(since)
    return {
        "mode": "read_only" if READ_ONLY else "live",
        "paused": paused(),
        "limits": {"writes_per_hour": MAX_WRITES_PER_HOUR, "writes_per_ticket_per_hour": MAX_WRITES_PER_TICKET},
        "allowed_link_domains": ALLOWED_LINK_DOMAINS,
        "last_hour": {
            "changes": sum(c["n"] for c in counts if c["mode"] in ("live", "shadow")),
            "refused": sum(c["n"] for c in counts if c["mode"] == "blocked"),
        },
    }
