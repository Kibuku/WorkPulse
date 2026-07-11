"""
WorkPulse feedback inbox — the "mother" receiver.

A tiny, self-contained service that RECEIVES pushes from external WorkPulse
instances. It never reaches back out to them: the flow is one-way (external
app → this inbox). You read what lands here on the inbox page.

Two roles, two keys (both from env — nothing secret is baked into the code):
  • INBOX_WRITE_KEY  — external WorkPulse instances present this to push. It is
                       write-only: holding it lets you POST a message, nothing else.
  • INBOX_ADMIN_KEY  — you present this (as the password in the browser prompt)
                       to read the inbox.

Endpoints:
  POST /api/push   — receive one message   (needs X-WorkPulse-Key: <write key>)
  GET  /           — the inbox, newest first (HTTP Basic; password = admin key)
  GET  /api/messages — same list as JSON     (HTTP Basic; password = admin key)
  GET  /health     — liveness probe          (public)

Run locally:
  INBOX_WRITE_KEY=devwrite INBOX_ADMIN_KEY=devadmin INBOX_DB=./inbox.db \
    uvicorn app:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DB_PATH   = Path(os.environ.get("INBOX_DB", "/data/inbox.db"))
WRITE_KEY = os.environ.get("INBOX_WRITE_KEY", "")
ADMIN_KEY = os.environ.get("INBOX_ADMIN_KEY", "")
MAX_BODY  = 64 * 1024          # 64 KB per message — plenty for feedback + diagnostics
KINDS     = {"feedback", "usage", "error"}

app = FastAPI(title="WorkPulse Inbox", docs_url=None, redoc_url=None)


# ── storage ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS message (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          received_at TEXT NOT NULL,
          install_id  TEXT,
          user_label  TEXT,
          kind        TEXT,
          app_version TEXT,
          platform    TEXT,
          ts_client   TEXT,
          message     TEXT,
          diagnostics TEXT
        )
        """
    )
    return con


# ── auth helpers ──────────────────────────────────────────────────────────────

def _match(provided: str, expected: str) -> bool:
    """Constant-time compare that fails closed when the key isn't configured."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _admin_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth[6:]).decode("utf-8", "replace")
    except Exception:
        return False
    _, _, password = raw.partition(":")
    return _match(password, ADMIN_KEY)


def _needs_admin() -> Response:
    return Response(
        "Authentication required.",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="WorkPulse Inbox"'},
    )


# ── ingest ────────────────────────────────────────────────────────────────────

@app.post("/api/push")
async def push(request: Request, x_workpulse_key: str = Header(default="")):
    if not _match(x_workpulse_key, WRITE_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    kind = str(data.get("kind") or "feedback")
    if kind not in KINDS:
        kind = "feedback"
    message = (data.get("message") or "").strip()
    diagnostics = data.get("diagnostics")
    diag_json = (
        json.dumps(diagnostics, ensure_ascii=False)
        if isinstance(diagnostics, (dict, list)) else None
    )
    if not message and diag_json is None:
        return JSONResponse({"error": "nothing to store"}, status_code=400)

    def _s(key: str, limit: int = 200) -> str | None:
        v = data.get(key)
        return str(v)[:limit] if v is not None else None

    con = _db()
    try:
        cur = con.execute(
            """INSERT INTO message
               (received_at, install_id, user_label, kind, app_version,
                platform, ts_client, message, diagnostics)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                _s("install_id"), _s("user_label", 80), kind,
                _s("app_version", 40), _s("platform", 80), _s("ts_client", 40),
                message[:8000], diag_json,
            ),
        )
        con.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        con.close()


# ── read side ─────────────────────────────────────────────────────────────────

def _rows(limit: int = 200) -> list[sqlite3.Row]:
    con = _db()
    try:
        return con.execute(
            "SELECT * FROM message ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        con.close()


@app.get("/api/messages")
def messages(request: Request):
    if not _admin_ok(request):
        return _needs_admin()
    return {"messages": [dict(r) for r in _rows()]}


@app.get("/health")
def health():
    return {"ok": True, "configured": bool(WRITE_KEY and ADMIN_KEY)}


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request):
    if not (WRITE_KEY and ADMIN_KEY):
        return HTMLResponse(_SETUP_HTML, status_code=200)
    if not _admin_ok(request):
        return _needs_admin()
    return HTMLResponse(_render(_rows()))


