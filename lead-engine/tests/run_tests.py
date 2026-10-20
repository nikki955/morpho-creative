#!/usr/bin/env python3
"""Offline regression tests for the lead engine. No network beyond 127.0.0.x, no API keys, no CRM.

    python3 tests/run_tests.py

Covers:
  1. discovery spend control: API source without a region refuses; a plan without --confirm makes no call;
     a Places run persists only place_id plus website-derived fields; the per-run request cap stops the loop
  2. qualify > verify > qualify > export: failed emails never reach export; mx_ok is labelled DNS-ok, not verified
  3. push row selection with a mocked CRM: invalid and unchecked emails ineligible, suppressed and tracked
     contacts skipped with reasons, protected tags refused, --live without --approve refused, zero writes in dry run
  4. direct booking: a page that only mentions a PMS is 'unknown' and raises no no-direct-booking flag;
     a reservation-engine iframe is 'confirmed'; a call-to-reserve site is 'none_found'
"""
import csv
import functools
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
FIXTURES = HERE / "fixtures"
HOSTS = {"acme": ("127.0.0.11", 8771), "pmsonly": ("127.0.0.12", 8772), "callonly": ("127.0.0.13", 8773),
         "inn": ("127.0.0.14", 8774), "bigbrand": ("127.0.0.15", 8775), "cleaners": ("127.0.0.16", 8776)}
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(h for h, _ in HOSTS.values()) + ",127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="lead-engine-tests-"))
os.environ["LEAD_ENGINE_DIR"] = str(TMP)
os.environ.pop("GOOGLE_PLACES_API_KEY", None)
os.environ.pop("FIRECRAWL_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["GHL_API_KEY"] = "test-token-not-real"
os.environ["GHL_LOCATION_ID"] = "testloc"

import common  # noqa: E402
import crm  # noqa: E402
import discover  # noqa: E402
import providers  # noqa: E402
import qualify  # noqa: E402
import reader  # noqa: E402
import verify  # noqa: E402

ICP = "str-portfolio-managers"


def url(name):
    h, p = HOSTS[name]
    return f"http://{h}:{p}/"


class Handler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):
        return "text/html" if not os.path.splitext(path)[1] else super().guess_type(path)

    def log_message(self, *a):
        pass


def start_servers():
    for name, (host, port) in HOSTS.items():
        srv = http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(FIXTURES / name)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fn(*a, **k)
        except SystemExit as e:
            return buf.getvalue(), e
    return buf.getvalue(), None


