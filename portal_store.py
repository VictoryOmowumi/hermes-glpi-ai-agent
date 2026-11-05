import os
import secrets
from datetime import datetime, timedelta

import streamlit as st
from dotenv import load_dotenv

# Chat history lives in hermes_store.py, so the Hermes API and daemon can use it without Streamlit
from hermes_store import connect as _connect, save_conversation, list_conversations, load_conversation, all_conversation_outcomes

load_dotenv()

# Comma-separated emails allowed to open the Analytics page (SSO sign-in required); empty means nobody
ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("PORTAL_ADMIN_EMAILS", "").split(",") if e.strip()]

# =====================================================================
# 🔐 SIGN-IN (SSO when .streamlit/secrets.toml has an [auth] section, demo sign-in otherwise)
# =====================================================================
def sso_configured():
    try:
        return "auth" in st.secrets
    except Exception:
        return False

def require_user():
    """Stops the page until someone is signed in, then returns {'email', 'name', 'sso'}"""
    if sso_configured():
        if not st.user.is_logged_in:
            st.title("Hermes · SBC IT Service Desk")
            st.write("Sign in with your company account to continue.")
            st.button("🔐 Sign in with SSO", on_click=st.login, type="primary")
            st.stop()
        email = (st.user.get("email") or st.user.get("preferred_username") or st.user.get("sub") or "").lower()
        return {"email": email, "name": st.user.get("name") or email, "sso": True}

    # No identity provider configured yet: ask who is chatting. This is NOT secure and is for demos only.
    if "demo_user" not in st.session_state:
        # A page reload starts a new session; the ?session= token in the address restores the sign-in
        restored = load_demo_session(st.query_params.get("session"))
        if restored:
            st.session_state.demo_user = restored
            return restored

        st.title("Hermes · SBC IT Service Desk")
        st.warning("Demo sign-in: SSO is not configured, so the email below is not verified.")
        with st.form("demo_sign_in"):
            name = st.text_input("Your name")
            email = st.text_input("Your work email")
            if st.form_submit_button("Continue", type="primary"):
                if "@" in email:
                    user = {"email": email.strip().lower(), "name": name.strip() or email.strip(), "sso": False}
                    st.session_state.demo_user = user
                    st.query_params["session"] = create_demo_session(user)
                    st.rerun()
                st.error("Enter a valid email address.")
        st.stop()
    return st.session_state.demo_user

def sign_out():
    if sso_configured():
        st.logout()
    else:
        delete_demo_session(st.query_params.get("session"))
        st.query_params.clear()
        st.session_state.clear()
        st.rerun()

# Demo sign-ins survive a page reload for this long
DEMO_SESSION_DAYS = 7

def create_demo_session(user):
    token = secrets.token_urlsafe(24)
    with _connect() as connection:
        connection.execute("INSERT INTO demo_sessions (token, user_email, user_name, created_at) VALUES (?, ?, ?, ?)",
                           (token, user["email"], user["name"], datetime.now().isoformat(timespec="seconds")))
    return token

def load_demo_session(token):
    if not token:
        return None
    oldest = (datetime.now() - timedelta(days=DEMO_SESSION_DAYS)).isoformat(timespec="seconds")
    with _connect() as connection:
        row = connection.execute("SELECT user_email, user_name FROM demo_sessions WHERE token = ? AND created_at >= ?", (token, oldest)).fetchone()
    return {"email": row["user_email"], "name": row["user_name"], "sso": False} if row else None

def delete_demo_session(token):
    if token:
        with _connect() as connection:
            connection.execute("DELETE FROM demo_sessions WHERE token = ?", (token,))

def is_admin(user):
    # Deny by default: only emails listed in PORTAL_ADMIN_EMAILS, and only for SSO-verified sign-ins
    return user["sso"] and user["email"] in ADMIN_EMAILS

