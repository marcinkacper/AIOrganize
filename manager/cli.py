#!/usr/bin/env python3
import sys
import os
import time
import re
import argparse
import subprocess
# Tabulate is not needed

# Ensure manager path is available
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import core
import tmux_ops
import monitor_klajner

def cmd_profiles(args):
    profiles = core.list_all_profiles()
    active_sessions = core.get_active_sessions()
    tmux_sessions = tmux_ops.list_agy_tmux_sessions()

    session_by_profile = {s.get("profile"): s for s in active_sessions}

    print(f"\n=== Antigravity CLI Profiles ({len(profiles)}) ===")
    print(f"{'PROFILE':<13} {'LOGGED IN':<11} {'EMAIL':<34} {'TMUX SESSION':<16} {'PID':<8} {'ACTIVE UUID':<38}")
    print("-" * 124)

    for p in profiles:
        is_logged = "YES" if core.is_profile_logged_in(p) else "NO"
        email = core.get_profile_email(p) or "-"
        s_info = session_by_profile.get(p)
        tmux_name = tmux_ops.get_tmux_session_name(p)
        tmux_active = tmux_name if tmux_name in tmux_sessions else "-"
        pid_str = str(s_info.get("pid")) if s_info else "-"
        uuid_str = s_info.get("conversation_uuid") or "-" if s_info else "-"

        print(f"{p:<13} {is_logged:<11} {email:<34} {tmux_active:<16} {pid_str:<8} {uuid_str:<38}")
    print()

def cmd_models(args):
    profile = args.profile
    if not core.validate_profile_name(profile):
        print(f"Error: Invalid profile name '{profile}'", file=sys.stderr)
        sys.exit(1)
    if not core.is_profile_logged_in(profile):
        print(f"Profile '{profile}' is not logged in. Run 'agy-manager login {profile}' first.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching models for {profile}...")
    models = core.get_profile_models(profile)
    if not models:
        print("No models returned or authorization error.")
        return

    print(f"\nAvailable models for {profile}:")
    for mid, desc in models:
        print(f"  {mid:<26} {desc}")
    print()

def cmd_conversations(args):
    limit = args.limit
    convs = core.list_conversations(limit=limit)
    print(f"\n=== Shared Conversations (Latest {len(convs)}) ===")
    print(f"{'UUID':<38} {'STEPS':<6} {'SIZE':<10} {'MODIFIED':<20} {'TITLE'}")
    print("-" * 110)

    for c in convs:
        size_str = f"{round(c.get('size_bytes', 0) / 1024, 1)} KB"
        mtime = str(c.get("last_modified", ""))[:19]
        title = (c.get("title") or "")[:40]
        steps = c.get("steps", 0)
        uuid_str = c.get("uuid", "")
        wal_mark = "*" if c.get("has_wal") else " "
        print(f"{uuid_str:<38} {steps:<6} {size_str:<10} {mtime:<20} {wal_mark}{title}")
    print("\n* indicates uncommitted WAL / active conversation")

def cmd_status(args):
    cmd_profiles(args)
    
    # Active Locks
    print("=== Active Conversations & Locks ===")
    active = core.get_active_sessions()
    if not active:
        print("No active conversation processes.")
    else:
        for a in active:
            print(f"PID {a['pid']} | Profile: {a['profile']} | UUID: {a['conversation_uuid']}")

    # Klajner status
    print("\n=== External Resource: Klajner AGY Engine ===")
    k_status = monitor_klajner.check_klajner_health()
    stat_str = "ONLINE (Healthy)" if k_status.get("online") else f"OFFLINE ({k_status.get('status')})"
    print(f"Status: {stat_str} | Latency: {k_status.get('response_time_ms')} ms")
    print(f"Engine: OAuth Session Active={k_status.get('oauth_active')} | Model: {k_status.get('default_model')}")
    if k_status.get("rate_limit_detected"):
        print(f"⚠️ Rate Limit / Quota Alert: {k_status.get('last_log_error')}")
    else:
        print("Limits: Normal (No rate limit / 429 detected)")
    print()

