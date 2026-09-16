#!/usr/bin/env python3
"""Generate the site list for a Google Programmable Search Engine covering every blog in README.md.

The RSS-based search index (scripts/build_search_index.py) only knows about posts that appeared in a
feed since indexing started. A Google Programmable Search Engine (https://programmablesearchengine.google.com/)
restricted to these sites searches every post ever published. This script writes the two files you
need to set one up:

  cse_sites.txt        one URL pattern per line - paste into "Sites to search" (bulk add)
  cse_annotations.xml  the same list in the Annotations XML format (Search features -> Advanced ->
                       Annotations -> upload), with --label set to your engine's label, e.g. _cse_abc123

Afterwards paste the engine's "cx" id into GOOGLE_CSE_ID in search/index.html.

Usage: scripts/google_cse_sites.py [--readme README.md] [--label _cse_xxxx] [--out-dir .]
"""
from __future__ import annotations

import argparse
import re
from urllib.parse import urlparse
from xml.sax.saxutils import quoteattr

ENTRY_RE = re.compile(r"^\* (.*?):?\s+(https?://\S+)\s*$")
# Hosts shared by many unrelated blogs: restrict to the path instead of the whole host.
SHARED_HOSTS = ("medium.com", "substack.com", "github.io", "blogspot.com", "wordpress.com", "tumblr.com", "dev.to", "hashnode.dev")


def pattern_for(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    if any(host == h or host.endswith("." + h) for h in SHARED_HOSTS) and path:
        # e.g. medium.com/airbnb-engineering/*  or  medium.com/@user/*
        first = "/".join(path.split("/")[1:2])
        return f"{host}/{first}/*" if first else f"{host}/*"
    return f"{host}/*"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--label", default="_include_", help="annotation label, e.g. _cse_abc123 (from the CSE control panel)")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    patterns: list[str] = []
    seen = set()
    with open(args.readme, encoding="utf-8") as fh:
        for line in fh:
            m = ENTRY_RE.match(line)
            if not m:
                continue
            pat = pattern_for(m.group(2))
            if pat not in seen:
                seen.add(pat)
                patterns.append(pat)

    with open(f"{args.out_dir}/cse_sites.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(patterns) + "\n")
    with open(f"{args.out_dir}/cse_annotations.xml", "w", encoding="utf-8") as fh:
        fh.write("<Annotations>\n")
        for pat in patterns:
            fh.write(f"  <Annotation about={quoteattr(pat)} score=\"1\">\n    <Label name={quoteattr(args.label)}/>\n  </Annotation>\n")
        fh.write("</Annotations>\n")
    print(f"{len(patterns)} site patterns written to {args.out_dir}/cse_sites.txt and cse_annotations.xml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
