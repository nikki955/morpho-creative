"""Shared helpers for the Morpho lead engine (multi-ICP)."""
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

SKILL_DIR = Path(__file__).resolve().parent.parent
ICP_DIR = SKILL_DIR / "icps"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MorphoLeadEngine/1.0")

# Sites that show up in search results but are never the company itself.
AGGREGATOR_DOMAINS = (
    "google.com", "yelp.com", "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.com", "airbnb.com", "vrbo.com", "booking.com", "expedia.com",
    "tripadvisor.com", "hotels.com", "zillow.com", "realtor.com", "redfin.com", "trulia.com",
    "yellowpages.com", "yelp.ca", "bbb.org", "mapquest.com", "manta.com", "chamberofcommerce.com",
    "cylex.us.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "angi.com", "thumbtack.com",
    "houzz.com", "nextdoor.com", "wikipedia.org", "reddit.com", "quora.com", "medium.com",
    "crunchbase.com", "zoominfo.com", "apollo.io", "dnb.com", "buzzfile.com", "bizapedia.com",
    "opencorporates.com", "clutch.co", "g2.com", "capterra.com", "amazon.com", "etsy.com",
    "linktr.ee", "wixsite.com", "godaddysites.com", "square.site", "business.site",
    "evolve.com", "vacasa.com", "hipcamp.com", "campspot.com", "rvshare.com", "outdoorsy.com",
    "theknot.com", "weddingwire.com", "zola.com", "eventective.com", "peerspace.com",
    "hostfully.com", "guesty.com", "hostaway.com", "lodgify.com", "ownerrez.com",
    "campendium.com", "rvparky.com", "thedyrt.com", "goodsam.com", "allstays.com", "rvparkreviews.com",
    "campspot.com", "reserveamerica.com", "recreation.gov", "koa.com", "glampinghub.com", "bedandbreakfast.com",
    "bbonline.com", "innsofamerica.com", "selectregistry.com", "historichotels.org", "trivago.com", "kayak.com",
    "priceline.com", "agoda.com", "hostelworld.com", "oyster.com", "travelocity.com", "orbitz.com",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\b[2-9]\d{2}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
JUNK_EMAIL_DOMAINS = (
    "example.com", "sentry.io", "wixpress.com", "domain.com", "email.com", "yourdomain.com",
    "godaddy.com", "squarespace.com", "mysite.com", "wordpress.org", "wordpress.com", "w3.org",
    "schema.org", "google.com", "googleapis.com", "cloudflare.com", "jquery.com", "2x.png",
)
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css", ".woff", ".woff2")


# ---------------------------------------------------------------- env / config
def load_env():
    """Load KEY=VALUE pairs from .env in the cwd, then the skill folder. Real env vars win."""
    for p in (Path.cwd() / ".env", SKILL_DIR / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def env(key, default=None):
    load_env()
    return os.environ.get(key) or default


def require_env(*keys):
    load_env()
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}. Add them to .env (see .env.example).")
    return [os.environ[k] for k in keys]


def list_icps():
    return sorted(p.stem for p in ICP_DIR.glob("*.json") if not p.stem.startswith("_"))


def load_icp(slug):
    p = ICP_DIR / f"{slug}.json"
    if not p.exists():
        sys.exit(f"No ICP file {p}. Available: {', '.join(list_icps()) or 'none'}. "
                 f"Create one with: lead_engine.py new-icp --slug {slug}")
    icp = json.loads(p.read_text())
    errors = validate_icp(icp)
    if errors:
        sys.exit("ICP file has problems:\n  " + "\n  ".join(errors))
    return icp


REQUIRED_ICP_KEYS = ["slug", "name", "description", "search_terms", "locations", "size",
                     "business_types", "decision_makers", "exclude", "signals", "email", "pages", "crm"]


def validate_icp(icp):
    errs = [f"missing key: {k}" for k in REQUIRED_ICP_KEYS if k not in icp]
    if errs:
        return errs
    size = icp["size"]
    for k in ("metric", "min", "max"):
        if k not in size:
            errs.append(f"size.{k} missing")
    if not isinstance(icp["locations"], dict) or not icp["locations"]:
        errs.append("locations must be a non-empty object of region -> [places]")
    bt = icp["business_types"]
    if "accept" not in bt or "options" not in bt:
        errs.append("business_types needs 'accept' (list) and 'options' (object of key -> description)")
    else:
        for a in bt["accept"]:
            if a not in bt["options"]:
                errs.append(f"business_types.accept '{a}' is not in business_types.options")
    for seg in icp.get("segments", []):
        if "key" not in seg:
            errs.append("every segment needs a key")
    return errs


def default_run_id(icp_slug):
    return f"{icp_slug}-{date.today().isoformat()}"


def run_dir(run_id):
    base = Path(os.environ.get("LEAD_ENGINE_DIR", Path.cwd() / "leads"))
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "site_data").mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------- urls / domains
def root_domain(url):
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":")[0].split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_url(url):
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def is_aggregator(domain, icp=None):
    extra = tuple((icp or {}).get("exclude", {}).get("domains", []))
    for d in AGGREGATOR_DOMAINS + extra:
        if domain == d or domain.endswith("." + d):
            return True
    return False


