"""
Integration tests: compare what restic actually backs up against what
restic_preview.collect() predicts. Each test creates a fresh file tree and
restic repository in pytest's tmp_path.

Requires restic installed (brew install restic). Tests are skipped otherwise.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pathspec
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from restic_preview import collect, _load_exclude

from conftest import (
    assert_no_discrepancies,
    build_tree,
    make_exclude_file,
    run_backup_and_compare,
    skip_no_restic,
)

REAL_EXCLUDES = Path(__file__).parent.parent / "config" / "excludes_default.txt"


# ── Parametrized basic scenarios (tests 1–4) ──────────────────────────────────

@dataclass
class BasicScenario:
    name: str
    tree: dict
    patterns: list
    expected_included: frozenset = field(default_factory=frozenset)
    expected_excluded: frozenset = field(default_factory=frozenset)


BASIC_SCENARIOS = [
    BasicScenario(
        name="no_excludes",
        tree={
            "file.txt":      "hello",
            "sub/code.py":   "print()",
            "sub/data.json": '{"k":"v"}',
        },
        patterns=[],
        expected_included=frozenset(["file.txt", "sub/code.py", "sub/data.json"]),
    ),
    BasicScenario(
        name="glob_pattern",
        tree={
            "main.py":            "print()",
            "main.pyc":           b"\xed\x0d",
            "src/utils.py":       "def f(): pass",
            "src/utils.pyc":      b"\xed\x0d",
            "src/deep/helper.pyc": b"\xed\x0d",
        },
        patterns=["*.pyc"],
        expected_included=frozenset(["main.py", "src/utils.py"]),
        expected_excluded=frozenset(["main.pyc", "src/utils.pyc", "src/deep/helper.pyc"]),
    ),
    BasicScenario(
        name="dir_name_pattern",
        tree={
            "src/app.js":                        "app",
            "node_modules/lodash/index.js":      "lodash",
            "node_modules/react/react.js":       "react",
            "frontend/node_modules/webpack/bin.js": "webpack",
        },
        patterns=["node_modules"],
        expected_included=frozenset(["src/app.js"]),
        expected_excluded=frozenset(["node_modules", "frontend/node_modules"]),
    ),
    BasicScenario(
        name="trailing_slash_dir",
        tree={
            ".venv/bin/python":          "#!/usr/bin/env python3",
            ".venv/lib/site-packages/x.py": "pkg",
            "src/main.py":               "main",
        },
        patterns=[".venv/"],
        expected_included=frozenset(["src/main.py"]),
        expected_excluded=frozenset([".venv"]),
    ),
]


@pytest.mark.parametrize("scenario", BASIC_SCENARIOS, ids=lambda s: s.name)
@skip_no_restic
def test_basic_scenario(scenario, tmp_path, tmp_restic_repo):
    repo, env = tmp_restic_repo
    source = tmp_path / "source"
    build_tree(source, scenario.tree)
    excl = make_exclude_file(tmp_path, scenario.patterns)

    report = run_backup_and_compare(source, repo, env, excl)
    assert_no_discrepancies(report, source)

    for rel in scenario.expected_included:
        assert source / rel in report["included_set"], f"{rel!r} should be included"

    for rel in scenario.expected_excluded:
        p = source / rel
        excluded = (
            p in report["exc_dir_list"]
            or p in report["exc_file_set"]
            or any(str(p).startswith(str(ed)) for ed in report["exc_dir_list"])
        )
        assert excluded, f"{rel!r} should be excluded"


# ── Test 5: trailing / excludes both files AND directories by that name ───────

@skip_no_restic
def test_trailing_slash_pattern_file(tmp_path, tmp_restic_repo):
    """
    .env/ in an exclude file: restic ignores the slash and excludes a FILE named
    .env. After the _load_exclude fix (strip trailing /), collect() matches this.

    The directory case (.env/ dir excluded) is already covered by trailing_slash_dir.

    Also documents the pre-fix pathspec behavior as a contrast:
    without the fix, pathspec would treat .env/ as "directories only" and
    include the .env file — diverging from restic.
    """
    repo, env = tmp_restic_repo
    source = tmp_path / "source"
    build_tree(source, {
        ".env":          "SECRET=abc",    # FILE named .env — must be excluded
        ".env_backup":   "OLD=xyz",        # different name — must be included
        "config.py":     "import os",
        ".env_dir/data": "nested",         # .env_dir — must NOT match .env pattern
        "env_folder/x":  "data",          # env_folder — must NOT match
    })
    excl = make_exclude_file(tmp_path, [".env/"])

    # Document the pre-fix pathspec behavior (trailing / means "dirs only"):
    unfixed_spec = pathspec.PathSpec.from_lines("gitignore", [".env/"])
    assert not unfixed_spec.match_file(".env"), (
        "Pre-fix: .env/ pattern does NOT match a plain file named .env "
        "(pathspec treats trailing / as 'directories only')"
    )
    assert unfixed_spec.match_file(".env/"), (
        "Pre-fix: .env/ pattern DOES match a directory named .env"
    )

    # With the fix applied in _load_exclude, both agree with restic:
    report = run_backup_and_compare(source, repo, env, excl)
    assert_no_discrepancies(report, source)

    # .env file is excluded
    assert source / ".env" not in report["included_set"]
    # Other files are included
    assert source / ".env_backup" in report["included_set"]
    assert source / "config.py" in report["included_set"]
    assert source / ".env_dir" / "data" in report["included_set"]
    assert source / "env_folder" / "x" in report["included_set"]


# ── Test 6: .nobackup marker file ─────────────────────────────────────────────

@skip_no_restic
def test_nobackup_marker(tmp_path, tmp_restic_repo):
    """
    Dirs containing a .nobackup file have their contents excluded.
    restic keeps the marker file itself; collect() skips the entire dir including
    the marker — this known difference is handled in run_backup_and_compare.
    """
    repo, env = tmp_restic_repo
    source = tmp_path / "source"
    build_tree(source, {
        "normal.txt":           "hello",
        "public/readme.txt":    "public doc",
        "sensitive/data.csv":   "id,name\n1,alice",
        "sensitive/weights.bin": b"\x00\x01\x02",
        "sensitive/.nobackup":  "",
    })
    excl = make_exclude_file(tmp_path, [])

    report = run_backup_and_compare(
        source, repo, env, excl, nobackup_file=".nobackup"
    )
    assert_no_discrepancies(report, source)

    # Contents of sensitive/ are excluded
    assert source / "sensitive" / "data.csv" not in report["restic_files"]
    assert source / "sensitive" / "data.csv" not in report["included_set"]

    # Normal files are included
    assert source / "normal.txt" in report["restic_files"]
    assert source / "normal.txt" in report["included_set"]
    assert source / "public" / "readme.txt" in report["included_set"]

    # collect() marks sensitive/ as a nobackup dir
    assert source / "sensitive" in report["nobackup_dir_list"]


# ── Test 7: real excludes_default.txt ─────────────────────────────────────────

@skip_no_restic
def test_real_excludes_file(tmp_path, tmp_restic_repo):
    """
    Run against the actual config/excludes_default.txt with a dummy source tree
    that exercises every category of pattern. No real project data is used.
    """
    repo, env = tmp_restic_repo
    source = tmp_path / "source"
    build_tree(source, {
        # Should be INCLUDED
        "src/main.py":                    "def main(): pass",
        "README.md":                      "# project",
        "config.yaml":                    "key: value",
        # Python — EXCLUDED
        ".venv/bin/python":               "#!/usr/bin/env python3",
        ".venv/lib/site-packages/req.py": "requests",
        "src/__pycache__/main.pyc":       b"\xed\x0d",
        "src/main.pyc":                   b"\xed\x0d",
        ".coverage":                      b"\xed\x0d",
        "build/out.o":                    b"\x7fELF",
        "dist/bundle.js":                 "bundled",
        # Node — EXCLUDED
        "node_modules/react/index.js":    "react",
        # ML — EXCLUDED
        "models/best.pt":                 b"\x80\x02",
        "models/checkpoint.ckpt":         b"\x80\x02",
        # macOS / Editors — EXCLUDED
        ".DS_Store":                      b"\x00\x00\x00\x01",
        ".idea/workspace.xml":            "<project/>",
        "debug.log":                      "stack trace",
    })

    report = run_backup_and_compare(source, repo, env, REAL_EXCLUDES)
    assert_no_discrepancies(report, source)

    # Spot-checks: included
    assert source / "src" / "main.py" in report["included_set"]
    assert source / "README.md" in report["included_set"]
    # Spot-checks: excluded dirs
    assert source / "node_modules" in report["exc_dir_list"]
    assert source / ".venv" in report["exc_dir_list"]
    # Spot-checks: excluded files
    assert source / ".DS_Store" in report["exc_file_set"]
    assert source / "models" / "best.pt" in report["exc_file_set"]
    assert source / "debug.log" in report["exc_file_set"]


# ── Test 8: leading / anchors at the backup source root ───────────────────────

def test_leading_slash_anchor(tmp_path):
    """
    collect() uses gitignore semantics: /build anchors to the backup source root,
    excluding <source>/build/ but NOT <source>/src/build/.

    NOTE: restic does NOT share this behavior. Restic tests the pattern /build
    against absolute filesystem paths, so /build only matches a directory literally
    at /build on the filesystem root — it never matches anything inside a normal
    backup source like ~/projects. In practice, leading / patterns are useless in
    restic exclude files and should be avoided (see config/excludes_default.txt).

    This test verifies collect()'s gitignore-consistent behavior only (no restic).
    """
    source = tmp_path / "source"
    build_tree(source, {
        "build/out.bin":     b"\x7fELF",
        "build/tmp.o":       b"\x7fELF",
        "src/build/out.bin": b"\x7fELF",   # nested — must NOT be excluded
        "src/app.py":        "app code",
        "docs/readme.md":    "docs",
    })
    excl = make_exclude_file(tmp_path, ["/build"])
    spec, pat_specs = _load_exclude(excl)
    included, exc_dirs, exc_files, _, _ = collect(source, spec, pat_specs, set())

    included_set  = {p for p, _ in included}
    exc_dir_paths = [p for p, _, _ in exc_dirs]

    # Root build/ is excluded by collect() (gitignore anchoring)
    assert source / "build" in exc_dir_paths
    # src/build/ is NOT excluded (anchor only applies at source root)
    assert source / "src" / "build" not in exc_dir_paths
    assert source / "src" / "build" / "out.bin" in included_set
    # Regular files included
    assert source / "src" / "app.py" in included_set
    assert source / "docs" / "readme.md" in included_set
