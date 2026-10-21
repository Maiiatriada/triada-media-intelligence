"""TRIADA media collector. Python 3.11+, standard library only."""
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

DATABASE = Path('mentions.json')
START_DATE = '2026-06-01'
QUERIES = [
    '"Triada Robotics"', '"TriadaRobotics"', '"Тріада Роботікс"',
    '"Триада Роботикс"', '"Тріада Роботикс"', '"Триада Роботікс"',
    '"Triada Robotics" "BRAVO"', '"Triada" "BRAVO" drone',
    '"Triada" "BRAVO" UAV', '"Тріада" "Браво" дрон',
    '"Тріада" "Браво" БПЛА', '"Триада" "Браво" беспилотник',
    '"BRAVO" "deep strike" drone', '"BRAVO" "deep strike" UAV',
    '"BRAVO" "long-range drone" Ukraine', '"BRAVO" "strike drone" Ukraine',
    '"BRAVO" "UAV" "Triada"', '"Artem Veselov"',
    '"Artem Veselow"', '"Артем Веселов"',
    '"Artem Veselov" "Triada"', '"Artem Veselow" "Triada"',
    '"Артем Веселов" "Тріада"', '"Artem Veselov" "BRAVO"',
    '"Artem Veselow" "BRAVO"', '"Артем Веселов" "Браво"',
]
REGIONS = [('en', 'US', 'US:en'), ('uk', 'UA', 'UA:uk')]


def norm(value):
    return ' '.join(re.findall(r'\w+', str(value or '').casefold()))


def article_id(record):
    # Stable across old Google News GUID IDs and newer SHA256 IDs.
    key = norm(record.get('title')) + '|' + norm(record.get('source'))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def classify(title):
    t = norm(title)
    company = any(x in t for x in (
        'triada robotics', 'triadarobotics', 'тріада роботікс',
        'тріада роботикс', 'триада роботикс', 'триада роботікс'))
    ceo = any(x in t for x in ('artem veselov', 'artem veselow', 'артем веселов'))
    bravo = bool(re.search(r'\b(bravo|браво)\b', t))
    drone = any(x in t for x in (
        'drone', 'uav', 'дрон', 'бпла', 'безпілот', 'беспилот',
        'strike', 'удар', 'реактивн', 'дальност', 'дальніст'))
    triada = any(x in t for x in ('triada', 'тріада', 'триада'))
    if company and ceo:
        return 'relevant', 'Both'
    if company:
        return 'relevant', 'Triada Robotics'
    if ceo:
        return 'relevant', 'Artem Veselov'
    if bravo and (triada or drone):
        return 'review', 'BRAVO'  # BRAVO can name unrelated drones.
    # Only unmistakably off-topic titles are rejected automatically.
    unrelated = ('футбол', 'сборной испании', 'олимпиад', 'теннис',
                 'football match', 'soccer', 'basketball', 'голу сборной')
    if any(word in t for word in unrelated) and not (bravo or company or ceo):
        return 'irrelevant', None
    return 'review', None  # Google may match article body, not its title.


def unique(values):
    return list(dict.fromkeys(v for v in values if v))


def merge(old, new):
    """Preserve metadata from both records, including old IDs and URLs."""
    for field, list_field in [('id', 'legacy_ids'), ('url', 'urls'),
                              ('keyword', 'keywords')]:
        values = []
        for item in (old, new):
            values += item.get(list_field, [])
            if item.get(field):
                values.append(item[field])
        old[list_field] = unique(values)
    for key, value in new.items():
        if key not in old or old[key] in (None, ''):
            old[key] = value
    old['first_seen_at'] = min(filter(None, [old.get('first_seen_at'),
                                             new.get('first_seen_at')]), default='')
    return old


def prepare(record):
    record = dict(record)
    record['id'] = article_id(record)
    status, subject = classify(record.get('title', ''))
    # Do not overwrite manual review decisions.
    if record.get('status') not in ('approved', 'rejected'):
        record['status'] = status
    if subject:
        record['subject'] = subject
    record.setdefault('source_type', 'Online Media')
    return record


def load():
    if not DATABASE.exists():
        return {}, 0
    raw = json.loads(DATABASE.read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        raise ValueError('mentions.json must be a JSON list')
    result = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get('title'):
            raise ValueError('Invalid record in mentions.json; database left unchanged')
        fixed = prepare(item)
        # Preserve original ID before replacing it with stable one.
        fixed['legacy_ids'] = unique(item.get('legacy_ids', []) + [item.get('id')])
        key = fixed['id']
        if key in result:
            result[key] = merge(result[key], fixed)
        else:
            result[key] = fixed
    return result, len(raw)


def fetch(query, region):
    lang, country, edition = region
    params = urllib.parse.urlencode({
        'q': f'{query} after:2026-05-31', 'hl': lang,
        'gl': country, 'ceid': edition})
    req = urllib.request.Request('https://news.google.com/rss/search?' + params,
                                 headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return ET.fromstring(response.read()).findall('./channel/item')


def parse(item, query):
    title = (item.findtext('title') or '').strip()
    url = (item.findtext('link') or '').strip()
    date_text = (item.findtext('pubDate') or '').strip()
    if not (title and url and date_text):
        return None
    try:
        published = parsedate_to_datetime(date_text).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
    if published.date().isoformat() < START_DATE:
        return None
    source = item.find('source')
    source_name = (source.text or '').strip() if source is not None else 'Unknown'
    record = {'title': title, 'source': source_name, 'url': url,
              'published_at': published.isoformat(), 'keyword': query,
              'first_seen_at': datetime.now(timezone.utc).isoformat()}
    return prepare(record)


def save(records):
    ordered = sorted(records.values(),
                     key=lambda r: r.get('published_at', ''), reverse=True)
    tmp = DATABASE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    tmp.replace(DATABASE)


def main():
    records, before = load()
    print(f'Loaded {before} records; unique after deduplication: {len(records)}', flush=True)
    success = failed = added = 0
    try:
        for query in QUERIES:
            for region in REGIONS:
                try:
                    for item in fetch(query, region):
                        entry = parse(item, query)
                        if not entry:
                            continue
                        key = entry['id']
                        if key in records:
                            # Retain all matching search queries and URLs.
                            records[key] = merge(records[key], entry)
                        else:
                            records[key] = entry
                            added += 1
                            print(f'NEW [{entry["status"]}]: {entry["title"]}', flush=True)
                    success += 1
                except Exception as exc:
                    failed += 1
                    print(f'ERROR {query} {region[2]}: {exc}', flush=True)
                time.sleep(1)
    finally:
        # Existing records are safely migrated even if all RSS requests fail.
        save(records)
    counts = {status: sum(r.get('status') == status for r in records.values())
              for status in ('relevant', 'review', 'irrelevant', 'approved', 'rejected')}
    print(f'Searches: {success} successful, {failed} failed. '
          f'New: {added}. Total: {len(records)}. Statuses: {counts}', flush=True)
    if success == 0:
        raise RuntimeError('All Google News requests failed')


if __name__ == '__main__':
    main()
