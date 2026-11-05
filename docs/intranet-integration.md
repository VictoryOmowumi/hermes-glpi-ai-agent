# Hermes in the SBC Intranet: Integration Guide

How the Hermes IT support assistant is connected to the SBC intranet, how to run it locally, and the API the intranet uses. Proof of concept: Hermes runs against a local GLPI.

## How the pieces fit

```
Browser (intranet React app)
   │  /api/hermes/...  with the employee's existing intranet sign-in token
   ▼
Intranet back end (.NET, CMS_Web_API)  ── HermesController + HermesClient
   │  /v1/...  with X-Hermes-Key + the signed-in employee's email and name
   ▼
Hermes (Python, server.py) on 127.0.0.1:8000  ── conversation engine, AI, GLPI
   ▼
GLPI (tickets, knowledge base)
```

- **The browser never talks to Hermes.** It calls the intranet back end with its normal sign-in token, exactly like every other intranet feature.
- **The employee's identity comes from the sign-in token** (`Email` and name claims), never from the request body, so nobody can act as someone else.
- **Only the intranet back end holds the Hermes service key.** It is stored in .NET user secrets locally (a secret store in production), never in `appsettings.json` or the React app.
- **Hermes listens on the local machine only** and refuses any request without the correct key.
- **Only the Hermes routes listed below** can be reached through the intranet back end. The admin routes also require the Admin or SuperAdmin role (see "IT service desk admin section").
- **Employees only see their own data:** another employee's conversation or ticket returns "not found". Ticket details contain public information only; private technician notes, including Hermes' routing notes, are never returned.

## What was added

**Intranet front end** (repository `CMS`, branch `feat/hermes-integration`):

| File | Purpose |
|---|---|
| `src/modules/hermes/lib/hermesApi.js` | Calls to `api/hermes/...` through the existing `api` client (so the sign-in token is attached automatically) |
| `src/modules/hermes/components/HermesChat.jsx` | The chat: messages, formatted replies, ticket notices, choice buttons, typing indicator |
| `src/modules/hermes/components/HermesLauncher.jsx` | Floating Hermes button on every intranet page (hidden on the IT Support page) |
| `src/modules/hermes/components/MyTickets.jsx` | The employee's tickets with live status, and a ticket details dialog |
| `src/modules/hermes/pages/ITSupport.jsx` | IT Support page: conversations, chat, My tickets |
| `src/modules/service-desk/` | IT Service Desk console: layout, pages (Overview, Category suggestions, Routing suggestions, Audit log), and its API calls and queries |
| `src/AppRoutes.jsx` | New routes `/it-support` and `/service-desk/*` (`/cms/service-desk` redirects) |
| `src/components/Sidebar.jsx` | Menu items can be marked `exact`, so a section's home page is not highlighted on its sub-pages |
| `src/modules/main-intranet/components/layout/Layout.jsx` | Adds the launcher to the main intranet layout |
| `src/config/navigation.jsx` | "IT Support" under Knowledge Base, "IT Service Desk" (admin roles only) next to CMS, and the console's own menu `SERVICE_DESK_NAV` |
| `package.json` | Adds `react-markdown` (renders replies without allowing raw HTML) |

**Intranet back end** (repository `CMS_Web_API` at `CMS\CMS_Web_API\CMS_Web_API`, branch `feat/hermes-integration`, created from `feat/intranet-social-remodle`):

| File | Purpose |
|---|---|
| `Clients/HermesClient.cs` | Calls Hermes with the service key and the employee's identity; 2-minute timeout |
| `Controllers/HermesController.cs` | `api/hermes/...` routes, requires sign-in, takes the employee from the token |
| `Services/HermesNotificationWorker.cs` | Background worker that turns Hermes ticket updates into bell notifications |
| `Extensions/DependencyInjection.cs` | Registers `HermesClient` and the notification worker |
| `CMS.csproj` | `UserSecretsId`, so the Hermes key can live in .NET user secrets |

**Hermes** (this project): `hermes_engine.py` (conversation rules), `hermes_api.py` (the API), `hermes_admin_api.py` (the admin API), `hermes_store.py` (chat history, category suggestions and the admin audit log), `server.py` (starts everything).

## Running it locally

