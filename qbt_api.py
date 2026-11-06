#!/usr/bin/env python3
"""Shared client for the qBittorrent Web API (v2).

Supports both legacy login responses (HTTP 200 ``Ok.``) and the newer
HTTP 204 response used by qBittorrent 5.3+, paginates torrent listings, and
fetches free disk space without downloading the full ``sync/maindata`` payload.
"""

from __future__ import annotations

import csv
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import urlparse

import requests
from requests import Session
from requests.exceptions import RequestException

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 2
DEFAULT_PAGE_SIZE = 5000

RETRYABLE_STATUS = {500, 502, 503, 504}
FREE_SPACE_PREFIX_LIMIT = 8 * 1024 * 1024

LOGIN_PATH = "/api/v2/auth/login"
LOGOUT_PATH = "/api/v2/auth/logout"
VERSION_PATH = "/api/v2/app/version"
TORRENTS_PATH = "/api/v2/torrents/info"
EXPORT_PATH = "/api/v2/torrents/export"
DELETE_PATH = "/api/v2/torrents/delete"
ADD_PATH = "/api/v2/torrents/add"
SYNC_PATH = "/api/v2/sync/maindata"

_FREE_SPACE_RE = re.compile(rb'"free_space_on_disk"\s*:\s*(-?\d+)')


class QBittorrentError(RuntimeError):
    """Raised when a qBittorrent API call fails."""


class AuthError(QBittorrentError):
    """Raised on a rejected login or an authorization failure."""


@dataclass(frozen=True)
class HostConfig:
    base_url: str
    username: str
    password: str


def normalize_host(value: str) -> str:
    """Accept host:port or a full URL and return a clean base URL."""
    host = value.strip()
    if not host:
        return ""
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


def load_hosts(path: Path) -> list[HostConfig]:
    """Read host,username,password rows from a CSV file."""
    if not path.is_file():
        raise FileNotFoundError(f"hosts file not found: {path}")

    # utf-8-sig tolerates the BOM Excel adds when saving CSV.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"hosts file has no header row: {path}")

        columns = {name.strip().lower(): name for name in reader.fieldnames if name}
        for required in ("host", "username", "password"):
            if required not in columns:
                raise ValueError(
                    f"hosts file is missing a '{required}' column "
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
        raise ValueError(f"no configured host matches: {', '.join(missing)}")
    return [cfg for cfg in configs if cfg.base_url in chosen]


def tracker_domain(url: str) -> str:
    """Return a tracker hostname without exposing passkeys."""
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else "http://" + value)
    return parsed.hostname or ""


