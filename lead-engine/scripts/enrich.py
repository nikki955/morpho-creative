#!/usr/bin/env python3
"""Step 4: Fill the gaps cheaply, only for companies that already passed qualification.

Two jobs, both optional:

  --find-people   For rows missing a named decision maker: 1 Firecrawl search per company
                  ("<company>" <city> owner OR founder, 2 credits), plus an optional LinkedIn-scoped
                  search (--linkedin, 2 more credits). Snippets are parsed for "Name, Title" and
                  "Name - Title - Company | LinkedIn" shapes. Candidates land in
                  <run>/enrichment_candidates.json for review. A LinkedIn hit whose headline names
                  the company and carries a primary title is auto-promoted into enrichment.json
                  (name and title only, never an email).

  --verify-size   For rows in 'review': walk the listing index page and its pagination for free,
                  counting distinct listing links. Writes size + evidence into enrichment.json when
                  the count lands anywhere plausible (>= 3).

Then rerun qualify. Nothing here writes to the CRM.

Usage:
  python3 enrich.py --icp str-portfolio-managers --run-id <id> --verify-size
  python3 enrich.py --icp str-portfolio-managers --run-id <id> --find-people --max 30 [--linkedin]
"""
import argparse
import csv
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import Ledger, default_run_id, env, load_icp, read_json, root_domain, run_dir, split_name, title_tier, write_json
from reader import PEOPLE_PATTERNS, BAD_NAME_WORDS, count_listing_urls, fetch

LI_RE = re.compile(r"^(?P<name>[A-Z][A-Za-z'\-\.]+(?: [A-Z][A-Za-z'\-\.]+){1,3})\s*[-–|]\s*(?P<title>[^-–|]{3,60}?)\s*[-–|]\s*(?P<company>[^-–|]{2,80}?)\s*(?:[-–|]\s*LinkedIn)?$")


def company_tokens(name):
    stop = {"llc", "inc", "the", "and", "of", "co", "company", "group", "rentals", "rental", "vacation", "properties", "property", "management"}
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in stop and len(w) > 2}


def search_people(row, icp, ledger, linkedin):
    from providers import fc_search
    name, city = row["company"], row.get("city") or ""
    cands = []
    q = f'"{name}" {city} owner OR founder OR "managing partner"'.strip()
    try:
        results = fc_search(q, ledger, limit=5)
    except Exception as e:
        return cands, f"search failed: {e}"
    for res in results:
        blob = f"{res['title']}. {res['description']}"
        for i, pat in enumerate(PEOPLE_PATTERNS):
            for m in pat.finditer(blob[:1500]):
                g = m.groups()
                pname, ptitle = (g[0], g[1]) if i != 2 else (g[1], g[0])
                if any(w.lower() in BAD_NAME_WORDS for w in pname.split()):
                    continue
                cands.append({"name": pname, "title": ptitle.strip().title(), "source": res["url"], "snippet": blob[:240], "method": "web_search"})
    if linkedin:
        time.sleep(1)
        try:
            results = fc_search(f'site:linkedin.com/in "{name}" owner OR founder OR CEO OR president', ledger, limit=5)
        except Exception as e:
            return cands, f"linkedin search failed: {e}"
        toks = company_tokens(name)
        for res in results:
            m = LI_RE.match(res["title"].strip())
            if not m:
                continue
            comp_toks = company_tokens(m.group("company"))
            overlap = len(toks & comp_toks)
            cands.append({"name": m.group("name"), "title": m.group("title").strip(), "company_in_headline": m.group("company").strip(),
                          "linkedin_url": res["url"], "source": res["url"], "snippet": res["description"][:240],
                          "method": "linkedin_search", "company_match": overlap >= max(1, min(2, len(toks)))})
    return cands, "ok"