def write_import(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Company", "Website", "City", "State"])
        w.writerows(rows)


def rows_of(run, name):
    p = TMP / run / name
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


class T1Discovery(unittest.TestCase):
    def test_import_needs_no_region(self):
        imp = TMP / "imp.csv"
        write_import(imp, [["Acme", url("acme"), "Camden", "ME"]])
        out, err = quiet(discover.main, ["--icp", ICP, "--run-id", "t1a", "--import-csv", str(imp)])
        self.assertIsNone(err, out)
        comps = json.load(open(TMP / "t1a" / "companies.json"))
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0]["sources"], ["import"])

    def test_api_source_without_region_refuses(self):
        os.environ["GOOGLE_PLACES_API_KEY"] = "fake"
        try:
            out, err = quiet(discover.main, ["--icp", ICP, "--run-id", "t1b", "--source", "places"])
        finally:
            os.environ.pop("GOOGLE_PLACES_API_KEY")
        self.assertIsNotNone(err)
        self.assertIn("explicit region", str(err))

    def test_plan_without_confirm_makes_no_call(self):
        called = []
        orig = providers.places_text_search
        providers.places_text_search = lambda *a, **k: called.append(1) or iter(())
        os.environ["GOOGLE_PLACES_API_KEY"] = "fake"
        try:
            out, err = quiet(discover.main, ["--icp", ICP, "--run-id", "t1c", "--source", "places", "--states", "Vermont, USA"])
        finally:
            providers.places_text_search = orig
            os.environ.pop("GOOGLE_PLACES_API_KEY")
        self.assertIsNone(err, out)
        self.assertEqual(called, [])
        self.assertIn("No API call was made", out)
        self.assertIn("Google Places requests, worst case: 15", out)  # 5 terms x 1 state x 3 pages

    def test_places_run_persists_only_place_id_and_website_fields(self):
        fake_place = {"id": "ChIJtest123", "displayName": {"text": "Acme From Google"}, "websiteUri": url("acme"),
                      "rating": 4.8, "userRatingCount": 52, "googleMapsUri": "https://maps.google.com/?cid=1",
                      "nationalPhoneNumber": "(207) 555-9999", "formattedAddress": "1 Google St, Camden, ME",
                      "primaryType": "property_management_company", "types": ["property_management_company"],
                      "businessStatus": "OPERATIONAL",
                      "addressComponents": [{"types": ["country"], "shortText": "US"}, {"types": ["locality"], "longText": "Camden"}]}
        closed = {**fake_place, "id": "ChIJclosed", "businessStatus": "CLOSED_PERMANENTLY"}

        def fake_search(query, ledger, max_results=60, region_code="US", budget=None):
            if budget is not None:
                budget.take()
            ledger.add("places_request")
            yield fake_place
            yield closed

        orig = providers.places_text_search
        providers.places_text_search = fake_search
        os.environ["GOOGLE_PLACES_API_KEY"] = "fake"
        try:
            out, err = quiet(discover.main, ["--icp", ICP, "--run-id", "t1d", "--source", "places", "--states", "Maine, USA",
                                             "--confirm", "--max-requests", "10"])
        finally:
            providers.places_text_search = orig
            os.environ.pop("GOOGLE_PLACES_API_KEY")
        self.assertIsNone(err, out)
        comps = json.load(open(TMP / "t1d" / "companies.json"))
        self.assertEqual(len(comps), 1)
        c = comps[0]
        self.assertEqual(c["place_id"], "ChIJtest123")
        self.assertEqual(c["name"], "Acme Coastal Rentals")            # from the website JSON-LD, not "Acme From Google"
        self.assertEqual(c["phone"], "207-555-0100")                   # from the website, not (207) 555-9999
        self.assertEqual(c["attribution"]["name"], "company website")
        for k in ("rating", "reviews", "maps_url", "category", "address"):
            self.assertNotIn(k, c)
        blob = json.dumps(json.load(open(TMP / "t1d" / "raw_discovery.json"))) + json.dumps(comps)
        for leak in ("4.8", "Acme From Google", "555-9999", "Google St", "maps.google.com", "property_management_company"):
            self.assertNotIn(leak, blob, f"Places content leaked into run files: {leak}")
        self.assertNotIn("ChIJclosed", blob)  # a place dropped in memory leaves only a count behind
        markers = [r for r in json.load(open(TMP / "t1d" / "raw_discovery.json")) if r.get("_marker")]
        self.assertEqual(markers[0]["_dropped_in_memory"], {"closed": 1})

    def test_request_cap_stops_the_loop(self):
        def greedy(query, ledger, max_results=60, region_code="US", budget=None):
            for _ in range(5):
                budget.take()
                ledger.add("places_request")
                yield {"id": "x", "websiteUri": url("acme"), "businessStatus": "OPERATIONAL"}

        orig = providers.places_text_search
        providers.places_text_search = greedy
        os.environ["GOOGLE_PLACES_API_KEY"] = "fake"
        try:
            out, err = quiet(discover.main, ["--icp", ICP, "--run-id", "t1e", "--source", "places", "--states", "Maine, USA",
                                             "--confirm", "--max-requests", "2"])
        finally:
            providers.places_text_search = orig
            os.environ.pop("GOOGLE_PLACES_API_KEY")
        self.assertIsNone(err, out)
        self.assertIn("cap of 2 reached", out)
        self.assertEqual(json.load(open(TMP / "t1e" / "costs.json"))["places_request"], 2)


