import subprocess
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

try:
    from .core import list_all_profiles, is_profile_logged_in, get_profile_home, AGY_BIN, validate_profile_name
    from .db import get_connection
except Exception:
    from core import list_all_profiles, is_profile_logged_in, get_profile_home, AGY_BIN, validate_profile_name
    from db import get_connection

def parse_relative_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "-"
    try:
        # e.g. 2026-09-11T22:42:27Z
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = dt - now
        total_seconds = int(diff.total_seconds())
        if total_seconds <= 0:
            return "ready"
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return iso_str[:16]

def fetch_profile_quota(profile: str) -> Dict[str, Any]:
    if not validate_profile_name(profile) or not is_profile_logged_in(profile):
        return {"logged_in": False, "profile": profile}

    home_dir = get_profile_home(profile)
    env = os.environ.copy()
    env["HOME"] = home_dir
    env["PATH"] = f"/home/kacper/.local/bin:{env.get('PATH', '')}"

    result = {
        "profile": profile,
        "logged_in": True,
        "gemini_5h_pct": None,
        "gemini_5h_reset": None,
        "gemini_5h_human": "-",
        "gemini_weekly_pct": None,
        "gemini_weekly_reset": None,
        "gemini_weekly_human": "-",
        "claude_5h_pct": None,
        "claude_weekly_pct": None,
        "claude_weekly_reset": None,
        "claude_weekly_human": "-",
        "error": None,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        proc = subprocess.run(
            [AGY_BIN, "-p", "/usage", "--output-format", "json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=12
        )
        if proc.returncode != 0:
            result["error"] = proc.stderr.strip() or "Process error"
            return result

        data = json.loads(proc.stdout)
        groups = data.get("command", {}).get("data", {}).get("groups", [])
        for g in groups:
            g_name = g.get("name", "")
            buckets = g.get("buckets", [])
            if "Gemini" in g_name:
                for b in buckets:
                    w = b.get("window")
                    rem = b.get("remaining_fraction")
                    pct = round(rem * 100) if rem is not None else None
                    reset_t = b.get("reset_time")
                    if w == "5h":
                        result["gemini_5h_pct"] = pct
                        result["gemini_5h_reset"] = reset_t
                        result["gemini_5h_human"] = parse_relative_time(reset_t)
                    elif w == "weekly":
                        result["gemini_weekly_pct"] = pct
                        result["gemini_weekly_reset"] = reset_t
                        result["gemini_weekly_human"] = parse_relative_time(reset_t)
            elif "Claude" in g_name or "GPT" in g_name:
                for b in buckets:
                    w = b.get("window")
                    rem = b.get("remaining_fraction")
                    pct = round(rem * 100) if rem is not None else None
                    reset_t = b.get("reset_time")
                    if w == "5h":
                        result["claude_5h_pct"] = pct
                    elif w == "weekly":
                        result["claude_weekly_pct"] = pct
                        result["claude_weekly_reset"] = reset_t
                        result["claude_weekly_human"] = parse_relative_time(reset_t)
    except Exception as e:
        result["error"] = str(e)

    save_quota_to_db(result)
    return result

def save_quota_to_db(quota: Dict[str, Any]):
    conn = get_connection()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_quotas (
                profile TEXT PRIMARY KEY,
                gemini_5h_pct INTEGER,
                gemini_5h_reset TEXT,
                gemini_5h_human TEXT,
                gemini_weekly_pct INTEGER,
                gemini_weekly_reset TEXT,
                gemini_weekly_human TEXT,
                claude_5h_pct INTEGER,
                claude_weekly_pct INTEGER,
                claude_weekly_reset TEXT,
                claude_weekly_human TEXT,
                updated_at TEXT
            );
        """)
        conn.execute("""
            INSERT INTO profile_quotas (
                profile, gemini_5h_pct, gemini_5h_reset, gemini_5h_human,
                gemini_weekly_pct, gemini_weekly_reset, gemini_weekly_human,
                claude_5h_pct, claude_weekly_pct, claude_weekly_reset, claude_weekly_human, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile) DO UPDATE SET
                gemini_5h_pct=excluded.gemini_5h_pct,
                gemini_5h_reset=excluded.gemini_5h_reset,
                gemini_5h_human=excluded.gemini_5h_human,
                gemini_weekly_pct=excluded.gemini_weekly_pct,
                gemini_weekly_reset=excluded.gemini_weekly_reset,
                gemini_weekly_human=excluded.gemini_weekly_human,
                claude_5h_pct=excluded.claude_5h_pct,
                claude_weekly_pct=excluded.claude_weekly_pct,
                claude_weekly_reset=excluded.claude_weekly_reset,
                claude_weekly_human=excluded.claude_weekly_human,
                updated_at=excluded.updated_at
        """, (
            quota["profile"],
            quota.get("gemini_5h_pct"),
            quota.get("gemini_5h_reset"),
            quota.get("gemini_5h_human"),
            quota.get("gemini_weekly_pct"),
            quota.get("gemini_weekly_reset"),
            quota.get("gemini_weekly_human"),
            quota.get("claude_5h_pct"),
            quota.get("claude_weekly_pct"),
            quota.get("claude_weekly_reset"),
            quota.get("claude_weekly_human"),
            quota.get("updated_at")
        ))
    conn.close()

def get_cached_quotas() -> Dict[str, Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sqlite_master WHERE type='table' AND name='profile_quotas';")
    if not cursor.fetchone():
        conn.close()
        return {}
    cursor.execute("SELECT * FROM profile_quotas;")
    rows = cursor.fetchall()
    conn.close()
    return {r["profile"]: dict(r) for r in rows}

def refresh_all_quotas() -> Dict[str, Dict[str, Any]]:
    profiles = list_all_profiles()
    results = {}
    for p in profiles:
        if is_profile_logged_in(p):
            results[p] = fetch_profile_quota(p)
    return results

if __name__ == "__main__":
    print("Testing quota refresh for account-01 & account-02...")
    q1 = fetch_profile_quota("account-01")
    q2 = fetch_profile_quota("account-02")
    print("Account-01:", q1)
    print("Account-02:", q2)
