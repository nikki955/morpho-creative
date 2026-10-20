---
name: lead-engine
description: Multi-ICP lead engine for Morpho. Finds local businesses that match an ICP file (STR managers, RV parks, boutique hotels, glamping, inns, B&Bs), reads their websites for free, verifies size from stated numbers and listing pages, picks the decision maker and a published email, verifies the email, tags the buyer type, and exports to CSV or pushes tagged contacts into FG Funnels behind suppression checks and an explicit per-batch approval. Replaces the Apify plus Firecrawl pipeline at a few dollars per batch instead of ninety, and stores no Google Places content. Use when Nikki says "run lead gen for" an ICP, "find leads" for an avatar, "new lead batch", "build an ICP", "add an ICP profile", or invokes /lead-engine. For the STR ICP this supersedes /str-lead-gen.
---

# Morpho Lead Engine

One CLI, one ICP file per profile, one run folder per batch. Pipeline:

discover (Google Places or web search, region named by Nikki, confirmed before spend) > read (plain HTTP, 0 credits) > qualify (rules, plus optional Claude Haiku pass) > enrich gaps (capped, cheap) > verify emails (free) > export CSV, or push to FG Funnels (dry run, then --live with --approve).

Everything lives in `scripts/` ($SKILL below means the path to this skill folder). ICP files live in `icps/`. Guardrails live in `config/crm_guardrails.json`. Runs land in `./leads/RUN_ID/` under the working folder (or `LEAD_ENGINE_DIR`). Always pass the same `--icp` and `--run-id`; the run id becomes the CRM batch tag.

Read `references/icp-guide.md` before writing or editing an ICP, `references/cost-model.md` if Nikki asks what a batch costs, `references/places-data-policy.md` for what is and is not stored from Google, and `references/instantly-recon.md` for what this clones from Instantly's Lead Finder.

## Hard rules

1. Nikki names the region or states for every API discovery batch. Never pick a default region and never search every configured region. CSV import needs no region.
2. Show the discovery plan (queries, request ceiling, list-price estimate) and get her yes before passing `--confirm`. Respect `--max-requests`. Never describe an API call as free; a provider allowance may or may not cover it.
3. Never write to the CRM without a dry run she has seen and her explicit yes for that batch, expressed as `--live --approve RUN_ID`. One batch tag per run.
4. Never add a tag that triggers a workflow or belongs to the track scheme (`aio-sent`, `partner-sent`, `goodwill`, `track-*`, `morpho-ch`, `replied-*`, `warm-*`, and the rest in `config/crm_guardrails.json`). The engine refuses them; do not work around it.
5. Never invent or pattern-guess an email and treat it as found. Never export or push an email that failed verification.
6. Size must be verified (stated number, listing count, or a human check) unless the ICP sets `require_verified: false`.
7. Store nothing from Google Places except the place_id. Names, phones, addresses come from the company's own website and carry that attribution.
8. Respect site and platform terms: polite fetch rate, public pages only, no LinkedIn scraping (search snippets only).

## 0. Preflight (every run)

1. `python3 $SKILL/scripts/lead_engine.py doctor`. It lists which keys are set, which guardrails are loaded, and what each key unlocks. Minimum to run anything: `GOOGLE_PLACES_API_KEY` or `FIRECRAWL_API_KEY`, or a CSV of companies. `ANTHROPIC_API_KEY` turns on the Claude qualification pass. CRM push needs `GHL_API_KEY` and `GHL_LOCATION_ID`. Setup is in `references/setup.md`.
2. `pip install requests beautifulsoup4 dnspython anthropic` if the doctor reports missing modules.
3. Pick the ICP: `lead_engine.py list-icps`. Shipped: `str-portfolio-managers`, `rv-parks-campgrounds`, `boutique-hotels`, `glamping`, `inns`, `bed-and-breakfasts`. The five lodging ICPs come from the "Morpho ICPs: Independent Lodging Owners" doc and are configured for the US minus Maine. If the profile does not exist yet, go to section 7 first.
4. Ask Nikki for: the region key or the explicit states for this batch (required, no default), the batch size (default `batch_target` in the ICP), the run id (default `SLUG-YYYY-MM-DD`), and the per-run request cap if she wants something other than 100. If she is not around, do only the free steps (import, read, qualify, verify) and stop before any paid call or CRM write.

## 1. Discover

