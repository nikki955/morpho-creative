# Setup (one time)

Put a `.env` file in the folder where you run Claude Code, or in this skill folder. Copy `.env.example`. Never commit it.

## Python

```
pip install requests beautifulsoup4 dnspython anthropic
```
`dnspython` and `anthropic` are optional (better email checks, and the Claude qualification pass).

## Google Places API key (discovery)

This replaces the Apify Google Maps scraper. The field tier the engine requests (it asks for the website field, which puts the call on the Text Search Enterprise SKU) is billed at $35 per 1,000 requests at list price; one request returns up to 20 places. Google advertises a monthly free usage cap per SKU, but it is shared across everything on the key, it can change, and the engine does not assume it. Set a budget alert and read the billing page after the first pilot.

Data handling: the engine keeps only the place_id from each result. Names, phones and addresses are taken from each company's own website. See `references/places-data-policy.md`.

1. console.cloud.google.com, create a project (call it "morpho-lead-engine").
2. Billing: attach a card and set a budget alert at $5 so a mistake is visible the same day.
3. APIs & Services > Library > search "Places API (New)" > Enable. (Not the legacy "Places API".)
4. APIs & Services > Credentials > Create credentials > API key. Under "API restrictions" restrict it to Places API (New).
5. Paste it as `GOOGLE_PLACES_API_KEY`.

Check with `lead_engine.py doctor`, then a one-state plan: `discover --icp inns --run-id pilot --source places --states "Vermont, USA" --max-per-search 20`. It prints the request ceiling and cost and makes no call until you add `--confirm`.

## Firecrawl key (optional)

Used for web-search discovery, JS-only site fallback, and the capped people searches. Firecrawl advertises a monthly credit grant on its free plan; treat it as a bonus, not a budget. firecrawl.dev > API keys. `FIRECRAWL_API_KEY=fc-...`

## Anthropic key (optional, recommended)

Turns on `qualify --llm`. console.anthropic.com > API keys. `ANTHROPIC_API_KEY=sk-ant-...`. Default model is Claude Haiku 5.5, roughly 6 cents per 100 companies at list price. Set `LEAD_ENGINE_MODEL=claude-sonnet-5-5` for a harder profile.

## FG Funnels CRM (push)

FG Funnels runs on HighLevel, so the scripts use the LeadConnector v2 API.

1. In FG Funnels, open the Morpho sub-account. Settings > Private Integrations > Create new integration, name it "Claude lead engine".
2. Scopes: View Contacts, Edit Contacts, View Tags, Edit Tags. The push dry run uses View Contacts to look each email up and check DND, unsubscribe and bounce state before anything is written. Add View Workflows if you want to read workflow names from the API; the engine still cannot see what a workflow listens for, so the Automation > Workflows check stays manual.
3. Copy the token as `GHL_API_KEY`.
4. Settings > Business Profile > Location ID as `GHL_LOCATION_ID`.
5. Settings > Business Profile > Allow Duplicate Contact: leave off, or set matching to email first, so upserts update rather than duplicate.

If Private Integrations is missing, ask FG Funnels support to enable it for the plan.

## Where runs go

`./leads/<run-id>/` under the working folder. Set `LEAD_ENGINE_DIR` to move them (a Drive-synced folder works well). Each run folder is self-contained: `companies.json`, `site_data/`, `llm.json`, `enrichment.json`, `verification.json`, the CSVs, `costs.json`.
