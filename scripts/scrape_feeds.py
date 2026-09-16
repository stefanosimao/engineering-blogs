#!/usr/bin/env python3
"""Generate RSS feeds for blogs that do not publish one.

A few blogs in the list (Anthropic's research/news/engineering pages, LinkedIn
Engineering, Monzo, Riot Games, ...) have no RSS or Atom feed. This script
builds one from each blog's listing page so feed readers and the search index
can follow them anyway. It is deliberately conservative:

* only server-rendered listing pages, only links matching a per-site pattern;
* titles come from the card heading, or from the article's own og:title when
  the listing has no heading (a bounded number of article fetches per run);
* dates come from <time> elements, from dates written in the card text, or
  from the article page (article:published_time, JSON-LD, <time>, or the first
  date written near the top of the article; month-only dates map to the 1st);
* the previous run's feed is merged in, so an item never loses its date and a
  broken or changed page never empties a published feed.

Config (scripts/scraped_feeds.json) is a list of objects:
  name          display name (channel title)
  slug          output file name (<slug>.xml)
  url           listing page to scrape
  link_pattern  regex an absolute link must match to count as a post
  exclude       optional regex; matching links are ignored
  include_text  optional regex the card text must match (e.g. a category)
  max_items     items kept per feed (default 60)
  fetch_articles  fetch article pages for missing titles/dates (default true)

Usage:
  scripts/scrape_feeds.py --out _site/feeds [--previous URL-or-dir] [--only slug]

Requires: requests, feedparser, beautifulsoup4 (pip install -r scripts/requirements.txt)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import feedparser
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = (10, 30)
ARTICLE_FETCH_CAP = 25          # article pages fetched per site per run
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),                                  # 2026-09-09
    re.compile(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b"),  # Sep 9, 2026 / September 9 2026
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? ([A-Z][a-z]{2,8}),? (\d{4})\b"),     # 13 August 2026
]
GENERIC_TITLES = {"read more", "read", "learn more", "more", "continue reading", "view", "open"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- dates
def _iso(y: int, m: int, d: int) -> str | None:
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    if dt.year < 1995 or dt > date.today():
        return None
    return dt.isoformat()


def date_from_text(text: str) -> tuple[str | None, str]:
    """Return (iso date, matched substring) for the first date written in text."""
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        if i == 0:
            iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif i == 1:
            mon = MONTHS.get(m.group(1)[:3].lower())
            iso = _iso(int(m.group(3)), mon, int(m.group(2))) if mon else None
        else:
            mon = MONTHS.get(m.group(2)[:3].lower())
            iso = _iso(int(m.group(3)), mon, int(m.group(1))) if mon else None
        if iso:
            return iso, m.group(0)
    return None, ""


def date_from_datetime_attr(value: str) -> str | None:
    value = (value or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return date_from_text(value)[0]


# ----------------------------------------------------------------------------- fetching
def fetch(url: str) -> tuple[str, str]:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return resp.url, resp.text


def article_details(url: str) -> tuple[str | None, str | None]:
    """Best-effort (title, iso date) from an article page."""
    try:
        _, html = fetch(url)
    except Exception:  # noqa: BLE001
        return None, None
    soup = BeautifulSoup(html, "html.parser")
    title = None
    og = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "twitter:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    if title:
        title = re.sub(r"\s+", " ", title)
        title = re.split(r"\s+[|\\–—-]\s+(?:Anthropic|LinkedIn|Monzo|Riot Games|Trend Micro|Zomato)[^|]*$", title)[0].strip()
    found = None
    for attrs in ({"property": "article:published_time"}, {"name": "article:published_time"}, {"name": "date"},
                  {"name": "pubdate"}, {"name": "publish-date"}, {"name": "parsely-pub-date"}, {"itemprop": "datePublished"},
                  {"property": "og:article:published_time"}, {"name": "dc.date"}, {"name": "DC.date.issued"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            found = date_from_datetime_attr(tag["content"])
            if found:
                break
    if not found:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
            if m:
                found = date_from_datetime_attr(m.group(1))
                if found:
                    break
    if not found:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            found = date_from_datetime_attr(t["datetime"])
    if not found:
        # Last resort: the first date written near the top of the article body
        # ("Published May 25, 2026", "January 21, 2026", or month-level "August 2026").
        main = soup.find("main") or soup.find("article") or soup.body
        text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:2500] if main else ""
        found = date_from_text(text)[0] or month_year_from_text(text)
    return title, found


MONTH_YEAR_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\.? (\d{4})\b")


def month_year_from_text(text: str) -> str | None:
    """Month-level date ("August 2026") mapped to the first of that month."""
    m = MONTH_YEAR_RE.search(text)
    if not m:
        return None
    return _iso(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1)


# ----------------------------------------------------------------------------- extraction
def norm_link(url: str) -> str:
    p = urlparse(url)
    return p._replace(query="", fragment="").geturl().rstrip("/")


def card_of(anchor, link_re, base_url):
    """The largest ancestor that still contains only this post's link (the post's card).

    Climbing stops before an ancestor that also links to another post, so dates and
    excerpts are read from this post's card only and never from a neighbouring one.
    """
    href = norm_link(urljoin(base_url, anchor["href"].split("#")[0])).lower()
    node = anchor
    for _ in range(6):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "main"):
            break
        others = {norm_link(urljoin(base_url, a["href"].split("#")[0])).lower()
                  for a in parent.find_all("a", href=True) if link_re.search(urljoin(base_url, a["href"].split("#")[0]))}
        if others - {href}:
            break
        node = parent
    return node


def extract_items(html: str, base_url: str, cfg: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    link_re = re.compile(cfg["link_pattern"])
    excl_re = re.compile(cfg["exclude"]) if cfg.get("exclude") else None
    incl_re = re.compile(cfg["include_text"], re.I) if cfg.get("include_text") else None
    items: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].split("#")[0].strip())
        if not link_re.search(href) or (excl_re and excl_re.search(href)):
            continue
        key = norm_link(href).lower()
        if key in seen:
            continue
        card = card_of(a, link_re, base_url)
        card_text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        if incl_re and not incl_re.search(card_text):
            continue
        seen.add(key)
        heading = a.find(["h1", "h2", "h3", "h4", "h5"]) or (card.find(["h1", "h2", "h3", "h4", "h5"]) if card is not a else None)
        title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) if heading else re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        iso = None
        t = a.find("time", attrs={"datetime": True}) or card.find("time", attrs={"datetime": True})
        if t:
            iso = date_from_datetime_attr(t["datetime"])
        if not iso:
            iso, matched = date_from_text(card_text)
            if matched and not heading:
                title = title.replace(matched, " ")
        title = re.sub(r"\s+", " ", title).strip(" -–|·")
        needs_title = (not title or title.lower() in GENERIC_TITLES or len(title) < 8 or not heading)
        excerpt = card_text
        if title and title in excerpt:
            excerpt = excerpt.replace(title, " ", 1)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:300]
        items.append({"link": norm_link(href), "title": title, "date": iso, "excerpt": excerpt, "needs_title": needs_title})
        if len(items) >= cfg.get("max_items", 60):
            break
    return items


# ----------------------------------------------------------------------------- previous feed
def read_previous(base: str | None, slug: str) -> dict[str, dict]:
    if not base:
        return {}
    src = urljoin(base if base.endswith("/") else base + "/", f"{slug}.xml") if base.startswith("http") else os.path.join(base, f"{slug}.xml")
    try:
        if src.startswith("http"):
            r = requests.get(src, headers=HEADERS, timeout=(10, 60))
            if r.status_code != 200:
                return {}
            data = r.content
        else:
            with open(src, "rb") as fh:
                data = fh.read()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for e in feedparser.parse(data).entries:
        link = e.get("link")
        if not link:
            continue
        iso = None
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            try:
                iso = date(*t[:3]).isoformat()
            except (TypeError, ValueError):
                iso = None
        out[norm_link(link).lower()] = {"link": norm_link(link), "title": e.get("title", ""), "date": iso,
                                        "excerpt": re.sub(r"<[^>]+>", " ", e.get("summary", "") or "").strip()[:300]}
    return out


# ----------------------------------------------------------------------------- output
def write_rss(path: str, cfg: dict, items: list[dict], generated: datetime) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">', "  <channel>",
             f"    <title>{escape(cfg['name'])}</title>", f"    <link>{escape(cfg['url'])}</link>",
             f"    <description>{escape('Feed generated from ' + cfg['url'] + ' because the site publishes no RSS feed (engineering-blogs).')}</description>",
             f"    <lastBuildDate>{format_datetime(generated)}</lastBuildDate>", "    <generator>engineering-blogs scrape_feeds.py</generator>"]
    for it in items:
        lines.append("    <item>")
        lines.append(f"      <title>{escape(it['title'])}</title>")
        lines.append(f"      <link>{escape(it['link'])}</link>")
        lines.append(f'      <guid isPermaLink="true">{escape(it["link"])}</guid>')
        if it.get("date"):
            y, m, d = (int(x) for x in it["date"].split("-"))
            lines.append(f"      <pubDate>{format_datetime(datetime(y, m, d, 12, 0, tzinfo=timezone.utc))}</pubDate>")
        if it.get("excerpt"):
            lines.append(f"      <description>{escape(it['excerpt'])}</description>")
        lines.append("    </item>")
    lines += ["  </channel>", "</rss>", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ----------------------------------------------------------------------------- main per site
def build_feed(cfg: dict, out_dir: str, previous: str | None, generated: datetime) -> dict:
    slug = cfg["slug"]
    started = time.time()
    prev = read_previous(previous, slug)
    result = {"name": cfg["name"], "slug": slug, "url": cfg["url"], "file": f"{slug}.xml", "ok": False, "items": 0,
              "new": 0, "from_page": 0, "latest": "", "error": ""}
    try:
        final_url, html = fetch(cfg["url"])
        fresh = extract_items(html, final_url, cfg)
    except Exception as exc:  # noqa: BLE001
        fresh = []
        result["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
    result["from_page"] = len(fresh)

    if not fresh and not prev:
        log(f"  {cfg['name']}: nothing extracted and no previous feed ({result['error'] or 'no matching links'})")
        return result
    if not fresh:
        log(f"  {cfg['name']}: page yielded nothing ({result['error'] or 'no matching links'}); republishing previous feed")

    fetches = 0
    merged: list[dict] = []
    for it in fresh:
        old = prev.get(it["link"].lower())
        if old:
            if old.get("title") and (it["needs_title"] or not it["title"]):
                it["title"] = old["title"]
                it["needs_title"] = False
            it["date"] = it["date"] or old.get("date")
            it["excerpt"] = it["excerpt"] or old.get("excerpt", "")
        else:
            result["new"] += 1
        # Fetch the article for a missing title or date; items that stay undated are
        # retried on later runs, a bounded number per run.
        if cfg.get("fetch_articles", True) and fetches < ARTICLE_FETCH_CAP and (it["needs_title"] or not it["date"]):
            fetches += 1
            a_title, a_date = article_details(it["link"])
            if a_title and (it["needs_title"] or not it["title"]):
                it["title"] = a_title
            it["date"] = it["date"] or a_date
        if not it["title"]:
            it["title"] = it["link"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
        merged.append(it)
    on_page = {it["link"].lower() for it in merged}
    for key, old in prev.items():
        if key not in on_page and old.get("title"):
            merged.append(old)
    merged.sort(key=lambda it: it.get("date") or "0000", reverse=True)
    merged = merged[: cfg.get("max_items", 60)]

    os.makedirs(out_dir, exist_ok=True)
    write_rss(os.path.join(out_dir, f"{slug}.xml"), cfg, merged, generated)
    dates = [it["date"] for it in merged if it.get("date")]
    result.update(ok=True, items=len(merged), latest=max(dates) if dates else "",
                  undated=sum(1 for it in merged if not it.get("date")), article_fetches=fetches, ms=int((time.time() - started) * 1000))
    log(f"  {cfg['name']}: {len(merged)} items ({result['new']} new, {result['from_page']} on page, "
        f"{result['undated']} undated, latest {result['latest'] or '-'}), {fetches} article fetches")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "scraped_feeds.json"))
    ap.add_argument("--out", required=True, help="directory for the generated <slug>.xml files")
    ap.add_argument("--previous", help="URL or directory of the previously published feeds (merged in)")
    ap.add_argument("--only", help="only build the feed with this slug")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        sites = json.load(fh)
    if args.only:
        sites = [s for s in sites if s["slug"] == args.only]
    generated = datetime.now(timezone.utc)
    log(f"generating {len(sites)} feeds into {args.out}")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(lambda s: build_feed(s, args.out, args.previous, generated), sites):
            results.append(r)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": generated.strftime("%Y-%m-%dT%H:%M:%SZ"), "feeds": results}, fh, indent=1, ensure_ascii=False)
    ok = sum(1 for r in results if r["ok"])
    log(f"done: {ok}/{len(results)} feeds written")
    return 0 if ok or not results else 1


if __name__ == "__main__":
    sys.exit(main())
