import datetime
import unittest

import scan


class FullWatchlistTests(unittest.TestCase):
    def test_daily_rows_include_fetched_stocks_without_setup_hits(self):
        start = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        timestamps = [
            int((start + datetime.timedelta(days=index)).timestamp())
            for index in range(35)
        ]
        series = {
            "t": timestamps,
            "o": [100.0] * len(timestamps),
            "h": [101.0] * len(timestamps),
            "l": [99.0] * len(timestamps),
            "c": [100.0] * len(timestamps),
            "v": [150000] * len(timestamps),
            "meta": {"regularMarketPrice": 100.0, "regularMarketVolume": 150000},
        }

        rows = scan.daily_universe_rows(
            {"MCX.NS": series},
            ["MCX.NS"],
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            5,
            1000,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "MCX.NS")
        self.assertEqual(rows[0]["setups"], [])
        self.assertEqual(rows[0]["average_daily_volume"], 150000)
        self.assertTrue(rows[0]["liquid"])

    def test_daily_rows_populate_all_metrics_with_short_volume_history(self):
        start = datetime.datetime(2026, 1, 1, 4, tzinfo=datetime.timezone.utc)
        timestamps = [
            int((start + datetime.timedelta(days=index)).timestamp())
            for index in range(5)
        ]
        series = {
            "t": timestamps,
            "o": [100.0 + index for index in range(5)],
            "h": [101.0 + index for index in range(5)],
            "l": [99.0 + index for index in range(5)],
            "c": [100.0 + index for index in range(5)],
            "v": [100000, 200000, 300000, 400000, 500000],
            "meta": {"regularMarketPrice": 105.0},
        }

        rows = scan.daily_universe_rows(
            {"MCX.NS": series},
            ["MCX.NS"],
            datetime.datetime(2026, 1, 31, tzinfo=datetime.timezone.utc),
            0,
            0,
        )

        row = rows[0]
        self.assertEqual(row["volume"], 500000)
        self.assertEqual(row["average_daily_volume"], 300000)
        self.assertTrue(row["liquid"])
        self.assertIsNotNone(row["rsi"])
        self.assertIsNotNone(row["close_date"])

    def test_orb_rows_use_daily_average_and_intraday_rsi_and_volume(self):
        day = datetime.date(2026, 11, 25)
        cutoff = datetime.datetime.combine(day, datetime.time(9, 35))
        intraday_start = datetime.datetime.combine(
            day, datetime.time(3, 45), tzinfo=datetime.timezone.utc
        )
        intraday_times = [
            int((intraday_start + datetime.timedelta(minutes=index * 5)).timestamp())
            for index in range(4)
        ]
        intraday = {
            "t": intraday_times,
            "o": [100, 101, 102, 103],
            "h": [101, 102, 103, 104],
            "l": [99, 100, 101, 102],
            "c": [100, 101, 102, 103],
            "v": [1000, 2000, 3000, 4000],
        }
        daily_start = datetime.datetime(2026, 11, 20, 4, tzinfo=datetime.timezone.utc)
        daily = {
            "t": [
                int((daily_start + datetime.timedelta(days=index)).timestamp())
                for index in range(3)
            ],
            "c": [100, 101, 102],
            "v": [100000, 200000, 300000],
        }

        rows = scan.orb_universe_rows(
            {"MCX.NS": intraday},
            ["MCX.NS"],
            day,
            cutoff,
            0,
            0,
            [],
            {"MCX.NS": daily},
        )

        row = rows[0]
        self.assertEqual(row["volume"], 10000)
        self.assertEqual(row["average_daily_volume"], 200000)
        self.assertTrue(row["liquid"])
        self.assertIsNotNone(row["rsi"])
        self.assertEqual(row["close_date"], day.isoformat())


if __name__ == "__main__":
    unittest.main()
