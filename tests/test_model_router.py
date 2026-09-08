"""Offline unit tests for the model router.

No network calls are made: the models API payload and probe completions are
injected/faked, which keeps the suite hermetic and fast.
"""

import tempfile
import unittest
from pathlib import Path

from model_router import POOLS, ModelPool, _score_reply


def _fake_payload(model_ids):
    return {"data": [{"id": m} for m in model_ids]}


class ScoreReplyTest(unittest.TestCase):
    def test_judge_exact(self):
        self.assertEqual(_score_reply("judge", "YES", 5.0), 95.0)

    def test_judge_wrong_token_gets_low_score(self):
        self.assertLess(_score_reply("judge", "maybe", 5.0), 30.0)

    def test_judge_empty_gets_zero(self):
        self.assertEqual(_score_reply("judge", None, 5.0), 0.0)

    def test_writer_cyrillic_sentence_scores_high(self):
        reply = "Этот проект помогает автоматизировать рутинные задачи полностью."
        self.assertGreaterEqual(_score_reply("writer", reply, 2.0), 80.0)

    def test_writer_ascii_only_scores_zero(self):
        self.assertEqual(_score_reply("writer", "hello world", 2.0), 0.0)


class ModelPoolTest(unittest.TestCase):
    def test_refresh_reranks_judge_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")

            def fake_probe(model, prompt):
                if "nano-omni-30b-a3b-reasoning" in model:
                    return "YES", 1.0
                if "gemma-4-31b" in model:
                    return "YES", 10.0
                return "I think it could be yes", 3.0

            pool = ModelPool(
                probe=fake_probe,
                cache_path=cache_path,
                ttl_seconds=3600,
            )
            available = {
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
            }
            pool.refresh(lambda url: _fake_payload(available), force=True)
            ranked = pool.get_ranked("judge")
            self.assertEqual(
                ranked[0], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
            )
            for m in POOLS["judge"]:
                self.assertIn(m, ranked)

    def test_override_pinned_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            pool = ModelPool(probe=None, cache_path=cache_path, ttl_seconds=3600)
            pool.refresh(lambda url: _fake_payload({"x:free"}), force=True)
            ranked = pool.get_ranked("judge", override="my-override:free")
            self.assertEqual(ranked[0], "my-override:free")

    def test_transient_probe_failure_demotes_not_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            calls = {"n": 0}

            def fake_probe(model, prompt):
                # Only the reasoning endpoint works; everything else raises.
                calls["n"] += 1
                if "nano-omni-30b-a3b-reasoning" in model:
                    return "YES", 1.0
                raise RuntimeError("rate limited")

            pool = ModelPool(
                probe=fake_probe,
                cache_path=cache_path,
                ttl_seconds=3600,
            )
            available = set(POOLS["judge"])
            pool.refresh(lambda url: _fake_payload(available), force=True)
            ranked = pool.get_ranked("judge")
            # The failing models must still be present (demoted), not removed.
            for m in POOLS["judge"]:
                self.assertIn(m, ranked)
            self.assertEqual(
                ranked[0], "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
            )

    def test_missing_from_live_list_demoted_to_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            pool = ModelPool(probe=None, cache_path=cache_path, ttl_seconds=3600)
            live = {"only-one-available:free"}
            pool.refresh(lambda url: _fake_payload(live), force=True)
            ranked = pool.get_ranked("judge")
            # Pool models absent from the live list are kept but pushed to tail.
            self.assertEqual(ranked[-1], "google/gemma-4-26b-a4b-it:free")

    def test_stale_ttl_triggers_refresh_and_ranks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            pool = ModelPool(
                probe=None,
                cache_path=cache_path,
                ttl_seconds=-1,
            )
            pool.refresh(
                lambda url: _fake_payload({"a:free", "b:free"}), force=False
            )
            self.assertNotEqual(pool.get_ranked("judge"), [])
            self.assertTrue(pool._ranked)


if __name__ == "__main__":
    unittest.main()