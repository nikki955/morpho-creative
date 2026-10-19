# Setup (one time)

Put a `.env` file in the folder where you run Claude Code, or in this skill folder. Copy `.env.example`. Never commit it.

## Python

```
pip install requests beautifulsoup4 dnspython anthropic
```
`dnspython` and `anthropic` are optional (better email checks, and the Claude qualification pass).

## Google Places API key (discovery, free tier)

This replaces the Apify Google Maps scraper. The field tier the engine requests (website and phone) gets 1,000 free requests a month, about 20,000 businesses, then $35 per 1,000 requests.

1. console.cloud.google.com, create a project (call it "morpho-lead-engine").
2. Billing: attach a card. Nothing is charged inside the free tier, and you can set a budget alert at $5.
3. APIs & Services > Library > search "Places API (New)" > Enable. (Not the legacy "Places API".)
4. APIs & Services > Credentials > Create credentials > API key. Under "API restrictions" restrict it to Places API (New).
5. Paste it as `GOOGLE_PLACES_API_KEY`.

Check with `lead_engine.py doctor`, then a 1-state run: `discover --icp str-portfolio-managers --states "Maine, USA" --max-per-search 20`.

## Firecrawl key (optional)

Used for web-search discovery, JS-only site fallback, and the capped people searches. Free accounts get 1,000 credits a month. firecrawl.dev > API keys. `FIRECRAWL_API_KEY=fc-...`

## Anthropic key (optional, recommended)

Turns on `qualify --llm`. console.anthropic.com > API keys. `ANTHROPIC_API_KEY=sk-ant-...`. Default model is Claude Haiku 5.5, about 6 cents per 100 companies. Set `LEAD_ENGINE_MODEL=claude-sonnet-5-5` for a harder profile.

## FG Funnels CRM (push)

FG Funnels runs on HighLevel, so the scripts use the LeadConnector v2 API.

1. In FG Funnels, open the Morpho sub-account. Settings > Private Integrations > Create new integration, name it "Claude lead engine".
2. Scopes: View Contacts, Edit Contacts, View Tags, Edit Tags.
3. Copy the token as `GHL_API_KEY`.
4. Settings > Business Profile > Location ID as `GHL_LOCATION_ID`.
5. Settings > Business Profile > Allow Duplicate Contact: leave off, or set matching to email first, so upserts update rather than duplicate.

If Private Integrations is missing, ask FG Funnels support to enable it for the plan.

## Where runs go

`./leads/<run-id>/` under the working folder. Set `LEAD_ENGINE_DIR` to move them (a Drive-synced folder works well). Each run folder is self-contained: `companies.json`, `site_data/`, `llm.json`, `enrichment.json`, `verification.json`, the CSVs, `costs.json`.
