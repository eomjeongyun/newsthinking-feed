"""Adds hand-picked articles (JSON list of {title, subtitle, source, publishedAt, body, note}) to the 안동 관광 archive."""
import datetime
import hashlib
import json
import sys

from fetch_news import add_to_archive

src = json.load(open(sys.argv[1], encoding="utf-8"))
origin = sys.argv[2] if len(sys.argv) > 2 else "dad"
today = datetime.date.today().isoformat()
articles = []
for a in src:
    art = {k: a.get(k, "") for k in ("title", "subtitle", "source", "publishedAt", "body", "note")}
    art["id"] = hashlib.sha1((origin + a["title"]).encode()).hexdigest()[:12]
    art["url"] = ""
    art["image"] = ""
    articles.append(art)
print("added", add_to_archive(articles, origin, today))
