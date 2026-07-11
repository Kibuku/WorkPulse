"""Test bootstrap for the workpulse package.

- Makes `import workpulse` resolve without an install.
- Points the project resolver at a hermetic fixture taxonomy so tests never
  depend on a user's real config/projects.yaml (which isn't in the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

_FIXTURE = Path(__file__).resolve().parent / "tests" / "_projects_fixture.yaml"


@pytest.fixture(autouse=True)
def _projects_taxonomy(monkeypatch):
    """Point the resolver at the fixture taxonomy for every test, and reset
    its mtime cache so the fixture is always (re)loaded."""
    from workpulse.core import projects as wp_projects
    monkeypatch.setattr(wp_projects, "_DEFAULT_PATH", _FIXTURE)
    wp_projects._CACHE["mtime"] = 0
    wp_projects._CACHE["projects"] = []
    yield
    wp_projects._CACHE["mtime"] = 0
    wp_projects._CACHE["projects"] = []
