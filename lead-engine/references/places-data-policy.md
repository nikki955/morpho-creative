# Google Places data: what this engine stores, and why

Written 2026-10-20 after checking Google's terms. This is an engineering reading of the published terms, not legal advice. The decision that is still Nikki's is at the bottom.

## What the terms say

Checked against the Google Maps Platform Service Specific Terms (cloud.google.com/maps-platform/terms/maps-service-terms, section 14 "Places API", 14.3 "Caching") and the Places API policies page (developers.google.com/maps/documentation/places/web-service/policies). The sandbox this was written in could not open the developers.google.com page directly, so its wording was confirmed through search excerpts of that page; verify the live page once before the first paid run.

1. Caching is restricted by default. Places content may not be pre-fetched, cached, indexed or stored except as the terms allow.
2. Two exceptions: the place ID may be stored indefinitely (the Places API Policies exempt it from the caching restriction), and latitude/longitude may be kept for up to 30 consecutive days.
3. Everything else returned by a Places request is Places content: display name, website URI, phone, formatted address, rating, review count, Maps URL, types, business status. None of it has a storage allowance.
4. Attribution and "powered by Google" rules apply wherever Places content is displayed. A CRM record is a display.
5. The general Google Maps Platform terms also bar using the content to build a database or a competing product.

Yesterday's pipeline (`str-lead-gen`, via Apify, and the first version of this engine) wrote all of item 3 into `raw_discovery.json` and `companies.json`, and `maps_url`, `rating` and the Places-sourced name, phone and city flowed into CRM notes. That is the lasting database the terms prohibit.

## The redesign

Places is now a transient pointer, nothing more.

1. `discover --source places` calls Text Search and holds each result in memory only.
2. Closed, non-US, no-website, aggregator and wrong-category results are dropped in memory. The run file records the place_id and a reason word so the query is not repeated, nothing else.
3. For every surviving result the engine fetches the company's own website (a free HTTP request to the business's public site) and derives the durable fields from that page: domain (final URL after redirects), name (JSON-LD organization name, then the site's own title), phone, city, state, postal code (JSON-LD address).
4. What is written to disk per company: those website-derived fields, `place_id`, `sources: ["website_via_places"]`, and an `attribution` map naming "company website" for each field. No rating, review count, Maps URL, category, formatted address or Google display name is written anywhere, including CRM notes. The note now says "Data source: company website. No Google Places content stored."
5. If the website cannot be fetched, the company is not stored at all (only the place_id, as unresolved).

The offline test `tests/run_tests.py::T1Discovery::test_places_run_persists_only_place_id_and_website_fields` feeds a fake Places result carrying a Google name, phone, address, rating and Maps URL and asserts none of those strings appear in any run file.

`lead_engine.py scrub-places --icp SLUG --run-id ID --yes` rewrites an older run the same way (strips the Places fields, re-derives name/phone/address from `site_data`, relabels the source). It is not run automatically.

## What is still a judgement call

The selection of which websites to visit still comes from a Places search. The stored facts come from the websites, which are public and independently reachable, and the only Places artifact kept is the place_id, which the policy allows. That is the most conservative design that keeps Places usable for discovery at all. Two things remain Nikki's decision:

1. Whether to keep using Places for discovery or switch to web search and imports only. The engine supports both; `--source search` and `--import-csv` have no Places dependency.
2. Whether to run `scrub-places` on the existing STR run from 2026-10-19 (and the older Apify run folders), which still contain Places-derived fields and Maps URLs in `companies.json`, `raw_places.json` and CRM notes already written. Scrubbing the run folders is one command; the notes already in the CRM would need a separate, approved cleanup.

If a Google policy reviewer would read "visit the websites Google found and keep what the websites say" as building a database from Places content, then option 1 (no Places) is the safe answer. The engine does not claim compliance; it removes every stored Places field it can and leaves the rest visible.
