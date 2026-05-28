"""
Unit tests for guardrails.py check functions.
No external tools (restic, resticprofile) required.
"""

import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from guardrails import (
    _parse_size_to_bytes,
    build_details,
    build_short_message,
    check_added_size,
    check_file_count_growth,
    check_new_extensions,
    check_new_files_absolute,
    check_removed_size,
    check_size_growth,
    check_total_size,
    run_notify_commands,
    write_log,
)


# ── _parse_size_to_bytes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("s, expected", [
    ("1B",      1),
    ("1KB",     1024),
    ("1KiB",    1024),
    ("1MB",     1024**2),
    ("1MiB",    1024**2),
    ("1GB",     1024**3),
    ("1GiB",    1024**3),
    ("1TB",     1024**4),
    ("1TiB",    1024**4),
    ("10MB",    10 * 1024**2),
    ("5GB",     5 * 1024**3),
    ("1.5 MiB", int(1.5 * 1024**2)),
    ("7.927 MiB", int(7.927 * 1024**2)),
    ("500KB",   500 * 1024),
])
def test_parse_size_to_bytes(s, expected):
    assert _parse_size_to_bytes(s) == expected


def test_parse_size_to_bytes_invalid():
    with pytest.raises(ValueError):
        _parse_size_to_bytes("notasize")

    with pytest.raises(ValueError):
        _parse_size_to_bytes("10XB")


# ── check_size_growth ─────────────────────────────────────────────────────────

def _cfg(**kwargs):
    base = {
        "max_growth_ratio": 1.2,
        "max_file_count_growth_ratio": 1.2,
        "max_new_files": 100,
        "max_added_size": 10 * 1024**2,
        "max_total_size": 5 * 1024**3,
        "warn_on_new_extensions": True,
    }
    base.update(kwargs)
    return base


def test_size_growth_no_violation():
    curr = {"total_size": 1000}
    prev = {"total_size": 900}  # 1.11x growth, under 1.2
    assert check_size_growth(_cfg(), curr, prev) == []


def test_size_growth_violation():
    curr = {"total_size": 2000}
    prev = {"total_size": 1000}  # 2.0x growth
    warnings = check_size_growth(_cfg(), curr, prev)
    assert len(warnings) == 1
    assert "SIZE_GROWTH" in warnings[0]
    assert "2.00x" in warnings[0]


def test_size_growth_no_prev():
    curr = {"total_size": 1000}
    assert check_size_growth(_cfg(), curr, {}) == []


def test_size_growth_zero_prev():
    curr = {"total_size": 1000}
    prev = {"total_size": 0}
    assert check_size_growth(_cfg(), curr, prev) == []


def test_size_growth_custom_threshold():
    curr = {"total_size": 1500}
    prev = {"total_size": 1000}  # 1.5x growth
    assert check_size_growth(_cfg(max_growth_ratio=1.6), curr, prev) == []
    assert len(check_size_growth(_cfg(max_growth_ratio=1.4), curr, prev)) == 1


# ── check_file_count_growth ───────────────────────────────────────────────────

def test_file_count_growth_no_violation():
    curr_files = [{}] * 10
    prev_files = [{}] * 9  # 1.11x
    assert check_file_count_growth(_cfg(), curr_files, prev_files) == []


def test_file_count_growth_violation():
    curr_files = [{}] * 30
    prev_files = [{}] * 10  # 3.0x
    warnings = check_file_count_growth(_cfg(), curr_files, prev_files)
    assert len(warnings) == 1
    assert "FILE_COUNT_GROWTH" in warnings[0]
    assert "3.00x" in warnings[0]


def test_file_count_growth_no_prev():
    assert check_file_count_growth(_cfg(), [{}] * 10, []) == []


# ── check_new_extensions ──────────────────────────────────────────────────────

def test_new_extensions_no_prev():
    curr = {".py": 5, ".txt": 2}
    assert check_new_extensions(_cfg(), curr, {}) == []


