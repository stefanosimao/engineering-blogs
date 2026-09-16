#!/usr/bin/env python3
"""Check that every blog listed in README.md is still reachable.

For each `* Name URL` line in the README the script fetches the page with a
browser-like User-Agent, follows redirects and classifies the result:

  ok                reachable (2xx)
  moved             reachable, but the final URL is on a different host
  redirect_to_root  reachable, but the blog path collapsed to the site root
                    (the blog section may have been removed - review by hand)
  blocked           401/403/412/429 that looks like bot protection (probably fine
                    for humans; verified via the RSS feed when possible)
  parked            2xx but the page is a domain-parking / "for sale" page
  soft_404          2xx but the page is a hosting provider's "nothing here" page
  not_found         404 / 410
  server_error      5xx after a retry
  ssl_error / dns_error / connection_error / timeout / redirect_loop

If an OPML file is given, the matching RSS feed is fetched too. A working feed
is strong evidence the blog is alive even when the HTML page blocks robots, and
the newest entry date shows how stale the blog is.

Usage:
  scripts/check_links.py [--readme README.md] [--opml engineering_blogs.opml]
                         [--workers 24] [--json report.json] [--markdown report.md]
                         [--only-problems]

Requires: requests (pip install -r scripts/requirements.txt). feedparser is optional
but recommended for feed dates.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3

try:
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover
    feedparser = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENTRY_RE = re.compile(r"^\* (.*?):?\s+(https?://\S+)\s*$")
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
UA_PLAIN = "engineering-blogs-link-check/1.0 (+https://github.com/kilimchoi/engineering-blogs)"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BODY = 256 * 1024
TIMEOUT = (10, 25)

PARKED_HOSTS = (
    "sedoparking.com", "sedo.com", "hugedomains.com", "dan.com", "afternic.com",
    "parkingcrew.net", "bodis.com", "above.com", "undeveloped.com", "namesilo.com",
    "domainmarket.com", "buydomains.com", "godaddy.com/forsale", "porkbun.com/checkout",
    "squadhelp.com", "atom.com", "brandbucket.com", "saw.com", "domain.com/forsale",
)
PARKED_MARKERS = (
    "this domain is for sale", "domain is for sale", "buy this domain",
    "domain may be for sale", "purchase this domain", "make an offer on this domain",
    "make offer on this domain", "get this domain", "sedoparking", "parkingcrew",
    "hugedomains", "afternic", "domain parking", "parked free", "this domain has expired",
    "domain has expired", "domain name has expired", "the domain name is for sale",
    "inquire about this domain", "acquire this domain", "is available for purchase",
)
SOFT_404_MARKERS = (
    "site not found", "there isn't a github pages site here", "blog not found",
    "blog you were looking for does not exist", "no longer available",
    "this site has been archived or suspended", "the authors have deleted this site",
    "there's nothing here", "there is nothing here", "nothing here yet", "no such app",
    "account suspended", "website expired", "domain not found",
    "not connected to a website", "this site can't be reached", "page not found",
    "404 not found", "this page could not be found", "we couldn't find that page",
)
SOFT_404_TITLE_MARKERS = (
    "welcome to nginx", "apache2 ubuntu default page", "apache2 debian default page",
    "iis windows server", "index of /", "coming soon", "under construction",
    "default web site page", "it works", "site not found", "404", "not found",
    "page not found", "website expired", "account suspended", "domain expired",
)


@dataclass
class Result:
    name: str
    url: str
    status: str = ""
    http_status: int | None = None
    final_url: str = ""
    redirects: int = 0
    title: str = ""
    server: str = ""
    error: str = ""
    note: str = ""
    feed_url: str = ""
    feed_http_status: int | None = None
    feed_ok: bool | None = None
    feed_entries: int | None = None
    feed_latest: str = ""
    feed_error: str = ""
    verdict: str = ""  # reachable | check | dead


def parse_readme(path: str) -> list[tuple[str, str]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = ENTRY_RE.match(line)
            if m:
                out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def parse_opml(path: str) -> dict[str, str]:
    feeds: dict[str, str] = {}
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return feeds
    for node in tree.iter("outline"):
        if node.get("type") == "rss" and node.get("htmlUrl") and node.get("xmlUrl"):
            feeds[node.get("htmlUrl")] = node.get("xmlUrl")
    return feeds


def norm_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_error(exc: Exception) -> tuple[str, str]:
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl_error", msg
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect_loop", msg
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout", msg
    if isinstance(exc, requests.exceptions.ConnectionError):
        if any(s in low for s in ("name or service not known", "nodename nor servname",
                                  "nameresolutionerror", "temporary failure in name resolution",
                                  "no address associated", "getaddrinfo failed")):
            return "dns_error", msg
        return "connection_error", msg
    return "error", msg


def fetch(session: requests.Session, url: str, ua: str) -> tuple[requests.Response, bytes]:
    headers = dict(HEADERS, **{"User-Agent": ua})
    resp = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True)
    body = b""
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            body += chunk
            if len(body) >= MAX_BODY:
                break
    except Exception:  # body errors are not fatal for classification
        pass
    finally:
        resp.close()
    return resp, body


def extract_title(body: bytes) -> str:
    text = body.decode("utf-8", errors="ignore")
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200]


def check_page(name: str, url: str) -> Result:
    r = Result(name=name, url=url)
    session = requests.Session()
    session.max_redirects = 15
    resp = body = None
    attempts = 0
    ua = UA_BROWSER
    while attempts < 3:
        attempts += 1
        try:
            resp, body = fetch(session, url, ua)
        except Exception as exc:  # noqa: BLE001
            r.status, r.error = classify_error(exc)
            if r.status in ("timeout", "connection_error") and attempts < 2:
                time.sleep(3)
                continue
            return r
        # Retry bot-protection / 5xx answers once with a plain UA
        if resp.status_code in (403, 429, 503, 500, 502, 504) and attempts < 2:
            ua = UA_PLAIN
            time.sleep(2)
            continue
        break

    assert resp is not None and body is not None
    r.http_status = resp.status_code
    r.final_url = resp.url
    r.redirects = len(resp.history)
    r.server = resp.headers.get("server", "")[:60]
    r.title = extract_title(body)
    low = body.decode("utf-8", errors="ignore").lower()
    title_low = r.title.lower()
    final = urlparse(r.final_url)
    orig = urlparse(url)

    if resp.status_code in (404, 410):
        r.status = "not_found"
    elif resp.status_code >= 500:
        r.status = "server_error"
    elif resp.status_code in (401, 403, 412, 429, 451):
        r.status = "blocked"
        r.note = "bot protection?" if any(k in r.server.lower() for k in ("cloudflare", "akamai", "vercel", "imperva", "incapsula")) or "cloudflare" in low or "just a moment" in title_low else ""
    elif 200 <= resp.status_code < 400:
        if any(h in final.netloc.lower() or h in r.final_url.lower() for h in PARKED_HOSTS) or any(m in low for m in PARKED_MARKERS):
            r.status = "parked"
        elif any(m in title_low for m in SOFT_404_TITLE_MARKERS) or any(m in low[:20000] for m in SOFT_404_MARKERS if m not in ("page not found", "404 not found", "no longer available")):
            r.status = "soft_404"
        elif norm_host(final.netloc) != norm_host(orig.netloc):
            r.status = "moved"
            r.note = f"now on {final.netloc}"
        elif orig.path.strip("/") and not final.path.strip("/"):
            r.status = "redirect_to_root"
            r.note = "blog path redirects to site root"
        else:
            r.status = "ok"
            if len(body) < 400:
                r.note = "very small page"
    else:
        r.status = f"http_{resp.status_code}"
    return r


def check_feed(r: Result) -> None:
    if not r.feed_url:
        return
    session = requests.Session()
    try:
        resp, body = fetch(session, r.feed_url, UA_BROWSER)
    except Exception as exc:  # noqa: BLE001
        r.feed_error = classify_error(exc)[0]
        r.feed_ok = False
        return
    r.feed_http_status = resp.status_code
    if resp.status_code != 200:
        r.feed_ok = False
        return
    if feedparser is None:
        r.feed_ok = b"<rss" in body or b"<feed" in body or b"<rdf" in body
        return
    parsed = feedparser.parse(body)
    entries = parsed.get("entries", [])
    r.feed_entries = len(entries)
    r.feed_ok = len(entries) > 0 or (not parsed.get("bozo") and bool(parsed.get("feed")))
    latest = None
    for e in entries:
        for key in ("published_parsed", "updated_parsed", "created_parsed"):
            t = e.get(key)
            if t:
                try:
                    dt = datetime(*t[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if latest is None or dt > latest:
                    latest = dt
                break
    if latest:
        r.feed_latest = latest.date().isoformat()


def check_entry(name: str, url: str, feed_url: str) -> Result:
    r = check_page(name, url)
    r.feed_url = feed_url
    check_feed(r)
    # Overall verdict
    if r.status in ("ok", "moved", "redirect_to_root"):
        r.verdict = "reachable" if r.status == "ok" else "check"
    elif r.status == "blocked":
        r.verdict = "reachable" if r.feed_ok else "check"
    elif r.status in ("timeout", "server_error", "connection_error", "ssl_error"):
        r.verdict = "reachable" if r.feed_ok else "check"
    else:  # not_found, parked, soft_404, dns_error, redirect_loop, http_xxx
        r.verdict = "check" if r.feed_ok else "dead"
    return r


def to_markdown(results: list[Result], only_problems: bool) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines = [f"# Link check report ({datetime.now(timezone.utc).date().isoformat()})", ""]
    lines.append(f"Checked {len(results)} blogs.")
    lines.append("")
    lines.append("| status | count |")
    lines.append("|---|---:|")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    verdict_counts = {}
    for r in results:
        verdict_counts[r.verdict] = verdict_counts.get(r.verdict, 0) + 1
    lines.append("Verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(verdict_counts.items())))
    lines.append("")
    lines.append("| verdict | status | blog | url | http | final url | feed | latest post | note |")
    lines.append("|---|---|---|---|---:|---|---|---|---|")
    order = {"dead": 0, "check": 1, "reachable": 2}
    for r in sorted(results, key=lambda x: (order.get(x.verdict, 9), x.status, x.name.lower())):
        if only_problems and r.verdict == "reachable" and r.status == "ok":
            continue
        feed = "-"
        if r.feed_url:
            feed = "ok" if r.feed_ok else f"fail({r.feed_http_status or r.feed_error})"
        final = r.final_url if r.final_url and r.final_url.rstrip('/') != r.url.rstrip('/') else ""
        note = r.note or r.error[:80]
        lines.append(f"| {r.verdict} | {r.status} | {r.name} | {r.url} | {r.http_status or ''} | {final} | {feed} | {r.feed_latest} | {note} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--opml", default="engineering_blogs.opml")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--markdown", dest="md_out")
    ap.add_argument("--only-problems", action="store_true")
    ap.add_argument("--filter", help="only check blogs whose name or URL contains this text")
    args = ap.parse_args()

    entries = parse_readme(args.readme)
    feeds = parse_opml(args.opml) if args.opml else {}
    if args.filter:
        f = args.filter.lower()
        entries = [e for e in entries if f in e[0].lower() or f in e[1].lower()]
    print(f"Checking {len(entries)} blogs with {args.workers} workers...", file=sys.stderr)

    results: list[Result] = []
    started = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(check_entry, n, u, feeds.get(u, "")): (n, u) for n, u in entries}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if i % 25 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)} done ({time.time() - started:.0f}s)", file=sys.stderr)

    results.sort(key=lambda r: r.name.lower())
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump([asdict(r) for r in results], fh, indent=1, ensure_ascii=False)
    md = to_markdown(results, args.only_problems)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    else:
        print(md)
    problems = sum(1 for r in results if r.verdict != "reachable")
    print(f"Done: {len(results)} checked, {problems} need attention.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
