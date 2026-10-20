#!/usr/bin/env python3
"""Step 6: Export leads, or push them into FG Funnels (HighLevel / LeadConnector API v2).

export  Writes <run>/export_instantly.csv (Instantly's CSV import columns plus custom variables) and
        <run>/export_leads.csv (every column). Free, no keys needed.

push    Dry run by default. Nothing is written to the CRM unless --live AND --approve <run-id> are
        both passed (that is Nikki's explicit approval for this batch). Per lead:
          1. look the email up in the CRM (read only)
          2. skip if suppressed (DND, email DND, unsubscribe/bounce/do-not-contact tags) and record why
          3. skip if the contact already carries a Morpho track, pipeline or old-list tag
             (existing relationship), unless --update-existing, which adds tags only
          4. new contact: create via upsert, ADD tags, add a note
          5. existing untracked contact: ADD tags and a note; fields are never overwritten
        Tags are checked against config/crm_guardrails.json: anything that equals a live workflow
        trigger or a planned track tag aborts the run before any write.

Email eligibility (both destinations): invalid_syntax, no_mx and smtp_rejected are never exported or
pushed. 'mx_ok' means DNS found a mail server, 'catch_all' means the server accepts any address, and
only 'valid' means the mailbox itself answered. Unchecked emails are skipped by default; run verify.

Usage:
  python3 crm.py export --icp inns --run-id <id>
  python3 crm.py push   --icp inns --run-id <id>                          # dry run: lookups + plan, no writes
  python3 crm.py push   --icp inns --run-id <id> --live --approve <id> --limit 5
  python3 crm.py push   --icp inns --run-id <id> --live --approve <id>
Flags: --personal-only, --verified-only (mailbox-verified), --dns-ok-only, --allow-unverified,
       --allow-invalid (export only), --allow-no-email, --update-existing, --batch-tag, --note-always
"""
import argparse
import csv
import json
import re
import time

import requests

from common import SKILL_DIR, default_run_id, http_retry, load_icp, read_json, require_env, run_dir, slug

BASE = "https://services.leadconnectorhq.com"
GUARDRAILS = read_json(SKILL_DIR / "config" / "crm_guardrails.json", {}) or {}
INSTANTLY_COLS = ["email", "email_status", "first_name", "last_name", "company_name", "website", "phone", "personalization",
                  "city", "state", "size", "segment", "buyer_type", "title", "tech", "buying_signal", "icp", "batch"]
INVALID_EMAIL_STATUSES = {"invalid_syntax", "no_mx", "smtp_rejected"}
CHECKED_OK_STATUSES = {"valid", "mx_ok", "catch_all"}
EMAIL_STATUS_TAG = {"valid": "email-mailbox-verified", "mx_ok": "email-dns-ok", "catch_all": "email-catch-all"}


