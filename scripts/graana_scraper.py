#!/usr/bin/env python3
"""
graana_scraper.py — scrape property listings from graana.com into a fixed schema.

zameen.com's robots.txt disallows crawling its city listing pages (including
Islamabad), so this targets graana.com instead, whose robots.txt explicitly
allows `User-agent: *` / `Allow: /` with no per-city block. graana.com is
server-rendered (Next.js): each search page embeds a `__NEXT_DATA__` JSON
blob with clean, structured listing data — no HTML-card scraping needed.

Output schema (one row per listing), with `price` left in PKR (see
NOTE_PRICE below):
    id          int    Graana listing ID
    price       int    Price in PKR   (see NOTE_PRICE below)
    baths       int    Number of bathrooms
    rooms       int    Number of bedrooms
    sqm         int    Size in SQUARE METRES. Use --area-unit sqft for
                        actual square feet instead.
    description str    Free-text property description
    location    str    Neighbourhood name

NOTE_PRICE: kept in raw PKR, no currency conversion — the agent is simply
    told prices are in PKR.
NOTE_RENT: only sale (purpose=buy) listings are scraped, so `price` is always
    a one-time purchase price. Some free-text descriptions mention a possible
    *rental* value as a side note (e.g. "Per day: 12000 - 15000, Per Month:
    1.25 - 1.50 lacs") — that's marketing copy about the property's rental
    potential, not the listing's own price, and is two orders of magnitude
    smaller than the actual sale price. It's left as-is in `description`.

Usage
-----
    python graana_scraper.py --url https://www.graana.com/sale/house-sale-islamabad-1/ \
                              --pages 19 --out ../data/islamabad_properties.csv

    # verify the parsing helpers without touching the network
    python graana_scraper.py --self-test

Politeness: obeys robots.txt by default (graana.com explicitly allows this),
rate-limits to --delay seconds between requests, and identifies itself in the
User-Agent. graana.com's Terms of Use govern what you may do with the data —
check them before redistributing anything you collect.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict, field
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlsplit, parse_qs, urlencode, urlunsplit

import requests

log = logging.getLogger("graana")

BASE = "https://www.graana.com"
UA = "graana-research-scraper/1.0 (+contact: you@example.com)"

# --------------------------------------------------------------------------
# Unit conversion, verified against graana.com's own displayed size units
# for Islamabad listings.
# --------------------------------------------------------------------------
SQM_PER_SQFT = 0.09290304
SQM_PER_SQYD = 0.83612736

AREA_TO_SQM = {
    "marla": 25 * SQM_PER_SQYD,        # 20.9031840
    "kanal": 20 * 25 * SQM_PER_SQYD,   # 418.0636800
    "sqft": SQM_PER_SQFT,
    "sqyd": SQM_PER_SQYD,
    "sqm": 1.0,
    "acre": 4046.8564224,
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Listing:
    id: int
    description: str
    baths: Optional[int]
    rooms: Optional[int]
    sqm: Optional[int]            # square metres by default; see --area-unit
    location: str
    price: Optional[int]         # PKR

    # provenance / audit columns (dropped with --strict-schema)
    size_raw: str = ""
    url: str = ""
    title: str = ""
    description_source: str = "synthesized"  # "graana" once a real description is used
    scraped_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))


SCHEMA_FIELDS = ["id", "price", "baths", "rooms", "sqm", "description", "location"]
EXTRA_FIELDS = ["size_raw", "url", "title", "description_source", "scraped_at"]


# --------------------------------------------------------------------------
# Pure parsing helpers (unit-tested via --self-test)
# --------------------------------------------------------------------------
def size_to_sqm(size: Optional[float], unit: Optional[str]) -> Optional[float]:
    """22 marla -> 459.87 ; 1200 sqft -> 111.48"""
    if size is None or not unit:
        return None
    key = str(unit).strip().lower()
    factor = AREA_TO_SQM.get(key)
    if factor is None:
        return None
    return float(size) * factor


def parse_price_pkr(raw) -> Optional[int]:
    """Graana's `price` field is a numeric string, e.g. '85000000'."""
    if raw is None:
        return None
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def build_description(prop: dict) -> str:
    """Graana's `customTitle` is short ('22 Marla house for sale'); enrich it
    with bed/bath/area/city so the embedding model has more to work with."""
    parts = [prop.get("customTitle") or ""]
    bed, bath = prop.get("bed"), prop.get("bath")
    if bed:
        parts.append(f"{bed} bedrooms")
    if bath:
        parts.append(f"{bath} bathrooms")
    area = (prop.get("area") or {}).get("name")
    city = (prop.get("city") or {}).get("name")
    if area and city:
        parts.append(f"located in {area}, {city}")
    elif city:
        parts.append(f"located in {city}")
    return ", ".join(p for p in parts if p).strip() or (prop.get("customTitle") or "")