def cmd_sync(args):
    source = getattr(args, "source", "/home/kacper")
    print(f"\nSynchronizing session data from {source} to /srv/agy-manager/shared...")
    try:
        res = core.sync_sessions_from_home(source_home=source)
        print("✓ Synchronization complete:")
        print(f"  - Conversations synced/updated: {res.get('conversations_synced', 0)}")
        print(f"  - Summaries inserted: {res.get('summaries_inserted', 0)}")
        print(f"  - Summaries updated: {res.get('summaries_updated', 0)}")
    except Exception as e:
        print(f"Error during synchronization: {e}", file=sys.stderr)
        sys.exit(1)
    print()

def cmd_login(args):
    profile = args.profile
    if not core.validate_profile_name(profile):
        print(f"Error: Invalid profile name '{profile}'", file=sys.stderr)
        sys.exit(1)

    sess_name = f"agy-login-{profile}"
    home_dir = core.get_profile_home(profile)

    if tmux_ops.has_tmux_session(sess_name):
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)

    print(f"\nInitializing OAuth login for {profile}...")
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", sess_name, f"HOME={home_dir} PATH=/home/kacper/.local/bin:$PATH {core.AGY_BIN}"],
        check=True
    )
    time.sleep(1)
    subprocess.run(["tmux", "send-keys", "-t", sess_name, "Enter"], check=True)

    # Poll for clean URL
    url = None
    for _ in range(12):
        time.sleep(0.5)
        out = tmux_ops.capture_tmux_pane(sess_name, lines=100)
        if "https://accounts.google.com" in out:
            lines = out.splitlines()
            url_parts = []
            recording = False
            for line in lines:
                line_s = line.strip()
                if "https://accounts.google.com" in line_s:
                    recording = True
                if recording:
                    if "─" in line_s or "After authenticating" in line_s:
                        if url_parts:
                            break
                        else:
                            continue
                    url_parts.append(line_s)
            url = "".join(url_parts).replace(" ", "")
            break

    if not url:
        print("Error: Could not retrieve OAuth URL from CLI.", file=sys.stderr)
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)
        sys.exit(1)

    print("\nOtworz ponizszy link w przegladarce (calosc w jednej linii):")
    print("-" * 80)
    print(url)
    print("-" * 80)

    try:
        auth_code = input("\nWklej kod autoryzacyjny z przegladarki: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nOperacja przerwana.")
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)
        sys.exit(1)

    if not auth_code:
        print("Nie podano kodu. Logowanie przerwane.")
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)
        sys.exit(1)

    print("Zatwierdzanie kodu autoryzacyjnego...")
    subprocess.run(["tmux", "send-keys", "-t", sess_name, auth_code, "Enter"], check=True)
    time.sleep(3)

    # Check pane output
    final_out = tmux_ops.capture_tmux_pane(sess_name, lines=50)
    subprocess.run(["tmux", "send-keys", "-t", sess_name, "/exit", "Enter"], check=False)
    time.sleep(1)
    subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)

    if core.is_profile_logged_in(profile):
        email_match = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", final_out)
        email_str = f" ({email_match.group(1)})" if email_match else ""
        print(f"\n✓ Sukces: Profil {profile}{email_str} zostal pomyslnie zalogowany!")
    else:
        print(f"\nNie udalo sie zalogowac profilu {profile}. Sprobuj ponownie.")

