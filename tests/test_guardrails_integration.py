"""
Integration tests for guardrails.py using real resticprofile + restic.

Each test:
  1. Populates tmp source dir with controlled file trees
  2. Runs one or two backups via resticprofile
  3. Runs guardrails.py as a subprocess with PROFILE_NAME set
  4. Asserts the expected exit code and output content

Requires restic and resticprofile installed.
Never touches real user data — all paths are under pytest's tmp_path.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from conftest import (
    GUARDRAILS_SCRIPT,
    do_backup,
    run_guardrails,
    skip_no_resticprofile,
    build_tree,
)

pytestmark = skip_no_resticprofile


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mb(n: float) -> bytes:
    """Return n MB of random bytes. Random data is incompressible, so restic
    stores it at close to full size — necessary for ADDED_SIZE threshold tests."""
    return os.urandom(int(n * 1024 * 1024))


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_violations(tmp_guardrails_env):
    """Two similar backups with no significant changes → guardrails pass."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {
        "src/main.py":   "def main(): pass",
        "src/utils.py":  "def helper(): pass",
        "README.md":     "# project",
    })
    do_backup(config, profile)

    # Second backup: change one file, no new extensions, no big additions
    (source / "README.md").write_text("# project v2")
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "1.2",
        "--max-file-count-growth-ratio", "1.2",
        "--max-new-files", "100",
        "--max-added-size", "10MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 0, f"Expected no violations.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "All guardrails passed" in result.stdout


def test_size_growth_violation(tmp_guardrails_env):
    """Adding a large file causes the ratio guardrail to trigger."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"small.txt": "hello world"})
    do_backup(config, profile)

    # Add a large file — 2 MB vs ~11 bytes: >> 1.2x growth
    build_tree(source, {"big.bin": _mb(2)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "1.2",
        "--max-file-count-growth-ratio", "5.0",   # high, so only size triggers
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 1, f"Expected SIZE_GROWTH violation.\nstdout: {result.stdout}"
    assert "SIZE_GROWTH" in result.stdout


def test_file_count_growth_violation(tmp_guardrails_env):
    """Adding many files causes the file-count ratio guardrail to trigger."""
    config, source, profile = tmp_guardrails_env

    # Start with 3 files
    build_tree(source, {
        "a.txt": "a",
        "b.txt": "b",
        "c.txt": "c",
    })
    do_backup(config, profile)

    # Add 10 more files → count goes from 3 to 13 = 4.33x, well above 1.2x
    build_tree(source, {f"extra_{i}.txt": f"extra {i}" for i in range(10)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "10.0",            # high, so only count triggers
        "--max-file-count-growth-ratio", "1.2",
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 1, f"Expected FILE_COUNT_GROWTH.\nstdout: {result.stdout}"
    assert "FILE_COUNT_GROWTH" in result.stdout


def test_new_extensions_violation(tmp_guardrails_env):
    """Adding a file with a new extension triggers the extension guardrail."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"model.py": "import torch"})
    do_backup(config, profile)

    # Add a .csv file — new extension
    build_tree(source, {"data.csv": "id,value\n1,42\n"})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "10.0",
        "--max-file-count-growth-ratio", "10.0",
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 1, f"Expected NEW_EXTENSIONS.\nstdout: {result.stdout}"
    assert "NEW_EXTENSIONS" in result.stdout
    assert ".csv" in result.stdout


def test_new_extensions_disabled(tmp_guardrails_env):
    """--no-warn-on-new-extensions suppresses the extension guardrail."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"model.py": "import torch"})
    do_backup(config, profile)

    build_tree(source, {"data.csv": "id,value\n1,42\n"})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "10.0",
        "--max-file-count-growth-ratio", "10.0",
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
        "--no-warn-on-new-extensions",
    ])
    assert result.returncode == 0, f"Expected no violations with extensions disabled.\nstdout: {result.stdout}"
    assert "NEW_EXTENSIONS" not in result.stdout


def test_max_new_files_violation(tmp_guardrails_env):
    """Adding more new files than the absolute limit triggers the guardrail."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"seed.txt": "initial"})
    do_backup(config, profile)

    # Add 10 new files, set limit to 5
    build_tree(source, {f"new_{i}.txt": f"file {i}" for i in range(10)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "5",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
        "--no-warn-on-new-extensions",
    ])
    assert result.returncode == 1, f"Expected NEW_FILES violation.\nstdout: {result.stdout}"
    assert "NEW_FILES" in result.stdout


