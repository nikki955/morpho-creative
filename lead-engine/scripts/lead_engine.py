#!/usr/bin/env python3
"""Morpho lead engine: one CLI, many ICPs.

  lead_engine.py doctor                                  check keys, libraries, which sources are live
  lead_engine.py list-icps
  lead_engine.py new-icp --slug rv-park-owners [--prompt "..."]   draft an ICP file (Claude drafts it if a key exists)
  lead_engine.py discover --icp <slug> --run-id ID --source places --regions KEY | --states "Vermont, USA" [--confirm] [--max-requests N]
  lead_engine.py discover --icp <slug> --run-id ID --import-csv file.csv        (no region, no API)
  lead_engine.py read     --icp <slug> [--run-id ID] [--firecrawl-fallback]
  lead_engine.py qualify  --icp <slug> [--run-id ID] [--llm]
  lead_engine.py enrich   --icp <slug> [--run-id ID] --verify-size | --find-people [--linkedin] [--max N]
  lead_engine.py verify   --icp <slug> [--run-id ID] [--smtp]
  lead_engine.py export   --icp <slug> [--run-id ID]
  lead_engine.py push     --icp <slug> --run-id ID                 dry run: CRM lookups, suppression check, proposed tags
  lead_engine.py push     --icp <slug> --run-id ID --live --approve ID [--limit N]
  lead_engine.py status   --icp <slug> [--run-id ID]
  lead_engine.py scrub-places --icp <slug> --run-id ID --yes      strip Google Places content from an older run
  lead_engine.py run      --icp <slug> --run-id ID (--source places --regions KEY --confirm | --import-csv f) [--llm]
                          discover > read > qualify > verify > qualify > export. Never pushes.

Every step is resumable and idempotent. The run id doubles as the CRM batch tag. API discovery always needs an
explicit region or state list and --confirm; nothing paid runs by default.
"""
import importlib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ICP_DIR, SKILL_DIR, Ledger, default_run_id, env, list_icps, load_icp, read_json, run_dir, validate_icp, write_json  # noqa: E402

STEPS = {"discover": "discover", "read": "reader", "qualify": "qualify", "enrich": "enrich", "verify": "verify"}


def doctor():
    print("Morpho lead engine doctor\n")
    for mod in ("requests", "bs4", "dns.resolver", "anthropic"):
        try:
            importlib.import_module(mod)
            print(f"  ok   python module {mod}")
        except ImportError:
            hint = {"bs4": "pip install beautifulsoup4", "dns.resolver": "pip install dnspython (optional, better email checks)",
                    "anthropic": "pip install anthropic (optional, for --llm qualification)"}.get(mod, f"pip install {mod}")
            print(f"  MISSING python module {mod}: {hint}")
    print()
    keys = {
        "GOOGLE_PLACES_API_KEY": "discovery via Google Places (1,000 requests/month free, ~20 businesses each)",
        "FIRECRAWL_API_KEY": "discovery via web search + JS-site fallback + people search (1,000 credits/month free)",
        "ANTHROPIC_API_KEY": "LLM qualification (--llm), pennies per 100 companies",
        "GHL_API_KEY": "push to FG Funnels",
        "GHL_LOCATION_ID": "push to FG Funnels",
    }
    for k, why in keys.items():
        print(f"  {'set    ' if env(k) else 'not set'} {k}: {why}")
    print()
    src = "places" if env("GOOGLE_PLACES_API_KEY") else ("search" if env("FIRECRAWL_API_KEY") else "none (use --import-csv)")
    print(f"  default discovery source: {src}")
    print(f"  LLM model for --llm: {env('LEAD_ENGINE_MODEL', 'claude-haiku-5-5')}")
    print(f"  ICPs: {', '.join(list_icps()) or 'none yet'}")
    print(f"  runs go to: {env('LEAD_ENGINE_DIR', str(Path.cwd() / 'leads'))}")
    g = read_json(SKILL_DIR / "config" / "crm_guardrails.json", {}) or {}
    live = [k for k in (g.get("live_trigger_tags") or {}) if not k.startswith("_")]
    print(f"  CRM guardrails: {len(live)} live trigger tags refused ({', '.join(live)}), "
          f"{len((g.get('planned_track_tags') or {}).get('tags') or [])} reserved track tags refused")
    print("  Google Places: only place_id is stored; names, phones, addresses come from the company website")