```
python3 $SKILL/scripts/lead_engine.py discover --icp SLUG --run-id RUN_ID --source places --regions REGION
python3 $SKILL/scripts/lead_engine.py discover --icp SLUG --run-id RUN_ID --source places --states "Vermont, USA" "Stowe, VT"
```

The first call prints the plan and makes no API call. Show it to Nikki: queries, worst-case requests, list-price estimate, cap. On her yes, rerun with `--confirm`. `--max-requests N` is a hard stop enforced inside the provider calls (default 100 paid requests). `--source search` uses Firecrawl web search instead (2 credits per 10 results) and also harvests "top 10" list pages for free. `--import-csv file.csv` adds a list Nikki already has, needs no region and no key.

Places results are used in memory only. Each candidate's own website is fetched and that is where the stored name, phone and address come from; only the place_id is kept from Google. Ratings, review counts, Maps URLs and categories are never written anywhere.

Aim for about 3x the batch target. If short, add a region on her say-so.

## 2. Read every website (free)

```
python3 $SKILL/scripts/lead_engine.py read --icp SLUG --run-id RUN_ID --workers 6
```

Fetches the homepage, up to 4 more pages (about/team, listing index, owners, contact, careers), and the sitemap. Extracts emails, phones, people with titles, size statements, listing counts, software fingerprints, booking-path evidence, hiring, growth, ownership-change and multi-property signals. Zero credits.

Booking is classified from evidence, not from software: a link, iframe or form pointing at a reservation engine, a booking subdomain, or a form with a check-in date field means `confirmed`; an explicit "call to reserve" with nothing else means `none_found`; everything else, including a site that only loads a PMS script, is `unknown` and stays out of the no-direct-booking segment.

Sites that render nothing without JavaScript are flagged `JS-SHELL`. Rerun those with `--firecrawl-fallback --force --domains a.com b.com` (1 credit each) after telling Nikki the count.

## 3. Qualify

```
python3 $SKILL/scripts/lead_engine.py qualify --icp SLUG --run-id RUN_ID          # rules only, free
python3 $SKILL/scripts/lead_engine.py qualify --icp SLUG --run-id RUN_ID --llm    # plus one Haiku call per company
```

Use `--llm` whenever `ANTHROPIC_API_KEY` exists. It costs cents per 100 companies and improves business-type calls, decision-maker picks and size evidence. Results cache in `llm.json`. `LEAD_ENGINE_MODEL` overrides the model (default `claude-haiku-5-5`).

Every lead also gets a `buyer_type` (owner_operator, portfolio_owner_manager, recent_acquirer) from the LLM pass, or from ownership-change and multi-property phrases on the site when the LLM is off. It drives the `buyer-TYPE` CRM tag; `recent_acquirer` sorts higher.

Report `summary.json` in two lines. Statuses:
1. `ready`: in range, named decision maker, personal email. Goes to export or CRM once verified.
2. `ready_generic`: in range, but the best email is info@ style or there is no named person. Enrich first if possible.
3. `needs_enrichment`: in range, no email at all, or the email failed verification.
4. `review`: size not verified. Must be verified before it counts.
5. `excluded`: fails the ICP. Skim `excluded.csv` for misclassifications and override in `enrichment.json` with `"include": true`.

## 4. Enrich the gaps (cheap, capped)

```
python3 $SKILL/scripts/lead_engine.py enrich --icp SLUG --run-id RUN_ID --verify-size             # free: walks listing pages
python3 $SKILL/scripts/lead_engine.py enrich --icp SLUG --run-id RUN_ID --find-people --max 30    # 2 credits each, 4 with --linkedin
```

Tell Nikki the credit estimate before `--find-people`. Candidates land in `enrichment_candidates.json`; promote the right person into `enrichment.json` (format at the top of `scripts/qualify.py`). LinkedIn-headline hits that name the company with a primary title are auto-promoted with name and title only, never an email.

For whatever is still missing, work the list by hand in priority order: about pages, Google Business Profile owner replies, local news, chamber pages, state business registry, Facebook About tab, PDF management agreements. Only record an email you actually saw, with its URL as `email_source`. Then rerun qualify and spot check five `ready` rows against their source URLs.

## 5. Verify emails (free)

```
python3 $SKILL/scripts/lead_engine.py verify --icp SLUG --run-id RUN_ID --all      # syntax + DNS
python3 $SKILL/scripts/lead_engine.py verify --icp SLUG --run-id RUN_ID --all --smtp   # also asks the mail server, if port 25 is open
python3 $SKILL/scripts/lead_engine.py qualify --icp SLUG --run-id RUN_ID              # always re-qualify after verify
```

