#!/usr/bin/env python3
"""Step 1: Discover candidate companies for an ICP. No Apify.

Sources (pick with --source, default: places if a key exists, else search):
  places   Google Places API (New) text search. search_terms x locations. 20 places per request,
           up to 3 requests per query. First 1,000 requests each month are free, then $35 per 1,000.
  search   Firecrawl web search. 2 credits per 10 results. Also harvests "top 10 ..." list pages for
           outbound company links (free HTTP fetch), which is where most of the long tail hides.
  both     places then search, merged by domain.
  import   --import-csv file.csv with columns like name/company and website/domain/url. Free.
           This is Instantly's "bring your own companies".

Usage:
  python3 discover.py --icp str-portfolio-managers --run-id <id> --regions new_england
  python3 discover.py --icp str-portfolio-managers --run-id <id> --source search --max-per-search 30
  python3 discover.py --icp str-portfolio-managers --run-id <id> --import-csv my_list.csv

Writes <run>/raw_discovery.json (everything seen) and <run>/companies.json (deduped by domain, coarse filtered).
Resumable: each (term, location, source) is done once per run.
"""
import argparse
import csv
import re
import sys
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import (Ledger, default_run_id, env, is_aggregator, load_icp, normalize_url, read_json,
                    root_domain, run_dir, session, write_json)
from providers import fc_search, place_to_company, places_text_search

LIST_TITLE_RE = re.compile(r"\b(top|best|\d{1,3})\b.*\b(companies|managers|management|agencies|operators|firms|list|guide)\b", re.I)


def coarse_filter(c, icp):
    """Return a reason to drop the candidate, or None."""
    if (c.get("business_status") or "") == "CLOSED_PERMANENTLY":
        return "closed"
    if c.get("country") and c["country"].upper() not in ("US", "USA", "UNITED STATES"):
        return "non-US"
    if not c.get("website"):
        return "no website"
    dom = root_domain(c["website"])
    if not dom or "." not in dom:
        return "bad website"
    if is_aggregator(dom, icp):
        return f"excluded or aggregator domain {dom}"
    cats = " ".join([c.get("category") or ""] + list(c.get("categories") or [])).lower().replace("_", " ")
    protect = [t.lower() for t in icp["search_terms"]]
    for kw in icp["exclude"].get("category_keywords", []):
        if kw in cats and not any(p.split()[0] in cats for p in protect):
            return f"category: {kw}"
    return None


def clean_title(title):
    t = re.split(r"\s[|\-–—:]\s", title or "", 1)[0].strip()
    t = re.sub(r"\b(home|welcome|official site)\b", "", t, flags=re.I).strip(" -|")
    return t[:80] or None


def harvest_list_page(url, icp, limit=60):
    """Fetch a 'top 10 X companies' style page for free and return outbound company domains."""
    try:
        r = session().get(url, timeout=20, allow_redirects=True)
        if r.status_code >= 400 or "html" not in (r.headers.get("content-type") or ""):
            return []
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    src_dom = root_domain(url)
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if not href.startswith("http"):
            continue
        dom = root_domain(href)
        if not dom or dom == src_dom or dom.endswith("." + src_dom) or dom in seen or is_aggregator(dom, icp):
            continue
        if any(x in dom for x in ("amazon.", "apple.", "microsoft.", "adobe.", "shopify.", "hubspot.", "mailchimp.")):
            continue
        seen.add(dom)
        out.append({"name": clean_title(a.get_text(" ", strip=True)) or dom, "website": normalize_url(href),
                    "source": f"list:{url}"})
        if len(out) >= limit:
            break
    return out


def discover_places(icp, locations, out, raw, ledger, max_per_search):
    done = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") == "places"}
    for loc in locations:
        for term in icp["search_terms"]:
            if (term, loc) in done:
                continue
            q = f"{term} in {loc}"
            print(f"[places] {q}", flush=True)
            n = 0
            try:
                for p in places_text_search(q, ledger, max_results=max_per_search):
                    c = place_to_company(p)
                    c.update({"_source": "places", "_term": term, "_location": loc})
                    raw.append(c)
                    n += 1
            except Exception as e:
                print(f"  ERROR: {e}")
                if "403" in str(e) or "API key" in str(e):
                    sys.exit("Places API refused the key. Check GOOGLE_PLACES_API_KEY and that 'Places API (New)' is enabled.")
                continue
            # mark the query done even if it returned nothing, so reruns skip it
            raw.append({"_source": "places", "_term": term, "_location": loc, "_marker": True})
            write_json(out / "raw_discovery.json", raw)
            print(f"  {n} places")


def discover_search(icp, locations, out, raw, ledger, max_per_search, harvest):
    done = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") == "search"}
    per_query = min(max_per_search, 20)
    for loc in locations:
        for term in icp["search_terms"]:
            if (term, loc) in done:
                continue
            q = f"{term} {loc.replace(', USA', '')}"
            print(f"[search] {q}", flush=True)
            try:
                results = fc_search(q, ledger, limit=per_query)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue
            n_direct, n_list = 0, 0
            for res in results:
                dom = root_domain(res["url"])
                looks_like_list = bool(LIST_TITLE_RE.search(res["title"]))
                if is_aggregator(dom, icp) or looks_like_list:
                    if harvest and (looks_like_list or dom.endswith((".org", ".gov")) is False):
                        for c in harvest_list_page(res["url"], icp):
                            c.update({"_source": "search", "_term": term, "_location": loc, "_via": "list_page"})
                            raw.append(c)
                            n_list += 1
                    continue
                raw.append({"name": clean_title(res["title"]) or dom, "website": normalize_url(res["url"]),
                            "snippet": res["description"][:300], "_source": "search", "_term": term, "_location": loc})
                n_direct += 1
            raw.append({"_source": "search", "_term": term, "_location": loc, "_marker": True})
            write_json(out / "raw_discovery.json", raw)
            print(f"  {n_direct} direct, {n_list} harvested from list pages")


