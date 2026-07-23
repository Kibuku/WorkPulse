"""
cluster_context.py — enrich a cluster with git + captures + skill_runs.

The framework principle being load-bearing here: a `session` atom records
WHAT app was foreground, not WHAT WORK happened. "Claude 38 min" is true
but unhelpful. The work was building WorkPulse — visible in git, captures,
and skill_runs over the same time range.

This module joins those side channels deterministically. No LLM. The
output feeds the dashboard's Today card, the report fallback, and the
report LLM packet so the model has substance to synthesize from.

Public API:
    cluster_context(con, *, cluster_id=None, started_at=None,
                    ended_at=None, repos=None) -> dict
    git_commits_between(start, end, repo) -> list[dict]
    summary_line(context) -> str    # one-line human-readable

Result shape:
    {
      "captures":   [{ts, body, author, pinned_kind, pinned_id}, ...],
      "git_commits": [{repo, sha, ts, subject, files_count}, ...],
      "skill_runs": [{slug, count, total_in_tokens, total_out_tokens}, ...],
      "file_events": {"total": int, "by_extension": [(ext, n), ...]},
      "summary":     str,           # the one-line synthesis
    }
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from workpulse.common import ROOT, load_config


# ── git ──────────────────────────────────────────────────────────────────────

def git_commits_between(start_iso: str, end_iso: str,
                        repo: str | Path = ROOT) -> list[dict]:
    """Read commits from `repo` whose author date is in [start, end]. Empty
    list on failure — git not installed, not a repo, no commits, etc."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        return []
    try:
        # %H = sha, %aI = author date ISO, %s = subject, %an = author
        r = subprocess.run(
            ["git", "-C", str(repo), "log",
             f"--since={start_iso}", f"--until={end_iso}",
             "--format=%H|%aI|%an|%s", "--all"],
            capture_output=True, text=True, timeout=8,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if r.returncode != 0:
        return []
    out: list[dict] = []
    for line in r.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, ts, author, subject = parts
        # Files-touched count is a separate cheap call per commit; only do it
        # for the top few to keep the budget tight.
        out.append({
            "repo":    repo.name,
            "sha":     sha[:10],
            "ts":      ts,
            "author":  author,
            "subject": subject,
        })
    # Annotate top 6 commits with files-touched count
    for c in out[:6]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "diff-tree",
                 "--no-commit-id", "--name-only", "-r", c["sha"]],
                capture_output=True, text=True, timeout=4,
            )
            c["files_count"] = len([l for l in r.stdout.splitlines() if l])
        except (subprocess.SubprocessError, FileNotFoundError):
            c["files_count"] = None
    return out


# ── range resolution ────────────────────────────────────────────────────────

def _cluster_range(con: sqlite3.Connection, cluster_id: str
                   ) -> tuple[str, str] | None:
    row = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    if not row:
        return None
    return row["started_at"], row["ended_at"]


# ── side-channel joins ──────────────────────────────────────────────────────