NEW_ICP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string", "description": "who qualifies and who does not, concrete, 80-160 words"},
        "offer_context": {"type": "string"},
        "search_terms": {"type": "array", "items": {"type": "string"}, "description": "4-6 Google Maps style category phrases"},
        "size": {"type": "object", "properties": {
            "metric": {"type": "string"}, "label": {"type": "string"}, "min": {"type": "integer"}, "max": {"type": "integer"},
            "sweet_min": {"type": "integer"}, "sweet_max": {"type": "integer"},
            "patterns": {"type": "array", "items": {"type": "string"}, "description": "Python regexes with ONE capture group for the number, e.g. (\\d{1,4})\\+?\\s*(?:rooms|suites)"},
            "listing_url_patterns": {"type": "array", "items": {"type": "string"}},
            "listing_index_paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["metric", "label", "min", "max", "sweet_min", "sweet_max", "patterns", "listing_url_patterns", "listing_index_paths"],
            "additionalProperties": False},
        "segments": {"type": "array", "items": {"type": "object", "properties": {
            "key": {"type": "string"}, "label": {"type": "string"}, "min": {"type": ["integer", "null"]}, "max": {"type": ["integer", "null"]},
            "rule": {"type": ["string", "null"], "enum": ["no_direct_booking", None]}, "target_share": {"type": "number"}},
            "required": ["key", "label", "min", "max", "rule", "target_share"], "additionalProperties": False}},
        "business_types": {"type": "object", "properties": {
            "accept": {"type": "array", "items": {"type": "string"}},
            "options": {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "description": {"type": "string"}},
                                                   "required": ["key", "description"], "additionalProperties": False}}},
            "required": ["accept", "options"], "additionalProperties": False},
        "decision_makers": {"type": "object", "properties": {"primary": {"type": "array", "items": {"type": "string"}},
                                                             "secondary": {"type": "array", "items": {"type": "string"}}},
                            "required": ["primary", "secondary"], "additionalProperties": False},
        "exclude": {"type": "object", "properties": {"domains": {"type": "array", "items": {"type": "string"}},
                                                     "category_keywords": {"type": "array", "items": {"type": "string"}},
                                                     "text_signals": {"type": "array", "items": {"type": "string"}}},
                    "required": ["domains", "category_keywords", "text_signals"], "additionalProperties": False},
        "tech": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "signatures": {"type": "array", "items": {"type": "string"}}},
                                            "required": ["name", "signatures"], "additionalProperties": False},
                 "description": "software this ICP typically runs, with lowercase strings that appear in page HTML when it is installed"},
        "direct_booking": {"type": "array", "items": {"type": "string"}},
        "hiring": {"type": "array", "items": {"type": "string"}},
        "growth": {"type": "array", "items": {"type": "string"}},
        "contact_keywords": {"type": "array", "items": {"type": "string"}},
        "icp_tag": {"type": "string", "description": "lowercase kebab-case CRM tag starting with morpho-"},
    },
    "required": ["name", "description", "offer_context", "search_terms", "size", "segments", "business_types", "decision_makers",
                 "exclude", "tech", "direct_booking", "hiring", "growth", "contact_keywords", "icp_tag"],
    "additionalProperties": False,
}


