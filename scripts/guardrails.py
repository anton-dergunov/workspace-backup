"""
Post-backup guardrails: check for unexpected growth, new file types, etc.
Runs as resticprofile run-after hook. Exit non-zero on guardrail violation.

Environment (set automatically by resticprofile):
  PROFILE_NAME  — resticprofile profile name

Usage in profiles.yaml run-after:
  python3 /path/to/guardrails.py
    --max-growth-ratio 1.2 --max-new-files 100
    --max-added-size 10MB --max-removed-size 50MB --max-removed-files 100
    --log ~/resticprofile-guardrails.log
    --notify-short "apprise -t '{title}' -b '{message}' macosx://"
    --notify-long  "apprise -i html -t '{title}' mailto://... < {details_html_file}"

Template placeholders for --notify-short / --notify-long:
  {title}             — one-line notification title
  {message}           — compact multi-line summary (suitable for desktop)
  {details}           — full plain-text report with file listings and next-step commands
  {details_html}      — same report as a styled HTML document (use with apprise -i html)
  {details_file}      — path to a temp file holding {details} (auto-created/removed)
  {details_html_file} — path to a temp file holding {details_html} (auto-created/removed)
  {profile}           — resticprofile profile name
  {status}            — "WARNING" or "OK"
  {violations}        — number of violations as a string

For large bodies (especially HTML), prefer the *_file placeholders and redirect
them into the sender's stdin (e.g. "apprise ... < {details_html_file}"). Inlining
{details_html} into a quoted -b argument is fragile: any quote character in the
content can break shell parsing and produce an empty body.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from html import escape as _esc

# Set at startup from CLI args / env
_PROFILE_NAME: str = ""
_RESTICPROFILE_CONFIG: str = ""

# How many new file paths to list inline in the NEW_FILES violation message.
NEW_FILES_LISTED = 20


# ── Size parsing ──────────────────────────────────────────────────────────────

def _parse_size_to_bytes(s: str) -> int:
    """Parse size strings like '10MB', '5GiB', '1.5 KiB' to bytes."""
    units = {
        "b": 1,
        "kb": 1024, "kib": 1024,
        "mb": 1024**2, "mib": 1024**2,
        "gb": 1024**3, "gib": 1024**3,
        "tb": 1024**4, "tib": 1024**4,
    }
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)", s.strip())
    if not m:
        raise ValueError(f"Invalid size: {s!r}. Examples: 10MB, 5GiB, 500KB")
    value, unit = float(m.group(1)), m.group(2).lower()
    factor = units.get(unit)
    if factor is None:
        raise ValueError(f"Unknown size unit {unit!r} in {s!r}")
    return int(value * factor)


def _argparse_size(s: str) -> int:
    """Argparse type wrapper for _parse_size_to_bytes."""
    try:
        return _parse_size_to_bytes(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


# ── resticprofile runner ──────────────────────────────────────────────────────

def run_resticprofile(args: list) -> str:
    """Run resticprofile command and return stdout."""
    cmd = ["resticprofile"]
    if _RESTICPROFILE_CONFIG:
        cmd += ["--config", _RESTICPROFILE_CONFIG]
    cmd += ["--name", _PROFILE_NAME] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"ERROR: resticprofile command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stdout: {e.stdout}", file=sys.stderr)
        print(f"  stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: resticprofile not found. Install with: brew install resticprofile", file=sys.stderr)
        sys.exit(1)


# ── Data retrieval ────────────────────────────────────────────────────────────

def _extract_json(output: str, opening: str):
    """
    Extract the first complete JSON value from output that may contain
    resticprofile log lines before and after the JSON.
    Uses raw_decode to stop at the end of the first complete value.
    """
    start = output.find(opening)
    if start == -1:
        return None
    value, _ = json.JSONDecoder().raw_decode(output, start)
    return value


def get_snapshots(limit: int = 2) -> list:
    """Get the latest N snapshots as JSON."""
    output = run_resticprofile(["snapshots", f"--latest={limit}", "--json"])
    snapshots = _extract_json(output, "[")
    if snapshots is None:
        return []
    return sorted(snapshots, key=lambda s: s["time"])


def get_stats(snapshot_id: str) -> dict:
    """Get stats for a snapshot."""
    output = run_resticprofile(["stats", snapshot_id, "--json"])
    result = _extract_json(output, "{")
    return result if result is not None else {}


def get_files(snapshot_id: str) -> list:
    """Get file listing for a snapshot (newline-delimited JSON objects)."""
    output = run_resticprofile(["ls", snapshot_id, "--json"])
    files = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                files.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return files


def get_diff(snap1_id: str, snap2_id: str) -> dict:
    """
    Get diff stats between two snapshots.

    Returns dict with:
      new_files (int), added_bytes (int),
      new_file_paths (list[str]), modified_file_paths (list[str])
    """
    output = run_resticprofile(["diff", snap1_id, snap2_id])
    result: dict = {
        "new_files": 0,
        "removed_files": 0,
        "added_bytes": 0,
        "removed_bytes": 0,
        "data_blobs_new": 0,
        "new_file_paths": [],
        "removed_file_paths": [],
        "modified_file_paths": [],
    }
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Files:"):
            m = re.search(r"(\d+)\s+new", stripped)
            if m:
                result["new_files"] = int(m.group(1))
            m2 = re.search(r"(\d+)\s+removed", stripped)
            if m2:
                result["removed_files"] = int(m2.group(1))
        elif stripped.startswith("Data Blobs:"):
            m = re.search(r"(\d+)\s+new", stripped)
            if m:
                result["data_blobs_new"] = int(m.group(1))
        elif stripped.startswith("Added:"):
            m = re.match(r"Added:\s+(.+)$", stripped)
            if m:
                try:
                    result["added_bytes"] = _parse_size_to_bytes(m.group(1).strip())
                except ValueError:
                    pass
        elif stripped.startswith("Removed:"):
            m = re.match(r"Removed:\s+(.+)$", stripped)
            if m:
                try:
                    result["removed_bytes"] = _parse_size_to_bytes(m.group(1).strip())
                except ValueError:
                    pass
        elif len(line) >= 2 and line[0] in ("+", "-", "M") and line[1] == " ":
            # restic diff lists changes as "<indicator>    <path>" (indicator
            # left-justified in 5 columns, so the path starts at column 0+pad).
            indicator = line[0]
            path = line[1:].strip()
            if path:
                if indicator == "+":
                    result["new_file_paths"].append(path)
                elif indicator == "-":
                    result["removed_file_paths"].append(path)
                elif indicator == "M":
                    result["modified_file_paths"].append(path)
    return result


def extract_extensions(files: list) -> dict:
    """Extract unique extensions and their counts from file listing."""
    extensions = defaultdict(int)
    for entry in files:
        if entry.get("type") == "file" and entry.get("name"):
            name = entry["name"]
            if "." in name:
                ext = "." + name.split(".")[-1].lower()
                extensions[ext] += 1
    return dict(extensions)


# ── Shared formatting helpers ─────────────────────────────────────────────────

def format_rp_command(args: list) -> str:
    """Format a copy-pasteable resticprofile command line for notifications.

    Mirrors run_resticprofile()'s prefix so the commands shown to the user match
    how the script itself talks to the repository.
    """
    parts = ["resticprofile"]
    if _RESTICPROFILE_CONFIG:
        parts += ["--config", _RESTICPROFILE_CONFIG]
    parts += ["--name", _PROFILE_NAME or "<profile>"]
    parts += args
    return " ".join(shlex.quote(p) for p in parts)


def _file_listing(entries: list, limit: int = NEW_FILES_LISTED) -> str:
    """Render up to `limit` change entries as indented lines.

    entries: ordered list of (marker, path), marker in {"+", "-", "M"}.
    Returns "" when there is nothing to list.
    """
    lines = []
    for marker, path in entries[:limit]:
        lines.append(f"  {marker} {path}")
    if len(entries) > limit:
        lines.append(f"  ... and {len(entries) - limit:,} more")
    return "\n".join(lines)


def _append_listing(message: str, entries: list, limit: int = NEW_FILES_LISTED) -> str:
    """Append a file listing to a violation message, if there is anything to show."""
    listing = _file_listing(entries, limit)
    return f"{message}\n{listing}" if listing else message


# ── Guardrail checks ──────────────────────────────────────────────────────────

def check_size_growth(config: dict, curr_stats: dict, prev_stats: dict,
                      diff_stats: dict | None = None) -> list:
    """Warn if snapshot grew more than max_growth_ratio vs previous."""
    warnings = []
    if not prev_stats or "total_size" not in prev_stats:
        return warnings
    prev_size = prev_stats.get("total_size", 0)
    curr_size = curr_stats.get("total_size", 0)
    if prev_size > 0:
        ratio = curr_size / prev_size
        if ratio > config["max_growth_ratio"]:
            msg = (
                f"SIZE_GROWTH: snapshot grew {ratio:.2f}x "
                f"({prev_size / (1<<30):.2f} GB → {curr_size / (1<<30):.2f} GB), "
                f"threshold is {config['max_growth_ratio']:.2f}x"
            )
            warnings.append(_append_listing(msg, _added_entries(diff_stats or {})))
    return warnings


def check_file_count_growth(config: dict, curr_files: list, prev_files: list,
                            diff_stats: dict | None = None) -> list:
    """Warn if file count grew more than max_file_count_growth_ratio vs previous."""
    warnings = []
    prev_count = len(prev_files) if prev_files else 0
    curr_count = len(curr_files)
    if prev_count > 0:
        ratio = curr_count / prev_count
        if ratio > config["max_file_count_growth_ratio"]:
            msg = (
                f"FILE_COUNT_GROWTH: count jumped {ratio:.2f}x "
                f"({prev_count:,} → {curr_count:,}), "
                f"threshold is {config['max_file_count_growth_ratio']:.2f}x"
            )
            entries = [("+", p) for p in (diff_stats or {}).get("new_file_paths", [])]
            warnings.append(_append_listing(msg, entries))
    return warnings


def check_new_extensions(config: dict, curr_exts: dict, prev_exts: dict) -> list:
    """Warn if new file extensions appeared that weren't in the previous backup."""
    warnings = []
    if not config.get("warn_on_new_extensions", True):
        return warnings
    if not prev_exts:
        return warnings
    new_exts = set(curr_exts.keys()) - set(prev_exts.keys())
    if new_exts:
        ext_list = ", ".join(sorted(new_exts))
        warning = f"NEW_EXTENSIONS: found {len(new_exts)} new file type(s): {ext_list}\n"
        for ext in sorted(new_exts):
            warning += f"  {ext}: {curr_exts[ext]} file(s)\n"
        warnings.append(warning.rstrip())
    return warnings


