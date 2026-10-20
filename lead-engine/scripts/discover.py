#!/usr/bin/env python3
"""Step 1: Discover candidate companies for an ICP.

Sources (pick with --source):
  places   Google Places API (New) text search, search_terms x the locations YOU name. Places content is
           used only in memory to decide which company websites to visit. The company's own website is
           then fetched (free) and is the source of every field that is stored: domain, name, phone,
           address. Nothing from Places is written to disk except the place_id, which Google's Places
           API Policies exempt from the caching restrictions. No ratings, review counts, Maps URLs,
           categories or Places-sourced names/phones/addresses are ever persisted.
  search   Firecrawl web search (2 credits per 10 results). Also harvests "top 10 ..." list pages for
           outbound company links with a free HTTP fetch.
  both     places then search, merged by domain.
  import   --import-csv file.csv with name/company and website/domain/url columns. Free, no region needed.

Spend control: API sources require an explicit --regions or --states. The script prints the query
list, the request ceiling and the list-price cost estimate, then stops unless --confirm is passed.
--max-requests is a hard per-run cap (default 100) enforced inside the provider calls.

Usage:
  python3 discover.py --icp inns --run-id <id> --states "Vermont, USA"            # prints the plan, no calls
  python3 discover.py --icp inns --run-id <id> --states "Vermont, USA" --confirm  # runs it
  python3 discover.py --icp inns --run-id <id> --import-csv my_list.csv            # no region, no API

Writes <run>/raw_discovery.json (per-source candidates, resolved from company websites) and
<run>/companies.json (deduped by domain, coarse filtered, with per-field source attribution).
Resumable: each (term, location, source) is done once per run.
"""
import argparse
import concurrent.futures as cf
import csv
import math
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import (BudgetExceeded, Ledger, RequestBudget, UNIT_COST_USD, default_run_id, env, is_aggregator,
                    load_icp, normalize_url, read_json, root_domain, run_dir, session, write_json)

LIST_TITLE_RE = re.compile(r"\b(top|best|\d{1,3})\b.*\b(companies|managers|management|agencies|operators|firms|list|guide)\b", re.I)
API_SOURCES = ("places", "search", "both")
PAGES_PER_PLACES_QUERY = 3  # Google returns at most 3 pages of 20


def coarse_filter(c, icp):
    """Return a reason to drop the candidate, or None. Works on transient Places dicts and on resolved records."""
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
        return "excluded or aggregator domain"
    cats = " ".join([c.get("category") or ""] + list(c.get("categories") or [])).lower().replace("_", " ")
    protect = [t.lower() for t in icp["search_terms"]]
    for kw in icp["exclude"].get("category_keywords", []):
        if kw in cats and not any(p.split()[0] in cats for p in protect):
            return "category keyword"
    return None


def clean_title(title):
    t = re.split(r"\s[|\-–—:]\s", title or "", 1)[0].strip()
    t = re.sub(r"\b(home|welcome|official site)\b", "", t, flags=re.I).strip(" -|")
    return t[:80] or None


def resolve_site(url, timeout=15):
    """Fetch the company's own homepage and derive the durable identity fields from it.

    This is the independently permitted source for everything stored about a company found through a
    paid index: the business's public website. Returns (record, None) or (None, why)."""
    from reader import fetch, parse_page  # free HTTP fetch, same parser the read step uses
    final, html, status, err = fetch(url, timeout=timeout)
    if not html:
        return None, err or f"HTTP {status}"
    dom = root_domain(final)
    if not dom or "." not in dom:
        return None, "bad final domain"
    pg = parse_page(final, html, dom)
    jl = pg["jsonld"]
    addr = jl.get("address") or {}
    name = jl.get("org_name") or pg.get("site_name") or clean_title(pg["title"]) or dom
    return {
        "domain": dom, "website": normalize_url(final), "name": name,
        "phone": (pg["phones"] or [None])[0], "city": addr.get("city"), "state": addr.get("state"),
        "postal_code": addr.get("postal_code"), "site_title": (pg["title"] or "")[:120],
        "attribution": {"name": "company website", "phone": "company website", "address": "company website (JSON-LD)",
                        "domain": "company website (final URL after redirects)"},
    }, None


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
                    "source": f"list:{url}", "attribution": {"name": f"anchor text on {src_dom}", "domain": f"link on {src_dom}"}})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ plan / spend control