def test_new_extensions_no_violation():
    prev = {".py": 3, ".txt": 1}
    curr = {".py": 5, ".txt": 2}
    assert check_new_extensions(_cfg(), curr, prev) == []


def test_new_extensions_violation():
    prev = {".py": 3}
    curr  = {".py": 5, ".csv": 2}
    warnings = check_new_extensions(_cfg(), curr, prev)
    assert len(warnings) == 1
    assert "NEW_EXTENSIONS" in warnings[0]
    assert ".csv" in warnings[0]


def test_new_extensions_disabled():
    prev = {".py": 3}
    curr  = {".py": 5, ".csv": 2}
    assert check_new_extensions(_cfg(warn_on_new_extensions=False), curr, prev) == []


def test_new_extensions_multiple():
    prev = {".py": 1}
    curr  = {".py": 2, ".csv": 1, ".parquet": 1}
    warnings = check_new_extensions(_cfg(), curr, prev)
    assert len(warnings) == 1
    assert ".csv" in warnings[0]
    assert ".parquet" in warnings[0]


# ── check_new_files_absolute ──────────────────────────────────────────────────

def test_new_files_absolute_no_violation():
    diff = {"new_files": 50, "added_bytes": 0}
    assert check_new_files_absolute(_cfg(max_new_files=100), diff) == []


def test_new_files_absolute_violation():
    diff = {"new_files": 150, "added_bytes": 0}
    warnings = check_new_files_absolute(_cfg(max_new_files=100), diff)
    assert len(warnings) == 1
    assert "NEW_FILES" in warnings[0]
    assert "150" in warnings[0]


def test_new_files_absolute_exactly_at_limit():
    diff = {"new_files": 100, "added_bytes": 0}
    assert check_new_files_absolute(_cfg(max_new_files=100), diff) == []


def test_new_files_absolute_one_over():
    diff = {"new_files": 101, "added_bytes": 0}
    assert len(check_new_files_absolute(_cfg(max_new_files=100), diff)) == 1


# ── check_added_size ──────────────────────────────────────────────────────────

def test_added_size_no_violation():
    # Net = 5 MB (added only, no removed) — under threshold.
    diff = {"new_files": 0, "added_bytes": 5 * 1024**2, "removed_bytes": 0, "data_blobs_new": 3}
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_added_size_violation():
    # Net = 20 MB (added only) — over threshold.
    diff = {"new_files": 0, "added_bytes": 20 * 1024**2, "removed_bytes": 0, "data_blobs_new": 5}
    warnings = check_added_size(_cfg(max_added_size=10 * 1024**2), diff)
    assert len(warnings) == 1
    assert "ADDED_SIZE" in warnings[0]
    assert "net new data" in warnings[0]


def test_added_size_zero():
    diff = {"new_files": 0, "added_bytes": 0, "removed_bytes": 0, "data_blobs_new": 0}
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_added_size_tree_only_no_violation():
    # Tree-blob churn: added ≈ removed, net = 0.
    diff = {"new_files": 0, "added_bytes": 12 * 1024**2, "removed_bytes": 12 * 1024**2, "data_blobs_new": 0}
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_added_size_file_modification_no_violation():
    # Files modified: gross Added and Removed both large, net ≈ 0.
    diff = {"new_files": 0, "added_bytes": 12 * 1024**2, "removed_bytes": 12 * 1024**2, "data_blobs_new": 16}
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_added_size_with_data_blobs_violation():
    # Truly new large data (no matching removed).
    diff = {"new_files": 2, "added_bytes": 50 * 1024**2, "removed_bytes": 0, "data_blobs_new": 12}
    warnings = check_added_size(_cfg(max_added_size=10 * 1024**2), diff)
    assert len(warnings) == 1
    assert "ADDED_SIZE" in warnings[0]
    assert "net new data" in warnings[0]


# ── check_removed_size ────────────────────────────────────────────────────────

