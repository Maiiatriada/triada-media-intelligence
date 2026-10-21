import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

START_DATE = "2026-06-01"
DATABASE = Path("mentions.json")

# Пошукові запити
KEYWORDS = [
    # Компанія
    '"Triada Robotics"',
    '"TriadaRobotics"',
    '"Тріада Роботікс"',
    '"Триада Роботикс"',
    '"Triada Robotics" defence',
    '"Triada Robotics" defense',
    '"Triada Robotics" drone',
    '"Triada Robotics" UAV',

    # Продукт BRAVO
    '"Triada Robotics" "BRAVO"',
    '"TRIADA" "BRAVO" drone',
    '"BRAVO" "deep strike" drone',
    '"BRAVO" "long range" Ukraine drone',
    '"BRAVO" UAV Triada',
    '"БРАВО" "Тріада"',
    '"Браво" "Тріада Роботікс"',
    '"BRAVO" "Artem Veselov"',

    # CEO
    '"Artem Veselov"',
    '"Artem Veselow"',
    '"Artem Veselov" "Triada Robotics"',
    '"Artem Veselow" CEO',
    '"Artem Veselov" drone',
    '"Артем Веселов"',
    '"Артем Веселов" "Тріада"',
    '"Артем Веселов" "Браво"',
]

REGIONS = [
    ("en", "US", "US:en"),
    ("uk", "UA", "UA:uk"),
    ("ru", "UA", "UA:ru"),
]


def load_database():
    if not DATABASE.exists():
        return {}

    with DATABASE.open("r", encoding="utf-8") as file:
        records = json.load(file)

    return {record["id"]: record for record in records}


def fetch_articles(keyword, language, country, edition):
    params = urllib.parse.urlencode({
        "q": f"{keyword} after:2026-05-31",
        "hl": language,
        "gl": country,
        "ceid": edition,
    })

    url = f"https://news.google.com/rss/search?{params}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        root = ET.fromstring(response.read())

    return root.findall("./channel/item")


def parse_article(item, keyword):
    title = item.findtext("title", "").strip()
    link = item.findtext("link", "").strip()
    published = item.findtext("pubDate", "").strip()
    source_element = item.find("source")

    source = (
        source_element.text.strip()
        if source_element is not None and source_element.text
        else "Unknown"
    )

    if not title or not link or not published:
        return None

    try:
        date = parsedate_to_datetime(published).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

    if date.date().isoformat() < START_DATE:
        return None

    # Google News GUID helps identify duplicate articles
    guid = item.findtext("guid", "").strip()
    article_id = guid or link

    return {
        "id": article_id,
        "title": title,
        "source": source,
        "published_at": date.isoformat(),
        "url": link,
        "keyword": keyword,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }


def main():
    mentions = load_database()
    previous_count = len(mentions)

    successful_searches = 0
    failed_searches = 0

    for keyword in KEYWORDS:
        for language, country, edition in REGIONS:
            print(f"Searching: {keyword} | {edition}", flush=True)

            try:
                articles = fetch_articles(
                    keyword, language, country, edition
                )
                successful_searches += 1

                for item in articles:
                    record = parse_article(item, keyword)

                    if record and record["id"] not in mentions:
                        mentions[record["id"]] = record

            except Exception as error:
                failed_searches += 1
                print(f"Search failed: {error}", flush=True)

            time.sleep(2)

    records = sorted(
        mentions.values(),
        key=lambda record: record["published_at"],
        reverse=True,
    )

    with DATABASE.open("w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False, indent=2)

    print("\n=== TRIADA MEDIA MONITORING ===")
    print(f"Successful searches: {successful_searches}")
    print(f"Failed searches: {failed_searches}")
    print(f"Previously saved mentions: {previous_count}")
    print(f"New mentions: {len(records) - previous_count}")
    print(f"Total mentions: {len(records)}")
    print(f"Database: {DATABASE}")

    if successful_searches == 0:
        raise RuntimeError("All search requests failed")


if __name__ == "__main__":
    main()
