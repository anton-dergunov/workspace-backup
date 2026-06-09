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
    --notify-long  "apprise -t '{title}' -b '{details}' mailto://..."

Template placeholders for --notify-short / --notify-long:
  {title}      — one-line notification title
  {message}    — compact multi-line summary (suitable for desktop)
  {details}    — full report with file listings (suitable for email)
  {profile}    — resticprofile profile name
  {status}     — "WARNING" or "OK"
  {violations} — number of violations as a string
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

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


# ── Guardrail checks ──────────────────────────────────────────────────────────

def check_size_growth(config: dict, curr_stats: dict, prev_stats: dict) -> list:
    """Warn if snapshot grew more than max_growth_ratio vs previous."""
    warnings = []
    if not prev_stats or "total_size" not in prev_stats:
        return warnings
    prev_size = prev_stats.get("total_size", 0)
    curr_size = curr_stats.get("total_size", 0)
    if prev_size > 0:
        ratio = curr_size / prev_size
        if ratio > config["max_growth_ratio"]:
            warnings.append(
                f"SIZE_GROWTH: snapshot grew {ratio:.2f}x "
                f"({prev_size / (1<<30):.2f} GB → {curr_size / (1<<30):.2f} GB), "
                f"threshold is {config['max_growth_ratio']:.2f}x"
            )
    return warnings


def check_file_count_growth(config: dict, curr_files: list, prev_files: list) -> list:
    """Warn if file count grew more than max_file_count_growth_ratio vs previous."""
    warnings = []
    prev_count = len(prev_files) if prev_files else 0
    curr_count = len(curr_files)
    if prev_count > 0:
        ratio = curr_count / prev_count
        if ratio > config["max_file_count_growth_ratio"]:
            warnings.append(
                f"FILE_COUNT_GROWTH: count jumped {ratio:.2f}x "
                f"({prev_count:,} → {curr_count:,}), "
                f"threshold is {config['max_file_count_growth_ratio']:.2f}x"
            )
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
        new_paths = diff_stats.get("new_file_paths", [])
        for p in new_paths[:NEW_FILES_LISTED]:
            msg += f"\n  + {p}"
        if len(new_paths) > NEW_FILES_LISTED:
            msg += f"\n  ... and {len(new_paths) - NEW_FILES_LISTED} more"
        warnings.append(msg)
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
        warnings.append(
            f"ADDED_SIZE: {net_bytes / (1<<20):.1f} MiB net new data in this run "
            f"(added {diff_stats['added_bytes'] / (1<<20):.1f} MiB, "
            f"removed {diff_stats['removed_bytes'] / (1<<20):.1f} MiB), "
            f"threshold is {max_bytes / (1<<20):.1f} MiB"
        )
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
        warnings.append(
            f"REMOVED_SIZE: {net_removed / (1<<20):.1f} MiB net data removed in this run "
            f"(added {diff_stats['added_bytes'] / (1<<20):.1f} MiB, "
            f"removed {diff_stats['removed_bytes'] / (1<<20):.1f} MiB), "
            f"threshold is {max_bytes / (1<<20):.1f} MiB"
        )
    return warnings


def check_removed_files(config: dict, diff_stats: dict) -> list:
    """Warn if too many files were net-removed in this backup run.

    Uses removed−new so that moves within the backup scope (equal removed and
    new counts) don't trigger false positives.
    """
    warnings = []
    net_removed = max(0, diff_stats.get("removed_files", 0) - diff_stats.get("new_files", 0))
    if net_removed > config["max_removed_files"]:
        warnings.append(
            f"REMOVED_FILES: {net_removed:,} files net removed in this run "
            f"(threshold is {config['max_removed_files']:,})"
        )
    return warnings


def check_total_size(config: dict, curr_stats: dict) -> list:
    """Warn if total snapshot size exceeds absolute limit."""
    warnings = []
    total = curr_stats.get("total_size", 0)
    max_bytes = config["max_total_size"]
    if total > max_bytes:
        warnings.append(
            f"TOTAL_SIZE: snapshot is {total / (1<<30):.2f} GB, "
            f"threshold is {max_bytes / (1<<30):.2f} GB"
        )
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

    return "\n".join(lines)


# ── Notification dispatch ─────────────────────────────────────────────────────

def run_notify_commands(templates: list, context: dict) -> None:
    """
    Run each notification command template, substituting {placeholder} values.
    Failures are printed to stderr and silently ignored.

    Available placeholders: {title} {message} {details} {profile} {status} {violations}
    """
    for template in templates:
        try:
            cmd = template.format_map(context)
            subprocess.run(cmd, shell=True, check=False, timeout=30, capture_output=True)
        except KeyError as e:
            print(f"Warning: unknown placeholder {e} in notify template: {template!r}",
                  file=sys.stderr)
        except Exception as e:
            print(f"Warning: notification command failed: {e}", file=sys.stderr)


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
            "Repeatable. Placeholders: {title} {details} {profile} {status} {violations}. "
            "Example: \"apprise -t '{title}' -b '{details}' 'mailto://user:pass@gmail.com'\""
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

    all_warnings.extend(check_size_growth(config, curr_stats, prev_stats))
    all_warnings.extend(check_file_count_growth(config, curr_files, prev_files))
    all_warnings.extend(check_new_extensions(config, curr_exts, prev_exts))
    all_warnings.extend(check_total_size(config, curr_stats))

    if prev_snapshot:
        diff_stats = get_diff(prev_snapshot["id"], current_snapshot["id"])
        net_mib = (diff_stats["added_bytes"] - diff_stats["removed_bytes"]) / (1 << 20)
        blobs = diff_stats["data_blobs_new"]
        blob_note = f"{blobs} data blob(s)" if blobs else "metadata only"
        print(f"  Added this run: {diff_stats['added_bytes'] / (1<<20):.2f} MiB added, "
              f"{diff_stats['removed_bytes'] / (1<<20):.2f} MiB removed "
              f"(net {net_mib:+.2f} MiB, {diff_stats['new_files']:,} new / "
              f"{diff_stats['removed_files']:,} removed files, {blob_note})")
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
    context = {
        "title":      title,
        "message":    message,
        "details":    details,
        "profile":    _PROFILE_NAME,
        "status":     status,
        "violations": str(len(all_warnings)),
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
