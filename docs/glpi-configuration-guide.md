# GLPI Configuration Guide for Hermes

Step-by-step changes to make in GLPI's web interface for the security items in the backlog. Each part says what to change, how to check it worked, and how to undo it.

Do these while signed in to GLPI with your own administrator account, **not** the `ai.agent` account.

| Part | Backlog item | When |
|---|---|---|
| A | S4: verify Gmail's certificate on the mail receiver | Now |
| B | S3: a restricted profile for the `ai.agent` account | Now |
| C | F1: the "AI Self-Service" request source | When work on F1 starts |
| D | SLAs: response and resolution targets, which fill "Time to resolve" | When the business has agreed the targets |
| E | Routing: which team gets each category's tickets | Whenever a category or team is added |

---

## Part A: Verify Gmail's certificate on the mail receiver (S4)

**Why.** The mail receiver currently connects to Gmail with `novalidate-cert`, so the connection is encrypted but GLPI does not check it is really talking to Gmail. Someone on the network path could intercept the mailbox password and the emails.

**Already checked.** PHP on this machine verified Gmail's certificate successfully using the certificate bundle configured in `C:\xampp\php\php.ini` (`openssl.cafile`). The chain was imap.gmail.com, issued by WR2, issued by GTS Root R1. Nothing needs changing in PHP.

**Steps**
1. Go to **Setup > Receivers** and open **Support Inbox**.
2. Find the **Connection options** dropdowns next to the server name (`imap.gmail.com`). They currently read **IMAP**, **SSL**, **NO-VALIDATE-CERT**, **NO-TLS**.
3. Change **NO-VALIDATE-CERT** to **VALIDATE-CERT**. Leave the others as they are: IMAP with SSL on port 993 is already fully encrypted, so the TLS option stays NO-TLS.
4. Check that the server field now ends in `/imap/ssl/validate-cert/notls}GLPI-Tickets` (or the equivalent without `novalidate-cert`).
5. Click **Save**.

**Check it worked**
1. On the same receiver page, click **Get email tickets now**.
2. GLPI should report the number of messages fetched (zero is fine) with no connection or certificate error.
3. For a full test, send an email to the support address and confirm a ticket appears within a minute.

If GLPI shows a certificate error, tell me the exact message. The likely fix is updating the certificate bundle file, not going back to `NO-VALIDATE-CERT`.

**Undo:** set the dropdown back to NO-VALIDATE-CERT and save. Only as a temporary measure while the cause is found.

---

## Part B: A restricted profile for the `ai.agent` account (S3)

**Why.** Hermes signs in to GLPI as `ai.agent`, which currently has the **Super-Admin** profile. If its API token leaked, whoever held it would have full control of GLPI. Hermes only needs to work with tickets and read a few lists.

**What Hermes actually does in GLPI**

| Hermes action | GLPI permission needed |
|---|---|
| Read tickets and their history | Tickets: Read, See all tickets |
| Set category, urgency, impact and status | Tickets: Update, Change the priority; the status changes listed under the Life Cycles tab |
| Create tickets from the portal | Tickets: Create |
| Assign a group and a technician | Tickets: Assign a ticket |
| Post replies and internal notes | Followups: See public and private; add to all tickets |
| Post solutions | Covered by Tickets: Update |
| Count each technician's open tickets | Tickets: See all tickets (uses GLPI's search) |
| Read the knowledge base | Knowledge base: Read |
| Read groups and their members | Groups: Read; Users: Read |
| Read ticket categories | ITIL categories (under Dropdowns): Read |

Hermes never needs to delete or purge anything, or to change GLPI's setup, users, groups, categories or profiles.

### Step 1: Create an empty profile

Start from a new, empty profile rather than a copy of Technician. Technician has broad rights (for example full create, delete and purge on every asset type), and starting empty means nothing is granted by accident. GLPI's profile form also has no clone option.

1. Go to **Administration > Profiles** and click **+ Add**.
2. **Name:** Hermes API. **Comments:** *Service account for the Hermes AI agent. Do not assign to people.*
3. **Profile's interface:** Standard Interface.
4. Leave **Default Profile**, **Update your own password** and **Ticket creation form on login** unticked.
5. Click **Add**. GLPI then shows the profile with its rights tabs (Assets, Assistance, Life Cycles, Management, Tools, Administration, Setup).