1. **Hermes:** in this project, `python server.py` (or `python app.py`). Runs the ticket poller and the Hermes API on `http://127.0.0.1:8000`.
2. **Intranet back end:** in `CMS\CMS_Web_API\CMS_Web_API` on branch `feat/hermes-integration`, run `dotnet run --launch-profile http` (it listens on `http://localhost:5057`). It must run in the Development environment for user secrets to load.
3. **Intranet front end:** `npm run dev` in `CMS` on branch `feat/hermes-integration` (`http://localhost:5174`).
4. Sign in to the intranet as usual. Use the Hermes button at the bottom right, or **IT Support** in the sidebar.

**Configuration**

| Where | Setting | Value |
|---|---|---|
| Hermes `.env` | `HERMES_API_KEY` | Random 64-character key (generated) |
| Hermes `.env` | `HERMES_SELF_SERVICE_SOURCE_ID` | ID of GLPI's "AI Self-Service" request source |
| Hermes `.env` (optional) | `HERMES_CHAT_RETENTION_DAYS` | Days a conversation is kept after its last message. Default 365 |
| Hermes `.env` | `GLPI_CATEGORY_USER_TOKEN` | API token of the separate category account. Empty = Hermes' own account (proof of concept only) |
| Back end user secrets | `Hermes:ApiKey` | Same key as `HERMES_API_KEY` (`dotnet user-secrets set "Hermes:ApiKey" <key>`) |
| Back end configuration (optional) | `Hermes:BaseUrl` | Defaults to `http://127.0.0.1:8000/v1` |

GLPI must also have the "Hermes API" profile's **Life Cycles** set (guide Part B), otherwise Hermes cannot mark tickets solved or closed.

## Ticket update notifications

Employees get a notification on the intranet's bell when a **person** changes one of their tickets: a technician is assigned, the status changes, or a technician posts a public reply or a solution. Changes Hermes makes during a chat are not notified, because the employee already sees them in the chat.

How it works:
1. **Hermes tracks changes:** a background loop in Hermes (every 60 seconds) compares recently updated tickets with their last known state and records an update for each requester. The first scan only records a baseline, so switching it on never floods anyone with old changes.
2. **The intranet collects them:** `HermesNotificationWorker` in the back end (every 30 seconds) fetches pending updates from Hermes (`GET /v1/events`, service key only), creates each one through the intranet's own `INotificationService` (type `it_ticket_update`, which also pushes it live over SignalR), then confirms delivery (`POST /v1/events/ack`) so nothing is sent twice.
3. **Trust stays one-way:** Hermes never calls the intranet or holds intranet credentials. The two event routes are not exposed through `HermesController`, so browsers cannot reach them.

Notification text uses public information only: the ticket title, status, technician names and the start of a public reply. Private notes are never included.

Each notification carries the link `/it-support?ticket=<id>`. The bell in `Header.jsx` currently marks a notification read when clicked but does not follow its link; that would be a small change to `Header.jsx`.

## Confirming solutions

When a ticket is marked **Solved**, the employee is asked to confirm it:
- **Notification:** "a solution was posted. Please confirm whether it fixed your issue in IT Support", plus one reminder if they have not answered after 4 hours (`SOLUTION_REMINDER_HOURS` in `app.py`).
- **IT Support page:** a banner ("A solution is waiting for your confirmation") with a button that opens the ticket. In My tickets, the ticket carries an "Action needed · confirm solution" marker.
- **Ticket details:** "Yes, it's fixed — close the ticket" closes it. "No, it's not fixed" asks what is still wrong, records it on the ticket and sends it back to IT (status In progress). If Hermes wrote the refused solution and no technician is assigned, Hermes re-triages and assigns one.
- This works whether or not the technician wrote a solution record; a ticket only needs to be in Solved status.
- Only the ticket's requester can confirm or refuse, and only while it is Solved.
- It needs the Solved → Closed and Solved → Processing (assigned) transitions in the "Hermes API" profile's Life Cycles (guide Part B).

## IT Service Desk console

