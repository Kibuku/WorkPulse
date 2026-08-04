"""LearningPulse policy, session and enrolled-device engine.

The Classroom vault is physically separate from the Personal WorkPulse atom
store.  The engine reads one explicit, minimal projection of the latest local
signal (app/title/domain) and writes only policy state and policy events into
the Classroom vault.
"""

from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from workpulse.common import ROOT, ensure_dir
from workpulse.core import db as atom_db


# Keep the existing on-disk location through the product-name transition so an
# upgrade does not orphan enrolled devices or session history. A later explicit
# migration may rename the directory after verifying and backing up the vault.
DEFAULT_VAULT = ROOT / "vaults" / "classroom-prototype" / "classroom.db"

DEFAULT_NORMAL_POLICY = {
    "blocked_domains": [
        "malware.test",
        "phishing.test",
        "adult.example",
        "gambling.example",
    ],
    "blocked_apps": [],
    "allowed_domains": [],
    "allowed_apps": [],
}

DEFAULT_CLASS_POLICY = {
    "allowed_domains": [
        "elearning.strathmore.edu",
        "library.strathmore.edu",
        "scholar.google.com",
        "ourworldindata.org",
        "chatgpt.com",
    ],
    "allowed_apps": [
        "Google Chrome",
        "Microsoft Edge",
        "Safari",
        "Microsoft Word",
        "Microsoft PowerPoint",
        "Microsoft Teams",
    ],
    "blocked_domains": [
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "discord.com",
    ],
    "blocked_apps": ["Steam", "Discord"],
}