def e164(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


# ------------------------------------------------------------------ tags and guardrails
def tags_for(r, icp, batch_tag):
    t = [icp["crm"]["icp_tag"], batch_tag, f"seg-{r['segment']}" if r["segment"] else "seg-unknown"]
    for fl in filter(None, (r.get("flags") or "").split(";")):
        t.append(f"seg-{fl}")
    if r.get("buyer_type"):
        t.append(f"buyer-{slug(r['buyer_type'])}")
    if r["sweet_spot"] == "yes":
        t.append("size-sweet-spot")
    t.append(f"email-{r['email_type'] or 'missing'}" if r["email"] else "email-missing")
    if r.get("email_status") in EMAIL_STATUS_TAG:
        t.append(EMAIL_STATUS_TAG[r["email_status"]])
    if str(r["title_tier"]) == "1":
        t.append("dm-primary")
    elif str(r["title_tier"]) == "2":
        t.append("dm-secondary")
    for p in filter(None, (r.get("tech") or "").split(";")):
        t.append(f"tech-{slug(p)}")
    if r.get("hiring_signals") or r.get("growth_signals") or r.get("buying_signals"):
        t.append("signal-growth")
    return t


def protected_tags():
    live = {k for k in (GUARDRAILS.get("live_trigger_tags") or {}) if not k.startswith("_")}
    planned = set((GUARDRAILS.get("planned_track_tags") or {}).get("tags") or [])
    return live, planned, tuple(GUARDRAILS.get("protected_prefixes") or [])


def check_tags(tags):
    """Return a list of problems. Empty list means every tag is safe to add."""
    live, planned, prefixes = protected_tags()
    problems = []
    for t in sorted(set(tags)):
        tl = t.lower()
        if tl in live:
            problems.append(f"'{t}' is a LIVE workflow trigger ({GUARDRAILS['live_trigger_tags'][tl]}). Adding it sends email.")
        elif tl in planned:
            problems.append(f"'{t}' is a reserved track tag from the CRM rebuild plan.")
        elif tl.startswith(prefixes):
            problems.append(f"'{t}' starts with a protected prefix {[p for p in prefixes if tl.startswith(p)]}.")
    return problems


def suppression_reason(contact):
    """Why an existing CRM contact must not be touched, or None."""
    cfg = GUARDRAILS.get("suppression") or {}
    if cfg.get("dnd_flag") and contact.get("dnd") is True:
        return "DND (do not disturb) is on"
    email_dnd = ((contact.get("dndSettings") or {}).get("Email") or {})
    if cfg.get("dnd_email_channel") and str(email_dnd.get("status", "")).lower() == "active":
        return f"email channel DND active ({email_dnd.get('code') or email_dnd.get('message') or 'no reason given'})"
    tags = [str(t).lower() for t in (contact.get("tags") or [])]
    for sub in cfg.get("tag_substrings") or []:
        hit = next((t for t in tags if sub in t), None)
        if hit:
            return f"suppression tag '{hit}'"
    return None


def existing_relationship(contact):
    subs = (GUARDRAILS.get("existing_relationship") or {}).get("tag_substrings") or []
    tags = [str(t).lower() for t in (contact.get("tags") or [])]
    hits = sorted({t for t in tags for sub in subs if (t == sub or t.startswith(sub) or (len(sub) > 3 and sub in t))})
    return hits


def note_for(r, icp, batch_tag):
    s = icp["size"]
    lines = [
        f"{icp['crm'].get('note_header', icp['name'])} ({batch_tag})",
        f"Company: {r['company']} | {r['website']}",
        f"{s.get('label', s['metric']).capitalize()}: {r['size'] or 'unknown'} ({r['size_confidence']} confidence, source: {r['size_source']})",
        f"Evidence: {r['size_evidence']}" if r["size_evidence"] else "",
        f"Segment: {r['segment'] or 'unknown'} | Flags: {r['flags'] or 'none'} | Sweet spot: {r['sweet_spot']}",
        f"Business type: {r['business_type']} | Fit score: {r['fit_score']}" if r.get("business_type") else "",
        f"Buyer type: {r['buyer_type']} ({r.get('buyer_type_evidence', '')})" if r.get("buyer_type") else "",
        f"Ownership change: {r['ownership_signals']}" if r.get("ownership_signals") else "",
        (f"Decision maker: {r['first_name']} {r['last_name']} ({r['title'] or 'title unknown'})" if r["first_name"] else "Decision maker: not found yet"),
        f"Email type: {r['email_type']} | Check: {describe_status(r.get('email_status'))} | Source: {r['email_source']}",
        f"LinkedIn: {r['linkedin_url']}" if r["linkedin_url"] else "",
        f"Tech detected on site: {r['tech']}" if r["tech"] else "Tech detected on site: none",
        f"Direct booking path: {r.get('direct_booking') or 'unknown'}" + (f" ({r['booking_evidence']})" if r.get("booking_evidence") else ""),
        f"Hiring: {r['hiring_signals']}" if r["hiring_signals"] else "",
        f"Growth: {r['growth_signals']}" if r["growth_signals"] else "",
        f"Buying signals: {r['buying_signals']}" if r.get("buying_signals") else "",
        f"Summary: {r['summary']}" if r.get("summary") else "",
        f"Other emails: {r['other_emails']}" if r["other_emails"] else "",
        f"Notes: {r['notes']}" if r["notes"] else "",
        "Data source: company website (and the import file, if any). No Google Places content stored.",
        f"Priority score: {r['priority']}",
    ]
    return "\n".join(l for l in lines if l)


def describe_status(st):
    return {"valid": "mailbox verified by SMTP", "mx_ok": "DNS/MX found, mailbox not verified", "catch_all": "catch-all domain, mailbox not verified",
            "no_mx": "no mail server", "invalid_syntax": "invalid syntax", "smtp_rejected": "mailbox rejected", "": "not checked", None: "not checked"}.get(st, st)


# ------------------------------------------------------------------ CRM client
class GHL:
    def __init__(self, token, location_id, live):
        self.loc, self.live = location_id, live
        self.h = {"Authorization": f"Bearer {token}", "Version": "2021-07-28",
                  "Accept": "application/json", "Content-Type": "application/json"}
        self.writes = 0

    def call(self, method, path, body=None):
        is_read = method == "GET" or path.startswith("/contacts/search")
        if not self.live and not is_read:
            raise RuntimeError("write attempted in dry run")  # belt and braces: cmd_push never gets here in dry run
        if not is_read:
            self.writes += 1
        r = http_retry(lambda: requests.request(method, BASE + path, headers=self.h, json=body, timeout=30))
        time.sleep(0.12)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def lookup(self, email):
        """Find the existing contact for an email. Returns a contact dict, None, or the string 'unavailable'."""
        body = {"locationId": self.loc, "pageLimit": 5, "filters": [{"field": "email", "operator": "eq", "value": email}]}
        try:
            data = self.call("POST", "/contacts/search", body)
        except Exception as e:
            return "unavailable:" + str(e)[:80]
        contacts = data.get("contacts") or []
        return contacts[0] if contacts else None

    def ensure_tag(self, name):
        try:
            self.call("POST", f"/locations/{self.loc}/tags", {"name": name})
        except RuntimeError as e:
            if "exist" not in str(e).lower() and "400" not in str(e):
                print(f"  tag warn {name}: {e}")

    def count_tag(self, tag):
        body = {"locationId": self.loc, "pageLimit": 1, "filters": [{"field": "tags", "operator": "contains", "value": tag}]}
        try:
            return self.call("POST", "/contacts/search", body).get("total")
        except Exception as e:
            return f"search failed {e}"


# ------------------------------------------------------------------ row selection
def eligible(r, args, for_push):
    """Return (ok, reason)."""
    st = (r.get("email_status") or "").strip()
    if not r.get("email"):
        return (True, "") if getattr(args, "allow_no_email", False) and e164(r.get("phone")) else (False, "no email")
    if st in INVALID_EMAIL_STATUSES:
        if not for_push and getattr(args, "allow_invalid", False):
            return True, ""
        return False, f"email failed verification ({st})"
    if getattr(args, "personal_only", False) and r.get("email_type") != "personal":
        return False, "not a personal email"
    if getattr(args, "verified_only", False) and st != "valid":
        return False, "mailbox not verified (only 'valid' counts with --verified-only)"
    if getattr(args, "dns_ok_only", False) and st not in ("valid", "mx_ok"):
        return False, "not valid or mx_ok"
    if st not in CHECKED_OK_STATUSES and not getattr(args, "allow_unverified", False):
        return False, "email not verified yet (run verify, or pass --allow-unverified)"
    return True, ""


def load_rows(out, args, for_push):
    with open(out / "leads.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    if getattr(args, "allow_no_email", False) and (out / "needs_enrichment.csv").exists():
        with open(out / "needs_enrichment.csv", newline="") as f:
            rows += [r for r in csv.DictReader(f) if r["status"] == "needs_enrichment" and not r["email"] and e164(r["phone"])]
    kept, skipped = [], []
    for r in rows:
        ok, why = eligible(r, args, for_push)
        (kept if ok else skipped).append((r, why))
    if getattr(args, "limit", 0):
        kept = kept[:args.limit]
    return [r for r, _ in kept], skipped


# ------------------------------------------------------------------ commands
def cmd_export(args, icp, out, batch_tag):
    rows, skipped = load_rows(out, args, for_push=False)
    with open(out / "export_instantly.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INSTANTLY_COLS)
        w.writeheader()
        for r in rows:
            if not r["email"]:
                continue
            signal = (r.get("buying_signals") or r.get("growth_signals") or r.get("hiring_signals") or "").split(" | ")[0][:140]
            w.writerow({"email": r["email"], "email_status": r.get("email_status", ""), "first_name": r["first_name"], "last_name": r["last_name"],
                        "company_name": r["company"], "website": r["website"], "phone": e164(r["phone"]) or "",
                        "personalization": r.get("summary", "")[:200], "city": r["city"], "state": r["state"],
                        "size": r["size"], "segment": r["segment"], "buyer_type": r.get("buyer_type", ""), "title": r["title"], "tech": r["tech"],
                        "buying_signal": signal, "icp": icp["slug"], "batch": batch_tag})
    with open(out / "export_leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["email"])
        w.writeheader()
        w.writerows(rows)
    with open(out / "export_skipped.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "email", "email_status", "reason"])
        for r, why in skipped:
            w.writerow([r["company"], r["email"], r.get("email_status", ""), why])
    print(f"Exported {len(rows)} leads, skipped {len(skipped)} (see export_skipped.csv): {out / 'export_instantly.csv'}")


def plan_row(r, icp, batch_tag, args, contact):
    """Decide what push would do for one lead. Returns (action, reason, tags, existing_tags, contact_id)."""
    tags = tags_for(r, icp, batch_tag)
    if isinstance(contact, str):  # lookup unavailable
        return "blocked", f"CRM lookup unavailable ({contact[12:]}); cannot check suppression", tags, "", ""
    if contact is None:
        return "create", "new contact", tags, "", ""
    cid = contact.get("id", "")
    existing = ";".join(str(t) for t in (contact.get("tags") or []))
    why = suppression_reason(contact)
    if why:
        return "skip", f"suppressed: {why}", tags, existing, cid
    rel = existing_relationship(contact)
    if rel and not getattr(args, "update_existing", False):
        return "skip", f"existing relationship (tags: {', '.join(rel[:4])})", tags, existing, cid
    new_tags = [t for t in tags if t.lower() not in {x.lower() for x in (contact.get("tags") or [])}]
    if not new_tags:
        return "skip", "already carries every proposed tag", tags, existing, cid
    return "add-tags", "existing contact, tags added, fields untouched", new_tags, existing, cid


def cmd_push(args, icp, out, batch_tag):
    token, loc = require_env("GHL_API_KEY", "GHL_LOCATION_ID")
    live = bool(args.live)
    if live and args.approve != (args.run_id or batch_tag):
        raise SystemExit(f"--live needs Nikki's explicit approval for this batch: add --approve {args.run_id or batch_tag}")
    rows, skipped = load_rows(out, args, for_push=True)
    problems = check_tags([t for r in rows for t in tags_for(r, icp, batch_tag)] + [batch_tag])
    if problems:
        print("REFUSED. These tags would collide with the CRM's workflow triggers or reserved track tags:")
        for p in problems:
            print("  " + p)
        raise SystemExit(1)
    if not rows:
        print(f"No eligible leads. {len(skipped)} skipped before any CRM call:")
        for r, why in skipped[:20]:
            print(f"  {r['company']} <{r['email']}>: {why}")
        return

    ghl = GHL(token, loc, live)
    print(f"{'LIVE' if live else 'DRY RUN'}: {len(rows)} eligible leads, {len(skipped)} ineligible, batch tag '{batch_tag}'")
    results, actions = [], {}
    for i, r in enumerate(rows, 1):
        contact = ghl.lookup(r["email"])
        action, reason, tags, existing, cid = plan_row(r, icp, batch_tag, args, contact)
        res = {"company": r["company"], "email": r["email"], "action": action, "reason": reason,
               "tags": ";".join(tags), "existing_tags": existing, "contact_id": cid, "error": ""}
        if live and action in ("create", "add-tags"):
            try:
                if action == "create":
                    body = {"locationId": loc, "firstName": r["first_name"] or None, "lastName": r["last_name"] or None,
                            "email": r["email"], "phone": e164(r["phone"]), "companyName": r["company"], "website": r["website"],
                            "city": r["city"] or None, "state": r["state"] or None, "country": "US",
                            "source": icp["crm"].get("source", "Morpho lead engine")}
                    body = {k: v for k, v in body.items() if v not in (None, "")}
                    up = ghl.call("POST", "/contacts/upsert", body)
                    cid = (up.get("contact") or {}).get("id")
                    res["contact_id"] = cid
                    if not up.get("new"):
                        # the CRM matched someone we did not find: do not touch fields further, tags only
                        res["reason"] = "upsert matched an existing contact; tags added, no further field writes"
                for t in sorted(set(tags)):
                    ghl.ensure_tag(t)
                ghl.call("POST", f"/contacts/{cid}/tags", {"tags": tags})
                if action == "create" or args.note_always:
                    ghl.call("POST", f"/contacts/{cid}/notes", {"body": note_for(r, icp, batch_tag)})
            except Exception as e:
                res["error"] = str(e)[:300]
        elif live and action == "blocked":
            res["error"] = "not written: suppression state could not be checked"
        actions[action] = actions.get(action, 0) + 1
        results.append(res)
        print(f"[{i}/{len(rows)}] {action:9} {r['company']} <{r['email']}> {reason}" + (f" ERROR {res['error']}" if res["error"] else ""))

    path = out / ("crm_results.csv" if live else "crm_dryrun.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    with open(out / "crm_ineligible.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "email", "email_status", "reason"])
        for r, why in skipped:
            w.writerow([r["company"], r["email"], r.get("email_status", ""), why])

    all_tags = sorted({t for r in rows for t in tags_for(r, icp, batch_tag)})
    print(f"\nActions: {actions}. Ineligible before lookup: {len(skipped)} (crm_ineligible.csv).")
    print(f"Tags this batch would add ({len(all_tags)}): {', '.join(all_tags)}")
    live_triggers = [k for k in (GUARDRAILS.get("live_trigger_tags") or {}) if not k.startswith("_")]
    print(f"None of them equals a documented live trigger ({', '.join(live_triggers)}) or a reserved track tag.")
    print("Workflow-trigger risk: a published workflow can listen for ANY tag. The engine cannot read workflows with the")
    print("current token, so before --live open Automation > Workflows and confirm nothing triggers on the ICP tag, the")
    print("batch tag, or seg-/buyer-/email-/dm-/tech-/size-/signal- tags. Tagging never enrols anyone who already has a tag.")
    if not live:
        print(f"\nDry run. {ghl.writes} writes were made (should be 0). Results in {path}.")
        print(f"To execute exactly this plan: add --live --approve {args.run_id or batch_tag}")
    else:
        ok = sum(1 for x in results if not x["error"] and x["action"] in ("create", "add-tags"))
        print(f"\n{ok} contacts written, {ghl.writes} API writes. Contacts in CRM with tag '{batch_tag}': {ghl.count_tag(batch_tag)}")
        print(f"Smartlist filter: Tags  is  {batch_tag}   (name it: {icp['name']} | {batch_tag})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["export", "push"])
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--batch-tag", help="defaults to the run id")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approve", help="the run id, typed by Nikki, to approve a live push of this batch")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--personal-only", action="store_true")
    ap.add_argument("--verified-only", action="store_true", help="only emails whose mailbox answered (status valid)")
    ap.add_argument("--dns-ok-only", action="store_true", help="valid or mx_ok, excludes catch_all")
    ap.add_argument("--allow-unverified", action="store_true", help="include emails that verify has not checked")
    ap.add_argument("--allow-invalid", action="store_true", help="export only: include failed emails (never pushed)")
    ap.add_argument("--allow-no-email", action="store_true")
    ap.add_argument("--update-existing", action="store_true", help="add tags to contacts already in a Morpho track (never overwrites fields)")
    ap.add_argument("--note-always", action="store_true")
    args = ap.parse_args(argv)
    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    args.run_id = run_id
    out = run_dir(run_id)
    if not (out / "leads.csv").exists():
        raise SystemExit("leads.csv missing. Run qualify first.")
    batch_tag = slug(args.batch_tag or run_id)
    (cmd_export if args.command == "export" else cmd_push)(args, icp, out, batch_tag)


if __name__ == "__main__":
    main()