### Step 2: Set the rights

Open each tab of the **Hermes API** profile and tick only the rights listed below. Everything else stays **unticked**. Click **Save** at the bottom of each tab before moving to the next. A new profile may already have a few rights ticked by default; untick anything not in these lists.

**Assistance tab**
- **Tickets:** tick **Read**, **Update** and **Create**. Leave **Delete** and **Purge** unticked. In the extra ticket rights, tick **See all tickets**, **Assign a ticket** and **Change the priority**. Untick **Steal a ticket** and **Be in charge of a ticket**.
- **Followups:** tick **See public ones**, **See all** (private) and **Add to all tickets**. Untick update and delete rights.
- **Tasks, Validations, Problems, Changes, Planning, Statistics:** untick everything.

**Tools tab**
- **Knowledge base:** tick **Read** only.
- Everything else (Projects, Reminders, RSS feeds, Saved searches and so on): untick.

**Administration tab**
- **Users:** tick **Read** only.
- **Groups:** tick **Read** only.
- **Profiles, Entities, Rules, Logs, Transfer and everything else:** untick.

**Setup tab**
- **ITIL categories** (in some versions listed under **Dropdowns**): tick **Read** only.
- **General setup, Automatic actions, Notifications, Receivers, Authentication, Plugins and everything else:** untick.

**Life Cycles tab**

This tab controls which ticket status changes the profile may make. On a new profile **every change is unticked**, which stops Hermes marking tickets solved or closing them. In the **Tickets** life cycle grid (rows are the current status, columns the new status), tick exactly these:

| From (row) | To (columns to tick) | Why Hermes needs it |
|---|---|---|
| New | Processing (assigned), Pending, Solved | Routing a ticket; F1 parks a chat ticket as Pending while Hermes talks to the user; posting a knowledge base solution |
| Processing (assigned) | Solved | A solution posted after escalation (future use) |
| Pending | Processing (assigned), Solved, Closed | F1: escalating to a technician, posting a solution, closing an abandoned chat |
| Solved | Processing (assigned), Pending, Closed | Re-escalating when a solution did not work; F1: the user replies after a solution and the chat continues; F1's 24-hour auto-close |

Leave **Problems** and **Changes** life cycles fully unticked, and leave every other ticket transition unticked. Click **Save**.

**Assets and Management tabs**
- Untick everything.

### Step 3: Give `ai.agent` the new profile

1. Go to **Administration > Users** and open **ai.agent**.
2. Open the **Authorizations** tab.
3. Add an authorization: profile **Hermes API**, entity **Root entity**, **Recursive: Yes**. Click **Add**.
4. On the main tab of the user, set **Default profile** to **Hermes API** and save.
5. Back on **Authorizations**, tick the **Super-Admin** line and remove it with **Actions > Delete permanently**.

Only remove Super-Admin from `ai.agent`. Your own administrator account must keep it.

### Step 4: Check it worked

1. Restart the Hermes daemon (`python app.py`) so it signs in with the new profile.
2. Tell me when this is done. I will run a read-only check covering everything Hermes reads (tickets, categories, knowledge base, groups and members, technician workloads) and report any permission that is missing.
3. Then create one test ticket through GLPI's **Helpdesk** form with **Category left empty**. Within the polling interval, Hermes should categorise it, assign a group and technician, and post its reply and internal note. Any permission problem shows in the terminal as `❌` or `⚠️` with GLPI's reason.

If something fails, add only the one right GLPI's error points to, rather than going back to Super-Admin.

**Undo:** add the **Super-Admin** authorization back to `ai.agent` (Step 3, the other way round).

---

## Part C: The "AI Self-Service" request source (F1)

Do this when work on F1 starts. It lets reports separate tickets created by Hermes' chat from email and Helpdesk tickets.

