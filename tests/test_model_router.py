"""Offline unit tests for the model router.

No network calls are made: the models API payload, probe completions, and the
writer rater are injected/faked, which keeps the suite hermetic and fast.
"""

import tempfile
import unittest
from pathlib import Path

from model_router import (
    POOLS,
    ModelPool,
    _judge_score,
    _normalize_token,
    _parse_rater,
    _writer_sane,
)


def _fake_payload(model_ids):
    return {"data": [{"id": m} for m in model_ids]}


class NormalizeTokenTest(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(_normalize_token("YES"), "YES")

    def test_with_punctuation(self):
        self.assertEqual(_normalize_token("  no."), "NO")

    def test_verbose_reply_is_none(self):
        self.assertIsNone(_normalize_token("I think it could be yes"))

    def test_cyrillic_verdicts(self):
        self.assertEqual(_normalize_token("да"), "ДА")
        self.assertEqual(_normalize_token("нет"), "НЕТ")


class JudgeScoreTest(unittest.TestCase):
    def test_perfect_battery(self):
        replies = ["YES", "NO", "YES", "YES", "YES"]
        lat = [1.0, 1.0, 1.0, 1.0, 1.0]
        score = _judge_score(replies, lat)
        self.assertGreaterEqual(score, 90.0)

    def test_wrong_verdicts_reduce_score(self):
        perfect = ["YES", "NO", "YES", "YES", "YES"]
        wrong = ["NO", "YES", "NO", "NO", "NO"]
        lat = [1.0] * 5
        self.assertGreater(_judge_score(perfect, lat), _judge_score(wrong, lat))

    def test_rambling_replies_are_penalized(self):
        replies = ["YES", "NO", "YES", "YES", "sure, that sounds right"]
        lat = [1.0] * 5
        self.assertLess(_judge_score(replies, lat), _judge_score(["YES", "NO", "YES", "YES", "YES"], lat))

    def test_inconsistent_rephrased_trial_penalized(self):
        # trials 0 and 3 are rephrases; inconsistent answers must score lower.
        consistent = ["YES", "NO", "YES", "YES", "YES"]
        inconsistent = ["YES", "NO", "YES", "NO", "YES"]
        lat = [1.0] * 5
        self.assertGreater(_judge_score(consistent, lat), _judge_score(inconsistent, lat))


class WriterSanityTest(unittest.TestCase):
    def test_russian_pass(self):
        text = "Я хочу помогать с организацией процессов и автоматизацией рутины."
        ok, why = _writer_sane(text)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_ascii_fail(self):
        ok, why = _writer_sane("hello world and more words here")
        self.assertFalse(ok)
        self.assertEqual(why, "no-cyrillic")

    def test_placeholder_fail(self):
        ok, why = _writer_sane("Привет [Имя], напишите мне [Компания] пожалуйста")
        self.assertFalse(ok)
        self.assertEqual(why, "placeholders")

    def test_short_fail(self):
        ok, why = _writer_sane("Привет мир")
        self.assertFalse(ok)
        self.assertEqual(why, "too-short")


class RaterParseTest(unittest.TestCase):
    def test_parse_valid_json(self):
        raw = '{"sample_0": [5, 4, 4], "sample_1": [3, 2, 2]}'
        totals = _parse_rater([(0, "a"), (1, "b")], raw)
        self.assertEqual(totals[0], 13.0)
        self.assertEqual(totals[1], 7.0)

    def test_parse_noisy_json(self):
        raw = 'Sure! Here you go:\n{"sample_0": [5, 5, 5]} trailing'
        totals = _parse_rater([(0, "a")], raw)
        self.assertEqual(totals[0], 15.0)

    def test_parse_garbage(self):
        self.assertEqual(_parse_rater([(0, "a")], "Ce n'est pas du JSON"), {})


class ModelPoolTest(unittest.TestCase):
    def test_judge_rerank_prefers_consistent_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")

            def fake_probe(model, prompt, max_tokens=8):
                # model A answers all five trials correctly with strict tokens;
                # every other model rambles.
                if "nano-omni-30b-a3b-reasoning" in model:
                    expected = {"YES": ["Is 2+2 equal to 4?", "Is Paris the capital of France?", "Is 2+2 equal to four?", "$5/h"], "NO": ["Is 7 greater than 10?"]}
                    token = "NO" if "Is 7 greater than 10?" in prompt else "YES"
                    return token, 1.0
                return "I think this seems right", 2.0

            pool = ModelPool(probe=fake_probe, cache_path=cache_path, ttl_seconds=3600)
            available = set(POOLS["judge"])
            pool.refresh(lambda url: _fake_payload(available), force=True)
            ranked = pool.get_ranked("judge")
            for m in POOLS["judge"]:
                self.assertIn(m, ranked)

    def test_override_pinned_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            pool = ModelPool(probe=None, rater=None, cache_path=cache_path, ttl_seconds=3600)
            pool.refresh(lambda url: _fake_payload({"x:free"}), force=True)
            ranked = pool.get_ranked("judge", override="my-override:free")
            self.assertEqual(ranked[0], "my-override:free")

    def test_stale_ttl_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            pool = ModelPool(probe=None, rater=None, cache_path=cache_path, ttl_seconds=-1)
            pool.refresh(lambda url: _fake_payload({"a:free", "b:free"}), force=False)
            self.assertNotEqual(pool.get_ranked("judge"), [])

    def test_writer_uses_rater(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")

            def fake_probe(model, prompt, max_tokens=8):
                if "ultra" in model:
                    return "Это письмо написано естественно по русски." * 2, 1.0
                return "Это письмо написано естественно и красиво." * 2, 1.0

            def fake_rater(prompt):
                return '{"sample_0": [5,5,5], "sample_1": [2,2,2]}'

            pool = ModelPool(probe=fake_probe, rater=fake_rater, cache_path=cache_path, ttl_seconds=3600)
            pool.refresh(lambda url: _fake_payload(set(POOLS["writer"])), force=True)
            ranked = pool.get_ranked("writer")
            self.assertIn(ranked[0], POOLS["writer"])


if __name__ == "__main__":
    unittest.main()