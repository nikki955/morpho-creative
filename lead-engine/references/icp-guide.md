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

## buyer_types (optional)

Who is on the other end of the call, judged per prospect rather than per ICP. `options` is the menu the Claude qualifier picks from (with evidence); `on_ownership_change` and `on_portfolio_signal` are the rule-based fallbacks when the LLM pass is off, driven by `signals.ownership_change` and `signals.portfolio` phrases; `priority_bonus` nudges the sort. The value lands in the `buyer_type` column, the CRM tag `buyer-<type>`, the note, and the Instantly export. Nikki's lodging ICPs all use the same three: `owner_operator`, `portfolio_owner_manager`, `recent_acquirer` (from the "Morpho ICPs: Independent Lodging Owners" doc).

## The lodging ICPs (from the October 2026 ICP doc)

All five share: independent ownership (no flags, franchises or third-party management the owner cannot hire around), under 150 keys or sites per property, US-wide except Maine (the R3B1 study runs there), owner reachable, operating or opening within six months with funding. Budget floor (roughly $500K gross) is not detectable from a website and is left to the discovery call.

| ICP file | metric | band (sweet spot) | segments | notes |
| --- | --- | --- | --- | --- |
| `rv-parks-campgrounds` | sites | 25 to 150 (40 to 120) | small_park_check_rentals 25-39, core_park 40-150, no_online_booking | The doc's open question on 150 vs 300 sites is one number in `size.max`. Parks at 25-39 sites only qualify if cabins or rental units push revenue up; the segment name is the reminder. |
| `boutique-hotels` | keys | 15 to 150 (20 to 90) | small_boutique 15-50, mid_boutique 51-150, weak_direct_booking | Soft brands (Autograph, Curio, Tribute, Tapestry, Ascend, JdV) are excluded by text signal. Most likely avatar for a higher retainer. |
| `glamping` | units | 8 to 50 (10 to 35) | early_scale 8-20, scaled 21-50, platform_dependent | Below 8 units routes to the cohort; the ICP simply excludes them. RV parks with a few glamping units belong to the RV ICP. |
| `inns` | rooms | 10 to 40 (12 to 30) | inn_core 10-25, inn_large 26-40, inquiry_form_only | Innkeeper is a primary decision-maker title here. Restaurant and events signals ride along as tech (Tock, OpenTable, Resy) and growth phrases. |
| `bed-and-breakfasts` | rooms | 3 to 15 (8 to 15) | cohort_fit 3-7, retainer_fit 8-15, no_online_booking | Kept deliberately wide: 3-7 rooms are cohort or QuickScan prospects and are tagged as such instead of being thrown away. |

Shared location sets live in `_locations_us.json` for copy-paste. Each ICP also carries a metro list for dense drive-to markets, because a state-level Google query caps at 60 results. For big states run the metros, or pass `--states "Hill Country, TX" "Fredericksburg, TX"`.

Other profiles Nikki may add later (STR managers already exist): small multifamily operators (doors), hospitality-focused REI syndicators (assets), and the AI-coaching audience of non-technical service business owners (employees, `require_verified: false`). For profiles where Google Maps is weak, lean on `--source search` or `--import-csv` from her existing lists (the REI Owner's Club list, group members with websites).
