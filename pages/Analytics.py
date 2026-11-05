from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import requests
import streamlit as st

from app import GLPI_BASE_URL, APP_TOKEN, get_glpi_session, clean_html
from portal_store import require_user, is_admin, all_conversation_outcomes

# =====================================================================
# 📊 AI SERVICE DESK ANALYTICS (Streamlit page, shown in the portal sidebar)
# Tracks how many tickets the AI resolves on its own versus escalates to a technician
# =====================================================================
RESOLVED = "Resolved by AI"
ESCALATED = "Escalated to technician"
AWAITING = "Awaiting AI"
NOT_AI = "Not handled by AI"
AI_OUTCOMES = [RESOLVED, ESCALATED]

STATUS_LABELS = {1: "New", 2: "Assigned", 3: "Planned", 4: "Pending", 5: "Solved", 6: "Closed"}
PERIODS = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90, "All time": None}

def chart_colors():
    """Two fixed series colors (blue = resolved, orange = escalated), stepped for the active light or dark theme"""
    try:
        dark = st.context.theme.type == "dark"
    except Exception:
        dark = True
    if dark:
        return {"series": ["#3987e5", "#d95926"], "surface": "#0e1117", "grid": "#2c2c2a", "muted": "#898781"}
    return {"series": ["#2a78d6", "#eb6834"], "surface": "#ffffff", "grid": "#e1e0d9", "muted": "#898781"}

def glpi_list(session_token, path):
    headers = {"App-Token": APP_TOKEN, "Session-Token": session_token}
    response = requests.get(f"{GLPI_BASE_URL}/{path}", headers=headers)
    if response.status_code not in (200, 206):
        raise Exception(f"GLPI read failed for {path}: {response.text[:200]}")
    data = response.json()
    return data if isinstance(data, list) else []

@st.cache_data(ttl=60, show_spinner="Loading tickets from GLPI...")
def load_tickets():
    """Reads tickets and the AI agent's activity from GLPI and labels each ticket with its AI outcome"""
    session_token = get_glpi_session()
    headers = {"App-Token": APP_TOKEN, "Session-Token": session_token}
    ai_user_id = requests.get(f"{GLPI_BASE_URL}/getFullSession", headers=headers).json()["session"]["glpiID"]

    tickets = glpi_list(session_token, "Ticket?range=0-499&sort=id&order=DESC")
    solutions = glpi_list(session_token, "ITILSolution?range=0-999")
    followups = glpi_list(session_token, "ITILFollowup?range=0-1999")
    assignments = glpi_list(session_token, "Ticket_User?range=0-1999")
    users = {u["id"]: (f"{u.get('firstname') or ''} {u.get('realname') or ''}".strip() or u["name"]) for u in glpi_list(session_token, "User?range=0-499")}
    categories = {c["id"]: clean_html(c["name"]) for c in glpi_list(session_token, "ITILCategory?range=0-499")}
    requests.get(f"{GLPI_BASE_URL}/killSession", headers=headers)

    ai_solved = {s["items_id"] for s in solutions if s.get("itemtype") == "Ticket" and s.get("users_id") == ai_user_id}
    ai_replied = {f["items_id"] for f in followups if f.get("itemtype") == "Ticket" and f.get("users_id") == ai_user_id and any(marker in (f.get("content") or "") for marker in ("Automated AI Assistant Reply", "Hermes"))}
    technician_of = {a["tickets_id"]: users.get(a["users_id"], "Unknown") for a in assignments if a.get("type") == 2 and a.get("users_id")}

    rows = []
    for t in tickets:
        if int(t.get("is_deleted", 0)):
            continue
        status = int(t.get("status", 0))
        from_portal = (t.get("name") or "").startswith("Portal Request:")

        if t["id"] in ai_solved and status in (5, 6):
            outcome = RESOLVED
        elif t["id"] in ai_solved or t["id"] in ai_replied or from_portal:
            # Includes tickets the AI solved that were later reopened: a person had to step in
            outcome = ESCALATED
        elif status == 1 and int(t.get("itilcategories_id", 0)) == 0:
            outcome = AWAITING
        else:
            outcome = NOT_AI

        rows.append({
            "Ticket": t["id"],
            "Title": t.get("name"),
            "Created": pd.to_datetime(t.get("date_creation")),
            "Source": "Portal chat" if from_portal else "Email / GLPI",
            "Category": categories.get(t.get("itilcategories_id"), "Uncategorized"),
            "Outcome": outcome,
            "Status": STATUS_LABELS.get(status, str(status)),
            "Technician": technician_of.get(t["id"], "—"),
        })
    return pd.DataFrame(rows)

def outcome_color(colors):
    return alt.Color("Outcome:N", scale=alt.Scale(domain=AI_OUTCOMES, range=colors["series"]), legend=alt.Legend(title=None, orient="top"))

def styled(chart, colors, height):
    return (chart.properties(height=height)
            .configure_view(stroke=None)
            .configure_axis(gridColor=colors["grid"], domainColor=colors["grid"], tickColor=colors["grid"], labelColor=colors["muted"], titleColor=colors["muted"], labelFontSize=12, titleFontSize=12))

