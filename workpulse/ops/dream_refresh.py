"""
dream_refresh.py — fast cluster refresh + categorize pass.

Runs every 30 min during waking hours via launchd. Keeps job_view +
cluster_assignment current so the dashboard doesn't show stale data
between nightly dream-cycle runs.

What it does (in order):
  1. cluster.refresh()       — re-cluster sessions (idempotent, ~80ms)
  2. name_clusters.name_all() — fallback-name any new clusters (~50ms)
  3. categorize.assign_all() — score + assign every cluster (~150ms)

Total wall time: well under 1s on a typical DB. Cheap to run frequently.

CLI:
    python -m workpulse.ops.dream_refresh
    python -m workpulse.ops.dream_refresh --json
    python -m workpulse.ops.dream_refresh --quiet     # for launchd
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from workpulse.core import categorize as cat, cluster as wp_cluster
from workpulse.core import db
from workpulse.core import name_clusters as nc
from workpulse.common import load_config


def refresh_all(*, force_categorize: bool = False, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    con = db.connect(cfg)
    t0 = time.monotonic()

    # 1) Re-cluster sessions. Idempotent; cluster_ids are content-hashes,
    # so existing assignments / names / corrections stay intact.
    cluster_counts = wp_cluster.refresh(con)
    t_cluster = time.monotonic()

    # 2) Name any new clusters via fallback. LLM naming runs nightly via
    # the profile / consolidate paths; this pass is fast + offline.
    name_results = nc.name_all(con, force_fallback=True, cfg=cfg)
    named = sum(1 for r in name_results if not r.get("skipped"))
    t_name = time.monotonic()

    # 3) Categorizer. User assignments are protected even with force=True.
    cat_counts = cat.assign_all(con, force=force_categorize, cfg=cfg)
    t_cat = time.monotonic()

    return {
        "cluster_refresh": {
            "clusters":          cluster_counts.get("clusters", 0),
            "sessions_assigned": cluster_counts.get("sessions_assigned", 0),
            "ms":                round((t_cluster - t0) * 1000, 1),
        },
        "name_clusters": {
            "named": named,
            "ms":    round((t_name - t_cluster) * 1000, 1),
        },
        "categorize": {
            "total":          cat_counts["total"],
            "assigned_agent": cat_counts["assigned_agent"],
            "kept_user":      cat_counts["kept_user"],
            "fallback":       cat_counts["fallback"],
            "ms":             round((t_cat - t_name) * 1000, 1),
        },
        "total_ms": round((t_cat - t0) * 1000, 1),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp dream-refresh",
                                     description="30-min cluster + categorize pass.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--force-categorize", action="store_true",
                        help="re-do agent assignments (user overrides still kept)")
    args = parser.parse_args(argv[1:])
    summary = refresh_all(force_categorize=args.force_categorize)
    if args.quiet:
        return 0
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"dream-refresh complete in {summary['total_ms']} ms")
    cr = summary["cluster_refresh"]
    nm = summary["name_clusters"]
    ca = summary["categorize"]
    print(f"  cluster.refresh        {cr['clusters']:>4} clusters,  "
          f"{cr['sessions_assigned']:>5} sessions assigned   ({cr['ms']} ms)")
    print(f"  name_clusters          {nm['named']:>4} named                                    "
          f"({nm['ms']} ms)")
    print(f"  categorize             {ca['total']:>4} total,  "
          f"agent={ca['assigned_agent']}  user-kept={ca['kept_user']}  "
          f"fallback={ca['fallback']}   ({ca['ms']} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
