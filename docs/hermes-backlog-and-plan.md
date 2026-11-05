# Hermes: Backlog and Delivery Plan

This document collects everything Hermes still needs: management feedback from the October 2026 demo, the security and privacy gaps found in the proof of concept, and features already discussed. Each item says what it involves, what it depends on, and whether it belongs to the current proof-of-concept phase or to production.

Ground rule for every item: nothing is implemented unless it is secure practice for the organisation. Data privacy and security take priority over demo speed.

**Phases used in this document**

| Phase | Meaning |
|---|---|
| **Now** | Can be built in the proof of concept on the development machine, using test data only. |
| **Production** | Needs organisational infrastructure, contracts or approvals (hosting, data processing agreements, identity provider, security review) before it can go live. |

**Effort sizes:** S = up to 1 day, M = 2 to 5 days, L = 1 to 3 weeks. These are rough estimates for planning.

---

## 1. Management feedback (October 2026 demo)

### F1. A ticket for every chat, recorded as AI self-service

**Request.** Every portal conversation should create a GLPI ticket by default, registered as "AI self-service", and be closed once Hermes has offered a solution. The goal is data: who uses the service, what the frequent issues are, and how often Hermes resolves them.

**What changes from today.** Today a ticket is only created when the user asks for one, the fix fails, or the issue is urgent. Under F1, every conversation becomes a ticket, and the question shown to users changes from "log a ticket?" to "do you need a technician?".

**Approach**
- Add a request source called "AI Self-Service" in GLPI (Setup > Dropdowns > Request sources) and set it on every portal ticket, so these tickets can be reported on and filtered separately from email and Helpdesk tickets.
- Create the ticket once Hermes has understood the issue (after its first analysis, when the category is known), not on the first message. Greetings and empty conversations do not become tickets.
- Keep one ticket per conversation, with the chat transcript attached.
- When the user needs a technician, the same ticket is assigned to the group and least busy technician, as today. No second ticket is created.

**Resolution and the 24-hour rule**
- When Hermes offers a solution, it is recorded on the ticket and the ticket moves to Solved. The chat shows "Is your issue resolved?" with Yes and No buttons.
- **Yes:** the ticket is closed straight away.
- **No:** the ticket reopens and is escalated to a technician (re-triaged first, as with a refused solution today).
- **No answer within 24 hours** of the solution being offered: the ticket is closed automatically, with a note that it was closed for lack of response. Any reply from the user within the 24 hours resets the clock.
- **Which hours count:** the 24 hours are working hours as defined by the service desk's calendar in GLPI (Setup > Dropdowns > Calendars), the same calendar SLAs use. The service desk is intended to run 24/7, so today this means 24 consecutive hours. If the desk's hours ever change, only the GLPI calendar is updated and the timer follows it, with no change to Hermes.
- **Reopening:** within the 24 hours the user can reopen from the chat (the No button) or from GLPI (refusing the solution). Once a ticket is closed, GLPI does not allow it to be reopened; a further message in that conversation starts a new ticket, linked to the closed one so the history stays connected.

**How the 24-hour close is enforced.** GLPI has a built-in setting that closes solved tickets after a number of days, but it applies to every ticket in the entity, including those solved by technicians, and its scheduled check runs once a day, so tickets would close between 24 and 48 hours after the solution. Hermes should therefore close its own self-service tickets itself: on each polling cycle, it closes AI self-service tickets whose solution is more than 24 hours old with no reply since. Technician-solved tickets keep the organisation's existing rule.

**Dependencies.** Google Workspace single sign-on (S1). Without it, the requester on every ticket is an unverified email, which makes the data unreliable and lets anyone file tickets in someone else's name.

**Points to agree**
- Whether users should receive GLPI's "ticket closed" emails for self-service tickets. Probably not; this can be switched off for this request source once notifications are enabled.
- AI self-service tickets will raise ticket counts in existing reports. Reports should filter by request source.

**Effort:** M. **Phase:** Now (built and tested with test data once S1 is done); goes live in Production.

### F2. AI review of the category structure

**Request.** Tickets are often categorised wrongly. Hermes should look at the existing categories and the tickets coming in, and say whether categories need renaming, merging, or adding for tickets that fit nowhere.

**Feasibility.** Yes. This is a periodic analysis (for example monthly), not something that runs on every ticket.

