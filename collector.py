"""TRIADA Media Intelligence — Google News collector."""

import json
import re
import time
import hashlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DATABASE = Path("mentions.json")
START_DATE = "2026-06-01"

# SEARCH KEYWORDS
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

# CONFIRMED MENTIONS
COMPANY = re.compile(
    r"triada[\s-]*robotics|triadarobotics|"
    r"тріада\s*робот[іи]кс|триада\s*робот[иі]кс",
    re.I
)

CEO = re.compile(
    r"artem\s+veselov|artem\s+veselow|артем\s+веселов",
    re.I
)

PRODUCTS = {
    "BRAVO": re.compile(r"\bbravo\b|\bбраво\b", re.I),
    "KILO": re.compile(r"\bkilo\b|\bкіло\b", re.I),
}


def classify(title):
    categories = []

    if COMPANY.search(title):
        categories.append("Company")

    if CEO.search(title):
        categories.append("CEO")

    if categories:
        for product, pattern in PRODUCTS.items():
            if pattern.search(title):
                categories.append("Products")
                break

    return categories


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


def main():
    records = {}

    # Keep existing confirmed mentions
    if DATABASE.exists():
        for item in json.loads(DATABASE.read_text(encoding="utf-8")):
            if item.get("status") in ("relevant", "approved"):
                records[item["id"]] = item

    added = 0

    for query in QUERIES:
        for region in REGIONS:
            try:
                for item in fetch(query, region):
                    title = (item.findtext("title") or "").strip()
                    url = (item.findtext("link") or "").strip()
                    date = item.findtext("pubDate")

                    if not (title and url and date):
                        continue

                    published = parsedate_to_datetime(date).astimezone(
                        timezone.utc
                    ).isoformat()

                    if published[:10] < START_DATE:
                        continue

                    categories = classify(title)

                    # Only confirmed company or CEO mentions
                    if not categories:
                        continue

                    source = item.find("source")
                    source_name = (
                        source.text.strip()
                        if source is not None and source.text
                        else "Unknown"
                    )

                    key = hashlib.sha256(
                        (title + source_name).lower().encode()
                    ).hexdigest()

                    if key in records:
                        continue

                    records[key] = {
                        "id": key,
                        "title": title,
                        "source": source_name,
                        "url": url,
                        "published_at": published,
                        "categories": categories,
                        "status": "relevant",
                    }

                    added += 1
                    print(f"NEW: {title}", flush=True)

            except Exception as error:
                print(f"ERROR {query}: {error}", flush=True)

            time.sleep(1)

    result = sorted(
        records.values(),
        key=lambda x: x["published_at"],
        reverse=True
    )

    DATABASE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"New mentions: {added}")
    print(f"Total confirmed mentions: {len(result)}")


if __name__ == "__main__":
    main()
