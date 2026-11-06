#!/usr/bin/env python3
"""Apply an entry-only duplicate-deletion plan.

Deletes only torrent entries (deleteFiles=false), so downloaded files stay on
disk for qui's Orphan Scan to clean up later. Every deletion is backed up as a
.torrent file first, and each hash is re-validated against live state before it
is touched.

Without --apply this is a dry run and no host is contacted.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from plan_dedup import is_complete
from qbt_api import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    HostConfig,
    QBittorrentClient,
    load_hosts,
)

DEFAULT_PLAN = "dedup-plan.csv"
DEFAULT_HOSTS = "qbittorrent-hosts.csv"
DEFAULT_BATCH_SIZE = 50

LOG_FIELDS = [
    "timestamp",
    "host",
    "hash",
    "keeper_host",
    "status",
    "backup_file",
    "error",
]


def load_plan(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"plan file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"plan file has no header row: {path}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"plan file has no rows: {path}")
    return rows


def validate_plan(rows: Sequence[dict]) -> tuple[list[dict], dict]:
    keeps: dict[str, dict] = {}
    for row in rows:
        if row.get("action") != "keep":
            continue
        if row["hash"] in keeps:
            raise ValueError(f"multiple keep rows for {row['hash']}")
        keeps[row["hash"]] = row

    deletes: list[dict] = []
    for row in rows:
        if row.get("action") != "delete":
            continue
        if row.get("approved", "").strip().lower() not in {"yes", "y", "true", "1"}:
            continue
        if row.get("delete_files", "").strip().lower() == "true":
            raise ValueError(
                "plan contains delete_files=true; this pipeline is entry-only"
            )
        keeper = keeps.get(row["hash"])
        if keeper is None:
            raise ValueError(f"no keep row for {row['hash']}")
        if row.get("keeper_host") != keeper["host"]:
            raise ValueError(f"keeper mismatch for {row['hash']}")
        if row["host"] == keeper["host"]:
            raise ValueError(f"donor and keeper are the same host for {row['hash']}")
        deletes.append(row)

    if not deletes:
        raise ValueError("plan has no approved deletions")

    keys = [(row["hash"], row["host"]) for row in deletes]
    if len(keys) != len(set(keys)):
        raise ValueError("plan contains duplicate delete rows")

    summary = {
        "keeps": len(keeps),
        "deletes": len(deletes),
        "hosts": len({row["host"] for row in deletes}),
        "bytes": sum(int(row.get("size") or 0) for row in deletes),
        "vetoed": sum(
            1
            for row in rows
            if row.get("action") == "delete"
            and row.get("approved", "").strip().lower() not in {"yes", "y", "true", "1"}
        ),
        "skipped": sum(1 for row in rows if row.get("action") == "skip"),
    }
    return deletes, summary


def safe_host_name(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", host)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def chunks(items: Sequence[dict], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


class Applier:
    def __init__(
        self,
        *,
        configs: dict[str, HostConfig],
        backup_dir: Path,
        timeout: float,
        verify: bool,
        retries: int,
    ) -> None:
        self.configs = configs
        self.backup_dir = backup_dir
        self.timeout = timeout
        self.verify = verify
        self.retries = retries
        self.clients: dict[str, QBittorrentClient] = {}
        self.unavailable: dict[str, str] = {}
        self.log_path = backup_dir.parent / (
            f"dedup-apply-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log.csv"
        )
        self.log_file = self.log_path.open("w", encoding="utf-8", newline="")
        self.log_writer = csv.DictWriter(self.log_file, fieldnames=LOG_FIELDS)
        self.log_writer.writeheader()
        self.manifest_path = backup_dir / "manifest.jsonl"
        self.manifest_file = self.manifest_path.open("w", encoding="utf-8")
        self.deleted = 0
        self.failed = 0
        self.skipped = 0

    def close(self) -> None:
        for client in self.clients.values():
            client.logout()
            client.close()
        self.clients.clear()
        self.log_file.close()
        self.manifest_file.close()

    def log(
        self,
        row: dict,
        status: str,
        *,
        backup_file: str = "",
        error: str = "",
    ) -> None:
        self.log_writer.writerow(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "host": row["host"],
                "hash": row["hash"],
                "keeper_host": row["keeper_host"],
                "status": status,
                "backup_file": backup_file,
                "error": error,
            }
        )
        self.log_file.flush()

    def client(self, host: str) -> QBittorrentClient | None:
        if host in self.clients:
            return self.clients[host]
        if host in self.unavailable:
            return None
        config = self.configs.get(host)
        if config is None:
            self.unavailable[host] = "host not found in credentials CSV"
            return None
        try:
            client = QBittorrentClient(
                config,
                timeout=self.timeout,
                verify=self.verify,
                retries=self.retries,
            )
            client.login()
            self.clients[host] = client
            return client
        except Exception as exc:
            self.unavailable[host] = f"{type(exc).__name__}: {exc}"
            return None

    def backup(self, client: QBittorrentClient, row: dict) -> Path:
        data = client.export_torrent(row["hash"])
        directory = self.backup_dir / safe_host_name(row["host"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{row['hash']}.torrent"
        path.write_bytes(data)
        self.manifest_file.write(
            json.dumps(
                {
                    "host": row["host"],
                    "hash": row["hash"],
                    "keeper_host": row["keeper_host"],
                    "name": row.get("name", ""),
                    "size": int(row.get("size") or 0),
                    "save_path": row.get("save_path", ""),
                    "content_path": row.get("content_path", ""),
                    "category": row.get("category", ""),
                    "tags": row.get("tags", ""),
                    "private": row.get("private", ""),
                    "tracker_domain": row.get("tracker_domain", ""),
                    "backup_file": str(path),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.manifest_file.flush()
        return path

    def run(self, deletes: Sequence[dict]) -> None:
        by_host: dict[str, list[dict]] = defaultdict(list)
        for row in deletes:
            by_host[row["host"]].append(row)

        for host in sorted(by_host):
            rows = by_host[host]
            donor = self.client(host)
            if donor is None:
                reason = self.unavailable.get(host, "unavailable")
                print(f"[fail] {host}: {reason}", file=sys.stderr)
                for row in rows:
                    self.log(row, "skipped", error=reason)
                    self.skipped += 1
                continue

            print(f"Deleting {len(rows)} entries from {host}...")
            for batch_number, batch in enumerate(
                chunks(rows, DEFAULT_BATCH_SIZE), start=1
            ):
                self.process_batch(donor, host, batch)
                print(
                    f"  {host}: batch {batch_number} done "
                    f"({self.deleted} deleted, {self.skipped} skipped, "
                    f"{self.failed} failed)"
                )

    def process_batch(
        self, donor: QBittorrentClient, host: str, batch: Sequence[dict]
    ) -> None:
        keeper_hashes: dict[str, list[str]] = defaultdict(list)
        for row in batch:
            keeper_hashes[row["keeper_host"]].append(row["hash"])

        keeper_maps: dict[str, dict[str, dict]] = {}
        for keeper_host, hashes in keeper_hashes.items():
            keeper = self.client(keeper_host)
            if keeper is None:
                continue
            try:
                keeper_maps[keeper_host] = {
                    torrent["hash"]: torrent for torrent in keeper.get_torrents(hashes)
                }
            except Exception as exc:
                self.unavailable[keeper_host] = f"{type(exc).__name__}: {exc}"

        try:
            donor_torrents = {
                torrent["hash"]: torrent
                for torrent in donor.get_torrents([row["hash"] for row in batch])
            }
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for row in batch:
                self.log(row, "failed", error=reason)
                self.failed += 1
            return

        backed_up: list[dict] = []
        backups: dict[str, str] = {}
        for row in batch:
            infohash = row["hash"]
            keeper = keeper_maps.get(row["keeper_host"], {}).get(infohash)
            if keeper is None:
                reason = f"keeper unavailable on {row['keeper_host']}"
                self.log(row, "skipped", error=reason)
                self.skipped += 1
                continue
            if not is_complete(keeper):
                reason = "keeper is not complete"
                self.log(row, "skipped", error=reason)
                self.skipped += 1
                continue
            if infohash not in donor_torrents:
                self.log(row, "skipped", error="donor no longer has the torrent")
                self.skipped += 1
                continue
            try:
                path = self.backup(donor, row)
            except Exception as exc:
                self.log(
                    row,
                    "skipped",
                    error=f"backup failed: {type(exc).__name__}: {exc}",
                )
                self.skipped += 1
                continue
            backups[infohash] = str(path)
            backed_up.append(row)

        if not backed_up:
            return

        try:
            donor.delete_torrents(
                [row["hash"] for row in backed_up], delete_files=False
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for row in backed_up:
                self.log(row, "failed", backup_file=backups[row["hash"]], error=reason)
                self.failed += 1
            return

        try:
            remaining = {
                torrent["hash"]
                for torrent in donor.get_torrents([row["hash"] for row in backed_up])
            }
        except Exception:
            remaining = set()

        for row in backed_up:
            if row["hash"] in remaining:
                self.log(
                    row,
                    "failed",
                    backup_file=backups[row["hash"]],
                    error="torrent still present after delete",
                )
                self.failed += 1
            else:
                self.log(row, "deleted", backup_file=backups[row["hash"]])
                self.deleted += 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply entry-only duplicate deletions from a reviewed plan.",
    )
    parser.add_argument(
        "--plan", default=DEFAULT_PLAN, help=f"plan CSV (default: {DEFAULT_PLAN})"
    )
    parser.add_argument(
        "--hosts",
        default=DEFAULT_HOSTS,
        help=f"credentials CSV (default: {DEFAULT_HOSTS})",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default is dry run)"
    )
    parser.add_argument(
        "--confirm-count",
        type=int,
        default=None,
        help="must equal the number of approved deletions",
    )
    parser.add_argument(
        "--max-deletions", type=int, default=None, help="apply at most this many rows"
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="backup directory (default: backups/dedup-<timestamp>)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"request timeout (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"retries for transient errors (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        rows = load_plan(Path(args.plan))
        deletes, summary = validate_plan(rows)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.max_deletions is not None:
        if args.max_deletions < 1:
            print("error: --max-deletions must be at least 1", file=sys.stderr)
            return 2
        deletes = deletes[: args.max_deletions]

    if args.confirm_count is not None and args.confirm_count != len(deletes):
        print(
            f"error: --confirm-count {args.confirm_count} does not match "
            f"{len(deletes)} deletions",
            file=sys.stderr,
        )
        return 2

    print(f"Plan: {args.plan}")
    print(f"Approved entry-only deletions: {len(deletes):,}")
    print(f"Hosts affected: {summary['hosts']}")
    print(f"Orphaned bytes after apply: {human_bytes(summary['bytes'])}")
    print(f"Vetoed rows: {summary['vetoed']:,}")
    print(f"Skipped rows: {summary['skipped']:,}")

    if not args.apply:
        by_host: dict[str, int] = defaultdict(int)
        for row in deletes:
            by_host[row["host"]] += 1
        print("\nPer host:")
        for host in sorted(by_host):
            print(f"  {host}  {by_host[host]} deletions")
        print(
            "\nDry run only. To apply, pass --apply with the matching "
            f"--confirm-count {len(deletes)}."
        )
        return 0

    if args.confirm_count is None:
        print("error: --apply requires --confirm-count", file=sys.stderr)
        return 2

    try:
        configs = {config.base_url: config for config in load_hosts(Path(args.hosts))}
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    needed_hosts = {row["host"] for row in deletes} | {
        row["keeper_host"] for row in deletes
    }
    missing = sorted(host for host in needed_hosts if host not in configs)
    if missing:
        print(
            f"error: hosts not in credentials CSV: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = (
        Path(args.backup_dir)
        if args.backup_dir
        else Path("backups") / f"dedup-{timestamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)

    applier = Applier(
        configs=configs,
        backup_dir=backup_dir,
        timeout=args.timeout,
        verify=not args.insecure,
        retries=args.retries,
    )
    try:
        applier.run(deletes)
    finally:
        applier.close()

    print()
    print(
        f"Deleted: {applier.deleted:,}  Skipped: {applier.skipped:,}  "
        f"Failed: {applier.failed:,}"
    )
    print(f"Backups: {backup_dir}")
    print(f"Apply log: {applier.log_path}")
    if applier.unavailable:
        print("Unavailable hosts:", file=sys.stderr)
        for host, reason in sorted(applier.unavailable.items()):
            print(f"  {host}: {reason}", file=sys.stderr)

    if applier.failed:
        return 1
    if applier.skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
