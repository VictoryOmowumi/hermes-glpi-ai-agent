import os
import time
import requests
from dotenv import load_dotenv

# Load secure environment variables from your .env file
load_dotenv()

GLPI_BASE_URL = os.getenv("GLPI_BASE_URL", "http://localhost:8888/glpi/apirest.php")

# URL pointing directly to GLPI's core email routing controller (same GLPI install as the REST API)
CRON_URL = GLPI_BASE_URL.replace("/apirest.php", "/front/cron.php")

# POLL_INTERVAL defines how many seconds to wait before scanning again
POLL_INTERVAL = 30

if __name__ == "__main__":
    print(f"🔄 Starting Automatic Local Email Retrieval Loop against {CRON_URL} ...")
    while True:
        try:
            # Pinging cron.php makes GLPI run its next due automatic action (mailgate checks the inbox)
            response = requests.get(CRON_URL, timeout=120)
            if response.status_code == 200:
                print("📥 Triggered GLPI automatic actions successfully.")
            else:
                print(f"⚠️ GLPI cron returned HTTP {response.status_code}")
        except KeyboardInterrupt:
            print("\n🛑 Email retrieval loop terminated by user.")
            break
        except Exception as e:
            print(f"⚠️ Sync check delayed: {e}")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Email retrieval loop terminated by user.")
            break
