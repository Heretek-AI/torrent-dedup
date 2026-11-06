"""Tests for the entry-only applier."""

from __future__ import annotations

import csv
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.stub_qbt import FakeQBittorrent  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
HASH = "a" * 40


def torrent(infohash: str = HASH, *, complete: bool = True) -> dict:
    return {
        "hash": infohash,
        "name": "test",
        "size": 100,
        "progress": 1.0 if complete else 0.5,
        "amount_left": 0 if complete else 50,
        "state": "uploading" if complete else "downloading",
        "has_metadata": True,
    }


def plan_row(
    host: str,
    action: str,
    *,
    approved: str = "yes",
    delete_files: str = "false",
    keeper: str = "",
) -> dict:
    return {
        "hash": HASH,
        "host": host,
        "action": action,
        "approved": approved,
        "reason": "test",
        "keeper_host": keeper,
        "name": "test",
        "size": 100,
        "progress": "1.000000",
        "state": "uploading",
        "ratio": "1.0",
        "seeding_time": 100,
        "private": "yes",
        "tracker_domain": "tracker.example",
        "tracker_mismatch": "no",
        "save_path": "/data",
        "content_path": "/data/test",
        "free_space": 1000,
        "delete_files": delete_files,
        "expected_freed_bytes": 0,
        "category": "cat",
        "tags": "tag",
    }


class ApplyTests(unittest.TestCase):
    def setup_pair(self, *, keeper_complete: bool = True, export_ok: bool = True):
        keeper = FakeQBittorrent(
            login_mode="ok204", torrents=[torrent(complete=keeper_complete)]
        )
        donor = FakeQBittorrent(
            login_mode="ok204", torrents=[torrent()], export_ok=export_ok
        )
        self.addCleanup(keeper.close)
        self.addCleanup(donor.close)
        return keeper, donor

    def write_inputs(self, tmp: Path, keeper, donor, rows) -> tuple[Path, Path]:
        plan = tmp / "plan.csv"
        with plan.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        hosts = tmp / "hosts.csv"
        with hosts.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["host", "username", "password"])
            writer.writerow([keeper.url, "admin", "secret-keeper"])
            writer.writerow([donor.url, "admin", "secret-donor"])
        return plan, hosts

    def run_apply(self, plan: Path, hosts: Path, *extra: str):
        return subprocess.run(
            [
                sys.executable,
                str(PROJECT / "apply_dedup.py"),
                "--plan",
                str(plan),
                "--hosts",
                str(hosts),
                "--timeout",
                "10",
                *extra,
            ],
            cwd=PROJECT,
            capture_output=True,
            text=True,
        )

    def test_dry_run_and_apply(self):
        keeper, donor = self.setup_pair()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [
                plan_row(keeper.url, "keep", keeper=keeper.url),
                plan_row(donor.url, "delete", keeper=keeper.url),
            ]
            plan, hosts = self.write_inputs(tmp, keeper, donor, rows)
            backup_dir = tmp / "backups"

            dry = self.run_apply(plan, hosts, "--backup-dir", str(backup_dir))
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(donor.delete_calls, [])
            self.assertIn("Dry run only", dry.stdout)

            applied = self.run_apply(
                plan,
                hosts,
                "--backup-dir",
                str(backup_dir),
                "--apply",
                "--confirm-count",
                "1",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(
                donor.delete_calls,
                [{"hashes": [HASH], "delete_files": "false"}],
            )
            self.assertIsNone(donor.torrent(HASH))
            self.assertIsNotNone(keeper.torrent(HASH))
            backups = list(backup_dir.glob("**/*.torrent"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(backups[0].stat().st_size > 0)
            manifest = backup_dir / "manifest.jsonl"
            self.assertTrue(manifest.exists())
            log = next(tmp.glob("dedup-apply-*.log.csv"))
            text = log.read_text(encoding="utf-8")
            self.assertIn("deleted", text)
            self.assertNotIn("secret-keeper", text)
            self.assertNotIn("secret-donor", text)

    def test_incomplete_keeper_is_skipped(self):
        keeper, donor = self.setup_pair(keeper_complete=False)
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [
                plan_row(keeper.url, "keep", keeper=keeper.url),
                plan_row(donor.url, "delete", keeper=keeper.url),
            ]
            plan, hosts = self.write_inputs(tmp, keeper, donor, rows)
            result = self.run_apply(
                plan,
                hosts,
                "--backup-dir",
                str(tmp / "backups"),
                "--apply",
                "--confirm-count",
                "1",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(donor.delete_calls, [])
            self.assertIsNotNone(donor.torrent(HASH))

    def test_delete_files_plan_is_rejected(self):
        keeper, donor = self.setup_pair()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [
                plan_row(keeper.url, "keep", keeper=keeper.url),
                plan_row(
                    donor.url,
                    "delete",
                    keeper=keeper.url,
                    delete_files="true",
                ),
            ]
            plan, hosts = self.write_inputs(tmp, keeper, donor, rows)
            result = self.run_apply(
                plan,
                hosts,
                "--backup-dir",
                str(tmp / "backups"),
                "--apply",
                "--confirm-count",
                "1",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(donor.delete_calls, [])

    def test_backup_failure_skips_delete(self):
        keeper, donor = self.setup_pair(export_ok=False)
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [
                plan_row(keeper.url, "keep", keeper=keeper.url),
                plan_row(donor.url, "delete", keeper=keeper.url),
            ]
            plan, hosts = self.write_inputs(tmp, keeper, donor, rows)
            result = self.run_apply(
                plan,
                hosts,
                "--backup-dir",
                str(tmp / "backups"),
                "--apply",
                "--confirm-count",
                "1",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(donor.delete_calls, [])
            self.assertIsNotNone(donor.torrent(HASH))

    def test_confirm_count_mismatch(self):
        keeper, donor = self.setup_pair()
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [
                plan_row(keeper.url, "keep", keeper=keeper.url),
                plan_row(donor.url, "delete", keeper=keeper.url),
            ]
            plan, hosts = self.write_inputs(tmp, keeper, donor, rows)
            result = self.run_apply(
                plan,
                hosts,
                "--apply",
                "--confirm-count",
                "5",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(donor.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
