#!/usr/bin/env python3
"""Step 3: Qualify companies against the ICP, pick the decision maker and the best email.

Two tiers:
  rules  (free)      exclusions, size from stated numbers / listing counts / sitemap, decision maker
                     from people found on the site, email ranking.
  --llm  (pennies)   one Claude call per company that survived the rules. Reads the page text the
                     reader saved and returns business type, fit score, size with evidence, people,
                     buying signals. Default model claude-haiku-5-5 (set LEAD_ENGINE_MODEL to change).
                     About 4k input tokens per company: 100 companies is roughly $0.05 to $0.10.
                     Results are cached in <run>/llm.json; --force-llm redoes them.

Also merges <run>/enrichment.json (hand or agent written overrides) and <run>/verification.json.

enrichment.json format, keyed by domain, every field optional:
{
  "acme-rentals.com": {
    "decision_maker": {"first_name": "Jane", "last_name": "Doe", "title": "Owner",
                       "email": "jane@acme-rentals.com", "email_source": "https://acme-rentals.com/about",
                       "linkedin_url": "https://linkedin.com/in/...", "phone": "+12075551234"},
    "size": 32, "size_evidence": "Rentals page lists 32 homes",
    "include": true, "exclude": false, "exclude_reason": "", "notes": "Hiring a guest services coordinator"
  }
}

Outputs: leads.csv, needs_enrichment.csv, review.csv, excluded.csv, summary.json
"""
import argparse
import csv
import json
import re

from common import (Ledger, classify_email, default_run_id, email_matches_person, load_icp, read_json, run_dir,
                    split_name, title_tier, write_json)
from providers import llm_available, llm_json, llm_model

FIELDS = ["priority", "fit_score", "company", "domain", "website", "city", "state", "size", "size_source",
          "size_confidence", "size_evidence", "segment", "flags", "sweet_spot", "business_type",
          "buyer_type", "buyer_type_evidence", "ownership_signals", "portfolio_signals",
          "first_name", "last_name", "title", "title_tier", "email", "email_type", "email_status", "email_source",
          "linkedin_url", "phone", "tech", "ota_link_count", "direct_booking", "booking_evidence", "hiring_signals", "growth_signals",
          "buying_signals", "other_emails", "maps_url", "sources", "summary", "notes", "status", "reason"]

LLM_SYSTEM = """You qualify small businesses against an ideal customer profile (ICP) from text copied off their websites.
Be strict and literal. Only report a size number that the pages state or clearly list; never estimate from vibes.
If pages conflict, prefer the listing page over marketing copy. Report people exactly as named on the pages.
Return JSON matching the schema. fit_score: 0 means clearly not the ICP, 100 means textbook ICP.

ICP name: {name}
Who qualifies and who does not:
{description}

What is being sold to them (for judging buying signals): {offer}

Size metric: {metric} ({label}). Acceptable range {min} to {max}. Sweet spot {sweet_min} to {sweet_max}.

business_type must be one of these keys:
{types}

Decision maker titles, best first: {primary}. Secondary: {secondary}.{buyers}"""

BUYER_BLOCK = """

buyer_type: who is on the other end of a call, judged from the site (owner story, number of properties, ownership history). Must be one of these keys, or null if there is no evidence:
{buyer_types}
Quote the phrase that decided it in buyer_type_evidence."""