def build_plan(icp, locations, source, max_per_search, raw, max_requests):
    terms = icp["search_terms"]
    done_places = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") in ("places", "website_via_places") and r.get("_marker")}
    done_search = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") == "search" and r.get("_marker")}
    queries = [(t, l) for l in locations for t in terms]
    plan = {"queries": queries, "places_requests": 0, "search_requests": 0, "firecrawl_credits": 0}
    if source in ("places", "both"):
        pending = [q for q in queries if q not in done_places]
        pages = min(PAGES_PER_PLACES_QUERY, max(1, math.ceil(max_per_search / 20)))
        plan["places_requests"] = len(pending) * pages
        plan["places_pending_queries"] = len(pending)
    if source in ("search", "both"):
        pending = [q for q in queries if q not in done_search]
        per_query = min(max_per_search, 20)
        plan["search_requests"] = len(pending)
        plan["firecrawl_credits"] = len(pending) * 2 * math.ceil(per_query / 10)
        plan["search_pending_queries"] = len(pending)
    total = plan["places_requests"] + plan["search_requests"]
    plan["capped_requests"] = min(total, max_requests)
    plan["cost_places_usd"] = min(plan["places_requests"], max_requests) * UNIT_COST_USD["places_request"]
    plan["cost_firecrawl_usd"] = plan["firecrawl_credits"] * UNIT_COST_USD["firecrawl_credit"]
    return plan


def print_plan(plan, source, locations, max_requests):
    print("\nDiscovery plan")
    print(f"  source: {source}")
    print(f"  locations ({len(locations)}): {', '.join(locations[:8])}{' ...' if len(locations) > 8 else ''}")
    print(f"  queries: {len(plan['queries'])} (search terms x locations)")
    if plan["places_requests"]:
        print(f"  Google Places requests, worst case: {plan['places_requests']} "
              f"(~${plan['cost_places_usd']:.2f} at the $35 per 1,000 Text Search Enterprise list price)")
    if plan["search_requests"]:
        print(f"  Firecrawl searches: {plan['search_requests']} = {plan['firecrawl_credits']} credits "
              f"(~${plan['cost_firecrawl_usd']:.2f} at Hobby-plan list price)")
    print(f"  hard cap this run: {max_requests} paid requests (--max-requests). Ceiling after cap: {plan['capped_requests']}.")
    print("  Any monthly allowance on your Google or Firecrawl account may cover some of this. It is not assumed here;")
    print("  check the provider console before and after the run.")