def tracker_fingerprint(url: str) -> str | None:
    """Return a short hash of the full tracker URL, for cross-host comparison."""
    value = (url or "").strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class QBittorrentClient:
    """One authenticated session against one qBittorrent instance."""

    def __init__(
        self,
        config: HostConfig,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        verify: bool = True,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.verify = verify
        self.retries = retries
        self.session = Session()
        self.session.headers.update(
            {"Referer": config.base_url + "/", "Origin": config.base_url}
        )

    def __enter__(self) -> "QBittorrentClient":
        self.login()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.logout()
        self.close()
        return False

    def close(self) -> None:
        self.session.close()

    def _request_timeout(self) -> tuple[float, float]:
        return (min(10.0, self.timeout), self.timeout)

    def login(self) -> None:
        if not (self.config.username or self.config.password):
            return

        # Deliberately no retries: repeated failures can get the IP banned.
        response = self.session.post(
            self.config.base_url + LOGIN_PATH,
            data={"username": self.config.username, "password": self.config.password},
            timeout=self._request_timeout(),
            verify=self.verify,
        )
        if response.status_code in (401, 403):
            detail = (
                "IP banned" if response.status_code == 403 else "bad username/password"
            )
            raise AuthError(
                f"{self.config.base_url}: login failed ({response.status_code}): {detail}"
            )
        response.raise_for_status()

        # 4.x/5.1 return 200 "Ok."; 5.3+ return 204 with an empty body.
        body = response.text.strip()
        if body and body.lower() != "ok.":
            raise AuthError(f"{self.config.base_url}: login rejected: {body[:60]!r}")

    def logout(self) -> None:
        try:
            self.session.post(
                self.config.base_url + LOGOUT_PATH,
                timeout=(min(5.0, self.timeout), min(5.0, self.timeout)),
                verify=self.verify,
            )
        except RequestException:
            pass

    def _check_auth(self, response: requests.Response) -> None:
        if response.status_code in (401, 403):
            raise AuthError(
                f"{self.config.base_url}: access denied ({response.status_code})"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        retries: int | None = None,
        **kwargs,
    ) -> requests.Response:
        attempts = self.retries if retries is None else retries
        attempt = 0
        while True:
            try:
                response = self.session.request(
                    method,
                    self.config.base_url + path,
                    timeout=self._request_timeout(),
                    verify=self.verify,
                    **kwargs,
                )
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    raise RequestException(f"server returned {response.status_code}")
                return response
            except RequestException:
                if attempt >= attempts:
                    raise
                attempt += 1
                time.sleep(1.5 * attempt)

    def get_version(self) -> str:
        response = self._request("GET", VERSION_PATH)
        self._check_auth(response)
        response.raise_for_status()
        return response.text.strip()

    def get_free_space(self) -> int | None:
        """Read server_state.free_space_on_disk without downloading all torrents."""
        response = self._request("GET", SYNC_PATH, stream=True)
        with response:
            self._check_auth(response)
            response.raise_for_status()
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                buffer.extend(chunk)
                match = _FREE_SPACE_RE.search(buffer)
                if match:
                    value = int(match.group(1))
                    return value if value >= 0 else None
                if len(buffer) >= FREE_SPACE_PREFIX_LIMIT:
                    break
        return None

    def iter_torrents(
        self,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[dict]:
        offset = 0
        while True:
            response = self._request(
                "GET",
                TORRENTS_PATH,
                params={"limit": page_size, "offset": offset},
            )
            self._check_auth(response)
            response.raise_for_status()

            page = response.json()
            if not isinstance(page, list):
                raise QBittorrentError(
                    f"{self.config.base_url}: expected a list of torrents"
                )
            if not page:
                return

            yield from page
            offset += len(page)
            if progress is not None:
                progress(offset)

            if len(page) < page_size:
                return
            if offset > 5_000_000:
                raise QBittorrentError(
                    f"{self.config.base_url}: pagination did not terminate"
                )

    def get_torrents(self, hashes: Sequence[str] | None = None) -> list[dict]:
        params = {"hashes": "|".join(hashes)} if hashes else None
        response = self._request("GET", TORRENTS_PATH, params=params)
        self._check_auth(response)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise QBittorrentError(
                f"{self.config.base_url}: expected a list of torrents"
            )
        return payload

    def export_torrent(self, infohash: str) -> bytes:
        response = self._request("GET", EXPORT_PATH, params={"hash": infohash})
        self._check_auth(response)
        response.raise_for_status()
        data = response.content
        if not data or not data.startswith(b"d"):
            raise QBittorrentError(
                f"{self.config.base_url}: export for {infohash} was not bencoded"
            )
        return data

    def delete_torrents(self, hashes: Sequence[str], *, delete_files: bool) -> None:
        if not hashes:
            return
        response = self._request(
            "POST",
            DELETE_PATH,
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
            retries=0,
        )
        self._check_auth(response)
        response.raise_for_status()

    def add_torrent(
        self,
        torrent: bytes,
        *,
        save_path: str | None = None,
        category: str | None = None,
        tags: str | None = None,
        paused: bool = False,
    ) -> None:
        data: dict[str, str] = {"paused": "true" if paused else "false"}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        files = {"torrents": ("torrent.torrent", torrent, "application/x-bittorrent")}
        response = self._request("POST", ADD_PATH, data=data, files=files, retries=0)
        self._check_auth(response)
        response.raise_for_status()
