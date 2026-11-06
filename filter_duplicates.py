#!/usr/bin/env python3
"""Keep only infohashes that appear on two or more hosts in torrent-hashes.csv.

Reads the column-per-host CSV produced by dump_torrent_hashes.py, counts how
many distinct host columns contain each infohash, and writes a new CSV with the
same columns but only the hashes that occur on at least --min-hosts hosts.
Hashes unique to a single host are removed.

The input file is never modified.

Usage:
    python filter_duplicates.py
    python filter_duplicates.py --min-hosts 2 --out torrent-hashes-duplicates.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence

DEFAULT_INPUT = "torrent-hashes.csv"
DEFAULT_OUTPUT = "torrent-hashes-duplicates.csv"


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.is_file():
        raise SystemExit(f"error: input file not found: {path}")

    # utf-8-sig tolerates a BOM if the CSV was re-saved by Excel.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise SystemExit(f"error: input file is empty: {path}")
        rows = [row for row in reader if any(cell.strip() for cell in row)]

    width = len(header)
    if width == 0:
        raise SystemExit(f"error: input file has no host columns: {path}")

    normalized: list[list[str]] = []
    for line_number, row in enumerate(rows, start=2):
        if len(row) > width:
            raise SystemExit(
                f"error: row {line_number} has {len(row)} cells but the header has {width}"
            )
        normalized.append(row + [""] * (width - len(row)))
    return header, normalized


def count_hosts(
    header: Sequence[str], rows: Sequence[Sequence[str]]
) -> dict[str, set[int]]:
    """Map each infohash to the set of host columns that contain it."""
    hosts_by_hash: dict[str, set[int]] = {}
    for column in range(len(header)):
        for row in rows:
            value = row[column].strip().lower()
            if value:
                hosts_by_hash.setdefault(value, set()).add(column)
    return hosts_by_hash


def build_columns(
    header: Sequence[str],
    hosts_by_hash: dict[str, set[int]],
    min_hosts: int,
) -> tuple[list[list[str]], int]:
    """Build one filtered, sorted list of hashes per host column."""
    per_column: list[list[str]] = [[] for _ in header]
    kept = 0
    for value, host_columns in hosts_by_hash.items():
        if len(host_columns) >= min_hosts:
            kept += 1
            for column in host_columns:
                per_column[column].append(value)
    for values in per_column:
        values.sort()
    return per_column, kept


def write_table(
    path: Path, header: Sequence[str], columns: Sequence[Sequence[str]]
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    row_count = max((len(values) for values in columns), default=0)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for index in range(row_count):
            writer.writerow(
                [values[index] if index < len(values) else "" for values in columns]
            )
    return row_count


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove infohashes that appear on only one host from torrent-hashes.csv.",
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"column-per-host CSV to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help=f"filtered CSV to write (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--min-hosts",
        type=int,
        default=2,
        help="keep hashes present on at least this many hosts (default: 2)",
    )
    args = parser.parse_args(argv)
    if args.min_hosts < 2:
        parser.error("--min-hosts must be at least 2")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.out)

    if input_path.resolve() == output_path.resolve():
        print(
            "error: refusing to overwrite the input file; choose a different --out",
            file=sys.stderr,
        )
        return 2

    header, rows = read_table(input_path)
    hosts_by_hash = count_hosts(header, rows)
    per_column, kept = build_columns(header, hosts_by_hash, args.min_hosts)

    row_count = write_table(output_path, header, per_column)

    entries = sum(1 for row in rows for cell in row if cell.strip())
    removed = len(hosts_by_hash) - kept
    spread = Counter(
        len(host_columns)
        for host_columns in hosts_by_hash.values()
        if len(host_columns) >= args.min_hosts
    )
    extra_copies = (
        sum(
            len(host_columns)
            for host_columns in hosts_by_hash.values()
            if len(host_columns) >= args.min_hosts
        )
        - kept
    )

    print(f"Read {len(rows)} rows x {len(header)} hosts from {input_path}")
    print(f"  {entries} hash entries, {len(hosts_by_hash)} distinct hashes")
    print(f"  {removed} hashes on fewer than {args.min_hosts} hosts (removed)")
    print(f"  {kept} duplicated hashes kept ({extra_copies} extra copies)")
    for count in sorted(spread):
        print(f"    on exactly {count} hosts: {spread[count]}")
    print(
        f"Wrote {output_path}: {kept} hashes across {len(header)} columns ({row_count} rows)"
    )

    if not kept:
        print(
            "warning: no duplicate hashes found; output contains only the header",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
