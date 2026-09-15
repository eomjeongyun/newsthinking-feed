"""Collects today's News Thinking articles into news/YYYY-MM-DD.json, AES-GCM encrypted (GitHub Actions, skips if today is done)."""
import base64
import datetime
import os
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path

import trafilatura
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent
NEWS_DIR = ROOT / "news"
STATE = ROOT / "state.json"  # sha1 of already-used article URLs, so nothing readable is stored in plain text
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
PER_SECTION = 3
MIN_BODY = 400

RANDOM_TOPICS = [
    ("사회", "society"), ("정치", "politics"), ("경제", "economy"), ("연예", "entertainment"),
    ("국제", "international"), ("문화", "culture"), ("IT·산업", "industry"), ("스포츠", "sports"),
]
SKIP_TITLE = re.compile(r"^\s*\[(사진|포토|그래픽|영상|게시판|인사|부고|날씨|속보)\]|오늘의 주요일정|카드뉴스|헤드라인|주요뉴스|뉴스 브리핑|이 시각")


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def url_hash(url):
    return hashlib.sha1(url.encode()).hexdigest()


def encrypt(obj):
    key = base64.b64decode(os.environ["NEWS_KEY"])
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    return {"v": 1, "alg": "AES-GCM", "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def get(url):
    out = subprocess.run(["curl", "-sL", "--max-time", "25", "-A", UA, url], capture_output=True).stdout
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("utf-8", "ignore")


def clean_body(text):
    lines = [l.strip() for l in (text or "").splitlines()]
    for i, l in enumerate(lines):
        if "전체 내용을 이해하기 위해서는 기사 본문과 함께 읽어야" in l:
            lines = lines[i + 1:]
            break
    drop = re.compile(r"(무단\s*전재|재배포\s*금지|저작권자|기자\s*=|@yna\.co\.kr|구독|좋아요|제보는 카카오톡|ⓒ|©)")
    kept = [l for l in lines if l and not (len(l) < 80 and drop.search(l))]
    return "\n\n".join(kept).strip()


def article_from(url, title_hint="", source_hint=""):
    page = get(url)
    if not page:
        return None
    body = None
    if "n.news.naver.com" in url:
        m = re.search(r'<article id="dic_area"[^>]*>(.*?)</article>', page, re.S)
        if m:
            raw = re.sub(r"<br\s*/?>", "\n", m.group(1))
            raw = re.sub(r"<(em|strong|span)[^>]*class=\"img_desc\"[^>]*>.*?</\1>", "", raw, flags=re.S)
            raw = re.sub(r"<[^>]+>", "", raw)
            body = html.unescape(raw)
    if not body:
        body = trafilatura.extract(page, favor_precision=True, include_comments=False, include_tables=False)
    body = clean_body(body)
    if len(body) < MIN_BODY:
        return None
    title = title_hint
    tm = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    if tm:
        title = html.unescape(tm.group(1)).strip()
    source = source_hint
    sm = re.search(r'<meta property="og:article:author" content="([^"]+)"', page) or re.search(r'<meta name="twitter:creator" content="([^"]+)"', page)
    if sm and not source:
        source = html.unescape(sm.group(1)).split("|")[0].strip()
    image = ""
    im = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if im:
        image = html.unescape(im.group(1))
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:12],
        "title": re.sub(r"\s*[|:-]\s*(연합뉴스|네이버 뉴스)\s*$", "", title),
        "source": source or "",
        "url": url,
        "image": image,
        "body": body,
    }


def yonhap_section(slug, used_titles):
    rss = get(f"https://www.yna.co.kr/rss/{slug}.xml")
    items = re.findall(r"<item>(.*?)</item>", rss, re.S)
    picked = []
    for it in items:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not (t and l):
            continue
        title = html.unescape(t.group(1)).strip()
        if SKIP_TITLE.search(title) or title in used_titles:
            continue
        art = article_from(l.group(1).strip(), title, "연합뉴스")
        if not art:
            continue
        art["source"] = "연합뉴스"
        art["publishedAt"] = d.group(1).strip() if d else ""
        picked.append(art)
        used_titles.add(title)
        if len(picked) >= PER_SECTION:
            break
    return picked


ANDONG_FEEDS = [
    ("매일신문", "https://www.imaeil.com/rss"),
    ("경북도민일보", "https://www.hidomin.com/rss/allArticle.xml"),
    ("대구일보", "https://www.idaegu.com/rss/allArticle.xml"),
    ("경북일보", "https://www.kyongbuk.co.kr/rss/allArticle.xml"),
]
TOURISM = re.compile(r"관광|축제|여행|하회|탈춤|문화유산|세계유산|관광객|숙박|체류|페스티벌|명소|투어")


def andong_candidates():
    found = []
    for source, feed in ANDONG_FEEDS:
        rss = get(feed)
        for it in re.findall(r"<item>(.*?)</item>", rss, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            if not (t and l):
                continue
            title = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip()
            link = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", l.group(1))).strip()
            if "안동" in title and not SKIP_TITLE.search(title):
                d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
                found.append({"source": source, "title": title, "url": link, "publishedAt": d.group(1).strip() if d else ""})
    return found


def andong_sections(used_titles, past_urls):
    society, tourism = [], []
    for cand in andong_candidates():
        if url_hash(cand["url"]) in past_urls or cand["title"] in used_titles:
            continue
        bucket = tourism if TOURISM.search(cand["title"]) else society
        if len(bucket) >= PER_SECTION:
            continue
        art = article_from(cand["url"], cand["title"], cand["source"])
        if not art:
            continue
        art["title"], art["source"], art["publishedAt"] = cand["title"], cand["source"], cand["publishedAt"]
        bucket.append(art)
        used_titles.add(cand["title"])
        past_urls.add(url_hash(cand["url"]))
        if len(society) >= PER_SECTION and len(tourism) >= PER_SECTION:
            break
    return society, tourism


def main():
    today = datetime.date.today()
    force = "--force" in sys.argv
    out = NEWS_DIR / f"{today:%Y-%m-%d}.json"
    if out.exists() and not force:
        return
    used = set()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"used": []}
    past_hashes = set(state["used"])
    label, slug = RANDOM_TOPICS[today.toordinal() % len(RANDOM_TOPICS)]
    data = {
        "date": f"{today:%Y-%m-%d}",
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "random": {"topic": label, "articles": yonhap_section(slug, used)},
        "andong": {},
    }
    data["andong"]["society"], data["andong"]["tourism"] = andong_sections(used, past_hashes)
    counts = (len(data["random"]["articles"]), len(data["andong"]["society"]), len(data["andong"]["tourism"]))
    if sum(counts) == 0:
        log("nothing collected (network?) - will retry next run")
        return
    NEWS_DIR.mkdir(exist_ok=True)
    out.write_text(json.dumps(encrypt(data)), encoding="utf-8")
    for arts in (data["random"]["articles"], data["andong"]["society"], data["andong"]["tourism"]):
        past_hashes.update(url_hash(a["url"]) for a in arts)
    STATE.write_text(json.dumps({"used": sorted(past_hashes)}), encoding="utf-8")
    dates = sorted(p.stem for p in NEWS_DIR.glob("????-??-??.json"))
    (NEWS_DIR / "index.json").write_text(json.dumps({"dates": dates, "updatedAt": data["generatedAt"]}, ensure_ascii=False), encoding="utf-8")
    log(f"saved {out.name} random={label}:{counts[0]} andong-society={counts[1]} andong-tourism={counts[2]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"error: {exc!r}")
        raise
