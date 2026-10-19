---
name: lead-engine
description: Multi-ICP lead engine for Morpho. Finds local businesses that match an ICP file (STR managers, RV parks, boutique hotels, glamping, inns, B&Bs), reads their websites for free, verifies size from stated numbers and listing pages, picks the decision maker and a published email, verifies the email, tags the buyer type, and exports to CSV or pushes tagged contacts into FG Funnels. Replaces the Apify plus Firecrawl pipeline at roughly 1 percent of the cost. Use when Nikki says "run lead gen for" an ICP, "find leads" for an avatar, "new lead batch", "build an ICP", "add an ICP profile", or invokes /lead-engine. For the STR ICP this supersedes /str-lead-gen.
---

# Morpho Lead Engine

One CLI, one ICP file per profile, one run folder per batch. Pipeline:

discover (Google Places free tier or web search) > read (plain HTTP, 0 credits) > qualify (rules, plus optional Claude Haiku pass) > enrich gaps (capped, cheap) > verify emails (free) > export CSV or push to FG Funnels.

Everything lives in `scripts/` ($SKILL below means the path to this skill folder). ICP files live in `icps/`. Runs land in `./leads/RUN_ID/` under the working folder (or `LEAD_ENGINE_DIR`). Always pass the same `--icp` and `--run-id`; the run id becomes the CRM batch tag.

Read `references/icp-guide.md` before writing or editing an ICP. Read `references/cost-model.md` if Nikki asks what a batch costs. `references/instantly-recon.md` explains what this clones from Instantly's Lead Finder and what it deliberately does not.

## 0. Preflight (every run)

1. `python3 $SKILL/scripts/lead_engine.py doctor`. It lists which keys are set and which discovery source will be used. Minimum to run anything: `GOOGLE_PLACES_API_KEY` or `FIRECRAWL_API_KEY` (or a CSV of companies). `ANTHROPIC_API_KEY` turns on the Claude qualification pass. CRM push needs `GHL_API_KEY` and `GHL_LOCATION_ID`. Setup for each is in `references/setup.md`. If a key is missing, say which one and what it unlocks, then continue with what is available rather than stopping.
2. `pip install requests beautifulsoup4 dnspython anthropic` if the doctor reports missing modules.
3. Pick the ICP: `lead_engine.py list-icps`. Shipped: `str-portfolio-managers`, `rv-parks-campgrounds`, `boutique-hotels`, `glamping`, `inns`, `bed-and-breakfasts`. The five lodging ICPs come from the "Morpho ICPs: Independent Lodging Owners" doc and run nationally except Maine. If the profile Nikki wants does not exist yet, go to section 7 first.
4. Ask only what you cannot default: regions for this batch (default: the first region key in the ICP), batch size (default `batch_target` in the ICP, usually 100), run id (default `SLUG-YYYY-MM-DD`). If she is not around, use the defaults and say so at the top of the report.

## 1. Discover

```
python3 $SKILL/scripts/lead_engine.py discover --icp SLUG --run-id RUN_ID --regions REGION
```

Default source is Google Places when its key exists (20 businesses per request, 1,000 requests a month free), otherwise Firecrawl search (2 credits per 10 results, and it follows "top 10" list pages for free to harvest the long tail). `--source both` merges them. `--import-csv file.csv` adds a list Nikki already has (needs a website or domain column). Coarse filters drop closed places, aggregators, excluded national brands and wrong Google categories.

Check the count. Aim for about 3x the batch target. If short, add a region or `--source both`.

## 2. Read every website (free)

```
python3 $SKILL/scripts/lead_engine.py read --icp SLUG --run-id RUN_ID --workers 6
```

Fetches the homepage, then up to 4 more pages (about/team, listing index, owners, contact, careers), plus the sitemap. Extracts emails, phones, people with titles, size statements, listing counts, tech fingerprints, direct booking cues, hiring and growth signals, exclusion signals. Zero credits.

Sites that render nothing without JavaScript are flagged `JS-SHELL`. Rerun those with `--firecrawl-fallback --force --domains a.com b.com` (1 credit each). Tell Nikki the count before doing it if it is more than 30.

## 3. Qualify

```
python3 $SKILL/scripts/lead_engine.py qualify --icp SLUG --run-id RUN_ID          # rules only, free
python3 $SKILL/scripts/lead_engine.py qualify --icp SLUG --run-id RUN_ID --llm    # plus one Haiku call per company
```

Use `--llm` whenever `ANTHROPIC_API_KEY` exists. It costs about 6 cents per 100 companies and sharply improves business-type calls, decision-maker picks, and size evidence. Results cache in `llm.json`, so reruns are free. `LEAD_ENGINE_MODEL` overrides the model (default `claude-haiku-5-5`; `claude-sonnet-5-5` if a profile proves hard to classify).

Every lead also gets a `buyer_type` (owner_operator, portfolio_owner_manager, recent_acquirer) from the LLM pass, or from ownership-change and multi-property phrases on the site when the LLM is off. It drives the `buyer-TYPE` CRM tag and the messaging angle; `recent_acquirer` is the fastest closer per the ICP doc and sorts higher.