def discover_import(path, raw):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{path} is empty")
    cols = {c.lower().strip(): c for c in rows[0].keys()}
    name_col = next((cols[c] for c in ("name", "company", "company name", "company_name", "business") if c in cols), None)
    web_col = next((cols[c] for c in ("website", "domain", "url", "web", "site") if c in cols), None)
    if not web_col:
        sys.exit(f"{path} needs a website/domain/url column. Columns: {list(cols)}")
    n = 0
    for r in rows:
        web = (r.get(web_col) or "").strip()
        if not web:
            continue
        raw.append({"name": (r.get(name_col) or "").strip() if name_col else root_domain(web),
                    "website": normalize_url(web), "_source": "import",
                    "city": r.get(cols.get("city", ""), "") if "city" in cols else None,
                    "state": r.get(cols.get("state", ""), "") if "state" in cols else None,
                    "phone": r.get(cols.get("phone", ""), "") if "phone" in cols else None})
        n += 1
    print(f"[import] {n} rows from {path}")


def merge(raw, icp):
    companies, dropped = {}, {}
    for c in raw:
        if c.get("_marker"):
            continue
        reason = coarse_filter(c, icp)
        dom = root_domain(c.get("website"))
        if reason:
            dropped[dom or c.get("name") or "?"] = reason
            continue
        src = c.get("_source")
        if dom not in companies:
            companies[dom] = {
                "domain": dom, "name": c.get("name") or dom, "website": normalize_url(c["website"]),
                "phone": c.get("phone"), "address": c.get("address"), "city": c.get("city"), "state": c.get("state"),
                "postal_code": c.get("postal_code"), "category": c.get("category"), "maps_url": c.get("maps_url"),
                "rating": c.get("rating"), "reviews": c.get("reviews"), "snippet": c.get("snippet"),
                "sources": [src], "search_locations": [c.get("_location")] if c.get("_location") else [],
                "search_terms": [c.get("_term")] if c.get("_term") else [], "hits": 1,
            }
        else:
            k = companies[dom]
            k["hits"] += 1
            for fld in ("phone", "address", "city", "state", "postal_code", "category", "maps_url", "rating", "reviews", "snippet"):
                if not k.get(fld) and c.get(fld):
                    k[fld] = c[fld]
            if src not in k["sources"]:
                k["sources"].append(src)
            if c.get("_location") and c["_location"] not in k["search_locations"]:
                k["search_locations"].append(c["_location"])
            if c.get("_term") and c["_term"] not in k["search_terms"]:
                k["search_terms"].append(c["_term"])
            if (c.get("name") or "").lower() != dom and len(c.get("name") or "") > len(k["name"] or "") and src == "places":
                k["name"] = c["name"]
    return list(companies.values()), dropped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--regions", help="comma list of keys under icp.locations (default: all)")
    ap.add_argument("--states", nargs="*", help="override locations, e.g. 'Maine, USA'")
    ap.add_argument("--source", choices=["places", "search", "both", "import"], default=None)
    ap.add_argument("--max-per-search", type=int)
    ap.add_argument("--import-csv")
    ap.add_argument("--no-harvest", action="store_true", help="do not follow 'top 10' list pages")
    args = ap.parse_args(argv)

    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    ledger = Ledger(out)
    raw = read_json(out / "raw_discovery.json", []) or []
    max_per = args.max_per_search or icp.get("max_results_per_search", 60)

    if args.states:
        locations = args.states
    else:
        keys = [k.strip() for k in (args.regions or "").split(",") if k.strip()] or list(icp["locations"])
        locations = []
        for k in keys:
            if k not in icp["locations"]:
                sys.exit(f"Unknown region '{k}'. Options: {list(icp['locations'])}")
            locations += icp["locations"][k]

    source = args.source
    if args.import_csv:
        source = source or "import"
    if source is None:
        source = "places" if env("GOOGLE_PLACES_API_KEY") else ("search" if env("FIRECRAWL_API_KEY") else None)
        if source is None:
            sys.exit("No discovery source available. Set GOOGLE_PLACES_API_KEY (preferred, free tier) or "
                     "FIRECRAWL_API_KEY in .env, or pass --import-csv.")
        print(f"[discover] source auto-selected: {source}")

    if source in ("places", "both"):
        if not env("GOOGLE_PLACES_API_KEY"):
            sys.exit("GOOGLE_PLACES_API_KEY not set. See references/setup.md.")
        discover_places(icp, locations, out, raw, ledger, max_per)
    if source in ("search", "both"):
        if not env("FIRECRAWL_API_KEY"):
            sys.exit("FIRECRAWL_API_KEY not set.")
        discover_search(icp, locations, out, raw, ledger, max_per, harvest=not args.no_harvest)
    if args.import_csv:
        discover_import(args.import_csv, raw)
        write_json(out / "raw_discovery.json", raw)

    companies, dropped = merge(raw, icp)
    write_json(out / "companies.json", companies)
    write_json(out / "discover_dropped.json", dropped)
    by_src = {}
    for c in companies:
        for s in c["sources"]:
            by_src[s] = by_src.get(s, 0) + 1
    print(f"\n{len(companies)} unique companies kept ({by_src}), {len(dropped)} dropped. Run folder: {out}")
    print(ledger.summary())
    return run_id


if __name__ == "__main__":
    main()
