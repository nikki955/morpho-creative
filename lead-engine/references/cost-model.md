# Cost model: yesterday's pipeline vs the lead engine

Numbers per 300 companies discovered, which is roughly what it takes to land 100 ready leads for a tight ICP.

## Yesterday (str-lead-gen)

| step | tool | unit cost | per 300 companies |
| --- | --- | --- | --- |
| discover | Apify Google Maps scraper (compass/crawler-google-places) | ~$4 per 1,000 places plus platform compute, and the scraper over-fetches (5 terms x 6 states x 40 = 1,200 places to get 300 unique) | $5 to $12 |
| read | Firecrawl map + 4 scrapes with JSON extraction | ~17 credits per company | 5,100 credits = $85 on Hobby ($16 / 3,000), or most of a Standard plan |
| enrich | Firecrawl search per gap | 2 credits each | 100 to 200 credits |
| total | | | **roughly $90 to $100 per batch**, and a Firecrawl plan upgrade |

## Lead engine

| step | tool | unit cost | per 300 companies |
| --- | --- | --- | --- |
| discover | Google Places Text Search | 1,000 requests/month free, then $35 per 1,000. One request = 20 places. 30 queries x 3 pages = 90 requests | $0 (inside free tier) |
| discover (alt) | Firecrawl search | 2 credits per 10 results. 30 queries x 2 pages = 120 credits | $0 on the free 1,000 credits, else ~$2 |
| read | plain HTTP + HTML parsing | $0 | $0 |
| read fallback | Firecrawl scrape for JS-only sites, opt in | 1 credit each, typically 5 to 10 percent of sites | 15 to 30 credits, $0 to $0.50 |
| qualify rules | local | $0 | $0 |
| qualify --llm | Claude Haiku 5.5 | ~4,500 input + 300 output tokens per company at $0.10 / $0.50 per million | **$0.18** |
| enrich people | Firecrawl search, shortlist only | 2 credits (4 with --linkedin) x 30 companies | 60 to 120 credits |
| verify | DNS + optional SMTP | $0 | $0 |
| push | FG Funnels API | $0 | $0 |
| total | | | **under $1 per batch** inside free tiers, **$3 to $5** if every free tier is exhausted |

Eight ICPs at one batch each per month stays inside the Google Places free tier (720 requests) and inside a free Firecrawl account for the fallbacks and people searches. The only line that scales linearly is the LLM pass, at about 6 cents per 100 companies.

## Where the savings come from

1. The website is read once with a normal HTTP fetch instead of five browser renders. Small business sites are almost all server-rendered WordPress, Squarespace or Wix, which return full HTML without JavaScript.
2. JSON extraction moved from Firecrawl's per-page LLM (5 credits a page, several pages a company) to one Haiku call per company over all pages at once.
3. Size is counted from the sitemap and listing index for free before any model is asked.
4. Discovery uses Google's own Places API within its free quota instead of a scraper that pays to render Maps.
5. Enrichment searches only run for companies that already passed qualification, in priority order, with a cap.

## Price assumptions baked into costs.json

`scripts/common.py` holds `UNIT_COST_USD`. Edit it if your plans differ. The ledger reports worst case (list price, no free tier) so the number it prints is an upper bound.
