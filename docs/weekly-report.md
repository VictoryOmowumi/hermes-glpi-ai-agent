# Weekly service desk report

The IT Service Desk console (**Weekly report**) produces the weekly figures straight from GLPI, for any week in the last two years. **Excel** downloads the same figures, one sheet per section, for the weekly meeting.

## How each figure is worked out

| Figure | Definition |
|---|---|
| Week | Monday 00:00 to Sunday 23:59. |
| New | Opened during the week. |
| Brought forward | Opened before the week and not resolved when it started. |
| Handled | New + brought forward. |
| Resolved | Solved during the week, by GLPI's **solve date**. Closed tickets with no solve date use the close date. |
| Open at week end | Handled but not resolved by Sunday night. Past weeks show what was open then, not now. |
| SLA met / breached | Each ticket's own GLPI **Time to resolve** deadline, from the SLA on its category. Met = solved by the deadline. |
| No SLA | No deadline on the ticket. Never counted as met or breached. |
| SLA compliance | Met ÷ (met + breached), for tickets resolved in the week. |
| Overdue, still open | Open at week end with the deadline already passed. |
| Handling time | Opening to resolution in calendar hours. Median and average, for new tickets and for backlog cleared separately. |
| Technicians | Every technician on a ticket gets credit for it. Tickets on GLPI's `glpi` admin account count as **No technician**. |

## Why the figures differ from the hand-made spreadsheet

1. **Resolution uses the solve date, not "Last update".** "Last update" also moves when GLPI's scheduled job closes solved tickets, which batches many tickets at the same minute and inflates handling time and SLA breaches.
2. **Open tickets are never "met".** The spreadsheet's technician table counted open tickets as meeting SLA (85 met against 24 in the headline for week 43).
3. **Late open tickets are shown.** Compliance only judges resolved tickets, so open tickets past their deadline are reported separately as *Overdue, still open*.
4. **Old backlog is kept apart.** Clearing a month-old ticket makes the week's average look worse; the report shows it under *Backlog cleared*.
5. **No copied formulas.** In the week 43 sheet, the "Other Technicians" handling time was the overall figure (334.6 hours).
6. **Shared tickets count for everyone on them,** instead of only the first name a search pattern matches.

## Notes

- Figures are cached for one minute; **Refresh** reloads them from GLPI.
- *Status today* in the ticket list and *Open by status* show each ticket's current GLPI status.
- Tickets in GLPI's trash are not counted.
