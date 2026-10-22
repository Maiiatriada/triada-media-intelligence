import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
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

    def test_bing_rss_rejects_unrelated_article(self):
        import xml.etree.ElementTree as ET
        xml = ET.fromstring("<item><title>Drone technology</title><link>https://example.org/drone</link></item>")
        with patch("collector.fetch_bing_rss", return_value=[xml]), patch(
            "collector.fetch_alert_feeds", return_value=iter(())
        ), patch("collector.read_publisher", return_value=("Other unrelated drones", "https://example.org/drone")):
            confirmed = {}
            count = collector.collect_web_rss(confirmed, {})
        self.assertEqual(count, 0)
        self.assertEqual(confirmed, {})

    def test_bing_rss_accepts_verified_company_article(self):
        import xml.etree.ElementTree as ET
        xml = ET.fromstring("<item><title>Drone technology</title><link>https://example.org/drone</link></item>")
        with patch("collector.fetch_bing_rss", return_value=[xml]), patch(
            "collector.fetch_alert_feeds", return_value=iter(())
        ), patch("collector.read_publisher", return_value=("Triada Robotics developed a drone", "https://example.org/drone")):
            confirmed = {}
            count = collector.collect_web_rss(confirmed, {})
        self.assertEqual(count, 1)
        self.assertEqual(next(iter(confirmed.values()))["categories"], ["Company"])

    def test_blocked_article_not_confirmed(self):
        with patch("collector.urllib.request.urlopen", side_effect=OSError("blocked")):
            self.assertEqual(collector.read_article("https://example.org"), ("", ""))


# ---------------------------------------------------------------------------
# Connector diagnostics (collector_status.json)
# ---------------------------------------------------------------------------

# Fake feed address used only to prove that secrets never reach any output.
SECRET_URL = ("https://www.google.com/alerts/feeds/"
              "12345678901234567890/09876543210987654321")
SECRET_TOKENS = ("12345678901234567890", "09876543210987654321")
OLD_STATUS_KEYS = (
    "last_run_utc", "confirmed_total", "pending_internal", "google_news_added",
    "publisher_feeds_added", "gdelt_added", "bing_and_alerts_added",
    "google_news_query_errors",
)


def rss_item(title, link, source="Example Source",
             date="Wed, 21 Oct 2026 09:00:00 GMT"):
    return ET.fromstring(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{date}</pubDate><source>{source}</source></item>"
    )


def gdelt_article(title="Triada Robotics expands", url="https://example.org/t"):
    return {"title": title, "url": url, "domain": "example.org",
            "seendate": "20261022T110000Z"}


class ConnectorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        collector.DIAG.reset()
        self.addCleanup(collector.DIAG.reset)
        quiet = contextlib.redirect_stdout(io.StringIO())  # keep CI logs free of collector output
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {
            "GOOGLE_ALERTS_RSS": "", "GITHUB_SERVER_URL": "",
            "GITHUB_REPOSITORY": "", "GITHUB_RUN_ID": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def connector(self, name):
        return collector.DIAG.report({})["connectors"][name]

    def run_main(self, fetch=None, feed=None, gdelt=None, bing=None,
                 article=None, publisher=None, alerts_env="", previous=None):
        """Run main() in a temporary folder with every network call mocked."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original_dir = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, original_dir)
        if previous is not None:
            Path("collector_status.json").write_text(
                previous if isinstance(previous, str) else json.dumps(previous),
                encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": alerts_env}), \
                patch("collector.fetch", side_effect=fetch or (lambda q, r: [])), \
                patch("collector.fetch_publisher_feed", side_effect=feed or (lambda n, u: [])), \
                patch("collector.fetch_gdelt", side_effect=gdelt or (lambda q: [])), \
                patch("collector.fetch_bing_rss", side_effect=bing or (lambda q: [])), \
                patch("collector.read_article", side_effect=article or (lambda u: ("", ""))), \
                patch("collector.read_publisher", side_effect=publisher or (lambda u: ("", ""))), \
                patch("collector.resolve_google_news", side_effect=lambda url: url), \
                patch("collector.time.sleep"), \
                contextlib.redirect_stdout(output):
            collector.main()
        files = {p.name: p.read_text(encoding="utf-8") for p in Path(".").glob("*.json")}
        return json.loads(files["collector_status.json"]), files, output.getvalue()

    # ---- error cleaning and secrets -------------------------------------

    def test_safe_error_strips_urls_and_limits_length(self):
        text = collector.safe_error(
            OSError("failed https://example.org/a?token=abc " + "x" * 400))
        self.assertTrue(text.startswith("OSError:"))
        self.assertNotIn("example.org", text)
        self.assertNotIn("token=abc", text)
        self.assertLessEqual(len(text), 160)
        self.assertNotIn("\n", collector.safe_error(OSError("line one\nline two")))

    def test_safe_error_redacts_alert_secret_in_any_form(self):
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": SECRET_URL}):
            forms = [
                SECRET_URL,
                SECRET_URL.replace("https://", ""),
                "/alerts/feeds/12345678901234567890/09876543210987654321",
                "feed id 12345678901234567890 broke",
                urllib.parse.quote(SECRET_URL, safe=""),
            ]
            for form in forms:
                with self.subTest(form=form[:40]):
                    text = collector.safe_error(OSError(f"cannot read {form}"))
                    for token in SECRET_TOKENS:
                        self.assertNotIn(token, text)

    def test_alert_secret_never_reaches_any_output(self):
        def feed(name, url):
            if name == "Google Alerts":
                raise OSError(f"cannot read {url}")
            return []
        status, files, stdout = self.run_main(feed=feed, alerts_env=SECRET_URL)
        everything = stdout + "".join(files.values())
        for secret in SECRET_TOKENS + (SECRET_URL,):
            self.assertNotIn(secret, everything)
        self.assertIn("ALERT FEED UNAVAILABLE", stdout)
        self.assertEqual(status["connectors"]["google_alerts"]["status"], "error")

    # ---- status rules ------------------------------------------------------

    def test_status_rules(self):
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.items("gdelt", [1, 2])
        d.attempt("gdelt"); d.items("gdelt", [])
        d.attempt("bing"); d.attempt("bing"); d.failure("bing", OSError("x"))
        d.attempt("google_news"); d.failure("google_news", OSError("x"))
        d.not_configured("google_alerts")
        report = d.report({})["connectors"]
        self.assertEqual(report["gdelt"]["status"], "ok")
        self.assertEqual(report["bing"]["status"], "partial")
        self.assertEqual(report["google_news"]["status"], "error")
        self.assertEqual(report["google_alerts"]["status"], "not_configured")
        self.assertEqual(report["publisher_rss"]["status"], "not_run")
        self.assertEqual(report["gdelt"]["items_fetched"], 2)
        self.assertEqual(report["bing"]["failed_requests"], 1)

    def test_ok_means_technical_success_not_complete_coverage(self):
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.items("gdelt", [])  # answered, but returned nothing
        report = d.report({})
        self.assertEqual(report["connectors"]["gdelt"]["status"], "ok")
        self.assertEqual(report["connectors"]["gdelt"]["items_fetched"], 0)
        self.assertIn("does not guarantee", report["status_legend"]["ok"])
        for status in ("ok", "partial", "error", "not_configured", "not_run"):
            self.assertIn(status, report["status_legend"])

    def test_carry_over_success_time_and_failure_streak(self):
        before = "2026-10-01T00:00:00+00:00"
        previous = {"gdelt": {"last_success_utc": before, "last_attempt_utc": before,
                              "consecutive_failures": 2}}
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.failure("gdelt", OSError("down"))
        failing = d.report(previous)["connectors"]["gdelt"]
        self.assertEqual(failing["consecutive_failures"], 3)
        self.assertEqual(failing["last_success_utc"], before)
        self.assertNotEqual(failing["last_attempt_utc"], before)

        d = collector.Diagnostics()
        d.attempt("gdelt")
        recovered = d.report(previous)["connectors"]["gdelt"]
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["last_success_utc"], recovered["last_attempt_utc"])

        idle = collector.Diagnostics().report(previous)["connectors"]["gdelt"]
        self.assertEqual(idle["status"], "not_run")
        self.assertEqual(idle["consecutive_failures"], 2)
        self.assertEqual(idle["last_success_utc"], before)

    def test_malformed_previous_status_is_ignored(self):
        for previous in ("{not json", '["a list"]', '{"connectors": "text"}',
                         '{"connectors": {"gdelt": {"consecutive_failures": "x", '
                         '"last_success_utc": 5}}}'):
            with self.subTest(previous=previous[:20]):
                status, _, _ = self.run_main(previous=previous)
                gdelt = status["connectors"]["gdelt"]
                self.assertEqual(gdelt["status"], "ok")
                self.assertEqual(gdelt["consecutive_failures"], 0)
                self.assertIsInstance(gdelt["last_success_utc"], str)

    # ---- GDELT ---------------------------------------------------------------

    def test_gdelt_total_failure_is_reported_and_collection_continues(self):
        with patch("collector.fetch_gdelt", side_effect=OSError("boom")):
            self.assertEqual(collector.collect_gdelt({}, {}), 0)
        gdelt = self.connector("gdelt")
        self.assertEqual((gdelt["status"], gdelt["requests"], gdelt["failed_requests"]),
                         ("error", 3, 3))
        self.assertEqual(gdelt["last_error"], "OSError: boom")

    def test_gdelt_partial_success_counts_items_and_confirmations(self):
        calls = [OSError("boom"), [gdelt_article()], []]
        with patch("collector.fetch_gdelt", side_effect=calls), patch(
                "collector.read_article", return_value=("", "")):
            confirmed = {}
            self.assertEqual(collector.collect_gdelt(confirmed, {}), 1)
        gdelt = self.connector("gdelt")
        self.assertEqual(gdelt["status"], "partial")
        self.assertEqual((gdelt["items_fetched"], gdelt["new_confirmed"]), (1, 1))

    def test_gdelt_ok_with_no_results(self):
        with patch("collector.fetch_gdelt", return_value=[]):
            collector.collect_gdelt({}, {})
        self.assertEqual(self.connector("gdelt")["status"], "ok")

    # ---- Bing ------------------------------------------------------------------

    def test_bing_failures_and_confirmations(self):
        item = ET.fromstring("<item><title>Drone news</title>"
                             "<link>https://example.org/d</link></item>")
        with patch("collector.fetch_bing_rss", side_effect=OSError("down")), patch(
                "collector.fetch_alert_feeds", return_value=iter(())):
            self.assertEqual(collector.collect_web_rss({}, {}), 0)
        self.assertEqual(self.connector("bing")["status"], "error")
        self.assertEqual(self.connector("bing")["failed_requests"], 4)

        collector.DIAG.reset()
        with patch("collector.fetch_bing_rss",
                   side_effect=[[item], OSError("down"), [], []]), patch(
                "collector.fetch_alert_feeds", return_value=iter(())), patch(
                "collector.read_publisher",
                return_value=("Triada Robotics built a drone", "https://example.org/d")):
            confirmed = {}
            self.assertEqual(collector.collect_web_rss(confirmed, {}), 1)
        bing = self.connector("bing")
        self.assertEqual((bing["status"], bing["items_fetched"], bing["new_confirmed"]),
                         ("partial", 1, 1))
        self.assertEqual(self.connector("google_alerts")["new_confirmed"], 0)

    # ---- Google Alerts -------------------------------------------------------------

    def test_alerts_not_configured_without_secret(self):
        self.assertEqual(list(collector.fetch_alert_feeds()), [])
        alerts = self.connector("google_alerts")
        self.assertEqual((alerts["status"], alerts["requests"]), ("not_configured", 0))

    def test_alerts_configured_and_working(self):
        entries = [("T1", "https://x.example/1", "2026-10-21T09:00:00+00:00", "Google Alerts", ""),
                   ("T2", "https://x.example/2", "2026-10-21T09:00:00+00:00", "Google Alerts", "")]
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": SECRET_URL}), patch(
                "collector.fetch_publisher_feed", return_value=entries):
            self.assertEqual(len(list(collector.fetch_alert_feeds())), 2)
        alerts = self.connector("google_alerts")
        self.assertEqual((alerts["status"], alerts["items_fetched"]), ("ok", 2))

    def test_alerts_invalid_address_is_flagged_without_echoing_it(self):
        bad = "https://example.com/private-token-abcdefgh"
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": bad}):
            self.assertEqual(list(collector.fetch_alert_feeds()), [])
        alerts = self.connector("google_alerts")
        self.assertEqual(alerts["status"], "error")
        self.assertEqual(alerts["last_error"], collector.ALERTS_ADDRESS_IGNORED)
        self.assertNotIn("example.com", json.dumps(alerts))
        self.assertNotIn("abcdefgh", json.dumps(alerts))

    # ---- Publisher RSS ----------------------------------------------------------------

    def test_publisher_feeds_are_reported_individually(self):
        names = [name for name, _ in collector.PUBLISHER_FEEDS]
        broken = {names[1], names[3]}

        def feed(name, url):
            if name in broken:
                raise OSError("feed down")
            if name == names[0]:
                return [("Drone update", "https://publisher.example/story",
                         "2026-10-21T09:00:00+00:00", name,
                         "<p>Triada Robotics is mentioned here.</p>")]
            return []

        status, _, _ = self.run_main(feed=feed)
        publisher = status["connectors"]["publisher_rss"]
        self.assertEqual(publisher["status"], "partial")
        self.assertEqual([f["name"] for f in publisher["feeds"]], names)
        by_name = {f["name"]: f for f in publisher["feeds"]}
        self.assertEqual(by_name[names[0]]["status"], "ok")
        self.assertEqual((by_name[names[0]]["items_fetched"], by_name[names[0]]["new_confirmed"]), (1, 1))
        self.assertEqual(by_name[names[1]]["status"], "error")
        self.assertEqual(by_name[names[1]]["last_error"], "OSError: feed down")
        self.assertEqual((publisher["requests"], publisher["failed_requests"]), (len(names), 2))
        self.assertEqual(publisher["new_confirmed"], status["publisher_feeds_added"])

    # ---- Google News --------------------------------------------------------------------

    def test_google_news_total_failure(self):
        def fail(query, region):
            raise OSError("blocked")
        status, _, _ = self.run_main(fetch=fail)
        news = status["connectors"]["google_news"]
        total = len(collector.QUERIES) * len(collector.REGIONS)
        self.assertEqual((news["status"], news["requests"], news["failed_requests"]),
                         ("error", total, total))
        self.assertEqual(news["failed_requests"], status["google_news_query_errors"])
        self.assertEqual(news["last_error"], "OSError: blocked")

    def test_google_news_success_counts_items_and_confirmations(self):
        item = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        status, _, _ = self.run_main(
            fetch=lambda q, r: [item],
            article=lambda url: ("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        )
        news = status["connectors"]["google_news"]
        total = len(collector.QUERIES) * len(collector.REGIONS)
        self.assertEqual((news["status"], news["items_fetched"], news["new_confirmed"]),
                         ("ok", total, 1))
        self.assertEqual(news["new_confirmed"], status["google_news_added"])

    # ---- whole-run guarantees ------------------------------------------------------------

    def test_old_status_keys_are_kept_and_agree_with_connector_totals(self):
        gn = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        feed_entry = ("Update", "https://publisher.example/pf",
                      "2026-10-21T09:00:00+00:00", "Feed", "<p>Artem Veselov of Triada Robotics</p>")
        bing_item = ET.fromstring("<item><title>Defence news</title>"
                                  "<link>https://example.org/bing</link></item>")
        alert_entry = ("Alert story", "https://example.org/alert",
                       "2026-10-21T09:00:00+00:00", "Google Alerts", "")

        def feed(name, url):
            return [alert_entry] if name == "Google Alerts" else (
                [feed_entry] if name == collector.PUBLISHER_FEEDS[0][0] else [])

        def publisher(url):
            return ("Triada Robotics is here", url) if "bing" in url or "alert" in url else ("", "")

        status, _, _ = self.run_main(
            fetch=lambda q, r: [gn], feed=feed,
            gdelt=lambda q: [gdelt_article()] if "Triada" in q else [],
            bing=lambda q: [bing_item], publisher=publisher, alerts_env=SECRET_URL)

        for key in OLD_STATUS_KEYS:
            self.assertIn(key, status)
        c = status["connectors"]
        self.assertEqual(status["google_news_added"], c["google_news"]["new_confirmed"])
        self.assertEqual(status["publisher_feeds_added"], c["publisher_rss"]["new_confirmed"])
        self.assertEqual(status["gdelt_added"], c["gdelt"]["new_confirmed"])
        self.assertEqual(status["bing_and_alerts_added"],
                         c["bing"]["new_confirmed"] + c["google_alerts"]["new_confirmed"])
        self.assertEqual(status["bing_and_alerts_added"], 2)
        self.assertEqual(list(c), [key for key, _ in collector.CONNECTORS])
        self.assertEqual(status["schema_version"], 2)

    def test_run_section_and_github_run_url(self):
        status, _, _ = self.run_main()
        run = status["run"]
        self.assertIsNone(run["github_run_url"])
        self.assertLessEqual(run["started_utc"], run["finished_utc"])
        self.assertGreaterEqual(run["duration_seconds"], 0)
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "owner/repo",
               "GITHUB_RUN_ID": "42"}
        with patch.dict(os.environ, env):
            self.assertEqual(collector.github_run_url(), "https://github.com/owner/repo/actions/runs/42")

    def test_diagnostics_failure_does_not_change_results(self):
        gn = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        kwargs = dict(fetch=lambda q, r: [gn], gdelt=lambda q: [gdelt_article()])
        _, normal_files, _ = self.run_main(**kwargs)

        def corrupt(self):
            self.data = None  # every diagnostics method now fails internally
            self.feeds = None

        with patch.object(collector.Diagnostics, "reset", corrupt):
            status, broken_files, _ = self.run_main(**kwargs)

        for name in ("mentions.json", "mentions_review.json", "mentions_public.json"):
            self.assertEqual(normal_files[name], broken_files[name], name)
        for key in OLD_STATUS_KEYS:
            self.assertIn(key, status)
        self.assertNotIn("connectors", status)  # diagnostics skipped, old keys intact


if __name__ == "__main__":
    unittest.main()
