"""Tests for keeper selection and plan generation."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plan_dedup import build_plan  # noqa: E402
from qbt_api import tracker_domain, tracker_fingerprint  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
HASH_A = "a" * 40
HASH_B = "b" * 40
HASH_C = "c" * 40
UNIQUE = "f" * 40

HOST_A = "http://host-a:8080"
HOST_B = "http://host-b:8080"
HOST_C = "http://host-c:8080"


def torrent(
    host: str,
    infohash: str,
    *,
    free_space: int | None = None,
    complete: bool = True,
    state: str = "uploading",
    size: int = 100,
    content_path: str | None = None,
    tracker: str = "http://tracker.example/announce",
    ratio: float = 1.0,
    seeding_time: int = 100,
    private: bool = True,
) -> dict:
    return {
        "host": host,
        "hash": infohash,
        "name": f"name-{infohash[:4]}",
        "size": size,
        "total_size": size,
        "progress": 1.0 if complete else 0.5,
        "amount_left": 0 if complete else size // 2,
        "state": state,
        "ratio": ratio,
        "seeding_time": seeding_time,
        "last_activity": 0,
        "save_path": f"/data/{host.rsplit(':', 1)[-1]}",
        "content_path": content_path or f"/data/{host.rsplit(':', 1)[-1]}/{infohash}",
        "category": "cat",
        "tags": "tag",
        "private": private,
        "has_metadata": True,
        "availability": 1.0,
        "tracker_domain": tracker_domain(tracker),
        "tracker_fingerprint": tracker_fingerprint(tracker),
        "tracker_count": 1,
        "infohash_v1": infohash,
        "infohash_v2": "",
    }


def meta(
    free_spaces: dict[str, int | None], errors: dict[str, str] | None = None
) -> dict:
    errors = errors or {}
    return {
        "type": "meta",
        "inventory_version": 1,
        "status": "finished",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:10:00+00:00",
        "hosts": [
            {
                "host": host,
                "version": "v5.3.0",
                "free_space": free,
                "torrent_count": 1,
                "error": errors.get(host),
            }
            for host, free in free_spaces.items()
        ],
    }


def row_for(rows, infohash: str, host: str) -> dict:
    matches = [row for row in rows if row["hash"] == infohash and row["host"] == host]
    assert len(matches) == 1, (infohash, host, matches)
    return matches[0]


class PlanTests(unittest.TestCase):
    def test_most_free_space_wins(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=100),
                torrent(HOST_B, HASH_A, free_space=500),
            ],
            meta({HOST_A: 100, HOST_B: 500}),
        )
        keeper = row_for(rows, HASH_A, HOST_B)
        donor = row_for(rows, HASH_A, HOST_A)
        self.assertEqual(keeper["action"], "keep")
        self.assertEqual(donor["action"], "delete")
        self.assertEqual(donor["keeper_host"], HOST_B)
        self.assertEqual(donor["delete_files"], "true")
        self.assertEqual(donor["expected_freed_bytes"], 100)
        self.assertEqual(stats["duplicate_groups"], 1)
        self.assertEqual(stats["approved_deletions"], 1)

    def test_three_copies_keep_one(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=100),
                torrent(HOST_B, HASH_A, free_space=900),
                torrent(HOST_C, HASH_A, free_space=400),
            ],
            meta({HOST_A: 100, HOST_B: 900, HOST_C: 400}),
        )
        self.assertEqual(row_for(rows, HASH_A, HOST_B)["action"], "keep")
        self.assertEqual(row_for(rows, HASH_A, HOST_A)["action"], "delete")
        self.assertEqual(row_for(rows, HASH_A, HOST_C)["action"], "delete")
        self.assertEqual(stats["approved_deletions"], 2)

    def test_single_copy_untouched(self):
        rows, stats = build_plan(
            [torrent(HOST_A, HASH_A, free_space=100)],
            meta({HOST_A: 100}),
        )
        self.assertEqual(rows, [])
        self.assertEqual(stats["single_copy_hashes"], 1)

    def test_no_complete_copy_skips(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, complete=False),
                torrent(HOST_B, HASH_A, complete=False),
            ],
            meta({HOST_A: 100, HOST_B: 500}),
        )
        self.assertEqual({row["action"] for row in rows}, {"skip"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats["no_complete_groups"], 1)
        self.assertEqual(stats["approved_deletions"], 0)

    def test_complete_beats_more_free_space(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=10_000, complete=False),
                torrent(HOST_B, HASH_A, free_space=1, complete=True),
            ],
            meta({HOST_A: 10_000, HOST_B: 1}),
        )
        self.assertEqual(row_for(rows, HASH_A, HOST_B)["action"], "keep")
        self.assertEqual(row_for(rows, HASH_A, HOST_A)["action"], "delete")

    def test_path_overlap_downgrades_to_entry_only(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=500, content_path="/data/A/Show"),
                torrent(HOST_B, HASH_A, free_space=100, content_path="/data/B/Show"),
                # A unique torrent on the donor host lives inside the donor folder.
                torrent(
                    HOST_B,
                    UNIQUE,
                    free_space=100,
                    content_path="/data/B/Show/episode1.mkv",
                ),
            ],
            meta({HOST_A: 500, HOST_B: 100}),
        )
        donor = row_for(rows, HASH_A, HOST_B)
        self.assertEqual(donor["action"], "delete")
        self.assertEqual(donor["delete_files"], "false")
        self.assertIn("path-overlap", donor["reason"])
        self.assertEqual(stats["path_overlap_rows"], 1)
        self.assertEqual(stats["bytes_to_free"], 0)

    def test_path_prefix_boundary_is_not_overlap(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=500, content_path="/data/A/Show"),
                torrent(HOST_B, HASH_A, free_space=100, content_path="/data/B/Show"),
                torrent(
                    HOST_B,
                    UNIQUE,
                    free_space=100,
                    content_path="/data/B/Show2/episode1.mkv",
                ),
            ],
            meta({HOST_A: 500, HOST_B: 100}),
        )
        donor = row_for(rows, HASH_A, HOST_B)
        self.assertEqual(donor["delete_files"], "true")
        self.assertEqual(stats["path_overlap_rows"], 0)

    def test_tracker_mismatch_requires_manual_approval(self):
        rows, stats = build_plan(
            [
                torrent(
                    HOST_A,
                    HASH_A,
                    free_space=500,
                    tracker="http://tracker-one.example/announce?passkey=1",
                ),
                torrent(
                    HOST_B,
                    HASH_A,
                    free_space=100,
                    tracker="http://tracker-two.example/announce?passkey=2",
                ),
            ],
            meta({HOST_A: 500, HOST_B: 100}),
        )
        donor = row_for(rows, HASH_A, HOST_B)
        self.assertEqual(donor["approved"], "no")
        self.assertIn("tracker-mismatch", donor["reason"])
        self.assertEqual(stats["tracker_mismatch_groups"], 1)
        self.assertEqual(stats["approved_deletions"], 0)

    def test_entry_only(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=500),
                torrent(HOST_B, HASH_A, free_space=100),
            ],
            meta({HOST_A: 500, HOST_B: 100}),
            entry_only=True,
        )
        donor = row_for(rows, HASH_A, HOST_B)
        self.assertEqual(donor["delete_files"], "false")
        self.assertIn("entry-only", donor["reason"])
        self.assertEqual(stats["bytes_to_free"], 0)

    def test_duplicate_record_on_same_host_is_ignored(self):
        rows, stats = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=500),
                torrent(HOST_A, HASH_A, free_space=500),
                torrent(HOST_B, HASH_A, free_space=100),
            ],
            meta({HOST_A: 500, HOST_B: 100}),
        )
        self.assertEqual(row_for(rows, HASH_A, HOST_A)["action"], "keep")
        self.assertEqual(row_for(rows, HASH_A, HOST_B)["action"], "delete")
        self.assertEqual(stats["duplicate_copies_on_same_host"], 1)

    def test_higher_ratio_breaks_free_space_tie(self):
        rows, _ = build_plan(
            [
                torrent(HOST_A, HASH_A, free_space=100, ratio=0.5),
                torrent(HOST_B, HASH_A, free_space=100, ratio=2.0),
            ],
            meta({HOST_A: 100, HOST_B: 100}),
        )
        self.assertEqual(row_for(rows, HASH_A, HOST_B)["action"], "keep")

    def test_cli_end_to_end(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inventory = tmp / "inventory.jsonl"
            plan = tmp / "plan.csv"
            summary = tmp / "summary.txt"

            records = [
                torrent(HOST_A, HASH_A, free_space=100),
                torrent(HOST_B, HASH_A, free_space=500),
                torrent(HOST_A, HASH_B, free_space=100),
            ]
            with inventory.open("w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
                fh.write(json.dumps(meta({HOST_A: 100, HOST_B: 500})) + "\n")

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT / "plan_dedup.py"),
                    "--inventory",
                    str(inventory),
                    "--out",
                    str(plan),
                    "--summary",
                    str(summary),
                ],
                cwd=PROJECT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            with plan.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {(row["hash"], row["host"], row["action"]) for row in rows},
                {
                    (HASH_A, HOST_A, "delete"),
                    (HASH_A, HOST_B, "keep"),
                },
            )
            self.assertIn("Confirmation count: 1", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
