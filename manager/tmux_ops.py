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
        is_profile_reserved,
        get_lock_status,
        stop_conversation,
        unlock_stale_lock,
        kill_process_tree,
        get_process_descendants,
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
        is_profile_reserved,
        get_lock_status,
        stop_conversation,
        unlock_stale_lock,
        kill_process_tree,
        get_process_descendants,
        AGY_BIN,
        check_concurrent_account_conflict,
    )
    from db import log_audit

def get_tmux_session_name(profile: str, conversation_uuid: Optional[str] = None) -> str:
    if profile in ("bash", "claude", "codex"):
        return f"agy-{profile}"
    if conversation_uuid and validate_uuid(conversation_uuid):
        return f"agy-{profile}-{conversation_uuid[:8]}"
    return f"agy-{profile}"

def list_profile_sessions(profile: str) -> List[str]:
    """
    Returns all tmux sessions belonging to a specific profile,
    e.g. agy-account-02, agy-account-02-28a61098, etc.
    """
    all_sess = list_agy_tmux_sessions()
    prefix = f"agy-{profile}"
    matched = []
    for s in all_sess:
        if s == prefix or s.startswith(f"{prefix}-"):
            matched.append(s)
    return matched

def find_available_session_name(profile: str, conversation_uuid: Optional[str] = None) -> str:
    if profile in ("bash", "claude", "codex"):
        return f"agy-{profile}"
    if conversation_uuid and validate_uuid(conversation_uuid):
        return f"agy-{profile}-{conversation_uuid[:8]}"
    base = f"agy-{profile}"
    if not has_tmux_session(base):
        return base
    i = 2
    while has_tmux_session(f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"

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
    if profile.lower() in ("any", "auto", "best", "konsola", "default"):
        try:
            from . import pool_router
        except Exception:
            import pool_router
        profile, _ = pool_router.get_best_profile()

    if is_profile_reserved(profile):
        raise RuntimeError(f"Profil '{profile}' jest zarezerwowany dla silnika Pawła i nie może być używany do sesji użytkownika ani w puli ogólnej!")
    if not validate_profile_name(profile):
        raise ValueError(f"Invalid profile name: {profile}")
    if not is_profile_logged_in(profile):
        raise RuntimeError(f"Profile {profile} is not logged in!")

    conflict = check_concurrent_account_conflict(profile)
    if conflict:
        conf_p, email = conflict
        raise RuntimeError(
            f"Account conflict: Profile '{profile}' shares Google account '{email}' with currently running '{conf_p}'. "
            f"Concurrent sessions across duplicate profile configurations are blocked to prevent quota contamination."
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
        session_name = get_tmux_session_name(profile, conversation_uuid)
    else:
        session_name = find_available_session_name(profile)

    if profile == "bash":
        run_cmd = ["bash", "-l"]
    elif profile == "claude":
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

    # Only kill this specific session if it already exists (do NOT touch other sessions of this profile)
    if has_tmux_session(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)

    # Create session
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", workspace_dir] + run_cmd,
        check=True
    )

    # Dynamic auto-resize to client
    subprocess.run(["tmux", "set-option", "-t", session_name, "window-size", "latest"], check=False)
    subprocess.run(["tmux", "set-window-option", "-t", session_name, "aggressive-resize", "on"], check=False)

    # Disable tmux status bar and pane border so application gets 100% of terminal rows cleanly
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "status", "off"],
        check=False
    )
    subprocess.run(
        ["tmux", "set-window-option", "-t", session_name, "pane-border-status", "top"],
        check=False
    )
    subprocess.run(
        ["tmux", "set-option", "-s", "terminal-overrides", "xterm*:csr@:il@:il1@:dl@:dl1@:rin@:indn@"],
        check=False
    )
    # Keep tmux mouse enabled for mouse wheel history scrolling
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "mouse", "on"],
        check=False
    )
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "focus-events", "off"],
        check=False
    )
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "set-clipboard", "off"],
        check=False
    )
    # Never allow tmux to capture mouse dragging or select text without Shift
    subprocess.run(["tmux", "unbind-key", "-T", "root", "MouseDrag1Pane"], check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode", "MouseDrag1Pane"], check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode", "MouseDragEnd1Pane"], check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode-vi", "MouseDrag1Pane"], check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode-vi", "MouseDragEnd1Pane"], check=False)
    # Clicking in copy mode immediately cancels copy mode and returns to normal prompt
    subprocess.run(["tmux", "bind-key", "-T", "copy-mode", "MouseDown1Pane", "send-keys", "-X", "cancel"], check=False)
    subprocess.run(["tmux", "bind-key", "-T", "copy-mode-vi", "MouseDown1Pane", "send-keys", "-X", "cancel"], check=False)
    subprocess.run(
        ["tmux", "set-window-option", "-t", session_name, "mode-style", "bg=colour237,fg=colour111"],
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

def stop_tmux_session_cleanly(session_name: str, force: bool = False) -> bool:
    """
    Terminates all child processes inside a tmux session before destroying it,
    preventing orphaned processes from hanging on futexes/locks in the background.
    """
    if not has_tmux_session(session_name):
        return True

    # 1. Collect all pane PIDs
    pane_pids: List[int] = []
    try:
        tres = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if tres.returncode == 0:
            for line in tres.stdout.splitlines():
                if line.strip().isdigit():
                    pane_pids.append(int(line.strip()))
    except Exception:
        pass

    # 2. Collect all descendant processes inside each pane
    target_pids: List[int] = []
    for pp in pane_pids:
        target_pids.extend(get_process_descendants(pp))

    # 3. If not forced, try polite interrupt first
    if not force:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], check=False)
        time.sleep(0.4)

    # 4. Decisively terminate process trees
    for p in target_pids:
        kill_process_tree(p, timeout_sec=2.0, force=force)

    # 5. Destroy the tmux session
    subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
    log_audit("STOP", details=f"Tmux session {session_name} cleanly terminated (force={force})")
    return True