def new_icp(slug_, prompt, locations):
    path = ICP_DIR / f"{slug_}.json"
    if path.exists():
        sys.exit(f"{path} already exists. Edit it or pick another slug.")
    template = json.loads((ICP_DIR / "_template.json").read_text())
    icp = template
    icp["slug"] = slug_
    icp["crm"]["icp_tag"] = f"morpho-{slug_}"
    if locations:
        icp["locations"] = locations
    if prompt:
        from providers import llm_available, llm_json, llm_model
        if not llm_available():
            print("No ANTHROPIC_API_KEY, so writing the template for you to fill in by hand.")
        else:
            print(f"Drafting the ICP with {llm_model()} ...")
            example = json.loads((ICP_DIR / "str-portfolio-managers.json").read_text())
            system = ("You design ideal customer profile (ICP) configs for a local-business lead engine. "
                      "The engine searches Google Maps style categories by location, reads company websites, counts a size metric "
                      "from stated numbers and listing pages, fingerprints software, and finds the decision maker. "
                      "Produce precise, conservative values. Regex patterns must be valid Python with exactly one capture group for the number. "
                      "Here is a complete example ICP for reference:\n" + json.dumps(example, indent=1))
            ledger = Ledger(run_dir("_icp-drafts"))
            data, note = llm_json(system, f"Draft the ICP for: {prompt}", NEW_ICP_SCHEMA, ledger, max_tokens=6000)
            if not data:
                print(f"Draft failed ({note}); writing the template instead.")
            else:
                icp.update({"name": data["name"], "description": data["description"], "offer_context": data["offer_context"],
                            "search_terms": data["search_terms"]})
                icp["size"] = {**data["size"], "require_verified": True}
                for p in list(icp["size"]["patterns"]):
                    try:
                        if re.compile(p).groups != 1:
                            icp["size"]["patterns"].remove(p)
                    except re.error:
                        icp["size"]["patterns"].remove(p)
                icp["segments"] = [{k: v for k, v in s.items() if v is not None} for s in data["segments"]]
                icp["business_types"] = {"accept": data["business_types"]["accept"],
                                         "options": {o["key"]: o["description"] for o in data["business_types"]["options"]}}
                icp["decision_makers"] = data["decision_makers"]
                icp["exclude"] = data["exclude"]
                icp["signals"] = {"tech": {t["name"]: t["signatures"] for t in data["tech"]}, "direct_booking": data["direct_booking"],
                                  "ota_links": template["signals"]["ota_links"], "hiring": data["hiring"], "growth": data["growth"]}
                icp["pages"]["contact_keywords"] = sorted(set(template["pages"]["contact_keywords"] + data["contact_keywords"]))
                icp["crm"] = {"icp_tag": data["icp_tag"] if data["icp_tag"].startswith("morpho-") else f"morpho-{slug_}",
                              "source": "Morpho lead engine", "note_header": f"{data['name']} lead"}
                print(ledger.summary())
    errs = validate_icp(icp)
    path.write_text(json.dumps(icp, indent=2, ensure_ascii=False))
    print(f"Wrote {path}" + (f"\nFix before use: {errs}" if errs else ""))
    print("Open it, check locations, size range, search terms and exclusions, then: lead_engine.py discover --icp " + slug_)


def status(slug_, run_id):
    icp = load_icp(slug_)
    run_id = run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    print(f"Run {run_id} ({icp['name']}) in {out}\n")
    companies = read_json(out / "companies.json", []) or []
    read_n = len(list((out / "site_data").glob("*.json")))
    print(f"  discovered: {len(companies)}   read: {read_n}   llm classified: {len(read_json(out / 'llm.json', {}) or {})}")
    summ = read_json(out / "summary.json")
    if summ:
        for k in ("ready_personal_email", "ready_generic_email", "needs_enrichment", "review_size", "excluded", "segments_in_ready", "segment_targets"):
            print(f"  {k}: {summ.get(k)}")
    ver = read_json(out / "verification.json", {}) or {}
    if ver:
        counts = {}
        for v in ver.values():
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        print(f"  email verification: {counts}")
    for f in ("leads.csv", "needs_enrichment.csv", "review.csv", "excluded.csv", "export_instantly.csv", "crm_results.csv"):
        if (out / f).exists():
            print(f"  file: {out / f}")
    print()
    print(Ledger(out).summary())