def verify_size_free(row, icp, out):
    """Walk the listing index and pagination; return (count, evidence) or (None, why)."""
    site = read_json(out / "site_data" / f"{row['domain']}.json", {}) or {}
    index = next((p for p in site.get("pages", []) if p["kind"] == "listing_index"), None)
    start_urls = [index["url"]] if index else [urljoin(row["website"], p) for p in icp["size"].get("listing_index_paths", [])[:4]]
    domain = row["domain"]
    links, visited, queue = [], set(), list(start_urls)
    pages_ok = 0
    while queue and len(visited) < 12:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        final, html, status, err = fetch(url, timeout=15)
        if not html or status >= 400:
            continue
        pages_ok += 1
        soup = BeautifulSoup(html, "html.parser")
        page_links = []
        for a in soup.find_all("a", href=True):
            full = urljoin(final, a["href"]).split("#")[0]
            if root_domain(full) == domain:
                page_links.append(full)
                txt = a.get_text(" ", strip=True).lower()
                if (a.get("rel") and "next" in a.get("rel")) or txt in ("next", "next »", "»", "›", "next page", "load more", "more"):
                    queue.append(full)
            if re.search(r"[?&]page=\d+|/page/\d+", full) and root_domain(full) == domain:
                queue.append(full)
        links += page_links
        time.sleep(0.4)
    n = count_listing_urls(links, domain, icp)
    if pages_ok == 0:
        return None, "no listing index page reachable"
    if n < 3:
        return None, f"only {n} listing links across {pages_ok} page(s)"
    return n, f"{n} distinct listing links across {pages_ok} listing page(s) starting at {start_urls[0]}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--find-people", action="store_true")
    ap.add_argument("--linkedin", action="store_true", help="also run a linkedin.com/in scoped search (2 more credits each)")
    ap.add_argument("--verify-size", action="store_true")
    ap.add_argument("--max", type=int, default=30, help="max companies to spend searches on")
    ap.add_argument("--domains", nargs="*")
    args = ap.parse_args(argv)
    if not (args.find_people or args.verify_size):
        ap.error("pass --find-people and/or --verify-size")

    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    ledger = Ledger(out)
    path = out / "needs_enrichment.csv"
    if not path.exists():
        raise SystemExit("Run qualify first; needs_enrichment.csv is missing.")
    rows = list(csv.DictReader(open(path)))
    if args.domains:
        rows = [r for r in rows if r["domain"] in args.domains]
    enrichment = read_json(out / "enrichment.json", {}) or {}
    candidates = read_json(out / "enrichment_candidates.json", {}) or {}

    if args.verify_size:
        todo = [r for r in rows if r["status"] == "review" and not enrichment.get(r["domain"], {}).get("size")]
        print(f"[verify-size] {len(todo)} companies, free HTTP")
        for i, r in enumerate(todo, 1):
            n, why = verify_size_free(r, icp, out)
            if n:
                enrichment.setdefault(r["domain"], {}).update({"size": n, "size_evidence": why, "size_source": "listing_walk"})
                write_json(out / "enrichment.json", enrichment)
            print(f"  [{i}/{len(todo)}] {r['domain']}: {n if n else '-'} ({why})")

    if args.find_people:
        if not env("FIRECRAWL_API_KEY"):
            raise SystemExit("--find-people needs FIRECRAWL_API_KEY")
        todo = [r for r in rows if r["status"] in ("needs_enrichment", "ready_generic", "review") and not r["first_name"]
                and r["domain"] not in candidates]
        todo.sort(key=lambda r: -int(r["priority"] or 0))
        todo = todo[:args.max]
        est = len(todo) * (4 if args.linkedin else 2)
        print(f"[find-people] {len(todo)} companies, about {est} Firecrawl credits")
        promoted = 0
        for i, r in enumerate(todo, 1):
            cands, note = search_people(r, icp, ledger, args.linkedin)
            candidates[r["domain"]] = {"people": cands, "note": note, "searched_at": time.strftime("%Y-%m-%d")}
            write_json(out / "enrichment_candidates.json", candidates)
            auto = next((c for c in cands if c.get("method") == "linkedin_search" and c.get("company_match")
                         and title_tier(c["title"], icp) == 1), None)
            if auto and not enrichment.get(r["domain"], {}).get("decision_maker"):
                first, last = split_name(auto["name"])
                enrichment.setdefault(r["domain"], {})["decision_maker"] = {
                    "first_name": first, "last_name": last, "title": auto["title"], "email": None,
                    "linkedin_url": auto.get("linkedin_url"), "email_source": None, "auto": True, "source": auto["source"]}
                enrichment[r["domain"]]["notes"] = (enrichment[r["domain"]].get("notes", "") + " DM auto-promoted from LinkedIn search headline; confirm before outreach.").strip()
                write_json(out / "enrichment.json", enrichment)
                promoted += 1
            print(f"  [{i}/{len(todo)}] {r['company']}: {len(cands)} candidate(s) {'(auto-promoted DM) ' if auto else ''}{note if note != 'ok' else ''}")
            time.sleep(0.5)
        print(f"{promoted} decision makers auto-promoted (name + title only). Review enrichment_candidates.json for the rest.")

    print("Now rerun qualify to pick up the changes.")
    print(ledger.summary())


if __name__ == "__main__":
    main()
