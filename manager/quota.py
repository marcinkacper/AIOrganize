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
        total_hours = total_seconds // 3600
        if days > 0:
            return f"{days}d {hours}h (~{total_hours}h)"
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
        "gemini_effective_pct": None,
        "gemini_status": "Unknown",
        "gemini_wait_human": "-",
        "gemini_5h_pct": None,
        "gemini_5h_disabled": False,
        "gemini_5h_reset": None,
        "gemini_5h_human": "-",
        "gemini_weekly_pct": None,
        "gemini_weekly_reset": None,
        "gemini_weekly_human": "-",
        "claude_effective_pct": None,
        "claude_status": "Unknown",
        "claude_wait_human": "-",
        "claude_5h_pct": None,
        "claude_5h_disabled": False,
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
        
        gemini_5h_raw_pct = None
        gemini_5h_dis = False
        gemini_5h_res = None
        gemini_wk_pct = None
        gemini_wk_res = None

        claude_5h_raw_pct = None
        claude_5h_dis = False
        claude_5h_res = None
        claude_wk_pct = None
        claude_wk_res = None

        for g in groups:
            g_name = g.get("name", "")
            buckets = g.get("buckets", [])
            if "Gemini" in g_name:
                for b in buckets:
                    w = b.get("window")
                    dis = bool(b.get("disabled", False))
                    rem = b.get("remaining_fraction")
                    pct = round(rem * 100) if rem is not None else None
                    reset_t = b.get("reset_time")
                    if w == "5h":
                        gemini_5h_dis = dis
                        gemini_5h_raw_pct = pct
                        gemini_5h_res = reset_t
                    elif w == "weekly":
                        gemini_wk_pct = pct
                        gemini_wk_res = reset_t
            elif "Claude" in g_name or "GPT" in g_name:
                for b in buckets:
                    w = b.get("window")
                    dis = bool(b.get("disabled", False))
                    rem = b.get("remaining_fraction")
                    pct = round(rem * 100) if rem is not None else None
                    reset_t = b.get("reset_time")
                    if w == "5h":
                        claude_5h_dis = dis
                        claude_5h_raw_pct = pct
                        claude_5h_res = reset_t
                    elif w == "weekly":
                        claude_wk_pct = pct
                        claude_wk_res = reset_t

        # === Interpret Gemini Limits ===
        result["gemini_weekly_pct"] = gemini_wk_pct
        result["gemini_weekly_reset"] = gemini_wk_res
        result["gemini_weekly_human"] = parse_relative_time(gemini_wk_res)
        result["gemini_5h_reset"] = gemini_5h_res
        result["gemini_5h_disabled"] = gemini_5h_dis

        if gemini_wk_pct == 0 or gemini_5h_dis:
            # When weekly limit is 0%, Google returns 5h disabled: true (with fraction: 1.0).
            # The usable quota is 0%, and the reset time is the weekly reset.
            result["gemini_5h_pct"] = 0
            result["gemini_5h_human"] = "disabled"
            result["gemini_effective_pct"] = 0
            result["gemini_status"] = "Weekly Limit Reached"
            result["gemini_wait_human"] = result["gemini_weekly_human"]
        elif gemini_5h_raw_pct == 0:
            # 5-hour short term cooldown, but weekly quota remains
            result["gemini_5h_pct"] = 0
            result["gemini_5h_human"] = parse_relative_time(gemini_5h_res)
            result["gemini_effective_pct"] = 0
            result["gemini_status"] = "5h Limit Reached"
            result["gemini_wait_human"] = result["gemini_5h_human"]
        elif gemini_5h_raw_pct is not None and gemini_wk_pct is not None:
            result["gemini_5h_pct"] = gemini_5h_raw_pct
            result["gemini_5h_human"] = parse_relative_time(gemini_5h_res)
            result["gemini_effective_pct"] = min(gemini_5h_raw_pct, gemini_wk_pct)
            result["gemini_status"] = "Available"
            result["gemini_wait_human"] = "ready"
        else:
            eff = gemini_wk_pct if gemini_wk_pct is not None else gemini_5h_raw_pct
            result["gemini_5h_pct"] = gemini_5h_raw_pct
            result["gemini_5h_human"] = parse_relative_time(gemini_5h_res)
            result["gemini_effective_pct"] = eff
            result["gemini_status"] = "Available" if (eff or 0) > 0 else "Limit Reached"
            result["gemini_wait_human"] = "-"

        # === Interpret Claude / GPT Limits ===
        result["claude_weekly_pct"] = claude_wk_pct
        result["claude_weekly_reset"] = claude_wk_res
        result["claude_weekly_human"] = parse_relative_time(claude_wk_res)
        result["claude_5h_disabled"] = claude_5h_dis

        if claude_wk_pct == 0 or claude_5h_dis:
            result["claude_5h_pct"] = 0
            result["claude_effective_pct"] = 0
            result["claude_status"] = "Weekly Limit Reached"
            result["claude_wait_human"] = result["claude_weekly_human"]
        elif claude_5h_raw_pct == 0:
            result["claude_5h_pct"] = 0
            result["claude_effective_pct"] = 0
            result["claude_status"] = "5h Limit Reached"
            result["claude_wait_human"] = parse_relative_time(claude_5h_res)
        elif claude_5h_raw_pct is not None and claude_wk_pct is not None:
            result["claude_5h_pct"] = claude_5h_raw_pct
            result["claude_effective_pct"] = min(claude_5h_raw_pct, claude_wk_pct)
            result["claude_status"] = "Available"
            result["claude_wait_human"] = "ready"
        else:
            eff_c = claude_wk_pct if claude_wk_pct is not None else claude_5h_raw_pct
            result["claude_5h_pct"] = claude_5h_raw_pct
            result["claude_effective_pct"] = eff_c
            result["claude_status"] = "Available" if (eff_c or 0) > 0 else "Limit Reached"
            result["claude_wait_human"] = "-"

    except Exception as e:
        result["error"] = str(e)

    save_quota_to_db(result)
    return result

