"""
Unit tests for guardrails.py check functions.
No external tools (restic, resticprofile) required.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from guardrails import (
    _parse_size_to_bytes,
    check_added_size,
    check_file_count_growth,
    check_new_extensions,
    check_new_files_absolute,
    check_size_growth,
    check_total_size,
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
    diff = {"new_files": 0, "added_bytes": 5 * 1024**2}   # 5 MB
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


def test_added_size_violation():
    diff = {"new_files": 0, "added_bytes": 20 * 1024**2}  # 20 MB
    warnings = check_added_size(_cfg(max_added_size=10 * 1024**2), diff)
    assert len(warnings) == 1
    assert "ADDED_SIZE" in warnings[0]


def test_added_size_zero():
    diff = {"new_files": 0, "added_bytes": 0}
    assert check_added_size(_cfg(max_added_size=10 * 1024**2), diff) == []


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