**Approach**
- **Step 1, per ticket:** for a batch of recent tickets, Hermes reads the title, description and current category and records whether the category fits, which existing category fits better, and, if none fits, a short proposed label.
- **Step 2, in code:** count the results: how often each category is misused, which categories are never used, and which proposed labels recur.
- **Step 3, summary:** Hermes turns the counts into a report: categories to rename, merge or retire, new categories to create, and existing tickets to move to a better category, each with ticket counts and example ticket numbers.

**Approval, then Hermes applies the change.** Nobody has to recreate the recommendations by hand in GLPI, and nothing changes without a person's approval:
- The report appears on an admin page in the portal, one proposal per row, each with Approve and Reject.
- On approval, Hermes makes the change through the GLPI API: creating or renaming the category, or moving the listed tickets to the corrected category.
- Merging or retiring a category means moving its tickets first. These are applied as one approved batch per proposal, never piecemeal.
- Every applied change records who approved it, when, and exactly what changed, so it can be reviewed and reversed. GLPI's own ticket history also logs each category change.

**Security for the approval step**
- Only named service desk leads can approve, verified by single sign-on (S1), the same deny-by-default rule as the Analytics page.
- Category changes are made by a separate GLPI account whose only write permission is ticket categories (plus updating the category field on tickets). The everyday `ai.agent` account stays limited as in S3, so the triage service can never alter the category structure.
- Hermes can only apply proposals from an approved report. There is no free-form "edit categories" ability.

**Related quick win.** Hermes currently picks from a fixed list of categories in its code. It should read the category list from GLPI directly, so new or renamed categories are used immediately. Tickets Hermes files as "General / Unknown" should also record its proposed label, which feeds this analysis.

**Dependencies and privacy.**
- Running this on real ticket history sends large amounts of employee data to the AI provider, so it requires the data processing agreement in S2.
- Personal details (names, emails, phone numbers) should be masked before tickets are sent for analysis.
- Until S2 is in place, the tool is built and tested on test tickets only.

**Effort:** M for the report, plus M for the approval page and applying changes. **Phase:** Now for the tool, the approval flow and the dynamic category list (test data); Production for running it on real tickets.

### F3. Hermes as a service for the intranet and Google Chat

**Request.** Deploy Hermes so it can be built into the organisation's intranet (React front end, C# .NET back end), Google Chat and other channels.

**What it takes.** Today the conversation logic lives inside the Streamlit portal. It needs to become a standalone service with an API, which every channel calls:

| Piece | Description |
|---|---|
| Hermes API | The existing FastAPI service gains conversation endpoints: start a conversation, send a message, act on a choice ("that fixed it" or "I need a technician"), list my conversations, and later get my ticket's status. The Streamlit portal becomes one client of this API. |
| Intranet integration | **Decided: the employee chat lives in the SBC intranet** (React front end, .NET back end), which already signs staff in and knows their name, email, role and department. The .NET back end calls the Hermes API server to server, passing the signed-in employee's identity. The React app talks only to the .NET back end. No Hermes credentials ever reach the browser. Suggested placement: a chat launcher available on every intranet page, plus a full "IT Support" page in the sidebar. The Streamlit portal remains a prototype and test tool only |
| Google Chat app | A Chat app in the company's Google Workspace. Events arrive through Google Cloud Pub/Sub, so the server needs no inbound internet access. Each event carries the sender's verified Workspace identity. |
| Identity | Every request carries a verified user: the intranet passes the user it has already authenticated; Google Chat events are verified with Google's signed tokens. Hermes rejects anything else. |
| Hosting | An internal server near GLPI, served over HTTPS with a valid certificate behind a reverse proxy, never exposed to the public internet. |
| Storage | Conversation history moves from the local SQLite file to the organisation's database server (for example SQL Server), with encryption, access control and a retention period. |
| Operations | Secrets in a secrets store, logs that never contain message content or personal data, rate limiting, monitoring and alerting. |

**Dependencies.** S1 to S6 all apply. The intranet team needs to agree the authentication method between the .NET back end and Hermes.

**Effort:** L. **Phase:** Splitting the conversation logic out into the API can start Now; the integrations and deployment are Production.

### F4. Hermes admin portal (for selected employees)

**Purpose.** One place for service desk leads and administrators to review and act on what Hermes learns. It absorbs the approval page from F2 and the Analytics page. Access is limited to selected employees.

**Where it lives.** The intranet already has an **Admin Hub** with signed-in users and roles, so the admin portal should be built there on the Hermes API, like the chat. A Streamlit version can serve as an interim prototype.