Report `summary.json` in two lines. Statuses:
1. `ready`: in range, named decision maker, personal email. Goes to CRM.
2. `ready_generic`: in range, but the best email is info@ style or there is no named person. Goes to CRM, enrich first if possible.
3. `needs_enrichment`: in range, no email at all.
4. `review`: size not verified. Must be verified before it counts.
5. `excluded`: fails the ICP. Skim `excluded.csv` for misclassifications and override in `enrichment.json` with `"include": true`.

## 4. Enrich the gaps (cheap, capped)

```
python3 $SKILL/scripts/lead_engine.py enrich --icp SLUG --run-id RUN_ID --verify-size             # free: walks listing pages
python3 $SKILL/scripts/lead_engine.py enrich --icp SLUG --run-id RUN_ID --find-people --max 30    # 2 credits each, 4 with --linkedin
```

`--find-people` writes candidates to `enrichment_candidates.json`. Review them and promote the right person into `enrichment.json` (format at the top of `scripts/qualify.py`). LinkedIn-headline hits that name the company with a primary title are auto-promoted with name and title only, never an email.

For whatever is still missing, work the list by hand in priority order, the same way as before: about pages, Google Business Profile owner replies, local news, chamber pages, state business registry, Facebook About tab, PDF management agreements. Only record an email you actually saw, with its URL as `email_source`. Never pattern-guess an email and record it as found. Write results to `enrichment.json` (merge, never overwrite other entries). For big lists, split across parallel subagents writing `enrichment_partN.json`, then merge.

Then rerun qualify. Spot check five `ready` rows against their source URLs.

## 5. Verify emails (free)

```
python3 $SKILL/scripts/lead_engine.py verify --icp SLUG --run-id RUN_ID           # syntax + DNS
python3 $SKILL/scripts/lead_engine.py verify --icp SLUG --run-id RUN_ID --smtp    # also asks the mail server, if port 25 is open
```

Rows whose email fails move to `needs_enrichment` on the next qualify. If every SMTP probe comes back unknown, the network blocks port 25; DNS results still stand.

## 6. Export or push

```
python3 $SKILL/scripts/lead_engine.py export --icp SLUG --run-id RUN_ID
```
Writes `export_instantly.csv` (Instantly's import columns plus custom variables) and `export_leads.csv`. Use this if Nikki wants to send from Instantly or anywhere else.

```
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID                  # dry run, always first
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID --live --limit 5
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID --live
```
Show her the dry-run counts and two sample payloads, get an explicit yes, test with five, then push all. Upserts by email, adds tags without wiping existing ones, attaches a note with the evidence. Tags: the ICP tag from the file, the batch tag (run id), `seg-KEY`, `size-sweet-spot`, `email-TYPE`, `email-mx-ok` / `email-valid`, `dm-primary` / `dm-secondary`, `tech-NAME`, `signal-growth`. Flags: `--personal-only`, `--verified-only`, `--allow-no-email`.

Smartlist: the HighLevel API cannot create one, so in FG Funnels: Contacts > Smart Lists > More Filters > Tags is `RUN_ID` > Save as `"ICP NAME | RUN_ID"`. If Claude in Chrome is available and she says yes, do it for her and stop at any login screen.

## 7. Building a new ICP

```
python3 $SKILL/scripts/lead_engine.py new-icp --slug rv-park-owners --locations-from str-portfolio-managers \
  --prompt "Independent RV park and campground owners in New England and the Southeast with 25 to 150 sites, ..."
```

With an Anthropic key, Claude drafts the whole file from the prompt (this is the Instantly "AI Search" step). Without one, the template is copied for you to fill in. Either way, open the file and check: search terms (what the business is listed as on Google Maps), the size metric and its regexes, segments, accepted business types and the look-alikes to reject, exclusion list, tech fingerprints, CRM tag. Validate with `list-icps`. Run a 1-state pilot (`--states "Maine, USA"`) before a full batch and read `excluded.csv` to tune.

For a profile that is not shipped yet: interview Nikki (who, where, what size, who the buyer is, who to reject, what she sells them), run `new-icp`, and review the file with her. `references/icp-guide.md` has the field-by-field guide and the table of shipped lodging ICPs.

## 8. One-shot

```
python3 $SKILL/scripts/lead_engine.py run --icp SLUG --run-id RUN_ID --regions REGION --llm
```
Runs discover > read > qualify > verify > export. Enrichment and push stay manual on purpose.

## 9. Report

Keep it short: companies found (by source), read, qualified, ready with personal email, ready with generic email, pushed, segment split vs target, Smartlist name, the cost line from `status`, and anything that failed. Point to `leads.csv`. Do not recap each step.

## Rules

1. Never push to the CRM without a dry run and Nikki's yes for that batch.
2. Never invent or pattern-guess an email and treat it as found.
3. Size must be verified (stated number, listing count, or a human check) unless the ICP sets `require_verified: false`.
4. One batch tag per run so every Smartlist maps to exactly one batch.
5. Spend credits only on the shortlist: enrichment searches are capped and ordered by priority. Say the credit estimate before any step that will cost more than about 100 credits or 200 Places requests.
6. Respect site and platform terms: polite fetch rate, public pages only, no LinkedIn scraping (search snippets only).
