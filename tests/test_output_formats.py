"""
Output-format tests for restic_preview.py.

Verifies that all three output modes (stdout/terminal, .txt file, .html file)
produce structurally correct, non-empty output with the right sections.
No restic binary needed.
"""

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import build_tree, make_exclude_file

# Fixed tree used by all format tests
_TREE = {
    "main.py":       "print('hello')",
    "module.pyc":    b"\xed\x0d\x0d\x0a",
    "sub/helper.py": "# helper",
}
_PATTERNS = ["*.pyc"]


@pytest.fixture
def output_env(tmp_path):
    """Build the test tree and exclude file; return (source, excl)."""
    source = tmp_path / "source"
    build_tree(source, _TREE)
    excl = make_exclude_file(tmp_path, _PATTERNS)
    return source, excl


def _run(args: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "workspace_backup.preview", *args],
        capture_output=True, text=True, timeout=30, **kwargs,
    )


def _section_text(stdout: str, start_marker: str, end_marker: str) -> str:
    """Return the text between two section markers."""
    idx_start = stdout.find(start_marker)
    if idx_start == -1:
        return ""
    idx_end = stdout.find(end_marker, idx_start + len(start_marker))
    if idx_end == -1:
        return stdout[idx_start:]
    return stdout[idx_start:idx_end]


class _TagChecker(HTMLParser):
    """Minimal well-formedness check: every opened non-void tag must be closed."""
    VOID = {"area","base","br","col","embed","hr","img","input",
            "link","meta","param","source","track","wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"unexpected </{tag}>, open tags: {self.stack}")

    def assert_closed(self):
        if self.stack:
            self.errors.append(f"unclosed tags: {self.stack}")
        assert not self.errors, "\n".join(self.errors)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_stdout_output(output_env):
    source, excl = output_env
    result = _run([str(source), "--exclude-file", str(excl), "--show", "both"])

    assert result.returncode == 0, result.stderr
    out = result.stdout

    # Both sections present
    assert "INCLUDED FILES" in out
    assert "EXCLUDED BY RULE" in out

    # main.py is in the included section (before excluded section)
    included_section = _section_text(out, "INCLUDED FILES", "EXCLUDED BY RULE")
    assert "main.py" in included_section
    assert "sub" in included_section

    # module.pyc must NOT appear in the included section
    assert "module.pyc" not in included_section

    # A size value is present (e.g. "13  B" or "1.2 KB")
    assert re.search(r"\d+(\.\d+)?\s+(B|KB|MB|GB)", out)

    # Stats summary is present
    assert "Total size:" in out


def test_txt_file_output(output_env, tmp_path):
    source, excl = output_env
    out_file = tmp_path / "report.txt"
    result = _run([str(source), "--exclude-file", str(excl),
                   "--output", str(out_file)])

    assert result.returncode == 0, result.stderr
    assert out_file.exists()
    assert out_file.stat().st_size > 0

    content = out_file.read_text()
    assert "INCLUDED FILES" in content
    assert "main.py" in content
    assert re.search(r"\d+(\.\d+)?\s+(B|KB|MB|GB)", content)


def test_html_file_output(output_env, tmp_path):
    source, excl = output_env
    out_file = tmp_path / "report.html"
    result = _run([str(source), "--exclude-file", str(excl),
                   "--output", str(out_file)])

    assert result.returncode == 0, result.stderr
    assert out_file.exists()
    assert out_file.stat().st_size > 0

    content = out_file.read_text(encoding="utf-8")

    # Structural checks
    assert content.startswith("<!DOCTYPE html>")
    assert "</html>" in content
    assert "<body>" in content and "</body>" in content

    # Content checks
    assert "main.py" in content
    assert re.search(r"\d+(\.\d+)?\s*(B|KB|MB|GB)", content)

    # Well-formedness
    checker = _TagChecker()
    checker.feed(content)
    checker.assert_closed()
