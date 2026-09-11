import os
import re
import time
import requests
from datetime import datetime
from typing import Dict, Any, Optional

try:
    from .db import get_connection, log_audit
except Exception:
    from db import get_connection, log_audit

DEFAULT_URL = os.getenv("KLAJNER_URL", "http://127.0.0.1:8096")
DEFAULT_LOG = os.getenv("KLAJNER_LOG", "/srv/projects/klajner/project/agy_engine.log")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RATE_LIMIT_PATTERNS = [
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"rate\s*limit", re.IGNORECASE),
    re.compile(r"quota\s*exceeded", re.IGNORECASE),
    re.compile(r"resource\s*exhausted", re.IGNORECASE),
    re.compile(r"too\s*many\s*requests", re.IGNORECASE),
]

def send_telegram_alert(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    chat_id = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

def check_klajner_health() -> Dict[str, Any]:
    url = os.getenv("KLAJNER_URL", DEFAULT_URL)
    result = {
        "online": False,
        "status": "offline",
        "response_time_ms": None,
        "default_model": None,
        "oauth_active": False,
        "last_log_error": None,
        "rate_limit_detected": False,
        "checked_at": datetime.now().isoformat()
    }
    start = time.time()
    try:
        resp = requests.get(f"{url}/health", timeout=3)
        result["response_time_ms"] = round((time.time() - start) * 1000, 2)
        if resp.status_code == 200:
            data = resp.json()
            result["online"] = True
            result["status"] = data.get("status", "unknown")
            result["default_model"] = data.get("default_model")
            result["oauth_active"] = data.get("oauth_session_active", False)
    except Exception as e:
        result["status"] = f"error: {str(e)}"

    # Check recent logs for errors or rate limits
    log_path = os.getenv("KLAJNER_LOG", DEFAULT_LOG)
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-50:]
            for line in reversed(lines):
                for pat in RATE_LIMIT_PATTERNS:
                    if pat.search(line):
                        result["rate_limit_detected"] = True
                        result["last_log_error"] = line.strip()
                        break
                if result["rate_limit_detected"]:
                    break
        except Exception:
            pass

    # Save check in DB
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO klajner_status (is_healthy, status_code, response_time_ms, details, alert_sent)
            VALUES (?, ?, ?, ?, ?)
        """, (
            result["online"],
            200 if result["online"] else 0,
            result["response_time_ms"],
            result["last_log_error"] or result["status"],
            result["rate_limit_detected"]
        ))
    conn.close()

    if result["rate_limit_detected"]:
        msg = f"⚠️ *Alerty Klajner AGY Engine*:\nWykryto zbliżanie się do limitu lub błąd 429 w usłudze Klajner!\nLog: `{result['last_log_error']}`"
        send_telegram_alert(msg)

    return result

if __name__ == "__main__":
    res = check_klajner_health()
    print("Klajner Health Check Result:", res)
