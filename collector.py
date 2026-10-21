"""TRIADA Media Intelligence — media monitoring."""

import json
import re
import time
import hashlib
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

DATABASE = Path("mentions.json")
REVIEW = Path("mentions_review.json")
PUBLIC = Path("mentions_public.json")
START_DATE = "2026-06-01"

QUERIES = [
    '"Triada Robotics"',
    '"TriadaRobotics"',
    '"Тріада Роботікс"',
    '"Триада Роботикс"',
    '"Artem Veselov"',
    '"Artem Veselow"',
    '"Артем Веселов"',
    '"Артема Веселова"',
    '"Artem Veselov" "drone"',
    '"Артемом Веселовим"',
    '"Веселов" "Triada"',
    '"Veselov" "Triada Robotics"',
    '"TRIADA ROBOTICS" BRAVO',
    '"TRIADA ROBOTICS" KILO',
    '"Triada" "BRAVO"',
    '"Тріада" "Браво"',
    '"Триада" "Браво"',
    '"Triada" "KILO"',
    '"Тріада" "Кіло"',
    '"BRAVO" "Triada Robotics"',
    '"KILO" "Triada Robotics"',
]

# Publisher feeds supply direct article links that Google News RSS does not.
# Inaccessible feeds are skipped without interrupting other discovery.
PUBLISHER_FEEDS = [
    ("Мілітарний", "https://militarnyi.com/uk/feed/"),
    ("Militarnyi", "https://militarnyi.com/en/feed/"),
    ("Defender Media", "https://thedefender.media/feed/"),
    ("dev.ua", "https://dev.ua/rss"),
    ("ITC.ua", "https://itc.ua/feed/"),
    ("Euromaidan Press", "https://euromaidanpress.com/feed/"),
    ("РБК-Україна", "https://www.rbc.ua/ukr/rss"),
    ("UNITED24 Media", "https://united24media.com/rss"),
]

REGIONS = [
    ("en", "US", "US:en"),
    ("uk", "UA", "UA:uk"),
]

COMPANY = re.compile(
    r"\btriada[\s-]*robotics\b|"
    r"\bтріада[\s-]*робот[іи]кс\b|"
    r"\bтриада[\s-]*робот[иі]кс\b",
    re.I
)

CEO = re.compile(
    r"\bartem\s+vesel(?:ov|ow)\b|"
    r"\bартем(?:а|ом|у|ові)?\s+веселов(?:а|им|у|і|им|им)?\b",
    re.I
)

PRODUCTS = {
    "BRAVO": re.compile(r"\bbravo\b|\bбраво\b", re.I),
    "KILO": re.compile(r"\bkilo\b|\bкіло\b", re.I),
}


def normalize(text):
    return " ".join(re.findall(r"\w+", str(text).casefold()))


def article_id(title, source):
    value = normalize(title) + "|" + normalize(source)
    return hashlib.sha256(value.encode()).hexdigest()


def classify(text):
    company = bool(COMPANY.search(text))
    ceo = bool(CEO.search(text))

    if not (company or ceo):
        return None

    categories = []

    if company:
        categories.append("Company")

    if ceo:
        categories.append("CEO")

    products = [
        name for name, pattern in PRODUCTS.items()
        if pattern.search(text)
    ]

    if products and company:
        categories.append("Products")

    match = COMPANY.search(text) or CEO.search(text)

    return {
        "categories": categories,
        "products": products,
        "matched_text": text[
            max(0, match.start() - 80):
            min(len(text), match.end() + 120)
        ].strip()
    }


class TextExtractor(HTMLParser):
    """Extract article text without navigation, scripts, or site footers."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipped = 0
        self.article_depth = 0
        self.article_parts = []
        self.article_seen = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipped += 1
        if tag == "article":
            self.article_depth += 1
            self.article_seen = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipped = max(0, self.skipped - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)

    def handle_data(self, data):
        if not self.skipped and data.strip():
            self.parts.append(data)
            if self.article_depth:
                self.article_parts.append(data)

    def text(self):
        # Prefer article text to prevent matches in "related articles" menus.
        return " ".join(self.article_parts if self.article_parts else self.parts)


def publisher_url_from_google(page):
    """Use explicit publisher metadata, never an arbitrary external page link."""
    patterns = (
        r'<meta[^>]+(?:property|name)="og:url"[^>]+content="([^"]+)"',
        r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
    )
    for pattern in patterns:
        for candidate in re.findall(pattern, page, re.I):
            candidate = html.unescape(candidate)
            host = urllib.parse.urlparse(candidate).hostname or ""
            if host and not host.endswith(
                ("google.com", "googleusercontent.com", "gstatic.com")
            ):
                return candidate
    return ""


def read_article(url):
    """Only treat publisher HTML as verifiable article text."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type:
                return "", ""
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
        page = raw.decode(charset, errors="replace")
        host = urllib.parse.urlparse(final_url).hostname or ""
        if host.endswith("news.google.com"):
            publisher = publisher_url_from_google(page)
            if publisher:
                return read_publisher(publisher)
            return "", ""
        return extract_article(page), final_url
    except Exception:
        return "", ""