A section of the intranet of its own, at `/service-desk`, like Admin Hub and Jinja: outside the CMS, with its own sidebar. People with access reach it from **IT Service Desk** in the intranet sidebar; the old `/cms/service-desk` address redirects there. The code is in `src/modules/service-desk/` (pages, components, and its own API calls and queries in `lib/`). Its pages:
- **Overview:** how Hermes handled tickets over a chosen period (7, 30 or 90 days, or all time), read live from GLPI and cached for a minute. It shows:
  - Headline numbers: Hermes' resolution rate, tickets resolved by Hermes, escalated to a technician, auto-closed with no reply, chats solved without a ticket, and pending category suggestions.
  - Charts of outcomes by day and by category, and escalations by technician.
  - The tickets behind the numbers, filterable by outcome.
- **Category suggestions:** names Hermes suggested for tickets that fitted no existing category. Suggestions with the same name (ignoring case and spacing) are grouped into one proposal.
  - Each proposal shows its tickets and where they are filed now, and is flagged when a GLPI category with that name already exists.
  - **Placement:** each proposal is placed under the broad category its tickets were filed in (for example "Infrastructure Services > Hardware Upgrade Request"). Proposals from "General / Unknown" tickets become top-level categories.
  - The approve dialog shows where the category will be created. A parent that does not exist in GLPI is refused, both on screen and by Hermes, and the parent is stored with GLPI's exact spelling. The approved full name is what Hermes will create later.
  - Proposals can be approved (the name and parent can be edited first) or declined (a reason is required), one at a time or several at once.
  - A proposal can only be decided once.
- **Audit log:** every decision, with who made it, when, the category, the number of tickets and the note. Entries are never edited or removed.

**Outcome rules** (applied to each ticket):

| Outcome | Rule |
|---|---|
| Not handled by Hermes | Hermes never acted on the ticket (no reply, note or solution) and it did not come from IT Support chat |
| Auto-closed, no reply | Closed by the 24-hour auto-close |
| Escalated to a technician | A technician is assigned, or the ticket is In progress or Planned. This includes tickets where a Hermes fix was refused. |
| Resolved by Hermes | Solved or Closed with no technician involved |
| With Hermes | Still in an IT Support conversation |

**Approving applies the change in GLPI straight away:**
- **The category:** Hermes creates it under the chosen parent, with the parent's entity and visibility, available for incidents and requests. If a category with that exact full name already exists, Hermes uses it instead of creating a duplicate.
- **The tickets:** they are moved into the new category, but only while they are still in the category Hermes filed them under. A ticket a technician has recategorised since is left alone and reported as skipped.
- **A private note on each moved ticket** says who approved the change.
- **Hermes picks up the new category immediately** for incoming tickets.
- **When the decision is recorded:** each proposal is recorded as approved only after GLPI has accepted it. If GLPI refuses (usually a missing right), the proposal stays pending and the screen says why.
- **What is shown:** a message per approved category names what was created and which tickets moved. The audit log keeps the GLPI category ID and the moved and skipped tickets.

**Routing suggestions** (page `/service-desk/routing`, `hermes_routing.py`) are worked out from GLPI each time the tab opens, with no AI and no ticket text sent anywhere:

