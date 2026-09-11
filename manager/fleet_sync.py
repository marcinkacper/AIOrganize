import os
import re
import glob
import json
import sqlite3
import subprocess
import tempfile
from typing import Dict, List, Any, Optional

try:
    from .core import (
        SHARED_DIR, CONVERSATIONS_DIR, record_conversation_machine,
        update_conversation_summaries, get_connection
    )
    from .db import log_audit
except Exception:
    from core import (
        SHARED_DIR, CONVERSATIONS_DIR, record_conversation_machine,
        update_conversation_summaries, get_connection
    )
    from db import log_audit

FLEET_NODES = [
    {
        "name": "porsche",
        "ip": "100.69.214.41",
        "user": "kacper",
        "ssh_key": "/home/kacper/.ssh/id_ed25519",
        "via": None,
        "remote_gemini_dir": "/home/kacper/.gemini/antigravity-cli"
    },
    {
        "name": "home",
        "ip": "100.78.225.98",
        "user": "kacper",
        "ssh_key": "/home/kacper/.ssh/id_ed25519_homelab",
        "via": None,
        "remote_gemini_dir": "/home/kacper/.gemini/antigravity-cli"
    },
    {
        "name": "audi",
        "ip": "100.116.4.83",
        "user": "kacper",
        "ssh_key": "/home/kacper/.ssh/id_ed25519",
        "via": "porsche",
        "remote_gemini_dir": "/home/kacper/.gemini/antigravity-cli"
    }
]