# ------------------------------------------------------------------ sources
def discover_places(icp, locations, out, raw, ledger, max_per_search, budget, workers=6):
    done = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") in ("places", "website_via_places") and r.get("_marker")}
    from providers import place_to_company, places_text_search
    for loc in locations:
        for term in icp["search_terms"]:
            if (term, loc) in done:
                continue
            q = f"{term} in {loc}"
            print(f"[places] {q}", flush=True)
            transient, drop_counts = [], {}
            try:
                for p in places_text_search(q, ledger, max_results=max_per_search, budget=budget):
                    tmp = place_to_company(p)  # Places content, in memory only
                    reason = coarse_filter(tmp, icp)
                    if reason:
                        drop_counts[reason] = drop_counts.get(reason, 0) + 1
                        continue
                    transient.append((tmp.get("place_id"), tmp.get("website")))
            except BudgetExceeded as e:
                print(f"  STOP: {e}")
                write_json(out / "raw_discovery.json", raw)
                return False
            except Exception as e:
                print(f"  ERROR: {e}")
                if "403" in str(e) or "API key" in str(e):
                    sys.exit("Places API refused the key. Check GOOGLE_PLACES_API_KEY and that 'Places API (New)' is enabled.")
                continue
            # Resolve each candidate against its own website; only website-derived fields are kept.
            resolved, unresolved = 0, 0
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for pid, (rec, err) in zip([t[0] for t in transient], ex.map(lambda t: resolve_site(t[1]), transient)):
                    if rec:
                        rec.update({"_source": "website_via_places", "place_id": pid, "_term": term, "_location": loc})
                        raw.append(rec)
                        resolved += 1
                    else:
                        raw.append({"_source": "website_via_places", "place_id": pid, "_term": term, "_location": loc,
                                    "_unresolved": (err or "")[:60]})
                        unresolved += 1
            raw.append({"_source": "website_via_places", "_term": term, "_location": loc, "_marker": True,
                        "_dropped_in_memory": drop_counts})
            write_json(out / "raw_discovery.json", raw)
            print(f"  {resolved} websites resolved, {unresolved} unreachable, dropped before visiting: {drop_counts or 0}")
    return True


def discover_search(icp, locations, out, raw, ledger, max_per_search, harvest, budget):
    from providers import fc_search
    done = {(r.get("_term"), r.get("_location")) for r in raw if r.get("_source") == "search" and r.get("_marker")}
    per_query = min(max_per_search, 20)
    for loc in locations:
        for term in icp["search_terms"]:
            if (term, loc) in done:
                continue
            q = f"{term} {loc.replace(', USA', '')}"
            print(f"[search] {q}", flush=True)
            try:
                results = fc_search(q, ledger, limit=per_query, budget=budget)
            except BudgetExceeded as e:
                print(f"  STOP: {e}")
                write_json(out / "raw_discovery.json", raw)
                return False
            except Exception as e:
                print(f"  ERROR: {e}")
                continue
            n_direct, n_list = 0, 0
            for res in results:
                dom = root_domain(res["url"])
                looks_like_list = bool(LIST_TITLE_RE.search(res["title"]))
                if is_aggregator(dom, icp) or looks_like_list:
                    if harvest:
                        for c in harvest_list_page(res["url"], icp):
                            c.update({"_source": "search", "_term": term, "_location": loc, "_via": "list_page"})
                            raw.append(c)
                            n_list += 1
                    continue
                raw.append({"name": clean_title(res["title"]) or dom, "website": normalize_url(res["url"]),
                            "snippet": res["description"][:300], "_source": "search", "_term": term, "_location": loc,
                            "attribution": {"name": "web search result title", "domain": "web search result URL"}})
                n_direct += 1
            raw.append({"_source": "search", "_term": term, "_location": loc, "_marker": True})
            write_json(out / "raw_discovery.json", raw)
            print(f"  {n_direct} direct, {n_list} harvested from list pages")
    return True


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
                    "city": r.get(cols["city"]) if "city" in cols else None,
                    "state": r.get(cols["state"]) if "state" in cols else None,
                    "phone": r.get(cols["phone"]) if "phone" in cols else None,
                    "attribution": {"name": f"import {path}", "domain": f"import {path}"}})
        n += 1
    print(f"[import] {n} rows from {path}")


# ------------------------------------------------------------------ merge
PERSISTED_FIELDS = ("phone", "city", "state", "postal_code", "snippet", "site_title", "place_id")


