#!/usr/bin/env python3
"""Build a reviewable duplicate-deletion plan from a live inventory snapshot.

Reads the JSONL produced by collect_inventory.py, groups torrents by infohash,
chooses one keeper per duplicated hash (complete copy on the host with the most
free space), and writes a plan CSV. Nothing is changed on any qBittorrent host.

Usage:
    python plan_dedup.py
    python plan_dedup.py --inventory dedup-inventory-20260101T000000Z.jsonl
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import posixpath
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_PLAN = "dedup-plan.csv"
DEFAULT_SUMMARY = "dedup-plan-summary.txt"
COMPLETE_THRESHOLD = 0.9999
ACTIVE_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP"}

PLAN_FIELDS = [
    "hash",
    "host",
    "action",
    "approved",
    "reason",
    "keeper_host",
    "name",
    "size",
    "progress",
    "state",
    "ratio",
    "seeding_time",
    "private",
    "tracker_domain",
    "tracker_mismatch",
    "save_path",
    "content_path",
    "free_space",
    "delete_files",
    "expected_freed_bytes",
    "category",
    "tags",
]


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_path(value) -> str:
    path = str(value or "").replace("\\", "/")
    if not path:
        return ""
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return ""
    return normalized


def path_conflicts(
    donor_paths: Sequence[str],
    retained_set: set[str],
    retained_sorted: Sequence[str],
) -> bool:
    """True if a donor path overlaps a torrent that stays on the same host."""
    for raw in donor_paths:
        path = normalize_path(raw)
        if not path or path == "/":
            continue

        current = path
        while current and current != "/":
            if current in retained_set:
                return True
            parent = posixpath.dirname(current)
            if parent == current:
                break
            current = parent

        prefix = path.rstrip("/") + "/"
        index = bisect.bisect_left(retained_sorted, prefix)
        if index < len(retained_sorted) and retained_sorted[index].startswith(prefix):
            return True

    return False


def is_complete(torrent: dict) -> bool:
    if as_float(torrent.get("progress")) < COMPLETE_THRESHOLD:
        return False
    if as_int(torrent.get("amount_left")) != 0:
        return False
    if not torrent.get("has_metadata", True):
        return False
    if str(torrent.get("state") or "") in {"error", "missingFiles"}:
        return False
    return True


def keeper_key(torrent: dict, host_info: dict) -> tuple:
    free = host_info.get(torrent["host"], {}).get("free_space")
    return (
        0 if free is not None else 1,
        -(free if free is not None else 0),
        0 if str(torrent.get("state")) in ACTIVE_STATES else 1,
        -max(as_float(torrent.get("ratio")), 0.0),
        -as_int(torrent.get("seeding_time")),
        torrent["host"],
    )


def has_tracker_mismatch(copies: Sequence[dict]) -> bool:
    fingerprints = {
        copy.get("tracker_fingerprint")
        for copy in copies
        if copy.get("tracker_fingerprint")
    }
    return len(fingerprints) > 1


def load_inventory(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        raise FileNotFoundError(f"inventory file not found: {path}")

    torrents: list[dict] = []
    meta: dict | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if item.get("type") == "meta":
                meta = item
            elif item.get("host") and item.get("hash"):
                torrents.append(item)

    if meta is None:
        raise ValueError(f"{path}: no metadata line found")
    if not torrents:
        raise ValueError(f"{path}: no torrent records found")
    return torrents, meta


def latest_inventory(directory: Path) -> Path:
    candidates = [
        path for path in directory.glob("dedup-inventory-*.jsonl") if path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no dedup-inventory-*.jsonl files in {directory}; run collect_inventory.py first"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def host_information(torrents: Sequence[dict], meta: dict) -> dict[str, dict]:
    info: dict[str, dict] = {}
    for host in meta.get("hosts", []):
        name = host.get("host")
        if name:
            info[name] = dict(host)
    for torrent in torrents:
        info.setdefault(torrent["host"], {"host": torrent["host"]})
    return info


def plan_row(
    torrent: dict,
    *,
    action: str,
    approved: str,
    reason: str,
    keeper_host: str,
    delete_files: bool,
    tracker_mismatch: bool,
    free_space: int | None,
) -> dict:
    expected = as_int(torrent.get("size")) if action == "delete" and delete_files else 0
    return {
        "hash": torrent["hash"],
        "host": torrent["host"],
        "action": action,
        "approved": approved,
        "reason": reason,
        "keeper_host": keeper_host,
        "name": torrent.get("name") or "",
        "size": as_int(torrent.get("size")),
        "progress": f"{as_float(torrent.get('progress')):.6f}",
        "state": torrent.get("state") or "",
        "ratio": f"{as_float(torrent.get('ratio')):.6f}",
        "seeding_time": as_int(torrent.get("seeding_time")),
        "private": "yes" if torrent.get("private") else "no",
        "tracker_domain": torrent.get("tracker_domain") or "",
        "tracker_mismatch": "yes" if tracker_mismatch else "no",
        "save_path": torrent.get("save_path") or "",
        "content_path": torrent.get("content_path") or "",
        "free_space": free_space if free_space is not None else "",
        "delete_files": "true" if delete_files else "false",
        "expected_freed_bytes": expected,
        "category": torrent.get("category") or "",
        "tags": torrent.get("tags") or "",
    }


def build_plan(
    torrents: Sequence[dict],
    meta: dict,
    *,
    entry_only: bool = False,
) -> tuple[list[dict], dict]:
    host_info = host_information(torrents, meta)

    groups: dict[str, list[dict]] = defaultdict(list)
    duplicate_copies_on_same_host = 0
    for torrent in torrents:
        copies = groups[torrent["hash"]]
        if any(existing["host"] == torrent["host"] for existing in copies):
            duplicate_copies_on_same_host += 1
            continue
        copies.append(torrent)

    decisions: dict[str, dict] = {}
    single_copy_hashes = 0
    for infohash, copies in groups.items():
        if len(copies) < 2:
            single_copy_hashes += 1
            continue
        complete = [copy for copy in copies if is_complete(copy)]
        if not complete:
            decisions[infohash] = {
                "kind": "skip",
                "copies": copies,
                "reason": "no complete copy with files",
            }
            continue
        keeper = min(complete, key=lambda copy: keeper_key(copy, host_info))
        donors = [copy for copy in copies if copy["host"] != keeper["host"]]
        decisions[infohash] = {
            "kind": "dedupe",
            "copies": copies,
            "keeper": keeper,
            "donors": donors,
        }

    donor_hashes_by_host: dict[str, set[str]] = defaultdict(set)
    for infohash, decision in decisions.items():
        if decision["kind"] == "dedupe":
            for donor in decision["donors"]:
                donor_hashes_by_host[donor["host"]].add(infohash)

    retained_paths: dict[str, list[str]] = defaultdict(list)
    for torrent in torrents:
        if torrent["hash"] in donor_hashes_by_host.get(torrent["host"], set()):
            continue
        path = normalize_path(torrent.get("content_path"))
        if path:
            retained_paths[torrent["host"]].append(path)

    retained_sets = {host: set(paths) for host, paths in retained_paths.items()}
    retained_sorted = {host: sorted(paths) for host, paths in retained_paths.items()}

    rows: list[dict] = []
    per_host: dict[str, dict] = defaultdict(
        lambda: {
            "keepers": 0,
            "deletions": 0,
            "file_deletions": 0,
            "entry_only": 0,
            "bytes_to_free": 0,
            "bytes_orphaned": 0,
        }
    )
    no_complete_groups = 0
    path_overlap_rows = 0
    tracker_mismatch_groups = 0
    approved_deletions = 0
    vetoed_deletions = 0
    delete_rows = 0
    bytes_orphaned = 0
    skipped_rows = 0

    for infohash in sorted(decisions):
        decision = decisions[infohash]
        copies = decision["copies"]
        free_space = {
            copy["host"]: host_info.get(copy["host"], {}).get("free_space")
            for copy in copies
        }

        if decision["kind"] == "skip":
            no_complete_groups += 1
            for torrent in sorted(copies, key=lambda copy: copy["host"]):
                rows.append(
                    plan_row(
                        torrent,
                        action="skip",
                        approved="no",
                        reason=decision["reason"],
                        keeper_host="",
                        delete_files=False,
                        tracker_mismatch=False,
                        free_space=free_space.get(torrent["host"]),
                    )
                )
                skipped_rows += 1
            continue

        keeper = decision["keeper"]
        donors = decision["donors"]
        mismatch = has_tracker_mismatch(copies)
        if mismatch:
            tracker_mismatch_groups += 1

        rows.append(
            plan_row(
                keeper,
                action="keep",
                approved="yes",
                reason=f"keeper ({len(copies)} copies)",
                keeper_host=keeper["host"],
                delete_files=False,
                tracker_mismatch=mismatch,
                free_space=free_space.get(keeper["host"]),
            )
        )
        per_host[keeper["host"]]["keepers"] += 1

        for donor in sorted(donors, key=lambda copy: copy["host"]):
            reasons = [f"duplicate ({len(copies)} copies)"]
            approved = "no" if mismatch else "yes"
            if mismatch:
                reasons.append("tracker-mismatch")
            delete_files = not entry_only
            if entry_only:
                reasons.append("entry-only")

            conflict = delete_files and path_conflicts(
                [donor.get("content_path")],
                retained_sets.get(donor["host"], set()),
                retained_sorted.get(donor["host"], []),
            )
            if conflict:
                delete_files = False
                path_overlap_rows += 1
                reasons.append("path-overlap")

            row = plan_row(
                donor,
                action="delete",
                approved=approved,
                reason="; ".join(reasons),
                keeper_host=keeper["host"],
                delete_files=delete_files,
                tracker_mismatch=mismatch,
                free_space=free_space.get(donor["host"]),
            )
            rows.append(row)

            host_stats = per_host[donor["host"]]
            host_stats["deletions"] += 1
            delete_rows += 1
            if approved == "yes":
                approved_deletions += 1
            else:
                vetoed_deletions += 1
            if delete_files:
                host_stats["file_deletions"] += 1
                host_stats["bytes_to_free"] += as_int(donor.get("size"))
            else:
                host_stats["entry_only"] += 1
                if approved == "yes":
                    host_stats["bytes_orphaned"] += as_int(donor.get("size"))
                    bytes_orphaned += as_int(donor.get("size"))

    stats = {
        "host_info": host_info,
        "groups": len(groups),
        "duplicate_groups": sum(
            1 for decision in decisions.values() if decision["kind"] == "dedupe"
        ),
        "single_copy_hashes": single_copy_hashes,
        "duplicate_copies_on_same_host": duplicate_copies_on_same_host,
        "no_complete_groups": no_complete_groups,
        "tracker_mismatch_groups": tracker_mismatch_groups,
        "path_overlap_rows": path_overlap_rows,
        "delete_rows": delete_rows,
        "approved_deletions": approved_deletions,
        "vetoed_deletions": vetoed_deletions,
        "bytes_orphaned": bytes_orphaned,
        "skipped_rows": skipped_rows,
        "per_host": per_host,
        "bytes_to_free": sum(host["bytes_to_free"] for host in per_host.values()),
    }
    return rows, stats


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def summary_lines(
    *,
    inventory_path: Path,
    meta: dict,
    plan_path: Path,
    stats: dict,
    rows: Sequence[dict],
) -> list[str]:
    host_info = stats["host_info"]
    reachable = [host for host in host_info.values() if not host.get("error")]
    unreachable = [host for host in host_info.values() if host.get("error")]

    lines = [
        f"Inventory: {inventory_path}",
        f"Generated: {meta.get('generated_at', '?')}",
        f"Finished:  {meta.get('finished_at', '?')}",
        f"Hosts: {len(reachable)}/{len(host_info)} reachable",
    ]
    if meta.get("status") != "finished":
        lines.append(
            "WARNING: inventory did not finish cleanly; treat the plan as partial"
        )
    lines.extend(
        [
            "",
            f"Distinct hashes: {stats['groups']:,}",
            f"Single-copy hashes: {stats['single_copy_hashes']:,}",
            f"Duplicate hashes: {stats['duplicate_groups']:,}",
            f"Deletion rows: {stats['delete_rows']:,} "
            f"({stats['approved_deletions']:,} approved, "
            f"{stats['vetoed_deletions']:,} vetoed, "
            f"{stats['skipped_rows']:,} skipped rows)",
            f"  with file deletion: "
            f"{sum(host['file_deletions'] for host in stats['per_host'].values()):,}",
            f"  entry-only: "
            f"{sum(host['entry_only'] for host in stats['per_host'].values()):,}",
            f"Orphaned by approved entry-only deletions: "
            f"{human_bytes(stats['bytes_orphaned'])}",
            f"Expected space freed immediately: {human_bytes(stats['bytes_to_free'])}",
            f"Path-overlap downgrades: {stats['path_overlap_rows']:,}",
            f"Tracker-mismatch groups: {stats['tracker_mismatch_groups']:,} "
            f"(rows set to approved=no)",
            f"No-complete-copy groups: {stats['no_complete_groups']:,} (skipped)",
            "",
            "Per host:",
        ]
    )

    for host, info in sorted(host_info.items()):
        host_stats = stats["per_host"].get(
            host,
            {
                "keepers": 0,
                "deletions": 0,
                "file_deletions": 0,
                "entry_only": 0,
                "bytes_to_free": 0,
                "bytes_orphaned": 0,
            },
        )
        free = info.get("free_space")
        free_text = human_bytes(free) if free is not None else "?"
        state = info.get("error") or info.get("version") or "?"
        lines.append(
            f"  {host}  free={free_text}  keepers={host_stats['keepers']}  "
            f"delete={host_stats['deletions']}  files={host_stats['file_deletions']}  "
            f"entry-only={host_stats['entry_only']}  "
            f"orphaned={human_bytes(host_stats['bytes_orphaned'])}  "
            f"freeing={human_bytes(host_stats['bytes_to_free'])}  [{state}]"
        )

    if unreachable:
        lines.append("")
        lines.append("Unreachable hosts (not used for keepers or deletions):")
        for host in unreachable:
            lines.append(f"  {host['host']}: {host.get('error')}")

    lines.extend(
        [
            "",
            f"Plan: {plan_path}",
            f"Confirmation count: {stats['approved_deletions']}",
        ]
    )
    if stats["bytes_orphaned"]:
        lines.extend(
            [
                "",
                "Files are intentionally left on disk (entry-only deletions).",
                "After applying, run qui's Orphan Scan in report-only mode, review it,",
                "then clean. Make sure both completed and incomplete paths are scanned.",
            ]
        )
    lines.extend(
        [
            "",
            "Review the CSV, set approved=no on rows to veto, then apply with:",
            f"  python apply_dedup.py --plan {plan_path} "
            f"--confirm-count {stats['approved_deletions']} --apply",
        ]
    )
    return lines


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a duplicate-deletion plan from a qBittorrent inventory.",
    )
    parser.add_argument(
        "--inventory",
        default=None,
        help="inventory JSONL (default: most recent dedup-inventory-*.jsonl)",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_PLAN,
        help=f"plan CSV (default: {DEFAULT_PLAN})",
    )
    parser.add_argument(
        "--summary",
        default=DEFAULT_SUMMARY,
        help=f"summary text file (default: {DEFAULT_SUMMARY})",
    )
    parser.add_argument(
        "--entry-only",
        action="store_true",
        help="mark every deletion as entry-only (keep files)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        inventory_path = (
            Path(args.inventory) if args.inventory else latest_inventory(Path.cwd())
        )
        torrents, meta = load_inventory(inventory_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows, stats = build_plan(torrents, meta, entry_only=args.entry_only)
    plan_path = Path(args.out)
    summary_path = Path(args.summary)

    with plan_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    lines = summary_lines(
        inventory_path=inventory_path,
        meta=meta,
        plan_path=plan_path,
        stats=stats,
        rows=rows,
    )
    with summary_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n".join(lines))
    print()
    print(f"Wrote {plan_path} ({len(rows):,} rows)")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
