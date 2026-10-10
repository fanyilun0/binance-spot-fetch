import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch  # noqa: E402

ROW = [1791504000000, "84.23", "86.62", "83.40", "84.22", "268126.308",
       1791590399999, "22861797.70121", 57575, "104122.575", "8865873.69679", "0"]


class FetchTests(unittest.TestCase):
    def test_parse_kline_normalizes_microseconds(self):
        micro = [ROW[0] * 1000] + ROW[1:6] + [ROW[6] * 1000 + 999] + ROW[7:]
        k = fetch.parse_kline(micro)
        self.assertEqual(k["open_time"], ROW[0])
        self.assertEqual(k["open_time_utc"], "2026-10-09T00:00:00Z")
        self.assertEqual(fetch.same_kline(k, fetch.parse_kline(ROW)), [])

    def test_check_kline_flags_inconsistent_ohlc(self):
        self.assertEqual(fetch.check_kline(fetch.parse_kline(ROW), "ok"), [])
        bad = list(ROW)
        bad[2] = "80"  # high below close
        self.assertTrue(fetch.check_kline(fetch.parse_kline(bad), "bad"))

    def test_same_kline_detects_diff(self):
        other = list(ROW)
        other[4] = "84.21"
        self.assertEqual(fetch.same_kline(fetch.parse_kline(ROW), fetch.parse_kline(other)), ["close"])

    def test_utc_day_start(self):
        self.assertEqual(fetch.utc_day_start_ms(date(2026, 10, 9)), ROW[0])


if __name__ == "__main__":
    unittest.main()