def render_dashboard():
    st.set_page_config(page_title="AI Service Desk Analytics", page_icon="📊", layout="wide")
    user = require_user()
    if not is_admin(user):
        st.error("The Analytics page is restricted to service desk administrators.")
        st.stop()

    # The sidebar page list is hidden (see .streamlit/config.toml), so link back to the chat
    st.page_link("portal.py", label="Back to the support chat", icon=":material/arrow_back:")
    st.title("📊 AI Service Desk Analytics")
    st.caption("How many tickets the AI resolves on its own versus escalates to a technician. Data is read live from GLPI and refreshed every minute.")

    # Filters sit in one row above everything they control
    filter_period, filter_source, filter_refresh = st.columns([2, 2, 1], vertical_alignment="bottom")
    period = filter_period.selectbox("Period", list(PERIODS.keys()), index=1)
    source = filter_source.selectbox("Source", ["All sources", "Email / GLPI", "Portal chat"])
    if filter_refresh.button("🔄 Refresh", use_container_width=True):
        load_tickets.clear()

    try:
        tickets = load_tickets()
    except Exception as e:
        st.error(f"Could not load data from GLPI: {e}")
        st.stop()

    if PERIODS[period] and not tickets.empty:
        tickets = tickets[tickets["Created"] >= datetime.now() - timedelta(days=PERIODS[period])]
    if source != "All sources" and not tickets.empty:
        tickets = tickets[tickets["Source"] == source]

    conversations = pd.DataFrame(all_conversation_outcomes())
    if PERIODS[period] and not conversations.empty:
        conversations = conversations[pd.to_datetime(conversations["created_at"]) >= datetime.now() - timedelta(days=PERIODS[period])]

    if tickets.empty:
        st.info("No tickets in this period yet.")
        st.stop()

    handled = tickets[tickets["Outcome"].isin(AI_OUTCOMES)]
    resolved_count = int((handled["Outcome"] == RESOLVED).sum())
    escalated_count = int((handled["Outcome"] == ESCALATED).sum())
    awaiting_count = int((tickets["Outcome"] == AWAITING).sum())
    self_solved_chats = int((conversations["outcome"] == "self_solved").sum()) if not conversations.empty else 0
    resolution_rate = f"{resolved_count / len(handled):.0%}" if len(handled) else "—"

    # Headline numbers
    kpi = st.columns(5)
    kpi[0].metric("AI resolution rate", resolution_rate, help="Share of AI-handled tickets that the AI solved from the Knowledge Base without a technician.")
    kpi[1].metric("Resolved by AI", resolved_count, help="Tickets the AI solved from the Knowledge Base that are still Solved or Closed.")
    kpi[2].metric("Escalated to technician", escalated_count, help="Tickets the AI categorized and routed to a support group because no Knowledge Base fix applied.")
    kpi[3].metric("Awaiting AI", awaiting_count, help="New tickets the poller has not processed yet.")
    kpi[4].metric("Portal chats solved, no ticket", self_solved_chats, help="Portal conversations where the user confirmed the suggested fix worked, so no ticket was ever created.")

    if handled.empty:
        st.info("The AI has not handled any tickets in this period yet.")
        st.stop()

    colors = chart_colors()
    left, right = st.columns(2)

    with left:
        st.subheader("Resolved vs escalated, by day")
        daily = handled.assign(Day=handled["Created"].dt.strftime("%Y-%m-%d")).groupby(["Day", "Outcome"]).size().reset_index(name="Tickets")
        chart = alt.Chart(daily).mark_bar(stroke=colors["surface"], strokeWidth=2, size=28).encode(
            x=alt.X("Day:O", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Tickets:Q", title="Tickets", axis=alt.Axis(tickMinStep=1)),
            color=outcome_color(colors),
            order=alt.Order("Outcome:N", sort="descending"),
            tooltip=["Day", "Outcome", "Tickets"])
        st.altair_chart(styled(chart, colors, 300), use_container_width=True)

    with right:
        st.subheader("Resolved vs escalated, by category")
        by_category = handled.groupby(["Category", "Outcome"]).size().reset_index(name="Tickets")
        chart = alt.Chart(by_category).mark_bar(stroke=colors["surface"], strokeWidth=2, size=18).encode(
            y=alt.Y("Category:N", title=None, sort="-x", axis=alt.Axis(labelLimit=220)),
            x=alt.X("Tickets:Q", title="Tickets", axis=alt.Axis(tickMinStep=1)),
            color=outcome_color(colors),
            order=alt.Order("Outcome:N", sort="descending"),
            tooltip=["Category", "Outcome", "Tickets"])
        st.altair_chart(styled(chart, colors, 300), use_container_width=True)

    st.subheader("Escalated tickets by technician")
    escalated = handled[handled["Outcome"] == ESCALATED]
    if escalated.empty:
        st.caption("No escalated tickets in this period.")
    else:
        by_technician = escalated.assign(Technician=escalated["Technician"].replace("—", "Group queue only")).groupby("Technician").size().reset_index(name="Tickets")
        bars = alt.Chart(by_technician).mark_bar(color=colors["series"][0], cornerRadiusEnd=4, size=18).encode(
            y=alt.Y("Technician:N", title=None, sort="-x"),
            x=alt.X("Tickets:Q", title="Escalated tickets", axis=alt.Axis(tickMinStep=1)),
            tooltip=["Technician", "Tickets"])
        st.altair_chart(styled(bars, colors, max(120, 44 * len(by_technician))), use_container_width=True)

    # Table view of everything behind the charts
    st.subheader("Tickets")
    st.dataframe(tickets.sort_values("Ticket", ascending=False), hide_index=True, use_container_width=True,
                 column_config={"Created": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm")})

render_dashboard()
