"""Tests for the shared qBittorrent API client."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qbt_api import (  # noqa: E402
    AuthError,
    HostConfig,
    QBittorrentClient,
    QBittorrentError,
    tracker_domain,
    tracker_fingerprint,
)
from tests.stub_qbt import FakeQBittorrent  # noqa: E402


def torrent(infohash: str, **overrides) -> dict:
    data = {
        "hash": infohash,
        "name": f"name-{infohash[:4]}",
        "size": 100,
        "progress": 1.0,
        "amount_left": 0,
        "state": "uploading",
        "ratio": 1.5,
        "seeding_time": 100,
        "has_metadata": True,
        "content_path": f"/data/{infohash}",
    }
    data.update(overrides)
    return data


class QBittorrentClientTests(unittest.TestCase):
    def make_server(self, **kwargs) -> FakeQBittorrent:
        server = FakeQBittorrent(**kwargs)
        self.addCleanup(server.close)
        return server

    def config(self, server: FakeQBittorrent, password: str = "secret") -> HostConfig:
        return HostConfig(server.url, "admin", password)

    def test_login_ok_200(self):
        server = self.make_server(login_mode="ok200", version="v5.1.4")
        with QBittorrentClient(self.config(server)) as client:
            self.assertEqual(client.get_version(), "v5.1.4")

    def test_login_ok_204(self):
        server = self.make_server(login_mode="ok204", version="v5.3.0")
        with QBittorrentClient(self.config(server)) as client:
            self.assertEqual(client.get_version(), "v5.3.0")

    def test_login_401_raises(self):
        server = self.make_server(login_mode="fail401")
        with self.assertRaises(AuthError):
            with QBittorrentClient(self.config(server)):
                pass

    def test_legacy_fails_body_raises(self):
        server = self.make_server(login_mode="fail200")
        with self.assertRaises(AuthError):
            with QBittorrentClient(self.config(server)):
                pass

    def test_pagination(self):
        hashes = [f"{index:040x}" for index in range(7)]
        server = self.make_server(torrents=[torrent(h) for h in hashes])
        with QBittorrentClient(self.config(server)) as client:
            seen = [item["hash"] for item in client.iter_torrents(page_size=3)]
        self.assertEqual(seen, hashes)
        self.assertEqual(server.info_requests, 3)

    def test_hashes_filter(self):
        one, two = f"{1:040x}", f"{2:040x}"
        server = self.make_server(torrents=[torrent(one), torrent(two)])
        with QBittorrentClient(self.config(server)) as client:
            result = client.get_torrents([two])
        self.assertEqual([item["hash"] for item in result], [two])

    def test_free_space_stream(self):
        server = self.make_server(free_space=470_922_903_552)
        with QBittorrentClient(self.config(server)) as client:
            self.assertEqual(client.get_free_space(), 470_922_903_552)

    def test_export(self):
        server = self.make_server(torrents=[torrent(f"{1:040x}")])
        with QBittorrentClient(self.config(server)) as client:
            data = client.export_torrent(f"{1:040x}")
        self.assertTrue(data.startswith(b"d"))

    def test_export_rejects_non_bencoded(self):
        server = self.make_server(export_ok=False)
        with QBittorrentClient(self.config(server)) as client:
            with self.assertRaises(QBittorrentError):
                client.export_torrent(f"{1:040x}")

    def test_delete_sends_flag_and_removes(self):
        one, two = f"{1:040x}", f"{2:040x}"
        server = self.make_server(torrents=[torrent(one), torrent(two)])
        with QBittorrentClient(self.config(server)) as client:
            client.delete_torrents([one], delete_files=True)
        self.assertEqual(
            server.delete_calls,
            [{"hashes": [one], "delete_files": "true"}],
        )
        self.assertIsNone(server.torrent(one))
        self.assertIsNotNone(server.torrent(two))

    def test_delete_entry_only(self):
        one = f"{1:040x}"
        server = self.make_server(torrents=[torrent(one)])
        with QBittorrentClient(self.config(server)) as client:
            client.delete_torrents([one], delete_files=False)
        self.assertEqual(server.delete_calls[0]["delete_files"], "false")

    def test_add_torrent(self):
        server = self.make_server()
        with QBittorrentClient(self.config(server)) as client:
            client.add_torrent(
                b"d4:infod4:name4:testee",
                save_path="/data/x",
                category="cat",
                tags="a,b",
                paused=True,
            )
        self.assertEqual(len(server.add_calls), 1)
        self.assertIn("multipart/form-data", server.add_calls[0]["content_type"])

    def test_tracker_helpers(self):
        url = "http://tracker.example:2710/announce?passkey=secret"
        self.assertEqual(tracker_domain(url), "tracker.example")
        self.assertEqual(len(tracker_fingerprint(url)), 16)
        self.assertIsNone(tracker_fingerprint(""))
        self.assertEqual(tracker_domain(""), "")


if __name__ == "__main__":
    unittest.main()