def _added_entries(diff_stats: dict) -> list:
    """(marker, path) entries for files added/modified in this run."""
    return (
        [("+", p) for p in diff_stats.get("new_file_paths", [])]
        + [("M", p) for p in diff_stats.get("modified_file_paths", [])]
    )


def _removed_entries(diff_stats: dict) -> list:
    """(marker, path) entries for files removed in this run."""
    return [("-", p) for p in diff_stats.get("removed_file_paths", [])]


def check_new_files_absolute(config: dict, diff_stats: dict) -> list:
    """Warn if too many new files were added in this backup run.

    Lists the first NEW_FILES_LISTED new file paths inline so the notification
    shows which files triggered the violation.
    """
    warnings = []
    new_count = diff_stats.get("new_files", 0)
    if new_count > config["max_new_files"]:
        msg = (
            f"NEW_FILES: {new_count:,} new files added, "
            f"threshold is {config['max_new_files']:,}"
        )
        entries = [("+", p) for p in diff_stats.get("new_file_paths", [])]
        warnings.append(_append_listing(msg, entries))
    return warnings


def check_added_size(config: dict, diff_stats: dict) -> list:
    """Warn if net new data added in this run exceeds threshold.

    Uses Added−Removed so that file modifications (matching added/removed blobs)
    don't trigger false positives.
    """
    warnings = []
    net_bytes = max(0, diff_stats.get("added_bytes", 0) - diff_stats.get("removed_bytes", 0))
    max_bytes = config["max_added_size"]
    if net_bytes > max_bytes:
        msg = (
            f"ADDED_SIZE: {net_bytes / (1<<20):.1f} MiB net new data in this run "
            f"(added {diff_stats['added_bytes'] / (1<<20):.1f} MiB, "
            f"removed {diff_stats['removed_bytes'] / (1<<20):.1f} MiB), "
            f"threshold is {max_bytes / (1<<20):.1f} MiB"
        )
        warnings.append(_append_listing(msg, _added_entries(diff_stats)))
    return warnings