1. Go to **Setup > Dropdowns**, then **Assistance > Request sources**.
2. Click **Add**. Name: **AI Self-Service**. Comment: *Tickets created through Hermes' chat portal.*
3. Set **Active** to **Yes** and **Request source visible for tickets** to **Yes** (otherwise the source does not appear on tickets or in the ticket form's list). Leave every **Default for...** option and **Request source visible for followups** set to **No**.
4. Click **Add**, then note the **ID** shown on the item (or in the list).
5. Add this line to Hermes' `.env` file, with your ID in place of 7: `HERMES_SELF_SERVICE_SOURCE_ID=7`
6. Restart `python app.py` and `streamlit run portal.py`. Until this is set, the portal shows a message saying Hermes is not fully configured.

The `ai.agent` account does not need permission to create request sources, only to set one on a ticket, which it already has through Tickets: Create.

---

## Part D: SLAs (response and resolution targets)

**Why.** "Time to resolve" on a ticket is a **deadline set by an SLA**, not the time the ticket took. No SLA is attached to any ticket today, so the field is empty everywhere. The actual time taken is already recorded (the ticket's resolution date and Statistics); SLAs add the target to measure it against, and alerts before it is missed.

**How GLPI's SLA pieces fit together**

| Piece | What it is |
|---|---|
| **Calendar** | When the clock runs. The service desk is 24/7, so every hour counts |
| **Service level (SLM)** | A container for a set of SLAs, linked to the calendar |
| **SLA: Time to own (TTO)** | Deadline for a technician to take the ticket (assignment / first response) |
| **SLA: Time to resolve (TTR)** | Deadline for the ticket to be Solved. This fills "Time to resolve" |
| **Escalation levels** | Automatic actions shortly before a deadline, for example alerting a lead |
| **Business rules** | Attach the right SLAs to each ticket automatically, based on its priority |

The clock **pauses while a ticket is Pending** ("Waiting for you"). Hermes keeps AI self-service tickets Pending while it talks to the employee, so they do not count against IT until they are assigned.

### Step 1: A 24/7 calendar

1. Go to **Setup > Dropdowns > Calendars** and click **Add**. Name: **SBC 24/7**.
2. Open it, go to the **Time ranges** tab, and add one range per day (Monday to Sunday) from **00:00 to 24:00** (GLPI's end-of-day value, so the whole day counts).
3. Leave the **Close times** tab empty unless the desk does close on public holidays.

### Step 2: The service level

1. Go to **Setup > Service levels** and click **Add**.
2. Name: **SBC IT Service Desk**. Calendar: **SBC 24/7**. Click **Add**.

### Step 3: The SLAs

Open **SBC IT Service Desk**, go to the **SLA** tab and add one SLA per row below, choosing the **Type** (Time to own or Time to resolve) and the **Maximum time**. Leave "End of working day" off.

The targets below are a **suggested starting point only**; the service desk lead should agree the real values. GLPI's priority comes from urgency and impact, which Hermes sets on every ticket.

| Priority | Time to own (TTO) | Time to resolve (TTR) |
|---|---|---|
| Very high | 15 minutes | 4 hours |
| High | 30 minutes | 8 hours |
| Medium | 1 hour | 24 hours |
| Low | 4 hours | 3 days |
| Very low | 8 hours | 5 days |

Suggested names: **P1 Very high – Own 15m**, **P1 Very high – Resolve 4h**, and so on, so they are easy to pick in rules and reports. The existing "3rd Floor/Infrastructure Resolve Goal" (2 hours) can stay as an extra, stricter SLA for network outages, or be retired in favour of this table.

### Step 4: Escalation levels (recommended)

For each **Time to resolve** SLA, open it, go to the **Escalation levels** tab and add a level such as:
- **Name:** Breach warning
- **Execution:** 1 hour **before** the deadline (shorter for the 4-hour SLA, for example 30 minutes)
- **Action:** add the service desk lead group as an observer, or send a notification, so someone acts before the target is missed.

Notifications only go out once GLPI's email notifications are enabled (backlog item B3).

### Step 5: Attach SLAs automatically with business rules

1. Go to **Administration > Rules > Business rules for tickets** and click **Add**.
2. Create one rule per priority for the **resolution** target:
   - **Name:** SLA resolve – Very high
   - **Use rule for:** **Add / Update**. This matters: Hermes sets urgency and impact just *after* an email or Helpdesk ticket is created, so the rule must also run on updates to pick up the final priority.
   - **Criteria:** Priority **is** Very high
   - **Actions:** SLA Time to resolve **Assign** P1 Very high – Resolve 4h
3. Create one rule per priority for the **ownership** target, excluding AI self-service chats (many of those are resolved by Hermes without a technician, so a "time to own" target would always be missed):
   - **Name:** SLA own – Very high
   - **Use rule for:** Add / Update
   - **Criteria:** Priority **is** Very high **AND** Request source **is not** AI Self-Service
   - **Actions:** SLA Time to own **Assign** P1 Very high – Own 15m
4. Repeat for High, Medium, Low and Very low.

Check the urgency × impact → priority grid in **Setup > General > Assistance** matches how the desk thinks about priority, since every SLA follows it.

### Step 6: Check it worked

1. Create a test ticket through the Helpdesk form (or send a test email) and wait for Hermes to process it.
2. Open the ticket: **Time to resolve** (and **Time to own**, for non-chat tickets) should now show a date and time, and the SLA names should appear on the ticket's SLA fields.
3. If they are empty, check the rule's **Use rule for** is Add / Update and its criteria match the ticket's priority.

Hermes needs no extra permissions for this: GLPI applies the rules itself when Hermes updates the ticket.
---

## Part E: Routing tickets to the right team

Hermes reads categories and support groups live from GLPI, so new ones are used within five minutes. For each ticket, Hermes chooses the group like this:

1. **The category's Group in charge.** If the ticket's category, or its parent category, has a Group in charge, the ticket always goes there. This is predictable and does not depend on the AI.
2. **Otherwise, the AI's choice.** The AI picks from every group that can be assigned tickets, using each group's Comments to understand what it handles.
3. **If neither applies,** the ticket goes to Service Desk - Tier 1. A different default can be set with `HERMES_DEFAULT_GROUP_ID` in Hermes' `.env`.

The technician is the category's **Technician in charge** if one is set and active. Otherwise it is the least busy member of the chosen group.

**Set a category's team** (recommended for every category owned by a specialist team):
1. Go to **Setup > Dropdowns > ITIL categories** and open the category, for example *Business Applications > Beta Padi*.
2. Set **Group in charge**, for example *Development Team > Web Development Team*. Optionally, set **Technician in charge** as well.
3. Click **Save**. Setting it on a parent such as *Business Applications* covers all of its sub-categories that have no group of their own.

**Describe each group** (helps the AI for categories without a Group in charge):
1. Go to **Administration > Groups** and open the group.
2. In **Comments**, write one line on what the team handles, for example *Business applications: Beta Padi, SAP integrations, customer portal issues*.
3. Make sure **Can be assigned** is ticked under Tickets. Groups without it are never offered.

**Check it worked:** raise a test ticket in IT Support and ask for a technician. Hermes' private routing note on the ticket names the group, followed by "(the category's group in charge)" when step 1 applied.

## Part F: Tickets Hermes does not answer

Some new tickets have nobody to reply to, or should not get a reply. Hermes leaves these **New** for the service desk, with a private note saying why, and never picks them up again:

| Ticket | How Hermes recognises it |
|---|---|
| From an automated mailbox | The requester is listed in `HERMES_AUTOMATED_SENDERS`, or the address starts with no-reply, noreply, do-not-reply, mailer-daemon, postmaster, bounce, notification or alert. A reply could start a mail loop. |
| An Outlook recall | The title starts with "Recall:". |
| A forwarded GLPI notification | The title contains a ticket reference such as "[IT Service Desk #0022610]". The note names the ticket it refers to. |
| A duplicate | Same requester, same title (ignoring RE:/FW:) and almost the same description as an open ticket raised in the last 24 hours. The ticket is linked to the original as a duplicate. The same title alone is not enough, because one person often raises several genuine "Reverse back order to freight unit" tickets a day. |

Tickets raised by **members of a support group** (IT staff logging work or a fault) are categorised and routed as usual, but Hermes sends them no reply or Knowledge Base fix.

**Add a system mailbox:** in Hermes' `.env`, set `HERMES_AUTOMATED_SENDERS` to a comma-separated list of exact addresses. The default is `noc@sevenup.org,app-notification@sevenup.org`. Restart Hermes afterwards.