def cmd_start(args):
    profile = args.profile
    uuid_str = args.uuid
    model = args.model
    workdir = args.dir or "/srv/projects/agy"

    try:
        res = tmux_ops.start_profile_session(
            profile=profile,
            conversation_uuid=uuid_str,
            workspace_dir=workdir,
            model=model
        )
        print(f"Started session '{res['session_name']}' for profile '{profile}'.")
        print(f"To attach: agy-manager attach {profile}")
    except Exception as e:
        print(f"Error starting session: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_switch(args):
    target_profile = args.profile
    uuid_str = args.uuid
    model = args.model
    workdir = args.dir or "/srv/projects/agy"

    print(f"Switching conversation {uuid_str} to profile {target_profile}...")
    try:
        res = tmux_ops.switch_conversation(
            target_profile=target_profile,
            conversation_uuid=uuid_str,
            workspace_dir=workdir,
            model=model
        )
        print(f"Successfully switched to {target_profile} in session '{res['session_name']}'.")
        print(f"To attach: agy-manager attach {target_profile}")
    except Exception as e:
        print(f"Error switching conversation: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_stop(args):
    target = args.target
    print(f"Stopping target: {target}...")
    success = tmux_ops.stop_session(target)
    if success:
        print(f"Successfully stopped {target}.")
    else:
        print(f"Could not find running session or process for {target}.", file=sys.stderr)

def cmd_attach(args):
    profile = args.profile
    sess_name = tmux_ops.get_tmux_session_name(profile)
    if not tmux_ops.has_tmux_session(sess_name):
        print(f"No active tmux session '{sess_name}'. Start it with: agy-manager start {profile}", file=sys.stderr)
        sys.exit(1)
    if os.getenv("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", sess_name])
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", sess_name])

def cmd_unlock(args):
    uuid_str = args.uuid
    only_if_stale = args.only_if_stale
    try:
        if only_if_stale:
            core.unlock_stale_lock(uuid_str)
            print(f"Lock for {uuid_str} checked and unlocked if stale.")
        else:
            status = core.get_lock_status(uuid_str)
            if status.get("pid"):
                print(f"Refusing to unlock: Process PID {status['pid']} is alive and holding this lock!", file=sys.stderr)
                sys.exit(1)
            core.unlock_stale_lock(uuid_str)
            print(f"Unlocked {uuid_str}.")
    except Exception as e:
        print(f"Error unlocking: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_grid(args):
    profiles = args.profiles or ["account-01", "account-02"]
    grid_session = tmux_ops.create_grid_session(profiles)
    print(f"Created 2x2 grid in tmux '{grid_session}'.")
    print(f"To attach: tmux attach -t {grid_session}")

def cmd_klajner(args):
    print("Checking Klajner AGY Engine health...")
    res = monitor_klajner.check_klajner_health()
    for k, v in res.items():
        print(f"  {k}: {v}")

def cmd_web(args):
    host = args.host
    port = args.port
    print(f"Starting Antigravity Multi-Profile Web Dashboard on http://{host}:{port}...")
    cmd = [
        "/srv/agy-manager/venv/bin/uvicorn",
        "web.server:app",
        "--host", host,
        "--port", str(port),
        "--app-dir", "/srv/projects/agy"
    ]
    os.execv("/srv/agy-manager/venv/bin/uvicorn", cmd)

def cmd_usage(args):
    profile = args.profile
    if profile:
        if not core.validate_profile_name(profile):
            print(f"Error: Invalid profile name '{profile}'", file=sys.stderr)
            sys.exit(1)
        profiles = [profile]
    else:
        profiles = [p for p in core.list_all_profiles() if core.is_profile_logged_in(p)]

    print(f"\n=== Antigravity Real-Time Quotas & Limits ({len(profiles)} Profiles) ===")
    print(f"{'PROFILE':<14} {'GEMINI 5H':<14} {'RESET 5H':<12} {'GEMINI WK':<14} {'RESET WK':<12} {'CLAUDE WK':<12}")
    print("-" * 80)

    import quota
    cached = quota.get_cached_quotas()
    for p in profiles:
        q = cached.get(p) or quota.fetch_profile_quota(p)
        g5h = f"{q.get('gemini_5h_pct', '-')}%" if q.get('gemini_5h_pct') is not None else "-"
        g5h_r = q.get('gemini_5h_human', '-')
        gw = f"{q.get('gemini_weekly_pct', '-')}%" if q.get('gemini_weekly_pct') is not None else "-"
        gw_r = q.get('gemini_weekly_human', '-')
        cw = f"{q.get('claude_weekly_pct', '-')}%" if q.get('claude_weekly_pct') is not None else "-"
        print(f"{p:<14} {g5h:<14} {g5h_r:<12} {gw:<14} {gw_r:<12} {cw:<12}")
    print()

def main():
    db.init_db()
    parser = argparse.ArgumentParser(
        prog="agy-manager",
        description="Antigravity Multi-Profile Manager (12 Profiles & Shared Conversations)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # profiles
    subparsers.add_parser("profiles", help="List all 12 profiles and their states")

    # models
    p_models = subparsers.add_parser("models", help="List models for a given profile")
    p_models.add_argument("profile", help="Profile name (e.g. account-01)")

    # conversations
    p_convs = subparsers.add_parser("conversations", help="List shared conversations")
    p_convs.add_argument("--limit", type=int, default=30, help="Max conversations to display")

    # status
    subparsers.add_parser("status", help="Show system status, locks, and Klajner monitor")

    # login
    p_login = subparsers.add_parser("login", help="Launch interactive login for a profile")
    p_login.add_argument("profile", help="Profile name (e.g. account-03)")
    p_login.add_argument("--tmux", action="store_true", help="Launch in background tmux session")

    # start
    p_start = subparsers.add_parser("start", help="Start a profile in tmux")
    p_start.add_argument("profile", help="Profile name")
    p_start.add_argument("uuid", nargs="?", help="Conversation UUID (optional)")
    p_start.add_argument("--model", help="Model name")
    p_start.add_argument("--dir", help="Workspace directory")

    # switch
    p_switch = subparsers.add_parser("switch", help="Switch conversation to another profile")
    p_switch.add_argument("profile", help="Target profile name")
    p_switch.add_argument("uuid", help="Conversation UUID to switch")
    p_switch.add_argument("--model", help="Model name")
    p_switch.add_argument("--dir", help="Workspace directory")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop conversation or profile session")
    p_stop.add_argument("target", help="Conversation UUID or profile name")

    # attach
    p_attach = subparsers.add_parser("attach", help="Attach to profile tmux session")
    p_attach.add_argument("profile", help="Profile name")

    # unlock
    p_unlock = subparsers.add_parser("unlock", help="Unlock a stale conversation lock")
    p_unlock.add_argument("uuid", help="Conversation UUID")
    p_unlock.add_argument("--only-if-stale", action="store_true", help="Only unlock if no PID holds the lock")

    # grid
    p_grid = subparsers.add_parser("grid", help="Create a 2x2 grid view for profiles")
    p_grid.add_argument("profiles", nargs="*", help="List of up to 4 profiles")

    # klajner
    subparsers.add_parser("klajner", help="Check status and health of Klajner AGY Engine")

    # sync
    p_sync = subparsers.add_parser("sync", help="Sync session data from /home/kacper into shared storage")
    p_sync.add_argument("--source", default="/home/kacper", help="Source home directory (default: /home/kacper)")

    # usage
    p_usage = subparsers.add_parser("usage", help="Show real-time quotas and limit reset times")
    p_usage.add_argument("profile", nargs="?", help="Specific profile (optional)")

    # web
    p_web = subparsers.add_parser("web", help="Start FastAPI Web Dashboard")
    p_web.add_argument("--host", default="0.0.0.0", help="Host address")
    p_web.add_argument("--port", type=int, default=8099, help="Port number")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()
    cmds = {
        "profiles": cmd_profiles,
        "models": cmd_models,
        "conversations": cmd_conversations,
        "status": cmd_status,
        "sync": cmd_sync,
        "login": cmd_login,
        "start": cmd_start,
        "switch": cmd_switch,
        "stop": cmd_stop,
        "attach": cmd_attach,
        "unlock": cmd_unlock,
        "grid": cmd_grid,
        "klajner": cmd_klajner,
        "usage": cmd_usage,
        "web": cmd_web
    }

    handler = cmds.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
