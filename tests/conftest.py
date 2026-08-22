from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_state_dir(monkeypatch):
    """A developer's real $BEEBOT_STATE_DIR must not steer the tests. Cleared
    in-process, which is also what the subprocesses inherit."""
    for name in ("BEEBOT_STATE_DIR", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(name, raising=False)
