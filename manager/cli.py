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
    active_sessions = core.get_active_sessions()
    tmux_sessions = tmux_ops.list_agy_tmux_sessions()
    dups_by_engine = core.get_duplicate_accounts_by_engine()
    session_by_profile = {s.get("profile"): s for s in active_sessions}

    def print_section(title, prof_list, eng):
        print(f"\n=== {title} ({len(prof_list)}) ===")
        print(f"{'PROFILE':<13} {'LOGGED IN':<11} {'EMAIL':<42} {'TMUX SESSION':<16} {'PID':<8} {'ACTIVE UUID':<20}")
        print("-" * 124)
        eng_dups = dups_by_engine.get(eng, {})
        for p in prof_list:
            is_logged = "YES" if core.is_profile_logged_in(p) else "NO"
            email = core.get_profile_email(p) or "-"
            email_lower = email.strip().lower() if email != "-" else ""
            if email_lower and email_lower in eng_dups:
                other_p = [x for x in eng_dups[email_lower] if x != p]
                email_display = f"{email} ⚠️[DUP:{','.join(other_p)}]"
            else:
                email_display = email

            s_info = session_by_profile.get(p)
            matched_sess = tmux_ops.list_profile_sessions(p)
            tmux_active = s_info.get("session_name") if (s_info and s_info.get("session_name") in tmux_sessions) else (matched_sess[0] if matched_sess else "-")
            pid_str = str(s_info.get("pid")) if s_info else "-"
            uuid_raw = s_info.get("conversation_uuid") or "-" if s_info else "-"
            uuid_str = f"...{uuid_raw[-5:]}" if len(uuid_raw) >= 5 and uuid_raw != "-" else uuid_raw

            display_name = p.replace("account-", "agy-") if p.startswith("account-") else p
            print(f"{display_name:<13} {is_logged:<11} {email_display:<42} {tmux_active:<16} {pid_str:<8} {uuid_str:<20}")

    print_section("Profile Google Antigravity (agy-01..agy-XX)", core.list_google_profiles(), "google")
    print_section("Profile Anthropic Claude (claude-01..claude-XX)", core.list_claude_profiles(), "claude")
    print_section("Profile OpenAI Codex (codex-01..codex-XX)", core.list_codex_profiles(), "codex")

    has_dups = any(len(v) > 0 for v in dups_by_engine.values())
    if has_dups:
        print("\n⚠️  OSTRZEŻENIE: Wykryto zduplikowane konta w ramach tej samej usługi:")
        for eng, emap in dups_by_engine.items():
            for em, plist in emap.items():
                eng_label = "Google" if eng == "google" else ("Claude" if eng == "claude" else "Codex")
                print(f"   • [{eng_label}] {em} -> {', '.join(plist)} (współdzielą pulę limitów!)")
        print("   Wskazówka: Aby wylogować profil i zwolnić miejsce na unikalne konto: agy-manager logout <profile>")
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
    search = getattr(args, "search", None)
    convs = core.list_conversations(limit=limit, search=search)
    header_extra = f" matching '{search}'" if search else ""
    print(f"\n=== Shared Conversations (Found {len(convs)}{header_extra}) ===")
    print(f"{'UUID (L5)':<11} {'MACHINE':<12} {'WORKSPACE':<26} {'STEPS':<6} {'SIZE':<10} {'MODIFIED':<20} {'TITLE'}")
    print("-" * 136)

    for c in convs:
        size_str = f"{round(c.get('size_bytes', 0) / 1024, 1)} KB"
        mtime = str(c.get("last_modified", ""))[:19]
        title = (c.get("title") or "")[:35]
        ws = (c.get("workspace") or "-")[:24]
        mach = (c.get("machine") or "ferrari")[:10]
        steps = c.get("steps", 0)
        uuid_str = c.get("uuid", "")
        uuid_short = f"...{uuid_str[-5:]}" if len(uuid_str) >= 5 else uuid_str
        wal_mark = "*" if c.get("has_wal") else " "
        print(f"{uuid_short:<11} {mach:<12} {ws:<26} {steps:<6} {size_str:<10} {mtime:<20} {wal_mark}{title}")
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
            u = str(a.get('conversation_uuid', ''))
            u_short = f"...{u[-5:]}" if len(u) >= 5 else u
            print(f"PID {a['pid']} | Profile: {a['profile']} | UUID: {u_short} (Full: {u})")

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

    p = core.resolve_profile_name(profile)

    if p.startswith("claude-") or p == "claude":
        cfg_dir = core.get_claude_config_dir(p) if p.startswith("claude-") else "/home/kacper/.claude"
        os.makedirs(cfg_dir, exist_ok=True)
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = cfg_dir
        env["HOME"] = "/home/kacper"
        env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin:{env.get('PATH', '')}"
        print(f"\n=== Logowanie profilu Claude Code: {p} ===")
        print(f"Katalog profilu: {cfg_dir}")
        print("Uruchamianie procedury autoryzacji Claude...")
        subprocess.run(["claude", "auth", "login"], env=env)
        if core.is_profile_logged_in(p):
            email_str = core.get_profile_email(p) or ""
            email_disp = f" ({email_str})" if email_str else ""
            print(f"\n✓ Sukces: Profil {p}{email_disp} został pomyślnie zalogowany!")
            core.auto_expand_slots()
        else:
            print(f"\n❌ Profil {p} nie został zalogowany.")
        return

    if p.startswith("codex-") or p == "codex":
        cdx_dir = core.get_codex_home_dir(p) if p.startswith("codex-") else "/home/kacper/.codex"
        os.makedirs(cdx_dir, exist_ok=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = cdx_dir
        env["HOME"] = "/home/kacper"
        env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin:{env.get('PATH', '')}"
        print(f"\n=== Logowanie profilu OpenAI Codex: {p} ===")
        print(f"Katalog profilu: {cdx_dir}")
        print("Uruchamianie procedury logowania Codex (tryb --device-auth dla serwera)...")
        subprocess.run(["codex", "login", "--device-auth"], env=env)
        if core.is_profile_logged_in(p):
            email_str = core.get_profile_email(p) or ""
            email_disp = f" ({email_str})" if email_str else ""
            print(f"\n✓ Sukces: Profil {p}{email_disp} został pomyślnie zalogowany!")
            core.auto_expand_slots()
        else:
            print(f"\n❌ Profil {p} nie został zalogowany.")
        return

    sess_name = f"agy-login-{p}"
    home_dir = core.get_profile_home(p)

    if tmux_ops.has_tmux_session(sess_name):
        subprocess.run(["tmux", "kill-session", "-t", sess_name], check=False)

    print(f"\nInitializing OAuth login for {p}...")
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

    if core.is_profile_logged_in(p):
        email_str = core.get_profile_email(p) or ""
        email_disp = f" ({email_str})" if email_str else ""
        print(f"\n✓ Sukces: Profil {p}{email_disp} zostal pomyslnie zalogowany!")
        core.auto_expand_slots()

        # Verify duplicate account within the SAME engine
        engine = core.get_profile_engine(p)
        dups = core.get_duplicate_accounts(engine)
        email_lower = email_str.strip().lower()
        if email_lower and email_lower in dups:
            other_p = [x for x in dups[email_lower] if x != p]
            if other_p:
                engine_name = "Google" if engine == "google" else ("Anthropic Claude" if engine == "claude" else "OpenAI Codex")
                print(f"\n⚠️  UWAGA: Wykryto zduplikowane konto {engine_name}!")
                print(f"   Konto '{email_str}' jest już używane na innym profilu {engine_name}: {', '.join(other_p)}.")
                print(f"   Profile te dzielą tę samą pulę limitów {engine_name}. Powinniśmy się wystrzegać takich akcji!")
                print(f"   Aby wylogować i zwolnić profil na inne konto: agy-manager logout {p}")
    else:
        print(f"\nNie udalo sie zalogowac profilu {p}. Sprobuj ponownie.")

def cmd_add_slot(args):
    provider = getattr(args, "provider", "google") or "google"
    new_p = core.add_profile_slot(provider)
    print(f"✓ Utworzono nowy slot {provider}: {new_p}")

def cmd_logout(args):
    profile = args.profile
    if not core.validate_profile_name(profile):
        print(f"Error: Invalid profile name '{profile}'", file=sys.stderr)
        sys.exit(1)

    if not core.is_profile_logged_in(profile):
        print(f"Profile '{profile}' is not currently logged in.")
        return

    email = core.get_profile_email(profile) or "Unknown"
    core.logout_profile(profile)
    print(f"✓ Profile '{profile}' ({email}) has been logged out successfully. Token and active sessions removed.")

def cmd_fleet_sync(args):
    print("Starting fleet session synchronization across Tailscale nodes (porsche, audi, ford)...")
    try:
        from . import fleet_sync
        results = fleet_sync.sync_all_fleet_nodes()
        for r in results:
            node = r.get("node")
            synced = r.get("synced_conversations", 0)
            errs = r.get("errors", [])
            err_str = f" [Errors: {len(errs)}]" if errs else ""
            print(f"  ✓ {node:10}: {synced} conversations registered/synced{err_str}")
        print("Fleet synchronization completed.")
    except Exception as e:
        print(f"Error during fleet sync: {e}", file=sys.stderr)
        sys.exit(1)

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
    force = getattr(args, "force", False)
    print(f"Stopping target: {target} (force={force})...")
    success = tmux_ops.stop_session(target, force=force)
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
    only_if_stale = getattr(args, "only_if_stale", False)
    force = getattr(args, "force", False)
    try:
        if only_if_stale:
            core.unlock_stale_lock(uuid_str, force=False)
            print(f"Lock for {uuid_str} checked and unlocked if stale.")
        else:
            status = core.get_lock_status(uuid_str)
            if status.get("pid") and not force:
                if status.get("is_hung"):
                    print(f"Detected hung/defunct process PID {status['pid']} (futex wait). Automatically force-unlocking...")
                    core.unlock_stale_lock(uuid_str, force=True)
                    print(f"Successfully terminated hung PID {status['pid']} and unlocked {uuid_str}.")
                    return
                else:
                    print(f"Refusing to unlock: Process PID {status['pid']} is alive and holding this lock! Use --force (-f) to terminate it.", file=sys.stderr)
                    sys.exit(1)
            core.unlock_stale_lock(uuid_str, force=force)
            print(f"Unlocked {uuid_str}.")
    except Exception as e:
        print(f"Error unlocking: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_delete(args):
    uuid_str = args.uuid
    if not args.yes:
        confirm = input(f"Are you sure you want to permanently delete conversation {uuid_str}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    try:
        core.delete_conversation(uuid_str, force=args.force)
        print(f"Successfully deleted conversation {uuid_str}.")
    except Exception as e:
        print(f"Error deleting conversation: {e}", file=sys.stderr)
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

    print(f"\n=== Antigravity Real-Time Quotas & Limits ({len(profiles)} Logged In Profiles) ===")
    print(f"{'PROFILE':<12} {'EMAIL':<30} {'USABLE':<10} {'STATUS':<22} {'WAIT TIME':<16} {'GEMINI 5H':<12} {'GEMINI WK':<12} {'CLAUDE WK':<10}")
    print("-" * 128)

    import quota
    cached = quota.get_cached_quotas()
    for p in profiles:
        email = core.get_profile_email(p) or "-"
        q = cached.get(p) or quota.fetch_profile_quota(p)
        eff = f"{q.get('gemini_effective_pct', '-')}%" if q.get('gemini_effective_pct') is not None else "-"
        st = q.get('gemini_status', '-')
        wait = q.get('gemini_wait_human', '-')
        
        if q.get('gemini_5h_disabled'):
            g5h = "disabled"
        elif q.get('gemini_5h_pct') is not None:
            g5h = f"{q.get('gemini_5h_pct')}%"
        else:
            g5h = "-"
            
        gw = f"{q.get('gemini_weekly_pct', '-')}%" if q.get('gemini_weekly_pct') is not None else "-"
        cw = f"{q.get('claude_weekly_pct', '-')}%" if q.get('claude_weekly_pct') is not None else "-"
        
        print(f"{p:<12} {email[:28]:<30} {eff:<10} {st:<22} {wait:<16} {g5h:<12} {gw:<12} {cw:<10}")
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
    p_convs.add_argument("-s", "--search", "--query", dest="search", help="Search by UUID, title, workspace, machine")

    # status
    subparsers.add_parser("status", help="Show system status, locks, and Klajner monitor")

    # login
    p_login = subparsers.add_parser("login", help="Launch interactive login for a profile")
    p_login.add_argument("profile", help="Profile name (e.g. account-03)")
    p_login.add_argument("--tmux", action="store_true", help="Launch in background tmux session")

    # logout
    p_logout = subparsers.add_parser("logout", help="Log out a profile and remove credentials")
    p_logout.add_argument("profile", help="Profile name (e.g. account-07)")

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
    p_stop.add_argument("-f", "--force", action="store_true", help="Force terminate processes with SIGKILL")

    # attach
    p_attach = subparsers.add_parser("attach", help="Attach to profile tmux session")
    p_attach.add_argument("profile", help="Profile name")

    # unlock
    p_unlock = subparsers.add_parser("unlock", help="Unlock a stale conversation lock")
    p_unlock.add_argument("uuid", help="Conversation UUID")
    p_unlock.add_argument("-f", "--force", action="store_true", help="Force terminate process holding lock (SIGKILL) and clear lock")
    p_unlock.add_argument("--only-if-stale", action="store_true", help="Only unlock if no PID holds the lock")

    # delete / rm
    p_del = subparsers.add_parser("delete", aliases=["rm"], help="Permanently delete a conversation and its files")
    p_del.add_argument("uuid", help="Conversation UUID")
    p_del.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_del.add_argument("--force", action="store_true", help="Force stop and delete active conversation")

    # grid
    p_grid = subparsers.add_parser("grid", help="Create a 2x2 grid view for profiles")
    p_grid.add_argument("profiles", nargs="*", help="List of up to 4 profiles")

    # klajner
    subparsers.add_parser("klajner", help="Check status and health of Klajner AGY Engine")

    # sync
    p_sync = subparsers.add_parser("sync", help="Sync session data from /home/kacper into shared storage")
    p_sync.add_argument("--source", default="/home/kacper", help="Source home directory (default: /home/kacper)")

    # fleet-sync / sync-fleet
    subparsers.add_parser("fleet-sync", aliases=["sync-fleet"], help="Pull and sync Antigravity sessions from all fleet machines (porsche, audi, ford)")

    # usage
    p_usage = subparsers.add_parser("usage", help="Show real-time quotas and limit reset times")
    p_usage.add_argument("profile", nargs="?", help="Specific profile (optional)")

    # add-slot
    p_add_slot = subparsers.add_parser("add-slot", help="Add a new profile slot for Google, Claude, or Codex")
    p_add_slot.add_argument("provider", choices=["google", "claude", "codex"], default="google", nargs="?", help="Provider type (google, claude, codex)")

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
        "fleet-sync": cmd_fleet_sync,
        "sync-fleet": cmd_fleet_sync,
        "login": cmd_login,
        "logout": cmd_logout,
        "add-slot": cmd_add_slot,
        "start": cmd_start,
        "switch": cmd_switch,
        "stop": cmd_stop,
        "attach": cmd_attach,
        "unlock": cmd_unlock,
        "delete": cmd_delete,
        "rm": cmd_delete,
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
