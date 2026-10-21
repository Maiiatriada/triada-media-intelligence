import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# ==========================================
# TRIADA MEDIA INTELLIGENCE
# Automated Media Monitoring Collector
# ==========================================

START_DATE = "2026-06-01"
DATABASE = Path("mentions.json")

REQUEST_TIMEOUT = 15
REQUEST_DELAY = 1


# ==========================================
# SEARCH QUERIES
# ==========================================

# Broad queries capture company and CEO mentions.
# Product queries include context to reduce false positives.

SEARCH_QUERIES = [

    # COMPANY
    '"Triada Robotics"',
    '"TriadaRobotics"',
    '"Тріада Роботікс"',
    '"Триада Роботикс"',
    '"Тріада Роботикс"',
    '"Триада Роботікс"',

    # COMPANY + PRODUCT
    '"Triada Robotics" "BRAVO"',
    '"Triada" "BRAVO" drone',
    '"Triada" "BRAVO" UAV',
    '"Тріада" "Браво" дрон',
    '"Тріада" "Браво" БПЛА',
    '"Триада" "Браво" беспилотник',

    # PRODUCT
    '"BRAVO" "deep strike" drone',
    '"BRAVO" "deep strike" UAV',
    '"BRAVO" "long-range drone" Ukraine',
    '"BRAVO" "strike drone" Ukraine',
    '"BRAVO" "UAV" "Triada"',

    # CEO
    '"Artem Veselov"',
    '"Artem Veselow"',
    '"Артем Веселов"',

    # CEO + COMPANY
    '"Artem Veselov" "Triada"',
    '"Artem Veselow" "Triada"',
    '"Артем Веселов" "Тріада"',

    # CEO + PRODUCT
    '"Artem Veselov" "BRAVO"',
    '"Artem Veselow" "BRAVO"',
    '"Артем Веселов" "Браво"',
]


# ==========================================
# GOOGLE NEWS REGIONS
# ==========================================

REGIONS = [
    ("en", "US", "US:en"),
    ("uk", "UA", "UA:uk"),
]


# ==========================================
# NORMALIZATION
# ==========================================

def normalize_text(value):
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def make_article_id(title, source):
    key = (
        normalize_text(title)
        + "|"
        + normalize_text(source)
    )

    return hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()


# ==========================================
# RELEVANCE CHECK
# ==========================================

def classify_mention(title):

    text = normalize_text(title)

    company_terms = [
        "triada robotics",
        "triadarobotics",
        "тріада роботікс",
        "тріада роботикс",
        "триада роботикс",
        "триада роботікс",
    ]

    ceo_terms = [
        "artem veselov",
        "artem veselow",
        "артем веселов",
    ]

    product_terms = [
        "bravo",
        "браво",
    ]

    triada_terms = [
        "triada",
        "тріада",
        "триада",
    ]

    drone_terms = [
        "drone",
        "uav",
        "deep strike",
        "long range",
        "long range drone",
        "безпілот",
        "бпла",
        "дрон",
        "беспилот",
    ]

    company = any(
        term in text for term in company_terms
    )

    ceo = any(
        term in text for term in ceo_terms
    )

    product = any(
        term in text for term in product_terms
    )

    triada = any(
        term in text for term in triada_terms
    )

    drone_context = any(
        term in text for term in drone_terms
    )

    if company and ceo:
        return "Both"

    if company:
        return "Triada Robotics"

    if ceo:
        return "Artem Veselow"

    if product and triada:
        return "BRAVO"

    if product and drone_context:
        return "BRAVO"

    return None


# ==========================================
# DATABASE
# ==========================================

def load_database():

    if not DATABASE.exists():
        print("No existing database found.")
        return {}

    with DATABASE.open(
        "r",
        encoding="utf-8"
    ) as file:

        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError(
            "mentions.json must contain a list"
        )

    mentions = {}

    for record in records:

        if not isinstance(record, dict):
            continue

        record_id = record.get("id")

        if record_id:
            mentions[record_id] = record

    print(
        f"Loaded {len(mentions)} existing mentions.",
        flush=True
    )

    return mentions


def save_database(mentions):

    records = sorted(
        mentions.values(),
        key=lambda x: x.get("published_at", ""),
        reverse=True
    )

    temporary_file = Path("mentions.json.tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            records,
            file,
            ensure_ascii=False,
            indent=2
        )

    temporary_file.replace(DATABASE)

    print(
        f"\nDatabase saved: {len(records)} mentions",
        flush=True
    )