Statuses and what they mean: `valid` = the mailbox answered (mailbox verified). `mx_ok` = DNS found a mail server, mailbox not verified. `catch_all` = the server accepts any address, mailbox not verified. `no_mx`, `invalid_syntax`, `smtp_rejected` = failed; such rows move to `needs_enrichment` on re-qualify and are never exported or pushed. Unchecked emails are skipped by export and push until verify has run. CRM tags say `email-dns-ok`, `email-catch-all` or `email-mailbox-verified`, never "verified" for a DNS-only result.

## 6. Export or push

```
python3 $SKILL/scripts/lead_engine.py export --icp SLUG --run-id RUN_ID
```
Writes `export_instantly.csv` (with an `email_status` column), `export_leads.csv`, and `export_skipped.csv` listing every row left out and why.

```
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID                             # dry run
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID --live --approve RUN_ID --limit 5
python3 $SKILL/scripts/lead_engine.py push --icp SLUG --run-id RUN_ID --live --approve RUN_ID
```

The dry run makes read-only lookups in the CRM and writes nothing. For every eligible lead it shows the action and the reason: `create` (new contact), `add-tags` (existing contact with no Morpho track; tags added, fields never overwritten), `skip` with the reason (suppressed by DND, email DND, an unsubscribe/bounce/do-not-contact tag, or an existing relationship tag such as `morpho-ch`, `track-*`, `aio-sent`, the old real-estate list tags), or `blocked` when the lookup failed and suppression cannot be checked. It prints the full tag set and states that none equals a documented live trigger or reserved track tag.

Show Nikki the dry run, including the tag list and the workflow-trigger note. She must confirm in Automation > Workflows that no published workflow listens for the ICP tag, the batch tag, or any `seg-`, `buyer-`, `email-`, `dm-`, `tech-`, `size-`, `signal-` tag. Then, on her explicit yes for this batch, run `--live --approve RUN_ID` with `--limit 5` first, read the five contacts back, then the rest. `--update-existing` adds tags to contacts already in a Morpho track; it never overwrites their fields and still skips suppressed contacts. Nothing the engine does enrols anyone in a workflow unless a workflow is configured to fire on one of these tags, which is exactly what she is checking.

Smartlist: the HighLevel API cannot create one, so in FG Funnels: Contacts > Smart Lists > More Filters > Tags is RUN_ID > Save as "ICP NAME | RUN_ID". If Claude in Chrome is available and she says yes, do it for her and stop at any login screen.

## 7. Building a new ICP

```
python3 $SKILL/scripts/lead_engine.py new-icp --slug rv-park-owners --locations-from str-portfolio-managers \
  --prompt "Independent RV park and campground owners in New England and the Southeast with 25 to 150 sites, ..."
```

With an Anthropic key, Claude drafts the whole file from the prompt. Without one, the template is copied for you to fill in. Either way, open the file and check: search terms, the size metric and its regexes, segments, accepted business types and the look-alikes to reject, exclusion list, tech fingerprints, CRM tag (it must not start with a protected prefix; `check_tags` in `scripts/crm.py` will refuse it otherwise). Validate with `list-icps`. Run a one-state pilot before a full batch and read `excluded.csv` to tune.

## 8. One-shot

```
python3 $SKILL/scripts/lead_engine.py run --icp SLUG --run-id RUN_ID --source places --regions REGION --confirm --llm
python3 $SKILL/scripts/lead_engine.py run --icp SLUG --run-id RUN_ID --import-csv file.csv --llm
```
Runs discover > read > qualify > verify > qualify > export. Without `--confirm` it prints the discovery plan and stops. It never pushes to the CRM; enrichment and push stay manual on purpose.

## 9. Older runs

`lead_engine.py scrub-places --icp SLUG --run-id RUN_ID` shows how many companies in an older run folder still carry Google Places fields; `--yes` rewrites them from `site_data`. Run it only when Nikki decides to (see `references/places-data-policy.md`).

## 10. Report

Keep it short: companies found (by source), read, qualified, ready with personal email, ready with generic email, verified counts by status, pushed (create / add-tags / skipped with reasons), segment and buyer-type split vs target, Smartlist name, the cost line from `status`, and anything that failed. Point to `leads.csv`. Do not recap each step.