def test_removed_size_no_violation():
    # Net removed = 5 MB — under threshold.
    diff = {"added_bytes": 0, "removed_bytes": 5 * 1024**2}
    assert check_removed_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_removed_size_violation():
    # Net removed = 20 MB — over threshold.
    diff = {"added_bytes": 0, "removed_bytes": 20 * 1024**2}
    warnings = check_removed_size(_cfg(max_added_size=10 * 1024**2), diff)
    assert len(warnings) == 1
    assert "REMOVED_SIZE" in warnings[0]
    assert "net data removed" in warnings[0]


def test_removed_size_zero():
    diff = {"added_bytes": 0, "removed_bytes": 0}
    assert check_removed_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_removed_size_file_modification_no_violation():
    # Files modified: removed ≈ added, net removed = 0.
    diff = {"added_bytes": 12 * 1024**2, "removed_bytes": 12 * 1024**2}
    assert check_removed_size(_cfg(max_added_size=10 * 1024**2), diff) == []


# ── check_total_size ──────────────────────────────────────────────────────────

def test_total_size_no_violation():
    curr = {"total_size": 1 * 1024**3}   # 1 GB
    assert check_total_size(_cfg(max_total_size=5 * 1024**3), curr) == []


def test_total_size_violation():
    curr = {"total_size": 10 * 1024**3}  # 10 GB
    warnings = check_total_size(_cfg(max_total_size=5 * 1024**3), curr)
    assert len(warnings) == 1
    assert "TOTAL_SIZE" in warnings[0]


def test_total_size_exactly_at_limit():
    curr = {"total_size": 5 * 1024**3}
    assert check_total_size(_cfg(max_total_size=5 * 1024**3), curr) == []


def test_total_size_missing_key():
    assert check_total_size(_cfg(max_total_size=5 * 1024**3), {}) == []


# ── build_short_message ───────────────────────────────────────────────────────

_SNAP = {"id": "abc123ef0000", "time": "2026-05-27T10:05:30+01:00"}
_PREV = {"id": "def456ab0000", "time": "2026-05-27T09:45:00+01:00"}


def test_build_short_message_no_violations():
    msg = build_short_message("default", [])
    assert "All guardrails passed" in msg
    assert "default" in msg


def test_build_short_message_single_violation():
    warnings = ["SIZE_GROWTH: snapshot grew 2.00x (0.45 GB → 0.95 GB), threshold is 1.20x"]
    msg = build_short_message("default", warnings)
    assert "1 violation(s)" in msg
    assert "SIZE_GROWTH" in msg


def test_build_short_message_multiple_violations():
    warnings = [
        "SIZE_GROWTH: grew 2x",
        "NEW_FILES: 150 new files",
        "ADDED_SIZE: 20 MiB added",
    ]
    msg = build_short_message("myprofile", warnings)
    assert "3 violation(s)" in msg
    assert "SIZE_GROWTH" in msg
    assert "NEW_FILES" in msg
    assert "ADDED_SIZE" in msg


def test_build_short_message_truncates_beyond_3():
    warnings = [f"WARN_{i}: something" for i in range(5)]
    msg = build_short_message("p", warnings)
    assert "5 violation(s)" in msg
    assert "2 more violation(s)" in msg


def test_build_short_message_with_new_files():
    diff = {"new_file_paths": ["/a/b/file1.csv", "/a/b/file2.bin", "/a/b/file3.dat",
                                "/a/b/file4.py"]}
    msg = build_short_message("p", ["NEW_FILES: 4 new files"], diff)
    assert "file1.csv" in msg
    assert "+1 more" in msg


# ── build_details ─────────────────────────────────────────────────────────────

def test_build_details_contains_header():
    details = build_details("default", _SNAP, _PREV, [], None)
    assert "Backup Guardrail Report" in details
    assert "default" in details
    assert "abc123ef" in details
    assert "def456ab" in details


def test_build_details_lists_violations():
    warnings = ["SIZE_GROWTH: grew 2x", "NEW_FILES: 50 added"]
    details = build_details("default", _SNAP, _PREV, warnings, None)
    assert "VIOLATIONS (2)" in details
    assert "SIZE_GROWTH" in details
    assert "NEW_FILES" in details


