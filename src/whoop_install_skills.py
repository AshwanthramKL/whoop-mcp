"""
``whoop-mcp-install-skills`` — download the bundled Claude Code skills from
the canonical GitHub repo into the user's ``~/.claude/skills/`` directory.

Why this exists
---------------
MCP clients (Claude Code, Cursor, Windsurf, Zed) auto-discover skills from
fixed paths — typically ``~/.claude/skills/`` user-scope or
``.claude/skills/`` project-scope. Shipping ``skills/`` inside the repo or
the wheel doesn't make Claude find them — the user has to copy the files
into one of the discovery paths first.

This script does that copy for every install type (``uvx``, ``pipx``,
``pip``, git clone) without requiring the user to think about it.

Network requirement
-------------------
The skill files are fetched from GitHub raw at runtime (rather than
bundled inside the wheel) so that a single canonical source feeds all
install paths. Internet is required when this script runs; nothing is
stored that wasn't installable from your repo HEAD.

Use ``--source <path>`` to install from a local ``skills/`` directory
instead — useful for repo-native developers who want to test in-progress
skill changes before they hit ``main``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Pinned to ``main`` so users on older versions still get the latest
# (forward-compatible) skill instructions. Skills describe how to call
# tools; they don't ship tool implementations, so a 0.8.x user pulling a
# 0.9.x skill is fine until the skill references a tool that doesn't
# exist yet — at which point Claude returns a graceful "tool not
# available" response, not a crash.
SKILLS_BASE = "https://raw.githubusercontent.com/AshwanthramKL/whoop-mcp/main/skills"

# Catalogue of skills shipped with this MCP. Add entries here when new
# skills land under ``skills/<name>/`` in the repo.
SKILLS: dict[str, list[str]] = {
    "whoop-insights": [
        "SKILL.md",
        "dashboard_template.html",
    ],
}


def _claude_skills_dir() -> Path:
    """Default install location for Claude Code user-scope skills."""
    return Path.home() / ".claude" / "skills"


def _fetch_url(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest``. Raises on HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "whoop-mcp-install-skills"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GET {url} -> HTTP {resp.status}")
        dest.write_bytes(resp.read())


def _install_from_url(target_root: Path) -> int:
    """Fetch each catalogued skill from GitHub raw into ``target_root``."""
    for skill_name, files in SKILLS.items():
        skill_dir = target_root / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        for filename in files:
            url = f"{SKILLS_BASE}/{skill_name}/{filename}"
            dest = skill_dir / filename
            print(f"  fetching {url}")
            try:
                _fetch_url(url, dest)
            except (urllib.error.URLError, RuntimeError) as e:
                print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
                return 1
        print(f"installed: {skill_dir}")
    return 0


def _install_from_local(source_root: Path, target_root: Path) -> int:
    """Copy each catalogued skill from a local repo checkout."""
    if not source_root.is_dir():
        print(f"ERROR: source {source_root} is not a directory", file=sys.stderr)
        return 1
    for skill_name in SKILLS:
        src = source_root / skill_name
        if not src.is_dir():
            print(f"  WARN: source skill {src} missing, skipping", file=sys.stderr)
            continue
        dest = target_root / skill_name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        print(f"installed: {dest} (from {src})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="whoop-mcp-install-skills",
        description=(
            "Install the whoop-mcp Claude Code skills "
            "(currently: whoop-insights) into ~/.claude/skills/. "
            "Default source is GitHub main; pass --source for a local repo."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Local path to a `skills/` directory (e.g. <repo>/skills). "
        "If omitted, files are fetched from GitHub main.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help=f"Override install target. Defaults to {_claude_skills_dir()}.",
    )
    args = parser.parse_args(argv)

    target_root = args.target or _claude_skills_dir()
    target_root.mkdir(parents=True, exist_ok=True)
    print(f"installing skills into: {target_root}")

    if args.source is not None:
        rc = _install_from_local(args.source, target_root)
    else:
        rc = _install_from_url(target_root)

    if rc == 0:
        print()
        print("Done. Restart your Claude Code (or other MCP client) session")
        print("for the skill to appear in the available-skills list. Then try:")
        print("  > How am I doing? Run the whoop-insights skill.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