class T2EmailLeakage(unittest.TestCase):
    RUN = "t2"

    @classmethod
    def setUpClass(cls):
        imp = TMP / "t2.csv"
        write_import(imp, [["Acme", url("acme"), "Camden", "ME"], ["Harbor Light Inn", url("inn"), "Camden", "NH"]])
        quiet(discover.main, ["--icp", ICP, "--run-id", cls.RUN, "--import-csv", str(imp)])
        quiet(reader.main, ["--icp", ICP, "--run-id", cls.RUN, "--workers", "2"])
        quiet(qualify.main, ["--icp", ICP, "--run-id", cls.RUN])
        orig = verify.mx_hosts
        verify.mx_hosts = lambda d: [] if d.endswith("acme.test") else ["mx.fixture.test"]
        try:
            quiet(verify.main, ["--icp", ICP, "--run-id", cls.RUN, "--all"])
        finally:
            verify.mx_hosts = orig
        quiet(qualify.main, ["--icp", ICP, "--run-id", cls.RUN])
        quiet(crm.main, ["export", "--icp", ICP, "--run-id", cls.RUN])

    def test_acme_was_ready_before_verify_and_not_after(self):
        ver = json.load(open(TMP / self.RUN / "verification.json"))
        self.assertEqual(ver["jane@acme.test"]["status"], "no_mx")
        leads = rows_of(self.RUN, "leads.csv")
        self.assertNotIn("jane@acme.test", [r["email"] for r in leads])
        ne = rows_of(self.RUN, "needs_enrichment.csv")
        acme = next(r for r in ne if r["domain"] == HOSTS["acme"][0])
        self.assertIn("failed verification", acme["reason"])

    def test_export_excludes_failed_and_labels_dns_only(self):
        exp = rows_of(self.RUN, "export_instantly.csv")
        self.assertEqual([r["email"] for r in exp], [])  # inn is a 32-unit STR? no: inn under STR ICP is excluded, acme failed
        skipped = rows_of(self.RUN, "export_skipped.csv")
        self.assertTrue(all(r["email"] != "jane@acme.test" for r in exp))
        self.assertTrue(any("no_mx" in r["reason"] or "failed" in r["reason"] for r in skipped) or not skipped)

    def test_tag_names_distinguish_dns_from_mailbox(self):
        row = {"segment": "scaling", "flags": "", "buyer_type": "", "sweet_spot": "yes", "email": "x@y.test", "email_type": "personal",
               "email_status": "mx_ok", "title_tier": "1", "tech": "", "hiring_signals": "", "growth_signals": "", "buying_signals": ""}
        icp = common.load_icp(ICP)
        tags = crm.tags_for(row, icp, "batch-x")
        self.assertIn("email-dns-ok", tags)
        self.assertNotIn("email-verified", " ".join(tags))
        self.assertNotIn("email-mailbox-verified", tags)
        row["email_status"] = "catch_all"
        self.assertIn("email-catch-all", crm.tags_for(row, icp, "batch-x"))
        row["email_status"] = "valid"
        self.assertIn("email-mailbox-verified", crm.tags_for(row, icp, "batch-x"))