| Section | What it does |
|---|---|
| New category suggestions | Similar suggestions grouped into one card (for example "Display problems: 14 tickets in 30 days, no category fits") with example tickets. Accept (creates the category in GLPI, optionally moving the tickets), edit the name or parent before accepting, map to an existing category instead, or decline with a reason. A declined theme is not proposed again. A new category is only proposed when a theme recurs (for example at least 5 tickets in 30 days) |
| Re-categorisation queue | Tickets Hermes believes are misfiled, with current category, suggested category and Hermes' reasoning. Single or multi-select accept or decline, with filters. Bulk changes show a preview ("this will move 23 tickets from A to B") before applying |
| Category health | Ticket counts per category, categories never used, categories often confused with each other |
| Audit log | Every action: who, when, what changed (before and after), which tickets, and the reason. Append-only; undo is itself a logged action. Export to CSV |
| Knowledge base gaps | Frequent issues with no article, and articles whose fixes users mark "not resolved", so the team knows which articles to write or fix |
| Technician availability | Mark technicians on leave so the load balancer skips them |
| Routing review | Tickets technicians reassigned after Hermes routed them, the clearest signal of routing mistakes |
| Operations health | Last poll time, AI provider errors, use of the backup model, tickets Hermes gave up on, with a Retry button |
| Settings | Escalation threshold, question rounds, the 24-hour window and group mappings, editable and audited. Secrets are never shown or editable |
| Analytics | The existing dashboard |
| Data protection | Chat history retention, and export or deletion of one person's conversations on request (rights under Nigeria's Data Protection Act) |

**Security.** Signed-in users only, with roles (for example Lead, Admin, Viewer) taken from a Google Workspace group or the intranet's own roles. Category changes use a separate GLPI account limited to categories and ticket categories. Chat transcripts are not browsable by default; if ever needed, only a named role may view them and every view is logged. Bulk actions are capped and previewed, and retiring or merging a category can require a second approver.

