import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import market_intelligence as mi


class MarketIntelligenceTests(unittest.TestCase):
    def setUp(self):
        mi.FRED_CACHE.clear()
        mi.NEWS_CACHE.clear()

    def test_missing_fred_key_is_unavailable_without_request(self):
        session = Mock()
        result = mi.fetch_fred_series("DGS10", None, session=session)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["observations"], [])
        session.get.assert_not_called()

    def test_fred_observation_date_is_not_release_timestamp_and_is_cached(self):
        session = Mock()
        response = Mock()
        response.json.return_value = {"observations": [
            {"date": "2026-10-01", "value": "4.12"},
            {"date": "2026-10-02", "value": "."},
            {"date": "invalid", "value": "2"},
        ]}
        session.get.return_value = response
        first = mi.fetch_fred_series("DGS10", "test-key", session=session, now=100)
        second = mi.fetch_fred_series("DGS10", "test-key", session=session, now=101)
        self.assertEqual(first["observations"], [{"observation_date": "2026-10-01", "value": 4.12, "unit": "%"}])
        self.assertNotIn("release_at", first["observations"][0])
        self.assertIs(first, second)
        session.get.assert_called_once()

    def test_fred_timeout_propagates_to_route_boundary(self):
        session = Mock()
        session.get.side_effect = TimeoutError("timeout")
        with self.assertRaises(TimeoutError):
            mi.fetch_fred_series("UNRATE", "key", session=session)

    def test_news_cache_evicts_old_entries_at_its_limit(self):
        with patch.object(mi, "MAX_NEWS_CACHE_ENTRIES", 2):
            mi._store_news_cache("first", 1, [])
            mi._store_news_cache("second", 2, [])
            mi._store_news_cache("third", 3, [])
        self.assertEqual(len(mi.NEWS_CACHE), 2)
        self.assertNotIn("first", mi.NEWS_CACHE)

    def test_news_normalizes_publication_time_and_deduplicates(self):
        xml = """<rss><channel>
        <item><title>Market rises</title><link>https://publisher.example/a</link><source>Publisher</source><pubDate>Tue, 20 Oct 2026 12:00:00 +0000</pubDate><description>Story</description></item>
        <item><title>Market rises!</title><link>https://publisher.example/b</link><pubDate>bad-date</pubDate></item>
        <item><title>Broken article without link</title></item></channel></rss>"""
        items = mi.normalize_news_xml(xml, datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "Publisher")
        self.assertEqual(items[0]["published_at"], "2026-10-20T12:00:00Z")
        self.assertEqual(items[0]["sentiment"], "Unscored")

    def test_move_requires_two_prices_and_associates_only_dated_in_window_news(self):
        self.assertEqual(mi.analyze_move([{"x": 1, "y": 2}], "source")["status"], "insufficient_evidence")
        start_ms = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp() * 1000)
        news = [{"headline": "timely", "source": "Publisher", "url": "https://example.test/a", "published_at": "2026-10-20T12:30:00Z"},
                {"headline": "undated", "url": "https://example.test/b"}]
        result = mi.analyze_move([{"x": start_ms, "y": 10}, {"x": start_ms + 3600000, "y": 11}], "snapshot source", news)
        self.assertAlmostEqual(result["evidence"][0]["change_percent"], 10)
        self.assertEqual(len(result["related_news"]), 1)
        self.assertIn("does not establish", result["summary"])


if __name__ == "__main__":
    unittest.main()
