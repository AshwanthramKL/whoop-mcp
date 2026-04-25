"""Tests for ``whoop_install_skills``.

The script fetches files from GitHub raw or copies from a local
``skills/`` directory into ``~/.claude/skills/``. Both paths are tested
against ``tmp_path``; no real network is hit (URL path is monkey-patched).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import whoop_install_skills as wis


def test_local_source_copies_all_catalogued_skills(tmp_path: Path) -> None:
    # Fake source layout matching SKILLS catalogue.
    source = tmp_path / "skills"
    target = tmp_path / "claude-skills"
    skill_name = next(iter(wis.SKILLS))
    skill_files = wis.SKILLS[skill_name]

    skill_src = source / skill_name
    skill_src.mkdir(parents=True)
    for f in skill_files:
        (skill_src / f).write_text(f"contents-of-{f}")

    rc = wis.main(["--source", str(source), "--target", str(target)])

    assert rc == 0
    for f in skill_files:
        copied = target / skill_name / f
        assert copied.is_file(), f"missing {copied}"
        assert copied.read_text() == f"contents-of-{f}"


def test_local_source_replaces_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    target = tmp_path / "claude-skills"
    skill_name = next(iter(wis.SKILLS))

    # Pre-populate target with stale content.
    stale = target / skill_name
    stale.mkdir(parents=True)
    (stale / "STALE.txt").write_text("should be removed")

    skill_src = source / skill_name
    skill_src.mkdir(parents=True)
    for f in wis.SKILLS[skill_name]:
        (skill_src / f).write_text("fresh")

    rc = wis.main(["--source", str(source), "--target", str(target)])

    assert rc == 0
    assert not (target / skill_name / "STALE.txt").exists()
    for f in wis.SKILLS[skill_name]:
        assert (target / skill_name / f).read_text() == "fresh"


def test_local_source_missing_returns_error(tmp_path: Path) -> None:
    rc = wis.main(["--source", str(tmp_path / "does-not-exist"), "--target", str(tmp_path / "out")])
    assert rc == 1


def test_url_source_writes_each_catalogued_file(tmp_path: Path) -> None:
    target = tmp_path / "claude-skills"

    fetched: list[str] = []

    def fake_fetch(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(b"stub-from-url")

    with patch.object(wis, "_fetch_url", side_effect=fake_fetch):
        rc = wis.main(["--target", str(target)])

    assert rc == 0
    expected_count = sum(len(files) for files in wis.SKILLS.values())
    assert len(fetched) == expected_count
    for url in fetched:
        assert url.startswith(wis.SKILLS_BASE)
    for skill_name, files in wis.SKILLS.items():
        for f in files:
            assert (target / skill_name / f).read_bytes() == b"stub-from-url"


def test_url_source_propagates_fetch_errors(tmp_path: Path) -> None:
    target = tmp_path / "claude-skills"

    def boom(url: str, dest: Path) -> None:
        raise RuntimeError("network down")

    with patch.object(wis, "_fetch_url", side_effect=boom):
        rc = wis.main(["--target", str(target)])

    assert rc == 1


def test_default_target_is_user_claude_skills_dir(tmp_path: Path, monkeypatch) -> None:
    # No --target passed => uses ~/.claude/skills. Patch HOME so we don't
    # touch the real user dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with patch.object(wis, "_fetch_url", lambda url, dest: dest.write_bytes(b"x")):
        rc = wis.main([])

    assert rc == 0
    skill_name = next(iter(wis.SKILLS))
    assert (tmp_path / ".claude" / "skills" / skill_name).is_dir()


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_runs_cleanly(flag: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        wis.main([flag])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "whoop-mcp-install-skills" in captured.out
