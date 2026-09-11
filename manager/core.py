import os
import re
import fcntl
import glob
import time
import signal
import sqlite3
import subprocess
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

def list_all_profiles() -> List[str]:
    if not os.path.isdir(PROFILES_DIR):
        return []
    profiles = [d for d in os.listdir(PROFILES_DIR) if os.path.isdir(os.path.join(PROFILES_DIR, d))]
    profiles.sort()
    return profiles

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
            "title": f"Conversation {uuid_str[:8]}",
            "preview": "",
            "steps": 0,
            "last_modified": mtime,
            "workspace": ""
        })
        meta["size_bytes"] = size
        meta["has_wal"] = wal_exists
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