def run_all(argv):
    """discover > read > qualify > verify > qualify > export with shared flags. Never pushes to the CRM."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--regions", help="required for API discovery; no default")
    ap.add_argument("--states", nargs="*")
    ap.add_argument("--source", choices=["places", "search", "both", "import"])
    ap.add_argument("--confirm", action="store_true", help="execute the paid discovery plan")
    ap.add_argument("--max-requests", type=int, default=100)
    ap.add_argument("--import-csv")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--firecrawl-fallback", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    icp = load_icp(a.icp)
    run_id = a.run_id or default_run_id(icp["slug"])
    base = ["--icp", a.icp, "--run-id", run_id]
    d = base + ["--max-requests", str(a.max_requests)]
    d += ["--regions", a.regions] if a.regions else []
    d += ["--states", *a.states] if a.states else []
    d += ["--source", a.source] if a.source else []
    d += ["--import-csv", a.import_csv] if a.import_csv else []
    d += ["--confirm"] if a.confirm else []
    importlib.import_module("discover").main(d)
    out = run_dir(run_id)
    if not (read_json(out / "companies.json", []) or []):
        print("No companies yet (plan printed without --confirm, or nothing found). Stopping before read.")
        return
    importlib.import_module("reader").main(base + ["--workers", str(a.workers)] + (["--firecrawl-fallback"] if a.firecrawl_fallback else []))
    importlib.import_module("qualify").main(base + (["--llm"] if a.llm else []))
    importlib.import_module("verify").main(base + ["--all"])
    importlib.import_module("qualify").main(base)  # re-qualify so failed emails leave leads.csv before export
    importlib.import_module("crm").main(["export"] + base)
    print("\nDone. Next: review leads.csv, `enrich` for the gaps, then `push` (dry run first; --live needs --approve <run-id>).")


PLACES_ONLY_FIELDS = ("rating", "reviews", "maps_url", "category", "address", "apify_emails", "apify_people", "gmb_listings")


def scrub_places(slug_, run_id, yes):
    """Strip Google Places content from a run folder created before the retention redesign."""
    icp = load_icp(slug_)
    out = run_dir(run_id)
    companies = read_json(out / "companies.json", []) or []
    raw = read_json(out / "raw_discovery.json", []) or []
    raw_places = [r for r in raw if r.get("_source") == "places" and not r.get("_marker")]
    affected = [c for c in companies if "places" in (c.get("sources") or []) or any(k in c for k in PLACES_ONLY_FIELDS)]
    print(f"Run {run_id}: {len(companies)} companies, {len(affected)} carry Places-derived fields, "
          f"{len(raw_places)} raw Places records in raw_discovery.json")
    if not affected and not raw_places:
        print("Nothing to scrub.")
        return
    if not yes:
        print("Dry run. Add --yes to rewrite companies.json and raw_discovery.json in place. site_data, leads.csv and")
        print("the CRM are not touched; rerun qualify afterwards so the CSVs are rebuilt from website data only.")
        return
    for c in companies:
        if c not in affected:
            continue
        site = read_json(out / "site_data" / f"{c['domain']}.json", {}) or {}
        for k in PLACES_ONLY_FIELDS:
            c.pop(k, None)
        if "places" in (c.get("sources") or []):
            c["sources"] = [("website_via_places" if s == "places" else s) for s in c["sources"]]
            c["name"] = site.get("org_name") or site.get("title") or c["domain"]
            c["phone"] = (site.get("phones") or [None])[0]
            addr = site.get("jsonld_address") or {}
            c["city"], c["state"], c["postal_code"] = addr.get("city"), addr.get("state"), addr.get("postal_code")
            c["attribution"] = {"name": "company website", "phone": "company website", "address": "company website (JSON-LD)",
                                "domain": "company website", "scrubbed": "Places fields removed by scrub-places"}
    raw = [r for r in raw if not (r.get("_source") == "places" and not r.get("_marker"))]
    write_json(out / "companies.json", companies)
    write_json(out / "raw_discovery.json", raw)
    print(f"Scrubbed {len(affected)} companies and removed {len(raw_places)} raw Places records. Now rerun: qualify --icp {slug_} --run-id {run_id}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "doctor":
        return doctor()
    if cmd == "list-icps":
        for s in list_icps():
            icp = json.loads((ICP_DIR / f"{s}.json").read_text())
            print(f"  {s}: {icp.get('name')}  (size {icp['size']['min']}-{icp['size']['max']} {icp['size']['metric']}, regions {list(icp['locations'])})")
        return
    if cmd == "new-icp":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--slug", required=True)
        ap.add_argument("--prompt")
        ap.add_argument("--locations-from", help="copy locations from an existing ICP slug")
        a = ap.parse_args(rest)
        locs = json.loads((ICP_DIR / f"{a.locations_from}.json").read_text())["locations"] if a.locations_from else None
        return new_icp(re.sub(r"[^a-z0-9-]+", "-", a.slug.lower()).strip("-"), a.prompt, locs)
    if cmd == "status":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--icp", required=True)
        ap.add_argument("--run-id")
        a = ap.parse_args(rest)
        return status(a.icp, a.run_id)
    if cmd == "run":
        return run_all(rest)
    if cmd == "scrub-places":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--icp", required=True)
        ap.add_argument("--run-id", required=True)
        ap.add_argument("--yes", action="store_true")
        a = ap.parse_args(rest)
        return scrub_places(a.icp, a.run_id, a.yes)
    if cmd in ("export", "push"):
        return importlib.import_module("crm").main([cmd] + rest)
    if cmd in STEPS:
        return importlib.import_module(STEPS[cmd]).main(rest)
    sys.exit(f"Unknown command {cmd}.\n{__doc__}")


if __name__ == "__main__":
    main()