DEFAULT_EXAM_POLICY = {
    "allowed_domains": ["exam.strathmore.edu"],
    "allowed_apps": ["Google Chrome", "Microsoft Edge", "Safari"],
    "blocked_domains": [],
    "blocked_apps": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    vault = Path(path or DEFAULT_VAULT)
    ensure_dir(vault.parent)
    con = sqlite3.connect(vault)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS classroom_state (
          id               INTEGER PRIMARY KEY CHECK (id = 1),
          mode             TEXT NOT NULL DEFAULT 'normal',
          updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS classroom_session (
          id               TEXT PRIMARY KEY,
          mode             TEXT NOT NULL,
          title            TEXT NOT NULL,
          started_at       TEXT NOT NULL,
          ends_at          TEXT NOT NULL,
          ended_at         TEXT,
          status           TEXT NOT NULL,
          policy_json      TEXT NOT NULL,
          exercise         TEXT NOT NULL DEFAULT '',
          learning_goal    TEXT NOT NULL DEFAULT '',
          ai_use           TEXT NOT NULL DEFAULT 'approved_only'
        );
        CREATE TABLE IF NOT EXISTS classroom_event (
          id               TEXT PRIMARY KEY,
          session_id       TEXT,
          ts               TEXT NOT NULL,
          mode             TEXT NOT NULL,
          event_type       TEXT NOT NULL,
          severity         TEXT NOT NULL,
          action           TEXT NOT NULL,
          app              TEXT,
          title            TEXT,
          domain           TEXT,
          rule             TEXT NOT NULL,
          source_ref       TEXT,
          device_id       TEXT NOT NULL DEFAULT 'Device 01',
          occurrence_count INTEGER NOT NULL DEFAULT 1,
          last_seen        TEXT,
          FOREIGN KEY(session_id) REFERENCES classroom_session(id)
        );
        CREATE INDEX IF NOT EXISTS classroom_event_ts_idx
          ON classroom_event(ts DESC);
        CREATE TABLE IF NOT EXISTS classroom_policy (
          mode             TEXT PRIMARY KEY,
          policy_json      TEXT NOT NULL,
          updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS classroom_pairing (
          code_hash        TEXT PRIMARY KEY,
          expires_at       TEXT NOT NULL,
          used_at          TEXT
        );
        CREATE TABLE IF NOT EXISTS classroom_device (
          id               TEXT PRIMARY KEY,
          name             TEXT NOT NULL,
          platform         TEXT NOT NULL,
          token_hash       TEXT NOT NULL UNIQUE,
          enrolled_at      TEXT NOT NULL,
          last_seen        TEXT,
          last_app         TEXT,
          last_title       TEXT,
          last_domain      TEXT
        );
        CREATE TABLE IF NOT EXISTS classroom_intervention (
          id               TEXT PRIMARY KEY,
          session_id       TEXT NOT NULL,
          device_id        TEXT NOT NULL,
          kind             TEXT NOT NULL,
          message          TEXT NOT NULL,
          created_at       TEXT NOT NULL,
          acknowledged_at  TEXT,
          FOREIGN KEY(session_id) REFERENCES classroom_session(id),
          FOREIGN KEY(device_id) REFERENCES classroom_device(id)
        );
        CREATE TABLE IF NOT EXISTS classroom_session_device (
          session_id       TEXT NOT NULL,
          device_id        TEXT NOT NULL,
          first_seen       TEXT NOT NULL,
          last_seen        TEXT NOT NULL,
          heartbeat_count  INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(session_id, device_id),
          FOREIGN KEY(session_id) REFERENCES classroom_session(id),
          FOREIGN KEY(device_id) REFERENCES classroom_device(id)
        );
        """
    )
    event_columns = {
        row["name"] for row in con.execute("PRAGMA table_info(classroom_event)").fetchall()
    }
    if "device_id" not in event_columns:
        con.execute(
            "ALTER TABLE classroom_event ADD COLUMN device_id TEXT NOT NULL DEFAULT 'Device 01'"
        )
    if "occurrence_count" not in event_columns:
        con.execute(
            "ALTER TABLE classroom_event ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"
        )
    if "last_seen" not in event_columns:
        con.execute("ALTER TABLE classroom_event ADD COLUMN last_seen TEXT")
        con.execute("UPDATE classroom_event SET last_seen=ts WHERE last_seen IS NULL")
    session_columns = {
        row["name"] for row in con.execute("PRAGMA table_info(classroom_session)").fetchall()
    }
    for name, declaration in (
        ("exercise", "TEXT NOT NULL DEFAULT ''"),
        ("learning_goal", "TEXT NOT NULL DEFAULT ''"),
        ("ai_use", "TEXT NOT NULL DEFAULT 'approved_only'"),
    ):
        if name not in session_columns:
            con.execute(f"ALTER TABLE classroom_session ADD COLUMN {name} {declaration}")
    con.execute(
        "INSERT OR IGNORE INTO classroom_state(id, mode, updated_at) VALUES (1, 'normal', ?)",
        (_now(),),
    )
    for mode, policy in (
        ("normal", DEFAULT_NORMAL_POLICY),
        ("class", DEFAULT_CLASS_POLICY),
        ("exam", DEFAULT_EXAM_POLICY),
    ):
        con.execute(
            "INSERT OR IGNORE INTO classroom_policy(mode, policy_json, updated_at) VALUES (?, ?, ?)",
            (mode, json.dumps(policy), _now()),
        )
    con.commit()
    return con


def _policy_for_mode(mode: str, *, path: Path | None = None) -> dict[str, Any]:
    con = connect(path)
    try:
        row = con.execute(
            "SELECT policy_json FROM classroom_policy WHERE mode=?", (mode,)
        ).fetchone()
        if row:
            return json.loads(row["policy_json"])
    finally:
        con.close()
    return dict(DEFAULT_EXAM_POLICY if mode == "exam" else
                DEFAULT_CLASS_POLICY if mode == "class" else DEFAULT_NORMAL_POLICY)


def save_policy(mode: str, policy: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    if mode not in {"normal", "class", "exam"}:
        raise ValueError("unknown policy mode")
    clean: dict[str, list[str]] = {}
    for key in ("allowed_domains", "allowed_apps", "blocked_domains", "blocked_apps"):
        values = policy.get(key) or []
        clean[key] = list(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))
    con = connect(path)
    try:
        con.execute(
            """
            INSERT INTO classroom_policy(mode, policy_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(mode) DO UPDATE SET policy_json=excluded.policy_json,
              updated_at=excluded.updated_at
            """,
            (mode, json.dumps(clean), _now()),
        )
        con.commit()
    finally:
        con.close()
    return {"mode": mode, "policy": clean}


def get_policy(mode: str = "class", *, path: Path | None = None) -> dict[str, Any]:
    return {"mode": mode, "policy": _policy_for_mode(mode, path=path)}


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_pairing(*, minutes: int = 15, path: Path | None = None) -> dict[str, Any]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "-".join(
        "".join(secrets.choice(alphabet) for _ in range(3)) for _ in range(2)
    )
    expires = datetime.now(timezone.utc) + timedelta(minutes=max(2, min(minutes, 60)))
    con = connect(path)
    try:
        con.execute(
            "INSERT INTO classroom_pairing(code_hash, expires_at, used_at) VALUES (?, ?, NULL)",
            (_hash_secret(code), expires.isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {"code": code, "expires_at": expires.isoformat()}


def enroll_device(
    code: str, name: str, platform_name: str, *, path: Path | None = None
) -> dict[str, Any]:
    con = connect(path)
    try:
        row = con.execute(
            "SELECT * FROM classroom_pairing WHERE code_hash=?",
            (_hash_secret(code.strip().upper()),),
        ).fetchone()
        if not row or row["used_at"] or datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            raise ValueError("pairing code is invalid or expired")
        device_id = "Device " + secrets.token_hex(2).upper()
        token = secrets.token_urlsafe(32)
        con.execute(
            """
            INSERT INTO classroom_device
              (id, name, platform, token_hash, enrolled_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (device_id, name.strip() or device_id, platform_name, _hash_secret(token), _now(), _now()),
        )
        con.execute(
            "UPDATE classroom_pairing SET used_at=? WHERE code_hash=?",
            (_now(), row["code_hash"]),
        )
        con.commit()
        return {"device_id": device_id, "device_name": name.strip() or device_id, "token": token}
    finally:
        con.close()


def authenticate_device(token: str, *, path: Path | None = None) -> dict[str, Any] | None:
    con = connect(path)
    try:
        row = con.execute(
            "SELECT * FROM classroom_device WHERE token_hash=?", (_hash_secret(token),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def device_heartbeat(
    token: str, signal: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    device = authenticate_device(token, path=path)
    if not device:
        raise ValueError("device token is invalid")
    projected = {
        "source_ref": str(signal.get("source_ref") or f"agent:{secrets.token_hex(6)}"),
        "ts": str(signal.get("ts") or _now()),
        "app": str(signal.get("app") or ""),
        "title": str(signal.get("title") or ""),
        "domain": str(signal.get("domain") or ""),
        "source": "classroom_agent",
        "device_id": device["id"],
    }
    con = connect(path)
    try:
        con.execute(
            """
            UPDATE classroom_device
            SET last_seen=?, last_app=?, last_title=?, last_domain=?
            WHERE id=?
            """,
            (_now(), projected["app"], projected["title"], projected["domain"], device["id"]),
        )
        con.commit()
    finally:
        con.close()
    decision = evaluate_signal(projected, path=path)
    status = agent_policy(path=path)
    active = status.get("active_session")
    if active:
        seen = _now()
        con = connect(path)
        try:
            con.execute(
                """
                INSERT INTO classroom_session_device
                  (session_id, device_id, first_seen, last_seen, heartbeat_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(session_id, device_id) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  heartbeat_count=classroom_session_device.heartbeat_count+1
                """,
                (active["id"], device["id"], seen, seen),
            )
            con.commit()
        finally:
            con.close()
    status["interventions"] = pending_interventions(
        device["id"], active["id"] if active else None, path=path
    )
    return {"ok": True, "device_id": device["id"], "decision": decision,
            "status": status}


def devices(*, path: Path | None = None) -> list[dict[str, Any]]:
    con = connect(path)
    try:
        rows = con.execute(
            """
            SELECT id, name, platform, enrolled_at, last_seen,
                   last_app, last_title, last_domain
            FROM classroom_device ORDER BY enrolled_at
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def agent_policy(*, path: Path | None = None) -> dict[str, Any]:
    status = session_status(path=path)
    return {
        "mode": status["mode"],
        "active_session": status["active_session"],
        "scheduled_session": status["scheduled_session"],
    }


def send_intervention(
    device_id: str,
    message: str,
    *,
    kind: str = "support",
    path: Path | None = None,
) -> dict[str, Any]:
    """Persist a facilitator action for one real enrolled device."""
    clean_message = message.strip()
    if not clean_message:
        raise ValueError("message is required")
    if kind not in {"support", "resource", "policy_update"}:
        raise ValueError("unknown intervention type")
    con = connect(path)
    try:
        active = _active_session(con)
        if not active:
            raise ValueError("an active learning session is required")
        device = con.execute(
            "SELECT id, name FROM classroom_device WHERE id=?", (device_id,)
        ).fetchone()
        if not device:
            raise ValueError("device is not enrolled")
        item = {
            "id": uuid.uuid4().hex,
            "session_id": active["id"],
            "device_id": device["id"],
            "device_name": device["name"],
            "kind": kind,
            "message": clean_message,
            "created_at": _now(),
            "acknowledged_at": None,
        }
        con.execute(
            """
            INSERT INTO classroom_intervention
              (id, session_id, device_id, kind, message, created_at, acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (item["id"], item["session_id"], item["device_id"], item["kind"],
             item["message"], item["created_at"]),
        )
        con.commit()
        return item
    finally:
        con.close()


def pending_interventions(
    device_id: str,
    session_id: str | None = None,
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    if not session_id:
        return []
    con = connect(path)
    try:
        rows = con.execute(
            """
            SELECT id, session_id, device_id, kind, message, created_at
            FROM classroom_intervention
            WHERE session_id=? AND device_id=? AND acknowledged_at IS NULL
            ORDER BY created_at
            """,
            (session_id, device_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def acknowledge_intervention(
    token: str, intervention_id: str, *, path: Path | None = None
) -> dict[str, Any]:
    device = authenticate_device(token, path=path)
    if not device:
        raise ValueError("device token is invalid")
    con = connect(path)
    try:
        row = con.execute(
            "SELECT * FROM classroom_intervention WHERE id=? AND device_id=?",
            (intervention_id, device["id"]),
        ).fetchone()
        if not row:
            raise ValueError("intervention was not found for this device")
        acknowledged = row["acknowledged_at"] or _now()
        con.execute(
            "UPDATE classroom_intervention SET acknowledged_at=? WHERE id=?",
            (acknowledged, intervention_id),
        )
        con.commit()
        return {"ok": True, "id": intervention_id, "acknowledged_at": acknowledged}
    finally:
        con.close()


def start_session(
    *,
    mode: str,
    title: str,
    duration_minutes: int = 60,
    policy: dict[str, Any] | None = None,
    starts_at: str | None = None,
    exercise: str = "",
    learning_goal: str = "",
    ai_use: str = "approved_only",
    path: Path | None = None,
) -> dict[str, Any]:
    if mode not in {"class", "exam"}:
        raise ValueError("mode must be class or exam")
    if ai_use not in {"prohibited", "approved_only", "brainstorm", "attributed", "open"}:
        raise ValueError("unknown AI-use rule")
    duration = max(1, min(int(duration_minutes), 480))
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(starts_at) if starts_at else now
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    started = started.astimezone(timezone.utc)
    ends = started + timedelta(minutes=duration)
    session_state = "scheduled" if started > now else "active"
    sid = uuid.uuid4().hex
    actual_policy = _policy_for_mode(mode, path=path)
    if policy:
        for key in ("allowed_domains", "allowed_apps", "blocked_domains", "blocked_apps"):
            if key in policy:
                actual_policy[key] = [str(x).strip() for x in policy[key] if str(x).strip()]
    con = connect(path)
    try:
        con.execute(
            "UPDATE classroom_session SET status='ended', ended_at=? WHERE status IN ('active','scheduled')",
            (_now(),),
        )
        con.execute(
            """
            INSERT INTO classroom_session
              (id, mode, title, started_at, ends_at, ended_at, status, policy_json,
               exercise, learning_goal, ai_use)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (sid, mode, title.strip() or f"{mode.title()} session",
             started.isoformat(), ends.isoformat(), session_state, json.dumps(actual_policy),
             exercise.strip(), learning_goal.strip(), ai_use),
        )
        state_mode = mode if session_state == "active" else "normal"
        con.execute("UPDATE classroom_state SET mode=?, updated_at=? WHERE id=1", (state_mode, _now()))
        con.commit()
        return session_status(path=path)
    finally:
        con.close()


def end_session(*, path: Path | None = None) -> dict[str, Any]:
    con = connect(path)
    try:
        con.execute(
            "UPDATE classroom_session SET status='ended', ended_at=? WHERE status='active'",
            (_now(),),
        )
        con.execute("UPDATE classroom_state SET mode='normal', updated_at=? WHERE id=1", (_now(),))
        con.commit()
        return session_status(path=path)
    finally:
        con.close()


def _active_session(con: sqlite3.Connection) -> sqlite3.Row | None:
    scheduled = con.execute(
        "SELECT * FROM classroom_session WHERE status='scheduled' ORDER BY started_at LIMIT 1"
    ).fetchone()
    if scheduled and datetime.fromisoformat(scheduled["started_at"]) <= datetime.now(timezone.utc):
        con.execute("UPDATE classroom_session SET status='active' WHERE id=?", (scheduled["id"],))
        con.execute(
            "UPDATE classroom_state SET mode=?, updated_at=? WHERE id=1",
            (scheduled["mode"], _now()),
        )
        con.commit()
    row = con.execute(
        "SELECT * FROM classroom_session WHERE status='active' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row and datetime.fromisoformat(row["ends_at"]) <= datetime.now(timezone.utc):
        con.execute(
            "UPDATE classroom_session SET status='ended', ended_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        con.execute("UPDATE classroom_state SET mode='normal', updated_at=? WHERE id=1", (_now(),))
        con.commit()
        return None
    return row


def session_status(*, path: Path | None = None) -> dict[str, Any]:
    con = connect(path)
    try:
        row = _active_session(con)
        state = con.execute("SELECT mode, updated_at FROM classroom_state WHERE id=1").fetchone()
        events = [
            dict(r) for r in con.execute(
                """
                SELECT MIN(id) AS id, session_id, MIN(ts) AS ts, mode,
                       event_type, MAX(severity) AS severity, action, app,
                       MAX(title) AS title, domain, rule,
                       MAX(source_ref) AS source_ref, device_id,
                       SUM(occurrence_count) AS occurrence_count,
                       MAX(COALESCE(last_seen, ts)) AS last_seen
                FROM classroom_event
                GROUP BY session_id, device_id, app, domain, rule
                ORDER BY last_seen DESC
                LIMIT 20
                """
            ).fetchall()
        ]
        active = None
        if row:
            active = dict(row)
            active["policy"] = json.loads(active.pop("policy_json"))
        scheduled_row = con.execute(
            "SELECT * FROM classroom_session WHERE status='scheduled' ORDER BY started_at LIMIT 1"
        ).fetchone()
        scheduled = None
        if scheduled_row:
            scheduled = dict(scheduled_row)
            scheduled["policy"] = json.loads(scheduled.pop("policy_json"))
        return {
            "mode": state["mode"] if state else "normal",
            "updated_at": state["updated_at"] if state else _now(),
            "active_session": active,
            "scheduled_session": scheduled,
            "events": events,
            "vault": "classroom",
        }
    finally:
        con.close()


def session_report(session_id: str | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Return factual session evidence. No learner score or misconduct claim."""
    con = connect(path)
    try:
        if session_id:
            session = con.execute(
                "SELECT * FROM classroom_session WHERE id=?", (session_id,)
            ).fetchone()
        else:
            session = con.execute(
                """
                SELECT * FROM classroom_session
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'ended' THEN 1 ELSE 2 END,
                         started_at DESC LIMIT 1
                """
            ).fetchone()
        if not session:
            return {"session": None, "events": [], "interventions": [], "devices": []}
        session_data = dict(session)
        session_data["policy"] = json.loads(session_data.pop("policy_json"))
        events = [dict(row) for row in con.execute(
            """
            SELECT device_id, app, domain, rule, severity,
                   SUM(occurrence_count) AS occurrences,
                   MIN(ts) AS first_seen, MAX(COALESCE(last_seen, ts)) AS last_seen
            FROM classroom_event WHERE session_id=?
            GROUP BY device_id, app, domain, rule, severity
            ORDER BY last_seen DESC
            """,
            (session["id"],),
        ).fetchall()]
        interventions = [dict(row) for row in con.execute(
            """
            SELECT i.id, i.device_id, d.name AS device_name, i.kind, i.message,
                   i.created_at, i.acknowledged_at
            FROM classroom_intervention i
            JOIN classroom_device d ON d.id=i.device_id
            WHERE i.session_id=? ORDER BY i.created_at
            """,
            (session["id"],),
        ).fetchall()]
        devices = [dict(row) for row in con.execute(
            """
            SELECT d.id, d.name, d.platform, sd.first_seen, sd.last_seen,
                   sd.heartbeat_count
            FROM classroom_session_device sd
            JOIN classroom_device d ON d.id=sd.device_id
            WHERE sd.session_id=? ORDER BY d.name
            """,
            (session["id"],),
        ).fetchall()]
        return {
            "session": session_data,
            "events": events,
            "interventions": interventions,
            "devices": devices,
            "summary": {
                "devices_observed": len(devices),
                "policy_exceptions": sum(int(item["occurrences"] or 0) for item in events),
                "facilitator_actions": len(interventions),
                "acknowledged_actions": sum(bool(item["acknowledged_at"]) for item in interventions),
            },
        }
    finally:
        con.close()


def apply_retention(
    *,
    now: datetime | None = None,
    raw_hours: int = 24,
    detail_days: int = 100,
    path: Path | None = None,
) -> dict[str, int]:
    """Expire heavy learning evidence while keeping declared session markers.

    Window titles and source references are transient. Detailed exceptions,
    participation heartbeats, and facilitator messages have a bounded review
    period. The session declaration (exercise, goal, AI rule, policy and times)
    remains as the compact institutional marker.
    """
    if raw_hours < 1 or detail_days < 1:
        raise ValueError("retention windows must be positive")
    current = now or datetime.now(timezone.utc)
    raw_cutoff = (current - timedelta(hours=raw_hours)).isoformat()
    detail_cutoff = (current - timedelta(days=detail_days)).isoformat()
    con = connect(path)
    try:
        raw = con.execute(
            """
            SELECT COUNT(*) FROM classroom_event
            WHERE COALESCE(last_seen, ts) < ? AND (title != '' OR source_ref != '')
            """, (raw_cutoff,),
        ).fetchone()[0]
        event_count = con.execute(
            "SELECT COUNT(*) FROM classroom_event WHERE COALESCE(last_seen, ts) < ?",
            (detail_cutoff,),
        ).fetchone()[0]
        intervention_count = con.execute(
            "SELECT COUNT(*) FROM classroom_intervention WHERE created_at < ?",
            (detail_cutoff,),
        ).fetchone()[0]
        participation_count = con.execute(
            "SELECT COUNT(*) FROM classroom_session_device WHERE last_seen < ?",
            (detail_cutoff,),
        ).fetchone()[0]
        con.execute("BEGIN")
        con.execute(
            "UPDATE classroom_event SET title='', source_ref='' WHERE COALESCE(last_seen, ts) < ?",
            (raw_cutoff,),
        )
        con.execute(
            "DELETE FROM classroom_event WHERE COALESCE(last_seen, ts) < ?",
            (detail_cutoff,),
        )
        con.execute(
            "DELETE FROM classroom_intervention WHERE created_at < ?",
            (detail_cutoff,),
        )
        con.execute(
            "DELETE FROM classroom_session_device WHERE last_seen < ?",
            (detail_cutoff,),
        )
        con.execute("DELETE FROM classroom_pairing WHERE expires_at < ?", (current.isoformat(),))
        con.execute("COMMIT")
        return {
            "raw_evidence_redacted": int(raw),
            "events_expired": int(event_count),
            "interventions_expired": int(intervention_count),
            "participation_expired": int(participation_count),
        }
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def latest_signal() -> dict[str, Any] | None:
    """Return the freshest minimal signal projection from Personal atoms."""
    con = atom_db.connect()
    try:
        browser = con.execute(
            """
            SELECT b.id, b.ts, b.app, b.domain, bl.raw_title
            FROM browser_visit b
            LEFT JOIN browser_visit_local bl ON bl.visit_id=b.id
            ORDER BY b.ts DESC LIMIT 1
            """
        ).fetchone()
        session = con.execute(
            """
            SELECT s.id, s.started_at AS ts, s.app, sl.raw_title
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id=s.id
            ORDER BY s.started_at DESC LIMIT 1
            """
        ).fetchone()
        candidates: list[dict[str, Any]] = []
        if browser:
            candidates.append({
                "source_ref": f"browser:{browser['id']}",
                "ts": browser["ts"],
                "app": browser["app"],
                "title": browser["raw_title"] or "",
                "domain": browser["domain"] or "",
                "source": "browser",
                "device_id": "Device 01",
            })
        if session:
            candidates.append({
                "source_ref": f"session:{session['id']}",
                "ts": session["ts"],
                "app": session["app"],
                "title": session["raw_title"] or "",
                "domain": "",
                "source": "window",
                "device_id": "Device 01",
            })
        if not candidates:
            return None
        return max(candidates, key=lambda item: item["ts"] or "")
    finally:
        con.close()


def _domain_matches(domain: str, candidate: str) -> bool:
    domain = domain.lower().strip(".")
    candidate = candidate.lower().strip(".")
    return domain == candidate or domain.endswith("." + candidate)


def evaluate_signal(
    signal: dict[str, Any] | None = None,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    signal = signal or latest_signal()
    status = session_status(path=path)
    mode = status["mode"]
    active = status["active_session"]
    policy = active["policy"] if active else _policy_for_mode("normal", path=path)
    if not signal:
        return {"decision": "no_signal", "mode": mode, "event": None, "signal": None}

    app = str(signal.get("app") or "")
    title = str(signal.get("title") or "")
    domain = str(signal.get("domain") or "")
    decision = "allowed"
    severity = "information"
    rule = "No restrictive rule matched"

    blocked_apps = policy.get("blocked_apps", [])
    blocked_domains = policy.get("blocked_domains", [])
    allowed_apps = policy.get("allowed_apps", [])
    allowed_domains = policy.get("allowed_domains", [])
    ai_domains = ("chatgpt.com", "claude.ai", "gemini.google.com", "copilot.microsoft.com",
                  "deepseek.com", "mistral.ai")
    ai_use = str((active or {}).get("ai_use") or "approved_only")

    if domain and ai_use == "prohibited" and any(_domain_matches(domain, x) for x in ai_domains):
        decision, severity, rule = "policy_event", "attention", \
            f"AI use is not permitted for this exercise ({domain})"
    elif any(app.lower() == x.lower() for x in blocked_apps):
        decision, severity, rule = "policy_event", "attention", f"Application {app} is restricted"
    elif domain and any(_domain_matches(domain, x) for x in blocked_domains):
        decision, severity, rule = "policy_event", "attention", f"Domain {domain} is restricted"
    elif mode in {"class", "exam"}:
        domain_allowed = bool(domain) and any(_domain_matches(domain, x) for x in allowed_domains)
        app_allowed = any(app.lower() == x.lower() for x in allowed_apps)
        if domain and not domain_allowed:
            decision = "policy_event"
            severity = "urgent" if mode == "exam" else "attention"
            rule = f"Domain {domain} is outside the {mode} allowlist"
        elif not domain and allowed_apps and not app_allowed:
            decision = "policy_event"
            severity = "urgent" if mode == "exam" else "attention"
            rule = f"Application {app} is outside the {mode} allowlist"

    event = None
    if decision == "policy_event":
        event = _record_event(
            mode=mode,
            session_id=active["id"] if active else None,
            severity=severity,
            app=app,
            title=title,
            domain=domain,
            rule=rule,
            source_ref=str(signal.get("source_ref") or ""),
            device_id=str(signal.get("device_id") or "Device 01"),
            path=path,
        )
    return {"decision": decision, "mode": mode, "event": event, "signal": signal, "rule": rule}


def _record_event(
    *,
    mode: str,
    session_id: str | None,
    severity: str,
    app: str,
    title: str,
    domain: str,
    rule: str,
    source_ref: str,
    device_id: str,
    path: Path | None,
) -> dict[str, Any]:
    con = connect(path)
    try:
        duplicate = con.execute(
            """
            SELECT * FROM classroom_event
            WHERE session_id IS ? AND device_id=? AND app=? AND domain=? AND rule=?
            ORDER BY ts DESC LIMIT 1
            """,
            (session_id, device_id, app, domain, rule),
        ).fetchone()
        if duplicate:
            seen = _now()
            con.execute(
                """
                UPDATE classroom_event
                SET occurrence_count=occurrence_count+1, last_seen=?, source_ref=?
                WHERE id=?
                """,
                (seen, source_ref, duplicate["id"]),
            )
            con.commit()
            return dict(con.execute(
                "SELECT * FROM classroom_event WHERE id=?", (duplicate["id"],)
            ).fetchone())
        event = {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "ts": _now(),
            "mode": mode,
            "event_type": "policy_event",
            "severity": severity,
            "action": "flagged",
            "app": app,
            "title": title[:240],
            "domain": domain,
            "rule": rule,
            "source_ref": source_ref,
            "device_id": device_id,
            "occurrence_count": 1,
            "last_seen": _now(),
        }
        con.execute(
            """
            INSERT INTO classroom_event
              (id, session_id, ts, mode, event_type, severity, action,
               app, title, domain, rule, source_ref, device_id,
               occurrence_count, last_seen)
            VALUES (:id, :session_id, :ts, :mode, :event_type, :severity,
                    :action, :app, :title, :domain, :rule, :source_ref, :device_id,
                    :occurrence_count, :last_seen)
            """,
            event,
        )
        con.commit()
        return event
    finally:
        con.close()
