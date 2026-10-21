import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# Перший тестовий пошуковий запит
KEYWORD = '"Triada Robotics"'

params = urllib.parse.urlencode({
    "q": KEYWORD,
    "hl": "en",
    "gl": "US",
    "ceid": "US:en",
})

url = f"https://news.google.com/rss/search?{params}"

request = urllib.request.Request(
    url,
    headers={"User-Agent": "Mozilla/5.0"}
)

print(f"Searching Google News for: {KEYWORD}")

try:
    with urllib.request.urlopen(request, timeout=20) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    articles = root.findall("./channel/item")

    print(f"Found {len(articles)} articles\n")

    for article in articles[:10]:
        print("Title:", article.findtext("title"))
        print("Date:", article.findtext("pubDate"))
        print("Link:", article.findtext("link"))
        print("-" * 50)

except Exception as error:
    print("Search failed:", error)
    raise
