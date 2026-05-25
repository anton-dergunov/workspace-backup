"""
Shared fixtures and helpers for restic_preview integration tests.

parse_restic_list() and classify() are the same logic that was in compare_restic.py,
kept here so they're reused across all tests without a root-level helper script.
"""

import bisect
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from workspace_backup.preview import collect, _load_exclude, fmt_size

# ── Skip marker ───────────────────────────────────────────────────────────────

skip_no_restic = pytest.mark.skipif(
    shutil.which("restic") is None,
    reason="restic not found (brew install restic)",
)

# ── Restic list parsing (from compare_restic.py) ──────────────────────────────

def parse_restic_list(list_file: Path, source: Path):
    """
    Return (file_paths, symlink_paths) from 'restic ls latest' output.
    Directories are filtered out. Entries gone from disk are inferred via
    child-prefix heuristic.
    """
    entries = []
    with open(list_file) as f:
        for line in f:
            line = line.rstrip()
            if not line.startswith("/"):
                continue
            p = Path(line)
            try:
                p.relative_to(source)
            except ValueError:
                continue
            if p == source:
                continue
            entries.append(p)

    sorted_strs = sorted(str(e) for e in entries)
    files: set[Path] = set()
    symlinks: set[Path] = set()

    for p in entries:
        if p.is_symlink():
            symlinks.add(p)
            continue
        if p.exists():
            if p.is_file():
                files.add(p)
            continue
        # Path gone from disk — infer from listing
        prefix = str(p) + "/"
        idx = bisect.bisect_left(sorted_strs, prefix)
        has_children = idx < len(sorted_strs) and sorted_strs[idx].startswith(prefix)
        if not has_children:
            files.add(p)

    return files, symlinks


def _under_any(path: Path, prefixes: list[Path]) -> bool:
    for prefix in prefixes:
        try:
            path.relative_to(prefix)
            return True
        except ValueError:
            pass
    return False


def classify(path: Path,
             included_set: set,
             summarized_dirs: list,
             exc_dir_list: list,
             exc_file_set: set,
             nobackup_dir_list: list) -> str:
    if path in included_set:
        return "match"
    if _under_any(path, summarized_dirs):
        return "summarized"
    if path in exc_file_set:
        return "excluded_file"
    if _under_any(path, exc_dir_list):
        return "excluded_dir"
    if _under_any(path, nobackup_dir_list):
        return "nobackup_dir"
    return "unknown"


# ── Tree / file helpers ───────────────────────────────────────────────────────

def build_tree(root: Path, spec: dict) -> None:
    """Create a file tree from {relative_path: str|bytes} dict."""
    for rel, content in spec.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)


def make_exclude_file(tmp_path: Path, patterns: list[str]) -> Path:
    """Write patterns to a temp exclude file outside the source dir."""
    excl = tmp_path / "test_excludes.txt"
    excl.write_text("\n".join(patterns) + "\n")
    return excl


# ── Restic repo fixture ───────────────────────────────────────────────────────

@pytest.fixture
def tmp_restic_repo(tmp_path):
    """
    Init a fresh restic repo in tmp_path/restic_repo/.
    Yields (repo_path, env_dict).
    Function-scoped: every test gets its own empty repo.
    """
    repo = tmp_path / "restic_repo"
    repo.mkdir()
    env = {
        **os.environ,
        "RESTIC_PASSWORD": "test-integration-pw",
        "RESTIC_REPOSITORY": str(repo),
    }
    subprocess.run(
        ["restic", "init", "--repo", str(repo)],
        env=env, check=True, capture_output=True, timeout=30,
    )
    yield repo, env


# ── Core comparison helper ────────────────────────────────────────────────────

def run_backup_and_compare(
    source: Path,
    repo: Path,
    env: dict,
    exclude_file: Path,
    nobackup_file: str = "",
    summarize_names: frozenset = frozenset(),
) -> dict:
    """
    1. Run restic backup on source.
    2. Run restic ls latest → parse file list.
    3. Run collect() on source with the same options.
    4. Classify every restic file against collect()'s output.
    5. Return a report dict with discrepancy lists and all lookup structures.

    Expected non-discrepancies (not counted as errors):
    - nobackup_dir: restic keeps the marker file; collect() skips the entire dir
    - summarized: intentionally collapsed in preview
    - symlinks: restic backs them up; collect() skips them
    """
    # Backup
    cmd = ["restic", "backup", str(source), "--exclude-file", str(exclude_file)]
    if nobackup_file:
        cmd += ["--exclude-if-present", nobackup_file]
    subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=60)

    # ls → file on disk (sibling of source, not inside it)
    ls_result = subprocess.run(
        ["restic", "ls", "latest"],
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    ls_file = source.parent / "restic_ls.txt"
    ls_file.write_text(ls_result.stdout)

    restic_files, restic_symlinks = parse_restic_list(ls_file, source)

    # collect()
    spec, pat_specs = _load_exclude(exclude_file)
    included, exc_dirs, exc_files, summarized, nobackup_dirs = collect(
        source, spec, pat_specs, set(summarize_names), nobackup_file,
    )

    included_set      = {p for p, _ in included}
    summarized_dirs   = [p for p, _ in summarized]
    exc_dir_list      = [p for p, _, _ in exc_dirs]
    exc_file_set      = {p for p, _, _ in exc_files}
    nobackup_dir_list = [p for p, _ in nobackup_dirs]

    extra_in_restic: list[Path] = []
    for path in sorted(restic_files):
        cat = classify(path, included_set, summarized_dirs,
                       exc_dir_list, exc_file_set, nobackup_dir_list)
        if cat not in ("match", "summarized", "nobackup_dir"):
            extra_in_restic.append(path)

    missing_from_restic = sorted(
        p for p in included_set if p not in restic_files
    )

    return {
        "extra_in_restic":     extra_in_restic,
        "missing_from_restic": missing_from_restic,
        "restic_files":        restic_files,
        "restic_symlinks":     restic_symlinks,
        "included_set":        included_set,
        "exc_dir_list":        exc_dir_list,
        "exc_file_set":        exc_file_set,
        "nobackup_dir_list":   nobackup_dir_list,
    }


def assert_no_discrepancies(report: dict, source: Path) -> None:
    extra   = report["extra_in_restic"]
    missing = report["missing_from_restic"]
    parts = []
    if extra:
        rels = [str(p.relative_to(source)) for p in extra]
        parts.append(
            f"restic backed up {len(extra)} file(s) that collect() excluded:\n"
            + "\n".join(f"  {r}" for r in rels)
        )
    if missing:
        rels = [str(p.relative_to(source)) for p in missing]
        parts.append(
            f"collect() included {len(missing)} file(s) that restic excluded:\n"
            + "\n".join(f"  {r}" for r in rels)
        )
    assert not parts, "\n\n".join(parts)
