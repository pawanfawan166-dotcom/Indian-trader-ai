import os
import unittest
from unittest.mock import patch

import facts


class NSEFactsTests(unittest.TestCase):
    def test_xbrl_parser_reads_rupees_and_ignores_dimensions(self):
        xml = """<?xml version="1.0"?>
<xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
  <xbrli:context id="quarter">
    <xbrli:entity><xbrli:identifier>INE000000000</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="segment">
    <xbrli:entity><xbrli:identifier>INE000000000</xbrli:identifier>
      <xbrli:segment><xbrldi:explicitMember dimension="x:Segment">Retail</xbrldi:explicitMember>
      </xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
  <xbrli:unit id="INRPerShare"><xbrli:divide>
    <xbrli:unitNumerator><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
  </xbrli:divide></xbrli:unit>
  <RevenueFromOperations contextRef="quarter" unitRef="INR">1000</RevenueFromOperations>
  <RevenueFromOperations contextRef="segment" unitRef="INR">200</RevenueFromOperations>
  <DilutedEarningsLossPerShareFromContinuingOperations contextRef="quarter"
      unitRef="INRPerShare">2.5</DilutedEarningsLossPerShareFromContinuingOperations>
</xbrl>
"""
        filing = {
            "url": "https://nsearchives.nseindia.in/sample.xml",
            "filed": "2025-07-01", "end": "2025-06-30",
            "relating_to": "First Quarter",
            "financial_year": "01-Apr-2025 To 31-Mar-2026",
        }

        parsed = facts.parse_nse_xbrl(xml, filing)

        self.assertEqual(len(parsed["RevenueFromOperations"]), 1)
        self.assertEqual(parsed["RevenueFromOperations"][0]["val"], 1000)
        self.assertEqual(parsed["DilutedEarningsLossPerShareFromContinuingOperations"][0]["unit"],
                         "INRPerShare")

    def test_q4_uses_annual_less_nine_month_cumulative(self):
        def fact(start, end, value, filed="2026-01-01"):
            return {"start": start, "end": end, "val": value, "unit": "INR",
                    "filed": filed, "accn": "test", "form": "10-Q",
                    "fp": "Q1", "fy": "2026", "url": "https://nsearchives.nseindia.in/x.xml"}

        series = facts.nse_quarterly([
            fact("2025-04-01", "2025-06-30", 100),
            fact("2025-07-01", "2025-09-30", 120),
            fact("2025-10-01", "2025-12-31", 130),
            fact("2025-04-01", "2025-12-31", 350),
            fact("2025-04-01", "2026-03-31", 500, "2026-05-01"),
        ])

        self.assertEqual(series["2026-03-31"]["value"], 150)
        self.assertEqual(series["2026-03-31"]["start"], "2026-01-01")
        self.assertTrue(series["2026-03-31"]["derived"])
        self.assertEqual(series["2026-03-31"]["form"], "10-K")

    def test_smartapi_quote_uses_nse_equity_token(self):
        env = {
            "ANGELONE_API_KEY": "test-key",
            "ANGELONE_CLIENT_CODE": "test-client",
            "ANGELONE_PASSWORD": "test-pin",
            "ANGELONE_TOTP_SECRET": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            "ANGELONE_CLIENT_PUBLIC_IP": "203.0.113.1",
        }
        replies = [
            {"jwtToken": "token"},
            [{"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885"}],
            {"fetched": [{"tradingSymbol": "RELIANCE-EQ", "ltp": 1234.5}]},
        ]
        with patch.dict(os.environ, env, clear=True), patch.object(
                facts, "_smartapi_post", side_effect=replies) as post:
            quote, note = facts.smartapi_quote("RELIANCE.NS")

        self.assertIsNone(note)
        self.assertEqual(quote["price"], 1234.5)
        self.assertEqual(quote["currency"], "INR")
        self.assertEqual(post.call_args.args[1]["exchangeTokens"], {"NSE": ["2885"]})


if __name__ == "__main__":
    unittest.main()