**Effort:** L. **Phase:** the data it needs (suggestions and corrections stored in Hermes' own data store) is collected Now; the portal itself follows the Hermes API (F3).

### F5. Service requests (starting with new employee onboarding)

**Request.** Not everything is an incident. Requests such as HR asking for a laptop, ID card and accounts for a new employee need a different flow.

| | Incident ("my VPN won't connect") | Service request ("new hire needs a laptop and ID card") |
|---|---|---|
| Hermes' goal | Fix it, ideally from the knowledge base | Collect complete details, then route for fulfilment |
| Knowledge base fix | Yes | No; "Is your issue resolved?" does not apply |
| Chat flow | Troubleshoot, then escalate | Ask for what is needed (name, start date, department, items), then log |
| Approval | No | Often a manager or HR approves first (GLPI validations) |
| Fulfilment | One technician | Often several teams; some items (for example ID cards) may not be IT |

**Approach.** Hermes classifies each ticket as Incident or Request and sets GLPI's ticket Type, which it does not do today. GLPI categories are marked as valid for incidents, requests or both. Requests skip knowledge base troubleshooting. Recurring requests such as onboarding become a checklist of tasks across teams, with an approval step where the business wants one.

**Dependencies.** The business must first agree the service request catalogue: which requests exist, who approves each, and who fulfils each part.

**Effort:** M to L, depending on the catalogue. **Phase:** Now for Incident/Request classification; the fulfilment workflows once the catalogue is agreed.

### F6. No-action emails (announcements, auto-replies)

**Request.** Organisation-wide emails, for example from `people.announcement@sevenup.org`, arrive in the support inbox and become tickets. They should be categorised as Announcements and closed immediately.

**Approach**
- **Known senders: a fixed rule, not the AI.** A GLPI business rule on the mail receiver sets the category to Announcements when the sender matches, and the ticket is closed straight away with a "No action required" solution type. If the business does not need them on record at all, a Gmail filter or a GLPI mail import rule keeps them out entirely.
- **Unknown senders:** Hermes flags the long tail (out-of-office replies, newsletters, delivery notices) as no action needed, and they are closed with the same label.

**Why a rule for known senders.** It gives the same answer every time, costs nothing per email, and company-wide announcements are never sent to an outside AI provider.

**Dependencies.** An Announcements category; the solution-type labels; the New to Closed transition in the Hermes profile's Life Cycles tab.

**Effort:** S. **Phase:** Now.

### More feedback

To be added as remembered.

---

## 2. Security and privacy items

| # | Item | Risk today | Fix | Phase |
|---|---|---|---|---|
| S1 | Demo sign-in and the `?session=` token in the page address | Anyone can claim any email; the token ends up in browser history, screenshots and shared links | Configure Google Workspace single sign-on (OAuth client, "Internal" users only), then remove the demo sign-in and the address token together | Now |
| S2 | AI providers on free tiers | Ticket text containing personal data is sent to Groq and Gemini without a data processing agreement; Google's free-tier terms allow it to use submitted content to improve its products | Paid plans with a data processing agreement and no-training terms (provider comparison in [ai-model-evaluation.md](ai-model-evaluation.md)). Until then: test data only, or disable the Gemini fallback | Production (blocker for real data) |
| S3 | `ai.agent` uses GLPI's Super-Admin profile | A leaked API token would give full control of GLPI | A dedicated profile limited to what Hermes uses: tickets (read, update, assign, create), follow-ups, solutions, and read access to the knowledge base, categories, groups and users | Now |
| S4 | Mail receiver does not verify Google's certificate (`novalidate-cert`) | The connection to Gmail is encrypted but not verified, so it could be intercepted | Remove `novalidate-cert` from the mail receiver and make sure PHP has a certificate bundle; test the mail fetch afterwards | Now |
| S5 | Chat history in an unencrypted local SQLite file | Acceptable on a development machine only | Database server with encryption at rest, access control and a retention policy | Production |
| S6 | Manual-trigger endpoint `/glpi-webhook` has no authentication | Now limited to the local machine, so not reachable from the network | Require authentication before it is ever exposed beyond the local machine | Production |
| S7 | GLPI and the portal served over plain HTTP | Acceptable only on the local machine | HTTPS with valid certificates everywhere | Production |
| S8 | Secrets in a local `.env` file | Acceptable on a development machine if never committed | Secrets store in production; `.env` excluded from version control | Production |
| S9 | Approved category changes use Hermes' own GLPI account (`GLPI_CATEGORY_USER_TOKEN` empty), so "Hermes API" has the Create right on ITIL categories | The main Hermes account can create categories, which it never needs for ticket handling | Create the separate category account (rights: ITIL categories read and create, tickets read and update category, follow-ups create), set `GLPI_CATEGORY_USER_TOKEN`, and remove the ITIL categories Create right from "Hermes API" | Production (agreed proof-of-concept setting, 2026-10-20) |

Already fixed: the AI daemon and the portal listen on the local machine only, and the Analytics page requires single sign-on plus an approved admin email (deny by default).

---

## 3. Other features already discussed

| # | Feature | Notes | Depends on | Effort | Phase |
|---|---|---|---|---|---|
| B1 | Ask about a ticket's status in the chat | Only the user's own tickets; only public information (status, assignee, public replies, solution). Private technician notes, including Hermes' routing notes, are never passed to the AI | S1 | S | Now |
| B2 | Attachments (email, Helpdesk form, chat) | Screenshots read by a vision-capable model, PDFs and logs as text; filter out email signature images; file type and size limits; users told files are processed by AI | S2 | M | Production |
| B3 | Email notifications to requesters and technicians | GLPI's built-in notifications; needs SMTP | SMTP account | S | Production |
| B4 | Event-driven ticket intake instead of polling | GLPI 11 native webhooks or a plugin; replaces the polling loop | GLPI upgrade decision | M | Production |
| B5 | Knowledge base search at scale | Retrieve only the relevant articles per ticket (vector search) instead of sending the whole knowledge base | Knowledge base growth | M | Production |

---

## 4. Proposed plan

### Now (proof of concept, test data only)

**Done**
- **S3:** restricted "Hermes API" GLPI profile, in use and verified.
- **F2 quick win:** categories read live from GLPI, with broad parent categories as a fallback and suggestions stored in Hermes' data store.
- **F1:** a ticket for every chat (AI Self-Service, Pending, "Is your issue resolved?", 24-hour auto-close).
- **F3, first step:** the Hermes API and conversation engine, and the intranet integration (chat launcher, IT Support page, My tickets) on branch `feat/hermes-integration`; see [intranet-integration.md](intranet-integration.md).
- **F5, first step:** Incident or Request classification, setting GLPI's ticket Type.
- **Ticket notifications** on the intranet bell, and **solution confirmation** (accept or refuse from the intranet, with a reminder).
- **F4, first part:** the IT Service Desk console, a separate section of the intranet at `/service-desk` (Admin and SuperAdmin roles until a Service Desk Admin role exists). Its pages:
  - Overview: the analysis.
  - Category suggestions: grouped proposals, approve with an editable name or decline with a reason, multi-select.
  - Routing suggestions: Group in charge rules, tickets with the wrong team, tickets without a technician, technician changes.
  - Audit log.

  Approving creates the category in GLPI and moves its tickets, for now with Hermes' own account (S9). See [intranet-integration.md](intranet-integration.md).
- **Technician availability** in the console: mark technicians on leave so Hermes stops assigning them tickets. Their open tickets show up in Routing suggestions.
- **Data protection** in the console:
  - Chats are deleted automatically 12 months after their last message.
  - One person's data can be exported or deleted on request, with a reference, audited. GLPI tickets are kept.
  - Deleted rows are overwritten.
- **Ready for 1000+ employees: complete GLPI reads.** GLPI returns at most 1,000 items per request and silently cuts larger requests, so Hermes now reads in pages (`glpi_list_all`, `glpi_search_all` in `app.py`) and lets GLPI's search do the filtering:
  - The Overview reads only the chosen period.
  - Routing suggestions search for "open tickets, or opened in the last 90 days".
  - Auto-close, solution reminders and refused-solution handling find every matching ticket, not just the newest 100 to 200.
  - Independent reads run side by side, which brought the console from about 12–21 seconds to 5–6 seconds on the local GLPI. Each GLPI call has a fixed overhead of a second or more there.

**Waiting on GLPI settings (owner: service desk admin)**
- Life Cycles on the "Hermes API" profile (guide Part B, Step 2). Without them, solving, closing and auto-closing are refused.
- S4: certificate verification on the mail receiver (guide Part A).
- SLAs (guide Part D), and the solution-type labels.

**Can be built now, in suggested order**
1. **F5, next step:** request-specific chat flow. For requests, Hermes collects the details and then asks "Shall I submit this request?" instead of troubleshooting or offering a technician.
2. **B1:** ask about a ticket's status in the chat ("What's happening with my laptop request?"). Single sign-on is no longer a blocker, because the intranet supplies the verified identity.
3. **F6:** no-action emails (announcements, out-of-office replies): the GLPI rule for known senders, and Hermes closing the rest as "No action required".
4. **Solution-type labels** set by Hermes on every close (resolved by Hermes, auto-closed, abandoned, no action required), once the labels exist in GLPI.
5. **Retire the Streamlit portal and its demo sign-in** (the old S1). The intranet now provides verified sign-in, so the remaining demo sign-in is an unneeded insecure path.
6. **F4, next parts:**
   - Re-categorisation suggestions (tickets filed in the wrong category) in the same review queue.
   - The Service Desk Admin role in the intranet.
7. **Small intranet items:** clicking a notification opens the ticket (needs `Header.jsx`, after the stashed changes are resolved), and showing the SLA "Resolve by" deadline in My tickets once SLAs exist.

### Production (needs organisational approval and infrastructure)

1. **S2: AI provider contracts** with data processing agreements. This blocks any use of real employee data.
2. **Hosting:** internal server, HTTPS (S7), secrets store (S8), database server for history (S5), authenticated API (S6), monitoring.
3. **F3:** intranet chat through the .NET back end, then the Google Chat app. **F4:** the admin portal in the intranet's Admin Hub.
4. **F5:** service request workflows, once the request catalogue is agreed.
5. **F2:** category review on real ticket history, with personal details masked.
6. **B3:** email notifications; **B2:** attachments; **B4:** event-driven intake; **B5:** knowledge base search at scale.
7. **Revert demo-only settings:** GLPI's mail check (`mailgate`) is set to every 10 seconds and the polling intervals in `app.py` and `mail_poll.py` are short for demos; production uses GLPI's scheduled task and normal intervals.

---

## 5. Open questions

- Which identity provider setup will production use for the intranet: Google Workspace sign-in, or the intranet's existing login passed through the .NET back end?
- Which database server should hold conversation history in production?
- Who signs off the AI provider contracts, and is there an approved provider list?
- How long should chat transcripts and self-service tickets be kept?
- Who approves category changes recommended by the F2 review?
- The 24-hour auto-close follows the service desk calendar, which is 24/7. Is a 24/7 calendar already set up in GLPI, or does one need creating?
