#!/usr/bin/env python3
"""Step 6: Export leads, or push them into FG Funnels (HighLevel / LeadConnector API v2).

export  Writes <run>/export_instantly.csv (Instantly's CSV import columns plus custom variables) and
        <run>/export_leads.csv (every column). Free, no keys needed.

push    Dry run by default. Nothing is written to the CRM unless you pass --live.
        Per lead: upsert contact by email, ADD tags (never wipes existing tags), add a note with the
        qualification evidence (new contacts only unless --note-always). Then counts contacts
        carrying the batch tag so the Smartlist can be checked.

Usage:
  python3 crm.py export --icp str-portfolio-managers --run-id <id>
  python3 crm.py push   --icp str-portfolio-managers --run-id <id>                  # dry run
  python3 crm.py push   --icp str-portfolio-managers --run-id <id> --live --limit 5
  python3 crm.py push   --icp str-portfolio-managers --run-id <id> --live
Flags for push: --personal-only, --verified-only, --allow-no-email, --batch-tag, --note-always
"""
import argparse
import csv
import json
import re
import time

import requests

from common import default_run_id, http_retry, load_icp, require_env, run_dir, slug

BASE = "https://services.leadconnectorhq.com"
INSTANTLY_COLS = ["email", "first_name", "last_name", "company_name", "website", "phone", "personalization",
                  "city", "state", "size", "segment", "title", "tech", "buying_signal", "icp", "batch"]


