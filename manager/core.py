import os
import re
import fcntl
import glob
import time
import signal
import sqlite3
import subprocess
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

try:
    from .db import get_connection, log_audit
except Exception:
    from db import get_connection, log_audit

BASE_DIR = "/srv/agy-manager"
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
SHARED_DIR = os.path.join(BASE_DIR, "shared")
PRESENCE_DIR = os.path.join(SHARED_DIR, "presence")
CONVERSATIONS_DIR = os.path.join(SHARED_DIR, "conversations")
AGY_BIN = "/home/kacper/.local/bin/agy"

PROFILE_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")
UUID_REGEX = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

def validate_profile_name(profile: str) -> bool:
    return bool(PROFILE_REGEX.match(profile))

def validate_uuid(uuid_str: str) -> bool:
    return bool(UUID_REGEX.match(uuid_str))

def get_profile_home(profile: str) -> str:
    if not validate_profile_name(profile):
        raise ValueError(f"Invalid profile name: {profile}")
    return os.path.join(PROFILES_DIR, profile, "home")

def get_profile_token_path(profile: str) -> str:
    home = get_profile_home(profile)
    return os.path.join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token")

def is_profile_logged_in(profile: str) -> bool:
    token_path = get_profile_token_path(profile)
    return os.path.isfile(token_path) and os.path.getsize(token_path) > 50

def get_profile_email(profile: str) -> Optional[str]:
    token_path = get_profile_token_path(profile)
    if not os.path.isfile(token_path):
        return None
    try:
        import base64
        import json
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        id_token = data.get("id_token")
        if id_token:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8"))
                return payload.get("email")
    except Exception:
        pass
    return None

def list_all_profiles() -> List[str]:
    if not os.path.isdir(PROFILES_DIR):
        return []
    profiles = [d for d in os.listdir(PROFILES_DIR) if os.path.isdir(os.path.join(PROFILES_DIR, d))]
    profiles.sort()
    return profiles

def get_duplicate_accounts() -> Dict[str, List[str]]:
    """
    Returns mapping of {email: [profile1, profile2]} for emails used across multiple profiles.
    """
    email_map: Dict[str, List[str]] = {}
    for p in list_all_profiles():
        if is_profile_logged_in(p):
            email = get_profile_email(p)
            if email:
                email_map.setdefault(email, []).append(p)
    return {email: plist for email, plist in email_map.items() if len(plist) > 1}

def check_concurrent_account_conflict(target_profile: str) -> Optional[Tuple[str, str]]:
    """
    Checks if another profile sharing the same Google account email is currently running.
    Returns (conflicting_profile, email) if a conflict is found, else None.
    """
    target_email = get_profile_email(target_profile)
    if not target_email:
        return None

    active_sessions = get_active_sessions()
    for s in active_sessions:
        active_p = s.get("profile")
        if active_p and active_p != target_profile and is_profile_logged_in(active_p):
            other_email = get_profile_email(active_p)
            if other_email and other_email.lower() == target_email.lower():
                return (active_p, target_email)
    return None

def logout_profile(profile: str) -> bool:
    """
    Logs out a profile by removing its token and stopping any active session.
    """
    if not validate_profile_name(profile):
        raise ValueError(f"Invalid profile name: {profile}")

    # Stop tmux session if running
    sess_name = f"agy-{profile}"
    try:
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False, capture_output=True)
    except Exception:
        pass

    token_path = get_profile_token_path(profile)
    if os.path.isfile(token_path):
        try:
            os.remove(token_path)
        except Exception:
            pass

    # Clear cached quota
    try:
        conn = get_connection()
        with conn:
            conn.execute("DELETE FROM profile_quotas WHERE profile = ?;", (profile,))
        conn.close()
    except Exception:
        pass

    log_audit("LOGOUT", profile=profile, details="Profile logged out, token removed")
    return True