def _captures_in(con: sqlite3.Connection, start: str, end: str,
                 cluster_id: str | None) -> list[dict]:
    """Captures pinned to a session in this cluster, OR whose timestamp falls
    in the cluster's range. Either signal counts."""
    if cluster_id:
        # Sessions in the cluster
        session_ids = [r["id"] for r in con.execute(
            "SELECT id FROM session WHERE cluster_id = ?", (cluster_id,)
        )]
        if session_ids:
            qmarks = ",".join("?" * len(session_ids))
            rows = con.execute(
                f"""
                SELECT id, ts, author, body, pinned_kind, pinned_id
                FROM capture
                WHERE (pinned_kind = 'session' AND pinned_id IN ({qmarks}))
                   OR (ts BETWEEN ? AND ?)
                ORDER BY ts
                """,
                [*session_ids, start, end],
            ).fetchall()
            return [dict(r) for r in rows]
    rows = con.execute(
        """
        SELECT id, ts, author, body, pinned_kind, pinned_id
        FROM capture WHERE ts BETWEEN ? AND ? ORDER BY ts
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _skill_runs_in(con: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT skill_slug AS slug,
               COUNT(*) AS count,
               SUM(in_tokens)  AS total_in_tokens,
               SUM(out_tokens) AS total_out_tokens,
               SUM(cost_usd)   AS total_cost_usd
        FROM skill_run
        WHERE ts BETWEEN ? AND ?
        GROUP BY skill_slug
        ORDER BY count DESC
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _file_events_in(con: sqlite3.Connection, start: str, end: str) -> dict:
    """File events the watcher caught in this range. Public table only sees
    the path_hash; we look at session_local-linked raw paths via the
    file_event_local table for the extension breakdown."""
    rows = con.execute(
        """
        SELECT fe.kind, COALESCE(fl.raw_path, '') AS raw_path
        FROM file_event fe
        LEFT JOIN file_event_local fl ON fl.file_event_id = fe.id
        WHERE fe.ts BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchall()
    by_ext: Counter = Counter()
    for r in rows:
        path = r["raw_path"] or ""
        if not path:
            continue
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "(no-ext)"
        by_ext[ext] += 1
    return {
        "total": len(rows),
        "by_extension": by_ext.most_common(5),
    }


def _session_titles_in(con: sqlite3.Connection, cluster_id: str | None,
                       start: str, end: str) -> list[dict]:
    """Most-seen foreground titles, retained locally for output inference."""
    if cluster_id:
        rows = con.execute(
            """
            SELECT s.app, sl.raw_title AS title, COUNT(*) AS count
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id = s.id
            WHERE s.cluster_id = ?
              AND sl.raw_title IS NOT NULL AND sl.raw_title <> ''
            GROUP BY s.app, sl.raw_title
            ORDER BY count DESC
            LIMIT 12
            """,
            (cluster_id,),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT s.app, sl.raw_title AS title, COUNT(*) AS count
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id = s.id
            WHERE s.started_at BETWEEN ? AND ?
              AND sl.raw_title IS NOT NULL AND sl.raw_title <> ''
            GROUP BY s.app, sl.raw_title
            ORDER BY count DESC
            LIMIT 12
            """,
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def _calendar_events_in(con: sqlite3.Connection, start: str,
                        end: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT cel.raw_title AS title, ce.stream
        FROM calendar_event ce
        JOIN calendar_event_local cel ON cel.event_id = ce.id
        WHERE ce.started_at < ? AND ce.ended_at > ?
          AND cel.raw_title IS NOT NULL AND cel.raw_title <> ''
        ORDER BY ce.started_at
        """,
        (end, start),
    ).fetchall()
    return [dict(r) for r in rows]


_GENERIC_TITLES = {
    "chatgpt", "claude", "safari", "google chrome", "microsoft edge",
    "microsoft teams", "msteams", "microsoft outlook", "outlook",
    "microsoft word", "microsoft powerpoint", "microsoft excel", "code",
    "visual studio code", "dashboard", "getting started", "untitled",
    "system settings", "accessibility", "computer use controls",
    "usernotificationcenter", "whatsapp", "inbox",
}


def _clean_work_title(value: str) -> str:
    title = " ".join((value or "").replace("_", " ").split())
    title = re.sub(r"\s+[|•]\s+[^|•]+(?:\s+[|•]\s+[^|•]+)*$", "", title)
    title = re.sub(r"\s*[-–—]\s*(Microsoft (?:Word|Excel|PowerPoint)|"
                   r"Google Chrome|Safari|ChatGPT|Claude)$", "", title,
                   flags=re.IGNORECASE)
    return title.strip(" -–—|•")


def infer_output(ctx: dict, assigned_label: str | None = None) -> dict:
    """Choose the clearest local evidence of what moved forward.

    This is intentionally deterministic. It explains the chosen evidence and
    never invents an output when the only evidence is a generic application
    title.
    """
    titles = ctx.get("session_titles") or []
    total_title_samples = sum(int(item.get("count") or 0) for item in titles)
    code_samples = sum(
        int(item.get("count") or 0) for item in titles
        if (item.get("app") or "").casefold() in {
            "code", "visual studio code", "xcode", "pycharm",
            "intellij idea", "android studio",
        }
    )
    # A commit can be created by a parallel AI agent while the person's
    # foreground attention is elsewhere. Treat it as the output name only
    # when coding was materially present in this attention block.
    commit_is_attention_signal = (
        not titles
        or code_samples >= max(3, round(total_title_samples * 0.15))
    )
    commits = ctx.get("git_commits") or []
    if commits and commit_is_attention_signal:
        title = _clean_work_title(commits[0].get("subject") or "")
        if title:
            return {"title": title[:100], "evidence_kind": "commit",
                    "evidence_label": "Git commit", "specific": True}

    captures = [c for c in (ctx.get("captures") or [])
                if c.get("author") == "human" and c.get("body")]
    if captures:
        title = _clean_work_title(captures[-1]["body"])
        if title:
            return {"title": title[:100], "evidence_kind": "capture",
                    "evidence_label": "Your capture", "specific": True}

    events = ctx.get("calendar_events") or []
    if events:
        title = _clean_work_title(events[0].get("title") or "")
        if title and title.casefold() not in _GENERIC_TITLES:
            return {"title": title[:100], "evidence_kind": "calendar",
                    "evidence_label": "Calendar overlap", "specific": True}

    candidates = []
    for item in titles:
        title = _clean_work_title(item.get("title") or "")
        app = _clean_work_title(item.get("app") or "")
        folded = title.casefold().lstrip("\u200e")
        if (not title or folded in _GENERIC_TITLES
                or folded == app.casefold()
                or len(re.findall(r"[A-Za-z]{3,}", title)) < 2):
            continue
        score = int(item.get("count") or 0)
        # Artefact names are stronger than browser navigation/chrome titles.
        if re.search(r"\.(?:docx?|xlsx?|pptx?|pdf|md)\b", title, re.I):
            score += 20
        if app.casefold() in {"microsoft powerpoint", "microsoft word",
                              "microsoft excel", "code"}:
            score += 10
        candidates.append((score, title))
    if candidates:
        title = max(candidates, key=lambda x: x[0])[1]
        return {"title": title[:100], "evidence_kind": "foreground_title",
                "evidence_label": "Active document or page", "specific": True}

    label = (assigned_label or "").strip()
    if label and label != "Needs review":
        return {"title": f"{label} work block", "evidence_kind": "project",
                "evidence_label": "Project attribution", "specific": False}
    return {"title": "Unclear work block", "evidence_kind": "insufficient",
            "evidence_label": "Needs your review", "specific": False}


# ── summary one-liner ───────────────────────────────────────────────────────

def summary_line(ctx: dict) -> str:
    """Compose a single human-readable line from a context dict. Used by
    both the dashboard card and the report fallback so they speak the
    same shape."""
    parts: list[str] = []

    git = ctx.get("git_commits") or []
    if git:
        # Lead with the first commit's subject if it's specific
        head = git[0]["subject"]
        if len(head) > 60:
            head = head[:60] + "…"
        if len(git) == 1:
            parts.append(head)
        else:
            parts.append(f"{head} (+{len(git) - 1} more commits)")

    captures = ctx.get("captures") or []
    human_caps = [c for c in captures if c["author"] == "human"]
    sys_caps   = [c for c in captures if c["author"] == "system"]
    if human_caps:
        parts.append(f"{len(human_caps)} capture{'' if len(human_caps) == 1 else 's'}")
    elif sys_caps:
        # System captures are less informative — only mention them if there's
        # nothing else interesting to show.
        if not parts:
            parts.append(f"{len(sys_caps)} system trace{'' if len(sys_caps) == 1 else 's'}")

    skill_runs = ctx.get("skill_runs") or []
    think_runs = next((s for s in skill_runs if s["slug"] == "think"), None)
    if think_runs:
        parts.append(f"{think_runs['count']} brain ask{'' if think_runs['count'] == 1 else 's'}")

    files = ctx.get("file_events") or {}
    if files.get("total", 0) >= 10:
        top_ext = files.get("by_extension") or []
        ext_bit = ""
        if top_ext:
            ext_bit = f" ({', '.join(e[0] for e in top_ext[:2])})"
        parts.append(f"{files['total']} file events{ext_bit}")

    return " · ".join(parts) if parts else ""


# ── public ──────────────────────────────────────────────────────────────────

def cluster_context(con: sqlite3.Connection, *,
                    cluster_id: str | None = None,
                    started_at: str | None = None,
                    ended_at: str | None = None,
                    repos: list[str | Path] | None = None,
                    cfg: dict | None = None) -> dict:
    """Build the enrichment packet for a cluster (or any time range).
    Either pass cluster_id (range looked up from job_view) or pass
    started_at + ended_at explicitly.
    """
    if cluster_id and (started_at is None or ended_at is None):
        rng = _cluster_range(con, cluster_id)
        if rng is None:
            return {"captures": [], "git_commits": [], "skill_runs": [],
                    "session_titles": [], "calendar_events": [],
                    "file_events": {"total": 0, "by_extension": []},
                    "summary": ""}
        started_at, ended_at = rng
    if not started_at or not ended_at:
        return {"captures": [], "git_commits": [], "skill_runs": [],
                "session_titles": [], "calendar_events": [],
                "file_events": {"total": 0, "by_extension": []},
                "summary": ""}

    if repos is None:
        repos = [ROOT]
    git_commits: list[dict] = []
    for repo in repos:
        git_commits.extend(git_commits_between(started_at, ended_at, repo))
    git_commits.sort(key=lambda d: d["ts"], reverse=True)

    out = {
        "captures":    _captures_in(con, started_at, ended_at, cluster_id),
        "git_commits": git_commits,
        "skill_runs":  _skill_runs_in(con, started_at, ended_at),
        "file_events": _file_events_in(con, started_at, ended_at),
        "session_titles": _session_titles_in(
            con, cluster_id, started_at, ended_at),
        "calendar_events": _calendar_events_in(con, started_at, ended_at),
    }
    out["summary"] = summary_line(out)
    return out
