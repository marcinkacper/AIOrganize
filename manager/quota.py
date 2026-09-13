import subprocess
import json
import os
import time
import urllib.request
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

def fetch_claude_quota(profile: str) -> Dict[str, Any]:
    p = profile.replace("agy-", "account-") if profile.startswith("agy-") else profile
    cfg_dir = f"/srv/agy-manager/profiles/{p}/config" if p.startswith("claude-") else "/home/kacper"
    c_path = os.path.join(cfg_dir, ".claude.json")
    if not os.path.isfile(c_path) and p == "claude":
        c_path = "/home/kacper/.claude.json"

    fh_avail, sd_avail = 100.0, 100.0
    fh_res, sd_res = None, None
    fh_human, sd_human = "-", "-"
    live_fetched = False

    # 1. Try real-time live usage fetch via Anthropic OAuth API
    cred_paths = [
        os.path.join(cfg_dir, ".credentials.json"),
        "/home/kacper/.claude/.credentials.json"
    ]
    tokens = []
    for cp in cred_paths:
        if os.path.isfile(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    c_data = json.load(f)
                tok = c_data.get("claudeAiOauth", {}).get("accessToken")
                if tok and tok not in [t[1] for t in tokens]:
                    tokens.append((cp, tok, c_data))
            except Exception:
                pass

    for cp, tok, c_data in tokens:
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "User-Agent": "claude-code/2.1.269"
                }
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body)
                    fh = data.get("five_hour", {})
                    fh_used = fh.get("utilization", 0) if fh else 0
                    fh_avail = max(0.0, round(100.0 - (fh_used or 0), 1))
                    fh_res = fh.get("resets_at") if fh else None
                    fh_human = parse_relative_time(fh_res)

                    sd = data.get("seven_day", {})
                    sd_used = sd.get("utilization", 0) if sd else 0
                    sd_avail = max(0.0, round(100.0 - (sd_used or 0), 1))
                    sd_res = sd.get("resets_at") if sd else None
                    sd_human = parse_relative_time(sd_res)

                    live_fetched = True

                    # Synchronize valid credentials if needed
                    target_cred = os.path.join(cfg_dir, ".credentials.json")
                    if cp != target_cred and os.path.isdir(cfg_dir):
                        try:
                            with open(target_cred, "w", encoding="utf-8") as out_f:
                                json.dump(c_data, out_f, indent=2)
                        except Exception:
                            pass
                    break
        except Exception:
            pass

    # 2. Fallback to cached .claude.json if live fetch failed
    if not live_fetched and os.path.isfile(c_path):
        try:
            with open(c_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            util = data.get("cachedUsageUtilization", {}).get("utilization", {})
            fh = util.get("five_hour", {})
            fh_used = fh.get("utilization", 0) if fh else 0
            fh_avail = max(0.0, round(100.0 - (fh_used or 0), 1))
            fh_res = fh.get("resets_at") if fh else None
            fh_human = parse_relative_time(fh_res)

            sd = util.get("seven_day", {})
            sd_used = sd.get("utilization", 0) if sd else 0
            sd_avail = max(0.0, round(100.0 - (sd_used or 0), 1))
            sd_res = sd.get("resets_at") if sd else None
            sd_human = parse_relative_time(sd_res)
        except Exception:
            pass

    res = {
        "profile": profile,
        "logged_in": True,
        "gemini_effective_pct": None,
        "gemini_status": "Not Applicable",
        "gemini_wait_human": "-",
        "gemini_5h_pct": None,
        "gemini_5h_disabled": False,
        "gemini_5h_reset": None,
        "gemini_5h_human": "-",
        "gemini_weekly_pct": None,
        "gemini_weekly_reset": None,
        "gemini_weekly_human": "-",
        "claude_effective_pct": min(fh_avail, sd_avail),
        "claude_status": "Available" if min(fh_avail, sd_avail) > 0 else "Limit Reached",
        "claude_wait_human": fh_human if fh_avail == 0 else (sd_human if sd_avail == 0 else "ready"),
        "claude_5h_pct": fh_avail,
        "claude_5h_disabled": False,
        "claude_weekly_pct": sd_avail,
        "claude_weekly_reset": sd_res,
        "claude_weekly_human": sd_human,
        "error": None,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        save_quota_to_db(res)
    except Exception:
        pass
    return res

def fetch_codex_quota(profile: str) -> Dict[str, Any]:
    res = {
        "profile": profile,
        "logged_in": True,
        "gemini_effective_pct": 100,
        "gemini_status": "Available",
        "gemini_wait_human": "ready",
        "gemini_5h_pct": 100,
        "gemini_5h_disabled": False,
        "gemini_5h_reset": None,
        "gemini_5h_human": "ready",
        "gemini_weekly_pct": 100,
        "gemini_weekly_reset": None,
        "gemini_weekly_human": "ready",
        "claude_effective_pct": 100,
        "claude_status": "Available",
        "claude_wait_human": "ready",
        "claude_5h_pct": 100,
        "claude_5h_disabled": False,
        "claude_weekly_pct": 100,
        "claude_weekly_reset": None,
        "claude_weekly_human": "ready",
        "error": None,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        save_quota_to_db(res)
    except Exception:
        pass
    return res

def fetch_profile_quota(profile: str) -> Dict[str, Any]:
    if not validate_profile_name(profile) or not is_profile_logged_in(profile):
        return {"logged_in": False, "profile": profile}

    if profile == "claude" or profile.startswith("claude-"):
        return fetch_claude_quota(profile)
    if profile == "codex" or profile.startswith("codex-"):
        return fetch_codex_quota(profile)

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