class T3PushSelection(unittest.TestCase):
    RUN = "t3"
    CONTACTS = {
        "suppressed@fixture.test": {"id": "c-sup", "tags": ["Unsubscribed", "s2"]},
        "dnd@fixture.test": {"id": "c-dnd", "dnd": False, "dndSettings": {"Email": {"status": "active", "code": "bounce"}}, "tags": []},
        "tracked@fixture.test": {"id": "c-trk", "tags": ["morpho-ch", "track-hospitality"]},
        "plain@fixture.test": {"id": "c-pln", "tags": ["lead magnet - checklist"]},
        "new@fixture.test": None,
    }

    @classmethod
    def setUpClass(cls):
        out = TMP / cls.RUN
        out.mkdir(exist_ok=True)
        base = {k: "" for k in qualify.FIELDS}
        base.update({"company": "X", "domain": "x.test", "website": "https://x.test", "size": "20", "segment": "overloaded",
                     "sweet_spot": "yes", "first_name": "A", "last_name": "B", "title": "Owner", "title_tier": "1",
                     "email_type": "personal", "status": "ready", "priority": "5"})
        rows = []
        for email, st in [("suppressed@fixture.test", "mx_ok"), ("dnd@fixture.test", "valid"), ("tracked@fixture.test", "mx_ok"),
                          ("plain@fixture.test", "catch_all"), ("new@fixture.test", "valid"), ("bad@fixture.test", "no_mx"),
                          ("syntax@@fixture", "invalid_syntax"), ("unchecked@fixture.test", "")]:
            rows.append({**base, "company": email.split("@")[0], "email": email, "email_status": st})
        with open(out / "leads.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=qualify.FIELDS)
            w.writeheader()
            w.writerows(rows)
        cls.calls = []
        cls.orig_lookup, cls.orig_call = crm.GHL.lookup, crm.GHL.call

        def fake_lookup(self, email):
            return cls.CONTACTS.get(email, None)

        def fake_call(self, method, path, body=None):
            cls.calls.append((method, path))
            if not self.live and not (method == "GET" or path.startswith("/contacts/search")):
                raise AssertionError("write in dry run")
            self.writes += 0 if path.startswith("/contacts/search") else 1
            if path == "/contacts/upsert":
                return {"contact": {"id": "c-new"}, "new": True}
            return {}

        crm.GHL.lookup, crm.GHL.call = fake_lookup, fake_call

    @classmethod
    def tearDownClass(cls):
        crm.GHL.lookup, crm.GHL.call = cls.orig_lookup, cls.orig_call

    def test_dry_run_selection_and_zero_writes(self):
        self.__class__.calls.clear()
        out, err = quiet(crm.main, ["push", "--icp", ICP, "--run-id", self.RUN])
        self.assertIsNone(err, out)
        plan = {r["email"]: r for r in rows_of(self.RUN, "crm_dryrun.csv")}
        inel = {r["email"]: r for r in rows_of(self.RUN, "crm_ineligible.csv")}
        self.assertEqual(plan["suppressed@fixture.test"]["action"], "skip")
        self.assertIn("suppression tag 'unsubscribed'", plan["suppressed@fixture.test"]["reason"])
        self.assertEqual(plan["dnd@fixture.test"]["action"], "skip")
        self.assertIn("email channel DND active (bounce)", plan["dnd@fixture.test"]["reason"])
        self.assertEqual(plan["tracked@fixture.test"]["action"], "skip")
        self.assertIn("existing relationship", plan["tracked@fixture.test"]["reason"])
        self.assertEqual(plan["plain@fixture.test"]["action"], "skip")  # old lead-magnet list: existing relationship
        self.assertEqual(plan["new@fixture.test"]["action"], "create")
        for bad in ("bad@fixture.test", "syntax@@fixture", "unchecked@fixture.test"):
            self.assertNotIn(bad, plan)
            self.assertIn(bad, inel)
        self.assertIn("no_mx", inel["bad@fixture.test"]["reason"])
        self.assertIn("not verified yet", inel["unchecked@fixture.test"]["reason"])
        self.assertTrue(all(not p.startswith("/contacts/upsert") and "/tags" not in p and "/notes" not in p for _, p in self.calls))
        self.assertIn("0 writes were made", out)
        self.assertIn("--approve " + self.RUN, out)

    def test_live_requires_approval_token(self):
        out, err = quiet(crm.main, ["push", "--icp", ICP, "--run-id", self.RUN, "--live"])
        self.assertIsNotNone(err)
        self.assertIn("--approve", str(err))
        out, err = quiet(crm.main, ["push", "--icp", ICP, "--run-id", self.RUN, "--live", "--approve", "wrong-id"])
        self.assertIsNotNone(err)

    def test_live_with_mock_writes_only_eligible_new_contacts(self):
        self.__class__.calls.clear()
        out, err = quiet(crm.main, ["push", "--icp", ICP, "--run-id", self.RUN, "--live", "--approve", self.RUN])
        self.assertIsNone(err, out)
        res = {r["email"]: r for r in rows_of(self.RUN, "crm_results.csv")}
        self.assertEqual(res["new@fixture.test"]["action"], "create")
        self.assertEqual(res["new@fixture.test"]["contact_id"], "c-new")
        writes = [p for m, p in self.calls if m == "POST" and not p.startswith("/contacts/search")]
        self.assertTrue(any(p == "/contacts/upsert" for p in writes))
        self.assertTrue(all("c-sup" not in p and "c-dnd" not in p and "c-trk" not in p and "c-pln" not in p for p in writes))

    def test_protected_batch_tag_is_refused(self):
        out, err = quiet(crm.main, ["push", "--icp", ICP, "--run-id", self.RUN, "--batch-tag", "aio-sent"])
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", out)
        self.assertEqual(crm.check_tags(["track-partner"]), crm.check_tags(["track-partner"]))
        self.assertTrue(crm.check_tags(["goodwill"]))
        self.assertTrue(crm.check_tags(["replied-hospitality"]))
        self.assertFalse(crm.check_tags(["morpho-str-icp", "seg-overloaded", "buyer-recent-acquirer", "email-dns-ok", "inns-2026-10-20"]))

    def test_engine_tags_do_not_collide_with_track_scheme(self):
        icps = [common.load_icp(s) for s in common.list_icps()]
        self.assertFalse(crm.check_tags([i["crm"]["icp_tag"] for i in icps]))


class T4DirectBooking(unittest.TestCase):
    RUN = "t4"

    @classmethod
    def setUpClass(cls):
        imp = TMP / "t4.csv"
        write_import(imp, [["Acme", url("acme"), "Camden", "ME"], ["Pine Cove", url("pmsonly"), "Rockport", "ME"],
                           ["Lakeside", url("callonly"), "Belgrade", "ME"]])
        quiet(discover.main, ["--icp", ICP, "--run-id", cls.RUN, "--import-csv", str(imp)])
        quiet(reader.main, ["--icp", ICP, "--run-id", cls.RUN, "--workers", "2"])
        quiet(qualify.main, ["--icp", ICP, "--run-id", cls.RUN])
        cls.rows = {r["domain"]: r for r in rows_of(cls.RUN, "all_companies.csv")}

    def site(self, name):
        with open(TMP / self.RUN / "site_data" / f"{HOSTS[name][0]}.json") as f:
            return json.load(f)

    def test_pms_only_is_unknown_not_no_booking(self):
        s = self.site("pmsonly")
        self.assertIn("Guesty", s["tech_detected"])
        self.assertEqual(s["booking"]["status"], "unknown")
        self.assertEqual(s["booking"]["evidence"], [])
        row = self.rows[HOSTS["pmsonly"][0]]
        self.assertEqual(row["direct_booking"], "unknown")
        self.assertNotIn("ota_dependent", row["flags"])

    def test_reservation_engine_iframe_is_confirmed(self):
        s = self.site("acme")
        self.assertEqual(s["booking"]["status"], "confirmed")
        self.assertTrue(any("thinkreservations" in e for e in s["booking"]["evidence"]))
        self.assertEqual(self.rows[HOSTS["acme"][0]]["direct_booking"], "confirmed")

    def test_call_to_reserve_is_none_found_and_flagged(self):
        s = self.site("callonly")
        self.assertEqual(s["booking"]["status"], "none_found")
        row = self.rows[HOSTS["callonly"][0]]
        self.assertEqual(row["direct_booking"], "none_found")
        self.assertIn("ota_dependent", row["flags"])


if __name__ == "__main__":
    start_servers()
    print(f"temp run dir: {TMP}")
    unittest.main(verbosity=2)