def ensure_quota_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile_quotas (
            profile TEXT PRIMARY KEY,
            gemini_effective_pct INTEGER,
            gemini_status TEXT,
            gemini_wait_human TEXT,
            gemini_5h_pct INTEGER,
            gemini_5h_disabled INTEGER,
            gemini_5h_reset TEXT,
            gemini_5h_human TEXT,
            gemini_weekly_pct INTEGER,
            gemini_weekly_reset TEXT,
            gemini_weekly_human TEXT,
            claude_effective_pct INTEGER,
            claude_status TEXT,
            claude_wait_human TEXT,
            claude_5h_pct INTEGER,
            claude_5h_disabled INTEGER,
            claude_weekly_pct INTEGER,
            claude_weekly_reset TEXT,
            claude_weekly_human TEXT,
            updated_at TEXT
        );
    """)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(profile_quotas);")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    cols_to_add = [
        ("gemini_effective_pct", "INTEGER"),
        ("gemini_status", "TEXT"),
        ("gemini_wait_human", "TEXT"),
        ("gemini_5h_disabled", "INTEGER"),
        ("claude_effective_pct", "INTEGER"),
        ("claude_status", "TEXT"),
        ("claude_wait_human", "TEXT"),
        ("claude_5h_disabled", "INTEGER"),
    ]
    for col, ctype in cols_to_add:
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE profile_quotas ADD COLUMN {col} {ctype};")

def save_quota_to_db(quota: Dict[str, Any]):
    conn = get_connection()
    with conn:
        ensure_quota_table(conn)
        conn.execute("""
            INSERT INTO profile_quotas (
                profile, gemini_effective_pct, gemini_status, gemini_wait_human,
                gemini_5h_pct, gemini_5h_disabled, gemini_5h_reset, gemini_5h_human,
                gemini_weekly_pct, gemini_weekly_reset, gemini_weekly_human,
                claude_effective_pct, claude_status, claude_wait_human,
                claude_5h_pct, claude_5h_disabled,
                claude_weekly_pct, claude_weekly_reset, claude_weekly_human, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile) DO UPDATE SET
                gemini_effective_pct=excluded.gemini_effective_pct,
                gemini_status=excluded.gemini_status,
                gemini_wait_human=excluded.gemini_wait_human,
                gemini_5h_pct=excluded.gemini_5h_pct,
                gemini_5h_disabled=excluded.gemini_5h_disabled,
                gemini_5h_reset=excluded.gemini_5h_reset,
                gemini_5h_human=excluded.gemini_5h_human,
                gemini_weekly_pct=excluded.gemini_weekly_pct,
                gemini_weekly_reset=excluded.gemini_weekly_reset,
                gemini_weekly_human=excluded.gemini_weekly_human,
                claude_effective_pct=excluded.claude_effective_pct,
                claude_status=excluded.claude_status,
                claude_wait_human=excluded.claude_wait_human,
                claude_5h_pct=excluded.claude_5h_pct,
                claude_5h_disabled=excluded.claude_5h_disabled,
                claude_weekly_pct=excluded.claude_weekly_pct,
                claude_weekly_reset=excluded.claude_weekly_reset,
                claude_weekly_human=excluded.claude_weekly_human,
                updated_at=excluded.updated_at
        """, (
            quota["profile"],
            quota.get("gemini_effective_pct"),
            quota.get("gemini_status"),
            quota.get("gemini_wait_human"),
            quota.get("gemini_5h_pct"),
            1 if quota.get("gemini_5h_disabled") else 0,
            quota.get("gemini_5h_reset"),
            quota.get("gemini_5h_human"),
            quota.get("gemini_weekly_pct"),
            quota.get("gemini_weekly_reset"),
            quota.get("gemini_weekly_human"),
            quota.get("claude_effective_pct"),
            quota.get("claude_status"),
            quota.get("claude_wait_human"),
            quota.get("claude_5h_pct"),
            1 if quota.get("claude_5h_disabled") else 0,
            quota.get("claude_weekly_pct"),
            quota.get("claude_weekly_reset"),
            quota.get("claude_weekly_human"),
            quota.get("updated_at")
        ))
    conn.close()

def get_cached_quotas() -> Dict[str, Dict[str, Any]]:
    conn = get_connection()
    ensure_quota_table(conn)
    cursor = conn.cursor()
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
