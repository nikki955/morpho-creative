# Writing an ICP file

One JSON file per profile in `icps/`. The file is the whole "filter set": discovery, reading, qualification, tagging all read from it. Start from `_template.json` or `lead_engine.py new-icp`. `str-portfolio-managers.json` is a complete worked example.

## Fields

**slug, name, description, offer_context**
`description` is read by the Claude qualifier verbatim. Write it like a brief to a sharp junior: who qualifies (business model, size, geography, buyer), who does not (the look-alikes), what must be verified. `offer_context` is what Morpho sells this profile, used to judge buying signals.

**search_terms**
The 4 to 6 phrases this business is listed under on Google Maps. Think like the business owner picking a category, not like a marketer. "RV park", "campground", "RV resort" beat "outdoor hospitality".

**locations**
Region key to a list of place strings as Google understands them ("Maine, USA", "Asheville, NC"). States work for sparse categories; use metros for dense ones so the 60-result cap per query does not truncate.

**size**
`metric` is the noun (units, rooms, sites, locations, employees). `min`/`max` is the acceptable band, `sweet_min`/`sweet_max` the ideal. `patterns` are Python regexes with exactly one capture group for the number; they run over all page text, so make the noun specific ("rooms|suites|guest rooms" for inns, "sites|campsites|RV sites|hookups" for parks, "doors|units" for small multifamily). `listing_url_patterns` and `listing_index_paths` are how the sitemap and listing page are counted; for businesses with no listing pages (a venue, a single inn) leave them short and rely on patterns plus the LLM. `require_verified: false` lets rows through on low-confidence size, for profiles where size is soft (coaching clients, solopreneurs).

**segments**
Bands on the size metric with a `target_share`, plus optional rule segments. The only rule today is `no_direct_booking` (no booking engine or PMS detected). Keys become CRM tags `seg-<key>`.

**business_types**
`options` is the classifier's menu with one-line definitions; `accept` is the subset that qualifies. Put the look-alikes in the menu explicitly (the brokerage, the vendor, the franchise, the single-property host). A classifier with a named bucket for the wrong answer makes far fewer mistakes.

**decision_makers**
Title keywords, primary then secondary. Matched case-insensitively as substrings.

**exclude**
`domains` (national brands, franchisors), `category_keywords` (Google place types to drop at discovery), `text_signals` (phrases that mean "wrong business", like "request a demo").

**signals**
`tech` maps a product name to lowercase strings found in page HTML when it is installed (script hosts, widget class names). `direct_booking` phrases, `ota_links` hosts, `hiring` and `growth` phrases. Keep hiring phrases specific; a bare "careers" matches every nav bar.

**email**
`role_prefixes` are generic inboxes (ranked last), `owner_prefixes` are role inboxes that still reach the buyer (ranked before generic).

**pages**
Keywords that identify the about/team page and the contact/owners pages in URLs and link text.

**crm**
`icp_tag` (every contact from this ICP gets it), `source`, `note_header`.

## Suggested shapes for Nikki's likely profiles

These are starting points, not finished files. Confirm each with her.

| profile | metric | band | search terms | size patterns noun | look-alikes to reject |
| --- | --- | --- | --- | --- | --- |
| Boutique inns and B&Bs | rooms | 8 to 40 | bed and breakfast, inn, boutique hotel | rooms, suites, guest rooms | chain hotels, motels, single-room Airbnb |
| RV parks and campgrounds | sites | 25 to 200 | RV park, campground, RV resort | sites, campsites, RV sites, full hookups | KOA-style franchises (unless owner-run), state parks, dealers |
| Wedding and event venues | events per year or capacity | capacity 75 to 300 | wedding venue, event venue, barn venue | guests, seated, capacity | hotels with ballrooms, caterers, planners |
| Glamping and cabin resorts | units | 5 to 40 | glamping, cabin rentals, cabin resort | cabins, tents, domes, yurts, units | single-cabin hosts, booking platforms |
| Small multifamily operators | doors | 20 to 150 | property management company, apartment management | doors, units, apartments | national REITs, HOA managers, brokerages |
| Hospitality-focused REI syndicators | assets | 2 to 15 | real estate investment firm, hospitality investment | properties, assets, hotels | brokers, lenders, crowdfunding platforms |
| AI coaching: non-techy service business owners | employees | 1 to 20 | depends on vertical; pick 2 to 3 verticals per ICP | team of N, N employees | agencies that already sell AI, software companies |

For profiles where Google Maps is weak (syndicators, coaches' audiences), lean on `--source search` with sharper search terms, or `--import-csv` from her existing lists (the REI Owner's Club list, Facebook group members with websites).