def check_removed_size(config: dict, diff_stats: dict) -> list:
    """Warn if net data removed in this run exceeds threshold.

    Uses Removed−Added so that file modifications (matching removed/added blobs)
    don't trigger false positives.
    """
    warnings = []
    net_removed = max(0, diff_stats.get("removed_bytes", 0) - diff_stats.get("added_bytes", 0))
    max_bytes = config["max_removed_size"]
    if net_removed > max_bytes:
        msg = (
            f"REMOVED_SIZE: {net_removed / (1<<20):.1f} MiB net data removed in this run "
            f"(added {diff_stats['added_bytes'] / (1<<20):.1f} MiB, "
            f"removed {diff_stats['removed_bytes'] / (1<<20):.1f} MiB), "
            f"threshold is {max_bytes / (1<<20):.1f} MiB"
        )
        warnings.append(_append_listing(msg, _removed_entries(diff_stats)))
    return warnings


def check_removed_files(config: dict, diff_stats: dict) -> list:
    """Warn if too many files were net-removed in this backup run.

    Uses removed−new so that moves within the backup scope (equal removed and
    new counts) don't trigger false positives.
    """
    warnings = []
    net_removed = max(0, diff_stats.get("removed_files", 0) - diff_stats.get("new_files", 0))
    if net_removed > config["max_removed_files"]:
        msg = (
            f"REMOVED_FILES: {net_removed:,} files net removed in this run "
            f"(threshold is {config['max_removed_files']:,})"
        )
        warnings.append(_append_listing(msg, _removed_entries(diff_stats)))
    return warnings


