import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import (
    FNO_SYMBOLS,
    _isolated_config,
    _merge_order_proposals,
    _numeric_volume,
    _replay_passes,
    _scanner_setup_candidates,
)


class ScannerLiquidityTests(unittest.TestCase):
    def test_replay_gate_accepts_profitable_candidate_at_forty_percent(self):
        self.assertTrue(_replay_passes({"p_green": 0.40, "avg_R": 0.01}))

    def test_replay_gate_rejects_below_threshold_or_nonpositive_ev(self):
        self.assertFalse(_replay_passes({"p_green": 0.399, "avg_R": 0.2}))
        self.assertFalse(_replay_passes({"p_green": 0.5, "avg_R": 0}))
        self.assertFalse(_replay_passes({"p_green": float("nan"), "avg_R": 0.2}))

    def test_fno_universe_is_bounded_and_uses_nse_symbols(self):
        self.assertLessEqual(len(FNO_SYMBOLS), 50)
        self.assertGreater(len(FNO_SYMBOLS), 0)
        self.assertTrue(all(symbol.endswith(".NS") for symbol in FNO_SYMBOLS))

    def test_isolated_config_aligns_budget_to_risk_caps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _ = _isolated_config(Path(temp_dir), {"cash": 100000})

        self.assertEqual(config["max_order_inr"], 4000)
        self.assertEqual(config["max_daily_loss_inr"], 150)
        self.assertEqual(config["max_loss_per_trade_inr"], 150)
        self.assertTrue(config["paper"])

    def test_merge_replaces_matching_proposals_and_keeps_unrelated_orders(self):
        existing = [
            {"symbol": "RELIANCE.NS", "side": "buy", "qty": "2"},
            {"symbol": "AXISBANK.NS", "side": "buy", "qty": "1"},
        ]
        proposals = [{"symbol": "AXISBANK.NS", "side": "buy", "qty": "3"}]

        merged = _merge_order_proposals(existing, proposals)

        self.assertEqual(merged, [existing[0], proposals[0]])

    def test_numeric_volume_handles_commas_and_nan(self):
        self.assertEqual(_numeric_volume("NaN", "1,234,567"), 1234567.0)
        self.assertIsNone(_numeric_volume("NaN", float("nan"), None))

    def test_candidates_join_stock_rows_and_fallback_average_volume(self):
        scan_doc = {
            "hits": [
                {"symbol": "RELIANCE.NS", "price": "1,300", "setups": ["breakout"], "volume": None},
                {"symbol": "HDFCBANK.NS", "price": "850", "setups": ["breakout"], "volume": "95,000"},
                {"symbol": "SBIN.NS", "price": "700", "setups": ["breakout"], "volume": "NaN"},
                {"symbol": "ICICIBANK.NS", "price": "1,000", "setups": ["breakout"], "volume": 0},
            ],
            "stocks": [
                {
                    "symbol": "RELIANCE.NS",
                    "average_daily_volume": "1,200,000",
                    "volume": "1,100,000",
                    "liquid": True,
                },
                {
                    "symbol": "HDFCBANK.NS",
                    "average_daily_volume": None,
                    "volume": "250,000",
                    "liquid": False,
                },
                {
                    "symbol": "SBIN.NS",
                    "average_daily_volume": "NaN",
                    "volume": "80,000",
                    "liquid": None,
                },
                {
                    "symbol": "ICICIBANK.NS",
                    "average_daily_volume": "NaN",
                    "volume": "90,000",
                    "liquid": False,
                },
            ],
        }

        candidates = _scanner_setup_candidates(
            scan_doc,
            average_volumes={"SBIN.NS": 300000, "ICICIBANK.NS": 99000},
        )

        self.assertEqual(
            [row["symbol"] for row in candidates],
            ["RELIANCE.NS", "SBIN.NS"],
        )
        self.assertEqual(
            [row["_liquidity_volume"] for row in candidates],
            [1200000.0, 300000.0],
        )
        self.assertTrue(all(row["liquid"] for row in candidates))

    def test_candidate_requires_average_daily_volume_not_current_volume(self):
        rows = _scanner_setup_candidates({
            "hits": [
                {"symbol": "SBIN.NS", "price": 700, "volume": 500000},
            ],
            "stocks": [
                {
                    "symbol": "SBIN.NS",
                    "average_daily_volume": None,
                    "volume": 500000,
                    "liquid": None,
                },
            ],
        })
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
