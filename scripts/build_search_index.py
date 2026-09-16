#!/usr/bin/env python3
"""Build the data files behind the cross-blog search page (search/index.html).

Reads the OPML file, fetches every RSS/Atom feed in it and writes into <out>/data/:

  manifest.json      list of blogs, list of post shards, totals
  posts-<year>.json  posts grouped by publication year, as compact arrays
  status.json        per-feed fetch result (also a handy feed-health report)

Feeds only expose their most recent entries, so the index is *accumulated*:
pass --previous with the path or URL of the previously published data directory
and the old posts are merged in before the new ones are added. Running this on
a schedule therefore builds up an archive over time.

Example (what the GitHub Actions workflow runs):
  scripts/build_search_index.py --opml engineering_blogs.opml --readme README.md \
      --out _site --previous https://<owner>.github.io/engineering-blogs/data/

Requires: pip install -r scripts/requirements.txt
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests

UA = ("Mozilla/5.0 (compatible; engineering-blogs-search/1.0; "
      "+https://github.com/kilimchoi/engineering-blogs)")
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8"
TIMEOUT = (10, 30)
MAX_FEED_BYTES = 8 * 1024 * 1024
TRACKING_PARAMS = ("utm_", "source", "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "mkt_tok", "igshid", "_hsenc", "_hsmi")
POST_FIELDS = ["blog", "title", "url", "date", "excerpt", "tags"]
README_ENTRY_RE = re.compile(r"^\* (.*?):?\s+(https?://\S+)\s*$")
GROUP_RE = re.compile(r"^### (Companies|Individuals/Group Contributors|Products/Technologies)\s*$")
GROUP_SHORT = {"Companies": "Companies", "Individuals/Group Contributors": "Individuals", "Products/Technologies": "Technologies"}
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# ----------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def canonical_url(url: str) -> str:
    """Normalise a post URL so the same article from two fetches dedupes."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if (scheme == "https" and host.endswith(":443")) or (scheme == "http" and host.endswith(":80")):
        host = host.rsplit(":", 1)[0]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not any(k.lower().startswith(t) for t in TRACKING_PARAMS)]
    return urlunparse((scheme, host, path, "", urlencode(query), ""))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def excerpt_of(entry, limit: int) -> str:
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "") or ""
    if not raw:
        raw = entry.get("summary", "") or entry.get("description", "") or ""
    text = strip_html(raw)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def entry_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                d = datetime(*t[:6], tzinfo=timezone.utc).date()
            except (TypeError, ValueError):
                continue
            today = datetime.now(timezone.utc).date()
            if d > today:
                d = today
            if d.year < 1995:
                continue
            return d.isoformat()
    return ""


def entry_tags(entry) -> list[str]:
    seen, out = set(), []
    for t in entry.get("tags", []) or []:
        term = strip_html(t.get("term") or "")
        if not term or len(term) > 40:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= 6:
            break
    return out


