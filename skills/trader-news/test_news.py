import unittest

import news


class TickerAndNewsMatchingTests(unittest.TestCase):
    def test_nse_ticker_removes_spaces_and_duplicate_suffixes(self):
        for value in ("MCX. NS", "mcx ", "MCX.NS.NS"):
            with self.subTest(value=value):
                self.assertEqual(news.nse_ticker(value), "MCX.NS")

    def test_headline_must_mention_exact_ticker_or_company_name(self):
        self.assertTrue(news.relevant_to_ticker(
            {"title": "MCX shares rise after results", "summary": ""},
            "MCX.NS",
            "Multi Commodity Exchange of India Limited",
        ))
        self.assertTrue(news.relevant_to_ticker(
            {"title": "Multi Commodity Exchange of India posts results", "summary": ""},
            "MCX.NS",
            "Multi Commodity Exchange of India Limited",
        ))
        self.assertFalse(news.relevant_to_ticker(
            {"title": "Indian commodities market sees higher volumes", "summary": ""},
            "MCX.NS",
            "Multi Commodity Exchange of India Limited",
        ))

    def test_short_ticker_requires_exchange_or_parenthesis_context(self):
        self.assertFalse(news.relevant_to_ticker(
            {"title": "IT sector outlook remains positive", "summary": ""},
            "IT.NS",
        ))
        self.assertTrue(news.relevant_to_ticker(
            {"title": "NSE: IT reports quarterly results", "summary": ""},
            "IT.NS",
        ))


if __name__ == "__main__":
    unittest.main()
