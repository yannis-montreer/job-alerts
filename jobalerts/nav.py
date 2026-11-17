"""Public Arbeidsplassen/NAV job feed (pam-stilling-feed).

Docs: https://github.com/navikt/pam-stilling-feed (docs/index.markdown)
- GET {NAV_BASE}/api/v1/feed with Authorization: Bearer <JWT> and If-Modified-Since
- pages are chained by next_url; the feed ends when next_url is null
- each item has _feed_entry (status, municipality) and a detail url -> ad_content

Run alone to test the source:  python -m jobalerts.nav
"""
import html
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import requests

from . import config, db

log = logging.getLogger(__name__)
TIMEOUT = 30


def get_token() -> str:
    if config.NAV_TOKEN:
        return config.NAV_TOKEN
    r = requests.get(f"{config.NAV_BASE}/api/publicToken", timeout=TIMEOUT)
    r.raise_for_status()
    # The response is "Current public token ...:\n<jwt>": keep the last line.
    token = r.text.strip().splitlines()[-1].strip()
    if token.count(".") != 2:
        raise RuntimeError("NAV public token not found in the response")
    return token


def compute_since(con) -> datetime:
    """Start of the search window: last 24 h, or back to the last success after missed runs."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=config.WINDOW_HOURS)
    last = db.get_meta(con, "nav_last_success")
    if last:  # missed run(s): go back to the last success, with a 1 h margin
        since = min(since, datetime.fromisoformat(last) - timedelta(hours=1))
    return max(since, now - timedelta(hours=config.MAX_CATCHUP_HOURS))


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


def is_target_location(entry: dict) -> bool:
    muni = (entry.get("municipal") or "").strip().upper()
    # "?" or empty: unknown municipality, decided from the detail (county).
    return muni in config.TARGET_MUNICIPALITIES or muni in ("", "?")


def detail_in_target(ad: dict) -> bool:
    for loc in ad.get("workLocations") or []:
        if (loc.get("municipal") or "").upper() in config.TARGET_MUNICIPALITIES:
            return True
        if (loc.get("county") or "").upper() in config.TARGET_COUNTIES:
            return True
    return False


class Feed:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {get_token()}", "Accept": "application/json"}
        )

    def _get(self, path, **headers):
        r = self.s.get(config.NAV_BASE + path, headers=headers, timeout=TIMEOUT)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.json()

    def items_since(self, since: datetime):
        """Walk the feed from `since` and yield items (ad headers)."""
        ims = format_datetime(since.astimezone(timezone.utc), usegmt=True)
        path = "/api/v1/feed"
        pages = 0
        while path:
            page = self._get(path, **{"If-Modified-Since": ims})
            pages += 1
            if page is None:
                break
            yield from page.get("items", [])
            path = page.get("next_url")
        log.info("NAV feed: %d page(s) read", pages)

    def detail(self, url: str):
        """Ad detail, or None if unavailable (ad withdrawn in the meantime)."""
        try:
            data = self._get(url)
        except requests.HTTPError as e:
            log.warning("Detail %s: HTTP %s", url, e.response.status_code)
            return None
        ad = (data or {}).get("ad_content")
        if not ad:
            log.warning("Detail %s: no ad_content (keys: %s)", url, list((data or {}).keys()))
        return ad


def fetch_new_ads(con, since: datetime):
    """Return active, in-area, never-seen ads with their description."""
    feed = Feed()
    stats = dict(items=0, old=0, inactive=0, out_of_area=0, seen=0, new=0)
    new_ads = []
    for item in feed.items_since(since):
        stats["items"] += 1
        entry = item.get("_feed_entry") or {}
        modified = item.get("date_modified") or entry.get("sistEndret")
        if modified and datetime.fromisoformat(modified) < since:
            stats["old"] += 1
            continue
        if entry.get("status") != "ACTIVE":
            stats["inactive"] += 1
            continue
        if not is_target_location(entry):
            stats["out_of_area"] += 1
            continue
        uuid = entry.get("uuid") or item["id"]
        if db.is_seen(con, uuid):
            stats["seen"] += 1
            continue
        ad = feed.detail(item["url"])
        if ad is None:  # not stored: retried at the next run
            stats["detail_unavailable"] = stats.get("detail_unavailable", 0) + 1
            continue
        if not detail_in_target(ad):
            stats["out_of_area"] += 1
            continue
        record = dict(
            uuid=uuid,
            date_modified=modified,
            title=ad.get("title") or entry.get("title"),
            employer=(ad.get("employer") or {}).get("name") or entry.get("businessName"),
            municipal=entry.get("municipal"),
            url=ad.get("link") or ad.get("sourceurl")
            or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
            application_url=ad.get("applicationUrl"),
            description=html_to_text(ad.get("description")),
            source="nav",
        )
        if db.insert_ad(con, record):
            stats["new"] += 1
            new_ads.append(record)
    log.info("NAV: %s", stats)
    return new_ads, stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    con = db.connect()
    since = compute_since(con)
    logging.info("Window since %s", since.isoformat())
    new_ads, _ = fetch_new_ads(con, since)
    db.set_meta(con, "nav_last_success", db.now_iso())
    for ad in new_ads:
        print(f"- {ad['title']} | {ad['employer']} | {ad['municipal']}")
    print(f"{len(new_ads)} new ad(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