def get_lock_status(uuid_str: str) -> Dict[str, Any]:
    """
    Checks kernel flock and active process file descriptors for presence/<uuid>.lock
    """
    if not validate_uuid(uuid_str):
        raise ValueError(f"Invalid UUID: {uuid_str}")

    lock_file = os.path.join(PRESENCE_DIR, f"{uuid_str}.lock")
    if not os.path.exists(lock_file):
        return {"locked": False, "pid": None, "stale": False}

    # Test flock non-blocking
    try:
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Acquired flock, so nobody holds an exclusive lock!
            fcntl.flock(fd, fcntl.LOCK_UN)
            is_locked = False
        except BlockingIOError:
            is_locked = True
        finally:
            os.close(fd)
    except Exception:
        is_locked = True

    # Find PID holding this lock if locked
    holding_pid = None
    if is_locked:
        for pid_dir in glob.glob("/proc/[0-9]*"):
            pid_str = os.path.basename(pid_dir)
            fd_dir = os.path.join(pid_dir, "fd")
            if not os.path.isdir(fd_dir):
                continue
            try:
                for fd_name in os.listdir(fd_dir):
                    target = os.path.realpath(os.path.join(fd_dir, fd_name))
                    if target == lock_file:
                        holding_pid = int(pid_str)
                        break
            except (PermissionError, FileNotFoundError):
                continue
            if holding_pid:
                break

    # If is_locked is True but holding_pid is None, or vice versa
    stale = is_locked and (holding_pid is None)

    return {
        "locked": is_locked,
        "pid": holding_pid,
        "stale": stale,
        "lock_file": lock_file
    }

def get_active_sessions() -> List[Dict[str, Any]]:
    """
    Returns running agy processes per profile and their active conversation UUIDs.
    """
    active = []
    # Scan /proc for agy processes
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid_str = os.path.basename(pid_dir)
        try:
            with open(os.path.join(pid_dir, "cmdline"), "rb") as f:
                cmdline = f.read().split(b"\x00")
            cmd_args = [arg.decode("utf-8", errors="replace") for arg in cmdline if arg]
            if not cmd_args:
                continue
            if "agy" not in os.path.basename(cmd_args[0]):
                continue

            # Read environ to find HOME
            with open(os.path.join(pid_dir, "environ"), "rb") as f:
                env_raw = f.read().split(b"\x00")
            env_map = {}
            for e in env_raw:
                parts = e.decode("utf-8", errors="replace").split("=", 1)
                if len(parts) == 2:
                    env_map[parts[0]] = parts[1]

            home = env_map.get("HOME", "")
            profile_name = "unknown"
            if "/srv/agy-manager/profiles/" in home:
                profile_name = home.split("/srv/agy-manager/profiles/")[1].split("/")[0]

            # Find conversation UUID from cmdline or presence lock
            conv_uuid = None
            for arg in cmd_args:
                if arg.startswith("--conversation="):
                    conv_uuid = arg.split("=", 1)[1]
                elif arg == "--conversation" and cmd_args.index(arg) + 1 < len(cmd_args):
                    conv_uuid = cmd_args[cmd_args.index(arg) + 1]

            if not conv_uuid:
                # Check fd directory for lock file
                fd_dir = os.path.join(pid_dir, "fd")
                if os.path.isdir(fd_dir):
                    for fd_name in os.listdir(fd_dir):
                        try:
                            tgt = os.path.realpath(os.path.join(fd_dir, fd_name))
                            if tgt.startswith(PRESENCE_DIR) and tgt.endswith(".lock"):
                                conv_uuid = os.path.basename(tgt)[:-5]
                                break
                        except Exception:
                            pass

            active.append({
                "pid": int(pid_str),
                "profile": profile_name,
                "conversation_uuid": conv_uuid,
                "cmdline": " ".join(cmd_args),
                "home": home
            })
        except (PermissionError, FileNotFoundError):
            continue
    return active