def switch_conversation(
    target_profile: str,
    conversation_uuid: str,
    workspace_dir: str = "/srv/projects/agy",
    model: Optional[str] = None
) -> Dict[str, Any]:
    if target_profile.lower() in ("any", "auto", "best", "konsola", "default"):
        try:
            from . import pool_router
        except Exception:
            import pool_router
        target_profile, _ = pool_router.get_best_profile()

    if is_profile_reserved(target_profile):
        raise RuntimeError(f"Profil '{target_profile}' jest zarezerwowany dla silnika Pawła i nie może być używany do przełączania sesji!")
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
        # Stop existing conversation
        stop_conversation(conversation_uuid, timeout_sec=4, force=False)

    # Step 2: Ensure lock is released (escalate with force if hung on futex)
    time.sleep(0.3)
    lock_after = get_lock_status(conversation_uuid)
    if lock_after.get("locked"):
        log_audit("SWITCH", profile=target_profile, conversation_uuid=conversation_uuid,
                  details="Lock still held, escalating to forceful stop and unlock")
        stop_conversation(conversation_uuid, timeout_sec=2, force=True)
        unlock_stale_lock(conversation_uuid, force=True)

        lock_final = get_lock_status(conversation_uuid)
        if lock_final.get("locked"):
            raise RuntimeError(f"Failed to release lock on {conversation_uuid} before switch! Process may be unkillable.")

    # Step 3: Start conversation on target profile
    res = start_profile_session(
        profile=target_profile,
        conversation_uuid=conversation_uuid,
        workspace_dir=workspace_dir,
        model=model
    )
    log_audit("SWITCH", profile=target_profile, conversation_uuid=conversation_uuid, details="Switched successfully")
    return res

def stop_session(profile_or_uuid: str, force: bool = False) -> bool:
    # 1. If it's a UUID:
    if validate_uuid(profile_or_uuid):
        return stop_conversation(profile_or_uuid, force=force)

    # 2. If it's a specific tmux session name (e.g. agy-account-02-28a61098 or agy-account-02)
    if profile_or_uuid.startswith("agy-") and has_tmux_session(profile_or_uuid):
        return stop_tmux_session_cleanly(profile_or_uuid, force=force)

    # 3. If it's a profile name (e.g. account-02)
    if validate_profile_name(profile_or_uuid):
        sessions = list_profile_sessions(profile_or_uuid)
        stopped_any = False
        for s in sessions:
            if stop_tmux_session_cleanly(s, force=force):
                stopped_any = True
        return stopped_any or len(sessions) == 0

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