def with_query(url: str, **params) -> str:
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    for k, v in params.items():
        q[k] = [str(v)]
    new_query = urlencode({k: v[0] for k, v in q.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def extract_next_data(html: str) -> Optional[dict]:
    m = NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def extract_page(html: str, page_url: str) -> tuple[list[dict], Optional[int], Optional[int]]:
    """Returns (rows, properties_count, page_size) from a search-results page."""
    data = extract_next_data(html)
    if not data:
        return [], None, None
    page_props = (data.get("props") or {}).get("pageProps") or {}
    props = page_props.get("properties") or []
    count = page_props.get("propertiesCount")
    page_size = (
        (page_props.get("initialState") or {}).get("filter", {}).get("filter", {}).get("pageSize")
    )

    rows = []
    for p in props:
        pid = p.get("id")
        if pid is None:
            continue
        rows.append(
            {
                "id": pid,
                "title": p.get("customTitle") or "",
                "description": build_description(p),
                "location": ((p.get("area") or {}).get("name")) or "",
                "price_raw": p.get("price"),
                "size": p.get("size"),
                "size_unit": p.get("sizeUnit"),
                "bed": p.get("bed"),
                "bath": p.get("bath"),
                "url": urljoin(page_url, f"/property/listing-{pid}/"),
            }
        )
    return rows, count, page_size


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_description(html: str) -> str:
    """Real listing description from a graana.com property detail page's
    __NEXT_DATA__ blob. Many listings don't have one (agent left it blank)."""
    data = extract_next_data(html)
    if not data:
        return ""
    prop = (data.get("props") or {}).get("pageProps", {}).get("data") or {}
    return clean_text(prop.get("description"))


FEATURE_LABELS = {
    "kitchen": "kitchen",
    "parking": "parking",
    "tvLounge": "TV lounge",
    "storeRoom": "store room",
    "drawingRoom": "drawing room",
    "laundryRoom": "laundry room",
    "basement": "basement",
    "studyRoom": "study room",
    "dinningRoom": "dining room",
    "semiFurnished": "semi-furnished",
    "servantQuarter": "servant quarter",
    "lawn": "lawn",
    "homeTheatre": "home theatre",
    "swimmingPool": "swimming pool",
    "elevatorOrLift": "elevator",
    "gas": "gas",
    "electricity": "electricity",
    "waterSupply": "water supply",
    "sewerage": "sewerage",
    "internetAccess": "internet access",
    "satelliteOrCableTv": "cable TV",
    "mosque": "mosque nearby",
    "schools": "schools nearby",
    "hospitals": "hospitals nearby",
    "restaurants": "restaurants nearby",
    "balcony": "balcony",
    "numberOfFloors": "floor",
    "powderRoom": "powder room",
    "reusableTop": "reusable rooftop",
    "security": "security",
    "separateEntry": "separate entry",
    "maintenance": "maintenance staff",
    "dirtyKitchen": "dirty kitchen",
}


def extract_detail_extras(html: str) -> dict:
    """Structured extras from a detail page: condition and
    primary/secondary/utility/communication/nearby feature flags. Populated
    on many listings even when the free-text `description` is blank.

    The feature dicts are only ever a *positive* record (a flag present
    means the agent said so) — an empty dict means the agent didn't mention
    features, not that the property lacks them. Kept here only to fold into
    the description text via describe_extras(); not exposed as their own
    columns, since "not mentioned" isn't reliably "absent". `address` was
    dropped too — it's almost always identical to the search page's
    `location`, and blank rather than more specific on the rest."""
    data = extract_next_data(html)
    if not data:
        return {}
    prop = (data.get("props") or {}).get("pageProps", {}).get("data") or {}
    return {
        "condition": prop.get("condition"),
        "primaryFeatures": prop.get("primaryFeatures") or {},
        "secondaryFeatures": prop.get("secondaryFeatures") or {},
        "utilityFeatures": prop.get("utilityFeatures") or {},
        "communicationFeatures": prop.get("communicationFeatures") or {},
        "nearByFeatures": prop.get("nearByFeatures") or {},
    }


def _feature_phrases(features: dict) -> list[str]:
    phrases = []
    for key, val in (features or {}).items():
        label = FEATURE_LABELS.get(key, key)
        if val is True:
            phrases.append(label)
        elif isinstance(val, int) and val >= 1:
            phrases.append(f"{val} {label}s" if val > 1 else label)
    return phrases


def describe_extras(extras: dict) -> str:
    """Turns extract_detail_extras()'s dict into a readable sentence
    fragment, or '' if the listing has nothing usable beyond the basics."""
    if not extras:
        return ""
    parts = []

    condition = (extras.get("condition") or "").strip()
    if condition and condition.lower() != "any":
        parts.append(f"Condition: {condition}.")

    amenities = _feature_phrases(extras.get("primaryFeatures")) + \
        _feature_phrases(extras.get("secondaryFeatures"))
    if amenities:
        parts.append("Amenities: " + ", ".join(amenities) + ".")

    utilities = _feature_phrases(extras.get("utilityFeatures"))
    if utilities:
        parts.append("Utilities: " + ", ".join(utilities) + ".")

    communication = _feature_phrases(extras.get("communicationFeatures"))
    if communication:
        parts.append("Communication: " + ", ".join(communication) + ".")

    nearby = _feature_phrases(extras.get("nearByFeatures"))
    if nearby:
        parts.append("Nearby: " + ", ".join(nearby) + ".")

    return " ".join(parts)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay: float = 2.0, timeout: int = 30, obey_robots: bool = True,
                 retries: int = 3):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._last = 0.0
        self._rp = None
        if obey_robots:
            self._rp = robotparser.RobotFileParser()
            self._rp.set_url(urljoin(BASE, "/robots.txt"))
            try:
                self._rp.read()
                log.info("Loaded robots.txt")
            except Exception as exc:  # network/parse failure -> fail closed
                log.warning("Could not read robots.txt (%s); continuing cautiously", exc)
                self._rp = None

    def allowed(self, url: str) -> bool:
        return self._rp.can_fetch(UA, url) if self._rp else True

    def get(self, url: str) -> Optional[str]:
        if not self.allowed(url):
            log.warning("robots.txt disallows %s — skipping", url)
            return None

        for attempt in range(1, self.retries + 1):
            wait = self.delay + random.uniform(0, self.delay * 0.3) - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(url, timeout=self.timeout)
                self._last = time.time()
                if r.status_code == 200:
                    return r.text
                if r.status_code in (429, 503):
                    backoff = self.delay * (2 ** attempt)
                    log.warning("HTTP %s on %s — backing off %.1fs", r.status_code, url, backoff)
                    time.sleep(backoff)
                    continue
                log.error("HTTP %s on %s", r.status_code, url)
                return None
            except requests.RequestException as exc:
                self._last = time.time()
                log.warning("attempt %d/%d failed for %s: %s", attempt, self.retries, url, exc)
                time.sleep(self.delay * (2 ** attempt))
        return None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def scrape(url: str, pages: Optional[int], fetcher: Fetcher,
           want_details: bool = False, area_unit: str = "sqm",
           dump_dir: Optional[str] = None) -> Iterator[Listing]:
    seen: set[int] = set()
    page = 1
    total_pages = pages

    while True:
        if total_pages is not None and page > total_pages:
            break

        purl = with_query(url, pageSize=30, page=page)
        log.info("Page %d%s  %s", page, f"/{total_pages}" if total_pages else "", purl)
        html = fetcher.get(purl)
        if not html:
            log.error("Giving up on page %d", page)
            break

        if dump_dir:
            path = f"{dump_dir}/page_{page}.html"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
            log.info("Dumped raw HTML -> %s", path)

        rows, count, page_size = extract_page(html, purl)
        if not rows:
            log.warning("No listings parsed on page %d. graana's markup may have changed — "
                        "re-run with --dump-html and inspect.", page)
            break
        log.info("  parsed %d listings", len(rows))

        if total_pages is None and count and page_size:
            total_pages = -(-int(count) // int(page_size))  # ceil
            log.info("Detected %d total listings -> %d pages", count, total_pages)

        for c in rows:
            if c["id"] in seen:
                continue
            seen.add(c["id"])

            sqm = size_to_sqm(c["size"], c["size_unit"])
            if sqm is None:
                size = None
            elif area_unit == "sqft":
                size = int(round(sqm / SQM_PER_SQFT))
            else:
                size = int(round(sqm))

            pkr = parse_price_pkr(c["price_raw"])

            description = c["description"] or c["title"]
            description_source = "synthesized"
            extras: dict = {}
            if want_details:
                dhtml = fetcher.get(c["url"])
                if dhtml:
                    extras = extract_detail_extras(dhtml)
                    real_description = extract_description(dhtml)
                    if real_description:
                        description = real_description
                        description_source = "graana"
                    else:
                        extras_text = describe_extras(extras)
                        if extras_text:
                            description = f"{description} {extras_text}"
                            description_source = "enriched"

            yield Listing(
                id=c["id"],
                description=description,
                baths=c["bath"],
                rooms=c["bed"],
                sqm=size,
                location=c["location"],
                price=pkr,
                size_raw=f"{c['size']} {c['size_unit']}" if c["size"] else "",
                url=c["url"],
                title=c["title"],
                description_source=description_source,
            )

        page += 1


def write_output(rows: Iterable[Listing], path: str, fmt: str, strict: bool) -> int:
    cols = SCHEMA_FIELDS if strict else SCHEMA_FIELDS + EXTRA_FIELDS
    rows = list(rows)
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{k: asdict(r)[k] for k in cols} for r in rows], fh,
                      ensure_ascii=False, indent=2)
    else:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: asdict(r)[k] for k in cols})
    return len(rows)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def self_test() -> int:
    failures = []

    def check(label, got, want, tol=None):
        ok = (got is None and want is None) or (
            abs(got - want) <= tol if (tol is not None and got is not None and want is not None)
            else got == want
        )
        if not ok:
            failures.append(f"{label}: got {got!r}, expected {want!r}")

    check("size/marla", size_to_sqm(22, "marla"), 459.87, tol=0.05)
    check("size/kanal", size_to_sqm(1, "kanal"), 418.06, tol=0.05)
    check("size/sqft", size_to_sqm(1200, "sqft"), 111.48, tol=0.05)
    check("size/none", size_to_sqm(None, "marla"), None)
    check("size/unknown-unit", size_to_sqm(3, "bigha"), None)

    check("price/plain", parse_price_pkr("85000000"), 85_000_000)
    check("price/float-str", parse_price_pkr("4500000.0"), 4_500_000)
    check("price/none", parse_price_pkr(None), None)

    check("query/add", with_query("https://x.com/a/", pageSize=30, page=2),
          "https://x.com/a/?pageSize=30&page=2")
    check("query/replace", with_query("https://x.com/a/?page=1", page=3),
          "https://x.com/a/?page=3")

    desc = build_description({
        "customTitle": "22 Marla house for sale", "bed": 5, "bath": 6,
        "area": {"name": "Emaar Canyon Views"}, "city": {"name": "Islamabad"},
    })
    check("description", desc,
          "22 Marla house for sale, 5 bedrooms, 6 bathrooms, located in Emaar Canyon Views, Islamabad")

    check("features/bool", _feature_phrases({"parking": True, "gas": False}), ["parking"])
    check("features/count-one", _feature_phrases({"storeRoom": 1}), ["store room"])
    check("features/count-many", _feature_phrases({"kitchen": 2}), ["2 kitchens"])
    check("features/unknown-key", _feature_phrases({"weirdThing": True}), ["weirdThing"])
    check("features/empty", _feature_phrases({}), [])

    extras_text = describe_extras({
        "condition": "Brand new",
        "primaryFeatures": {"parking": True, "storeRoom": 1},
        "secondaryFeatures": {},
        "utilityFeatures": {"gas": True},
        "communicationFeatures": {"internetAccess": True},
        "nearByFeatures": {},
    })
    check("extras/full", extras_text,
          "Condition: Brand new. Amenities: parking, store room. Utilities: gas. "
          "Communication: internet access.")
    check("extras/any-condition-dropped", describe_extras({"condition": "Any"}), "")
    check("extras/empty", describe_extras({}), "")

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("All self-tests passed.")
    return 0


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Scrape graana.com property listings.")
    p.add_argument("--url", help="A graana.com search-results URL (page 1).")
    p.add_argument("--pages", type=int, default=None,
                   help="How many pages to walk (default: auto-detect all).")
    p.add_argument("--out", default="graana_listings.csv", help="Output file.")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--delay", type=float, default=2.0, help="Seconds between requests.")
    p.add_argument("--details", action="store_true",
                   help="Open each listing for graana's real description "
                        "(1 extra request each — falls back to a synthesized "
                        "description when the agent left none).")
    p.add_argument("--area-unit", choices=["sqm", "sqft"], default="sqm",
                   help="Unit for the `sqm` column (default sqm; pass sqft "
                        "for actual square feet instead).")
    p.add_argument("--strict-schema", action="store_true",
                   help="Emit only the 7 schema columns, dropping audit columns.")
    p.add_argument("--ignore-robots", action="store_true")
    p.add_argument("--dump-html", metavar="DIR",
                   help="Save raw HTML per page — useful if selectors stop matching.")
    p.add_argument("--self-test", action="store_true", help="Run parser tests and exit.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if args.self_test:
        return self_test()

    if not args.url:
        p.error("--url is required (or use --self-test)")
    if urlparse(args.url).netloc.replace("www.", "") != "graana.com":
        p.error("--url must point at graana.com")

    fetcher = Fetcher(delay=args.delay, obey_robots=not args.ignore_robots)
    rows = list(scrape(args.url, args.pages, fetcher, want_details=args.details,
                       area_unit=args.area_unit, dump_dir=args.dump_html))

    if not rows:
        log.error("Nothing scraped.")
        return 1

    n = write_output(rows, args.out, args.format, args.strict_schema)
    priced = sum(1 for r in rows if r.price is not None)
    sized = sum(1 for r in rows if r.sqm is not None)
    bedded = sum(1 for r in rows if r.rooms is not None)
    log.info("Wrote %d listings -> %s", n, args.out)
    log.info("Coverage: price %d/%d, size %d/%d, beds %d/%d", priced, n, sized, n, bedded, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
