"""
Dynamic selection of free OpenRouter models per task profile.

Free model availability on OpenRouter changes and individual endpoints go
temporarily unavailable or rate-limited. Hard-coded model lists therefore go
stale and break the bot. This module:

1. Always tries the user-configured OPENROUTER_MODEL first when set.
2. Maintains two curated task pools (judge / writer) that differ in fitness:
   strict, consistent rule-following for judge tasks; original, correct
   Russian prose for writer.
3. Evaluates live availability from the OpenRouter /models endpoint, then
   re-ranks the pools:

   - Judge: a battery of deterministic trials with known correct answers
     (mixed YES/NO polarity, a rephrased duplicate to catch inconsistency,
     and a domain-simulation rule-application case). Scoring rewards the
     number of correct verdicts, strict single-token format obedience, and
     consistency between rephrased trials.

   - Writer: a determinable sanity pre-filter (must be non-trivial Russian),
     then a single LLM-as-judge request that rates all candidate samples at
     once against a rubric (grammar, naturalness, originality). This keeps the
     request count small while making the ranking qualitative rather than a
     binary Cyrillic check.

4. Caches ranked results with a TTL so short-lived rate limits do not force
   re-probing on every cycle.

The system degrades gracefully to the hard-coded pool order whenever the
OpenRouter models endpoint, probes, or the judge reply are unavailable
(e.g. cold start with no network).
"""

import json
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_PATH = "/app/.model_pool_cache.json"
CACHE_TTL_SECONDS = 6 * 3600  # 6h
MAX_JUDGE_CANDIDATES = 2
MAX_WRITER_CANDIDATES = 3

# (prompt, expected_verdict). Trials 1 and 4 are rephrases of the same
# question; a reliable model must answer both identically.
JUDGE_TRIALS = [
    ("Reply with only YES or NO. Is 2+2 equal to 4?", "YES"),
    ("Reply with only YES or NO. Is 7 greater than 10?", "NO"),
    ("Reply with only YES or NO. Is Paris the capital of France?", "YES"),
    ("Reply with only YES or NO. Is 2+2 equal to four?", "YES"),
    (
        "Reply with only YES or NO. Given the rule 'reject if pay is below "
        "$10/h' and a vacancy offering $5/h, must the vacancy be rejected?",
        "YES",
    ),
]

WRITER_PROMPT = (
    "Напиши краткое сопроводительное письмо (2-3 предложения) на русском языке "
    "для вакансии помощника руководителя. Не используй шаблонные фразы ИИ."
)

RATER_PROMPT_TEMPLATE = """Оцени качество следующих сопроводительных писем на русском языке.

Для каждого письма поставь три оценки по шкале 1-5:
- correct: грамматика, орфография, согласование
- natural: естественность языка, отсутствие шаблонных ИИ-фраз
- original: уникальность и конкретность

Ответь строго одним JSON-объектом вида {{"sample_0": [correct, natural, original], "sample_1": [...]}}. Никакого другого текста.

{samples}"""


# Curated initial pools ordered by fitness and historical reliability. The
# runtime evaluation re-ranks them; these only define the default order used
# when probes or the API are unavailable.
POOLS: Dict[str, List[str]] = {
    "judge": [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
    "writer": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ],
}


def _normalize_token(reply: Optional[str]) -> Optional[str]:
    if not reply:
        return None
    token = re.sub(r"[^A-Za-zА-Яа-я]", "", reply).strip().upper()
    return token if token in {"YES", "NO", "ДА", "НЕТ"} else None


def _judge_score(replies: List[Optional[str]], latencies: List[float]) -> float:
    """Score a model against the judge battery.

    correct_verdict: exact expected token among the trials.
    format_obedience: the reply must be a single token with no commentary.
    consistency: rephrased trial 1 (index 0) and trial 4 (index 3) must match.
    """
    if not replies or len(replies) != len(JUDGE_TRIALS):
        return 0.0
    correct = 0
    obedient = 0
    for idx, (reply, (_, expected)) in enumerate(zip(replies, JUDGE_TRIALS)):
        token = _normalize_token(reply)
        if token == expected:
            correct += 1
        if token is not None:
            obedient += 1
        else:
            correct -= 1  # missed answer outweighs spurious text
    consistent = 1 if _normalize_token(replies[0]) == _normalize_token(replies[3]) else 0
    latency_penalty = min(sum(latencies) / max(len(latencies), 1) * 0.5, 15.0)
    base = 40.0 * (correct / len(JUDGE_TRIALS)) + 40.0 * (obedient / len(JUDGE_TRIALS))
    base += 20.0 * consistent
    return max(base - latency_penalty, 0.0)


def _writer_sane(reply: Optional[str]) -> Tuple[bool, str]:
    if not reply:
        return False, "empty"
    stripped = reply.strip()
    cyr = sum(1 for ch in stripped if "\u0400" <= ch <= "\u04FF")
    if cyr == 0:
        return False, "no-cyrillic"
    if len(stripped.split()) < 6:
        return False, "too-short"
    if any(ch in stripped for ch in "[]"):
        return False, "placeholders"
    return True, "ok"