def test_max_new_files_no_violation(tmp_guardrails_env):
    """Adding fewer files than the limit does not trigger."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"seed.txt": "initial"})
    do_backup(config, profile)

    # Add 3 new files, limit is 10
    build_tree(source, {f"new_{i}.txt": f"file {i}" for i in range(3)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "10",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
        "--no-warn-on-new-extensions",
    ])
    assert result.returncode == 0, f"Expected no violation.\nstdout: {result.stdout}"


def test_max_added_size_violation(tmp_guardrails_env):
    """Adding data beyond the added-size threshold triggers the guardrail."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"seed.txt": "initial"})
    do_backup(config, profile)

    # Add 2 MB of new data; set threshold to 1 MB
    build_tree(source, {"large.bin": _mb(2)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "1000",
        "--max-added-size", "1MB",
        "--max-total-size", "5GB",
        "--no-warn-on-new-extensions",
    ])
    assert result.returncode == 1, f"Expected ADDED_SIZE violation.\nstdout: {result.stdout}"
    assert "ADDED_SIZE" in result.stdout


def test_max_added_size_no_violation(tmp_guardrails_env):
    """Adding data within the added-size threshold does not trigger."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"seed.txt": "initial"})
    do_backup(config, profile)

    # Add ~100 bytes; threshold is 1 MB
    build_tree(source, {"note.txt": "just a note"})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "1000",
        "--max-added-size", "1MB",
        "--max-total-size", "5GB",
        "--no-warn-on-new-extensions",
    ])
    assert result.returncode == 0, f"Expected no violation.\nstdout: {result.stdout}"


def test_max_total_size_violation(tmp_guardrails_env):
    """Total snapshot size above the absolute limit triggers the guardrail."""
    config, source, profile = tmp_guardrails_env

    # Create a 500 KB file of random bytes; set total-size limit to 100 KB
    build_tree(source, {"data.bin": os.urandom(500 * 1024)})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "100KB",
    ])
    assert result.returncode == 1, f"Expected TOTAL_SIZE violation.\nstdout: {result.stdout}"
    assert "TOTAL_SIZE" in result.stdout


def test_max_total_size_no_violation(tmp_guardrails_env):
    """Total snapshot within limit does not trigger."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"small.txt": "tiny"})
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "100.0",
        "--max-file-count-growth-ratio", "100.0",
        "--max-new-files", "1000",
        "--max-added-size", "100MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 0, f"Expected no violation.\nstdout: {result.stdout}"


def test_first_snapshot_only(tmp_guardrails_env):
    """Single snapshot with no previous: only total-size check runs."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"hello.txt": "world"})
    do_backup(config, profile)

    # Ratio checks are skipped (no previous snapshot), total size check runs
    result = run_guardrails(config, profile, [
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 0, f"Expected no violation on first snapshot.\nstdout: {result.stdout}"
    assert "No previous snapshot to compare" in result.stdout


def test_missing_profile_name(tmp_guardrails_env):
    """Guardrails exits with an error if PROFILE_NAME is not set."""
    import os
    import subprocess

    config, source, profile = tmp_guardrails_env
    build_tree(source, {"x.txt": "x"})
    do_backup(config, profile)

    env = {k: v for k, v in os.environ.items() if k != "PROFILE_NAME"}
    result = subprocess.run(
        [sys.executable, str(GUARDRAILS_SCRIPT),
         "--resticprofile-config", str(config)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "PROFILE_NAME" in result.stderr


def test_multiple_violations(tmp_guardrails_env):
    """Multiple guardrails can fire at once; all appear in output."""
    config, source, profile = tmp_guardrails_env

    build_tree(source, {"seed.txt": "x"})
    do_backup(config, profile)

    # Trigger size growth AND file count growth AND new extensions AND added size
    build_tree(source, {
        "big.bin":  _mb(2),
        "a.csv":    "1,2,3",
        **{f"f{i}.txt": f"{i}" for i in range(10)},
    })
    do_backup(config, profile)

    result = run_guardrails(config, profile, [
        "--max-growth-ratio", "1.1",
        "--max-file-count-growth-ratio", "1.1",
        "--max-new-files", "5",
        "--max-added-size", "1MB",
        "--max-total-size", "5GB",
    ])
    assert result.returncode == 1
    stdout = result.stdout
    assert "SIZE_GROWTH" in stdout
    assert "FILE_COUNT_GROWTH" in stdout
    assert "NEW_EXTENSIONS" in stdout
    assert "NEW_FILES" in stdout
    assert "ADDED_SIZE" in stdout
    assert "VIOLATIONS DETECTED" in stdout