def extract_article(page):
    parser = TextExtractor()
    parser.feed(page)
    return html.unescape(parser.text())


def read_publisher(url):
    """Avoid recursive Google News redirects."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith("google.com"):
        return "", ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            final_url = response.geturl()
            if (urllib.parse.urlparse(final_url).hostname or "").endswith("google.com"):
                return "", ""
            if "html" not in response.headers.get("Content-Type", "").lower():
                return "", ""
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
        return extract_article(raw.decode(charset, errors="replace")), final_url
    except Exception:
        return "", ""


def fetch(query, region):
    lang, country, edition = region

    params = urllib.parse.urlencode({
        "q": f"{query} after:2026-05-31",
        "hl": lang,
        "gl": country,
        "ceid": edition,
    })

    url = "https://news.google.com/rss/search?" + params

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return ET.fromstring(response.read()).findall("./channel/item")


def fetch_publisher_feed(name, url):
    """Read RSS/Atom, including publisher-authored article content."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        root = ET.fromstring(response.read(2_000_000))

    entries = []
    atom = "{http://www.w3.org/2005/Atom}"
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"

    if root.tag == atom + "feed":
        items = root.findall(atom + "entry")
        for item in items:
            title = " ".join(item.findtext(atom + "title", default="").split())
            link_element = next(
                (x for x in item.findall(atom + "link")
                 if x.get("rel", "alternate") == "alternate"), None
            )
            link = link_element.get("href", "") if link_element is not None else ""
            date = item.findtext(atom + "published") or item.findtext(atom + "updated")
            raw_body = (item.findtext(atom + "content") or
                        item.findtext(atom + "summary") or "")
            if not (title and link and date):
                continue
            try:
                published = __import__("datetime").datetime.fromisoformat(
                    date.replace("Z", "+00:00")
                ).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
            if published[:10] >= START_DATE:
                entries.append((title, link, published, name, raw_body))
    else:
        for item in root.findall("./channel/item"):
            title = " ".join((item.findtext("title") or "").split())
            link = (item.findtext("link") or "").strip()
            date = item.findtext("pubDate")
            raw_body = (item.findtext(content_ns + "encoded") or
                        item.findtext("description") or "")
            if not (title and link and date):
                continue
            try:
                published = parsedate_to_datetime(date).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
            if published[:10] >= START_DATE:
                entries.append((title, link, published, name, raw_body))
    return entries


def match_publisher_feed(confirmed, pending, entries):
    """Verify direct article text or publisher-authored RSS content."""
    added = 0
    existing = {article_id(x["title"], x["source"]): x
                for x in list(confirmed.values()) + list(pending.values())}
    for entry in entries:
        title, link, published, source = entry[:4]
        feed_body = entry[4] if len(entry) > 4 else ""
        key = article_id(title, source)
        record = existing.get(key)
        if key in confirmed:
            if not (urllib.parse.urlparse(confirmed[key].get("url", "")).hostname or "").endswith("news.google.com"):
                continue

        # A publisher's own feed is trusted as the source, not Google snippets.
        feed_text = extract_article(html.unescape(feed_body)) if feed_body else ""
        title_match = classify(title)
        feed_match = classify(feed_text) if feed_text else None
        match = title_match or feed_match
        verification_method = "publisher_feed_title" if title_match else "publisher_feed_body"

        # Read the original article if the feed does not contain a relevant mention.
        direct = ""
        if not match:
            text, direct = read_article(link)
            match = classify(text) if text else None
            verification_method = "publisher_article_body"
        if not match:
            continue
        if record is None:
            record = {"id": key, "title": title, "source": source,
                      "published_at": published, "keyword": "publisher RSS"}
        record.update(match)
        record.update({"status": "relevant", "url": direct or link,
                       "direct_url": direct or link,
                       "verification_method": verification_method})
        confirmed[key] = record
        pending.pop(key, None)
        added += 1
    return added



