"""
discovery.py — propose the project taxonomy from raw signal (plan U2, KTD2/KTD4).

No hand-authored keywords. Reads distinct raw window titles, groups them by
their dominant shared token, names each group, and upserts the results into the
`project` table as `candidate` rows. The deterministic path here is the
local-first floor that always works with no LLM and no network; when a richer
semantic layer is available it refines these candidates (naming, client
detection) but never has to exist for the backbone to function.

# ponytail: token-frequency grouping is a heuristic floor, not clustering.
# Upgrade path: sqlite-vec embedding similarity + optional LLM naming (KTD4),
# layered over this same candidate-upsert without changing the schema.

Public API:
    discover_projects(con, cfg=None, *, min_evidence=2) -> list[dict]
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from workpulse.core import atoms
from workpulse.core.name_clusters import _tokens

# A client prefix convention seen in real filenames: "Client_Project ...".
_CLIENT_SEP = "_"
# Tokens appearing in more than this fraction of all titles are too generic to
# key a project group on (app names, "report", …).
_UBIQUITOUS_FRACTION = 0.8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _distinct_titles(con: sqlite3.Connection) -> list[tuple[str, int]]:
    # Drop titles that are just the app's own name (Terminal, Finder, "Browser",
    # a bare "Claude") — they carry no project signal and would otherwise form
    # app-shaped "projects". The generic-window denylist catches the rest.
    rows = con.execute(
        "SELECT sl.raw_title AS t, COUNT(*) AS n "
        "FROM session s JOIN session_local sl ON sl.session_id = s.id "
        "WHERE sl.raw_title IS NOT NULL AND sl.raw_title <> '' "
        "  AND LOWER(TRIM(sl.raw_title)) <> LOWER(TRIM(s.app)) "
        "GROUP BY sl.raw_title"
    ).fetchall()
    return [(r["t"], r["n"]) for r in rows
            if r["t"].strip().lower() not in _GENERIC_TITLES]


# Window titles that name a surface, not a project.
_GENERIC_TITLES = frozenset({
    "browser", "window", "loginwindow", "untitled", "new tab", "calendar",
})


def _document_frequency(titles: list[tuple[str, int]]) -> Counter:
    df: Counter = Counter()
    for t, _ in titles:
        for tok in set(_tokens(t)):
            df[tok] += 1
    return df


def _group_key(title: str, df: Counter, ubiquitous: set[str]) -> str | None:
    """The dominant shared token: highest corpus frequency, skipping tokens so
    common they'd merge unrelated work. Ties break alphabetically for stability.
    """
    toks = [tk for tk in set(_tokens(title)) if tk not in ubiquitous]
    if not toks:
        toks = list(set(_tokens(title)))  # everything ubiquitous — fall back
    if not toks:
        return None
    return max(toks, key=lambda tk: (df.get(tk, 0), tk))


def _client_of(title: str) -> str | None:
    """Best-effort two-level split: "Client_Project ..." filenames carry the
    client before the first underscore. Accept only a short, capitalized prefix.
    """
    if _CLIENT_SEP not in title:
        return None
    head = title.split(_CLIENT_SEP, 1)[0].strip()
    # Reject filename slugs: hyphenated runs, over-long heads, or all-caps codes.
    if "-" in head or len(head) > 30:
        return None
    words = head.split()
    if 1 <= len(words) <= 3 and all(w[:1].isupper() for w in words if w):
        return head
    return None


def _name_group(members: list[tuple[str, int]]) -> str:
    bag: Counter = Counter()
    for t, n in members:
        for tok in _tokens(t):
            bag[tok] += n
    top = [w.capitalize() for w, _ in bag.most_common(4) if len(w) >= 3][:3]
    return " ".join(top) if top else "Untitled Project"


def discover_projects(con: sqlite3.Connection, cfg: dict | None = None,
                      *, min_evidence: int = 2) -> list[dict]:
    titles = _distinct_titles(con)
    if not titles:
        return []
    df = _document_frequency(titles)
    cutoff = _UBIQUITOUS_FRACTION * len(titles)
    ubiquitous = {tok for tok, c in df.items() if c > cutoff}

    groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for t, n in titles:
        key = _group_key(t, df, ubiquitous)
        if key is not None:
            groups[key].append((t, n))

    candidates: list[dict] = []
    for members in groups.values():
        evidence = sum(n for _, n in members)
        if evidence < min_evidence:
            continue
        clients = Counter(c for c in (_client_of(t) for t, _ in members) if c)
        candidates.append({
            "client": clients.most_common(1)[0][0] if clients else None,
            "name": _name_group(members),
            "confidence": round(min(1.0, evidence / (evidence + 3.0)), 3),
            "evidence": evidence,
        })

    _upsert_candidates(con, candidates)
    return candidates


def refine_taxonomy(con: sqlite3.Connection, cfg: dict | None = None) -> dict:
    """LLM cleanup/naming of the candidate taxonomy (plan U3, R7, KTD7).

    Merges duplicate candidates and renames noisy ones using the `discovery`
    feature's provider. Each name/client is redacted before it leaves the
    machine (R12). No provider configured -> no-op, deterministic names stand
    (R4). Only `candidate` rows are ever touched; a bad response is a no-op.
    """
    from workpulse.core import llm, content_capture
    backend, _ = llm._resolve_route(cfg, "discovery")
    if backend == "none":
        return {"renamed": 0, "merged": 0}
    cands = con.execute(
        "SELECT id, client, name FROM project WHERE status = 'candidate'").fetchall()
    if not cands:
        return {"renamed": 0, "merged": 0}
    lines = [f"{c['id']}: {content_capture.redact((c['client'] or '') + ' | ' + c['name'])}"
             for c in cands]
    prompt = (
        "These are candidate project names auto-discovered from window titles. "
        "Merge duplicates and give each a clean 'Client - Project' name. Reply "
        'ONLY as JSON: {"rename":[{"id","name","client"}],"merge":[{"from","into"}]}.\n'
        + "\n".join(lines))
    obj, _meta = llm.ask_json(prompt, feature="discovery", cfg=cfg)
    if not isinstance(obj, dict):
        return {"renamed": 0, "merged": 0}

    renamed = merged = 0
    con.execute("BEGIN")
    try:
        for r in (obj.get("rename") or []):
            pid, new = r.get("id"), r.get("name")
            if not pid or not new:
                continue
            cur = con.execute(
                "UPDATE project SET name = ?, client = ? "
                "WHERE id = ? AND status = 'candidate'",
                (new, r.get("client"), pid))
            renamed += cur.rowcount
        for m in (obj.get("merge") or []):
            frm = m.get("from")
            if not frm:
                continue
            cur = con.execute(
                "UPDATE project SET status = 'dismissed' "
                "WHERE id = ? AND status = 'candidate'", (frm,))
            merged += cur.rowcount
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"renamed": renamed, "merged": merged}


def _upsert_candidates(con: sqlite3.Connection, candidates: list[dict]) -> None:
    con.execute("BEGIN")
    try:
        for c in candidates:
            # Dedupe on (client, name); never disturb a confirmed/dismissed row (KTD5).
            row = con.execute(
                "SELECT id, status FROM project "
                "WHERE IFNULL(client,'') = IFNULL(?, '') AND name = ?",
                (c["client"], c["name"]),
            ).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO project(id, client, name, status, confidence, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (atoms.new_id(), c["client"], c["name"], "candidate",
                     c["confidence"], _now_iso()),
                )
            elif row["status"] == "candidate":
                con.execute("UPDATE project SET confidence = ? WHERE id = ?",
                            (c["confidence"], row["id"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
