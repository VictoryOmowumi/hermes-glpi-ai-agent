# Satisfaction surveys

Every closed IT ticket asks the person who raised it to rate how it was handled: 1 to 5 stars, a quick "what went wrong?" for 1 or 2 stars (Not fixed, Too slow, Poor communication, Other) and an optional comment.

## What employees see

- **A rating card** slides in at the bottom right of the intranet (above the Hermes button) soon after a ticket closes. They rate it there, or close the card.
- **The notification bell**: "How did we do on IT ticket #29?", which opens the ticket in IT Support.
- **My tickets**: closed tickets waiting for a rating show **Rate this ticket**; the ticket itself has the rating form. Rated tickets show the stars given.

Closing the card stops it popping up for that ticket; it can still be rated from My tickets. A survey stays open for **14 days**. One answer per ticket, it cannot be changed, and only the person who raised the ticket can answer it.

## When a survey is opened

For **every closed ticket**, whoever closed it: the requester in IT Support chat ("close the ticket", "Yes, it's fixed"), the requester in My tickets, a technician in GLPI, or Hermes' automatic closing. Hermes notices within a minute (chat closes open it at once). Tickets Hermes deliberately did not answer (system mailboxes, recalls, forwarded notifications, duplicates) get no survey.

## Who sees the answers

- **Service desk admins**: IT Service Desk console → **Satisfaction**. Average rating, % satisfied (4–5 stars), response rate, ratings by technician, team and category, the "what went wrong" counts, and every response with its comment. Low ratings can be marked as **followed up** with a note (audited).
- **Technicians**: on the ticket in GLPI (**Satisfaction** tab), once GLPI's survey is set up as below.
- **Weekly report**: average rating, % satisfied, response rate and low ratings for the week, and each technician's average rating (also in the Excel download).

## GLPI setup (so technicians see ratings in GLPI)

Hermes copies each answer to GLPI's own satisfaction survey on the ticket. Until GLPI allows this, answers are kept in Hermes (and shown in the console) and Hermes retries every minute.

1. **Switch the survey on.** Setup → Entities → root entity → **Satisfaction survey** tab:
   - Configuring the satisfaction survey: **Internal survey**
   - Create survey after: **0 days** · Rate to trigger survey: **100%** · Duration of survey: **14 days**
   - **Save.** (GLPI's own survey email can be left off: the intranet asks instead.)
2. **Let Hermes record answers.** Administration → Profiles → the Hermes API profile → **Assistance** tab → **Satisfaction survey**: tick **Read** and **Update** (and **Create**, so Hermes can record an answer before GLPI's scheduled job has created the survey). Save.
3. **Check it worked:** rate a closed test ticket in the intranet, wait a minute, and open the ticket in GLPI: the **Satisfaction** tab shows the stars and comment. The console's Satisfaction page shows "x of y copied to GLPI".

## Personal data

Answers are personal data. A data protection **export** includes them; a **deletion** request removes the person's email and comment but keeps the star rating anonymously, so the figures stay correct.
