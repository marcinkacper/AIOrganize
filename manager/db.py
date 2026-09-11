import sqlite3
import os
from datetime import datetime

DB_PATH = "/srv/agy-manager/data/manager.db"

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile TEXT NOT NULL,
        conversation_uuid TEXT,
        pid INTEGER,
        tmux_session TEXT,
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        ended_at DATETIME,
        status TEXT NOT NULL
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversation_locks (
        conversation_uuid TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        pid INTEGER,
        tmux_session TEXT,
        locked_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        action TEXT NOT NULL,
        profile TEXT,
        conversation_uuid TEXT,
        details TEXT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS klajner_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_healthy BOOLEAN,
        status_code INTEGER,
        response_time_ms REAL,
        details TEXT,
        alert_sent BOOLEAN DEFAULT 0
    );
    """)
    conn.commit()
    conn.close()

def log_audit(action: str, profile: str = None, conversation_uuid: str = None, details: str = None):
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO audit_log (action, profile, conversation_uuid, details) VALUES (?, ?, ?, ?)",
            (action, profile, conversation_uuid, details)
        )
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at", DB_PATH)
