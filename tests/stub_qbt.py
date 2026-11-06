"""In-process fake qBittorrent WebUI used by the test suite."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOGIN_PATH = "/api/v2/auth/login"
LOGOUT_PATH = "/api/v2/auth/logout"
VERSION_PATH = "/api/v2/app/version"
TORRENTS_PATH = "/api/v2/torrents/info"
EXPORT_PATH = "/api/v2/torrents/export"
DELETE_PATH = "/api/v2/torrents/delete"
ADD_PATH = "/api/v2/torrents/add"
SYNC_PATH = "/api/v2/sync/maindata"

TORRENT_DATA = b"d4:infod4:name4:testee"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def owner(self) -> "FakeQBittorrent":
        return self.server.owner

    def _send(self, code, body=b"", *, content_type="text/plain", cookie=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _authorized(self) -> bool:
        # Old versions use "SID=...", 5.3+ uses "QBT_SID_<port>=...".
        return "SID" in (self.headers.get("Cookie") or "")

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == LOGIN_PATH:
            self.owner.login_requests += 1
            self._form()
            if self.owner.login_mode == "ok200":
                self._send(200, "Ok.", cookie="SID=fake; path=/")
            elif self.owner.login_mode == "ok204":
                self._send(
                    204, cookie=f"QBT_SID_{self.server.server_port}=fake; path=/"
                )
            elif self.owner.login_mode == "fail401":
                self._send(401, "Unauthorized")
            else:
                self._send(200, "Fails.")
            return

        if parsed.path == LOGOUT_PATH:
            self._send(200)
            return

        if not self._authorized():
            self._send(403, "Forbidden")
            return

        if parsed.path == DELETE_PATH:
            form = self._form()
            hashes = [h for h in form.get("hashes", [""])[0].split("|") if h]
            delete_files = form.get("deleteFiles", ["false"])[0]
            self.owner.delete_calls.append(
                {"hashes": hashes, "delete_files": delete_files}
            )
            self.owner.torrents = [
                torrent
                for torrent in self.owner.torrents
                if torrent["hash"] not in hashes
            ]
            self._send(200)
            return

        if parsed.path == ADD_PATH:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            self.owner.add_calls.append(
                {
                    "bytes": len(raw),
                    "content_type": self.headers.get("Content-Type") or "",
                }
            )
            self._send(200, "Ok.")
            return

        self._send(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send(403, "Forbidden")
            return

        if parsed.path == VERSION_PATH:
            self._send(200, self.owner.version)
            return

        if parsed.path == TORRENTS_PATH:
            self.owner.info_requests += 1
            query = parse_qs(parsed.query)
            items = list(self.owner.torrents)

            if "hashes" in query:
                wanted = set(query["hashes"][0].split("|"))
                items = [torrent for torrent in items if torrent["hash"] in wanted]
            else:
                limit = int(query.get("limit", ["1000000"])[0])
                offset = int(query.get("offset", ["0"])[0])
                items = items[offset : offset + limit]

            self._send(200, json.dumps(items), content_type="application/json")
            return

        if parsed.path == EXPORT_PATH:
            if not self.owner.export_ok:
                self._send(200, b"")
            else:
                self._send(200, TORRENT_DATA, content_type="application/x-bittorrent")
            return

        if parsed.path == SYNC_PATH:
            payload = {
                "categories": {
                    f"cat{i}": {"name": f"cat{i}", "savePath": ""} for i in range(200)
                },
                "server_state": {"free_space_on_disk": self.owner.free_space},
                # Deliberately large so a full download would be obvious in timing.
                "torrents": {str(i): {"hash": str(i)} for i in range(5000)},
            }
            self._send(200, json.dumps(payload), content_type="application/json")
            return

        self._send(404, "Not Found")


class FakeQBittorrent:
    def __init__(
        self,
        *,
        login_mode: str = "ok204",
        torrents: list[dict] | None = None,
        free_space: int = 0,
        version: str = "v5.3.0",
        export_ok: bool = True,
    ) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.owner = self
        self.login_mode = login_mode
        self.torrents = [dict(torrent) for torrent in (torrents or [])]
        self.free_space = free_space
        self.version = version
        self.export_ok = export_ok
        self.login_requests = 0
        self.info_requests = 0
        self.delete_calls: list[dict] = []
        self.add_calls: list[dict] = []
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def port(self) -> int:
        return self.server.server_port

    def torrent(self, infohash: str) -> dict | None:
        return next(
            (torrent for torrent in self.torrents if torrent["hash"] == infohash),
            None,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