def e164(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def tags_for(r, icp, batch_tag):
    t = [icp["crm"]["icp_tag"], batch_tag, f"seg-{r['segment']}" if r["segment"] else "seg-unknown"]
    for fl in filter(None, (r.get("flags") or "").split(";")):
        t.append(f"seg-{fl}")
    if r["sweet_spot"] == "yes":
        t.append("size-sweet-spot")
    t.append(f"email-{r['email_type'] or 'missing'}" if r["email"] else "email-missing")
    if r.get("email_status") in ("valid", "mx_ok", "catch_all"):
        t.append(f"email-{r['email_status'].replace('_', '-')}")
    if str(r["title_tier"]) == "1":
        t.append("dm-primary")
    elif str(r["title_tier"]) == "2":
        t.append("dm-secondary")
    for p in filter(None, (r.get("tech") or "").split(";")):
        t.append(f"tech-{slug(p)}")
    if r.get("hiring_signals") or r.get("growth_signals") or r.get("buying_signals"):
        t.append("signal-growth")
    return t


def note_for(r, icp, batch_tag):
    s = icp["size"]
    lines = [
        f"{icp['crm'].get('note_header', icp['name'])} ({batch_tag})",
        f"Company: {r['company']} | {r['website']}",
        f"{s.get('label', s['metric']).capitalize()}: {r['size'] or 'unknown'} ({r['size_confidence']} confidence, source: {r['size_source']})",
        f"Evidence: {r['size_evidence']}" if r["size_evidence"] else "",
        f"Segment: {r['segment'] or 'unknown'} | Flags: {r['flags'] or 'none'} | Sweet spot: {r['sweet_spot']}",
        f"Business type: {r['business_type']} | Fit score: {r['fit_score']}" if r.get("business_type") else "",
        (f"Decision maker: {r['first_name']} {r['last_name']} ({r['title'] or 'title unknown'})" if r["first_name"] else "Decision maker: not found yet"),
        f"Email type: {r['email_type']} | Status: {r['email_status'] or 'unchecked'} | Source: {r['email_source']}",
        f"LinkedIn: {r['linkedin_url']}" if r["linkedin_url"] else "",
        f"Tech: {r['tech']}" if r["tech"] else "Tech: none detected",
        f"Hiring: {r['hiring_signals']}" if r["hiring_signals"] else "",
        f"Growth: {r['growth_signals']}" if r["growth_signals"] else "",
        f"Buying signals: {r['buying_signals']}" if r.get("buying_signals") else "",
        f"Summary: {r['summary']}" if r.get("summary") else "",
        f"Other emails: {r['other_emails']}" if r["other_emails"] else "",
        f"Notes: {r['notes']}" if r["notes"] else "",
        f"Priority score: {r['priority']}",
    ]
    return "\n".join(l for l in lines if l)


class GHL:
    def __init__(self, token, location_id, live):
        self.loc, self.live = location_id, live
        self.h = {"Authorization": f"Bearer {token}", "Version": "2021-07-28",
                  "Accept": "application/json", "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        if not self.live and method != "GET" and not path.startswith("/contacts/search"):
            print(f"  DRY {method} {path} {json.dumps(body)[:300]}")
            return {"dry_run": True, "contact": {"id": "DRY"}, "new": True}
        r = http_retry(lambda: requests.request(method, BASE + path, headers=self.h, json=body, timeout=30))
        time.sleep(0.12)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def ensure_tag(self, name):
        try:
            self.call("POST", f"/locations/{self.loc}/tags", {"name": name})
        except RuntimeError as e:
            if "exist" not in str(e).lower() and "400" not in str(e):
                print(f"  tag warn {name}: {e}")

    def count_tag(self, tag):
        body = {"locationId": self.loc, "pageLimit": 1, "filters": [{"field": "tags", "operator": "contains", "value": tag}]}
        try:
            r = requests.post(BASE + "/contacts/search", headers=self.h, json=body, timeout=30)
            return r.json().get("total") if r.status_code < 300 else f"search failed {r.status_code}"
        except Exception as e:
            return f"search failed {e}"


def load_rows(out, args):
    rows = list(csv.DictReader(open(out / "leads.csv")))
    if getattr(args, "personal_only", False):
        rows = [r for r in rows if r["email_type"] == "personal"]
    if getattr(args, "verified_only", False):
        rows = [r for r in rows if r.get("email_status") in ("valid", "mx_ok", "catch_all")]
    if getattr(args, "allow_no_email", False) and (out / "needs_enrichment.csv").exists():
        rows += [r for r in csv.DictReader(open(out / "needs_enrichment.csv"))
                 if r["status"] == "needs_enrichment" and not r["email"] and e164(r["phone"])]
    if getattr(args, "limit", 0):
        rows = rows[:args.limit]
    return rows


def cmd_export(args, icp, out, batch_tag):
    rows = load_rows(out, args)
    with open(out / "export_instantly.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INSTANTLY_COLS)
        w.writeheader()
        for r in rows:
            if not r["email"]:
                continue
            signal = (r.get("buying_signals") or r.get("growth_signals") or r.get("hiring_signals") or "").split(" | ")[0][:140]
            w.writerow({"email": r["email"], "first_name": r["first_name"], "last_name": r["last_name"],
                        "company_name": r["company"], "website": r["website"], "phone": e164(r["phone"]) or "",
                        "personalization": r.get("summary", "")[:200], "city": r["city"], "state": r["state"],
                        "size": r["size"], "segment": r["segment"], "title": r["title"], "tech": r["tech"],
                        "buying_signal": signal, "icp": icp["slug"], "batch": batch_tag})
    with open(out / "export_leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["email"])
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} leads: {out / 'export_instantly.csv'} and {out / 'export_leads.csv'}")


def cmd_push(args, icp, out, batch_tag):
    token, loc = require_env("GHL_API_KEY", "GHL_LOCATION_ID")
    rows = load_rows(out, args)
    if not rows:
        raise SystemExit("No leads to push. Check leads.csv and your flags.")
    ghl = GHL(token, loc, args.live)
    print(f"{'LIVE' if args.live else 'DRY RUN'}: pushing {len(rows)} leads with batch tag '{batch_tag}'")
    for t in sorted({t for r in rows for t in tags_for(r, icp, batch_tag)}):
        ghl.ensure_tag(t)
    results = []
    for i, r in enumerate(rows, 1):
        body = {"locationId": loc, "firstName": r["first_name"] or None, "lastName": r["last_name"] or None,
                "email": r["email"] or None, "phone": e164(r["phone"]), "companyName": r["company"], "website": r["website"],
                "city": r["city"] or None, "state": r["state"] or None, "country": "US", "source": icp["crm"].get("source", "Morpho lead engine")}
        body = {k: v for k, v in body.items() if v not in (None, "")}
        res = {"company": r["company"], "email": r["email"], "contact_id": "", "new": "", "error": ""}
        try:
            up = ghl.call("POST", "/contacts/upsert", body)
            cid = (up.get("contact") or {}).get("id")
            res["contact_id"], res["new"] = cid, up.get("new")
            ghl.call("POST", f"/contacts/{cid}/tags", {"tags": tags_for(r, icp, batch_tag)})
            if up.get("new") or args.note_always:
                ghl.call("POST", f"/contacts/{cid}/notes", {"body": note_for(r, icp, batch_tag)})
            print(f"[{i}/{len(rows)}] ok {r['company']} <{r['email']}> {'new' if up.get('new') else 'updated'}")
        except Exception as e:
            res["error"] = str(e)[:300]
            print(f"[{i}/{len(rows)}] FAIL {r['company']}: {e}")
        results.append(res)
    with open(out / ("crm_results.csv" if args.live else "crm_dryrun.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    ok = sum(1 for x in results if not x["error"])
    print(f"\n{ok}/{len(results)} succeeded.")
    if args.live:
        print(f"Contacts in CRM with tag '{batch_tag}': {ghl.count_tag(batch_tag)}")
    print(f"Smartlist filter: Tags  is  {batch_tag}   (name it: {icp['name']} | {batch_tag})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["export", "push"])
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--batch-tag", help="defaults to the run id")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--personal-only", action="store_true")
    ap.add_argument("--verified-only", action="store_true")
    ap.add_argument("--allow-no-email", action="store_true")
    ap.add_argument("--note-always", action="store_true")
    args = ap.parse_args(argv)
    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    if not (out / "leads.csv").exists():
        raise SystemExit("leads.csv missing. Run qualify first.")
    batch_tag = slug(args.batch_tag or run_id)
    (cmd_export if args.command == "export" else cmd_push)(args, icp, out, batch_tag)


if __name__ == "__main__":
    main()