def fetch_gdelt(query):
    """GDELT DOC API: independent discovery with publisher URLs."""
    params = urllib.parse.urlencode({
        "query": query,
        "mode": "artlist",
        "maxrecords": 100,
        "format": "json",
        "startdatetime": "20260601000000",
        "sort": "datedesc",
    })
    request = urllib.request.Request(
        "https://api.gdeltproject.org/api/v2/doc/doc?" + params,
        headers={"User-Agent": "TRIADA-Media-Monitor/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return data.get("articles", [])


def collect_gdelt(confirmed, pending):
    """Publish only articles verified in the title or publisher body."""
    added = 0
    for query in ('"Triada Robotics"', '"Artem Veselov"', '"TriadaRobotics"'):
        try:
            articles = fetch_gdelt(query)
        except Exception as error:
            print(f"GDELT UNAVAILABLE {query}: {error}", flush=True)
            continue
        for article in articles:
            title = html.unescape(article.get("title") or "").strip()
            url = article.get("url") or ""
            source = article.get("domain") or "GDELT"
            date = article.get("seendate") or ""
            if not (title and url and date):
                continue
            key = article_id(title, source)
            if key in confirmed:
                continue
            # A GDELT query match alone is not evidence of a mention.
            title_match = classify(title)
            body, direct_url = read_article(url)
            body_match = classify(body) if body else None
            match = body_match or title_match
            if not match:
                continue
            # GDELT dates are UTC YYYYMMDDTHHMMSSZ.
            try:
                published = __import__("datetime").datetime.strptime(
                    date[:15], "%Y%m%dT%H%M%S"
                ).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
            if published[:10] < START_DATE:
                continue
            record = {
                "id": key, "title": title, "source": source,
                "url": direct_url or url, "direct_url": direct_url or url,
                "published_at": published, "keyword": query,
                "status": "relevant",
                "verification_method": "gdelt_article_body" if body_match else "gdelt_title",
            }
            record.update(match)
            confirmed[key] = record
            pending.pop(key, None)
            added += 1
    return added


def load(path):
    if not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"Invalid JSON database: {path}")

    return data


def save(path, records):
    data = sorted(
        records.values(),
        key=lambda x: x.get("published_at", ""),
        reverse=True
    )

    temp = path.with_name(path.name + ".tmp")

    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    temp.replace(path)


def main():
    confirmed = {}
    pending = {}

    # Rebuild stable IDs to eliminate duplicates.
    for record in load(DATABASE):
        key = article_id(record["title"], record["source"])
        record["id"] = key
        confirmed.setdefault(key, record)

    for record in load(REVIEW):
        key = article_id(record["title"], record["source"])
        record["id"] = key

        if key not in confirmed:
            pending.setdefault(key, record)

    # Recheck previously unverified articles.
    for key, record in list(pending.items()):
        title_match = classify(record["title"])

        if title_match:
            record.update(title_match)
            record["status"] = "relevant"
            record["verification_method"] = "title"
            confirmed[key] = record
            del pending[key]
            continue

        text, direct_url = read_article(
            record.get("direct_url") or record["url"]
        )

        match = classify(text) if text else None

        if match:
            record.update(match)
            record["status"] = "relevant"
            record["verification_method"] = "article_body"

            if direct_url:
                record["direct_url"] = direct_url
                record["url"] = direct_url

            confirmed[key] = record
            del pending[key]

    added = 0
    errors = 0

    for query in QUERIES:
        for region in REGIONS:
            try:
                items = fetch(query, region)

                for item in items:
                    title = (item.findtext("title") or "").strip()
                    url = (item.findtext("link") or "").strip()
                    date = item.findtext("pubDate")

                    if not (title and url and date):
                        continue

                    published = parsedate_to_datetime(
                        date
                    ).astimezone(timezone.utc).isoformat()

                    if published[:10] < START_DATE:
                        continue

                    source = item.find("source")
                    source_name = (
                        source.text.strip()
                        if source is not None and source.text
                        else "Unknown"
                    )

                    key = article_id(title, source_name)

                    if key in confirmed or key in pending:
                        continue

                    match = classify(title)
                    direct_url = ""

                    verification_method = "title" if match else ""
                    if not match:
                        text, direct_url = read_article(url)
                        match = classify(text) if text else None
                        if match:
                            verification_method = "article_body"

                    record = {
                        "id": key,
                        "title": title,
                        "source": source_name,
                        "url": direct_url or url,
                        "published_at": published,
                        "keyword": query,
                    }

                    if match:
                        record.update(match)
                        record["status"] = "relevant"
                        record["verification_method"] = verification_method
                        confirmed[key] = record

                        added += 1
                        print(f"CONFIRMED: {title}", flush=True)

                    else:
                        record["status"] = "review"
                        pending[key] = record

            except Exception as error:
                errors += 1
                print(f"ERROR {query}: {error}", flush=True)

            time.sleep(1)

    if errors == len(QUERIES) * len(REGIONS):
        print("Google News unavailable; continuing with publisher feeds and GDELT", flush=True)

    publisher_added = 0
    for source, feed in PUBLISHER_FEEDS:
        try:
            entries = fetch_publisher_feed(source, feed)
            publisher_added += match_publisher_feed(confirmed, pending, entries)
        except Exception as error:
            print(f"FEED UNAVAILABLE {source}: {error}", flush=True)

    gdelt_added = collect_gdelt(confirmed, pending)
    print(f'Confirmed via GDELT: {gdelt_added}', flush=True)

    save(DATABASE, confirmed)
    save(REVIEW, pending)
    save(PUBLIC, confirmed)

    print(f"New confirmed from Google News: {added}")
    print(f"Confirmed via publisher feeds: {publisher_added}")
    print(f"Total confirmed: {len(confirmed)}")
    print(f"Pending verification: {len(pending)}")
    print(f"Search errors: {errors}")


if __name__ == "__main__":
    main()