| Kind | When it is suggested | Approving |
|---|---|---|
| Routing rule | Technicians moved 2 or more tickets in a category from the group Hermes chose (read from Hermes' routing notes) to the same other group | Sets that group as the category's Group in charge (category account) |
| Ticket with the wrong team | An open ticket is not with its category's Group in charge (inherited from the parent category) | Moves the ticket to that group and to the category's technician in charge or the least busy active member, with a private note |
| Ticket without a technician | An open ticket is in a group's queue with nobody assigned | Assigns the category's technician in charge or the least busy active member of the group, with a private note |
| Technician change | An open ticket's technician is inactive, or not a member of the ticket's group | Replaces the technician with the least busy active member of the group, with a private note |

Before applying, Hermes checks that the ticket or category has not changed since. Decided suggestions are stored in Hermes' `routing_decisions` table and are never proposed again; each decision is in the audit log.

**Technicians** (page `/service-desk/technicians`):
- **What it shows:** everyone in a team that can be assigned tickets, with their teams, open tickets and status (available, on leave, or inactive in GLPI).
- **Leave:** GLPI has no leave field, so leave periods are kept in Hermes (`technician_absences`). Adding and cancelling leave are in the audit log.
- **While someone is on leave**, Hermes skips them when it assigns technicians to new tickets, escalations and re-triage, even when they are a category's technician in charge. If nobody in the team is available, the ticket waits in the team queue. The leave dialog warns about this beforehand.
- **Their open tickets** appear in Routing suggestions ("Technician changes") wherever someone else in the team is available.

**Data protection** (page `/service-desk/data-protection`), agreed 2026-10-20:
- **Retention:** Hermes deletes a conversation 12 months after its last message, and delivered notifications as old, once a day (`enforce_chat_retention` in `server.py`; period `HERMES_CHAT_RETENTION_DAYS`). Each clean-up that deletes something is in the audit log.
- **One person's request** (Nigeria Data Protection Act). An admin enters the person's exact email and sees counts and dates only. Transcripts cannot be browsed.
  - **Export** downloads everything Hermes holds about them as a JSON file to send to the person.
  - **Delete** permanently removes their chats and Hermes notifications.
  - Both need a request reference and are recorded in the audit log.
  - Admins cannot delete their own data; another admin has to handle that request.
- **What is kept:** IT tickets in GLPI (including the chat transcript copied onto a ticket) are business records and are not exported or deleted. The intranet's own notification bell rows (`cms.NotificationDeliveries`) are also outside Hermes and are not covered.
- **Deleted means gone:** Hermes' database overwrites deleted rows (`secure_delete`), so deleted chats cannot be recovered from the file.

**Which GLPI account makes the change:**
- Set `GLPI_CATEGORY_USER_TOKEN` in Hermes' `.env` to the separate category account.
- **Proof of concept:** while it is empty, Hermes uses its own account. The "Hermes API" profile then needs the **Create** right on **ITIL categories** (Administration > Profiles > Hermes API > Setup tab), and Hermes prints a reminder on start.
- **Before going live:** create the separate account, set its token, and remove the ITIL categories Create right from "Hermes API" again (backlog S9).

Re-categorisation suggestions (tickets filed in the wrong category) come next.

**Access:**
- Only the Admin and SuperAdmin roles can open the console.
- The `/service-desk` route and its sidebar item check the role.
- The back end checks it again on every admin call (`[Authorize(Roles = "Admin,SuperAdmin")]` in `HermesController`), so the routes cannot be called directly by other employees.
- When the planned Service Desk Admin role exists, add it to:
  - `ServiceDeskAdminRoles` in `HermesController.cs`
  - the role map in `Program.cs`
  - the `/service-desk` route in `AppRoutes.jsx`
  - the IT Service Desk item in `INTRANET_NAV` in `navigation.jsx`
- Hermes itself relies on the intranet back end for the role check, the same one-way trust as the rest of the integration, and records the signed-in admin on every decision.

## API reference (as called by the intranet front end)

All routes are under the intranet back end's `api/hermes/` and need the normal intranet sign-in.

| Method and route | Body | Returns |
|---|---|---|
| `GET conversations` | | The employee's conversations: `id`, `title`, `ticket_id`, `outcome`, `updated_at` |
| `POST conversations` | | A new, empty conversation |
| `GET conversations/{id}` | | The conversation (shape below) |
| `POST conversations/{id}/messages` | `{ "text": "..." }` (up to 4,000 characters) | The updated conversation, including Hermes' reply |
| `POST conversations/{id}/actions` | `{ "action": "resolved" }` | The updated conversation. `409` if that choice is not available right now |
| `GET tickets` | | The employee's tickets: `id`, `title`, `status`, `opened`, `updated`, `category`, `team`, `technician` |
| `GET tickets/{id}` | | Public ticket details: status, category, teams, technicians, dates, public updates, latest solution, `awaiting_confirmation` |
| `POST tickets/{id}/solution` | `{ "decision": "accept" }` or `{ "decision": "reject", "comment": "..." }` | Accept closes the ticket; reject (comment required) sends it back. `409` if the ticket is not Solved |
| `GET admin/analytics?days=30` (admin) | | `totals`, `daily`, `by_category`, `by_technician` and up to 300 `tickets` with their outcome. `days=0` is all time |
| `GET admin/category-suggestions?status=pending` (admin) | | `{ "proposals": [...], "categories": [...] }`. Each proposal has `key`, `label`, `proposed_name` (full name with parent), `ticket_count`, `filed_under`, `already_exists`, `tickets`, and the decision fields. `categories` is GLPI's current list (`null` if GLPI is down). Status `pending`, `approved` or `declined` |
| `POST admin/category-suggestions/decisions` (admin) | `{ "decision": "approve", "keys": ["vpn access"], "note": "", "labels": { "vpn access": "Network > VPN access" } }` | `{ "decided": 1, "results": [{ "name", "category_id", "category_created", "moved": [23], "skipped": [{ "ticket_id", "reason" }] }] }`. Approving creates the category and moves the tickets in GLPI. Decline needs a `note`; up to 50 proposals per call; `409` if all were already decided; `502` if GLPI refused (earlier proposals in the call stay approved) |
| `GET admin/routing-suggestions?status=pending` (admin) | | Suggestions with `id`, `kind` (`group_in_charge`, `regroup`, `reassign`), `title`, `reason` and the from and to team and technician; decided ones also have `note`, `decided_by`, `decided_at` |
| `POST admin/routing-suggestions/decisions` (admin) | `{ "decision": "approve", "ids": ["regroup:18:2"], "note": "" }` | `{ "decided": 1, "results": [...] }`. Approving applies each change in GLPI; decline needs a `note`; `409` if a ticket changed since or all were already decided |
| `GET admin/technicians` (admin) | | `technicians` (`id`, `name`, `teams`, `active`, `open_tickets`, `on_leave_until`) and `absences` (current, upcoming and the last 30 days) |
| `POST admin/technicians/{userId}/leave` (admin) | `{ "startsOn": "2026-10-21", "endsOn": "2026-10-25", "note": "" }` | `{ "id": n }`. Refused if the dates are invalid, already past, or the person is not in a support team |
| `DELETE admin/technicians/leave/{id}` (admin) | | Cancels the leave. Audited |
| `GET admin/data-protection` (admin) | | `retention_days`, `conversations`, `oldest_activity`, `last_purge` |
| `POST admin/data-protection/lookup` (admin) | `{ "email": "..." }` | Counts and dates for that person only, never content |
| `POST admin/data-protection/export` (admin) | `{ "email": "...", "reference": "DPA-2026-014" }` | Everything Hermes holds about the person (conversations with messages, notifications). Audited |
| `POST admin/data-protection/delete` (admin) | `{ "email": "...", "reference": "..." }` | `{ "conversations": n, "notifications": n }` deleted. GLPI tickets kept. Audited; refused for the admin's own email |
| `GET admin/audit-log` (admin) | | Decisions, newest first: `actor_email`, `actor_name`, `action`, `target`, `details`, `created_at` |

Routes marked (admin) return `403` for anyone without the Admin or SuperAdmin role.

**Conversation shape**

```json
{
  "id": 7,
  "messages": [
    { "role": "user", "content": "My VPN won't connect", "notice": null, "time": "2026-10-15T15:55:10" },
    { "role": "assistant", "content": "Markdown reply...", "notice": "Recorded as self-service request #22.", "time": "..." }
  ],
  "ticket": { "id": 22, "escalated": false },
  "choice": {
    "prompt": "Is your issue resolved?",
    "actions": [
      { "id": "resolved", "label": "Yes, it's resolved", "style": "success" },
      { "id": "not_resolved", "label": "No, it's not resolved", "style": "neutral" }
    ]
  }
}
```

The front end renders `choice` as buttons and sends the chosen `id` to the actions route. Hermes decides which choices exist, so the screen needs no business rules. Styles: `success`, `primary`, `neutral`, `link`.

**Conversation flow** (decided by Hermes)
- The ticket is created as soon as Hermes can place the issue in a category (AI Self-Service, Pending, no technician).
- A knowledge base fix is posted as the solution, followed by "Is your issue resolved?". Yes closes the ticket. No leads to "Shall I assign this to a technician?", where Yes assigns it and No continues troubleshooting.
- With no fix, Hermes asks up to two rounds of questions, then "Do you need a technician to look at this?".
- "Talk to a technician" is available at any time until the ticket is with a technician.
- Urgent issues and explicit requests for a person are assigned immediately.
- Self-service tickets with 24 hours of inactivity close automatically.