LLM_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "business_type": {"type": "string"},
        "fit_score": {"type": "integer"},
        "fit_reason": {"type": "string"},
        "size_estimate": {"type": ["integer", "null"]},
        "size_evidence": {"type": ["string", "null"], "description": "exact short phrase from the page that states the number"},
        "size_confidence": {"type": "string", "enum": ["stated", "inferred", "none"]},
        "people": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "title": {"type": ["string", "null"]}, "email": {"type": ["string", "null"]}},
            "required": ["name", "title", "email"], "additionalProperties": False}},
        "buyer_type": {"type": ["string", "null"]},
        "buyer_type_evidence": {"type": ["string", "null"]},
        "decision_maker": {"type": ["object", "null"], "properties": {
            "name": {"type": "string"}, "title": {"type": ["string", "null"]}},
            "required": ["name", "title"], "additionalProperties": False},
        "buying_signals": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "locations_served": {"type": "array", "items": {"type": "string"}},
        "one_line_summary": {"type": "string"},
    },
    "required": ["business_type", "fit_score", "fit_reason", "size_estimate", "size_evidence", "size_confidence",
                 "buyer_type", "buyer_type_evidence", "people", "decision_maker", "buying_signals", "red_flags",
                 "locations_served", "one_line_summary"],
    "additionalProperties": False,
}
PAGE_BUDGET = {"home": 3500, "people": 3000, "listing_index": 1800, "owners": 1500, "contact": 1000, "careers": 700}


def llm_system(icp):
    s = icp["size"]
    types = "\n".join(f"  {k}: {v}" for k, v in icp["business_types"]["options"].items())
    bt = icp.get("buyer_types", {}).get("options") or {}
    buyers = BUYER_BLOCK.format(buyer_types="\n".join(f"  {k}: {v}" for k, v in bt.items())) if bt else ""
    return LLM_SYSTEM.format(buyers=buyers,name=icp["name"], description=icp["description"], offer=icp.get("offer_context", ""),
                             metric=s["metric"], label=s.get("label", s["metric"]), min=s["min"], max=s["max"],
                             sweet_min=s.get("sweet_min", s["min"]), sweet_max=s.get("sweet_max", s["max"]),
                             types=types, primary=", ".join(icp["decision_makers"]["primary"]),
                             secondary=", ".join(icp["decision_makers"]["secondary"]))


def llm_user(company, site):
    parts = [f"Company: {company.get('name')}  Domain: {company['domain']}  "
             f"Location: {company.get('city') or ''} {company.get('state') or ''}  Google category: {company.get('category') or ''}"]
    if company.get("snippet"):
        parts.append(f"Search snippet: {company['snippet']}")
    hints = {"emails_found": site.get("emails", [])[:8],
             "people_found_by_heuristics": [{"name": p["name"], "title": p.get("title")} for p in site.get("people", [])[:8]],
             "numbers_found_by_regex": [{"n": c["n"], "evidence": c["evidence"]} for c in site.get("size_claims", [])[:6]],
             "listing_urls_in_sitemap": site.get("listing_url_count"), "index_total_text": site.get("index_total_text"),
             "tech_detected_note": "software fingerprints only; not proof a guest can book here",
             "tech_detected": site.get("tech_detected"), "direct_booking_path": booking_status(site)}
    parts.append("Extractor hints (verify against the text, do not trust blindly): " + json.dumps(hints))
    for pg in site.get("pages", []):
        budget = PAGE_BUDGET.get(pg["kind"], 1000)
        parts.append(f"\n--- PAGE [{pg['kind']}] {pg['url']}\nTITLE: {pg.get('title')}\n{pg['text'][:budget]}")
    return "\n".join(parts)


def run_llm(companies, sites, icp, out, ledger, force, limit):
    cache = read_json(out / "llm.json", {}) or {}
    schema = json.loads(json.dumps(LLM_SCHEMA_BASE))
    schema["properties"]["business_type"]["enum"] = list(icp["business_types"]["options"])
    bt = icp.get("buyer_types", {}).get("options") or {}
    if bt:
        schema["properties"]["buyer_type"]["enum"] = list(bt) + [None]
    system = llm_system(icp)
    todo = [c for c in companies if sites.get(c["domain"], {}).get("pages") and (force or c["domain"] not in cache)]
    if limit:
        todo = todo[:limit]
    print(f"[llm] {len(todo)} companies to classify with {llm_model()}")
    for i, c in enumerate(todo, 1):
        data, note = llm_json(system, llm_user(c, sites[c["domain"]]), schema, ledger)
        cache[c["domain"]] = {"result": data, "note": note, "model": llm_model()}
        write_json(out / "llm.json", cache)
        r = data or {}
        print(f"  [{i}/{len(todo)}] {c['domain']}: {r.get('business_type', note)} fit={r.get('fit_score', '-')} "
              f"size={r.get('size_estimate')} ({r.get('size_confidence', '')}) dm={(r.get('decision_maker') or {}).get('name')}")
        if note.startswith("rate limited"):
            import time
            time.sleep(10)
    return cache