def run_remote_ssh(node: Dict[str, Any], cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Runs a command on a remote node via SSH."""
    if node.get("via") == "porsche":
        ssh_cmd = [
            "ssh", "-i", node["ssh_key"],
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=5",
            f"{node['user']}@100.69.214.41",
            f"ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 {node['user']}@{node['name']} {subprocess.list2cmdline([cmd])}"
        ]
    else:
        ssh_cmd = [
            "ssh", "-i", node["ssh_key"],
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=5",
            f"{node['user']}@{node['ip']}",
            cmd
        ]
    return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)

def sync_fleet_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """
    Syncs conversation databases, brains, summaries, and history from a single fleet node.
    """
    name = node["name"]
    ip = node["ip"]
    r_dir = node["remote_gemini_dir"]
    stats = {"node": name, "synced_conversations": 0, "errors": []}

    print(f"[*] Starting fleet sync for node: {name} ({ip})...")

    # 1. Fetch remote list of conversation UUIDs
    try:
        list_cmd = f"ls {r_dir}/conversations/*.db 2>/dev/null || true"
        proc = run_remote_ssh(node, list_cmd, timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            db_paths = proc.stdout.strip().splitlines()
            for p in db_paths:
                p = p.strip()
                if not p:
                    continue
                basename = os.path.basename(p)
                uuid_match = re.match(r"^([a-f0-9-]{36})\.db$", basename)
                if uuid_match:
                    conv_uuid = uuid_match.group(1)
                    record_conversation_machine(conv_uuid, name, ip)
                    stats["synced_conversations"] += 1
            print(f"[{name}] Registered {stats['synced_conversations']} conversations from {name}.")
    except Exception as e:
        stats["errors"].append(f"Failed to list conversations: {e}")
        print(f"[{name}] Error listing conversations: {e}")

    # 2. Rsync conversations and brains
    os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
    shared_brain_dir = os.path.join(SHARED_DIR, "brain")
    os.makedirs(shared_brain_dir, exist_ok=True)

    try:
        if node.get("via") == "porsche":
            push_cmd = (
                f"rsync -a -u --ignore-existing -e 'ssh -o StrictHostKeyChecking=accept-new' "
                f"{r_dir}/conversations/ kacper@100.70.110.7:{CONVERSATIONS_DIR}/"
            )
            run_remote_ssh(node, push_cmd, timeout=3600)

            push_brain_cmd = (
                f"rsync -a -u --ignore-existing -e 'ssh -o StrictHostKeyChecking=accept-new' "
                f"{r_dir}/brain/ kacper@100.70.110.7:{shared_brain_dir}/"
            )
            run_remote_ssh(node, push_brain_cmd, timeout=3600)
        else:
            rsync_conv_cmd = [
                "rsync", "-a", "-u", "--ignore-existing",
                "-e", f"ssh -i {node['ssh_key']} -o StrictHostKeyChecking=accept-new",
                f"{node['user']}@{node['ip']}:{r_dir}/conversations/",
                f"{CONVERSATIONS_DIR}/"
            ]
            subprocess.run(rsync_conv_cmd, check=True, timeout=3600)

            rsync_brain_cmd = [
                "rsync", "-a", "-u", "--ignore-existing",
                "-e", f"ssh -i {node['ssh_key']} -o StrictHostKeyChecking=accept-new",
                f"{node['user']}@{node['ip']}:{r_dir}/brain/",
                f"{shared_brain_dir}/"
            ]
            subprocess.run(rsync_brain_cmd, check=True, timeout=3600)
    except Exception as e:
        stats["errors"].append(f"Rsync error: {e}")
        print(f"[{name}] Rsync error: {e}")

    # 3. Merge conversation_summaries.db
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = os.path.join(tmp_dir, f"summaries_{name}.db")
        try:
            if node.get("via") == "porsche":
                subprocess.run(
                    [
                        "ssh", "-i", node["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
                        f"{node['user']}@100.69.214.41",
                        f"ssh -o StrictHostKeyChecking=accept-new {node['user']}@{node['name']} 'cat {r_dir}/conversation_summaries.db 2>/dev/null || true'"
                    ],
                    stdout=open(tmp_db, "wb"),
                    timeout=30
                )
            else:
                subprocess.run(
                    [
                        "scp", "-i", node["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
                        f"{node['user']}@{node['ip']}:{r_dir}/conversation_summaries.db",
                        tmp_db
                    ],
                    capture_output=True,
                    timeout=30
                )

            if os.path.exists(tmp_db) and os.path.getsize(tmp_db) > 0:
                local_summaries = os.path.join(SHARED_DIR, "conversation_summaries.db")
                if os.path.exists(local_summaries):
                    conn = sqlite3.connect(local_summaries)
                    with conn:
                        conn.execute(f"ATTACH DATABASE '{tmp_db}' AS remote_db;")
                        local_cols = [c[1] for c in conn.execute("PRAGMA table_info(conversation_summaries);").fetchall()]
                        remote_cols = [c[1] for c in conn.execute("PRAGMA remote_db.table_info(conversation_summaries);").fetchall()]
                        common_cols = [c for c in local_cols if c in remote_cols]
                        if common_cols:
                            cols_str = ", ".join(f"`{c}`" for c in common_cols)
                            conn.execute(f"""
                                INSERT OR IGNORE INTO conversation_summaries ({cols_str})
                                SELECT {cols_str} FROM remote_db.conversation_summaries;
                            """)
                        conn.execute("DETACH DATABASE remote_db;")
                    conn.close()
                    print(f"[{name}] Merged summaries from {name}.")
        except Exception as e:
            stats["errors"].append(f"Summaries merge error: {e}")

    # 4. Merge history.jsonl
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_hist = os.path.join(tmp_dir, f"history_{name}.jsonl")
        try:
            if node.get("via") == "porsche":
                subprocess.run(
                    [
                        "ssh", "-i", node["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
                        f"{node['user']}@100.69.214.41",
                        f"ssh -o StrictHostKeyChecking=accept-new {node['user']}@{node['name']} 'cat {r_dir}/history.jsonl 2>/dev/null || true'"
                    ],
                    stdout=open(tmp_hist, "wb"),
                    timeout=30
                )
            else:
                subprocess.run(
                    [
                        "scp", "-i", node["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
                        f"{node['user']}@{node['ip']}:{r_dir}/history.jsonl",
                        tmp_hist
                    ],
                    capture_output=True,
                    timeout=30
                )

            if os.path.exists(tmp_hist) and os.path.getsize(tmp_hist) > 0:
                local_hist = os.path.join(SHARED_DIR, "history.jsonl")
                existing_lines = set()
                if os.path.exists(local_hist):
                    with open(local_hist, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            existing_lines.add(line.strip())

                added = 0
                with open(tmp_hist, "r", encoding="utf-8", errors="replace") as f_in:
                    with open(local_hist, "a", encoding="utf-8") as f_out:
                        for line in f_in:
                            sline = line.strip()
                            if sline and sline not in existing_lines:
                                f_out.write(sline + "\n")
                                existing_lines.add(sline)
                                added += 1
                print(f"[{name}] Merged {added} new history entries from {name}.")
        except Exception as e:
            stats["errors"].append(f"History merge error: {e}")

    return stats

def sync_all_fleet_nodes() -> List[Dict[str, Any]]:
    """
    Runs sync for all fleet nodes, updates summaries, and logs audit action.
    """
    results = []
    total_synced = 0
    for node in FLEET_NODES:
        res = sync_fleet_node(node)
        results.append(res)
        total_synced += res.get("synced_conversations", 0)

    try:
        update_conversation_summaries()
    except Exception as e:
        print(f"Warning: update_conversation_summaries failed: {e}")

    log_audit("FLEET_SYNC", details=f"Synced fleet sessions. Total detected: {total_synced}")
    return results
