"""TRIADA media monitoring collector (Python 3.11+, standard library only).

Public mentions.json contains ONLY confirmed mentions. Unverified candidates are kept
in mentions_review.json, never displayed by the dashboard. A backup is created before
migrating an existing database. Google News RSS is discovery, NOT verification.
"""
import hashlib
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

DATABASE = Path('mentions.json')
REVIEW_DATABASE = Path('mentions_review.json')
BACKUP = Path('mentions_before_filtering.json')
START_DATE = '2026-06-01'
REGIONS = [('en', 'US', 'US:en'), ('uk', 'UA', 'UA:uk')]
QUERIES = [
    '"Triada Robotics"', '"TriadaRobotics"',
    '"Тріада Роботікс"', '"Тріада Роботикс"',
    '"Триада Роботикс"', '"Триада Роботікс"',
    '"Artem Veselov"', '"Artem Veselow"', '"Артем Веселов"',
    '"Triada" "BRAVO"', '"Тріада" "Браво"', '"Триада" "Браво"',
    '"Triada" "KILO"', '"Тріада" "Кіло"',
]
USER_AGENT = 'Mozilla/5.0 (compatible; TriadaMediaMonitor/1.0)'
# Only the company/CEO can independently confirm a mention. BRAVO/KILO alone
# are ambiguous and never sufficient evidence.
COMPANY = [
    r'\btriada\s*robotics\b', r'\btriadarobotics\b',
    r'\bтріада\s*робот[іи]кс\b', r'\bтриада\s*робот[иі]кс\b',
]
CEO = [r'\bartem\s+vesel(?:ov|ow)\b', r'\bартем\s+веселов\b']
PRODUCTS = {
    'BRAVO': [r'\bbravo\b', r'\bбраво\b'],
    'KILO': [r'\bkilo\b', r'\bкіло\b', r'\bкило\b'],
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalized(text):
    return ' '.join(re.findall(r'\w+', str(text or '').casefold()))


def stable_id(record):
    # Compatible with the previous collector's deduplication scheme.
    key = normalized(record.get('title')) + '|' + normalized(record.get('source'))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def unique(items):
    return list(dict.fromkeys(x for x in items if x))


def combine(a, b):
    result = dict(a)
    for field, plural in [('url', 'urls'), ('keyword', 'keywords'), ('id', 'legacy_ids')]:
        result[plural] = unique(list(a.get(plural) or []) + list(b.get(plural) or [])
                                + [a.get(field), b.get(field)])
    for key, value in b.items():
        if key not in result or result[key] in (None, '', []):
            result[key] = value
    if a.get('status') == 'approved' or b.get('status') == 'approved':
        result['status'] = 'approved'
    return result


def read_records(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        raise ValueError(f'{path} must contain a JSON list')
    records = {}
    for record in data:
        if not isinstance(record, dict) or not record.get('title'):
            raise ValueError(f'Invalid record in {path}; no files changed')
        record = dict(record)
        original_id = record.get('id')
        record['id'] = stable_id(record)
        record['legacy_ids'] = unique(list(record.get('legacy_ids') or []) + [original_id])
        key = record['id']
        records[key] = combine(records[key], record) if key in records else record
    return records


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'):
            self.skip += 1
        elif tag in ('p', 'h1', 'h2', 'h3', 'article', 'section', 'div', 'br'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg', 'template') and self.skip:
            self.skip -= 1
        elif not self.skip and tag in ('p', 'h1', 'h2', 'h3', 'article', 'section', 'div'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def download_article(url):
    """Best effort. A blocked page is NOT evidence of absence of a mention."""
    if not url.startswith(('https://', 'http://')):
        return '', '', 'invalid_url'
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'text/html,application/xhtml+xml'})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            final_url = response.geturl()
            content_type = response.headers.get('Content-Type', '').lower()
            if 'html' not in content_type:
                return '', final_url, 'not_html'
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return '', final_url, 'too_large'
            charset = response.headers.get_content_charset() or 'utf-8'
            markup = raw.decode(charset, errors='replace')
            parser = ArticleText()
            parser.feed(markup)
            return re.sub(r'\s+', ' ', html.unescape(' '.join(parser.parts))).strip(), final_url, ''
    except (OSError, ValueError, UnicodeError) as exc:
        return '', '', type(exc).__name__


def evidence(text):
    company = next((m for pattern in COMPANY if (m := re.search(pattern, text, re.I))), None)
    ceo = next((m for pattern in CEO if (m := re.search(pattern, text, re.I))), None)
    if not (company or ceo):
        return None
    categories = []
    if company:
        categories.append('Company')
    if ceo:
        categories.append('CEO')
    # Product categories are assigned only when an unambiguous company/CEO
    # mention is also present in the same document.
    products = [name for name, patterns in PRODUCTS.items()
                if any(re.search(pattern, text, re.I) for pattern in patterns)]
    if products:
        categories.append('Products')
    match = company or ceo
    left = max(0, match.start() - 100)
    right = min(len(text), match.end() + 160)
    return {'categories': categories, 'products': products,
            'matched_text': text[left:right].strip()}


def verify(record):
    record = dict(record)
    title = record.get('title') or ''
    title_match = evidence(title)
    if title_match:
        record.update(title_match)
        record['verification_method'] = 'title'
        record['status'] = 'relevant'
        return record, True

    # Check article body only when the title itself is not conclusive.
    article_text, final_url, error = download_article(record.get('url', ''))
    host = (urllib.parse.urlparse(final_url).hostname or '').lower()
    # Google News landing pages are not publisher articles; their text can
    # contain unrelated recommendations and must not confirm a mention.
    is_publisher = bool(host and host != 'news.google.com' and
                        not host.endswith('.news.google.com'))
    if is_publisher:
        record['direct_url'] = final_url
    body_match = evidence(article_text) if article_text and is_publisher else None
    if body_match:
        record.update(body_match)
        record['verification_method'] = 'page_text'
        record['status'] = 'relevant'
        return record, True
    record['status'] = 'review'
    record['verification_method'] = 'unverified'
    record['verification_note'] = error or 'No confirmed company/CEO phrase in available page text'
    return record, False


def fetch(query, region):
    lang, country, edition = region
    params = urllib.parse.urlencode({'q': f'{query} after:2026-05-31',
                                     'hl': lang, 'gl': country, 'ceid': edition})
    req = urllib.request.Request('https://news.google.com/rss/search?' + params,
                                 headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as response:
        return ET.fromstring(response.read()).findall('./channel/item')


def parse(item, query):
    title = (item.findtext('title') or '').strip()
    url = (item.findtext('link') or '').strip()
    pub = (item.findtext('pubDate') or '').strip()
    if not (title and url and pub):
        return None
    try:
        date = parsedate_to_datetime(pub).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
    if date.date().isoformat() < START_DATE:
        return None
    source = item.find('source')
    name = (source.text or '').strip() if source is not None and source.text else 'Unknown'
    record = {'title': title, 'source': name, 'url': url,
              'published_at': date.isoformat(), 'keyword': query,
              'first_seen_at': utcnow(), 'source_type': 'Online Media'}
    record['id'] = stable_id(record)
    return record


def write_json(path, records):
    data = sorted(records.values(), key=lambda x: x.get('published_at', ''), reverse=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def main():
    # Backup must be created BEFORE any modification to the original database.
    if DATABASE.exists() and not BACKUP.exists():
        shutil.copy2(DATABASE, BACKUP)
        print(f'Backup created: {BACKUP}', flush=True)
    public = read_records(DATABASE)
    pending = read_records(REVIEW_DATABASE)
    all_records = dict(pending)
    for key, item in public.items():
        all_records[key] = combine(all_records[key], item) if key in all_records else item
    print(f'Loaded {len(all_records)} unique historical records', flush=True)

    # Verify historical records as well as newly discovered records.
    verified = {}
    review = {}
    def process(item):
        key = stable_id(item)
        if item.get('status') == 'rejected':
            review[key] = item
            return
        if item.get('status') == 'approved':
            verified[key] = item
            return
        checked, confirmed = verify(item)
        (verified if confirmed else review)[key] = checked

    for i, item in enumerate(all_records.values(), 1):
        process(item)
        if i % 10 == 0:
            print(f'Checked {i}/{len(all_records)} historical records', flush=True)

    success = failed = added = 0
    for query in QUERIES:
        for region in REGIONS:
            try:
                items = fetch(query, region)
                for rss_item in items:
                    entry = parse(rss_item, query)
                    if entry is None:
                        continue
                    key = entry['id']
                    if key in verified:
                        verified[key] = combine(verified[key], entry)
                    elif key in review:
                        review[key] = combine(review[key], entry)
                    else:
                        checked, confirmed = verify(entry)
                        (verified if confirmed else review)[key] = checked
                        added += 1
                        print(f'NEW [{"confirmed" if confirmed else "unverified"}]: {entry["title"]}', flush=True)
                success += 1
            except Exception as exc:
                failed += 1
                print(f'ERROR {query} {region[2]}: {exc}', flush=True)
            time.sleep(1)

    # Don't replace the public database if the search service is fully down.
    if success == 0:
        raise RuntimeError('All Google News searches failed; databases not overwritten')
    write_json(DATABASE, verified)
    write_json(REVIEW_DATABASE, review)
    print(f'Searches: {success} succeeded, {failed} failed; new candidates: {added}; '
          f'CONFIRMED ON SITE: {len(verified)}; unverified (not on site): {len(review)}', flush=True)


if __name__ == '__main__':
    main()