def merge(raw, icp):
    companies, dropped = {}, {}
    for c in raw:
        if c.get("_marker") or c.get("_unresolved"):
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
                "phone": c.get("phone"), "city": c.get("city"), "state": c.get("state"), "postal_code": c.get("postal_code"),
                "snippet": c.get("snippet"), "site_title": c.get("site_title"), "place_id": c.get("place_id"),
                "sources": [src], "attribution": dict(c.get("attribution") or {}),
                "search_locations": [c["_location"]] if c.get("_location") else [],
                "search_terms": [c["_term"]] if c.get("_term") else [], "hits": 1,
            }
        else:
            k = companies[dom]
            k["hits"] += 1
            for fld in PERSISTED_FIELDS:
                if not k.get(fld) and c.get(fld):
                    k[fld] = c[fld]
                    if fld in ("phone", "city", "state", "postal_code"):
                        k["attribution"][fld] = (c.get("attribution") or {}).get(fld if fld == "phone" else "address", src)
            if src not in k["sources"]:
                k["sources"].append(src)
            if c.get("_location") and c["_location"] not in k["search_locations"]:
                k["search_locations"].append(c["_location"])
            if c.get("_term") and c["_term"] not in k["search_terms"]:
                k["search_terms"].append(c["_term"])
            if src == "website_via_places" and c.get("name"):
                k["name"], k["attribution"]["name"] = c["name"], "company website"
    return list(companies.values()), dropped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--regions", help="comma list of keys under icp.locations. Required for API discovery; no default.")
    ap.add_argument("--states", nargs="*", help="explicit locations instead of --regions, e.g. 'Vermont, USA' 'Stowe, VT'")
    ap.add_argument("--source", choices=["places", "search", "both", "import"], default=None)
    ap.add_argument("--max-per-search", type=int)
    ap.add_argument("--max-requests", type=int, default=100, help="hard cap on paid API requests this run (default 100)")
    ap.add_argument("--confirm", action="store_true", help="actually make the paid API calls shown in the plan")
    ap.add_argument("--import-csv")
    ap.add_argument("--no-harvest", action="store_true", help="do not follow 'top 10' list pages")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    ledger = Ledger(out)
    raw = read_json(out / "raw_discovery.json", []) or []
    max_per = args.max_per_search or icp.get("max_results_per_search", 60)

    source = args.source
    if args.import_csv and source is None:
        source = "import"
    if source is None:
        sys.exit("Pick a source: --source places | search | both, or --import-csv file.csv. "
                 "Nothing is auto-selected because API sources cost money.")

    locations = []
    if source in API_SOURCES:
        if args.states:
            locations = args.states
        elif args.regions:
            keys = [k.strip() for k in args.regions.split(",") if k.strip()]
            for k in keys:
                if k not in icp["locations"]:
                    sys.exit(f"Unknown region '{k}'. Options: {list(icp['locations'])}")
                locations += icp["locations"][k]
        if not locations:
            sys.exit("API discovery needs an explicit region or state list. Nikki names the region per batch.\n"
                     f"  --regions one of {list(icp['locations'])}\n  or --states \"Vermont, USA\" \"Stowe, VT\"")
        if source in ("places", "both") and not env("GOOGLE_PLACES_API_KEY"):
            sys.exit("GOOGLE_PLACES_API_KEY not set. See references/setup.md.")
        if source in ("search", "both") and not env("FIRECRAWL_API_KEY"):
            sys.exit("FIRECRAWL_API_KEY not set.")
        plan = build_plan(icp, locations, source, max_per, raw, args.max_requests)
        print_plan(plan, source, locations, args.max_requests)
        if plan["capped_requests"] == 0:
            print("  nothing pending (all queries already done in this run)")
        elif not args.confirm:
            print("\nNo API call was made. Rerun with --confirm to execute this plan, or adjust --regions/--states/--max-requests.")
            return run_id
        budget = RequestBudget(args.max_requests)
        if source in ("places", "both"):
            discover_places(icp, locations, out, raw, ledger, max_per, budget, workers=args.workers)
        if source in ("search", "both"):
            discover_search(icp, locations, out, raw, ledger, max_per, harvest=not args.no_harvest, budget=budget)
        print(f"\npaid requests used this invocation: {budget.used} of {budget.max}")

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
