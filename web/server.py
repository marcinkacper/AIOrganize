import os
import sys
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

# Ensure manager is in path
sys.path.insert(0, "/srv/projects/agy")
import manager.core as core
import manager.tmux_ops as tmux_ops
import manager.monitor_klajner as monitor_klajner
import manager.db as db

app = FastAPI(title="Antigravity Multi-Profile Manager", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
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

@app.on_event("startup")
def startup_event():
    db.init_db()

@app.get("/api/profiles")
def get_profiles():
    profiles = core.list_all_profiles()
    active = core.get_active_sessions()
    tmux_sessions = tmux_ops.list_agy_tmux_sessions()
    session_by_profile = {s.get("profile"): s for s in active}

    data = []
    for p in profiles:
        logged_in = core.is_profile_logged_in(p)
        tmux_name = tmux_ops.get_tmux_session_name(p)
        is_tmux_active = tmux_name in tmux_sessions
        s_info = session_by_profile.get(p)
        pid = s_info.get("pid") if s_info else None
        active_uuid = s_info.get("conversation_uuid") if s_info else None

        data.append({
            "name": p,
            "logged_in": logged_in,
            "tmux_active": is_tmux_active,
            "tmux_session": tmux_name,
            "pid": pid,
            "active_uuid": active_uuid
        })
    return {"profiles": data}

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

@app.post("/api/sync")
def post_sync():
    try:
        result = core.sync_sessions_from_home()
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

# Static files
app.mount("/", StaticFiles(directory="/srv/projects/agy/web/static", html=True), name="static")