def read_json(source: str):
    """Read JSON from a local path or an http(s) URL. Returns None when unavailable."""
    try:
        if source.startswith(("http://", "https://")):
            r = requests.get(source, headers={"User-Agent": UA}, timeout=(10, 60))
            if r.status_code != 200:
                return None
            return r.json()
        with open(source, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log(f"  (previous data not loaded from {source}: {type(exc).__name__})")
        return None


def join_source(base: str, name: str) -> str:
    if base.startswith(("http://", "https://")):
        return urljoin(base if base.endswith("/") else base + "/", name)
    return os.path.join(base, name)


# ----------------------------------------------------------------------------- inputs
def load_blogs(opml_path: str, readme_path: str | None) -> list[dict]:
    groups: dict[str, str] = {}
    if readme_path and os.path.exists(readme_path):
        group = None
        with open(readme_path, encoding="utf-8") as fh:
            for line in fh:
                m = GROUP_RE.match(line)
                if m:
                    group = GROUP_SHORT[m.group(1)]
                    continue
                m = README_ENTRY_RE.match(line)
                if m and group:
                    groups[m.group(2).strip()] = group
                    groups[m.group(1).strip().lower()] = group
    blogs = []
    tree = ET.parse(opml_path)
    for node in tree.iter("outline"):
        if node.get("type") != "rss" or not node.get("xmlUrl"):
            continue
        name = node.get("text") or node.get("title") or node.get("htmlUrl")
        url = node.get("htmlUrl") or ""
        blogs.append({
            "id": len(blogs),
            "name": name,
            "url": url,
            "feed": node.get("xmlUrl"),
            "group": groups.get(url) or groups.get(name.lower()) or "",
        })
    return blogs


def load_previous(base: str | None, blogs: list[dict]) -> tuple[dict, dict]:
    """Return (posts keyed by canonical url, previous status keyed by feed url)."""
    posts: dict[str, dict] = {}
    prev_status: dict[str, dict] = {}
    if not base:
        return posts, prev_status
    manifest = read_json(join_source(base, "manifest.json"))
    if not manifest:
        log("  no previous manifest found; starting a fresh index")
        return posts, prev_status

    # map old blog ids to new blog ids (feed url first, then html url, then name)
    by_feed = {b["feed"]: b["id"] for b in blogs}
    by_url = {b["url"]: b["id"] for b in blogs if b["url"]}
    by_name = {b["name"].lower(): b["id"] for b in blogs}
    id_map: dict[int, int] = {}
    for ob in manifest.get("blogs", []):
        nid = by_feed.get(ob.get("feed"))
        if nid is None:
            nid = by_url.get(ob.get("url"))
        if nid is None:
            nid = by_name.get((ob.get("name") or "").lower())
        if nid is not None:
            id_map[ob["id"]] = nid

    fields = manifest.get("fields", POST_FIELDS)
    for shard in manifest.get("shards", []):
        data = read_json(join_source(base, shard["file"]))
        if not data:
            continue
        for row in data.get("posts", []):
            rec = dict(zip(fields, row))
            nid = id_map.get(rec.get("blog"))
            if nid is None or not rec.get("url"):
                continue
            rec["blog"] = nid
            rec.setdefault("tags", [])
            posts[canonical_url(rec["url"])] = rec
    status = read_json(join_source(base, "status.json")) or {}
    for s in status.get("feeds", []):
        if s.get("feed"):
            prev_status[s["feed"]] = s
    log(f"  loaded {len(posts)} posts from previous index ({len(id_map)}/{len(manifest.get('blogs', []))} blogs matched)")
    return posts, prev_status


# ----------------------------------------------------------------------------- fetching
def fetch_feed(blog: dict, prev: dict | None, excerpt_chars: int) -> dict:
    started = time.time()
    headers = {"User-Agent": UA, "Accept": FEED_ACCEPT}
    if prev and prev.get("ok"):
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("modified"):
            headers["If-Modified-Since"] = prev["modified"]
    result = {"blog": blog["id"], "name": blog["name"], "feed": blog["feed"], "http": None, "ok": False,
              "entries": 0, "latest": "", "error": "", "etag": "", "modified": "", "ms": 0, "posts": []}
    try:
        resp = requests.get(blog["feed"], headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True)
        result["http"] = resp.status_code
        if resp.status_code == 304:
            result.update(ok=True, unchanged=True, etag=prev.get("etag", ""), modified=prev.get("modified", ""))
            result["latest"] = prev.get("latest", "")
            resp.close()
            return result
        body = b""
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > MAX_FEED_BYTES:
                break
        resp.close()
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}"
            return result
        parsed = feedparser.parse(body)
        entries = parsed.get("entries", [])
        if not entries:
            result["error"] = "no entries" + (" (parse error)" if parsed.get("bozo") else "")
            return result
        feed_link = (parsed.get("feed") or {}).get("link") or blog["url"] or blog["feed"]
        posts = []
        for e in entries:
            link = (e.get("link") or "").strip()
            if not link and e.get("id", "").startswith(("http://", "https://")):
                link = e["id"].strip()
            title = strip_html(e.get("title") or "")
            if not link or not title:
                continue
            link = urljoin(feed_link, link)
            posts.append({
                "blog": blog["id"], "title": title[:300], "url": link, "date": entry_date(e),
                "excerpt": excerpt_of(e, excerpt_chars), "tags": entry_tags(e),
            })
        result.update(ok=True, entries=len(posts), posts=posts,
                      etag=resp.headers.get("ETag", ""), modified=resp.headers.get("Last-Modified", ""))
        dates = [p["date"] for p in posts if p["date"]]
        result["latest"] = max(dates) if dates else ""
    except requests.exceptions.SSLError:
        result["error"] = "ssl error"
    except requests.exceptions.Timeout:
        result["error"] = "timeout"
    except requests.exceptions.ConnectionError as exc:
        result["error"] = "dns error" if "resolution" in str(exc).lower() or "getaddrinfo" in str(exc).lower() else "connection error"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        result["ms"] = int((time.time() - started) * 1000)
    return result


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opml", default="engineering_blogs.opml")
    ap.add_argument("--readme", default="README.md", help="used to tag blogs with their README section")
    ap.add_argument("--out", required=True, help="site directory; data is written to <out>/data/")
    ap.add_argument("--previous", help="path or URL of the previously published data/ directory to merge")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--excerpt-chars", type=int, default=240)
    ap.add_argument("--per-blog", type=int, default=200, help="keep at most this many (newest) posts per blog")
    ap.add_argument("--max-age-days", type=int, default=0, help="drop posts older than this (0 = keep everything)")
    ap.add_argument("--min-success", type=float, default=0.25,
                    help="abort (exit 2) if fewer than this fraction of feeds could be fetched")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N feeds (for testing)")
    ap.add_argument("--filter", help="only process blogs whose name contains this text (for testing)")
    args = ap.parse_args()

    blogs = load_blogs(args.opml, args.readme)
    if args.filter:
        blogs = [b for b in blogs if args.filter.lower() in b["name"].lower()]
    if args.limit:
        blogs = blogs[: args.limit]
    for i, b in enumerate(blogs):  # re-number after filtering
        b["id"] = i
    log(f"{len(blogs)} feeds from {args.opml}")

    log("loading previous index...")
    posts, prev_status = load_previous(args.previous, blogs)

    log(f"fetching feeds with {args.workers} workers...")
    started = time.time()
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(fetch_feed, b, prev_status.get(b["feed"]), args.excerpt_chars) for b in blogs]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            results.append(fut.result())
            if i % 50 == 0 or i == len(futs):
                log(f"  {i}/{len(futs)} feeds done ({time.time() - started:.0f}s)")
    results.sort(key=lambda r: r["blog"])

    ok = sum(1 for r in results if r["ok"])
    if blogs and ok < max(1, int(len(blogs) * args.min_success)):
        log(f"ERROR: only {ok}/{len(blogs)} feeds fetched; refusing to publish a broken index")
        return 2

    # merge
    today = datetime.now(timezone.utc).date().isoformat()
    new_count = 0
    for r in results:
        for p in r.pop("posts", []):
            key = canonical_url(p["url"])
            old = posts.get(key)
            if old is None:
                new_count += 1
                posts[key] = p
            else:
                old.update(blog=p["blog"], title=p["title"], url=p["url"],
                           excerpt=p["excerpt"] or old.get("excerpt", ""), tags=p["tags"] or old.get("tags", []))
                if p["date"]:
                    old["date"] = p["date"]
    # caps
    if args.max_age_days:
        cutoff = date.fromordinal(date.today().toordinal() - args.max_age_days).isoformat()
        posts = {k: v for k, v in posts.items() if not v.get("date") or v["date"] >= cutoff}
    by_blog: dict[int, list[dict]] = {}
    for p in posts.values():
        by_blog.setdefault(p["blog"], []).append(p)
    kept: list[dict] = []
    for bid, plist in by_blog.items():
        plist.sort(key=lambda p: p.get("date") or "", reverse=True)
        kept.extend(plist[: args.per_blog])

    # per-blog stats
    for b in blogs:
        b["posts"] = 0
        b["latest"] = ""
    for p in kept:
        b = blogs[p["blog"]]
        b["posts"] += 1
        if p.get("date", "") > b["latest"]:
            b["latest"] = p["date"]

    # write shards
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    for old in os.listdir(data_dir):
        if old.startswith("posts-") and old.endswith(".json"):
            os.remove(os.path.join(data_dir, old))
    shards = []
    by_year: dict[str, list[dict]] = {}
    for p in kept:
        by_year.setdefault(p["date"][:4] if p.get("date") else "undated", []).append(p)
    for year in sorted(by_year, key=lambda y: (y != "undated", y), reverse=True):
        rows = sorted(by_year[year], key=lambda p: p.get("date") or "", reverse=True)
        fname = f"posts-{year}.json"
        payload = {"year": year, "fields": POST_FIELDS,
                   "posts": [[p["blog"], p["title"], p["url"], p.get("date", ""), p.get("excerpt", ""), p.get("tags", [])] for p in rows]}
        with open(os.path.join(data_dir, fname), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        shards.append({"file": fname, "year": year, "count": len(rows), "bytes": os.path.getsize(os.path.join(data_dir, fname))})

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {"generated": generated, "fields": POST_FIELDS, "posts": len(kept), "new_posts": new_count,
                "blogs": blogs, "shards": shards,
                "feeds_ok": ok, "feeds_failed": len(results) - ok}
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, separators=(",", ":"))
    status = {"generated": generated, "feeds": results,
              "summary": {"total": len(results), "ok": ok, "failed": len(results) - ok, "posts": len(kept), "new_posts": new_count}}
    with open(os.path.join(data_dir, "status.json"), "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=1)

    total_bytes = sum(s["bytes"] for s in shards)
    log(f"done: {len(kept)} posts ({new_count} new) from {ok}/{len(blogs)} feeds; "
        f"{len(shards)} shards, {total_bytes / 1024:.0f} KB; {time.time() - started:.0f}s")
    failed = [r for r in results if not r["ok"]]
    if failed:
        log("feeds that failed:")
        for r in sorted(failed, key=lambda r: r["name"].lower()):
            log(f"  {r['name']}: {r['error']} ({r['feed']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
