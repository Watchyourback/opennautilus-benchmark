"""Offline checks for scoring and price resolution; no network, no real stdin."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import score  # noqa: E402

RATES = {"USD": (1.0, "-"), "EUR": (1.1, "2026-10-09"), "CNY": (0.15, "2026-10-09")}


def fake_rate(currency):
    if currency not in RATES:
        raise score.ScoreError("offline")
    return RATES[currency]


def make_report(single=1.0, multi=1.0, steal=0.05, spread=0.0):
    def phase(factor, with_ms):
        out = {}
        for s in score.SCENARIOS:
            eps = 100_000 * factor
            run = {"events_per_second": eps}
            if with_ms:
                run["samples_ms"] = [1000 * 20_000 / eps] * 3
            else:
                run["samples_events_per_second"] = [eps * (1 - spread), eps, eps * (1 + spread)]
            out[s] = {"run_only": run}
        return out

    return {
        "benchmark_version": 3,
        "source_commit": "abc",
        "single_core_results": phase(single, True),
        "all_core_results": phase(multi, False),
        "single_core_resources": {"system_cpu_steal_percent": steal, "cpu_percent_of_one_core": 99.7},
        "all_core_resources": {"system_cpu_steal_percent": steal, "cpu_percent_of_one_core": 370.0},
    }


BASELINE = {
    "benchmark_version": 3,
    "source_commit": "abc",
    "ref_monthly_price": 4.0,
    "events_per_second": {p: {s: 100_000 for s in score.SCENARIOS} for p in score.PHASES},
}


class ScoreTests(unittest.TestCase):
    def test_reference_machine_scores_100(self):
        scores = score.compute_scores(make_report(), BASELINE, 4.0)
        self.assertAlmostEqual(scores["single"], 100)
        self.assertAlmostEqual(scores["multi"], 100)
        self.assertAlmostEqual(scores["value"], 100)

    def test_geometric_mean_and_value(self):
        report = make_report(single=2.0, multi=4.0)
        report["all_core_results"]["replay_only"]["run_only"]["events_per_second"] = 100_000 * 16
        scores = score.compute_scores(report, BASELINE, 8.0)
        self.assertAlmostEqual(scores["single"], 200)
        self.assertAlmostEqual(scores["multi"], 100 * math.exp((math.log(16) + 3 * math.log(4)) / 4))
        self.assertAlmostEqual(scores["value"], scores["multi"] * 4.0 / 8.0)

    def test_no_price_means_no_value(self):
        self.assertIsNone(score.compute_scores(make_report(), BASELINE)["value"])

    def test_commit_or_version_mismatch_refused(self):
        with self.assertRaises(score.ScoreError):
            score.compute_scores(make_report(), {**BASELINE, "source_commit": "other"})
        with self.assertRaises(score.ScoreError):
            score.compute_scores(make_report(), {**BASELINE, "benchmark_version": 4})

    def test_quality_flags(self):
        self.assertEqual(score.quality_flags(make_report()), [])
        flags = score.quality_flags(make_report(steal=2.0, spread=0.3))
        self.assertTrue(any("steal" in f for f in flags))
        self.assertTrue(any("极差" in f for f in flags))

    def test_score_report_skips_instead_of_raising(self):
        scores, text = score.score_report({"benchmark_version": 999, "source_commit": "abc"}, 4.0)
        self.assertIsNone(scores)
        self.assertIn("已跳过打分", text)

    def test_report_json_leads_with_three_scores(self):
        report = make_report()
        report["scores"] = score.compute_scores(report, BASELINE, 4.0)
        report["zebra"] = 1
        report["alpha"] = 1
        text = score.dumps_report(report)
        head = text.split('"ratios"', 1)[0]
        self.assertTrue(text.startswith('{\n  "scores": {\n    "single":'))
        self.assertLess(head.index('"single"'), head.index('"multi"'))
        self.assertLess(head.index('"multi"'), head.index('"value"'))
        self.assertLess(text.index('"value"'), text.index('"alpha"'))
        self.assertLess(text.index('"alpha"'), text.index('"zebra"'))
        self.assertEqual(json.loads(text)["scores"]["single"], report["scores"]["single"])

    def test_format_mentions_missing_price(self):
        text = score.format_scores(score.compute_scores(make_report(), BASELINE), price_note="(缺价格)")
        self.assertIn("(缺价格)", text)
        self.assertIn("单核", text)


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = Path(self.temp.name) / "local.json"

    def resolve(self, env=None, answers=None, rate=fake_rate):
        prompts = []
        it = iter(answers or [])

        def ask(prompt):
            prompts.append(prompt)
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        price, note = score.resolve_monthly_price(
            env or {}, self.record, ask=ask if answers is not None else None, fetch_rate=rate
        )
        return price, note, prompts

    def test_env_monthly_and_yearly(self):
        self.assertEqual(self.resolve({"BENCHMARK_MONTHLY_PRICE": "4.2"})[0], 4.2)
        self.assertAlmostEqual(self.resolve({"BENCHMARK_YEARLY_PRICE": "50.4"})[0], 4.2)

    def test_env_currency_converted(self):
        env = {"BENCHMARK_YEARLY_PRICE": "120", "BENCHMARK_PRICE_CURRENCY": "rmb"}
        self.assertAlmostEqual(self.resolve(env)[0], 120 * 0.15 / 12)

    def test_env_invalid_or_conflicting_skips(self):
        self.assertIsNone(self.resolve({"BENCHMARK_MONTHLY_PRICE": "abc"})[0])
        self.assertIsNone(self.resolve({"BENCHMARK_MONTHLY_PRICE": "-1"})[0])
        self.assertIsNone(self.resolve({"BENCHMARK_MONTHLY_PRICE": "1", "BENCHMARK_YEARLY_PRICE": "12"})[0])
        self.assertIsNone(self.resolve({"BENCHMARK_MONTHLY_PRICE": "1", "BENCHMARK_PRICE_CURRENCY": "XXX"})[0])

    def test_non_interactive_without_record_skips(self):
        price, note, _ = self.resolve()
        self.assertIsNone(price)
        self.assertIn("BENCHMARK_MONTHLY_PRICE", note)
        self.assertFalse(self.record.exists())

    def test_interactive_yearly_euro_saved_and_reused(self):
        price, note, prompts = self.resolve(answers=["y", "EUR", "60"])
        self.assertAlmostEqual(price, 60 * 1.1 / 12)
        self.assertEqual(len(prompts), 3)
        saved = json.loads(self.record.read_text())
        self.assertEqual(saved["input"]["currency"], "EUR")
        self.assertNotIn("EUR", json.dumps({k: v for k, v in saved.items() if k != "input"}))
        again, _, prompts = self.resolve(answers=[""])
        self.assertAlmostEqual(again, price)
        self.assertEqual(len(prompts), 1)
        self.assertIn("60", prompts[0])
        self.assertIn("EUR/年", prompts[0])
        self.assertIn("重新输入", prompts[0])
        silent, _, silent_prompts = self.resolve()
        self.assertAlmostEqual(silent, price)
        self.assertEqual(silent_prompts, [])

    def test_interactive_defaults_are_monthly_usd(self):
        price, _, _ = self.resolve(answers=["", "", "4.2"])
        self.assertEqual(price, 4.2)

    def test_rmb_alias_and_bad_input_retries(self):
        price, _, _ = self.resolve(answers=["x", "m", "??", "RMB", "abc", "-3", "100"])
        self.assertAlmostEqual(price, 15.0)

    def test_blank_amount_skips_without_saving(self):
        price, note, _ = self.resolve(answers=["m", "USD", ""])
        self.assertIsNone(price)
        self.assertFalse(self.record.exists())

    def test_eof_skips(self):
        self.assertIsNone(self.resolve(answers=[])[0])

    def test_rate_failure_falls_back_to_usd_amount(self):
        price, _, prompts = self.resolve(answers=["y", "JPY", "10000", "50.4"])
        self.assertAlmostEqual(price, 4.2)
        self.assertIn("USD", prompts[-1])

    def test_stale_monthly_is_recomputed_from_yearly_input(self):
        self.record.write_text(json.dumps({
            "fingerprint": score.machine_fingerprint(),
            "monthly_price_usd": 0.45,
            "input": {"amount": 50.4, "currency": "USD", "period": "y", "rate": 1.0, "rate_date": "-"},
        }))
        price, _, prompts = self.resolve(answers=[""])
        self.assertAlmostEqual(price, 4.2)
        self.assertEqual(len(prompts), 1)
        self.assertIn("50.4 USD/年", prompts[0])
        self.assertIn("4.2 美元/月", prompts[0])

    def test_confirm_yes_replaces_saved_price(self):
        self.resolve(answers=["m", "USD", "4.2"])
        price, _, prompts = self.resolve(answers=["y", "y", "USD", "50.4"])
        self.assertAlmostEqual(price, 4.2)
        self.assertIn("重新输入", prompts[0])
        saved = json.loads(self.record.read_text())
        self.assertEqual(saved["input"]["amount"], 50.4)
        self.assertEqual(saved["input"]["period"], "y")

    def test_reentry_blank_amount_keeps_saved_price(self):
        self.resolve(answers=["m", "USD", "4.2"])
        price, _, _ = self.resolve(answers=["y", "m", "USD", ""])
        self.assertEqual(price, 4.2)

    def test_other_machine_record_is_ignored(self):
        self.record.write_text(json.dumps({"fingerprint": "someone-else", "monthly_price_usd": 99.0}))
        price, _, prompts = self.resolve(answers=["m", "USD", "5"])
        self.assertEqual(price, 5.0)
        self.assertEqual(len(prompts), 3)

    def test_corrupt_record_is_ignored(self):
        self.record.write_text("{not json")
        self.assertIsNone(self.resolve()[0])

    def test_rate_request_sets_user_agent(self):
        import io
        from unittest import mock

        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            return io.BytesIO(b'{"date": "2026-10-09", "rates": {"USD": 1.5}}')

        with mock.patch.object(score.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(score.fetch_usd_rate("EUR"), (1.5, "2026-10-09"))
        self.assertIn("EUR", seen[0].full_url)
        self.assertNotIn("Python-urllib", seen[0].get_header("User-agent"))

    def test_currency_normalisation(self):
        self.assertEqual(score.normalize_currency("rmb"), "CNY")
        self.assertEqual(score.normalize_currency("€"), "EUR")
        with self.assertRaises(score.ScoreError):
            score.normalize_currency("dollars")


if __name__ == "__main__":
    unittest.main()