def check_total_size(config: dict, curr_stats: dict,
                     diff_stats: dict | None = None) -> list:
    """Warn if total snapshot size exceeds absolute limit."""
    warnings = []
    total = curr_stats.get("total_size", 0)
    max_bytes = config["max_total_size"]
    if total > max_bytes:
        msg = (
            f"TOTAL_SIZE: snapshot is {total / (1<<30):.2f} GB, "
            f"threshold is {max_bytes / (1<<30):.2f} GB"
        )
        warnings.append(_append_listing(msg, _added_entries(diff_stats or {})))
    return warnings


# ── Notification content builders ─────────────────────────────────────────────

def build_short_message(
    profile: str,
    all_warnings: list,
    diff_stats: dict | None = None,
) -> str:
    """
    Compact multi-line message suitable for desktop notifications.
    Shows up to 3 violation summaries and the top new files.
    """
    if not all_warnings:
        return f"[{profile}] All guardrails passed"

    lines = [f"[{profile}] {len(all_warnings)} violation(s) detected"]
    for w in all_warnings[:3]:
        lines.append(w.splitlines()[0])
    if len(all_warnings) > 3:
        lines.append(f"... and {len(all_warnings) - 3} more violation(s)")

    new_paths = (diff_stats or {}).get("new_file_paths", [])
    if new_paths:
        names = [os.path.basename(p) for p in new_paths[:3]]
        suffix = f" (+{len(new_paths) - 3} more)" if len(new_paths) > 3 else ""
        lines.append(f"New files: {', '.join(names)}{suffix}")

    return "\n".join(lines)


def build_action_guide(
    current_id: str,
    prev_id: str | None,
    warnings: list,
    diff_stats: dict | None,
) -> list:
    """
    Build investigate/remediate sections tailored to which violations fired.

    Each section is a dict:
      {"title": str, "note": str | None,
       "steps": [(comment: str, [command: str, ...]), ...]}

    Commands are concrete resticprofile invocations using the real snapshot IDs
    and an example path from the diff (when available), so the user can copy,
    adjust the path/pattern, and run them.
    """
    if not warnings or not current_id:
        return []

    codes = {w.split(":", 1)[0].strip() for w in warnings}
    diff_stats = diff_stats or {}

    def example(paths_key: str, fallback: str = "<PATH>") -> str:
        paths = diff_stats.get(paths_key, [])
        return paths[0] if paths else fallback

    sections: list = []

    # ── Always: inspect what changed ──
    inspect_steps = []
    if prev_id:
        inspect_steps.append(
            ("See everything that changed between the two snapshots",
             [format_rp_command(["diff", prev_id, current_id])]))
    inspect_steps.append(
        ("List files in the new snapshot with sizes (spot the large/unexpected ones)",
         [format_rp_command(["ls", "-l", current_id])]))
    inspect_steps.append(
        ("Snapshot statistics",
         [format_rp_command(["stats", current_id])]))
    sections.append({"title": "Investigate — what changed",
                     "note": None, "steps": inspect_steps})

    added_like = codes & {"NEW_FILES", "FILE_COUNT_GROWTH", "ADDED_SIZE",
                          "NEW_EXTENSIONS", "SIZE_GROWTH", "TOTAL_SIZE"}
    size_like = codes & {"SIZE_GROWTH", "TOTAL_SIZE", "ADDED_SIZE"}
    removed_like = codes & {"REMOVED_FILES", "REMOVED_SIZE"}

    if size_like:
        cmds = [format_rp_command(["stats", current_id, "--mode", "raw-data"])]
        if prev_id:
            cmds.append(format_rp_command(["stats", prev_id, "--mode", "raw-data"]))
        sections.append({
            "title": "Investigate — size",
            "note": None,
            "steps": [("Compare raw (deduplicated) data size of each snapshot", cmds)],
        })

    if "NEW_EXTENSIONS" in codes:
        sections.append({
            "title": "Investigate — new file types",
            "note": "Replace <EXT> with the flagged extension, e.g. parquet.",
            "steps": [("Find every file of a given type across all snapshots",
                       [format_rp_command(["find", "*.<EXT>"])])],
        })

    if added_like:
        path = example("new_file_paths")
        steps = [
            ("Preview removing an unwanted path from the latest snapshot (no changes made)",
             [format_rp_command(["rewrite", "--dry-run", "--exclude", path, current_id])]),
            ("Remove it from the latest snapshot",
             [format_rp_command(["rewrite", "--forget", "--exclude", path, current_id])]),
            ("Remove it from ALL snapshots in history",
             [format_rp_command(["rewrite", "--forget", "--exclude", path])]),
            ("Reclaim disk space afterwards",
             [format_rp_command(["prune"])]),
            ("Stop it being backed up again: add a pattern to config/excludes.txt "
             "(e.g. '*.parquet' or a directory path)", []),
        ]
        if codes & {"SIZE_GROWTH", "TOTAL_SIZE"}:
            steps.append(
                ("Or drop old snapshots per your retention policy, then prune",
                 [format_rp_command(["forget", "--prune"])]))
        sections.append({
            "title": "Remediate — remove unwanted data",
            "note": ("These rewrite snapshot history — review carefully. --exclude "
                     "takes restic patterns (e.g. '*.parquet', '/dir/**'); the example "
                     "path below is just the first flagged file."),
            "steps": steps,
        })

    if removed_like:
        path = example("removed_file_paths")
        steps = []
        if prev_id:
            steps.append(
                ("Restore an accidentally removed file from the previous snapshot",
                 [format_rp_command(["restore", prev_id, "--include", path,
                                     "--target", "<RESTORE_DIR>"])]))
        steps.append(("If the deletion was intentional, no action is needed.", []))
        sections.append({
            "title": "Remediate — recover removed data",
            "note": None,
            "steps": steps,
        })

    sections.append({
        "title": "If resticprofile intercepts a flag",
        "note": "Run restic directly after exporting the repository credentials.",
        "steps": [("Example", [
            "export RESTIC_REPOSITORY=... RESTIC_PASSWORD=...",
            f"restic rewrite --forget --exclude '<PATH>' {current_id}",
        ])],
    })

    return sections


