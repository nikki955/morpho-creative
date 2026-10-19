#!/usr/bin/env python3
"""Step 2: Read every company website for free (plain HTTP + HTML parsing).

Replaces the Firecrawl map + 4 scrapes per company (about 17 credits) with zero-credit fetches.
Firecrawl is used only as a fallback for sites that render nothing without JavaScript, and only
when --firecrawl-fallback is passed (1 credit per page).

Per company:
  1. GET the homepage. Parse title, meta, JSON-LD (Organization / LocalBusiness / Person), links,
     emails (text + mailto), phones, social links, visible text.
  2. Pick up to --max-pages more pages from the internal links: about/team, contact/owners,
     the listing index, plus a careers page if linked. Fetch and parse the same way.
  3. GET /sitemap.xml (and child sitemaps) and count listing URLs = free size proxy.
  4. Run the ICP's size patterns, tech fingerprints, OTA links, direct-booking cues, exclusion
     signals, hiring and growth signals, and people heuristics over everything collected.

Results are cached per domain in <run>/site_data/<domain>.json. --force redoes a domain.

Usage:
  python3 reader.py --icp str-portfolio-managers --run-id <id> [--workers 6] [--max-pages 4] [--firecrawl-fallback]
"""
import argparse
import concurrent.futures as cf
import json
import re
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import (Ledger, PHONE_RE, default_run_id, env, extract_emails, load_icp, read_json, root_domain,
                    run_dir, session, write_json)

MAX_BYTES = 2_500_000
TEXT_KEEP = 7000
TITLE_WORDS = ("founder", "co-founder", "cofounder", "owner", "co-owner", "ceo", "president", "principal",
               "managing partner", "partner", "proprietor", "director", "manager", "coo", "cfo", "vp",
               "vice president", "head of", "general manager", "broker", "innkeeper", "chef", "operator")
NAME_RE = r"([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2})"
TITLE_RE = r"((?:co-?)?(?:founder|owner|ceo|president|principal|proprietor|managing partner|partner|coo|cfo|" \
           r"general manager|operations manager|director of operations|operations director|property manager|" \
           r"portfolio manager|vp of operations|director|manager|broker|innkeeper|operator)(?: (?:&|and) (?:co-?)?(?:founder|owner|ceo|operator))?)"
PEOPLE_PATTERNS = [
    re.compile(NAME_RE + r"\s*[,|–—\-:]\s*" + TITLE_RE + r"\b", re.I),               # Jane Doe, Owner
    re.compile(NAME_RE + r"\s+(?:is|as)\s+(?:the|our|a)?\s*" + TITLE_RE + r"\b", re.I),  # Jane Doe is the owner
    re.compile(TITLE_RE + r"\s*[:,\-–—]?\s+" + NAME_RE + r"\b"),                         # Owner: Jane Doe
    re.compile(r"(?:meet|by|from)\s+" + NAME_RE + r",?\s+(?:the\s+|our\s+)?" + TITLE_RE + r"\b", re.I),
]
BAD_NAME_WORDS = {"vacation", "rental", "rentals", "property", "properties", "management", "guest", "services",
                  "our", "the", "team", "about", "contact", "home", "homes", "llc", "inc", "maine", "beach",
                  "lake", "mountain", "island", "coast", "north", "south", "east", "west", "new", "owner",
                  "privacy", "policy", "terms", "book", "now", "read", "more", "learn", "view", "all"}
LEAD_IN_WORDS = {"meet", "by", "from", "welcome", "hi", "hello", "introducing", "with", "and", "contact", "call", "email", "ask", "owner", "owners"}
TOTAL_RE = re.compile(r"(?:showing|displaying)?\s*(?:\d+\s*[-–]\s*\d+\s*of\s*)?(\d{1,4})\s*(?:results|properties|rentals|homes|listings|units|cottages|cabins|condos|sites|rooms)\b", re.I)


