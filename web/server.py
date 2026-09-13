import os
import sys
import time
import pty
import fcntl
import termios
import struct
import select
import socket
import signal
import asyncio
import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure manager is in path
sys.path.insert(0, "/srv/projects/agy")
import manager.core as core
import manager.tmux_ops as tmux_ops
import manager.monitor_klajner as monitor_klajner
import manager.db as db
import manager.quota as quota
import manager.fleet_sync as fleet_sync
import uuid
from manager.pool_router import pool_router, normalize_model, ALLOWED_MODELS

app = FastAPI(title="Antigravity Multi-Profile Manager", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background quota refresher loop
async def quota_periodic_refresher():
    while True:
        try:
            # Run in thread so it doesn't block async loop
            await asyncio.to_thread(quota.refresh_all_quotas)
        except Exception as e:
            print(f"[Quota Background Error] {e}", file=sys.stderr)
        # Sleep 5 minutes
        await asyncio.sleep(300)

@app.on_event("startup")
async def startup_event():
    db.init_db()
    # Start background 5-minute quota poller
    asyncio.create_task(quota_periodic_refresher())

# Request Models
class StartRequest(BaseModel):
    profile: str
    uuid: Optional[str] = None
    model: Optional[str] = None
    workspace_dir: Optional[str] = "/srv/projects/agy"

class SwitchRequest(BaseModel):
    target_profile: str
    uuid: str
    model: Optional[str] = None
    workspace_dir: Optional[str] = "/srv/projects/agy"

class StopRequest(BaseModel):
    target: str
    force: bool = False

class UnlockRequest(BaseModel):
    uuid: str
    only_if_stale: bool = True
    force: bool = False

@app.get("/favicon.ico")
def favicon_route():
    return FileResponse("/srv/projects/agy/web/static/favicon.svg", media_type="image/svg+xml")

@app.get("/api/profiles")
def get_profiles():
    try:
        core.auto_expand_slots()
    except Exception as e:
        logger.warning(f"Error auto-expanding slots: {e}")
    profiles = core.list_all_profiles()
    active = core.get_active_sessions()
    cached_quotas = quota.get_cached_quotas()
    dups = core.get_duplicate_accounts()
    conv_machines = core.get_conversation_machines()
    hostname = socket.gethostname() or "ferrari"

    # Group active processes by profile
    active_by_profile: Dict[str, List[Dict[str, Any]]] = {}
    for s in active:
        p_name = s.get("profile")
        if p_name:
            active_by_profile.setdefault(p_name, []).append(s)

    data = []
    for p in profiles:
        logged_in = core.is_profile_logged_in(p)
        profile_tmux_sessions = tmux_ops.list_profile_sessions(p)

        # Build list of active session objects for this profile
        proc_list = active_by_profile.get(p, [])
        seen_sess_names = set()
        p_sessions = []

        for pr in proc_list:
            s_name = pr.get("session_name") or f"agy-{p}"
            seen_sess_names.add(s_name)
            u = pr.get("conversation_uuid")
            m = hostname
            orig_m = conv_machines.get(u) if u else None
            if orig_m and orig_m not in [hostname, "kacper"]:
                m = f"{hostname} ({orig_m})"
            
            p_sessions.append({
                "session_name": s_name,
                "pid": pr.get("pid"),
                "conversation_uuid": u,
                "directory": pr.get("cwd") or "/srv/projects/agy",
                "machine": m,
                "cmdline": pr.get("cmdline")
            })

        # Add any tmux sessions for this profile not yet in proc_list
        for ts in profile_tmux_sessions:
            if ts not in seen_sess_names:
                p_sessions.append({
                    "session_name": ts,
                    "pid": None,
                    "conversation_uuid": None,
                    "directory": "/srv/projects/agy",
                    "machine": hostname,
                    "cmdline": None
                })
                seen_sess_names.add(ts)

        is_tmux_active = len(p_sessions) > 0
        primary_sess = p_sessions[0] if p_sessions else None
        pid = primary_sess["pid"] if primary_sess else None
        active_uuid = primary_sess["conversation_uuid"] if primary_sess else None
        active_cwd = primary_sess["directory"] if primary_sess else None
        active_machine = primary_sess["machine"] if primary_sess else None
        primary_tmux_name = primary_sess["session_name"] if primary_sess else tmux_ops.get_tmux_session_name(p)

        q = cached_quotas.get(p, {})
        email = core.get_profile_email(p)
        is_dup = bool(email and email in dups)
        dup_with = [x for x in dups.get(email, []) if x != p] if is_dup else []

        is_res = core.is_profile_reserved(p)
        res_for = "pawel" if p.lower() == "klajner" else ("system" if is_res else None)

        active_model = core.get_profile_active_model(p)
        active_family = "gemini"
        if active_model:
            m_lower = active_model.lower()
            if "claude" in m_lower:
                active_family = "claude"
            elif "gemini" in m_lower:
                active_family = "gemini"

        engine_type = core.get_profile_engine(p)
        display_name = p.replace("account-", "agy-") if p.startswith("account-") else p

        data.append({
            "name": p,
            "display_name": display_name,
            "engine": engine_type,
            "email": email,
            "is_duplicate": is_dup,
            "duplicate_with": dup_with,
            "is_reserved": is_res,
            "reserved_for": res_for,
            "logged_in": logged_in,
            "tmux_active": is_tmux_active,
            "tmux_session": primary_tmux_name,
            "sessions": p_sessions,
            "pid": pid,
            "active_uuid": active_uuid,
            "active_machine": active_machine,
            "active_directory": active_cwd,
            "active_model": active_model,
            "active_family": active_family,
            "quota": {
                "gemini_effective_pct": q.get("gemini_effective_pct"),
                "gemini_status": q.get("gemini_status") or "Unknown",
                "gemini_wait_human": q.get("gemini_wait_human") or "-",
                "gemini_5h_pct": q.get("gemini_5h_pct"),
                "gemini_5h_disabled": bool(q.get("gemini_5h_disabled")),
                "gemini_5h_reset": q.get("gemini_5h_reset"),
                "gemini_5h_human": q.get("gemini_5h_human") or "-",
                "gemini_weekly_pct": q.get("gemini_weekly_pct"),
                "gemini_weekly_reset": q.get("gemini_weekly_reset"),
                "gemini_weekly_human": q.get("gemini_weekly_human") or "-",
                "claude_effective_pct": q.get("claude_effective_pct"),
                "claude_status": q.get("claude_status") or "Unknown",
                "claude_wait_human": q.get("claude_wait_human") or "-",
                "claude_5h_pct": q.get("claude_5h_pct"),
                "claude_5h_disabled": bool(q.get("claude_5h_disabled")),
                "claude_weekly_pct": q.get("claude_weekly_pct"),
                "claude_weekly_reset": q.get("claude_weekly_reset"),
                "claude_weekly_human": q.get("claude_weekly_human") or "-",
                "updated_at": q.get("updated_at")
            }
        })

    google_list = [x for x in data if x.get("engine") == "google"]
    claude_list = [x for x in data if x.get("engine") == "claude"]
    codex_list = [x for x in data if x.get("engine") == "codex"]

    return {
        "profiles": data,
        "google": google_list,
        "claude": claude_list,
        "codex": codex_list
    }

@app.post("/api/slots/add")
def add_slot_api(req: Dict[str, Any]):
    provider = req.get("provider", "google")
    new_slot = core.add_profile_slot(provider)
    return {"status": "ok", "provider": provider, "new_slot": new_slot}

@app.post("/api/quota/refresh")
async def refresh_quotas_endpoint():
    res = await asyncio.to_thread(quota.refresh_all_quotas)
    return {"status": "ok", "refreshed": list(res.keys())}

@app.get("/api/models/{profile}")
def get_models(profile: str):
    if not core.validate_profile_name(profile):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    if not core.is_profile_logged_in(profile):
        raise HTTPException(status_code=400, detail="Profile not logged in")
    models = core.get_profile_models(profile)
    return {"profile": profile, "models": [{"id": m[0], "name": m[1]} for m in models]}

@app.get("/api/conversations")
def get_conversations(limit: int = 60, query: Optional[str] = None, category: Optional[str] = None):
    convs = core.list_conversations(limit=limit, search=query, category=category)
    counts = core.get_conversations_stats(search=query)
    return {"conversations": convs, "counts": counts}

@app.get("/api/klajner")
def get_klajner():
    return monitor_klajner.check_klajner_health()

@app.get("/api/audit")
def get_audit(limit: int = 30):
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, timestamp, action, profile, conversation_uuid, details
        FROM audit_log
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return {"audit": [dict(r) for r in rows]}

@app.get("/api/pool/best")
def get_best_pool_profile(family: str = "gemini"):
    """
    Zwraca konto z puli ogólnej (account-01..account-12) posiadające najwięcej dostępnych zasobów.
    Ściśle wyklucza profil 'klajner' (zarezerwowany dla silnika Pawła) oraz profile systemowe.
    """
    try:
        best_p, q = pool_router.get_best_profile(family=family)
        return {
            "status": "ok",
            "profile": best_p,
            "gemini_effective_pct": q.get("gemini_effective_pct"),
            "claude_effective_pct": q.get("claude_effective_pct"),
            "gemini_status": q.get("gemini_status"),
            "gemini_5h_pct": q.get("gemini_5h_pct"),
            "quota": q
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/start")
def start_session(req: StartRequest, request: Request):
    try:
        target_p = req.profile
        if target_p.lower() in ("any", "auto", "best", "konsola", "default"):
            best_p, _ = pool_router.get_best_profile()
            target_p = best_p
            req.profile = best_p

        if core.is_profile_reserved(target_p):
            raise HTTPException(
                status_code=403,
                detail=f"Profil '{target_p}' jest ściśle zarezerwowany dla silnika Pawła i nie może być używany do sesji użytkownika!"
            )

        client_ip = request.client.host if request.client else None
        machine = core.resolve_machine(client_ip)
        if req.uuid:
            core.record_conversation_machine(req.uuid, machine, client_ip or "")

        res = tmux_ops.start_profile_session(
            profile=target_p,
            conversation_uuid=req.uuid,
            workspace_dir=req.workspace_dir or "/srv/projects/agy",
            model=req.model
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/switch")
def switch_session(req: SwitchRequest, request: Request):
    try:
        target_p = req.target_profile
        if target_p.lower() in ("any", "auto", "best", "konsola", "default"):
            best_p, _ = pool_router.get_best_profile()
            target_p = best_p
            req.target_profile = best_p

        if core.is_profile_reserved(target_p):
            raise HTTPException(
                status_code=403,
                detail=f"Profil '{target_p}' jest ściśle zarezerwowany dla silnika Pawła i nie może być używany do przełączania sesji!"
            )

        client_ip = request.client.host if request.client else None
        machine = core.resolve_machine(client_ip)
        if req.uuid:
            core.record_conversation_machine(req.uuid, machine, client_ip or "")

        res = tmux_ops.switch_conversation(
            target_profile=target_p,
            conversation_uuid=req.uuid,
            workspace_dir=req.workspace_dir or "/srv/projects/agy",
            model=req.model
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/terminal/quota/{profile}")
def get_terminal_quota_endpoint(profile: str, uuid: Optional[str] = None, session: Optional[str] = None):
    """
    Zwraca bieżący limit tokenów, aktywny model oraz katalog roboczy sesji (cwd) dla wskazanego profilu / okna konsoli.
    Dynamicznie wykrywa, czy użytkownik przełączył się na Gemini czy Claude.
    """
    try:
        actual_profile = profile
        if actual_profile.lower() in ("any", "auto", "best", "konsola", "default"):
            best_p, _ = pool_router.get_best_profile()
            actual_profile = best_p

        cwd = None
        target_session = session
        if not target_session:
            if actual_profile == "bash":
                target_session = "agy-bash"
            elif uuid:
                target_session = f"agy-{actual_profile}-{uuid[:8]}"
            else:
                target_session = f"agy-{actual_profile}"

        if target_session:
            try:
                res = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", target_session, "#{pane_current_path}"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                if res.returncode == 0 and res.stdout.strip():
                    cwd = res.stdout.strip()
            except Exception:
                pass

        if actual_profile in ["claude", "codex", "bash"] or actual_profile.startswith("claude-") or actual_profile.startswith("codex-"):
            email = core.get_profile_email(actual_profile)
            engine_family = core.get_profile_engine(actual_profile)
            q = quota.fetch_profile_quota(actual_profile) if (actual_profile.startswith("claude-") or actual_profile.startswith("codex-")) else {}
            eff_pct = q.get("claude_effective_pct", 100) if engine_family == "claude" else 100
            model_name = "Claude Code" if engine_family == "claude" else ("Codex" if engine_family == "codex" else "Bash Console")
            return {
                "status": "ok",
                "profile": actual_profile,
                "session": target_session,
                "cwd": cwd,
                "email": email,
                "active_model": model_name,
                "active_family": engine_family,
                "active_effective_pct": eff_pct,
                "gemini": None,
                "claude": {
                    "effective_pct": q.get("claude_effective_pct", 100),
                    "status": q.get("claude_status") or "Available",
                    "5h_pct": q.get("claude_5h_pct"),
                    "weekly_pct": q.get("claude_weekly_pct"),
                    "wait_human": q.get("claude_wait_human") or "ready"
                } if engine_family == "claude" else None
            }

        active_model = core.get_profile_active_model(actual_profile)
        q = quota.get_cached_quotas().get(actual_profile, {})
        email = core.get_profile_email(actual_profile)

        gemini_eff = q.get("gemini_effective_pct")
        claude_eff = q.get("claude_effective_pct")

        active_family = "gemini"
        if active_model:
            m_lower = active_model.lower()
            if "claude" in m_lower:
                active_family = "claude"
            elif "gemini" in m_lower:
                active_family = "gemini"

        active_effective_pct = claude_eff if active_family == "claude" else gemini_eff

        return {
            "status": "ok",
            "profile": actual_profile,
            "session": target_session,
            "cwd": cwd,
            "email": email,
            "active_model": active_model or "Gemini 3.1 Pro (Domyślny)",
            "active_family": active_family,
            "active_effective_pct": active_effective_pct,
            "gemini": {
                "effective_pct": gemini_eff,
                "status": q.get("gemini_status") or "Available",
                "5h_pct": q.get("gemini_5h_pct"),
                "weekly_pct": q.get("gemini_weekly_pct"),
                "wait_human": q.get("gemini_wait_human") or "ready"
            },
            "claude": {
                "effective_pct": claude_eff,
                "status": q.get("claude_status") or "Available",
                "5h_pct": q.get("claude_5h_pct"),
                "weekly_pct": q.get("claude_weekly_pct"),
                "wait_human": q.get("claude_wait_human") or "ready"
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stop")
def stop_session(req: StopRequest):
    success = tmux_ops.stop_session(req.target, force=req.force)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to stop {req.target}")
    return {"success": True, "target": req.target}

@app.post("/api/logout/{profile}")
def logout_profile_api(profile: str):
    if not core.validate_profile_name(profile):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    success = core.logout_profile(profile)
    return {"status": "ok", "profile": profile}

@app.post("/api/unlock")
def unlock_lock(req: UnlockRequest):
    try:
        if req.only_if_stale:
            core.unlock_stale_lock(req.uuid, force=False)
        else:
            status = core.get_lock_status(req.uuid)
            if status.get("pid") and not req.force:
                if status.get("is_hung"):
                    core.unlock_stale_lock(req.uuid, force=True)
                    return {"success": True, "uuid": req.uuid, "hung_killed": True}
                raise RuntimeError(f"Lock active with live PID {status['pid']}. Set force=true to terminate.")
            core.unlock_stale_lock(req.uuid, force=req.force)
        return {"success": True, "uuid": req.uuid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/conversations/{uuid}")
def delete_conversation_route(uuid: str, force: bool = False):
    try:
        core.delete_conversation(uuid, force=force)
        return {"success": True, "uuid": uuid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/engines")
def get_engines_route():
    return {"engines": core.get_engines_status()}

fleet_sync_state = {"running": False, "last_result": None}

def _run_fleet_sync_task():
    global fleet_sync_state
    fleet_sync_state["running"] = True
    try:
        res = fleet_sync.sync_all_fleet_nodes()
        fleet_sync_state["last_result"] = res
    except Exception as e:
        fleet_sync_state["last_result"] = [{"error": str(e)}]
    finally:
        fleet_sync_state["running"] = False

@app.post("/api/fleet/sync")
def trigger_fleet_sync_route(background_tasks: BackgroundTasks):
    global fleet_sync_state
    if fleet_sync_state["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(_run_fleet_sync_task)
    return {"status": "started"}

@app.get("/api/fleet/status")
def get_fleet_status_route():
    global fleet_sync_state
    return fleet_sync_state

@app.get("/api/preview/{profile}")
def preview_session(profile: str):
    sess_name = tmux_ops.get_tmux_session_name(profile)
    content = tmux_ops.capture_tmux_pane(sess_name, lines=60)
    return {"profile": profile, "content": content}

@app.api_route("/terminal/{target}", methods=["GET", "HEAD"])
def standalone_terminal_view(target: str):
    if target.lower() in ("any", "auto", "best", "konsola", "default"):
        return FileResponse("/srv/projects/agy/web/static/terminal.html")
    if core.is_profile_reserved(target):
        raise HTTPException(status_code=403, detail="Profil jest ściśle zarezerwowany dla silnika Pawła i nie może być używany do sesji użytkownika!")
    if not (core.validate_profile_name(target) or target.startswith("agy-")):
        raise HTTPException(status_code=400, detail="Invalid profile or session name")
    return FileResponse("/srv/projects/agy/web/static/terminal.html")

# WebSocket Interactive Web Console (PTY + tmux attach)
@app.websocket("/ws/terminal/{target}")
async def websocket_terminal(
    websocket: WebSocket,
    target: str,
    session: Optional[str] = None,
    uuid: Optional[str] = None,
    workspace_dir: Optional[str] = None
):
    await websocket.accept()

    # Determine profile and session_name
    profile = target
    session_name = session

    if target.lower() in ("any", "auto", "best", "konsola", "default"):
        try:
            best_p, _ = pool_router.get_best_profile()
            profile = best_p
            if not session_name:
                session_name = tmux_ops.get_tmux_session_name(profile, uuid) if uuid else tmux_ops.get_tmux_session_name(profile)
        except Exception as e:
            await websocket.send_text(f"\r\n[Błąd wyboru konta z puli: {e}]\r\n")
            await websocket.close(code=1011)
            return

    elif target.startswith("agy-"):
        session_name = target
        stripped = target[4:]
        if stripped in ("bash", "claude", "codex"):
            profile = stripped
        elif stripped.startswith("claude-") or stripped.startswith("codex-"):
            parts = stripped.split("-")
            profile = f"{parts[0]}-{parts[1]}"
        elif stripped.startswith("account-"):
            parts = stripped.split("-")
            if len(parts) >= 2:
                profile = f"{parts[0]}-{parts[1]}"
            else:
                profile = stripped
        else:
            profile = stripped.split("-")[0]
    elif not session_name:
        if uuid:
            session_name = tmux_ops.get_tmux_session_name(target, uuid)
        else:
            session_name = tmux_ops.get_tmux_session_name(target)

    if core.is_profile_reserved(profile):
        await websocket.send_text(f"\r\n[BŁĄD: Profil '{profile}' jest ściśle zarezerwowany dla silnika Pawła i nie może być używany w konsoli!]\r\n")
        await websocket.close(code=1008)
        return

    if not core.validate_profile_name(profile):
        await websocket.close(code=1008)
        return

    # Client machine tracking
    client_ip = websocket.client.host if websocket.client else None
    machine = core.resolve_machine(client_ip)
    active_uuid = uuid
    if not active_uuid:
        for s in core.get_active_sessions():
            if s.get("session_name") == session_name:
                active_uuid = s.get("conversation_uuid")
                break
            if s.get("profile") == profile and not active_uuid:
                active_uuid = s.get("conversation_uuid")

    if active_uuid:
        core.record_conversation_machine(active_uuid, machine, client_ip or "")

    home_dir = core.get_profile_home(profile)

    # Open PTY
    master_fd, slave_fd = pty.openpty()
    
    # Default 120 cols x 32 rows
    initial_winsize = struct.pack("HHHH", 32, 120, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, initial_winsize)

    env = os.environ.copy()
    env["HOME"] = home_dir
    env["TERM"] = "xterm-256color"
    env["PATH"] = f"/home/kacper/.local/bin:{env.get('PATH', '')}"

    # Spawn tmux new-session -A to attach if running, or launch if not
    if profile == "bash":
        home_dir = "/home/kacper"
        run_cmd = ["bash", "-l"]
    elif profile == "claude" or profile.startswith("claude-"):
        cfg_dir = core.get_claude_config_dir(profile) if profile.startswith("claude-") else "/home/kacper/.claude"
        run_cmd = ["bash", "-c", f"CLAUDE_CONFIG_DIR={cfg_dir} HOME=/home/kacper PATH=/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin claude"]
    elif profile == "codex" or profile.startswith("codex-"):
        cdx_dir = core.get_codex_home_dir(profile) if profile.startswith("codex-") else "/home/kacper/.codex"
        run_cmd = ["bash", "-c", f"CODEX_HOME={cdx_dir} HOME=/home/kacper PATH=/usr/local/bin:/usr/bin:/bin:/home/kacper/.local/bin codex"]
    else:
        cmd = [f"HOME={home_dir}", f"PATH=/home/kacper/.local/bin:$PATH", core.AGY_BIN]
        if active_uuid:
            cmd.extend(["--conversation", active_uuid])
        run_cmd = ["bash", "-c", " ".join(cmd)]

    def preexec_hook():
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    ws_dir = workspace_dir
    if not ws_dir or ws_dir == "/srv/projects/agy":
        if target.lower() in ("konsola", "auto", "best") or (session_name and "klajner" in session_name.lower()):
            ws_dir = "/srv/projects/klajner/project"
        else:
            ws_dir = "/srv/projects/agy"

    proc = subprocess.Popen(
        ["tmux", "new-session", "-A", "-s", session_name, "-c", ws_dir] + run_cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=preexec_hook,
        env=env
    )
    os.close(slave_fd)

    # Ensure tmux session dynamically adapts window size to client, has no status bar and preserves mouse scrolling
    subprocess.run(["tmux", "set-option", "-t", session_name, "window-size", "latest"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-window-option", "-t", session_name, "aggressive-resize", "on"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-option", "-t", session_name, "status", "off"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-window-option", "-t", session_name, "pane-border-status", "top"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-option", "-s", "terminal-overrides", "xterm*:csr@:il@:il1@:dl@:dl1@:rin@:indn@"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-option", "-t", session_name, "mouse", "on"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-option", "-t", session_name, "focus-events", "off"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-option", "-t", session_name, "set-clipboard", "off"], capture_output=True, check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "root", "MouseDrag1Pane"], capture_output=True, check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode", "MouseDrag1Pane"], capture_output=True, check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode", "MouseDragEnd1Pane"], capture_output=True, check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode-vi", "MouseDrag1Pane"], capture_output=True, check=False)
    subprocess.run(["tmux", "unbind-key", "-T", "copy-mode-vi", "MouseDragEnd1Pane"], capture_output=True, check=False)
    subprocess.run(["tmux", "bind-key", "-T", "copy-mode", "MouseDown1Pane", "send-keys", "-X", "cancel"], capture_output=True, check=False)
    subprocess.run(["tmux", "bind-key", "-T", "copy-mode-vi", "MouseDown1Pane", "send-keys", "-X", "cancel"], capture_output=True, check=False)
    subprocess.run(["tmux", "set-window-option", "-t", session_name, "mode-style", "bg=colour237,fg=colour111"], capture_output=True, check=False)

    loop = asyncio.get_running_loop()

    async def pty_to_websocket():
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass

    async def websocket_to_pty():
        try:
            while True:
                msg = await websocket.receive()
                if "text" in msg:
                    raw_text = msg["text"]
                    # Ignore terminal focus-in / focus-out escape sequences to prevent aborting tasks
                    if raw_text in ("\x1b[I", "\x1b[O"):
                        continue
                    if raw_text.startswith("{") and "type" in raw_text:
                        try:
                            cmd = json.loads(raw_text)
                            if cmd.get("type") == "resize":
                                cols = int(cmd.get("cols", 120))
                                rows = int(cmd.get("rows", 32))
                                ws = struct.pack("HHHH", rows, cols, 0, 0)
                                try:
                                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
                                except Exception:
                                    pass
                                try:
                                    os.kill(proc.pid, signal.SIGWINCH)
                                except Exception:
                                    pass
                                # Force tmux window to instantly match client viewport
                                subprocess.run(
                                    ["tmux", "resize-window", "-t", session_name, "-x", str(cols), "-y", str(rows)],
                                    capture_output=True,
                                    check=False
                                )
                                subprocess.run(
                                    ["tmux", "refresh-client", "-S"],
                                    capture_output=True,
                                    check=False
                                )
                                continue
                        except Exception:
                            pass
                    os.write(master_fd, raw_text.encode("utf-8"))
                elif "bytes" in msg:
                    os.write(master_fd, msg["bytes"])
        except (WebSocketDisconnect, Exception):
            pass

    task1 = asyncio.create_task(pty_to_websocket())
    task2 = asyncio.create_task(websocket_to_pty())

    done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()

    try:
        os.close(master_fd)
    except Exception:
        pass

    try:
        proc.terminate()
    except Exception:
        pass

# -------------------------------------------------------------
# OPENAI COMPATIBLE AGY COLLECTOR POOL ROUTER
# -------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = ""
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None

@app.get("/api/collector/status")
def get_collector_status():
    """Zwraca stan puli kont kolektora, zdrowe konta, historię i statystyki rotacji."""
    return pool_router.get_pool_status()

@app.get("/v1/models")
def list_collector_models():
    """Zwraca listę obsługiwanych modeli w formacie OpenAI."""
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 1789150000,
                "owned_by": "google-agy-collector",
                "permission": [],
                "root": m,
                "parent": None
            }
            for m in ALLOWED_MODELS
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    """
    OpenAI-kompatybilny endpoint obsługujący żądania ze SmartStaff.
    Automatycznie wybiera zdrowe konto z puli 12 profili kolektora /srv/projects/agy.
    """
    model = normalize_model(req.model)
    
    # 1. Assembling prompt from system instructions and conversation history
    system_parts: List[str] = []
    dialogue_parts: List[str] = []
    
    for msg in req.messages:
        role = msg.role.lower()
        content = msg.content or ""
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            dialogue_parts.append(f"Klient: {content}")
        elif role == "assistant":
            dialogue_parts.append(f"Asystent: {content}")
            
    prompt_sections: List[str] = []
    if system_parts:
        prompt_sections.append("### INSTRUKCJE SYSTEMOWE:\n" + "\n\n".join(system_parts))
        
    if req.tools:
        tools_desc = []
        for t in req.tools:
            fn = t.get("function", {})
            name = fn.get("name")
            desc = fn.get("description", "")
            params = json.dumps(fn.get("parameters", {}), ensure_ascii=False)
            tools_desc.append(f"- Funkcja `{name}`: {desc}\n  Parametry: {params}")
        prompt_sections.append(
            "### DOSTĘPNE NARZĘDZIA:\n" + "\n".join(tools_desc) + "\n\n"
            "Jeśli sytuacja wymaga wywołania narzędzia, na końcu odpowiedzi dodaj dokładnie taki blok:\n"
            "```tool_call:<nazwa_funkcji>\n"
            "{\"parametr\": \"wartość\"}\n"
            "```"
        )
        
    if dialogue_parts:
        prompt_sections.append("### PRZEBIEG ROZMOWY:\n" + "\n".join(dialogue_parts))
        
    prompt_sections.append("### TWOJE ZADANIE:\nOdpowiedz bezpośrednio i precyzyjnie jako Asystent AI zgodnie z powyższymi wytycznymi.")
    
    full_prompt = "\n\n".join(prompt_sections)
    
    # Extract optional conversation UUID from header or request
    conv_uuid = request.headers.get("X-Conversation-Id") or request.headers.get("X-Session-Id")
    
    try:
        agy_result = await pool_router.execute_prompt(
            prompt=full_prompt,
            model=model,
            conversation_uuid=conv_uuid,
            max_retries=3
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Błąd wykonania zapytania w puli AGY: {str(e)}")
        
    raw_response = (agy_result.get("response") or "").strip()
    
    # Tool call parsing
    tool_calls = None
    finish_reason = "stop"
    tool_match = re.search(r"```tool_call:([a-zA-Z0-9_\-]+)\s*(\{.*?\})\s*```", raw_response, re.DOTALL)
    if tool_match:
        try:
            tool_name = tool_match.group(1)
            tool_args_str = tool_match.group(2)
            json.loads(tool_args_str)
            tool_call_id = f"call_{uuid.uuid4().hex[:8]}"
            tool_calls = [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": tool_args_str
                    }
                }
            ]
            raw_response = raw_response.replace(tool_match.group(0), "").strip()
            finish_reason = "tool_calls"
        except Exception as err:
            pass
            
    usage_info = agy_result.get("usage") or {}
    
    response_payload = {
        "id": f"chatcmpl-collector-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": agy_result.get("model_used", model),
        "account_used": agy_result.get("account_used"),
        "quota_remaining_pct": agy_result.get("quota_remaining_pct"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": raw_response if raw_response else None,
                    "tool_calls": tool_calls
                },
                "finish_reason": finish_reason
            }
        ],
        "usage": {
            "prompt_tokens": usage_info.get("input_tokens", 0),
            "completion_tokens": usage_info.get("output_tokens", 0),
            "total_tokens": usage_info.get("total_tokens", 0)
        }
    }
    
    resp = JSONResponse(response_payload)
    resp.headers["X-AGY-Account"] = str(agy_result.get("account_used", ""))
    resp.headers["X-AGY-Quota"] = f"{agy_result.get('quota_remaining_pct', '')}%"
    resp.headers["X-AGY-Duration"] = f"{agy_result.get('duration_seconds', 0):.2f}s"
    return resp

# Mount static frontend
app.mount("/", StaticFiles(directory="/srv/projects/agy/web/static", html=True), name="static")
