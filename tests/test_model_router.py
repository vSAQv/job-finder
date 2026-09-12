"""Offline unit tests for the lean model pool.

The pool makes no network calls: pools are static and demotion is recorded
in-memory. Tests cover token normalization, the writer sanity gate, override
pinning, and passive demotion.
"""

import unittest
import time

from model_router import POOLS, ModelPool, _normalize_token, _writer_sane


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

    def test_empty_is_none(self):
        self.assertIsNone(_normalize_token(None))
        self.assertIsNone(_normalize_token(""))


class WriterSanityTest(unittest.TestCase):
    def test_russian_pass(self):
        text = "Я хочу помогать с организацией процессов и автоматизацией рутины."
        self.assertTrue(_writer_sane(text))

    def test_ascii_fail(self):
        self.assertFalse(_writer_sane("hello world and more words here"))

    def test_placeholder_fail(self):
        self.assertFalse(_writer_sane("Привет [Имя], напишите мне [Компания] пожалуйста"))

    def test_short_fail(self):
        self.assertFalse(_writer_sane("Привет мир"))

    def test_empty_fail(self):
        self.assertFalse(_writer_sane(None))


class ModelPoolTest(unittest.TestCase):
    def test_pools_exist_and_are_static(self):
        for task in ("judge", "writer"):
            self.assertIn(task, POOLS)
            self.assertGreater(len(POOLS[task]), 0)

    def test_get_ranked_no_network(self):
        pool = ModelPool(demotion_seconds=3600)
        ranked = pool.get_ranked("judge")
        self.assertEqual(ranked, POOLS["judge"])

    def test_override_pinned_first(self):
        pool = ModelPool()
        ranked = pool.get_ranked("writer", override="my-override:free")
        self.assertEqual(ranked[0], "my-override:free")

    def test_demotion_moves_failed_model_to_tail(self):
        pool = ModelPool(demotion_seconds=3600)
        failing = POOLS["judge"][0]
        pool.mark_failure("judge", failing)
        ranked = pool.get_ranked("judge")
        self.assertEqual(ranked[-1], failing)

    def test_demotion_expires_after_cooldown(self):
        pool = ModelPool(demotion_seconds=0)  # instant expiry
        failing = POOLS["writer"][0]
        pool.mark_failure("writer", failing)
        ranked = pool.get_ranked("writer")
        self.assertEqual(ranked[0], failing)  # back at the top

    def test_demotion_ignores_unknown_model(self):
        pool = ModelPool()
        pool.mark_failure("judge", "not/in-any-pool:free")
        self.assertEqual(pool.get_ranked("judge"), POOLS["judge"])


if __name__ == "__main__":
    unittest.main()