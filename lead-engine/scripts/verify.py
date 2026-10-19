#!/usr/bin/env python3
"""Step 5: Verify emails for free before anything goes to the CRM or a sender.

Checks, in order, per unique email in leads.csv (and needs_enrichment.csv with --all):
  1. syntax
  2. disposable / obviously fake domains
  3. DNS: MX records (falls back to an A record, which some small businesses rely on)
  4. --smtp: connect to the MX on port 25 and ask RCPT TO without sending. Also probes a random
     address to detect catch-all domains. Many home and office ISPs block port 25; if the connect
     fails the result is 'unknown', never 'invalid'.

Statuses: valid (SMTP accepted), catch_all (SMTP accepts anything), mx_ok (DNS ok, SMTP not run or
blocked), no_mx, invalid_syntax, smtp_rejected, unknown.

Writes <run>/verification.json keyed by email and rewrites the email_status column of leads.csv,
needs_enrichment.csv and all_companies.csv. qualify also reads verification.json on later runs.

Usage:
  python3 verify.py --icp str-portfolio-managers --run-id <id> [--smtp] [--all]
"""
import argparse
import csv
import random
import re
import smtplib
import socket
import string
import time

from common import default_run_id, load_icp, read_json, run_dir, write_json

SYNTAX = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")
DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com", "trashmail.com",
              "sharklasers.com", "dispostable.com", "getnada.com", "temp-mail.org"}
_mx_cache = {}


def mx_hosts(domain):
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts = []
    try:
        import dns.resolver
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=8)
            hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            hosts = []
        except Exception:
            hosts = None
    except ImportError:
        hosts = None
    if hosts is None or hosts == []:
        # dnspython missing or no MX: accept an A record as a weak fallback
        try:
            socket.getaddrinfo(domain, 25)
            hosts = hosts or [f"a:{domain}"]
        except socket.gaierror:
            hosts = []
    _mx_cache[domain] = hosts
    return hosts


def smtp_probe(email, hosts, helo_domain, mail_from, timeout=10):
    """Return (status, detail). Never raises."""
    domain = email.split("@")[1]
    real_hosts = [h for h in hosts if not h.startswith("a:")] or [domain]
    for host in real_hosts[:2]:
        try:
            s = smtplib.SMTP(timeout=timeout)
            s.connect(host, 25)
            s.helo(helo_domain)
            s.mail(mail_from)
            code, msg = s.rcpt(email)
            if code in (250, 251):
                rand = "".join(random.choices(string.ascii_lowercase, k=14)) + "@" + domain
                code2, _ = s.rcpt(rand)
                s.quit()
                return ("catch_all" if code2 in (250, 251) else "valid"), f"{host} {code}"
            if code in (550, 551, 553, 554) or (code == 450 and b"user" in msg.lower()):
                s.quit()
                return "smtp_rejected", f"{host} {code} {msg[:60]!r}"
            s.quit()
            return "unknown", f"{host} {code} {msg[:60]!r}"
        except (socket.timeout, ConnectionRefusedError, OSError, smtplib.SMTPException) as e:
            last = f"{host}: {type(e).__name__}"
            continue
    return "unknown", f"port 25 unreachable ({last})" if real_hosts else "no host"


def check(email, do_smtp, helo_domain, mail_from):
    if not SYNTAX.match(email):
        return {"status": "invalid_syntax", "detail": ""}
    domain = email.split("@")[1].lower()
    if domain in DISPOSABLE:
        return {"status": "no_mx", "detail": "disposable domain"}
    hosts = mx_hosts(domain)
    if not hosts:
        return {"status": "no_mx", "detail": "no MX or A record"}
    res = {"status": "mx_ok", "detail": ",".join(hosts[:2])}
    if do_smtp:
        st, det = smtp_probe(email, hosts, helo_domain, mail_from)
        res = {"status": st if st != "unknown" else "mx_ok", "detail": det, "smtp": st}
    return res


def rewrite_csv(path, verification):
    if not path.exists():
        return
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return
    for r in rows:
        if r.get("email"):
            r["email_status"] = (verification.get(r["email"]) or {}).get("status", r.get("email_status", ""))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icp", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--smtp", action="store_true", help="also probe the mail server (port 25, often blocked)")
    ap.add_argument("--all", action="store_true", help="also verify emails in needs_enrichment.csv")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--helo", default="morphocreativeconsulting.com")
    ap.add_argument("--mail-from", default="verify@morphocreativeconsulting.com")
    args = ap.parse_args(argv)

    icp = load_icp(args.icp)
    run_id = args.run_id or default_run_id(icp["slug"])
    out = run_dir(run_id)
    verification = read_json(out / "verification.json", {}) or {}
    files = [out / "leads.csv"] + ([out / "needs_enrichment.csv"] if args.all else [])
    emails = []
    for p in files:
        if p.exists():
            for r in csv.DictReader(open(p)):
                for e in [r.get("email")] + (r.get("other_emails") or "").split(";"):
                    e = (e or "").strip().lower()
                    if e and e not in emails:
                        emails.append(e)
    todo = [e for e in emails if args.force or e not in verification or (args.smtp and "smtp" not in verification[e])]
    print(f"Verifying {len(todo)} emails ({'DNS + SMTP' if args.smtp else 'DNS only'}, free)")
    for i, e in enumerate(todo, 1):
        verification[e] = {**check(e, args.smtp, args.helo, args.mail_from), "checked": time.strftime("%Y-%m-%d")}
        if i % 10 == 0 or i == len(todo):
            write_json(out / "verification.json", verification)
        print(f"  [{i}/{len(todo)}] {e}: {verification[e]['status']} {verification[e].get('detail', '')[:50]}")
    write_json(out / "verification.json", verification)
    for name in ("leads.csv", "needs_enrichment.csv", "all_companies.csv", "review.csv"):
        rewrite_csv(out / name, verification)
    counts = {}
    for e in emails:
        st = (verification.get(e) or {}).get("status", "unchecked")
        counts[st] = counts.get(st, 0) + 1
    print(f"Result: {counts}")
    if args.smtp and all((verification.get(e) or {}).get("smtp") in (None, "unknown") for e in todo) and todo:
        print("Every SMTP probe came back unknown: port 25 is probably blocked on this network. DNS results still stand.")


if __name__ == "__main__":
    main()
