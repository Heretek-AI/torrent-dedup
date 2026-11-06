#!/usr/bin/env python3
"""Dump qBittorrent infohashes from several hosts into one column-per-host CSV.

Reads a credentials CSV (host,username,password), logs into each qBittorrent
Web API instance (4.x/5.1 "Ok." style and 5.3+ 204 style), pages through
/api/v2/torrents/info, and writes a CSV whose header row is the host URLs and
whose columns contain the sorted, de-duplicated infohashes reported by that host.

Example output:

    http://qb1.local:8080,http://qb2.local:8081
    0123456789abcdef...,fedcba9876543210...
    abcdef0123456789...,

Usage:
    python dump_torrent_hashes.py --hosts qbittorrent-hosts.csv --out torrent-hashes.csv

Exit codes: 0 = all hosts succeeded, 1 = some failed, 2 = none succeeded.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import requests
import urllib3
from requests import Session
from requests.exceptions import RequestException

DEFAULT_HOSTS_FILE = "qbittorrent-hosts.csv"
DEFAULT_OUTPUT_FILE = "torrent-hashes.csv"
DEFAULT_TIMEOUT = 60.0
DEFAULT_WORKERS = 8
DEFAULT_PAGE_SIZE = 5000
DEFAULT_RETRIES = 2

LOGIN_PATH = "/api/v2/auth/login"
LOGOUT_PATH = "/api/v2/auth/logout"
VERSION_PATH = "/api/v2/app/version"
TORRENTS_PATH = "/api/v2/torrents/info"

RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class HostConfig:
    base_url: str
    username: str
    password: str


@dataclass
class HostResult:
    host: str
    version: str | None = None
    hashes: list[str] = field(default_factory=list)
    error: str | None = None


def normalize_host(value: str) -> str:
    """Accept host:port or a full URL and return a clean base URL."""
    host = value.strip()
    if not host:
        return ""
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


def read_hosts(path: Path) -> list[HostConfig]:
    if not path.is_file():
        raise SystemExit(f"error: hosts file not found: {path}")

    # utf-8-sig tolerates the BOM Excel adds when saving CSV.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"error: hosts file has no header row: {path}")

        columns = {name.strip().lower(): name for name in reader.fieldnames if name}
        for required in ("host", "username", "password"):
            if required not in columns:
                raise SystemExit(
                    f"error: hosts file is missing a '{required}' column "
                    f"(found: {', '.join(reader.fieldnames)})"
                )

        configs: list[HostConfig] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            host = normalize_host(row.get(columns["host"]) or "")
            if not host:
                continue
            if host in seen:
                print(
                    f"warning: duplicate host on line {line_number}, skipping {host}",
                    file=sys.stderr,
                )
                continue
            seen.add(host)
            configs.append(
                HostConfig(
                    base_url=host,
                    username=(row.get(columns["username"]) or "").strip(),
                    password=row.get(columns["password"]) or "",
                )
            )
    return configs


def select_hosts(
    configs: Sequence[HostConfig], selectors: Sequence[str]
) -> list[HostConfig]:
    """Keep only hosts matching a full URL or a trailing :port."""
    chosen: set[str] = set()
    missing: list[str] = []
    for selector in selectors:
        cleaned = selector.strip().rstrip("/")
        normalized = normalize_host(cleaned)
        matches = [
            cfg.base_url
            for cfg in configs
            if cfg.base_url == normalized or cfg.base_url.endswith(":" + cleaned)
        ]
        if not matches:
            missing.append(selector)
        chosen.update(matches)
    if missing:
        raise SystemExit(f"error: no configured host matches: {', '.join(missing)}")
    return [cfg for cfg in configs if cfg.base_url in chosen]


def request_timeout(timeout: float) -> tuple[float, float]:
    return (min(10.0, timeout), timeout)


def request_with_retries(
    session: Session,
    method: str,
    url: str,
    *,
    retries: int,
    timeout: float,
    verify: bool,
    **kwargs,
):
    """Retry transient network errors and 5xx responses; never retry 4xx."""
    attempt = 0
    while True:
        try:
            response = session.request(
                method, url, timeout=request_timeout(timeout), verify=verify, **kwargs
            )
            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                raise RequestException(f"server returned {response.status_code}")
            return response
        except RequestException:
            if attempt >= retries:
                raise
            attempt += 1
            time.sleep(1.5 * attempt)


def login(
    session: Session, cfg: HostConfig, *, timeout: float, verify: bool
) -> str | None:
    """Log in and return an error message, or None on success."""
    response = session.post(
        cfg.base_url + LOGIN_PATH,
        data={"username": cfg.username, "password": cfg.password},
        timeout=request_timeout(timeout),
        verify=verify,
    )
    if response.status_code in (401, 403):
        detail = "IP banned" if response.status_code == 403 else "bad username/password"
        return f"login failed ({response.status_code}): {detail}"
    response.raise_for_status()

    # 4.x/5.1 return 200 "Ok."; 5.3+ return 204 with an empty body.
    body = response.text.strip()
    if body and body.lower() != "ok.":
        return f"login rejected: {body[:60]!r}"
    return None


def fetch_hashes(
    cfg: HostConfig,
    *,
    timeout: float,
    verify: bool,
    page_size: int,
    retries: int,
) -> HostResult:
    session = Session()
    session.headers.update({"Referer": cfg.base_url + "/", "Origin": cfg.base_url})
    version: str | None = None
    try:
        if cfg.username or cfg.password:
            login_error = login(session, cfg, timeout=timeout, verify=verify)
            if login_error:
                return HostResult(cfg.base_url, error=login_error)

        try:
            version_response = session.get(
                cfg.base_url + VERSION_PATH,
                timeout=request_timeout(timeout),
                verify=verify,
            )
            if version_response.ok:
                version = version_response.text.strip() or None
        except RequestException:
            pass

        hashes: set[str] = set()
        offset = 0
        while True:
            response = request_with_retries(
                session,
                "GET",
                cfg.base_url + TORRENTS_PATH,
                params={"limit": page_size, "offset": offset},
                retries=retries,
                timeout=timeout,
                verify=verify,
            )
            if response.status_code in (401, 403):
                return HostResult(
                    cfg.base_url,
                    version=version,
                    error=f"access denied ({response.status_code}): login required or IP banned",
                )
            response.raise_for_status()

            page = response.json()
            if not isinstance(page, list):
                return HostResult(
                    cfg.base_url,
                    version=version,
                    error="unexpected API response: expected a torrent list",
                )
            if not page:
                break

            before = len(hashes)
            for torrent in page:
                if isinstance(torrent, dict):
                    value = str(torrent.get("hash", "")).strip().lower()
                    if value:
                        hashes.add(value)

            offset += len(page)
            print(
                f"  {cfg.base_url}: {offset} torrents read, {len(hashes)} unique hashes",
                file=sys.stderr,
                flush=True,
            )

            if len(page) < page_size:
                break
            if len(hashes) == before:
                # Server ignored offset; stop instead of looping forever.
                break

        return HostResult(cfg.base_url, version=version, hashes=sorted(hashes))

    except (RequestException, ValueError) as exc:
        return HostResult(cfg.base_url, version=version, error=f"request failed: {exc}")
    finally:
        try:
            session.post(
                cfg.base_url + LOGOUT_PATH,
                timeout=(min(5.0, timeout), min(5.0, timeout)),
                verify=verify,
            )
        except RequestException:
            pass
        session.close()


def collect(
    configs: Sequence[HostConfig],
    *,
    workers: int,
    timeout: float,
    verify: bool,
    page_size: int,
    retries: int,
) -> list[HostResult]:
    results: dict[str, HostResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(configs)))) as pool:
        futures = {
            pool.submit(
                fetch_hashes,
                cfg,
                timeout=timeout,
                verify=verify,
                page_size=page_size,
                retries=retries,
            ): cfg
            for cfg in configs
        }
        for future in as_completed(futures):
            cfg = futures[future]
            try:
                results[cfg.base_url] = future.result()
            except Exception as exc:  # defensive: never lose a whole run to one host
                results[cfg.base_url] = HostResult(
                    cfg.base_url, error=f"internal error: {exc}"
                )
    return [results[cfg.base_url] for cfg in configs]


def write_csv(path: Path, hosts: Sequence[str], results: Sequence[HostResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row_count = max((len(result.hashes) for result in results), default=0)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(hosts)
        for index in range(row_count):
            writer.writerow(
                [
                    result.hashes[index] if index < len(result.hashes) else ""
                    for result in results
                ]
            )


def report(results: Sequence[HostResult]) -> int:
    ok = [result for result in results if result.error is None]
    failed = [result for result in results if result.error is not None]

    for result in results:
        label = f"{result.host} [{result.version}]" if result.version else result.host
        if result.error is None:
            print(f"[ok]   {label}  {len(result.hashes)} hashes")
        else:
            print(f"[fail] {label}  {result.error}", file=sys.stderr)

    occurrences = Counter(infohash for result in ok for infohash in result.hashes)
    shared = sum(1 for count in occurrences.values() if count > 1)
    print(
        f"{len(ok)}/{len(results)} hosts succeeded, "
        f"{sum(len(result.hashes) for result in ok)} hashes collected, "
        f"{len(occurrences)} unique, {shared} present on 2+ hosts"
    )

    if not ok:
        return 2
    return 1 if failed else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump qBittorrent infohashes into a CSV with one column per host.",
    )
    parser.add_argument(
        "--hosts",
        default=DEFAULT_HOSTS_FILE,
        help=f"credentials CSV (default: {DEFAULT_HOSTS_FILE})",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT_FILE,
        help=f"output CSV (default: {DEFAULT_OUTPUT_FILE})",
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
    if args.page_size < 1:
        parser.error("--page-size must be at least 1")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    configs = read_hosts(Path(args.hosts))
    if not configs:
        print("error: no hosts found in the credentials CSV", file=sys.stderr)
        return 2
    if args.only:
        selectors = [
            part for value in args.only for part in value.split(",") if part.strip()
        ]
        configs = select_hosts(configs, selectors)

    print(f"Querying {len(configs)} host(s), {args.page_size} torrents per page...")
    results = collect(
        configs,
        workers=args.workers,
        timeout=args.timeout,
        verify=not args.insecure,
        page_size=args.page_size,
        retries=args.retries,
    )
    write_csv(Path(args.out), [config.base_url for config in configs], results)
    print(f"Wrote {args.out}")
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
