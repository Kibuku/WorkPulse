"""
tree.py — stream hierarchy data layer.

Streams form an arbitrary-depth tree. Each node carries an optional `parent`;
top-level nodes have parent=None. Jobs attach at any node and roll up the
chain automatically — today's "Work" total is the sum of every descendant.

Back-compat note: ``config.yaml`` can use either shape for ``streams``:

    streams:
      misc: Miscellaneous                                  # flat (legacy)
      verst-carbon:                                        # tree
        label: Verst Carbon
        parent: work
      work:
        label: Work

Anything that's just a string is treated as a top-level node. So existing
installs keep working with zero migration; new installs use the tree shape
written by the taxonomy wizard.

Public API:
    normalise(cfg) -> dict[key, {label, parent}]
    label_of(key, cfg) -> str
    parent_of(key, cfg) -> str | None
    children_of(key, cfg) -> list[str]
    ancestors(key, cfg, *, include_self=False) -> list[str]   # root → ... → self
    descendants(key, cfg, *, include_self=False) -> list[str]
    path(key, cfg) -> list[str]                                # root → self, inclusive
    breadcrumb(key, cfg, *, sep=" › ") -> str                  # "Work › Verst Carbon"
    top_level(cfg) -> list[str]                                # parent=None roots
    is_descendant(child, ancestor, cfg) -> bool
    validate_tree(cfg) -> list[str]                            # human-readable errors

The module deliberately holds no state; pass cfg in. Callers that want to
amortise the normalise call should cache the result themselves.
"""

from __future__ import annotations

from workpulse.common import load_config


# ── normalisation ────────────────────────────────────────────────────────────

def normalise(cfg: dict | None = None) -> dict[str, dict]:
    """Return ``{key: {"label": str, "parent": str|None}}`` regardless of the
    on-disk shape. Unknown / malformed parent references are dropped to None
    so a typo in config.yaml never produces a dangling tree.
    """
    if cfg is None:
        cfg = load_config()
    raw = cfg.get("streams") or {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, dict] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(val, str):
            out[key] = {"label": val, "parent": None}
        elif isinstance(val, dict):
            label = str(val.get("label") or key)
            parent = val.get("parent")
            if parent is not None and not isinstance(parent, str):
                parent = None
            out[key] = {"label": label, "parent": parent or None}
        else:
            # Anything else: keep the key as a labelless top-level node.
            out[key] = {"label": key, "parent": None}

    # Second pass: drop parent references that don't resolve.
    keys = set(out.keys())
    for k, rec in out.items():
        p = rec.get("parent")
        if p is not None and p not in keys:
            rec["parent"] = None
        if p == k:
            # A node cannot be its own parent.
            rec["parent"] = None

    # Third pass: break any cycles by walking up; if we revisit a node, snap
    # the offending parent to None. Cycles can only arise from manual YAML
    # editing — defensive, not paranoid.
    def _has_cycle(start: str) -> bool:
        seen = {start}
        cur = out[start].get("parent")
        while cur:
            if cur in seen:
                return True
            seen.add(cur)
            cur = out.get(cur, {}).get("parent")
        return False
    for k in list(out.keys()):
        if _has_cycle(k):
            out[k]["parent"] = None

    return out


# ── public lookups ───────────────────────────────────────────────────────────

def labels(cfg: dict | None = None) -> dict[str, str]:
    """Flat {key: label_string} view of streams — what legacy call sites
    expected when ``streams`` was a flat dict in config.yaml. New code
    should use ``normalise`` so it can also see parent info."""
    return {k: rec["label"] for k, rec in normalise(cfg).items()}


def label_of(key: str, cfg: dict | None = None) -> str:
    tree = normalise(cfg)
    rec = tree.get(key)
    if rec is None:
        return key
    return rec.get("label") or key


def parent_of(key: str, cfg: dict | None = None) -> str | None:
    tree = normalise(cfg)
    rec = tree.get(key)
    if rec is None:
        return None
    return rec.get("parent")


def children_of(key: str, cfg: dict | None = None) -> list[str]:
    tree = normalise(cfg)
    return sorted([k for k, rec in tree.items() if rec.get("parent") == key])


def ancestors(key: str, cfg: dict | None = None, *,
              include_self: bool = False) -> list[str]:
    """Root → … → self ordering. Empty list if key unknown."""
    tree = normalise(cfg)
    if key not in tree:
        return []
    chain: list[str] = []
    cur = tree[key].get("parent")
    while cur:
        chain.append(cur)
        cur = tree.get(cur, {}).get("parent")
    chain.reverse()  # root-first
    if include_self:
        chain.append(key)
    return chain


def descendants(key: str, cfg: dict | None = None, *,
                include_self: bool = False) -> list[str]:
    """All descendants (depth-first), excluding self by default."""
    tree = normalise(cfg)
    if key not in tree:
        return []
    out: list[str] = []
    if include_self:
        out.append(key)
    stack = list(reversed(children_of(key, cfg)))
    while stack:
        n = stack.pop()
        out.append(n)
        # Push children in reverse so the visit order is alphabetical.
        for c in reversed(children_of(n, cfg)):
            stack.append(c)
    return out


def path(key: str, cfg: dict | None = None) -> list[str]:
    """Root → self, inclusive. Empty list if key unknown."""
    return ancestors(key, cfg, include_self=True)


def breadcrumb(key: str, cfg: dict | None = None, *, sep: str = " › ") -> str:
    """Human-readable breadcrumb using labels, e.g. 'Work › Verst Carbon'."""
    tree = normalise(cfg)
    return sep.join(tree.get(k, {}).get("label") or k for k in path(key, cfg))


def top_level(cfg: dict | None = None) -> list[str]:
    tree = normalise(cfg)
    return sorted([k for k, rec in tree.items() if not rec.get("parent")])


def is_descendant(child: str, ancestor_key: str,
                  cfg: dict | None = None) -> bool:
    if child == ancestor_key:
        return False
    return ancestor_key in ancestors(child, cfg)


# ── validation (for the wizard, for tests, for diagnostics) ──────────────────

def validate_tree(cfg: dict | None = None) -> list[str]:
    """Return a list of human-readable issues. Empty list = clean.
    Does NOT touch the file — pure read-only diagnostic."""
    tree = normalise(cfg)
    issues: list[str] = []
    keys = set(tree.keys())
    raw = (cfg or load_config()).get("streams") or {}

    # Check for parents that don't resolve in the original (pre-normalise) data.
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                p = v.get("parent")
                if p and p not in raw:
                    issues.append(f"stream '{k}' has parent '{p}' which is not a stream")
                if p == k:
                    issues.append(f"stream '{k}' is its own parent")
    # Check for keys that don't satisfy the standard format.
    import re as _re
    for k in keys:
        if not _re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,29}", k):
            issues.append(f"stream key '{k}' should be lowercase letters/digits/hyphens, 2–30 chars")
    return issues
