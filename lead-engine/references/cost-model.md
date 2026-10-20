# Cost model: what a batch costs, stated honestly

Updated 2026-10-20. Every figure below is a list price. Both Google and Firecrawl advertise a monthly allowance (Google: a per-SKU monthly free usage cap on Places; Firecrawl: a monthly credit grant on the free plan). Those allowances change, are shared with any other project on the same key or account, and are not visible to this engine, so no estimate here assumes them. The run ledger (`costs.json`, shown by `status`) also reports list price. If the console shows the allowance covered a run, good, but do not plan on it.

Numbers per 300 companies discovered, which is roughly what it takes to land 100 ready leads for a tight ICP.

## Yesterday's pipeline (str-lead-gen)

| step | tool | unit cost | per 300 companies |
| --- | --- | --- | --- |
| discover | Apify Google Maps scraper | roughly $4 per 1,000 places plus platform compute; over-fetches 3 to 4x to reach 300 unique | $5 to $12 |
| read | Firecrawl map plus 4 scrapes with JSON extraction | about 17 credits per company | about 5,100 credits, $85 on the Hobby plan |
| enrich | Firecrawl search per gap | 2 credits each | 100 to 200 credits |
| total | | | roughly $90 to $100 per batch, and it stored Google Places content |

## Lead engine

| step | tool | list price | per 300 companies |
| --- | --- | --- | --- |
| discover | Google Places Text Search (Enterprise SKU because the request asks for the website field) | $35 per 1,000 requests; one request returns up to 20 places; a query is up to 3 requests | 30 queries = 90 requests = $3.15 |
| discover (alternative) | Firecrawl search | 2 credits per 10 results, $16 per 3,000 credits on Hobby | 30 queries = 60 to 120 credits = $0.32 to $0.64 |
| read | plain HTTP plus HTML parsing | $0 | $0 |
| read fallback | Firecrawl scrape, opt in, JavaScript-only sites | 1 credit each | typically 15 to 30 credits, $0.08 to $0.16 |
| qualify, rules | local | $0 | $0 |
| qualify --llm | Claude Haiku 5.5 | $0.10 per million input tokens, $0.50 per million output; about 4,500 in and 300 out per company | about $0.18 |
| enrich people | Firecrawl search, shortlist only, capped | 2 credits (4 with --linkedin) per company | 30 companies = 60 to 120 credits, $0.32 to $0.64 |
| verify | DNS plus optional SMTP | $0 | $0 |
| push | FG Funnels API | $0 | $0 |
| total | | | about $4 to $5 per batch at list price, versus $90 to $100 before |

Eight ICPs at one batch each per month: about 720 Places requests ($25 at list price) plus a few hundred Firecrawl credits. Whether any of that is absorbed by an allowance is something to read off the billing console after the first pilot, not something to assume.

## Spend controls in the engine

1. API discovery refuses to run without an explicit `--regions` or `--states`. There is no default region and no "all regions".
2. Before any paid call, `discover` prints the query list, the worst-case request count, and the list-price estimate, then exits unless `--confirm` is passed.
3. `--max-requests` (default 100) is enforced inside the provider calls, so a runaway loop stops at the cap. One Places request is 20 places, so 100 requests is up to 2,000 places, which is more than a batch needs.
4. Enrichment searches run only for the qualified shortlist, in priority order, capped by `--max`.
5. The ledger prints after every step.

## Where the savings come from

1. The website is read once with a normal HTTP fetch instead of five browser renders. Small business sites are mostly server-rendered.
2. JSON extraction moved from Firecrawl's per-page LLM to one Haiku call per company over all pages at once.
3. Size is counted from the sitemap and listing index before any model is asked.
4. Discovery uses Google's own Places API, within a stated per-run cap, instead of a scraper that pays to render Maps, and keeps only the place_id.
5. Enrichment searches only run for companies that already passed qualification.

## Price assumptions baked into costs.json

`scripts/common.py` holds `UNIT_COST_USD`. Edit it if your plans differ. The ledger reports list price with no allowance applied, so the number it prints is an upper bound on what the provider can bill for that run.
