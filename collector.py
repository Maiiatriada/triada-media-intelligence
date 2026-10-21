"""TRIADA Media Intelligence — media monitoring."""

import json
import re
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

DATABASE = Path("mentions.json")
REVIEW = Path("mentions_review.json")
START_DATE = "2026-06-01"

QUERIES = [
    '"Triada Robotics"',
    '"TriadaRobotics"',
    '"Тріада Роботікс"',
    '"Триада Роботикс"',
    '"Artem Veselov"',
    '"Artem Veselow"',
    '"Артем Веселов"',
    '"Triada" "BRAVO"',
    '"Тріада" "Браво"',
    '"Триада" "Браво"',
    '"Triada" "KILO"',
    '"Тріада" "Кіло"',
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
    r"\bартем\s+веселов\b",
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
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_article(url):
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            final_url = response.geturl()

            if "news.google.com" in (
                urllib.parse.urlparse(final_url).hostname or ""
            ):
                return "", ""

            if "html" not in response.headers.get(
                "Content-Type", ""
            ).lower():
                return "", ""

            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"

        parser = TextExtractor()
        parser.feed(raw.decode(charset, errors="replace"))

        return " ".join(parser.parts), final_url

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

                    if not match:
                        text, direct_url = read_article(url)
                        match = classify(text) if text else None

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

    save(DATABASE, confirmed)
    save(REVIEW, pending)

    print(f"New confirmed: {added}")
    print(f"Total confirmed: {len(confirmed)}")
    print(f"Pending verification: {len(pending)}")
    print(f"Search errors: {errors}")


if __name__ == "__main__":
    main()
