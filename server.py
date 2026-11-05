"""Starts Hermes: the ticket poller, the manual trigger endpoint and the Hermes API, on this machine only.

Run with:  python server.py   (python app.py does the same)
"""
import threading
import time

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

import app
import hermes_guard
import hermes_store
from hermes_admin_api import router as admin_router
from hermes_api import router

app.api_app.include_router(router)
app.api_app.include_router(admin_router)

@app.api_app.exception_handler(app.GLPIUnavailable)
async def glpi_unavailable(request: Request, exc: app.GLPIUnavailable):
    # A clear, safe answer for callers; the details stay in this log
    print(f"⚠️ {exc}")
    return JSONResponse(status_code=503, content={"detail": "The IT ticket system is temporarily unavailable. Please try again shortly."})

@app.api_app.exception_handler(hermes_guard.HermesPaused)
async def hermes_paused(request: Request, exc: hermes_guard.HermesPaused):
    return JSONResponse(status_code=503, content={"detail": f"{exc}. Resume Hermes in the IT Service Desk console (Safety) first."})

RETENTION_CHECK_SECONDS = 24 * 60 * 60

def enforce_chat_retention():
    """Deletes chats past the retention period, at start-up and then once a day"""
    while True:
        try:
            removed = hermes_store.purge_expired_chats()
            if removed["conversations"] or removed["notifications"]:
                print(f"🧹 Retention: deleted {removed['conversations']} conversations and {removed['notifications']} notifications "
                      f"older than {hermes_store.CHAT_RETENTION_DAYS} days")
            hermes_store.purge_safety_records()
        except Exception as e:
            print(f"⚠️ Chat retention check failed (will retry tomorrow): {e}")
        time.sleep(RETENTION_CHECK_SECONDS)

def main():
    threading.Thread(target=app.poll_new_tickets, daemon=True).start()
    threading.Thread(target=app.poll_ticket_changes, daemon=True).start()
    threading.Thread(target=enforce_chat_retention, daemon=True).start()
    print("⚡ Hermes running on http://127.0.0.1:8000 (ticket poller, manual trigger /glpi-webhook, Hermes API /v1)")
    if not __import__("hermes_api").HERMES_API_KEY:
        print("⚠️ HERMES_API_KEY is not set in .env, so the Hermes API will refuse all requests")
    if hermes_guard.READ_ONLY:
        print("👀 HERMES_READ_ONLY is on: nothing will be changed in GLPI, and IT Support chat is closed")
    if hermes_guard.paused():
        print(f"⏸️ Hermes is paused ({hermes_guard.paused()['reason']}). Resume it in the IT Service Desk console (Safety).")
    if not app.CATEGORY_USER_TOKEN:
        print("ℹ️ GLPI_CATEGORY_USER_TOKEN is not set: approved category changes use Hermes' own GLPI account "
              "(proof of concept only; production needs the separate category account)")
    # Listen on this machine only: callers are local back ends, never browsers or other machines
    uvicorn.run(app.api_app, host="127.0.0.1", port=8000)

if __name__ == "__main__":
    main()