_KIND_BADGE = {
    "feedback": ("#2563eb", "feedback"),
    "usage":    ("#0e9f6e", "usage"),
    "error":    ("#dc2626", "error"),
}

_SETUP_HTML = """<!doctype html><meta charset=utf-8>
<title>WorkPulse Inbox — setup</title>
<body style="font-family:ui-sans-serif,system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px;color:#2b2622;background:#fbf8f1">
<h1 style="font-weight:650">Inbox not configured</h1>
<p>Set two environment variables and restart:</p>
<pre style="background:#fff;border:1px solid #ece3d2;border-radius:10px;padding:14px;overflow:auto">INBOX_WRITE_KEY   # external WorkPulse instances use this to push
INBOX_ADMIN_KEY   # you use this to read the inbox (browser password)</pre>
<p>Generate strong values with:<br>
<code>python -c "import secrets; print(secrets.token_urlsafe(24))"</code></p>
</body>"""


def _render(rows: list[sqlite3.Row]) -> str:
    cards = []
    for r in rows:
        color, label = _KIND_BADGE.get(r["kind"], ("#6b7280", r["kind"] or "?"))
        meta = " · ".join(
            x for x in (r["user_label"], r["platform"], r["app_version"]) if x
        )
        diag = ""
        if r["diagnostics"]:
            try:
                pretty = json.dumps(json.loads(r["diagnostics"]), indent=2, ensure_ascii=False)
            except Exception:
                pretty = r["diagnostics"]
            diag = (
                "<details><summary>diagnostics</summary>"
                f"<pre>{escape(pretty)}</pre></details>"
            )
        msg = escape(r["message"] or "").replace("\n", "<br>") or "<em class=muted>(no message)</em>"
        cards.append(
            f"""<article class=card>
  <header>
    <span class=badge style="background:{color}">{escape(label)}</span>
    <time>{escape(r["received_at"] or "")}</time>
  </header>
  <div class=meta>{escape(meta) or '<span class=muted>unknown sender</span>'}</div>
  <div class=msg>{msg}</div>
  {diag}
</article>"""
        )
    body = "\n".join(cards) or '<p class=muted>No messages yet.</p>'
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>WorkPulse Inbox</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, -apple-system, system-ui, sans-serif; margin: 0;
          background: #fbf8f1; color: #2b2622; }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 40px 20px 80px; }}
  h1 {{ font-weight: 650; letter-spacing: -.01em; margin: 0 0 4px; }}
  .sub {{ color: #6b6156; margin: 0 0 24px; font-size: 14px; }}
  .card {{ background: #fff; border: 1px solid #ece3d2; border-radius: 12px;
           padding: 14px 16px; margin-bottom: 12px;
           box-shadow: 0 1px 2px rgba(60,48,30,.04); }}
  .card header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }}
  .badge {{ color: #fff; font-size: 11px; font-weight: 700; text-transform: uppercase;
            letter-spacing: .04em; padding: 2px 8px; border-radius: 20px; }}
  time {{ color: #9a8f80; font-size: 12px; font-variant-numeric: tabular-nums; }}
  .meta {{ font-size: 12px; color: #6b6156; margin-bottom: 8px; }}
  .msg {{ font-size: 14.5px; line-height: 1.5; }}
  .muted {{ color: #9a8f80; }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; font-size: 12px; color: #6b6156; }}
  pre {{ background: #f6f1e6; border: 1px solid #ece3d2; border-radius: 8px;
         padding: 10px; overflow-x: auto; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #171512; color: #efe8dc; }}
    .card {{ background: #211e1a; border-color: #332e27; }}
    .sub, .meta, summary {{ color: #b3a692; }}
    time, .muted {{ color: #7d7263; }}
    pre {{ background: #1b1815; border-color: #332e27; }}
  }}
</style></head>
<body><div class=wrap>
  <h1>WorkPulse Inbox</h1>
  <p class=sub>{len(rows)} most recent message{'s' if len(rows) != 1 else ''} · pushed by your WorkPulse instances</p>
  {body}
</div></body></html>"""
