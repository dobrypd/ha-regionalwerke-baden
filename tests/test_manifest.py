"""Manifest rules that only hassfest enforces, checked locally too.

hassfest runs in CI only, so a violation here used to surface as a red build
after a push rather than as a failing test before one.
"""

import json
import pathlib

MANIFEST = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "regionalwerke_baden"
    / "manifest.json"
)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_keys_are_sorted_the_way_hassfest_wants():
    """domain, name, then alphabetical — hassfest fails the build otherwise."""
    keys = list(_manifest())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_the_keys_hacs_requires_are_present():
    manifest = _manifest()
    for key in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
    ):
        assert manifest.get(key), f"HACS requires a non-empty {key}"


def test_version_is_a_plain_release_number():
    """HACS matches this against the GitHub release tag."""
    version = _manifest()["version"]
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), version