def parse_int(text):
    if not text:
        return None
    nums = [int(n) for n in re.findall(r"\b(\d{1,4})\b", str(text))]
    return max(nums) if nums else None


def choose_size(site, enr, llm, icp):
    """Return (n, source, confidence, evidence)."""
    s = icp["size"]
    if enr.get("size"):
        return int(enr["size"]), "manual_or_agent", "high", enr.get("size_evidence", "")
    if llm and llm.get("size_estimate") and llm.get("size_confidence") == "stated":
        return int(llm["size_estimate"]), "llm_stated", "high", llm.get("size_evidence") or ""
    total = parse_int(site.get("index_total_text"))
    if total:
        return total, "listing_page_total", "high", site["index_total_text"]
    claims = site.get("size_claims") or []
    if claims:
        strong = [c for c in claims if re.search(r"\b(manage|managing|portfolio|represent|oversee|operate|over|more than)\b", c["evidence"], re.I)]
        pick = max(strong or claims, key=lambda c: c["n"])
        return pick["n"], "site_claim", "medium", pick["evidence"]
    idx = site.get("index_listing_links") or 0
    luc = site.get("listing_url_count") or 0
    best = max(idx, luc)
    if best >= 5:
        return best, "listing_urls", "medium", f"{best} distinct listing pages found in sitemap or listing index"
    if llm and llm.get("size_estimate") and llm.get("size_confidence") == "inferred":
        return int(llm["size_estimate"]), "llm_inferred", "low", llm.get("size_evidence") or ""
    if 0 < best < 5:
        return best, "listing_urls", "low", f"only {best} listing pages found"
    return None, "unknown", "none", ""


def assign_segment(n, icp):
    for seg in icp.get("segments", []):
        if "min" in seg and "max" in seg and n is not None and seg["min"] <= n <= seg["max"]:
            return seg["key"]
    return ""


def booking_status(site):
    """confirmed | none_found | unknown. Older site_data stored a boolean, which is treated as unknown unless True."""
    b = site.get("booking")
    if isinstance(b, dict) and b.get("status"):
        return b["status"]
    v = site.get("direct_booking")
    if v is True:
        return "confirmed"
    if isinstance(v, str) and v in ("confirmed", "none_found", "unknown"):
        return v
    return "unknown" if site else ""


