"""End-to-end tests for collect_inventory.py against fake qBittorrent hosts."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.stub_qbt import FakeQBittorrent  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]


def torrent(infohash: str, *, tracker: str = "http://tracker.example/announce") -> dict:
    return {
        "hash": infohash.upper(),  # inventory must normalise to lower case
        "name": f"name-{infohash[:4]}",
        "size": 1234,
        "total_size": 1234,
        "progress": 1.0,
        "amount_left": 0,
        "state": "uploading",
        "ratio": 1.25,
        "seeding_time": 3600,
        "added_on": 100,
        "completion_on": 200,
        "last_activity": 300,
        "save_path": "/data/complete",
        "content_path": f"/data/complete/{infohash}",
        "category": "cat",
        "tags": "tag",
        "private": True,
        "has_metadata": True,
        "availability": 1.0,
        "tracker": tracker,
        "trackers_count": 1,
        "infohash_v1": infohash,
        "infohash_v2": "",
    }


class CollectInventoryTests(unittest.TestCase):
    def test_inventory_with_partial_failure(self):
        hashes_a = [f"{index:040x}" for index in range(5)]
        hashes_b = [f"{index + 10:040x}" for index in range(2)]

        server_a = FakeQBittorrent(
            login_mode="ok204",
            torrents=[torrent(h) for h in hashes_a],
            free_space=111,
            version="v5.3.0",
        )
        server_b = FakeQBittorrent(
            login_mode="ok200",
            torrents=[torrent(h) for h in hashes_b],
            free_space=222,
            version="v5.1.4",
        )
        server_bad = FakeQBittorrent(login_mode="fail401")
        for server in (server_a, server_b, server_bad):
            self.addCleanup(server.close)

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hosts_csv = tmp / "hosts.csv"
            output = tmp / "inventory.jsonl"

            # utf-8-sig reproduces an Excel BOM.
            with hosts_csv.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["host", "username", "password"])
                writer.writerow([server_a.url, "admin", "secret-a"])
                writer.writerow([server_b.url, "admin", "secret-b"])
                writer.writerow([server_bad.url, "admin", "secret-bad"])

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "collect_inventory.py"),
                    "--hosts",
                    str(hosts_csv),
                    "--out",
                    str(output),
                    "--workers",
                    "3",
                    "--page-size",
                    "2",
                    "--timeout",
                    "10",
                ],
                cwd=PROJECT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)

            lines = output.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines if line.strip()]
            torrents = [
                item for item in records if item.get("host") and item.get("hash")
            ]
            metas = [item for item in records if item.get("type") == "meta"]
            final_meta = metas[-1]

            self.assertEqual(final_meta["status"], "finished")
            self.assertEqual(final_meta["totals"]["hosts"], 3)
            self.assertEqual(final_meta["totals"]["hosts_ok"], 2)
            self.assertEqual(final_meta["totals"]["torrents"], 7)
            self.assertEqual(len(torrents), 7)
            self.assertEqual(
                {item["hash"] for item in torrents},
                set(hashes_a) | set(hashes_b),
            )
            self.assertTrue(
                all(item["hash"] == item["hash"].lower() for item in torrents)
            )
            self.assertTrue(
                all(item["tracker_domain"] == "tracker.example" for item in torrents)
            )
            self.assertTrue(
                all(len(item["tracker_fingerprint"]) == 16 for item in torrents)
            )

            failed_host = next(
                host for host in final_meta["hosts"] if host["host"] == server_bad.url
            )
            self.assertIn("AuthError", failed_host["error"])

            # The failed host must contribute no partial records.
            self.assertTrue(all(item["host"] != server_bad.url for item in torrents))

            # Pagination: 5 items at page size 2 -> 3 calls; 2 items -> 2 calls.
            self.assertEqual(server_a.info_requests, 3)
            self.assertEqual(server_b.info_requests, 2)

            host_summary = output.with_suffix(".hosts.csv")
            with host_summary.open("r", encoding="utf-8", newline="") as fh:
                summary_rows = list(csv.DictReader(fh))
            self.assertEqual(len(summary_rows), 3)
            free_by_host = {
                row["host"]: row["free_space_bytes"] for row in summary_rows
            }
            self.assertEqual(free_by_host[server_a.url], "111")
            self.assertEqual(free_by_host[server_b.url], "222")

            combined = output.read_text(encoding="utf-8") + host_summary.read_text(
                encoding="utf-8"
            )
            self.assertNotIn("secret-a", combined)
            self.assertNotIn("secret-b", combined)
            self.assertNotIn("secret-bad", combined)
            self.assertNotIn("passkey", combined)

    def test_only_filter(self):
        server = FakeQBittorrent(
            login_mode="ok204",
            torrents=[torrent(f"{1:040x}")],
            free_space=1,
        )
        self.addCleanup(server.close)
        other = FakeQBittorrent(
            login_mode="ok204",
            torrents=[torrent(f"{2:040x}")],
            free_space=2,
        )
        self.addCleanup(other.close)

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hosts_csv = tmp / "hosts.csv"
            output = tmp / "inventory.jsonl"
            with hosts_csv.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["host", "username", "password"])
                writer.writerow([server.url, "admin", "x"])
                writer.writerow([other.url, "admin", "y"])

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "collect_inventory.py"),
                    "--hosts",
                    str(hosts_csv),
                    "--out",
                    str(output),
                    "--only",
                    str(server.port),
                    "--timeout",
                    "10",
                ],
                cwd=PROJECT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            torrents = [item for item in records if item.get("hash")]
            self.assertEqual(len(torrents), 1)
            self.assertEqual(torrents[0]["host"], server.url)


if __name__ == "__main__":
    unittest.main()
