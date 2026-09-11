import os
import sys
import pty
import fcntl
import termios
import struct
import select
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure manager is in path
sys.path.insert(0, "/srv/projects/agy")
import manager.core as core
import manager.tmux_ops as tmux_ops
import manager.monitor_klajner as monitor_klajner
import manager.db as db
import manager.quota as quota

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

class UnlockRequest(BaseModel):
    uuid: str
    only_if_stale: bool = True

@app.get("/api/profiles")
def get_profiles():
    profiles = core.list_all_profiles()
    active = core.get_active_sessions()
    tmux_sessions = tmux_ops.list_agy_tmux_sessions()
    session_by_profile = {s.get("profile"): s for s in active}
    cached_quotas = quota.get_cached_quotas()

    data = []
    for p in profiles:
        logged_in = core.is_profile_logged_in(p)
        tmux_name = tmux_ops.get_tmux_session_name(p)
        is_tmux_active = tmux_name in tmux_sessions
        s_info = session_by_profile.get(p)
        pid = s_info.get("pid") if s_info else None
        active_uuid = s_info.get("conversation_uuid") if s_info else None
        q = cached_quotas.get(p, {})

        data.append({
            "name": p,
            "logged_in": logged_in,
            "tmux_active": is_tmux_active,
            "tmux_session": tmux_name,
            "pid": pid,
            "active_uuid": active_uuid,
            "quota": {
                "gemini_5h_pct": q.get("gemini_5h_pct"),
                "gemini_5h_reset": q.get("gemini_5h_reset"),
                "gemini_5h_human": q.get("gemini_5h_human") or "-",
                "gemini_weekly_pct": q.get("gemini_weekly_pct"),
                "gemini_weekly_reset": q.get("gemini_weekly_reset"),
                "gemini_weekly_human": q.get("gemini_weekly_human") or "-",
                "claude_5h_pct": q.get("claude_5h_pct"),
                "claude_weekly_pct": q.get("claude_weekly_pct"),
                "claude_weekly_reset": q.get("claude_weekly_reset"),
                "claude_weekly_human": q.get("claude_weekly_human") or "-",
                "updated_at": q.get("updated_at")
            }
        })
    return {"profiles": data}

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
def get_conversations(limit: int = 40):
    convs = core.list_conversations(limit=limit)
    return {"conversations": convs}

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

@app.post("/api/start")
def start_session(req: StartRequest):
    try:
        res = tmux_ops.start_profile_session(
            profile=req.profile,
            conversation_uuid=req.uuid,
            workspace_dir=req.workspace_dir or "/srv/projects/agy",
            model=req.model
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/switch")
def switch_session(req: SwitchRequest):
    try:
        res = tmux_ops.switch_conversation(
            target_profile=req.target_profile,
            conversation_uuid=req.uuid,
            workspace_dir=req.workspace_dir or "/srv/projects/agy",
            model=req.model
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/stop")
def stop_session(req: StopRequest):
    success = tmux_ops.stop_session(req.target)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to stop {req.target}")
    return {"success": True, "target": req.target}

@app.post("/api/unlock")
def unlock_lock(req: UnlockRequest):
    try:
        if req.only_if_stale:
            core.unlock_stale_lock(req.uuid)
        else:
            status = core.get_lock_status(req.uuid)
            if status.get("pid"):
                raise RuntimeError(f"Lock active with live PID {status['pid']}")
            core.unlock_stale_lock(req.uuid)
        return {"success": True, "uuid": req.uuid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/preview/{profile}")
def preview_session(profile: str):
    sess_name = tmux_ops.get_tmux_session_name(profile)
    content = tmux_ops.capture_tmux_pane(sess_name, lines=60)
    return {"profile": profile, "content": content}

# WebSocket Interactive Web Console (PTY + tmux attach)
@app.websocket("/ws/terminal/{profile}")
async def websocket_terminal(websocket: WebSocket, profile: str):
    await websocket.accept()

    if not core.validate_profile_name(profile):
        await websocket.close(code=1008)
        return

    home_dir = core.get_profile_home(profile)
    session_name = tmux_ops.get_tmux_session_name(profile)

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
    # If starting fresh, launch agy directly in that session
    start_cmd = f"HOME={home_dir} PATH=/home/kacper/.local/bin:$PATH {core.AGY_BIN}"
    proc = subprocess.Popen(
        ["tmux", "new-session", "-A", "-s", session_name, "-c", "/srv/projects/agy", start_cmd],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
        env=env
    )
    os.close(slave_fd)

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
                    if raw_text.startswith("{") and "type" in raw_text:
                        try:
                            cmd = json.loads(raw_text)
                            if cmd.get("type") == "resize":
                                cols = int(cmd.get("cols", 120))
                                rows = int(cmd.get("rows", 32))
                                ws = struct.pack("HHHH", rows, cols, 0, 0)
                                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
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

# Mount static frontend
app.mount("/", StaticFiles(directory="/srv/projects/agy/web/static", html=True), name="static")