def _parse_rater(samples: List[Tuple[int, str]], raw: Optional[str]) -> Dict[int, float]:
    """Parse the judge's JSON into per-sample total scores (best effort)."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (ValueError, json.JSONDecodeError):
        return {}
    totals: Dict[int, float] = {}
    for idx, text in samples:
        key = f"sample_{idx}"
        scores = data.get(key) or data.get(str(idx))
        if isinstance(scores, list) and len(scores) == 3:
            try:
                totals[idx] = float(scores[0]) + float(scores[1]) + float(scores[2])
            except (TypeError, ValueError):
                continue
    return totals


class ModelPool:
    """Thread-safe, TTL-cached ranked per-task model pool.

    All remote calls (models list fetch, one-shot completions, the writer
    rater) are injected as callables so the class is testable offline.
    """

    def __init__(
        self,
        probe: Optional[Callable[..., tuple[Optional[str], float]]] = None,
        rater: Optional[Callable[[str], Optional[str]]] = None,
        cache_path: str = CACHE_PATH,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._probe = probe
        self._rater = rater
        self._cache_path = cache_path
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._ranked: Dict[str, List[str]] = {}
        self._refreshed_at = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._ranked = data.get("ranked", {})
            self._refreshed_at = float(data.get("refreshed_at", 0.0))
        except (OSError, ValueError, json.JSONDecodeError):
            self._ranked = {}
            self._refreshed_at = 0.0

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "ranked": self._ranked,
                        "refreshed_at": self._refreshed_at,
                    },
                    fh,
                    ensure_ascii=False,
                )
        except OSError:
            # Best-effort; failing to write the cache must not break the bot.
            pass

    def _stale(self) -> bool:
        return time.time() - self._refreshed_at > self._ttl_seconds

    def refresh(
        self,
        http_get: Callable[[str], dict],
        force: bool = False,
    ) -> None:
        """Fetch the current free-model list and re-rank each pool.

        http_get must return the OpenRouter /models JSON payload.
        """
        if not force and not self._stale():
            return
        try:
            payload = http_get(OPENROUTER_MODELS_URL)
        except Exception:
            # Keep the current cache on transient API or network errors.
            return

        available: set[str] = {
            m.get("id")
            for m in payload.get("data", [])
            if m.get("id", "").endswith(":free")
        }
        available.discard(None)

        ranked: Dict[str, List[str]] = {}
        ranked["judge"] = self._rank_judges(POOLS["judge"], available)
        ranked["writer"] = self._rank_writers(POOLS["writer"], available)
        with self._lock:
            self._ranked = ranked
            self._refreshed_at = time.time()
        self._save_cache()

    def _rank_judges(
        self, pool: List[str], available: set
    ) -> List[str]:
        """Rank candidate judges by battery completion accuracy."""
        if self._probe is None:
            return list(pool)
        scored: List[tuple[float, int, str]] = []
        fallback_order = {m: i for i, m in enumerate(pool)}
        probed = 0
        for model in pool:
            if probed >= MAX_JUDGE_CANDIDATES:
                break
            if model not in available:
                continue
            replies: List[Optional[str]] = []
            latencies: List[float] = []
            failed = False
            for prompt, _expected in JUDGE_TRIALS:
                try:
                    reply, latency = self._probe(model, prompt, max_tokens=6)
                except Exception:
                    failed = True
                    break
                replies.append(reply)
                latencies.append(latency)
            if failed:
                continue
            score = _judge_score(replies, latencies)
            if score > 0.0:
                scored.append((score, fallback_order[model], model))
            probed += 1
        # Unprobed pool members keep their curated order as a fallback tail.
        probed_models = {m for _, _, m in scored}
        tail = [
            (0.0, fallback_order[m], m) for m in pool if m not in probed_models
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [m for _, _, m in scored + tail]

    def _rank_writers(
        self, pool: List[str], available: set
    ) -> List[str]:
        """Rank candidate writers: sanity filter, then one combined rater call."""
        if self._probe is None:
            return list(pool)
        samples: List[Tuple[int, str]] = []
        indexed: Dict[int, str] = {}
        for idx, model in enumerate(pool):
            if idx >= MAX_WRITER_CANDIDATES:
                break
            if model not in available:
                continue
            try:
                reply, _latency = self._probe(model, WRITER_PROMPT, max_tokens=90)
            except Exception:
                continue
            sane, _why = _writer_sane(reply)
            if not sane:
                continue
            samples.append((idx, reply or ""))
            indexed[idx] = model

        rater_totals: Dict[int, float] = {}
        if samples and self._rater is not None:
            blocks = "\n".join(
                f"sample_{idx}: {text}" for idx, text in samples
            )
            raw = self._rater(RATER_PROMPT_TEMPLATE.format(samples=blocks))
            rater_totals = _parse_rater(samples, raw)

        fallback_order = {m: i for i, m in enumerate(pool)}
        scored: List[tuple[float, int, str]] = []
        for idx, model in indexed.items():
            total = rater_totals.get(idx, 9.0)  # neutral default on parse failure
            scored.append((total, fallback_order[model], model))
        probed_models = {m for _, _, m in scored}
        tail = [
            (0.0, fallback_order[m], m) for m in pool if m not in probed_models
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [m for _, _, m in scored + tail]

    def get_ranked(self, task: str, override: Optional[str] = None) -> List[str]:
        """Ordered candidate list for a task, with optional override pinned first."""
        with self._lock:
            ranked = list(self._ranked.get(task, []))
        if not ranked:
            ranked = list(POOLS.get(task, []))
        if override:
            ranked = [override] + [m for m in ranked if m != override]
        return ranked


def default_override() -> Optional[str]:
    """OPENROUTER_MODEL env value if set, else None."""
    return os.environ.get("OPENROUTER_MODEL")