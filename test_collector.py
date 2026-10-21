import unittest
from unittest.mock import patch
import collector


class CollectorTests(unittest.TestCase):
    def test_stable_deduplication(self):
        self.assertEqual(
            collector.article_id("Triada Robotics’ Bravo!", "Defender Media"),
            collector.article_id("Triada Robotics Bravo", "Defender Media"),
        )
        self.assertNotEqual(
            collector.article_id("Triada Robotics Bravo", "Defender Media"),
            collector.article_id("Тріада Роботікс Bravo", "Defender Media"),
        )

    def test_company_and_ceo(self):
        self.assertIsNotNone(collector.classify("Triada Robotics develops drones"))
        self.assertIsNotNone(collector.classify("Interview with Artem Veselov"))
        self.assertIsNotNone(collector.classify("Інтерв’ю з Артемом Веселовим"))
        self.assertIsNone(collector.classify("BRAVO football team scores"))

    def test_article_body_and_no_navigation(self):
        page = "<nav>Triada Robotics</nav><article><h1>Drone news</h1><p>Artem Veselov said this.</p></article>"
        text = collector.extract_article(page)
        self.assertIn("Artem Veselov", text)
        self.assertNotIn("Triada Robotics", text)

    def test_publisher_url_extraction(self):
        page = '<meta property="og:url" content="https://example.org/article">'
        self.assertEqual(
            collector.publisher_url_from_google(page),
            "https://example.org/article",
        )

    def test_no_arbitrary_publisher_link(self):
        self.assertEqual(collector.publisher_url_from_google('<a href="https://other.org">Other</a>'), "")

    def test_publisher_feed_confirms_body_mention(self):
        from unittest.mock import patch
        entries = [("Drone technology update", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Example Publisher")]
        with patch("collector.read_article",
                   return_value=("Triada Robotics develops BRAVO", "https://publisher.example/story")):
            confirmed, pending = {}, {}
            added = collector.match_publisher_feed(confirmed, pending, entries)
        self.assertEqual(added, 1)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(next(iter(confirmed.values()))["url"], "https://publisher.example/story")

    def test_publisher_feed_rejects_unrelated_body(self):
        from unittest.mock import patch
        entries = [("Bravo wins football match", "https://publisher.example/football",
                    "2026-10-21T09:00:00+00:00", "Example Publisher")]
        with patch("collector.read_article", return_value=("Sports match report", "https://publisher.example/football")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 0)
        self.assertEqual(confirmed, {})

    def test_publisher_feed_content_verifies_body_without_fetch(self):
        entries = [("New defence technology", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Publisher",
                    "<p>Artem Veselov of Triada Robotics spoke about drones.</p>")]
        with patch("collector.read_article", side_effect=AssertionError("No fetch needed")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 1)
        self.assertEqual(next(iter(confirmed.values()))["verification_method"],
                         "publisher_feed_body")

    def test_publisher_feed_ignores_unrelated_content(self):
        entries = [("Drone technology", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Publisher",
                    "<p>Generic BRAVO and KILO names only.</p>")]
        with patch("collector.read_article", return_value=("", "")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 0)
        self.assertFalse(confirmed)

    def test_rss_content_encoded(self):
        import io
        from unittest.mock import MagicMock
        feed = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><item><title>New drone</title><link>https://publisher.example/news</link>
          <pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate>
          <content:encoded><![CDATA[<p>Triada Robotics is mentioned here.</p>]]></content:encoded>
          </item></channel></rss>"""
        response = MagicMock()
        response.read.return_value = feed
        response.__enter__.return_value = response
        with patch("collector.urllib.request.urlopen", return_value=response):
            entries = collector.fetch_publisher_feed("Publisher", "https://publisher.example/rss")
        self.assertEqual(len(entries), 1)
        self.assertIn("Triada Robotics", entries[0][4])

    def test_gdelt_verifies_body_before_publication(self):
        article = {"title": "New drone capabilities",
                   "url": "https://example.org/triada", "domain": "example.org",
                   "seendate": "20261022T110000Z"}
        with patch("collector.fetch_gdelt", return_value=[article]), patch(
            "collector.read_article",
            return_value=("Triada Robotics develops drones.", "https://example.org/triada")
        ):
            confirmed = {}
            count = collector.collect_gdelt(confirmed, {})
        self.assertEqual(count, 1)
        self.assertEqual(len(confirmed), 1)

    def test_gdelt_rejects_keyword_only_search_hit(self):
        article = {"title": "Random drone news", "url": "https://example.org/random",
                   "domain": "example.org", "seendate": "20261022T110000Z"}
        with patch("collector.fetch_gdelt", return_value=[article]), patch(
            "collector.read_article", return_value=("No company mention.", "https://example.org/random")
        ):
            confirmed = {}
            count = collector.collect_gdelt(confirmed, {})
        self.assertEqual(count, 0)
        self.assertFalse(confirmed)

    def test_blocked_article_not_confirmed(self):
        with patch("collector.urllib.request.urlopen", side_effect=OSError("blocked")):
            self.assertEqual(collector.read_article("https://example.org"), ("", ""))


if __name__ == "__main__":
    unittest.main()