def get_profile_models(profile: str) -> List[Tuple[str, str]]:
    if not validate_profile_name(profile):
        raise ValueError("Invalid profile name")
    if not is_profile_logged_in(profile):
        return []

    home = get_profile_home(profile)
    env = os.environ.copy()
    env["HOME"] = home
    env["PATH"] = f"/home/kacper/.local/bin:{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            [AGY_BIN, "models"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15
        )
        if proc.returncode != 0:
            return []

        models = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or "Fetching available models" in line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                models.append((parts[0].strip(), parts[1].strip()))
            elif len(parts) == 1:
                models.append((parts[0].strip(), parts[0].strip()))
        return models
    except Exception as e:
        return []

TAILSCALE_MAP: Dict[str, str] = {
    "100.70.110.7": "ferrari",
    "100.116.4.83": "audi",
    "100.69.214.41": "porsche",
    "100.78.225.98": "home",
    "100.90.204.127": "maluch",
    "100.102.222.75": "kacper",
    "100.117.157.103": "hp-czsk",
    "100.123.209.32": "iphone",
    "100.74.216.93": "tablet",
    "127.0.0.1": "ferrari",
    "::1": "ferrari",
}

def resolve_machine(client_ip: Optional[str] = None) -> str:
    """
    Resolves client machine name based on client IP or SSH session.
    """
    if not client_ip:
        ssh_conn = os.environ.get("SSH_CLIENT", "") or os.environ.get("SSH_CONNECTION", "")
        if ssh_conn:
            client_ip = ssh_conn.split()[0]

    if not client_ip:
        return "ferrari"

    client_ip = client_ip.strip()
    if client_ip in TAILSCALE_MAP:
        return TAILSCALE_MAP[client_ip]

    # Try dynamic lookup via tailscale status
    try:
        proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            for peer in data.get("Peer", {}).values():
                ips = peer.get("TailscaleIPs", [])
                if client_ip in ips:
                    hname = peer.get("HostName", "").lower()
                    if hname:
                        TAILSCALE_MAP[client_ip] = hname
                        return hname
    except Exception:
        pass

    return "ferrari"

