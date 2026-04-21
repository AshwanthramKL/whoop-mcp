"""Version drift guard.

`src/__version__.py` is the single source of truth for the package
version. `SERVER_VERSION` in `whoop_mcp_server.py` must be derived from
it (not hard-coded), and `pyproject.toml` should match.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_server_version_matches_version_module():
    """SERVER_VERSION is whatever __version__.py says it is."""
    import __version__ as version_mod
    import whoop_mcp_server

    assert whoop_mcp_server.SERVER_VERSION == version_mod.__version__


def test_version_module_has_a_nonempty_string():
    import __version__ as version_mod

    assert isinstance(version_mod.__version__, str)
    assert version_mod.__version__.strip() == version_mod.__version__
    assert version_mod.__version__  # non-empty


def test_pyproject_version_matches_version_module():
    """Keep pyproject.toml in sync so `pip install .` reports the right version."""
    import __version__ as version_mod

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected_line = f'version = "{version_mod.__version__}"'
    assert expected_line in pyproject, (
        f"pyproject.toml must contain {expected_line!r}; "
        f"update its version pin to match src/__version__.py"
    )