# ------------------------------------------------------------------ fetching
def fetch(url, timeout=20):
    """Return (final_url, html, status, error). Tries https, then http on TLS trouble."""
    tries = [url]
    if url.startswith("https://"):
        tries.append("http://" + url[len("https://"):])
    last_err = ""
    for u in tries:
        try:
            r = session().get(u, timeout=timeout, allow_redirects=True, stream=True)
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}"
                r.close()
                if r.status_code in (403, 429, 503):
                    return u, "", r.status_code, last_err  # blocked; fallback may help
                continue
            if "html" not in ct and "xml" not in ct and "text" not in ct:
                r.close()
                return r.url, "", r.status_code, f"not html ({ct[:40]})"
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) > MAX_BYTES:
                    break
            r.close()
            enc = r.encoding or "utf-8"
            try:
                html = raw.decode(enc, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            return r.url, html, r.status_code, ""
        except requests.exceptions.SSLError as e:
            last_err = f"ssl: {str(e)[:80]}"
            continue
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    return url, "", 0, last_err


def visible_text(soup):
    for t in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def parse_jsonld(soup):
    items = []
    for tag in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                items.append(d)
                for k in ("@graph", "founder", "employee", "employees", "member", "author"):
                    v = d.get(k)
                    if isinstance(v, (list, dict)):
                        stack.append(v)
    return items


def jsonld_facts(items):
    facts = {"org_name": None, "emails": [], "phones": [], "people": [], "types": [], "address": None}
    for d in items:
        t = d.get("@type")
        types = [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]
        facts["types"] += types
        tl = " ".join(types).lower()
        if any(x in tl for x in ("organization", "localbusiness", "lodgingbusiness", "realestateagent", "business", "store", "service")):
            facts["org_name"] = facts["org_name"] or d.get("name")
            if d.get("email"):
                facts["emails"].append(str(d["email"]).replace("mailto:", ""))
            if d.get("telephone"):
                facts["phones"].append(str(d["telephone"]))
            addr = d.get("address")
            if isinstance(addr, dict) and not facts["address"]:
                facts["address"] = {"city": addr.get("addressLocality"), "state": addr.get("addressRegion"),
                                    "postal_code": addr.get("postalCode")}
            for k in ("founder", "employee", "employees"):
                v = d.get(k)
                for p in (v if isinstance(v, list) else [v]):
                    if isinstance(p, dict) and p.get("name"):
                        facts["people"].append({"name": p["name"], "title": p.get("jobTitle") or ("Founder" if k == "founder" else None),
                                                "email": (p.get("email") or "").replace("mailto:", "") or None, "method": "jsonld"})
                    elif isinstance(p, str) and k == "founder":
                        facts["people"].append({"name": p, "title": "Founder", "email": None, "method": "jsonld"})
        if "person" in tl and d.get("name"):
            facts["people"].append({"name": d["name"], "title": d.get("jobTitle"), "email": (d.get("email") or "").replace("mailto:", "") or None,
                                    "method": "jsonld"})
    return facts


def parse_page(url, html, domain):
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    desc = ""
    for m in soup.find_all("meta"):
        if (m.get("name") or m.get("property") or "").lower() in ("description", "og:description") and m.get("content"):
            desc = m["content"][:300]
            break
    site_name = next((m.get("content") for m in soup.find_all("meta", property="og:site_name") if m.get("content")), None)
    jl = jsonld_facts(parse_jsonld(soup))
    internal, external, mailto = [], [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            mailto.append(unquote(href[7:]).split("?")[0])
            continue
        if href.startswith(("tel:", "javascript:", "#")):
            continue
        full = urljoin(url, href)
        if not full.startswith("http"):
            continue
        d = root_domain(full)
        anchor = a.get_text(" ", strip=True)[:80]
        if d == domain or d.endswith("." + domain):
            internal.append((full.split("#")[0], anchor))
        else:
            external.append(full)
    html_l = html.lower()
    text = visible_text(soup)
    return {
        "url": url, "title": title, "description": desc, "site_name": site_name, "jsonld": jl,
        "internal_links": internal, "external_links": external,
        "emails": extract_emails(text, html, " ".join(mailto), " ".join(jl["emails"])),
        "phones": list(dict.fromkeys(PHONE_RE.findall(text)))[:3] + jl["phones"][:2],
        "social": {k: next((e for e in external if k in e), None) for k in ("facebook.com", "instagram.com", "linkedin.com")},
        "text": text, "html_l": html_l,
    }


# ------------------------------------------------------------------ sitemap
def sitemap_urls(home, cap=6000):
    urls, seen_maps = [], set()
    queue = [urljoin(home, "/sitemap.xml"), urljoin(home, "/sitemap_index.xml"), urljoin(home, "/wp-sitemap.xml")]
    while queue and len(urls) < cap and len(seen_maps) < 12:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        _, xml, status, _ = fetch(sm, timeout=15)
        if not xml or status >= 400 or "<" not in xml[:200]:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
        if "<sitemapindex" in xml[:2000]:
            queue += locs[:10]
        else:
            urls += locs
            if not locs and "<urlset" not in xml:
                continue
    return list(dict.fromkeys(urls))[:cap]


def count_listing_urls(urls, domain, icp):
    pats = icp["size"].get("listing_url_patterns", [])
    slugs = set()
    for u in urls:
        if root_domain(u) != domain:
            continue
        path = urlparse(u).path.lower()
        if any(x in path for x in ("/page/", "/category/", "/tag/", "/blog", "/author/", "/feed")):
            continue
        for pat in pats:
            i = path.find(pat)
            if i >= 0:
                rest = path[i + len(pat):].strip("/")
                if rest and "/" not in rest and not rest.isdigit() and len(rest) > 2:
                    slugs.add(pat + rest)
                break
    return len(slugs)


# ------------------------------------------------------------------ page picking
def pick_pages(home_url, internal, icp, domain, max_pages):
    """Choose extra pages worth reading, in priority order."""
    people_kw = icp["pages"]["people_keywords"]
    contact_kw = icp["pages"]["contact_keywords"]
    index_paths = [p.strip("/") for p in icp["size"].get("listing_index_paths", [])]
    cands = {"people": [], "contact": [], "listing_index": [], "careers": [], "owners": []}
    seen = set()
    for url, anchor in internal:
        if url in seen or url.rstrip("/") == home_url.rstrip("/"):
            continue
        seen.add(url)
        p = urlparse(url)
        path = p.path.lower().strip("/")
        if p.query and "page=" in p.query:
            continue
        segs = [s for s in path.split("/") if s]
        if len(segs) > 2 or any(x in path for x in ("/blog/", "wp-content", ".pdf", ".jpg", ".png", "/feed")):
            continue
        last = segs[-1] if segs else ""
        al = anchor.lower()
        score = len(segs)
        if any(k in last for k in people_kw) or any(k in al for k in ("about", "our team", "meet", "our story", "who we are")):
            cands["people"].append((score, url))
        elif any(k in last for k in ("career", "jobs", "join-our-team", "employment", "hiring")) or "career" in al or "join our team" in al:
            cands["careers"].append((score, url))
        elif path in index_paths or last in index_paths or any(k in al for k in ("all rentals", "our rentals", "our properties", "browse", "all properties", "view rentals", "our homes", "our cottages", "our cabins", "find a rental")):
            cands["listing_index"].append((score, url))
        elif any(k in last for k in contact_kw) or "contact" in al:
            kind = "owners" if any(k in last for k in ("owner", "homeowner", "list-your", "management", "partner")) else "contact"
            cands[kind].append((score, url))
    order = ["people", "listing_index", "owners", "contact", "careers"]
    picked = []
    for kind in order:
        if cands[kind]:
            cands[kind].sort()
            picked.append((kind, cands[kind][0][1]))
    return picked[:max_pages]


# ------------------------------------------------------------------ signal extraction
def find_people(pages, icp):
    people, seen = [], set()
    for pg in pages:
        for p in pg["jsonld"]["people"]:
            key = p["name"].lower()
            if key not in seen:
                seen.add(key)
                people.append({**p, "source_url": pg["url"]})
    for pg in pages:
        text = pg["text"][:20000]
        for i, pat in enumerate(PEOPLE_PATTERNS):
            for m in pat.finditer(text):
                g = m.groups()
                name, title = (g[0], g[1]) if i != 2 else (g[1], g[0])
                words = name.split()
                while words and words[0].lower() in LEAD_IN_WORDS:
                    words.pop(0)
                name = " ".join(words)
                if any(w.lower() in BAD_NAME_WORDS for w in words) or len(words) < 2:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                start = max(0, m.start() - 60)
                people.append({"name": name, "title": title.strip().title(), "email": None, "method": "text_pattern",
                               "source_url": pg["url"], "evidence": text[start:m.end() + 40]})
    return people[:12]


def find_size_claims(pages, icp):
    claims = []
    pats = [re.compile(p, re.I) for p in icp["size"].get("patterns", [])]
    lo, hi = 1, 5000
    for pg in pages:
        text = pg["text"]
        for pat in pats:
            for m in pat.finditer(text):
                try:
                    n = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                if not (lo <= n <= hi):
                    continue
                ev = text[max(0, m.start() - 50): m.end() + 50]
                if re.search(r"\b(19|20)\d{2}\b", m.group(0)):      # years are not counts
                    continue
                if re.search(r"(sq\.? ?ft|square|bed(?!room)|bath|guests?(?!\s*(?:rooms?|suites?))|sleeps|acres?|miles?|minutes?|\$|%|reviews?|years?)", ev, re.I):
                    continue
                claims.append({"n": n, "evidence": ev.strip(), "url": pg["url"]})
    # dedupe by number, keep first evidence
    out, seen = [], set()
    for c in claims:
        if c["n"] not in seen:
            seen.add(c["n"])
            out.append(c)
    return out[:8]


def signal_hits(pages, phrases, window=70):
    hits = []
    for pg in pages:
        tl = pg["text"].lower()
        for ph in phrases:
            i = tl.find(ph.lower())
            if i >= 0:
                hits.append({"phrase": ph, "url": pg["url"], "evidence": pg["text"][max(0, i - window): i + len(ph) + window].strip()})
                break
    return hits


def detect_tech(pages, icp):
    html = " ".join(pg["html_l"] for pg in pages)
    tech = [name for name, sigs in icp["signals"].get("tech", {}).items() if any(s in html for s in sigs)]
    ota = sum(html.count(s) for s in icp["signals"].get("ota_links", []))
    direct_cues = [ph for ph in icp["signals"].get("direct_booking", []) if ph in html]
    widget = any(x in html for x in ("availability", "booking-widget", "book-now", "reservation", "checkin", "check-in", "arrival"))
    return tech, ota, bool(tech) or (len(direct_cues) >= 2 and widget)


# ------------------------------------------------------------------ per-company
def process(company, icp, out, max_pages, fc_fallback, ledger):
    domain, home = company["domain"], company["website"]
    res = {"domain": domain, "errors": [], "pages": [], "fetched_at": time.strftime("%Y-%m-%d")}
    final, html, status, err = fetch(home)
    js_shell = False
    if html:
        hp = parse_page(final, html, domain)
        markers = ('id="root"', 'id="__next"', 'id="app"', "__nuxt", "data-reactroot", "window.__INITIAL", "ng-app")
        js_shell = status == 200 and (len(hp["text"]) < 150 or (len(hp["text"]) < 600 and any(m in html for m in markers)))
        pages = [("home", hp)]
    else:
        res["errors"].append(f"home: {err}")
        pages = []

    if (not html or js_shell or status in (403, 429, 503)) and fc_fallback and env("FIRECRAWL_API_KEY"):
        from providers import fc_scrape
        try:
            d = fc_scrape(home, ledger)
            raw_html = d.get("rawHtml") or d.get("html") or ""
            if raw_html:
                hp = parse_page(home, raw_html, domain)
                if len(hp["text"]) < 300 and d.get("markdown"):
                    hp["text"] = re.sub(r"\s+", " ", d["markdown"])[:TEXT_KEEP * 2]
                pages = [("home", hp)]
                res["firecrawl_fallback"] = True
                res["errors"] = [e for e in res["errors"] if not e.startswith("home:")]
                js_shell = False
        except Exception as e:
            res["errors"].append(f"firecrawl: {e}")
    res["js_shell"] = js_shell

    if pages:
        home_url = pages[0][1]["url"]
        internal = list(pages[0][1]["internal_links"])
        for kind, url in pick_pages(home_url, internal, icp, domain, max_pages):
            time.sleep(0.4)
            f, h, s, e = fetch(url, timeout=15)
            if h:
                pg = parse_page(f, h, domain)
                pages.append((kind, pg))
                internal += pg["internal_links"]
            else:
                res["errors"].append(f"{kind}: {e}")
        res["sitemap_urls"] = sitemap_urls(home_url)
        all_urls = res["sitemap_urls"] + [u for u, _ in internal]
        res["listing_url_count"] = count_listing_urls(all_urls, domain, icp)
        res["sitemap_url_count"] = len(res["sitemap_urls"])
        del res["sitemap_urls"]

        index_pg = next((pg for k, pg in pages if k == "listing_index"), None)
        if index_pg:
            m = TOTAL_RE.search(index_pg["text"][:4000])
            res["index_total_text"] = m.group(0).strip() if m else None
            res["index_listing_links"] = count_listing_urls([u for u, _ in index_pg["internal_links"]], domain, icp)

        pgs = [pg for _, pg in pages]
        res["tech_detected"], res["ota_link_count"], res["direct_booking"] = detect_tech(pgs, icp)
        res["exclude_text_hits"] = [h["phrase"] for h in signal_hits(pgs, icp["exclude"].get("text_signals", []))]
        res["hiring_signals"] = signal_hits(pgs, icp["signals"].get("hiring", []))
        res["growth_signals"] = signal_hits(pgs, icp["signals"].get("growth", []))
        res["ownership_signals"] = signal_hits(pgs, icp["signals"].get("ownership_change", []))
        res["portfolio_signals"] = signal_hits(pgs, icp["signals"].get("portfolio", []))
        res["size_claims"] = find_size_claims(pgs, icp)
        res["people"] = find_people(pgs, icp)
        emails = set()
        for pg in pgs:
            emails |= set(pg["emails"])
        res["emails"] = sorted(emails)
        res["phones"] = list(dict.fromkeys(p for pg in pgs for p in pg["phones"]))[:3]
        res["social"] = {k: next((pg["social"].get(k) for pg in pgs if pg["social"].get(k)), None) for k in ("facebook.com", "instagram.com", "linkedin.com")}
        h0 = pgs[0]
        res["title"], res["description"] = h0["title"], h0["description"]
        res["org_name"] = h0["jsonld"]["org_name"] or h0["site_name"]
        res["jsonld_types"] = sorted(set(t for pg in pgs for t in pg["jsonld"]["types"]))[:10]
        res["jsonld_address"] = h0["jsonld"]["address"]
        res["pages"] = [{"kind": k, "url": pg["url"], "title": pg["title"], "text": pg["text"][:TEXT_KEEP]} for k, pg in pages]
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-pages", type=int, default=4, help="extra pages per site after the homepage")
    ap.add_argument("--firecrawl-fallback", action="store_true", help="use Firecrawl (1 credit) for JS-only or blocked homepages")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--domains", nargs="*")
    args = ap.parse_args(argv)

    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    ledger = Ledger(out)
    companies = read_json(out / "companies.json")
    if not companies:
        raise SystemExit("No companies.json in this run. Run discover first.")
    todo = [c for c in companies if (not args.domains or c["domain"] in args.domains)
            and (args.force or not (out / "site_data" / f"{c['domain']}.json").exists())]
    if args.limit:
        todo = todo[:args.limit]
    print(f"Reading {len(todo)} sites with plain HTTP (0 credits; Firecrawl fallback {'on' if args.firecrawl_fallback else 'off'})")

    def work(c):
        try:
            r = process(c, icp, out, args.max_pages, args.firecrawl_fallback, ledger)
        except Exception as e:
            r = {"domain": c["domain"], "errors": [f"fatal: {type(e).__name__}: {e}"], "pages": []}
        write_json(out / "site_data" / f"{c['domain']}.json", r)
        return c["domain"], r

    stats = {"ok": 0, "failed": 0, "js_shell": 0}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (dom, r) in enumerate(ex.map(work, todo), 1):
            ok = bool(r.get("pages"))
            stats["ok" if ok else "failed"] += 1
            stats["js_shell"] += 1 if r.get("js_shell") else 0
            print(f"[{i}/{len(todo)}] {dom}: pages={len(r.get('pages', []))} people={len(r.get('people', []))} "
                  f"emails={len(r.get('emails', []))} listing_urls={r.get('listing_url_count', '-')} "
                  f"size_claims={[c['n'] for c in r.get('size_claims', [])]} tech={r.get('tech_detected')} "
                  f"{'JS-SHELL ' if r.get('js_shell') else ''}errors={len(r.get('errors', []))}", flush=True)
    print(f"\nDone. {stats}. Site data in {out / 'site_data'}")
    if stats["js_shell"] and not args.firecrawl_fallback:
        print(f"{stats['js_shell']} sites rendered nothing without JavaScript. Rerun with --firecrawl-fallback "
              f"--force --domains <those> to read them for ~1 credit each.")
    print(ledger.summary())


if __name__ == "__main__":
    main()
