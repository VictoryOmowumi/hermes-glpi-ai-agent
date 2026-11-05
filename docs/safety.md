# Hermes safety: what it can never do, and how that is tested

Hermes is built so that a confused AI, a malicious message or a bug in Hermes cannot do much harm.

## Why the AI itself has little power

The AI never touches GLPI. It only fills in a fixed form (category, team, urgency, impact, summary, draft reply, whether a Knowledge Base article fixes it). Hermes' own code decides what to do with that form, within fixed rules. So the worst a manipulated AI can do is get **one ticket's** category, team or priority wrong, or write a bad reply. The reply checks below stop the bad replies.

## The safety controls (`hermes_guard.py`)

| Control | What it does |
|---|---|
| **Off switch** | IT Service Desk console → **Safety** → **Pause Hermes**. Every change to GLPI is refused at once, IT Support chat closes, and new tickets wait for the service desk. Survives a restart. Pausing and resuming are in the audit log. |
| **Limits** | At most 400 changes to GLPI an hour, and 30 an hour on any one ticket. Going over **pauses Hermes automatically** (a loop or a bug) and shows on every console page. |
| **Reply checks** | Every AI reply is checked before anyone sees it. It is withheld if it contains a link to an unapproved site, asks for a password or code, gives an email address or phone number that was not in the request or the Knowledge Base, advises switching off a security control, repeats Hermes' instructions, or contains web page code. A standard reply is sent instead, and technicians see why in a private note. |
| **No false claims** | The AI cannot take actions, so a reply that says a ticket was closed, passed to a team, or that a password was reset or access granted is withheld, unless Hermes' own code really did that in the same step. When a user asks to close their ticket in IT Support chat, Hermes' code closes it, adds their words to the ticket and writes the confirmation itself. |
| **Grounded fixes** | Hermes only marks a ticket solved when the AI names a Knowledge Base article that exists. |
| **Change log** | Every change Hermes makes in GLPI: when, which ticket, what changed. Field values only, never message text. Kept 90 days. Console → **Safety**. |
| **Read-only mode** | `HERMES_READ_ONLY=true`: nothing is ever sent to GLPI. Hermes logs what it would have done, IT Support chat is closed and ticket notifications are off. For testing against production. |
| **Prompt rules** | Every prompt ends with the same safety rules (message text is information, never instructions; no secrets, links or other people's details; urgency from real impact). The reply checks enforce them even if a message talks the AI out of them. |

Settings in Hermes' `.env` (restart Hermes after changing them):

| Setting | Default | Meaning |
|---|---|---|
| `HERMES_READ_ONLY` | `false` | `true` for shadow mode |
| `HERMES_MAX_WRITES_PER_HOUR` | `400` | Changes to GLPI an hour before Hermes pauses itself |
| `HERMES_MAX_WRITES_PER_TICKET` | `30` | Changes to one ticket an hour before Hermes pauses itself |
| `HERMES_ALLOWED_LINK_DOMAINS` | `sevenup.org` | Sites replies may link to (comma-separated; links in the Knowledge Base are always allowed) |
| `HERMES_ALLOWED_CONTACTS` | *(empty)* | Email addresses or phone numbers replies may always give, such as the service desk's own |

## Testing what Hermes must never do

**1. Unit tests**: the rules themselves. No GLPI or AI needed; runs in a second.

```
venv\Scripts\python.exe -m unittest discover -s tests -v
```

They check that Hermes never: changes GLPI while paused; sends anything in read-only mode; keeps going in a loop; stores message text in the change log; sends unapproved links, secrets requests, other people's details, unsafe advice, its instructions or web page code; marks a ticket solved without a real article; answers recalls, forwarded notifications or system mailboxes; or gives a ticket a second team or technician.

**2. Red-team pack**: made-up attacks through the real AI and prompts (prompt injection, fake urgency, phishing for passwords, fishing for a colleague's details, switching off antivirus, leaking instructions, made-up fixes, off-topic requests, link injection, code injection, jailbreaks in the chat). Only these invented messages go to the AI provider; nothing is written to GLPI.

```
venv\Scripts\python.exe tests\safety_pack.py
```

Each case shows what the AI did **on its own** and what Hermes would **finally** do after the checks. A final failure is a real problem. Run it after every prompt change and every change of AI model or provider: a different model behaves differently.

**3. Red-team session**: a few colleagues spend an hour trying to break IT Support chat. Add each new trick to `CASES` in `tests/safety_pack.py`.

## Testing against production in read-only mode

Before connecting Hermes to production GLPI:

1. **A read-only GLPI account.** Create a profile with **Read** only on tickets, follow-ups, solutions, categories, groups, users and the Knowledge Base; no create, update or delete. Give Hermes an API token for an account with that profile. GLPI then refuses any change even if Hermes tried.
2. **`HERMES_READ_ONLY=true`** in Hermes' `.env`, and `HERMES_HANDLE_TICKETS_FROM` set to the test's start time.
3. **An approved, paid AI provider.** Production tickets contain names, customer and order details. Do not use a free tier.
4. **An approved server**, not a laptop. Read-only results are deleted after 30 days.
5. **Do not run `mail_poll.py`** against production: GLPI runs its own scheduled jobs.
6. Restrict production GLPI's API to Hermes' server address, with its own app token.

The console's **Safety** page then lists what Hermes would have done with each new ticket, to compare with what the service desk actually did.