def rule_flags(site, icp):
    """The no_direct_booking rule only fires on positive evidence of absence, never on 'unknown'."""
    flags = []
    for seg in icp.get("segments", []):
        if seg.get("rule") == "no_direct_booking" and site and booking_status(site) == "none_found":
            flags.append(seg["key"])
    return flags


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--llm", action="store_true", help="classify with Claude (cached per domain)")
    ap.add_argument("--force-llm", action="store_true")
    ap.add_argument("--llm-limit", type=int, default=0)
    ap.add_argument("--allow-low-confidence", action="store_true", help="treat low-confidence size as qualified")
    ap.add_argument("--min-fit", type=int, default=40, help="LLM fit_score below this is excluded")
    args = ap.parse_args(argv)

    icp = load_icp(args.icp)
    s = icp["size"]
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    ledger = Ledger(out)
    companies = read_json(out / "companies.json", []) or []
    enrichment = read_json(out / "enrichment.json", {}) or {}
    verification = read_json(out / "verification.json", {}) or {}
    sites = {c["domain"]: (read_json(out / "site_data" / f"{c['domain']}.json", {}) or {}) for c in companies}

    llm_cache = read_json(out / "llm.json", {}) or {}
    if args.llm:
        if not llm_available():
            raise SystemExit("--llm needs ANTHROPIC_API_KEY in .env (or an `ant auth login` profile).")
        llm_cache = run_llm(companies, sites, icp, out, ledger, args.force_llm, args.llm_limit)

    accept = set(icp["business_types"]["accept"])
    rows = []
    for c in companies:
        dom = c["domain"]
        site, enr = sites.get(dom, {}), enrichment.get(dom, {})
        llm = (llm_cache.get(dom) or {}).get("result") or None
        r = {k: "" for k in FIELDS}
        r.update({"company": site.get("org_name") or c.get("name") or dom, "domain": dom, "website": c["website"],
                  "city": c.get("city") or (site.get("jsonld_address") or {}).get("city") or "",
                  "state": c.get("state") or (site.get("jsonld_address") or {}).get("state") or "",
                  "maps_url": c.get("maps_url") or "", "sources": ";".join(c.get("sources", [])),
                  "tech": ";".join(site.get("tech_detected") or []), "ota_link_count": site.get("ota_link_count", ""),
                  "direct_booking": booking_status(site),
                  "booking_evidence": "; ".join(((site.get("booking") or {}).get("evidence") or (site.get("booking") or {}).get("call_to_reserve") or [])[:2])[:200],
                  "hiring_signals": " | ".join(h["evidence"][:120] for h in site.get("hiring_signals", [])[:2]),
                  "growth_signals": " | ".join(h["evidence"][:120] for h in site.get("growth_signals", [])[:2]),
                  "buying_signals": " | ".join((llm or {}).get("buying_signals", [])[:4]),
                  "ownership_signals": " | ".join(h["evidence"][:120] for h in site.get("ownership_signals", [])[:2]),
                  "portfolio_signals": " | ".join(h["evidence"][:120] for h in site.get("portfolio_signals", [])[:2]),
                  "summary": (llm or {}).get("one_line_summary", ""), "fit_score": (llm or {}).get("fit_score", ""),
                  "business_type": (llm or {}).get("business_type", ""), "notes": enr.get("notes", "")})

        # ---- buyer type (LLM first, then rule-of-thumb from site signals)
        bt_cfg = icp.get("buyer_types") or {}
        buyer = (llm or {}).get("buyer_type") if llm else None
        buyer_ev = (llm or {}).get("buyer_type_evidence") or ""
        if bt_cfg and not buyer:
            if site.get("ownership_signals"):
                buyer, buyer_ev = bt_cfg.get("on_ownership_change", ""), site["ownership_signals"][0]["evidence"][:160]
            elif site.get("portfolio_signals"):
                buyer, buyer_ev = bt_cfg.get("on_portfolio_signal", ""), site["portfolio_signals"][0]["evidence"][:160]
        r.update({"buyer_type": buyer or "", "buyer_type_evidence": (buyer_ev or "")[:200]})

        # ---- exclusion
        reason = ""
        if enr.get("exclude"):
            reason = enr.get("exclude_reason") or "excluded during enrichment"
        elif not site or not site.get("pages"):
            reason = "site not read (" + "; ".join(site.get("errors", [])[:1]) + ")" if site else "site not read yet"
        elif enr.get("include"):
            reason = ""
        elif llm and llm.get("business_type") and llm["business_type"] not in accept:
            reason = f"business type: {llm['business_type']}"
        elif llm and isinstance(llm.get("fit_score"), int) and llm["fit_score"] < args.min_fit:
            reason = f"fit score {llm['fit_score']}: {llm.get('fit_reason', '')[:120]}"
        elif site.get("exclude_text_hits"):
            reason = "site text: " + "; ".join(site["exclude_text_hits"])

        # ---- size + segment
        n, src, conf, ev = choose_size(site, enr, llm, icp)
        n_weak = False
        r.update({"size": n if n is not None else "", "size_source": src, "size_confidence": conf, "size_evidence": (ev or "")[:200]})
        flags = rule_flags(site, icp)
        r["flags"] = ";".join(flags)
        if n is not None:
            r["segment"] = assign_segment(n, icp)
            r["sweet_spot"] = "yes" if s.get("sweet_min", s["min"]) <= n <= s.get("sweet_max", s["max"]) else "no"
            too_big = n > s["max"] and conf in ("high", "medium")
            too_small = n < s["min"] and conf == "high"
            if not reason and (too_big or too_small):
                reason = f"{n} {s['metric']} outside {s['min']}-{s['max']}"
            elif not reason and n < s["min"]:
                n_weak = True

        # ---- decision maker
        people = []
        if enr.get("decision_maker"):
            dm = enr["decision_maker"]
            people.append({"name": f"{dm.get('first_name', '')} {dm.get('last_name', '')}".strip(), "title": dm.get("title"),
                           "email": dm.get("email"), "phone": dm.get("phone"), "linkedin_url": dm.get("linkedin_url"),
                           "source": dm.get("email_source") or "enrichment", "_rank": 0})
        if llm and llm.get("decision_maker") and llm["decision_maker"].get("name"):
            d = llm["decision_maker"]
            em = next((p.get("email") for p in llm.get("people", []) if p.get("name") == d["name"] and p.get("email")), None)
            people.append({"name": d["name"], "title": d.get("title"), "email": em, "source": "website (llm)", "_rank": 1})
        for p in (llm or {}).get("people", []):
            people.append({"name": p["name"], "title": p.get("title"), "email": p.get("email"), "source": "website (llm)", "_rank": 2})
        for p in site.get("people") or []:
            people.append({"name": p["name"], "title": p.get("title"), "email": p.get("email"), "source": p.get("source_url", "website"), "_rank": 3})
        people = [p for p in people if p["name"] and 2 <= len(p["name"].split()) <= 4]
        people.sort(key=lambda p: (title_tier(p.get("title"), icp), p["_rank"]))
        dm = people[0] if people else {}
        first, last = split_name(dm.get("name"))
        tier = title_tier(dm.get("title"), icp) if dm else 4
        if dm and tier == 4 and not enr.get("decision_maker"):
            # a name with no title at all is a weak pick; keep it but flag
            pass
        r.update({"first_name": first, "last_name": last, "title": dm.get("title") or "", "title_tier": tier if dm else "",
                  "linkedin_url": dm.get("linkedin_url") or "", "phone": dm.get("phone") or c.get("phone") or (site.get("phones") or [""])[0]})

        # ---- email
        all_emails = set(site.get("emails") or [])
        all_emails |= {p["email"].lower() for p in people if p.get("email")}
        all_emails = {e for e in all_emails if "@" in e}
        email, etype, esrc = "", "", ""
        if dm.get("email"):
            email, etype, esrc = dm["email"].lower(), "personal", dm.get("source") or "website"
        else:
            match = [e for e in all_emails if email_matches_person(e, first, last)]
            if match:
                email, etype, esrc = match[0], "personal", "matched to decision maker name"
        if not email and all_emails:
            order = {"owner_role": 0, "probably_personal": 1, "generic": 2, "freemail": 3, "external": 4}
            ranked = sorted(all_emails, key=lambda e: order[classify_email(e, icp, dom)])
            email = ranked[0]
            etype = classify_email(email, icp, dom)
            if etype == "probably_personal" and first and not email_matches_person(email, first, last):
                etype = "probably_personal"
            esrc = "website"
        r.update({"email": email, "email_type": etype, "email_source": esrc,
                  "email_status": (verification.get(email) or {}).get("status", "") if email else "",
                  "other_emails": ";".join(sorted(all_emails - {email}))[:300]})

        # ---- status + priority
        verified_ok = conf in ("high", "medium") or (conf == "low" and args.allow_low_confidence) or not s.get("require_verified", True)
        if reason:
            r["status"], r["reason"] = "excluded", reason
        elif n is None or not verified_ok or n_weak:
            r["status"], r["reason"] = "review", f"{s['metric']} not verified" + (f" (at least {n} seen, {conf} confidence)" if n_weak else "")
        elif not email:
            r["status"], r["reason"] = "needs_enrichment", "no email" + ("" if first else ", no named decision maker")
        elif not first or etype not in ("personal",):
            r["status"] = "ready_generic"
            r["reason"] = "no named decision maker" if not first else f"email is {etype}, not personal"
        else:
            r["status"] = "ready"
        if r["email_status"] in ("invalid_syntax", "no_mx", "smtp_rejected") and r["status"] in ("ready", "ready_generic"):
            r["status"], r["reason"] = "needs_enrichment", f"email failed verification ({r['email_status']})"
        score = 0
        score += 3 if r["sweet_spot"] == "yes" else 0
        score += 3 if etype == "personal" else (1 if etype in ("owner_role", "probably_personal") else 0)
        score += 2 if tier == 1 else (1 if tier == 2 else 0)
        score += 2 if (r["hiring_signals"] or r["growth_signals"] or r["buying_signals"]) else 0
        score += 1 if r["tech"] else 0
        score += 1 if conf == "high" else 0
        score += int((llm or {}).get("fit_score") or 0) // 25
        score += int((bt_cfg.get("priority_bonus") or {}).get(buyer or "", 0))
        r["priority"] = score
        rows.append(r)

    rows.sort(key=lambda r: -int(r["priority"] or 0))

    def dump(name, subset):
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(subset)

    ready = [r for r in rows if r["status"] in ("ready", "ready_generic")]
    dump("leads.csv", ready)
    dump("needs_enrichment.csv", [r for r in rows if r["status"] in ("needs_enrichment", "ready_generic", "review")])
    dump("review.csv", [r for r in rows if r["status"] == "review"])
    dump("excluded.csv", [r for r in rows if r["status"] == "excluded"])
    dump("all_companies.csv", rows)

    target = icp.get("batch_target", 100)
    seg_counts = {}
    for seg in icp.get("segments", []):
        k = seg["key"]
        if "rule" in seg:
            seg_counts[k] = sum(1 for r in ready if k in r["flags"].split(";"))
        else:
            seg_counts[k] = sum(1 for r in ready if r["segment"] == k)
    summary = {
        "run_id": run_id, "icp": icp["slug"],
        "companies": len(rows),
        "ready_personal_email": sum(1 for r in rows if r["status"] == "ready"),
        "ready_generic_email": sum(1 for r in rows if r["status"] == "ready_generic"),
        "needs_enrichment": sum(1 for r in rows if r["status"] == "needs_enrichment"),
        "review_size": sum(1 for r in rows if r["status"] == "review"),
        "excluded": sum(1 for r in rows if r["status"] == "excluded"),
        "top_exclusion_reasons": top_reasons(rows),
        "segments_in_ready": seg_counts,
        "buyer_types_in_ready": {b: sum(1 for r in ready if r["buyer_type"] == b) for b in (icp.get("buyer_types", {}).get("options") or {})},
        "segment_targets": {seg["key"]: round(seg.get("target_share", 0) * target) for seg in icp.get("segments", [])},
        "llm_used": bool(llm_cache),
    }
    write_json(out / "summary.json", summary)
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(ledger.summary())


def top_reasons(rows):
    counts = {}
    for r in rows:
        if r["status"] == "excluded":
            key = re.sub(r"\d+", "N", r["reason"].split(":")[0])[:40]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:6])


if __name__ == "__main__":
    main()
