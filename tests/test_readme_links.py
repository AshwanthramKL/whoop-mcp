"""README / CHANGELOG internal link integrity.

Walk both documents, extract every markdown link, and assert that:

- Relative file links (``./foo`` or ``foo.md``) point at a file that
  actually exists in the repo.
- In-document anchor links (``#some-heading``) resolve to a heading in
  the *same* document (slug-normalised the GitHub way: lower-cased,
  non-alphanumerics dropped, spaces to dashes).

External ``http(s)://`` links are out of scope — this test never hits
the network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# [text](target)  — captures the target.
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _slugify(heading: str) -> str:
    """GitHub-flavored heading slug.

    Lower-case, strip any char that isn't ``[a-z0-9 -]``, collapse
    whitespace to single dashes. Good enough for our simple headings.
    """
    s = heading.lower()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _extract_headings(text: str) -> list[str]:
    return [_slugify(m.group(2)) for m in _HEADING_RE.finditer(text)]


def _extract_links(text: str) -> list[str]:
    return _LINK_RE.findall(text)


def _classify(target: str) -> tuple[str, str, str]:
    """Return (kind, path_part, anchor_part) for a link target.

    kind ∈ {"external", "anchor", "file", "file+anchor"}
    """
    if target.startswith(("http://", "https://", "mailto:")):
        return ("external", target, "")
    if target.startswith("#"):
        return ("anchor", "", target[1:])
    if "#" in target:
        path_part, _, anchor = target.partition("#")
        return ("file+anchor", path_part, anchor)
    return ("file", target, "")


def _check_document(doc_path: Path) -> list[str]:
    """Return a list of human-readable failure strings (empty = clean)."""
    text = doc_path.read_text(encoding="utf-8")
    headings = set(_extract_headings(text))
    failures: list[str] = []

    for raw in _extract_links(text):
        target = raw.strip()
        kind, path_part, anchor = _classify(target)

        if kind == "external":
            continue

        if kind == "anchor":
            if anchor not in headings:
                failures.append(
                    f"{doc_path.name}: anchor #{anchor} has no matching "
                    f"heading (known: {sorted(headings)})"
                )
            continue

        # file or file+anchor
        path_part_clean = path_part
        if path_part_clean.startswith("./"):
            path_part_clean = path_part_clean[2:]
        resolved = (REPO_ROOT / path_part_clean).resolve()
        if not os.path.exists(resolved):
            failures.append(
                f"{doc_path.name}: link target {path_part!r} does not "
                f"resolve to a file on disk ({resolved})"
            )
            continue

        if kind == "file+anchor" and resolved.is_file():
            # Validate the anchor against the *target* document's headings.
            try:
                other_text = resolved.read_text(encoding="utf-8")
            except Exception:
                continue
            other_headings = set(_extract_headings(other_text))
            if anchor not in other_headings:
                failures.append(
                    f"{doc_path.name}: anchor #{anchor} in {path_part!r} "
                    f"has no matching heading in target document"
                )
    return failures


def test_readme_links_resolve():
    failures = _check_document(REPO_ROOT / "README.md")
    assert not failures, "\n".join(failures)


def test_changelog_links_resolve():
    failures = _check_document(REPO_ROOT / "CHANGELOG.md")
    assert not failures, "\n".join(failures)
