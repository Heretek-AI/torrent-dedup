#!/usr/bin/env python3
"""Collect a live inventory from every configured qBittorrent host.

Writes a JSONL snapshot (one torrent per line plus metadata lines) and a host
summary CSV. This is read-only: no torrent is added, changed, or deleted.

Usage:
    python collect_inventory.py
    python collect_inventory.py --only 8083 --out dedup-inventory-test.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import threading
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from qbt_api import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    HostConfig,
    QBittorrentClient,
    load_hosts,
    select_hosts,
    tracker_domain,
    tracker_fingerprint,
)

DEFAULT_HOSTS_FILE = "qbittorrent-hosts.csv"
DEFAULT_WORKERS = 8


@dataclass
class HostSnapshot:
    host: str
    version: str | None = None
    free_space: int | None = None
    torrent_count: int = 0
    error: str | None = None
    elapsed: float = 0.0
    part_path: Path | None = None


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


def torrent_record(host: str, torrent: dict) -> dict:
    tracker = str(torrent.get("tracker") or "")
    return {
        "host": host,
        "hash": str(torrent.get("hash") or "").strip().lower(),
        "name": str(torrent.get("name") or ""),
        "size": as_int(torrent.get("size")),
        "total_size": as_int(torrent.get("total_size")),
        "progress": as_float(torrent.get("progress")),
        "state": str(torrent.get("state") or ""),
        "ratio": as_float(torrent.get("ratio")),
        "seeding_time": as_int(torrent.get("seeding_time")),
        "added_on": as_int(torrent.get("added_on")),
        "completion_on": as_int(torrent.get("completion_on")),
        "last_activity": as_int(torrent.get("last_activity")),
        "save_path": str(torrent.get("save_path") or ""),
        "content_path": str(torrent.get("content_path") or ""),
        "category": str(torrent.get("category") or ""),
        "tags": str(torrent.get("tags") or ""),
        "private": bool(torrent.get("private")),
        "has_metadata": bool(torrent.get("has_metadata", True)),
        "amount_left": as_int(torrent.get("amount_left")),
        "availability": as_float(torrent.get("availability"), -1.0),
        "tracker_domain": tracker_domain(tracker),
        "tracker_fingerprint": tracker_fingerprint(tracker),
        "tracker_count": as_int(torrent.get("trackers_count")),
        "infohash_v1": str(torrent.get("infohash_v1") or ""),
        "infohash_v2": str(torrent.get("infohash_v2") or ""),
    }


def fetch_host(
    config: HostConfig,
    *,
    part_path: Path,
    timeout: float,
    verify: bool,
    retries: int,
    page_size: int,
) -> HostSnapshot:
    started = time.monotonic()
    version: str | None = None
    free_space: int | None = None
    count = 0

    try:
        with QBittorrentClient(
            config, timeout=timeout, verify=verify, retries=retries
        ) as client:
            try:
                version = client.get_version()
            except Exception:
                version = None

            try:
                free_space = client.get_free_space()
            except Exception:
                free_space = None

            with part_path.open("w", encoding="utf-8", newline="\n") as fh:

                def on_progress(seen: int) -> None:
                    print(
                        f"  {config.base_url}: {seen} torrents...",
                        file=sys.stderr,
                        flush=True,
                    )

                for torrent in client.iter_torrents(
                    page_size=page_size, progress=on_progress
                ):
                    record = torrent_record(config.base_url, torrent)
                    if not record["hash"]:
                        continue
                    fh.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                    count += 1

        return HostSnapshot(
            host=config.base_url,
            version=version,
            free_space=free_space,
            torrent_count=count,
            elapsed=time.monotonic() - started,
            part_path=part_path,
        )
    except Exception as exc:
        part_path.unlink(missing_ok=True)
        return HostSnapshot(
            host=config.base_url,
            version=version,
            free_space=free_space,
            torrent_count=0,
            error=f"{type(exc).__name__}: {exc}",
            elapsed=time.monotonic() - started,
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a read-only qBittorrent inventory snapshot.",
    )
    parser.add_argument(
        "--hosts",
        default=DEFAULT_HOSTS_FILE,
        help=f"credentials CSV (default: {DEFAULT_HOSTS_FILE})",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output JSONL path (default: dedup-inventory-<timestamp>.jsonl)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel hosts (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"torrents per API call (default: {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"retries for transient errors (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="HOST_OR_PORT",
        help="only query these hosts (full URL or port; space/comma separated)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification for https hosts",
    )
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.page_size < 1:
        parser.error("--page-size must be at least 1")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    return args


def append_part(destination: Path, part: Path, lock: threading.Lock) -> None:
    with (
        lock,
        destination.open("a", encoding="utf-8", newline="\n") as out,
        part.open("r", encoding="utf-8", newline="") as source,
    ):
        shutil.copyfileobj(source, out, length=1024 * 1024)


def write_host_summary(path: Path, snapshots: Sequence[HostSnapshot]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "host",
                "version",
                "free_space_bytes",
                "torrent_count",
                "error",
                "elapsed_seconds",
            ]
        )
        for snapshot in snapshots:
            writer.writerow(
                [
                    snapshot.host,
                    snapshot.version or "",
                    snapshot.free_space if snapshot.free_space is not None else "",
                    snapshot.torrent_count,
                    snapshot.error or "",
                    f"{snapshot.elapsed:.1f}",
                ]
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        configs = load_hosts(Path(args.hosts))
        if args.only:
            selectors = [
                part for value in args.only for part in value.split(",") if part.strip()
            ]
            configs = select_hosts(configs, selectors)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not configs:
        print("error: no hosts found in the credentials CSV", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        Path(args.out) if args.out else Path(f"dedup-inventory-{timestamp}.jsonl")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    host_csv_path = output_path.with_suffix(".hosts.csv")

    initial_meta = {
        "type": "meta",
        "inventory_version": 1,
        "status": "started",
        "generated_at": started_at.isoformat(),
        "hosts": [{"host": config.base_url} for config in configs],
    }
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(initial_meta, ensure_ascii=False) + "\n")

    print(f"Reading {len(configs)} host(s)...")
    lock = threading.Lock()
    snapshots: list[HostSnapshot] = []
    part_paths: dict[str, Path] = {
        config.base_url: output_path.with_name(f".{output_path.name}.{index}.part")
        for index, config in enumerate(configs)
    }

    with ThreadPoolExecutor(
        max_workers=max(1, min(args.workers, len(configs)))
    ) as pool:
        futures = {
            pool.submit(
                fetch_host,
                config,
                part_path=part_paths[config.base_url],
                timeout=args.timeout,
                verify=not args.insecure,
                retries=args.retries,
                page_size=args.page_size,
            ): config
            for config in configs
        }
        for future in as_completed(futures):
            snapshot = future.result()
            snapshots.append(snapshot)
            if snapshot.error is None and snapshot.part_path is not None:
                append_part(output_path, snapshot.part_path, lock)
                snapshot.part_path.unlink(missing_ok=True)
                snapshot.part_path = None
                free = (
                    f"{snapshot.free_space / 1024**3:.1f} GiB free"
                    if snapshot.free_space is not None
                    else "free space unknown"
                )
                print(
                    f"[ok]   {snapshot.host} [{snapshot.version or '?'}] "
                    f"{snapshot.torrent_count} torrents, {free}, "
                    f"{snapshot.elapsed:.1f}s"
                )
            else:
                print(f"[fail] {snapshot.host}  {snapshot.error}", file=sys.stderr)

    order = {config.base_url: index for index, config in enumerate(configs)}
    snapshots.sort(key=lambda snapshot: order.get(snapshot.host, 0))

    finished_at = datetime.now(timezone.utc)
    ok_count = sum(1 for snapshot in snapshots if snapshot.error is None)
    total_torrents = sum(
        snapshot.torrent_count for snapshot in snapshots if snapshot.error is None
    )
    final_meta = {
        "type": "meta",
        "inventory_version": 1,
        "status": "finished",
        "generated_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "hosts": [
            {
                "host": snapshot.host,
                "version": snapshot.version,
                "free_space": snapshot.free_space,
                "torrent_count": snapshot.torrent_count,
                "error": snapshot.error,
                "elapsed_seconds": round(snapshot.elapsed, 1),
            }
            for snapshot in snapshots
        ],
        "totals": {
            "hosts": len(snapshots),
            "hosts_ok": ok_count,
            "torrents": total_torrents,
        },
    }
    with output_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(final_meta, ensure_ascii=False) + "\n")

    write_host_summary(host_csv_path, snapshots)

    size_mb = output_path.stat().st_size / 1024**2
    print(f"{ok_count}/{len(snapshots)} hosts succeeded, {total_torrents:,} torrents")
    print(f"Wrote {output_path} ({size_mb:.1f} MB)")
    print(f"Wrote {host_csv_path}")

    if ok_count == 0:
        return 2
    return 0 if ok_count == len(snapshots) else 1


if __name__ == "__main__":
    sys.exit(main())
