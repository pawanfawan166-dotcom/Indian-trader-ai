import datetime
import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import pandas as pd

from orchestrator import (
    FNO_SYMBOLS,
    _isolated_config,
    _fno_setup_candidates,
    _intraday_features,
    _merge_order_proposals,
    _numeric_volume,
    _replay_passes,
    _scanner_setup_candidates,
)

ROOT = Path(__file__).resolve().parent


def _load_skill_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXIT_SKILL = _load_skill_module("autopilot_exit_skill", "skills/trader-exit/exit.py")
BUDGET_SKILL = _load_skill_module("autopilot_budget_skill", "skills/trader-budget/size.py")
ENTRY_SKILL = _load_skill_module("autopilot_entry_skill", "skills/trader-entry/entry.py")


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

    def test_intraday_features_detect_long_and_short_breakouts(self):
        index = pd.date_range(
            "2026-11-25 09:15", periods=40, freq="5min", tz="Asia/Kolkata",
        )
        long_prices = [100 + i * 0.3 for i in range(40)]
        short_prices = [120 - i * 0.3 for i in range(40)]

        def bars(prices):
            return pd.DataFrame(
                {
                    "Open": prices,
                    "High": [price + 0.1 for price in prices],
                    "Low": [price - 0.1 for price in prices],
                    "Close": prices,
                    "Volume": [10000] * len(prices),
                },
                index=index,
            )

        long_features = _intraday_features(bars(long_prices), average_daily_volume=250000)
        short_features = _intraday_features(bars(short_prices), average_daily_volume=300000)

        self.assertEqual(long_features["direction"], "LONG")
        self.assertEqual(short_features["direction"], "SHORT")
        self.assertGreater(long_features["price"], long_features["vwap"])
        self.assertLess(short_features["price"], short_features["vwap"])
        self.assertIsNotNone(long_features["rsi"])
        self.assertIsNotNone(long_features["ema20"])
        self.assertGreater(long_features["volume"], 100000)

    def test_intraday_candidate_uses_mean_volume_fallback_and_filters_liquidity(self):
        index = pd.date_range(
            "2026-11-25 09:15", periods=40, freq="5min", tz="Asia/Kolkata",
        )
        prices = [100 + i * 0.3 for i in range(40)]
        frame = pd.DataFrame(
            {
                "Open": prices,
                "High": [price + 0.1 for price in prices],
                "Low": [price - 0.1 for price in prices],
                "Close": prices,
                "Volume": [10000] * len(prices),
            },
            index=index,
        )

        features = _intraday_features(frame)
        candidates = _fno_setup_candidates({"RELIANCE.NS": frame})
        self.assertEqual(features["average_daily_volume"], 400000)
        self.assertEqual([row["symbol"] for row in candidates], ["RELIANCE.NS"])

    def test_short_exit_and_sizing_keep_direction_and_one_percent_risk_cap(self):
        plan = EXIT_SKILL.compute(
            100, "or_high", None, "r", Decimal("2"), 105, 95, side="sell",
        )
        self.assertGreater(plan["stop"], plan["entry"])
        self.assertLess(plan["target"], plan["entry"])
        self.assertEqual(float(plan["reward_risk"]), 2.0)

        with tempfile.TemporaryDirectory() as temp_dir:
            config, cash = _isolated_config(Path(temp_dir), {"cash": 100000})
        short_plan = {
            "symbol": "SBIN.NS",
            "side": "sell",
            "entry": float(plan["entry"]),
            "stop": float(plan["stop"]),
            "target": float(plan["target"]),
            "reward_risk": float(plan["reward_risk"]),
        }
        sizing = BUDGET_SKILL.size_trade(
            short_plan, config, budget=cash, cash=cash,
        )
        order = BUDGET_SKILL.bracket_order(sizing)
        self.assertEqual(order["side"], "sell")
        self.assertLessEqual(sizing["max_loss"], cash * 0.01)

    def test_entry_skill_triggers_short_opening_range_breakdown(self):
        index = pd.date_range(
            "2026-11-25 09:15", periods=40, freq="5min", tz="Asia/Kolkata",
        )
        prices = [120 - i * 0.3 for i in range(len(index))]
        bars = [
            {
                "t": int(timestamp.timestamp()),
                "o": price,
                "h": price + 0.1,
                "l": price - 0.1,
                "c": price,
                "v": 10000,
            }
            for timestamp, price in zip(index, prices)
        ]
        chart = {
            "symbol": "SBIN.NS",
            "bars": bars,
            "data_time": bars[-1]["t"] + 300,
        }
        now = datetime.datetime(2026, 11, 25, 7, 6, tzinfo=datetime.timezone.utc)

        result = ENTRY_SKILL.evaluate(chart, now, side="sell")

        self.assertEqual(result["side"], "sell")
        self.assertEqual(result["status"], "triggered")
        self.assertLess(result["trigger"]["price"], result["opening_range"]["low"])


if __name__ == "__main__":
    unittest.main()