def test_build_details_lists_new_files():
    diff = {"new_file_paths": [f"/project/file{i}.py" for i in range(60)],
            "modified_file_paths": []}
    details = build_details("default", _SNAP, _PREV, [], diff)
    assert "New files (60)" in details
    assert "/project/file0.py" in details
    assert "/project/file49.py" in details
    assert "/project/file50.py" not in details
    assert "and 10 more" in details


def test_build_details_caps_modified_files():
    diff = {"new_file_paths": [],
            "modified_file_paths": [f"/src/mod{i}.py" for i in range(25)]}
    details = build_details("default", _SNAP, _PREV, [], diff)
    assert "Modified files (25)" in details
    assert "/src/mod0.py" in details
    assert "/src/mod19.py" in details
    assert "/src/mod20.py" not in details
    assert "and 5 more" in details


def test_build_details_no_prev_snapshot():
    details = build_details("default", _SNAP, None, [], None)
    assert "none" in details.lower() or "first backup" in details.lower()
    assert "abc123ef" in details


# ── run_notify_commands ───────────────────────────────────────────────────────

_CTX = {
    "title": "Backup WARNING: 1 violation(s) in 'default'",
    "message": "short message",
    "details": "long details",
    "profile": "default",
    "status": "WARNING",
    "violations": "1",
}


def test_run_notify_commands_substitutes_template():
    with patch("guardrails.subprocess.run") as mock_run:
        run_notify_commands(["echo '{title}' '{message}'"], _CTX)
    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert _CTX["title"] in cmd
    assert _CTX["message"] in cmd


def test_run_notify_commands_no_env_vars_leaked():
    with patch("guardrails.subprocess.run") as mock_run:
        run_notify_commands(["echo hi"], _CTX)
    _, kwargs = mock_run.call_args
    assert "env" not in kwargs


def test_run_notify_commands_multiple_templates():
    templates = ["echo '{title}'", "echo '{message}'", "echo '{profile}'"]
    with patch("guardrails.subprocess.run") as mock_run:
        run_notify_commands(templates, _CTX)
    assert mock_run.call_count == 3


def test_run_notify_commands_empty_list():
    with patch("guardrails.subprocess.run") as mock_run:
        run_notify_commands([], _CTX)
    mock_run.assert_not_called()


def test_run_notify_commands_failed_silently():
    with patch("guardrails.subprocess.run", side_effect=RuntimeError("boom")):
        run_notify_commands(["echo hi"], _CTX)  # must not raise


def test_run_notify_commands_unknown_placeholder(capsys):
    with patch("guardrails.subprocess.run"):
        run_notify_commands(["cmd {unknown_key}"], _CTX)
    captured = capsys.readouterr()
    assert "unknown_key" in captured.err or "unknown placeholder" in captured.err.lower()


# ── write_log ─────────────────────────────────────────────────────────────────

def test_write_log_creates_file(tmp_path):
    log = tmp_path / "guardrails.log"
    write_log(str(log), "hello world")
    text = log.read_text()
    assert "### " in text
    assert "hello world" in text


def test_write_log_multiple_runs(tmp_path):
    log = tmp_path / "guardrails.log"
    write_log(str(log), "run 1 content")
    write_log(str(log), "run 2 content")
    text = log.read_text()
    assert "run 1 content" in text
    assert "run 2 content" in text
    assert text.count("### ") == 2


def test_write_log_keeps_last_n(tmp_path):
    log = tmp_path / "guardrails.log"
    for i in range(5):
        write_log(str(log), f"run {i}", keep_runs=3)
    text = log.read_text()
    assert text.count("### ") == 3
    assert "run 2" in text
    assert "run 3" in text
    assert "run 4" in text
    assert "run 0" not in text
    assert "run 1" not in text


def test_write_log_empty_path_noop(tmp_path):
    write_log("", "should not create file")
    assert list(tmp_path.iterdir()) == []


def test_write_log_bad_path_silent(capsys):
    write_log("/nonexistent/dir/guardrails.log", "content")  # must not raise
    captured = capsys.readouterr()
    assert "Warning" in captured.err or captured.err == ""