def record_conversation_machine(uuid_str: str, machine: str, client_ip: str = ""):
    """
    Records which machine was used for a conversation.
    """
    if not uuid_str:
        return
    try:
        conn = get_connection()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_machines (
                    conversation_uuid TEXT PRIMARY KEY,
                    machine TEXT NOT NULL,
                    client_ip TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                INSERT INTO conversation_machines (conversation_uuid, machine, client_ip, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(conversation_uuid) DO UPDATE SET
                    machine=excluded.machine,
                    client_ip=excluded.client_ip,
                    updated_at=CURRENT_TIMESTAMP;
            """, (uuid_str, machine, client_ip))
        conn.close()
    except Exception:
        pass

def get_conversation_machines() -> Dict[str, str]:
    """
    Retrieves all recorded machine mappings for conversations.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS conversation_machines (conversation_uuid TEXT PRIMARY KEY, machine TEXT NOT NULL, client_ip TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);")
        cursor.execute("SELECT conversation_uuid, machine FROM conversation_machines;")
        rows = cursor.fetchall()
        conn.close()
        return {r["conversation_uuid"]: r["machine"] for r in rows}
    except Exception:
        return {}

def list_conversations(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Lists shared conversations from conversation_summaries.db and filesystem.
    """
    summaries_db = os.path.join(SHARED_DIR, "conversation_summaries.db")
    results = {}

    if os.path.isfile(summaries_db):
        try:
            conn = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("""
            SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris
            FROM conversation_summaries
            ORDER BY last_modified_time DESC
            LIMIT ?;
            """, (limit,))
            for row in cursor.fetchall():
                results[row[0]] = {
                    "uuid": row[0],
                    "title": row[1] or "Untitled Conversation",
                    "preview": row[2] or "",
                    "steps": row[3],
                    "last_modified": row[4],
                    "workspace": row[5] or ""
                }
            conn.close()
        except Exception:
            pass

    # Load workspace mappings from history.jsonl and cache
    history_ws = {}
    for h_path in [os.path.join(SHARED_DIR, "history.jsonl"), "/home/kacper/.gemini/antigravity-cli/history.jsonl"]:
        if os.path.isfile(h_path):
            try:
                with open(h_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                            cid = d.get("conversationId")
                            ws = d.get("workspace")
                            if cid and ws:
                                history_ws[cid] = ws
                        except Exception:
                            pass
            except Exception:
                pass

    # Load recorded machine mappings
    conv_machines = get_conversation_machines()

    # Merge with actual files on disk in case some are not indexed in summaries
    db_files = glob.glob(os.path.join(CONVERSATIONS_DIR, "*.db"))
    db_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    final_list = []
    seen = set()
    for db_path in db_files[:limit]:
        uuid_str = os.path.splitext(os.path.basename(db_path))[0]
        seen.add(uuid_str)
        mtime = datetime.fromtimestamp(os.path.getmtime(db_path)).isoformat()
        size = os.path.getsize(db_path)
        wal_exists = os.path.exists(f"{db_path}-wal")

        meta = results.get(uuid_str, {
            "uuid": uuid_str,
            "title": f"Conversation {uuid_str[-5:]}",
            "preview": "",
            "steps": 0,
            "last_modified": mtime,
            "workspace": ""
        })

        # Resolve workspace if empty
        ws_val = meta.get("workspace") or history_ws.get(uuid_str) or ""
        if not ws_val or ws_val == '""' or ws_val == "[]":
            # Check transcript
            tr_file = os.path.join(SHARED_DIR, "brain", uuid_str, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(tr_file):
                try:
                    with open(tr_file, "r", encoding="utf-8", errors="replace") as f:
                        tr_chunk = f.read(8192)
                        matches = re.findall(r"/(?:srv/projects|home/[a-zA-Z0-9_-]+)/[a-zA-Z0-9_.-]+", tr_chunk)
                        if matches:
                            ws_val = matches[0]
                except Exception:
                    pass
        
        # Clean up file:// prefix or JSON formatting
        if ws_val:
            ws_val = ws_val.replace("file://", "").strip("[]\"' ")

        meta["workspace"] = ws_val or "-"
        meta["size_bytes"] = size
        meta["has_wal"] = wal_exists
        meta["machine"] = conv_machines.get(uuid_str) or "ferrari"
        final_list.append(meta)

    return final_list

def stop_conversation(uuid_str: str, timeout_sec: int = 10) -> bool:
    """
    Gracefully stops the process holding the lock for uuid_str.
    Waits for process termination and WAL file closure.
    """
    if not validate_uuid(uuid_str):
        raise ValueError("Invalid UUID format")

    status = get_lock_status(uuid_str)
    pid = status.get("pid")
    if not pid:
        # Check active sessions in case PID detection via lock failed
        for s in get_active_sessions():
            if s.get("conversation_uuid") == uuid_str:
                pid = s.get("pid")
                break

    if not pid:
        log_audit("STOP", details=f"No active PID found for UUID {uuid_str}")
        return True

    log_audit("STOP", conversation_uuid=uuid_str, details=f"Sending SIGINT to PID {pid}")
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return True

    # Wait for process to exit
    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        try:
            # Check if PID still exists
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            break

    # If still alive after timeout, send SIGTERM
    try:
        os.kill(pid, 0)
        log_audit("STOP", conversation_uuid=uuid_str, details=f"Sending SIGTERM to PID {pid}")
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
    except ProcessLookupError:
        pass

    # Wait until WAL and SHM files are committed
    wal_file = os.path.join(CONVERSATIONS_DIR, f"{uuid_str}.db-wal")
    wal_wait_start = time.time()
    while os.path.exists(wal_file) and time.time() - wal_wait_start < 5:
        time.sleep(0.5)

    log_audit("STOP", conversation_uuid=uuid_str, details=f"Conversation {uuid_str} stopped successfully")
    return True

def unlock_stale_lock(uuid_str: str) -> bool:
    if not validate_uuid(uuid_str):
        raise ValueError("Invalid UUID")
    status = get_lock_status(uuid_str)
    if not status.get("locked"):
        return True
    if status.get("pid") is not None:
        raise RuntimeError(f"Cannot unlock: active process PID {status['pid']} is still running!")

    lock_file = status.get("lock_file")
    if lock_file and os.path.exists(lock_file):
        os.remove(lock_file)
        log_audit("UNLOCK", conversation_uuid=uuid_str, details="Stale lock removed")
        return True
    return False

def update_conversation_summaries() -> Dict[str, int]:
    """
    Extracts real titles, user prompts, steps and timestamps from brain/ and history.jsonl,
    and updates /srv/agy-manager/shared/conversation_summaries.db.
    """
    brain_dir = os.path.join(SHARED_DIR, "brain")
    hist_file = os.path.join(SHARED_DIR, "history.jsonl")
    conv_dir = os.path.join(SHARED_DIR, "conversations")
    summaries_db = os.path.join(SHARED_DIR, "conversation_summaries.db")

    hist_map = {}
    if os.path.exists(hist_file):
        try:
            with open(hist_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line.strip())
                        cid = d.get("conversationId")
                        disp = d.get("display", "").strip()
                        ws = d.get("workspace", "").strip()
                        if cid and disp and cid not in hist_map:
                            hist_map[cid] = {"title": disp[:80], "workspace": ws}
                    except Exception:
                        pass
        except Exception:
            pass

    trans_map = {}
    for p in glob.glob(os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript.jsonl")):
        cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(p))))
        title = ""
        preview = ""
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                step_count = 0
                for line in f:
                    try:
                        d = json.loads(line.strip())
                        step_count += 1
                        if not title:
                            content = d.get("content", "")
                            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                            if m:
                                clean = m.group(1).strip().replace("\n", " ")
                                title = clean[:80]
                                preview = clean[:200]
                            elif d.get("source") == "USER_EXPLICIT" and content:
                                clean = content.strip().replace("\n", " ")
                                title = clean[:80]
                                preview = clean[:200]
                    except Exception:
                        pass
                trans_map[cid] = {"title": title, "preview": preview, "steps": step_count}
        except Exception:
            pass

    conn = sqlite3.connect(summaries_db)
    cur = conn.cursor()
    cur.execute("SELECT conversation_id FROM conversation_summaries;")
    existing = {r[0] for r in cur.fetchall()}

    conv_files = glob.glob(os.path.join(conv_dir, "*.db"))
    inserted = 0
    updated = 0

    for cf in conv_files:
        cid = os.path.splitext(os.path.basename(cf))[0]
        mtime = datetime.fromtimestamp(os.path.getmtime(cf)).isoformat()
        t_info = trans_map.get(cid, {})
        h_info = hist_map.get(cid, {})
        title = h_info.get("title") or t_info.get("title") or f"Conversation {cid[:8]}"
        preview = t_info.get("preview") or title
        steps = t_info.get("steps", 1)
        workspace = h_info.get("workspace", "")

        if cid in existing:
            cur.execute("""
                UPDATE conversation_summaries 
                SET title = ?, preview = ?, step_count = ?, last_modified_time = ?, workspace_uris = ?
                WHERE conversation_id = ?;
            """, (title, preview, steps, mtime, workspace, cid))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO conversation_summaries (
                    conversation_id, title, preview, step_count, last_modified_time, workspace_uris,
                    status, source, project_id, agent_name, parent_conversation_id, nesting_depth,
                    battle_id, winning_conversation_id, not_fully_idle, killed, last_user_input_time,
                    last_user_input_step_index, app_data_dir, group_id
                ) VALUES (?, ?, ?, ?, ?, ?, "", "", "default-cli-project", "", "", 0, "", "", 0, 0, ?, 0, "antigravity-cli", "");
            """, (cid, title, preview, steps, mtime, workspace, mtime))
            inserted += 1

    conn.commit()
    conn.close()
    return {"inserted": inserted, "updated": updated}

def sync_sessions_from_home(source_home: str = "/home/kacper") -> Dict[str, Any]:
    """
    Safely synchronizes session data (conversations, brain, annotations, cache, history)
    from source_home to /srv/agy-manager/shared, preserving SQLite transactional consistency
    via online backup API without corrupting active WAL files.
    """
    src_cli = os.path.join(source_home, ".gemini", "antigravity-cli")
    if not os.path.isdir(src_cli):
        raise ValueError(f"Source antigravity-cli directory not found: {src_cli}")

    synced_conv = 0
    # 1. Sync SQLite conversations
    src_conv = os.path.join(src_cli, "conversations")
    dst_conv = os.path.join(SHARED_DIR, "conversations")
    os.makedirs(dst_conv, exist_ok=True)

    for sf in glob.glob(os.path.join(src_conv, "*.db")):
        fname = os.path.basename(sf)
        df = os.path.join(dst_conv, fname)
        need_sync = False
        if not os.path.exists(df):
            need_sync = True
        else:
            if os.path.getmtime(sf) > os.path.getmtime(df) or os.path.getsize(sf) != os.path.getsize(df):
                need_sync = True

        if need_sync:
            tmp_df = df + ".synctmp"
            try:
                src_conn = sqlite3.connect(f"file:{sf}?mode=ro", uri=True)
                dst_conn = sqlite3.connect(tmp_df)
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
                os.utime(tmp_df, (os.path.getatime(sf), os.path.getmtime(sf)))
                os.replace(tmp_df, df)
                synced_conv += 1
            except Exception as e:
                if os.path.exists(tmp_df):
                    os.remove(tmp_df)

    # 2. Sync Brain files
    p_brain = subprocess.run(
        ["rsync", "-au", f"{src_cli}/brain/", f"{SHARED_DIR}/brain/"],
        capture_output=True, text=True
    )

    # 3. Sync Annotations
    p_ann = subprocess.run(
        ["rsync", "-au", f"{src_cli}/annotations/", f"{SHARED_DIR}/annotations/"],
        capture_output=True, text=True
    )

    # 4. Sync Cache
    src_last_conv = os.path.join(src_cli, "cache", "last_conversations.json")
    dst_last_conv = os.path.join(SHARED_DIR, "cache", "last_conversations.json")
    if os.path.exists(src_last_conv):
        try:
            with open(src_last_conv, "r", encoding="utf-8") as f:
                src_data = json.load(f)
            dst_data = {}
            if os.path.exists(dst_last_conv):
                with open(dst_last_conv, "r", encoding="utf-8") as f:
                    dst_data = json.load(f)
            merged = {**dst_data, **src_data}
            with open(dst_last_conv + ".tmp", "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            os.replace(dst_last_conv + ".tmp", dst_last_conv)
        except Exception:
            pass

    # 5. Sync History.jsonl
    src_hist = os.path.join(src_cli, "history.jsonl")
    dst_hist = os.path.join(SHARED_DIR, "history.jsonl")
    if os.path.exists(src_hist):
        seen_timestamps = set()
        lines = []
        if os.path.exists(dst_hist):
            with open(dst_hist, "r", encoding="utf-8", errors="replace") as f:
                for l in f:
                    l_str = l.strip()
                    if l_str:
                        try:
                            ts = json.loads(l_str).get("timestamp")
                            if ts: seen_timestamps.add(ts)
                        except Exception: pass
                        lines.append(l_str)
        with open(src_hist, "r", encoding="utf-8", errors="replace") as f:
            for l in f:
                l_str = l.strip()
                if l_str:
                    try:
                        ts = json.loads(l_str).get("timestamp")
                        if ts and ts in seen_timestamps:
                            continue
                        seen_timestamps.add(ts)
                    except Exception: pass
                    lines.append(l_str)
        with open(dst_hist + ".tmp", "w", encoding="utf-8") as f:
            for l in lines:
                f.write(l + "\n")
        os.replace(dst_hist + ".tmp", dst_hist)

    # 6. Update conversation summaries
    sum_res = update_conversation_summaries()

    log_audit("SYNC", details=f"Synchronized {synced_conv} DBs from {source_home}")
    return {
        "conversations_synced": synced_conv,
        "summaries_inserted": sum_res.get("inserted", 0),
        "summaries_updated": sum_res.get("updated", 0)
    }

