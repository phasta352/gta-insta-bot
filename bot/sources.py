"""Collects fresh stories from RSS feeds and Google News."""
import calendar
import hashlib
import html
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import requests

UA = "Mozilla/5.0 (compatible; CyclingNewsBot/1.0)"
TIMEOUT = 20


@dataclass
class Story:
    title: str
    url: str
    source: str
    summary: str = ""
    published: float = 0.0
    via_google: bool = False
    text: str = ""          # article text, filled in later for shortlisted stories
    key: str = field(default="")

    def __post_init__(self):
        if not self.key:
            self.key = story_key(self.title, self.url)


def clean(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def norm_title(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", "", t.lower())
    return " ".join(t.split()[:12])


def story_key(title: str, url: str) -> str:
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]


def strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _entry_time(e) -> float:
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return calendar.timegm(e[k])
    return time.time()


def fetch_feed(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"not a valid feed ({feed.bozo_exception})")
    return feed


def google_news_url(query: str) -> str:
    return ("https://news.google.com/rss/search?q=" + quote_plus(query + " when:1d")
            + "&hl=en-US&gl=US&ceid=US:en")


def collect(settings: dict, state: dict) -> list[Story]:
    cfg = settings["sources"]
    max_age = cfg.get("max_age_hours", 24) * 3600
    now = time.time()
    stories: dict[str, Story] = {}
    errors = {}

    for src in cfg.get("rss", []):
        try:
            feed = fetch_feed(src["url"])
            for e in feed.entries[:40]:
                st = Story(
                    title=clean(e.get("title", "")),
                    url=strip_query(e.get("link", "")),
                    source=src["name"],
                    summary=clean(e.get("summary", ""))[:600],
                    published=_entry_time(e),
                )
                if st.title and now - st.published < max_age:
                    stories.setdefault(st.key, st)
        except Exception as ex:  # one broken feed must not stop the bot
            errors[src["name"]] = str(ex)[:200]

    for q in cfg.get("google_news", []):
        try:
            feed = fetch_feed(google_news_url(q))
            for e in feed.entries[:25]:
                title = clean(e.get("title", ""))
                source = clean(e.get("source", {}).get("title", "")) if e.get("source") else ""
                if source and title.endswith(" - " + source):
                    title = title[: -len(source) - 3]
                st = Story(
                    title=title,
                    url=e.get("link", ""),
                    source=source or "Google News",
                    summary="",
                    published=_entry_time(e),
                    via_google=True,
                )
                if st.title and now - st.published < max_age:
                    stories.setdefault(st.key, st)   # direct feeds win over Google copies
        except Exception as ex:
            errors[f"Google News: {q}"] = str(ex)[:200]

    state["feed_errors"] = errors
    # Optional keyword filter, for general feeds that cover more than this page's topic.
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    if keywords:
        stories = {k: s for k, s in stories.items()
                   if any(w in f"{s.title} {s.summary}".lower() for w in keywords)}
    fresh = [s for s in stories.values() if s.key not in state["seen"]]
    fresh.sort(key=lambda s: s.published, reverse=True)
    return fresh[:60]


def resolve_google_link(url: str) -> str:
    """Google News links are redirects; turn them into the publisher's real article URL (best effort)."""
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        if res.get("status") and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception as ex:
        print(f"[bot] Couldn't resolve Google News link: {ex}", flush=True)
    return url


def fetch_article_text(story: Story, limit: int = 6000) -> str:
    """Best-effort download of the article text so the AI has real facts to work from."""
    try:
        if story.via_google:
            real = resolve_google_link(story.url)
            if real != story.url:
                story.url = real          # also gives the draft a direct source link
        r = requests.get(story.url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or "news.google.com" in r.url:
            return ""
        try:
            import trafilatura
            text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
        except Exception:
            paras = re.findall(r"<p[^>]*>(.*?)</p>", r.text, flags=re.S | re.I)
            text = " ".join(clean(p) for p in paras)
        return text[:limit]
    except Exception:
        return ""
