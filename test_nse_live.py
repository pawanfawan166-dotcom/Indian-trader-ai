import unittest
from unittest.mock import patch

import nse_live


class NseLiveQuoteTests(unittest.TestCase):
    def test_normalize_ticker_removes_spaces_and_duplicate_suffixes(self):
        for value in ("MCX. NS", "mcx ", "MCX.NS.NS"):
            with self.subTest(value=value):
                self.assertEqual(nse_live.normalize_ticker(value), "MCX")

    def test_parse_nse_quote_extracts_live_fields(self):
        quote = nse_live.parse_nse_quote(
            {
                "priceInfo": {
                    "lastPrice": 123.45,
                    "pChange": 2.5,
                    "intraDayHighLow": {"max": 126, "min": 120},
                },
                "marketDeptOrderBook": {
                    "tradeInfo": {"totalTradedVolume": "4567"},
                },
            },
            "MCX",
        )
        self.assertEqual(
            (quote["price"], quote["day_high"], quote["day_low"], quote["change_pct"], quote["volume"]),
            (123.45, 126.0, 120.0, 2.5, 4567),
        )
        self.assertEqual(quote["source"], "Live: NSE")

    def test_live_quote_uses_yahoo_if_nse_has_no_quote(self):
        yahoo = {
            "symbol": "MCX",
            "price": 123.45,
            "source": "Fallback: Yahoo",
        }
        with (
            patch.object(nse_live, "get_nse_quote", return_value=None) as nse,
            patch.object(nse_live, "get_yahoo_quote", return_value=yahoo) as fallback,
        ):
            self.assertEqual(nse_live.get_live_quote("MCX.NS"), yahoo)
        nse.assert_called_once_with("MCX.NS", timeout=6)
        fallback.assert_called_once_with("MCX.NS")


if __name__ == "__main__":
    unittest.main()