def _rule(title: str, width: int = 70) -> str:
    """A section rule like '── Title ─────…'."""
    return f"── {title} ".ljust(width, "─")


def render_guide_text(guide: list) -> str:
    """Render an action guide (from build_action_guide) as plain text."""
    if not guide:
        return ""
    out: list = []
    for section in guide:
        out.append(_rule(section["title"]))
        if section.get("note"):
            out.append(section["note"])
        out.append("")
        for comment, cmds in section["steps"]:
            out.append(f"# {comment}")
            out.extend(cmds)
            out.append("")
    return "\n".join(out).rstrip()


def build_details(
    profile: str,
    current_snapshot: dict,
    prev_snapshot: dict | None,
    all_warnings: list,
    diff_stats: dict | None,
) -> str:
    """
    Full multi-line report with violation details and file listings.
    Suitable for email or log files.
    """
    sep = "=" * 70
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        sep,
        "Backup Guardrail Report",
        sep,
        f"Profile:          {profile}",
        f"Time:             {ts}",
        f"Current snapshot: {current_snapshot['id'][:8]} ({current_snapshot['time']})",
    ]
    if prev_snapshot:
        lines.append(
            f"Previous snapshot: {prev_snapshot['id'][:8]} ({prev_snapshot['time']})"
        )
    else:
        lines.append("Previous snapshot: (none — first backup)")
    lines.append("")

    if all_warnings:
        lines.append(f"VIOLATIONS ({len(all_warnings)}):")
        lines.append("")
        for w in all_warnings:
            lines.append(w)
            lines.append("")
    else:
        lines.append("All guardrails passed.")
        lines.append("")

    if diff_stats:
        # New files are listed inline in the NEW_FILES violation (when it fires),
        # so only the modified-files listing is added here to avoid duplication.
        mod_paths = diff_stats.get("modified_file_paths", [])

        if mod_paths:
            lines.append(f"Modified files ({len(mod_paths)}):")
            for p in mod_paths[:20]:
                lines.append(f"  M {p}")
            if len(mod_paths) > 20:
                lines.append(f"  ... and {len(mod_paths) - 20} more")
            lines.append("")

    if all_warnings:
        guide = build_action_guide(
            current_snapshot["id"][:8],
            prev_snapshot["id"][:8] if prev_snapshot else None,
            all_warnings,
            diff_stats,
        )
        guide_text = render_guide_text(guide)
        if guide_text:
            lines.append("WHAT TO DO NEXT")
            lines.append("")
            lines.append(guide_text)
            lines.append("")

    return "\n".join(lines)


# ── HTML notification (for email clients that render it) ──────────────────────

_EMAIL_CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  color:#24292f;background:#f6f8fa;margin:0;padding:24px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;background:#fff;border:1px solid #d0d7de;
  border-radius:8px;overflow:hidden}
