import subprocess
import time
import os
from typing import Optional, List, Dict, Any
try:
    from .core import (
        validate_profile_name,
        validate_uuid,
        get_profile_home,
        is_profile_logged_in,
        get_lock_status,
        stop_conversation,
        AGY_BIN,
        check_concurrent_account_conflict,
    )
    from .db import log_audit
except Exception:
    from core import (
        validate_profile_name,
        validate_uuid,
        get_profile_home,
        is_profile_logged_in,
        get_lock_status,
        stop_conversation,
        AGY_BIN,
        check_concurrent_account_conflict,
    )
    from db import log_audit

def get_tmux_session_name(profile: str) -> str:
    return f"agy-{profile}"

def has_tmux_session(session_name: str) -> bool:
    res = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return res.returncode == 0

def list_agy_tmux_sessions() -> List[str]:
    res = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        return []
    sessions = []
    for line in res.stdout.splitlines():
        name = line.strip()
        if name.startswith("agy-"):
            sessions.append(name)
    return sessions

def capture_tmux_pane(session_name: str, lines: int = 50) -> str:
    res = subprocess.run(
        ["tmux", "capture-pane", "-pt", session_name, "-S", f"-{lines}"],
        capture_output=True,
        text=True
    )
    if res.returncode == 0:
        return res.stdout
    return ""

def start_profile_session(
    profile: str,
    conversation_uuid: Optional[str] = None,
    workspace_dir: str = "/srv/projects/agy",
    model: Optional[str] = None
) -> Dict[str, Any]:
    if not validate_profile_name(profile):
        raise ValueError(f"Invalid profile name: {profile}")
    if not is_profile_logged_in(profile):
        raise RuntimeError(f"Profile {profile} is not logged in!")

    conflict = check_concurrent_account_conflict(profile)
    if conflict:
        conf_p, email = conflict
        raise RuntimeError(
            f"Account conflict: Profile '{profile}' shares Google account '{email}' with currently running '{conf_p}'. "
            f"Concurrent sessions on the same Google account are blocked to prevent rate-limit exhaustion and session contamination."
        )

    if conversation_uuid:
        if not validate_uuid(conversation_uuid):
            raise ValueError(f"Invalid UUID: {conversation_uuid}")
        # Check lock
        lock_info = get_lock_status(conversation_uuid)
        if lock_info.get("locked"):
            raise RuntimeError(
                f"Conversation {conversation_uuid} is currently locked by PID {lock_info.get('pid')}!"
            )

    session_name = get_tmux_session_name(profile)

    if profile == "claude":
        run_cmd = ["bash", "-c", "HOME=/home/kacper PATH=/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin claude"]
    elif profile == "codex":
        run_cmd = ["bash", "-c", "HOME=/home/kacper PATH=/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin codex"]
    else:
        home_dir = get_profile_home(profile)
        cmd = [f"HOME={home_dir}", f"PATH=/home/kacper/.local/bin:$PATH", AGY_BIN]
        if conversation_uuid:
            cmd.extend(["--conversation", conversation_uuid])
        if model:
            cmd.extend(["--model", model])
        run_cmd = ["bash", "-c", " ".join(cmd)]

    # If session already exists, kill it cleanly or replace window
    if has_tmux_session(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)

    # Create session
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", workspace_dir] + run_cmd,
        check=True
    )

    # Configure status bar for clarity
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "status-left", f"[{profile}] "],
        check=False
    )
    title = f"{profile} | {conversation_uuid or 'NEW'}"
    subprocess.run(
        ["tmux", "rename-window", "-t", session_name, title],
        check=False
    )

    time.sleep(1)
    log_audit("START", profile=profile, conversation_uuid=conversation_uuid, details=f"Started in tmux {session_name}")

    return {
        "status": "started",
        "profile": profile,
        "session_name": session_name,
        "conversation_uuid": conversation_uuid,
        "workspace_dir": workspace_dir
    }

def switch_conversation(
    target_profile: str,
    conversation_uuid: str,
    workspace_dir: str = "/srv/projects/agy",
    model: Optional[str] = None
) -> Dict[str, Any]:
    if not validate_profile_name(target_profile):
        raise ValueError(f"Invalid target profile: {target_profile}")
    if not validate_uuid(conversation_uuid):
        raise ValueError(f"Invalid UUID: {conversation_uuid}")
    if not is_profile_logged_in(target_profile):
        raise RuntimeError(f"Target profile {target_profile} is not logged in!")

    # Step 1: Check if conversation is locked by any active process
    lock_info = get_lock_status(conversation_uuid)
    if lock_info.get("locked"):
        pid = lock_info.get("pid")
        log_audit("SWITCH", profile=target_profile, conversation_uuid=conversation_uuid,
                  details=f"Stopping existing instance (PID {pid})")
        # Gracefully stop the conversation
        stop_conversation(conversation_uuid, timeout_sec=10)

    # Step 2: Ensure lock is released
    time.sleep(0.5)
    lock_after = get_lock_status(conversation_uuid)
    if lock_after.get("locked"):
        raise RuntimeError(f"Failed to release lock on {conversation_uuid} before switch!")

    # Step 3: Start conversation on target profile
    res = start_profile_session(
        profile=target_profile,
        conversation_uuid=conversation_uuid,
        workspace_dir=workspace_dir,
        model=model
    )
    log_audit("SWITCH", profile=target_profile, conversation_uuid=conversation_uuid, details="Switched successfully")
    return res

def stop_session(profile_or_uuid: str) -> bool:
    # If it's a UUID:
    if validate_uuid(profile_or_uuid):
        return stop_conversation(profile_or_uuid)

    # Otherwise treat as profile name
    if validate_profile_name(profile_or_uuid):
        session_name = get_tmux_session_name(profile_or_uuid)
        if has_tmux_session(session_name):
            subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], check=False)
            time.sleep(1)
            subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
            log_audit("STOP", profile=profile_or_uuid, details=f"Tmux session {session_name} killed")
            return True
    return False

def create_grid_session(profiles: List[str]) -> str:
    """
    Creates a 2x2 grid tmux session to monitor up to 4 profiles simultaneously.
    """
    grid_session = "agy-grid"
    if has_tmux_session(grid_session):
        subprocess.run(["tmux", "kill-session", "-t", grid_session], check=False)

    valid_profiles = [p for p in profiles if validate_profile_name(p)][:4]
    if not valid_profiles:
        raise ValueError("At least one profile must be provided for grid view")

    first = valid_profiles[0]
    home_first = get_profile_home(first)
    first_cmd = f"HOME={home_first} PATH=/home/kacper/.local/bin:$PATH {AGY_BIN}"
    subprocess.run(["tmux", "new-session", "-d", "-s", grid_session, first_cmd], check=True)

    for p in valid_profiles[1:]:
        p_home = get_profile_home(p)
        p_cmd = f"HOME={p_home} PATH=/home/kacper/.local/bin:$PATH {AGY_BIN}"
        subprocess.run(["tmux", "split-window", "-t", grid_session, p_cmd], check=True)
        subprocess.run(["tmux", "select-layout", "-t", grid_session, "tiled"], check=True)

    return grid_session
