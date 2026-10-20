"""Thin wrappers around the paid or metered providers. Everything here records usage in the Ledger.

Providers:
  Google Places API (New) text search   -> discovery. 1,000 requests/month free at the field tier we use.
  Firecrawl /v2/search                   -> discovery and enrichment. 2 credits per 10 results.
  Firecrawl /v2/scrape                   -> fallback only, for JavaScript-only sites. 1 credit per page.
  Claude (Anthropic API)                 -> optional qualification. Haiku 5.5 by default, pennies per 100 companies.
"""
import json
import os
import time

import requests

from common import env, http_retry

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join([
    "places.id", "places.displayName", "places.websiteUri", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.primaryType", "places.types", "places.rating",
    "places.userRatingCount", "places.googleMapsUri", "places.businessStatus",
    "places.addressComponents", "nextPageToken",
])
FC = "https://api.firecrawl.dev/v2"


# ------------------------------------------------------------------ Google Places
def places_text_search(query, ledger, max_results=60, region_code="US", budget=None):
    """Yield place dicts for a free-text query. Up to 3 pages of 20 (Google's cap).

    Everything yielded is Google Places content: use it in memory to decide what to visit, then
    throw it away. Only the place_id may be stored (Places API Policies, caching exemption)."""
    key = env("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS}
    token, got = None, 0
    while got < max_results:
        body = {"textQuery": query, "pageSize": 20, "regionCode": region_code, "languageCode": "en"}
        if token:
            body["pageToken"] = token
        if budget is not None:
            budget.take()
        r = http_retry(lambda: requests.post(PLACES_URL, headers=headers, json=body, timeout=30))
        ledger.add("places_request")
        if r.status_code >= 300:
            raise RuntimeError(f"Places {r.status_code}: {r.text[:300]}")
        data = r.json()
        places = data.get("places") or []
        for p in places:
            got += 1
            yield p
        token = data.get("nextPageToken")
        if not token or not places:
            break
        time.sleep(1.5)  # next page token needs a moment to become valid


def place_to_company(p):
    comps = {}
    for c in p.get("addressComponents") or []:
        for t in c.get("types") or []:
            comps.setdefault(t, c.get("shortText") or c.get("longText"))
    return {
        "name": (p.get("displayName") or {}).get("text"),
        "website": p.get("websiteUri"),
        "phone": p.get("nationalPhoneNumber"),
        "address": p.get("formattedAddress"),
        "city": comps.get("locality") or comps.get("postal_town") or comps.get("sublocality"),
        "state": comps.get("administrative_area_level_1"),
        "postal_code": comps.get("postal_code"),
        "country": comps.get("country"),
        "category": p.get("primaryType"),
        "categories": p.get("types") or [],
        "maps_url": p.get("googleMapsUri"),
        "rating": p.get("rating"),
        "reviews": p.get("userRatingCount"),
        "business_status": p.get("businessStatus"),
        "place_id": p.get("id"),
    }


# ------------------------------------------------------------------ Firecrawl
def _fc_headers():
    key = env("FIRECRAWL_API_KEY")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def fc_search(query, ledger, limit=10, location=None, tbs=None, budget=None):
    """Web search. Returns list of {url, title, description}. Costs 2 credits per 10 results."""
    if budget is not None:
        budget.take()
    body = {"query": query, "limit": limit, "sources": ["web"], "country": "US"}
    if location:
        body["location"] = location
    if tbs:
        body["tbs"] = tbs
    r = http_retry(lambda: requests.post(f"{FC}/search", headers=_fc_headers(), json=body, timeout=60))
    ledger.add("firecrawl_credit", 2 * ((limit + 9) // 10))
    if r.status_code >= 300:
        raise RuntimeError(f"Firecrawl search {r.status_code}: {r.text[:300]}")
    data = r.json().get("data") or {}
    web = data.get("web") if isinstance(data, dict) else data
    out = []
    for item in web or []:
        if isinstance(item, dict) and item.get("url"):
            out.append({"url": item["url"], "title": item.get("title") or "", "description": item.get("description") or ""})
    return out


def fc_scrape(url, ledger, formats=("markdown", "rawHtml", "links")):
    """Scrape one page with a real browser. 1 credit. Returns the data dict (markdown, rawHtml, links, metadata)."""
    body = {"url": url, "formats": list(formats), "onlyMainContent": False, "timeout": 45000, "blockAds": True}
    r = http_retry(lambda: requests.post(f"{FC}/scrape", headers=_fc_headers(), json=body, timeout=120))
    ledger.add("firecrawl_credit", 1)
    if r.status_code >= 300:
        raise RuntimeError(f"Firecrawl scrape {r.status_code}: {r.text[:300]}")
    return r.json().get("data") or {}


# ------------------------------------------------------------------ Claude
def llm_available():
    return bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN") or os.path.exists(
        os.path.expanduser("~/.config/anthropic")))


def llm_model():
    return env("LEAD_ENGINE_MODEL", "claude-haiku-5-5")


def llm_json(system, user, schema, ledger, max_tokens=2000):
    """One structured-output call. Returns (dict_or_None, note). Records tokens in the ledger."""
    import anthropic  # imported lazily so the rest of the engine runs without the SDK installed

    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=llm_model(),
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.RateLimitError as e:
        return None, f"rate limited: {e}"
    except anthropic.APIStatusError as e:
        return None, f"api error {e.status_code}: {str(e)[:200]}"
    except anthropic.APIConnectionError as e:
        return None, f"connection error: {e}"
    u = resp.usage
    ledger.add("llm_input_mtok", (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
               + (getattr(u, "cache_read_input_tokens", 0) or 0))
    ledger.add("llm_output_mtok", u.output_tokens or 0)
    if resp.stop_reason == "refusal":
        return None, "model declined to classify this page"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text), "ok"
    except json.JSONDecodeError:
        return None, "unparseable model output"