.head{padding:18px 24px;border-bottom:1px solid #d0d7de}
.head h1{font-size:18px;margin:0 0 6px}
.sub{font-size:13px;color:#57606a}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:2px 10px;
  border-radius:999px;color:#fff;vertical-align:middle}
.badge.warn{background:#cf222e}.badge.ok{background:#1a7f37}
.sec{padding:16px 24px;border-bottom:1px solid #eaeef2}
.sec h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;
  color:#57606a;margin:0 0 10px}
table.meta{border-collapse:collapse;font-size:13px}
table.meta td{padding:2px 12px 2px 0;vertical-align:top}
table.meta td.k{color:#57606a;white-space:nowrap}
.viol{border-left:4px solid #cf222e;background:#fff8f8;padding:10px 14px;
  border-radius:4px;margin-bottom:12px}
.viol .code{font-weight:700;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.viol .msg{color:#57606a;font-size:13px;margin-top:2px}
ul.files{margin:8px 0 0;padding:0;list-style:none;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#444}
.step{margin:0 0 12px}
.step .cmt{font-size:13px;color:#57606a;margin-bottom:4px}
pre{margin:0;background:#0d1117;color:#e6edf3;border-radius:6px;padding:10px 12px;
  white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.remediate{border-left:4px solid #d4a72c;background:#fffbef;padding:12px 16px;
  border-radius:4px}
.note{font-size:12px;color:#9a6700;margin-bottom:10px}
.ok-msg{color:#1a7f37;font-weight:600;margin:0}
.foot{padding:12px 24px;font-size:11px;color:#8c959f}
"""


def _split_warning(warning: str) -> tuple:
    """Split a violation string into (headline, [listing line, ...])."""
    parts = warning.split("\n")
    return parts[0], [p for p in parts[1:] if p.strip()]


def build_details_html(
    profile: str,
    current_snapshot: dict,
    prev_snapshot: dict | None,
    all_warnings: list,
    diff_stats: dict | None,
) -> str:
    """Render the full report as a self-contained HTML document for email."""
    status_ok = not all_warnings
    n = len(all_warnings)
    badge = ('<span class="badge ok">OK</span>' if status_ok
             else '<span class="badge warn">WARNING</span>')
    subtitle = ("All guardrails passed" if status_ok
                else f"{n} violation{'s' if n != 1 else ''} detected")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    h: list = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<style>{_EMAIL_CSS}</style></head><body>",
        '<div class="wrap">',
        '<div class="head">',
        f"<h1>Backup Guardrail Report {badge}</h1>",
        f'<div class="sub">{_esc(profile)} — {_esc(subtitle)}</div>',
        "</div>",
    ]

    def row(k: str, v: str) -> str:
        return f'<tr><td class="k">{_esc(k)}</td><td>{_esc(v)}</td></tr>'

    h.append('<div class="sec"><h2>Snapshot</h2><table class="meta">')
    h.append(row("Profile", profile))
    h.append(row("Time", ts))
    h.append(row("Current", f"{current_snapshot['id'][:8]}  ({current_snapshot['time']})"))
    if prev_snapshot:
        h.append(row("Previous", f"{prev_snapshot['id'][:8]}  ({prev_snapshot['time']})"))
    else:
        h.append(row("Previous", "(none — first backup)"))
    h.append("</table></div>")

    if all_warnings:
        h.append(f'<div class="sec"><h2>Violations ({n})</h2>')
        for w in all_warnings:
            headline, listing = _split_warning(w)
            code, _, rest = headline.partition(":")
            h.append('<div class="viol">')
            h.append(f'<div class="code">{_esc(code.strip())}</div>')
            if rest.strip():
                h.append(f'<div class="msg">{_esc(rest.strip())}</div>')
            if listing:
                h.append('<ul class="files">')
                for line in listing:
                    h.append(f"<li>{_esc(line.strip())}</li>")
                h.append("</ul>")
            h.append("</div>")
        h.append("</div>")
    else:
        h.append('<div class="sec"><p class="ok-msg">✓ All guardrails passed.</p></div>')

    if all_warnings:
        guide = build_action_guide(
            current_snapshot["id"][:8],
            prev_snapshot["id"][:8] if prev_snapshot else None,
            all_warnings,
            diff_stats,
        )
        for section in guide:
            remediate = section["title"].lower().startswith("remediate")
            h.append('<div class="sec">')
            h.append(f'<h2>{_esc(section["title"])}</h2>')
            h.append('<div class="remediate">' if remediate else "<div>")
            if section.get("note"):
                h.append(f'<div class="note">{_esc(section["note"])}</div>')
            for comment, cmds in section["steps"]:
                h.append('<div class="step">')
                h.append(f'<div class="cmt">{_esc(comment)}</div>')
                if cmds:
                    h.append(f"<pre><code>{_esc(chr(10).join(cmds))}</code></pre>")
                h.append("</div>")
            h.append("</div></div>")

    h.append(f'<div class="foot">Generated by guardrails.py at {ts}</div>')
    h.append("</div></body></html>")
    return "\n".join(h)


# ── Notification dispatch ─────────────────────────────────────────────────────

# Placeholders that materialize a context value into a temp file and substitute
# the file path. Lets large/HTML bodies be redirected into a sender's stdin
# instead of being inlined (and quote-mangled) in the shell command string.
_FILE_PLACEHOLDERS = {
    "details_file":      ("details",      ".txt"),
    "details_html_file": ("details_html", ".html"),
}


def run_notify_commands(templates: list, context: dict) -> None:
    """
    Run each notification command template, substituting {placeholder} values.
    Failures are printed to stderr and silently ignored.

    Available placeholders: {title} {message} {details} {details_html}
    {details_file} {details_html_file} {profile} {status} {violations}

    The *_file placeholders write the corresponding content to a temp file and
    substitute its path (shell-safe), so callers can redirect it into the
    sender's stdin, e.g. "apprise -i html ... < {details_html_file}".
    """
    for template in templates:
        ctx = context
        tmp_paths: list = []
        try:
            for ph, (src_key, suffix) in _FILE_PLACEHOLDERS.items():
                if "{" + ph + "}" not in template:
                    continue
                fd, path = tempfile.mkstemp(prefix="guardrails-", suffix=suffix)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(context.get(src_key, ""))
                if ctx is context:
                    ctx = dict(context)
                ctx[ph] = path
                tmp_paths.append(path)
            cmd = template.format_map(ctx)
            subprocess.run(cmd, shell=True, check=False, timeout=30, capture_output=True)
        except KeyError as e:
            print(f"Warning: unknown placeholder {e} in notify template: {template!r}",
                  file=sys.stderr)
        except Exception as e:
            print(f"Warning: notification command failed: {e}", file=sys.stderr)
        finally:
            for path in tmp_paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ── Log management ────────────────────────────────────────────────────────────

_LOG_ENTRY_PATTERN = re.compile(
    r"(?=^### \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ###\n)", re.MULTILINE
)


def write_log(path: str, content: str, keep_runs: int = 100) -> None:
    """
    Append a timestamped entry to a log file, keeping at most keep_runs entries.
    Each entry is prefixed with a ### DATETIME ### marker for splitting.
    No-op when path is empty.
    """
    if not path:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"### {ts} ###\n{content.rstrip()}\n"

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""

        runs = [r for r in _LOG_ENTRY_PATTERN.split(text) if r.strip()]
        runs.append(entry)

        if keep_runs > 0 and len(runs) > keep_runs:
            runs = runs[-keep_runs:]

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(runs))
    except OSError as e:
        print(f"Warning: could not write to log {path!r}: {e}", file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post-backup guardrails. Run as a resticprofile run-after hook.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--max-growth-ratio", type=float, default=1.2, metavar="RATIO",
        help="Max size growth ratio vs previous snapshot",
    )
    parser.add_argument(
        "--max-file-count-growth-ratio", type=float, default=1.2, metavar="RATIO",
        help="Max file count growth ratio vs previous snapshot",
    )
    parser.add_argument(
        "--max-new-files", type=int, default=100, metavar="N",
        help="Max number of new files added per backup run",
    )
    parser.add_argument(
        "--max-added-size", type=_argparse_size,
        default=_parse_size_to_bytes("10MB"), metavar="SIZE",
        help="Max net data added per run (file modifications excluded), e.g. 10MB, 1GiB",
    )
    parser.add_argument(
        "--max-removed-size", type=_argparse_size,
        default=_parse_size_to_bytes("50MB"), metavar="SIZE",
        help="Max net data removed per run (file modifications excluded), e.g. 50MB, 1GiB",
    )
    parser.add_argument(
        "--max-removed-files", type=int, default=100, metavar="N",
        help="Max number of files net-removed per backup run (moves within scope excluded)",
    )
    parser.add_argument(
        "--max-total-size", type=_argparse_size,
        default=_parse_size_to_bytes("5GB"), metavar="SIZE",
        help="Max absolute total snapshot size, e.g. 5GB, 500MiB",
    )
    parser.add_argument(
        "--no-warn-on-new-extensions", action="store_true",
        help="Disable warnings for new file extensions",
    )
    parser.add_argument(
        "--notify-short", action="append", default=[], metavar="CMD_TEMPLATE",
        dest="notify_short",
        help=(
            "Command template for short (desktop) notifications on violation. "
            "Repeatable. Placeholders: {title} {message} {profile} {status} {violations}. "
            "Example: \"apprise -t '{title}' -b '{message}' macosx://\""
        ),
    )
    parser.add_argument(
        "--notify-long", action="append", default=[], metavar="CMD_TEMPLATE",
        dest="notify_long",
        help=(
            "Command template for detailed (email) notifications on violation. "
            "Repeatable. Placeholders: {title} {details} {details_html} "
            "{details_file} {details_html_file} {profile} {status} {violations}. "
            "Prefer redirecting a *_file placeholder into stdin for HTML bodies, "
            "e.g. \"apprise -i html -t '{title}' 'mailto://user:pass@gmail.com' < {details_html_file}\""
        ),
    )
    parser.add_argument(
        "--log", default="", metavar="PATH",
        help="Append full guardrail report to this file on every run.",
    )
    parser.add_argument(
        "--log-keep-runs", type=int, default=100, metavar="N",
        help="Max number of runs to keep in the log file (oldest are dropped)",
    )
    parser.add_argument(
        "--resticprofile-config", default="", metavar="PATH",
        help="Path to resticprofile config file (uses resticprofile default if omitted)",
    )
    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _PROFILE_NAME, _RESTICPROFILE_CONFIG

    args = parse_args()

    _PROFILE_NAME = os.environ.get("PROFILE_NAME", "")
    if not _PROFILE_NAME:
        print("ERROR: PROFILE_NAME environment variable not set.", file=sys.stderr)
        print("  Run this script as a resticprofile run-after hook.", file=sys.stderr)
        sys.exit(1)

    _RESTICPROFILE_CONFIG = args.resticprofile_config

    config = {
        "max_growth_ratio":            args.max_growth_ratio,
        "max_file_count_growth_ratio": args.max_file_count_growth_ratio,
        "max_new_files":               args.max_new_files,
        "max_added_size":              args.max_added_size,
        "max_removed_size":            args.max_removed_size,
        "max_removed_files":           args.max_removed_files,
        "max_total_size":              args.max_total_size,
        "warn_on_new_extensions":      not args.no_warn_on_new_extensions,
    }

    print("=" * 70)
    print("Running post-backup guardrails...")
    print("=" * 70)

    snapshots = get_snapshots(limit=2)
    if len(snapshots) < 1:
        print("ERROR: No snapshots found in repository", file=sys.stderr)
        sys.exit(1)

    current_snapshot = snapshots[-1]
    prev_snapshot = snapshots[-2] if len(snapshots) >= 2 else None

    print(f"\nCurrent snapshot:  {current_snapshot['id'][:8]} ({current_snapshot['time']})")
    if prev_snapshot:
        print(f"Previous snapshot: {prev_snapshot['id'][:8]} ({prev_snapshot['time']})")
    else:
        print("No previous snapshot to compare")

    curr_stats = get_stats(current_snapshot["id"])
    curr_files = get_files(current_snapshot["id"])
    curr_exts  = extract_extensions(curr_files)

    prev_stats = get_stats(prev_snapshot["id"]) if prev_snapshot else {}
    prev_files = get_files(prev_snapshot["id"]) if prev_snapshot else []
    prev_exts  = extract_extensions(prev_files)

    all_warnings = []
    diff_stats: dict | None = None

    print(f"\nSnapshot sizes:")
    print(f"  Current:  {curr_stats.get('total_size', 0) / (1<<30):.3f} GB  "
          f"({curr_stats.get('total_file_count', 0):,} files)")
    if prev_stats:
        print(f"  Previous: {prev_stats.get('total_size', 0) / (1<<30):.3f} GB  "
              f"({prev_stats.get('total_file_count', 0):,} files)")

    # Compute the diff up front so every check can report the files involved.
    if prev_snapshot:
        diff_stats = get_diff(prev_snapshot["id"], current_snapshot["id"])
        net_mib = (diff_stats["added_bytes"] - diff_stats["removed_bytes"]) / (1 << 20)
        blobs = diff_stats["data_blobs_new"]
        blob_note = f"{blobs} data blob(s)" if blobs else "metadata only"
        print(f"  Added this run: {diff_stats['added_bytes'] / (1<<20):.2f} MiB added, "
              f"{diff_stats['removed_bytes'] / (1<<20):.2f} MiB removed "
              f"(net {net_mib:+.2f} MiB, {diff_stats['new_files']:,} new / "
              f"{diff_stats['removed_files']:,} removed files, {blob_note})")

    all_warnings.extend(check_size_growth(config, curr_stats, prev_stats, diff_stats))
    all_warnings.extend(check_file_count_growth(config, curr_files, prev_files, diff_stats))
    all_warnings.extend(check_new_extensions(config, curr_exts, prev_exts))
    all_warnings.extend(check_total_size(config, curr_stats, diff_stats))

    if prev_snapshot:
        all_warnings.extend(check_new_files_absolute(config, diff_stats))
        all_warnings.extend(check_added_size(config, diff_stats))
        all_warnings.extend(check_removed_size(config, diff_stats))
        all_warnings.extend(check_removed_files(config, diff_stats))

    # Build notification context (used for log and notify commands)
    status = "WARNING" if all_warnings else "OK"
    if all_warnings:
        title = f"Backup WARNING: {len(all_warnings)} violation(s) in '{_PROFILE_NAME}'"
    else:
        title = f"Backup OK: '{_PROFILE_NAME}' all guardrails passed"

    message = build_short_message(_PROFILE_NAME, all_warnings, diff_stats)
    details = build_details(
        _PROFILE_NAME, current_snapshot, prev_snapshot, all_warnings, diff_stats
    )
    details_html = build_details_html(
        _PROFILE_NAME, current_snapshot, prev_snapshot, all_warnings, diff_stats
    )
    context = {
        "title":        title,
        "message":      message,
        "details":      details,
        "details_html": details_html,
        "profile":      _PROFILE_NAME,
        "status":       status,
        "violations":   str(len(all_warnings)),
    }

    write_log(args.log, details, args.log_keep_runs)

    if all_warnings:
        print("\n" + "=" * 70)
        print("GUARDRAIL VIOLATIONS DETECTED")
        print("=" * 70)
        for w in all_warnings:
            print(f"\n{w}")
        print()
        run_notify_commands(args.notify_short, context)
        run_notify_commands(args.notify_long, context)
        sys.exit(1)
    else:
        print("\n✓ All guardrails passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