# ==========================================
# GOOGLE NEWS SEARCH
# ==========================================

def fetch_articles(
    query,
    language,
    country,
    edition
):

    search_query = (
        f"{query} after:2026-05-31"
    )

    params = urllib.parse.urlencode({
        "q": search_query,
        "hl": language,
        "gl": country,
        "ceid": edition,
    })

    url = (
        "https://news.google.com/rss/search?"
        + params
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT
    ) as response:

        xml_data = response.read()

    root = ET.fromstring(xml_data)

    return root.findall("./channel/item")


# ==========================================
# PARSE ARTICLES
# ==========================================

def parse_article(item, query):

    title = item.findtext(
        "title", ""
    ).strip()

    url = item.findtext(
        "link", ""
    ).strip()

    published = item.findtext(
        "pubDate", ""
    ).strip()

    source_element = item.find("source")

    source = (
        source_element.text.strip()
        if source_element is not None
        and source_element.text
        else "Unknown"
    )

    if not title or not url or not published:
        return None

    try:
        publication_date = (
            parsedate_to_datetime(published)
            .astimezone(timezone.utc)
        )

    except (ValueError, TypeError):
        return None

    if (
        publication_date.date().isoformat()
        < START_DATE
    ):
        return None

    subject = classify_mention(title)

    if subject is None:
        return None

    article_id = make_article_id(
        title,
        source
    )

    return {
        "id": article_id,
        "title": title,
        "source": source,
        "source_type": "Online Media",
        "subject": subject,
        "published_at": (
            publication_date.isoformat()
        ),
        "url": url,
        "keyword": query,
        "first_seen_at": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "status": "pending",
    }


# ==========================================
# DISPLAY RESULTS
# ==========================================

def display_mention(record):

    print("\n" + "=" * 65)

    print("NEW MEDIA MENTION")

    print("=" * 65)

    print(
        f"Title: {record['title']}"
    )

    print(
        f"Source: {record['source']}"
    )

    print(
        f"Date: {record['published_at'][:10]}"
    )

    print(
        f"Subject: {record['subject']}"
    )

    print(
        f"Link: {record['url']}"
    )

    print("=" * 65, flush=True)


# ==========================================
# MAIN
# ==========================================

def main():

    print("\nTRIADA MEDIA INTELLIGENCE")
    print("=========================")

    print(
        f"Historical search from: {START_DATE}"
    )

    print(
        f"Search queries: {len(SEARCH_QUERIES)}"
    )

    print(
        f"Regions: {len(REGIONS)}"
    )

    mentions = load_database()

    previous_count = len(mentions)

    successful = 0
    failed = 0
    new_mentions = 0

    try:

        for query in SEARCH_QUERIES:

            for language, country, edition in REGIONS:

                print(
                    f"\nSearching: {query} | {edition}",
                    flush=True
                )

                try:

                    articles = fetch_articles(
                        query,
                        language,
                        country,
                        edition
                    )

                    successful += 1

                    print(
                        f"Returned: {len(articles)}",
                        flush=True
                    )

                    for item in articles:

                        record = parse_article(
                            item,
                            query
                        )

                        if record is None:
                            continue

                        if record["id"] in mentions:
                            continue

                        mentions[
                            record["id"]
                        ] = record

                        new_mentions += 1

                        display_mention(record)

                except Exception as error:

                    failed += 1

                    print(
                        f"Search error: {error}",
                        flush=True
                    )

                time.sleep(REQUEST_DELAY)

    finally:

        if successful > 0:
            save_database(mentions)

    print("\n================================")
    print("TRIADA MEDIA MONITORING REPORT")
    print("================================")

    print(
        f"Successful searches: {successful}"
    )

    print(
        f"Failed searches: {failed}"
    )

    print(
        f"Previously saved: {previous_count}"
    )

    print(
        f"New mentions: {new_mentions}"
    )

    print(
        f"Total mentions: {len(mentions)}"
    )

    print(
        f"Database: {DATABASE}"
    )

    print("================================")

    if successful == 0:
        raise RuntimeError(
            "All Google News requests failed."
        )


if __name__ == "__main__":
    main()