# ---------------------------------------------------------------- json / csv
def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- emails
def extract_emails(*texts):
    found = set()
    for t in texts:
        if not t:
            continue
        for m in EMAIL_RE.findall(t):
            e = m.strip(".").lower()
            dom = e.split("@")[-1]
            if e.endswith(JUNK_EMAIL_SUFFIXES) or any(dom == j or dom.endswith("." + j) for j in JUNK_EMAIL_DOMAINS):
                continue
            if len(e) > 80 or e.startswith("%") or "noreply" in e or "no-reply" in e or "@2x" in e:
                continue
            if re.search(r"\.(png|jpg|svg|gif|webp)$", e.split("@")[0]):
                continue
            found.add(e)
    return sorted(found)


def classify_email(email, icp, domain):
    local, dom = email.split("@")[0].lower(), email.split("@")[-1].lower()
    cfg = icp["email"]
    if any(local == p or local.startswith(p + ".") for p in cfg.get("owner_prefixes", [])):
        return "owner_role"
    if any(local == p or local.startswith(p) for p in cfg.get("role_prefixes", [])):
        return "generic"
    if dom == domain or dom.endswith("." + domain):
        return "probably_personal"
    if dom in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "me.com", "comcast.net"):
        return "freemail"
    return "external"


def email_matches_person(email, first, last):
    if not email or not first:
        return False
    local = re.sub(r"[^a-z]", "", email.split("@")[0].lower())
    f, l = re.sub(r"[^a-z]", "", first.lower()), re.sub(r"[^a-z]", "", (last or "").lower())
    cands = {f, f + l, l + f, (f[:1] + l) if l else None, (f + l[:1]) if l else None, (l + f[:1]) if l else None}
    return local in {c for c in cands if c and len(c) >= 3}


def split_name(name):
    parts = (name or "").replace(",", " ").split()
    parts = [p for p in parts if p.lower().strip(".") not in ("mr", "mrs", "ms", "dr", "jr", "sr", "ii", "iii")]
    if not parts:
        return "", ""
    return parts[0].title(), " ".join(parts[1:]).title()


def title_tier(title, icp):
    t = (title or "").lower()
    dm = icp["decision_makers"]
    if any(k in t for k in dm.get("primary", [])):
        return 1
    if any(k in t for k in dm.get("secondary", [])):
        return 2
    return 3 if t else 4


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ---------------------------------------------------------------- http
_session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return _session


def http_retry(fn, tries=4, base_wait=2.0):
    """Call fn() which returns a requests.Response. Retries on 429 and 5xx."""
    resp = None
    for i in range(tries):
        try:
            resp = fn()
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            time.sleep(base_wait * (2 ** i))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            try:
                wait = float(resp.headers.get("Retry-After", base_wait * (2 ** i)))
            except ValueError:
                wait = base_wait * (2 ** i)
            time.sleep(min(wait, 60))
            continue
        return resp
    return resp


# ---------------------------------------------------------------- cost ledger
# Rough unit prices so each run can report what it cost. Edit if your plans differ.
UNIT_COST_USD = {
    "places_request": 0.035,        # Text Search Enterprise after the 1,000 free/month
    "firecrawl_credit": 0.0167,     # Hobby plan: $16 / 3,000 credits (Standard is ~$0.0008)
    "llm_input_mtok": 0.10,         # Claude Haiku 5.5 per 1M input tokens
    "llm_output_mtok": 0.50,        # Claude Haiku 5.5 per 1M output tokens
}
FREE_PER_MONTH = {"places_request": 1000, "firecrawl_credit": 1000}


class Ledger:
    """Counts paid units per run so the report can say what a batch cost."""

    def __init__(self, out):
        self.path = Path(out) / "costs.json"
        self.data = read_json(self.path, {}) or {}

    def add(self, unit, n=1):
        self.data[unit] = self.data.get(unit, 0) + n
        write_json(self.path, self.data)

    def summary(self):
        lines, total = [], 0.0
        for unit, n in sorted(self.data.items()):
            price = UNIT_COST_USD.get(unit, 0)
            if unit.endswith("_mtok"):
                usd = n / 1e6 * price
                lines.append(f"{unit}: {n:,} tokens  ~${usd:.3f}")
            else:
                usd = n * price
                free = FREE_PER_MONTH.get(unit)
                note = f" (first {free:,}/month are free on the provider's free tier)" if free else ""
                lines.append(f"{unit}: {n:,}  ~${usd:.2f} at list price{note}")
            total += usd
        lines.append(f"estimated spend this run (worst case, ignoring free tiers): ${total:.2f}")
        return "\n".join(lines)
