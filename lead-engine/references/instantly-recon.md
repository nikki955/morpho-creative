# Recon map: Instantly.ai Lead Finder / SuperSearch

Clean-room recon from public sources only (help center, marketing pages, pricing, third-party reviews). No account, no private APIs. The goal is to rebuild the *flow* of their lead finding and qualification with tools Morpho already pays for or can get free, not to copy their data.

## Sources

| source | what it gave us |
| --- | --- |
| help.instantly.ai/en/articles/11364248-supersearch | the full feature list, the three-step flow (search, enrich, export), the credit system |
| instantly.ai/b2b-lead-finder | positioning: filters, keywords, lookalikes, "only pay for verified leads" |
| instantly.ai/blog/instantly-features | enrichment types: AI web research agent, job listings, technology scan, news |
| enrich.so and salesforge.ai pricing breakdowns | Lead Finder plans: 1,000 leads for $47/mo, 4,500 for $96, 10,000 for $169 |
| docs.firecrawl.dev, developers.google.com/maps | the cost of the pieces we substitute |

## The core loop (what users pay for)

F01 Build a list and send it
    S01 search bar (AI prompt or filters) -> S02 results table -> S03 "Enrich & AI" -> S04 verified emails appear -> S05 export or push to campaign
    Happy path: 5 clicks. Our equivalent: 4 commands (discover, read, qualify, export) or one `run`.

## Screen inventory (inferred)

| ID | screen | purpose | key components |
| --- | --- | --- | --- |
| S01 | Search | AI prompt box ("Founders of SaaS companies in New York with 50-100 employees") or structured filters: job title, seniority, industry, company size, revenue, location, keywords, technology, funding, lookalike domains | prompt input, filter sidebar, saved searches |
| S02 | Results | one row per person: name, title, company, location, LinkedIn | table, select all, column picker |
| S03 | Enrich & AI | choose enrichment: work email (waterfall, 1 to 2+ credits), full profile (0.5), AI columns (0.5), verification (0.25) | modal with credit estimate |
| S04 | Enriched table | emails with verified badge, AI columns (summaries, research) | table |
| S05 | Export | CSV, CRM, or straight into an Instantly campaign | destination picker |
| S06 | Bring your own companies | upload company name + website, they match people | CSV upload |

## Inferred data model

```
Search      id, prompt_text, filters(json), saved
Company     name, domain, industry, size_band, revenue_band, location, technologies[], funding, news[], job_postings[]
Person      company_id, name, title, seniority, linkedin_url, location
Email       person_id, address, status (verified | catch_all | invalid | not_found), provider_used
Enrichment  person_id, type (work_email | profile | ai_column | verification), credits, result(json)
Export      destination (csv | crm | campaign), rows
```
Evidence: help article field lists, FAQ ("work email, job title, company name, location, LinkedIn URL, contact summary, headline, subindustry, company size, company description"). Confidence: high on fields, medium on statuses.

## Feature matrix

| feature | area | priority | Instantly | lead-engine | how |
| --- | --- | --- | --- | --- | --- |
| Natural-language ICP prompt | search | must | yes | yes | `new-icp --prompt` drafts the ICP JSON with Claude; the JSON is the filter set |
| Location filter | search | must | yes | yes | `locations` per ICP, searched per state or metro |
| Industry / keyword filter | search | must | yes | yes | `search_terms` (Google Maps categories) + `business_types` classification |
| Company size filter | search | must | yes (employee bands from a database) | yes, different metric | size counted from the company's own site: stated numbers, listing pages, sitemap. Units, rooms, sites, locations, whatever the ICP defines |
| Technology filter | search | should | yes | yes | HTML fingerprints per ICP (`signals.tech`) |
| Job-posting / hiring signal | enrich | should | yes | yes | hiring phrases + careers page |
| News / growth signal | enrich | could | yes | partial | growth phrases on the site; no news feed |
| Lookalike domains | search | should | yes | partial | `--import-csv` of seed companies plus list-page harvesting (top-10 lists are lookalike sets by construction) |
| Bring your own companies | search | must | yes | yes | `discover --import-csv` |
| Person search (title, seniority) | search | must | yes, 610M-row database | partial | people named on the site, JSON-LD, then targeted web / LinkedIn-scoped search for the shortlist only |
| Work email, waterfall over 5+ providers | enrich | must | yes | partial | emails actually published on the site, name-matched to the decision maker; generic inbox as fallback; no guessing |
| Email verification | enrich | must | yes (0.25 credits) | yes, free | syntax, MX, optional SMTP probe with catch-all detection |
| AI research columns | enrich | should | yes (0.5 credits) | yes | `qualify --llm`: business type, fit score, size with evidence, people, buying signals, one-line summary |
| Credit meter | billing | should | yes | yes | `costs.json` ledger per run and `status` |
| Export CSV | export | must | yes | yes | `export_instantly.csv` in their import shape, `export_leads.csv` full |
| Push to CRM | export | must | yes | yes | FG Funnels (HighLevel) upsert + tags + note |
| Push to a sending campaign | export | must | yes | no, by design | Instantly, or FG Funnels workflows, does the sending |
| Saved searches | search | could | yes | yes | the ICP file plus the run folder |

## What cannot be cloned, and what we do instead

| skip | why | substitute |
| --- | --- | --- |
| The 610M contact database | licensed data, years of aggregation | Google Places (free tier) + web search + the company's own website. Slower, but the ICPs here are local businesses with 3 to 20 staff, which contact databases cover badly anyway |
| Waterfall email providers | paid partner contracts | only report emails that were actually seen, with the source URL. For a personal email on a 15-person company, the about page beats most databases |
| LinkedIn person data | terms of service | public search snippets scoped to linkedin.com/in, name and title only, no scraping of profiles |
| Real-time verification at scale | their infrastructure | DNS + optional SMTP; port 25 is often blocked on home networks, in which case `mx_ok` is as far as it goes |

## Size

Screens 6, flows 1 core, entities 6. Hard parts: size verification without an LLM (solved with three fallbacks), people extraction from messy about pages (heuristics plus optional Claude pass), email verification from a residential network (SMTP optional). Size: M. Built.
