"""
Unit tests for guardrails.py check functions.
No external tools (restic, resticprofile) required.
"""

import os
import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import guardrails
from guardrails import (
    _parse_size_to_bytes,
    build_action_guide,
    build_details,
    build_details_html,
    build_short_message,
    check_added_size,
    check_file_count_growth,
    check_new_extensions,
    check_new_files_absolute,
    check_removed_files,
    check_removed_size,
    check_size_growth,
    check_total_size,
    format_rp_command,
    get_diff,
    render_guide_text,
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
        "max_removed_size": 50 * 1024**2,
        "max_removed_files": 100,
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


def test_new_files_absolute_lists_paths():
    diff = {"new_files": 150, "added_bytes": 0,
            "new_file_paths": [f"/project/file{i}.csv" for i in range(150)]}
    warnings = check_new_files_absolute(_cfg(max_new_files=100), diff)
    assert len(warnings) == 1
    msg = warnings[0]
    # First NEW_FILES_LISTED (20) paths are listed, the rest are summarized.
    assert "/project/file0.csv" in msg
    assert "/project/file19.csv" in msg
    assert "/project/file20.csv" not in msg
    assert "and 130 more" in msg


def test_new_files_absolute_lists_all_when_few():
    diff = {"new_files": 105, "added_bytes": 0,
            "new_file_paths": [f"/p/f{i}.txt" for i in range(105)][:5]}
    warnings = check_new_files_absolute(_cfg(max_new_files=100), diff)
    msg = warnings[0]
    assert "/p/f4.txt" in msg
    assert "more" not in msg


def test_new_files_absolute_no_paths_available():
    # Violation still reported even if path list is missing/empty.
    diff = {"new_files": 150, "added_bytes": 0, "new_file_paths": []}
    warnings = check_new_files_absolute(_cfg(max_new_files=100), diff)
    assert len(warnings) == 1
    assert "more" not in warnings[0]


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
    # Net removed = 5 MB — under 50 MB threshold.
    diff = {"added_bytes": 0, "removed_bytes": 5 * 1024**2}
    assert check_removed_size(_cfg(max_removed_size=50 * 1024**2), diff) == []


def test_removed_size_violation():
    # Net removed = 100 MB — over 50 MB threshold.
    diff = {"added_bytes": 0, "removed_bytes": 100 * 1024**2}
    warnings = check_removed_size(_cfg(max_removed_size=50 * 1024**2), diff)
    assert len(warnings) == 1
    assert "REMOVED_SIZE" in warnings[0]
    assert "net data removed" in warnings[0]


def test_removed_size_zero():
    diff = {"added_bytes": 0, "removed_bytes": 0}
    assert check_removed_size(_cfg(), diff) == []


def test_removed_size_file_modification_no_violation():
    # Files modified: removed ≈ added, net removed = 0.
    diff = {"added_bytes": 12 * 1024**2, "removed_bytes": 12 * 1024**2}
    assert check_removed_size(_cfg(max_removed_size=50 * 1024**2), diff) == []


# ── check_removed_files ───────────────────────────────────────────────────────

def test_removed_files_no_violation():
    # Net removed = 50 — under default threshold of 100.
    diff = {"new_files": 0, "removed_files": 50}
    assert check_removed_files(_cfg(max_removed_files=100), diff) == []


def test_removed_files_violation():
    # Net removed = 200 — over threshold.
    diff = {"new_files": 0, "removed_files": 200}
    warnings = check_removed_files(_cfg(max_removed_files=100), diff)
    assert len(warnings) == 1
    assert "REMOVED_FILES" in warnings[0]


def test_removed_files_zero():
    diff = {"new_files": 0, "removed_files": 0}
    assert check_removed_files(_cfg(), diff) == []


def test_removed_files_net_zero_move():
    # Files moved within backup scope: new_files ≈ removed_files, net = 0.
    diff = {"new_files": 300, "removed_files": 300}
    assert check_removed_files(_cfg(max_removed_files=100), diff) == []


def test_removed_files_exactly_at_limit():
    # Net = threshold — no violation (strict >).
    diff = {"new_files": 0, "removed_files": 100}
    assert check_removed_files(_cfg(max_removed_files=100), diff) == []


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


def test_build_details_omits_new_files_section():
    # New files are listed inline in the NEW_FILES violation, not duplicated here.
    diff = {"new_file_paths": [f"/project/file{i}.py" for i in range(60)],
            "modified_file_paths": [f"/src/mod{i}.py" for i in range(3)]}
    details = build_details("default", _SNAP, _PREV, [], diff)
    assert "New files (" not in details
    assert "/project/file0.py" not in details
    # Modified files are still listed.
    assert "Modified files (3)" in details
    assert "/src/mod0.py" in details


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


def test_run_notify_commands_details_html_file_stdin(tmp_path):
    # HTML body containing a single quote would break a quoted -b argument;
    # the *_file placeholder + stdin redirect must deliver it intact.
    out = tmp_path / "body.html"
    ctx = dict(_CTX, details_html="<style>font-family:'Segoe UI'</style><b>hi</b>")

    created = []
    real_mkstemp = guardrails.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    with patch("guardrails.tempfile.mkstemp", side_effect=spy):
        run_notify_commands([f"cat {{details_html_file}} > {out}"], ctx)

    assert out.read_text() == ctx["details_html"]
    # Temp file is removed after the command runs.
    assert created and all(not os.path.exists(p) for p in created)


def test_run_notify_commands_no_temp_file_when_unused():
    with patch("guardrails.tempfile.mkstemp") as mock_mkstemp, \
            patch("guardrails.subprocess.run"):
        run_notify_commands(["echo '{title}'"], _CTX)
    mock_mkstemp.assert_not_called()


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


# ── get_diff parsing ──────────────────────────────────────────────────────────

_DIFF_OUTPUT = """\
comparing snapshot c17dff7e to a301fbe1:

+    /data/new1.txt
+    /data/new2.txt
-    /data/gone.txt
M    /data/changed.txt

Files:           2 new,     1 removed,     1 changed
Data Blobs:      3 new,     1 removed
  Added:   1.464 KiB
  Removed: 758 B
"""


def test_get_diff_captures_all_change_kinds():
    with patch("guardrails.run_resticprofile", return_value=_DIFF_OUTPUT):
        d = get_diff("c17dff7e", "a301fbe1")
    assert d["new_file_paths"] == ["/data/new1.txt", "/data/new2.txt"]
    assert d["removed_file_paths"] == ["/data/gone.txt"]
    assert d["modified_file_paths"] == ["/data/changed.txt"]
    assert d["new_files"] == 2
    assert d["removed_files"] == 1


# ── generalized inline file listings ──────────────────────────────────────────

def test_size_growth_lists_added_and_modified_files():
    curr = {"total_size": 2000}
    prev = {"total_size": 1000}  # 2x growth
    diff = {"new_file_paths": ["/a/new.bin"], "modified_file_paths": ["/a/mod.py"]}
    warnings = check_size_growth(_cfg(), curr, prev, diff)
    assert len(warnings) == 1
    assert "+ /a/new.bin" in warnings[0]
    assert "M /a/mod.py" in warnings[0]


def test_size_growth_no_diff_headline_only():
    curr = {"total_size": 2000}
    prev = {"total_size": 1000}
    warnings = check_size_growth(_cfg(), curr, prev)  # diff_stats omitted
    assert len(warnings) == 1
    assert "\n" not in warnings[0]


def test_added_size_lists_files():
    diff = {"new_files": 0, "added_bytes": 20 * 1024**2, "removed_bytes": 0,
            "data_blobs_new": 5, "new_file_paths": ["/a/big.bin"],
            "modified_file_paths": []}
    warnings = check_added_size(_cfg(max_added_size=10 * 1024**2), diff)
    assert "+ /a/big.bin" in warnings[0]


def test_total_size_lists_files():
    diff = {"new_file_paths": ["/a/big.bin"], "modified_file_paths": []}
    warnings = check_total_size(_cfg(max_total_size=1000), {"total_size": 5000}, diff)
    assert "+ /a/big.bin" in warnings[0]


def test_removed_files_lists_removed_paths():
    diff = {"removed_files": 150, "new_files": 0,
            "removed_file_paths": [f"/gone/f{i}" for i in range(150)]}
    warnings = check_removed_files(_cfg(max_removed_files=100), diff)
    assert "- /gone/f0" in warnings[0]
    assert "and 130 more" in warnings[0]


def test_removed_size_lists_removed_paths():
    diff = {"new_files": 0, "added_bytes": 0, "removed_bytes": 80 * 1024**2,
            "removed_file_paths": ["/gone/big.bin"]}
    warnings = check_removed_size(_cfg(max_removed_size=10 * 1024**2), diff)
    assert "- /gone/big.bin" in warnings[0]


# ── format_rp_command ─────────────────────────────────────────────────────────

def test_format_rp_command_includes_profile():
    with patch.object(guardrails, "_PROFILE_NAME", "myprofile"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", ""):
        cmd = format_rp_command(["diff", "a", "b"])
    assert cmd == "resticprofile --name myprofile diff a b"


def test_format_rp_command_includes_config_when_set():
    with patch.object(guardrails, "_PROFILE_NAME", "default"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", "/etc/profiles.yaml"):
        cmd = format_rp_command(["stats", "abc"])
    assert "--config /etc/profiles.yaml" in cmd
    assert "--name default" in cmd


def test_format_rp_command_quotes_spaces():
    with patch.object(guardrails, "_PROFILE_NAME", "default"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", ""):
        cmd = format_rp_command(["rewrite", "--exclude", "/a b/c.txt"])
    assert "'/a b/c.txt'" in cmd


# ── build_action_guide ────────────────────────────────────────────────────────

def _guide_text(warnings, diff, current="a301fbe1", prev="c17dff7e"):
    with patch.object(guardrails, "_PROFILE_NAME", "default"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", ""):
        guide = build_action_guide(current, prev, warnings, diff)
        return render_guide_text(guide), guide


def test_action_guide_empty_without_warnings():
    assert build_action_guide("a301fbe1", "c17dff7e", [], {}) == []


def test_action_guide_always_has_inspect_diff():
    text, _ = _guide_text(["NEW_FILES: 138 new files added, threshold is 100"],
                          {"new_file_paths": ["/a/x.parquet"]})
    assert "diff c17dff7e a301fbe1" in text


def test_action_guide_new_files_has_rewrite_with_example_path():
    text, _ = _guide_text(["NEW_FILES: 138 new files added, threshold is 100"],
                          {"new_file_paths": ["/ml/data/batch.parquet"]})
    assert "rewrite --forget --exclude /ml/data/batch.parquet a301fbe1" in text
    assert "rewrite --forget --exclude /ml/data/batch.parquet" in text  # all-history
    assert "prune" in text
    assert "config/excludes.txt" in text


def test_action_guide_removed_files_offers_restore_not_rewrite():
    text, _ = _guide_text(["REMOVED_FILES: 150 files net removed in this run"],
                          {"removed_file_paths": ["/ml/keep.txt"]})
    assert "restore c17dff7e --include /ml/keep.txt" in text
    # No "remove unwanted data" remediation for a deletion-type violation.
    assert "Remediate — remove unwanted data" not in text


def test_action_guide_size_growth_has_raw_data_stats():
    text, _ = _guide_text(["SIZE_GROWTH: snapshot grew 2.00x"],
                          {"new_file_paths": [], "modified_file_paths": []})
    assert "stats a301fbe1 --mode raw-data" in text


def test_action_guide_uses_placeholder_when_no_paths():
    text, _ = _guide_text(["NEW_FILES: 138 new files added, threshold is 100"], {})
    assert "<PATH>" in text


# ── build_details with action guide ───────────────────────────────────────────

def test_build_details_includes_next_steps_on_violation():
    with patch.object(guardrails, "_PROFILE_NAME", "default"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", ""):
        details = build_details(
            "default", _SNAP, _PREV,
            ["NEW_FILES: 138 new files added, threshold is 100"],
            {"new_file_paths": ["/a/x.parquet"], "modified_file_paths": []},
        )
    assert "WHAT TO DO NEXT" in details
    assert "Remediate" in details
    assert "diff def456ab abc123ef" in details


def test_build_details_no_next_steps_when_clean():
    details = build_details("default", _SNAP, _PREV, [], None)
    assert "WHAT TO DO NEXT" not in details


# ── build_details_html ────────────────────────────────────────────────────────

def test_build_details_html_structure_and_escaping():
    with patch.object(guardrails, "_PROFILE_NAME", "default"), \
         patch.object(guardrails, "_RESTICPROFILE_CONFIG", ""):
        html = build_details_html(
            "default", _SNAP, _PREV,
            ["NEW_FILES: 1 new file added, threshold is 0\n  + /a/<b>&.txt"],
            {"new_file_paths": ["/a/<b>&.txt"], "modified_file_paths": []},
        )
    assert html.startswith("<!DOCTYPE html>")
    assert "badge warn" in html
    assert "NEW_FILES" in html
    assert "<pre><code>" in html
    # raw angle brackets / ampersand are escaped
    assert "<b>" not in html.replace("<body>", "")  # the path's <b> is escaped
    assert "&lt;b&gt;" in html
    assert "&amp;" in html


def test_build_details_html_ok_state():
    html = build_details_html("default", _SNAP, _PREV, [], None)
    assert "badge ok" in html
    assert "All guardrails passed" in html
    assert "<pre>" not in html  # no command blocks when clean
