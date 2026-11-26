"""Finn.no via the alert emails ("Søkeagent") read from Gmail over IMAP.

Respecting Finn's terms:
- By default, only the alert emails received in your own mailbox are read and no finn.no page
  is downloaded (Finn's robots.txt and terms forbid automated access without written
  permission). Only title, employer, location and link are then known.
- With FINN_FETCH_PAGES=true (only if Finn gave YOU written permission), each new ad page linked
  from the alert emails is downloaded once, during the daily run, at least FINN_DELAY_S seconds
  apart, with a User-Agent that names the tool and FINN_CONTACT_EMAIL. Only the description is
  kept, in the local database, never shared.

Delete the stored Finn descriptions when the job search ends:  python -m jobalerts.finn --purge
"""
import html
import imaplib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

import requests

from . import config, db

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
ALERT_SENDER = "agent@finn.no"
DEFAULT_LOOKBACK_DAYS = 3
MAX_LOOKBACK_DAYS = 14
LIMITED_INFO = (
    "(Finn ad: the description is not available. Only the title, employer and location are "
    "known, from the alert email.)"
)

DETAILS_RE = re.compile(r"Flere detaljer:\s*https?://www\.finn\.no/(?:job/ad/)?(\d+)")


def parse_alert_text(text: str) -> list[dict]:
    """Extract (code, title, employer, location) from the text of an alert email.

    Observed format: one block per ad = title line, "Employer, Location" line,
    then "Flere detaljer: https://www.finn.no/<code>?...".
    """
    lines = [l.strip() for l in text.splitlines()]
    ads = []
    for i, line in enumerate(lines):
        m = DETAILS_RE.search(line)
        if not m:
            continue
        prev = [l for l in lines[:i] if l][-2:]
        if len(prev) < 2:
            continue
        title, meta = prev
        employer, _, location = meta.rpartition(",")
        if not employer:  # no comma: everything is the employer, location unknown
            employer, location = meta, ""
        ads.append(dict(code=m.group(1), title=title, employer=employer.strip(), location=location.strip()))
    return ads


def _place_matches(part: str, name: str) -> bool:
    """`part` is the place `name` itself, or `name` followed by a qualifier ("Oslo sentrum",
    "Nes på Romerike"). Not "<name> i <somewhere>": that is another place with the same
    first word (e.g. "Nes i Ådal" in Buskerud is not Nes in Akershus)."""
    if part == name:
        return True
    if not part.startswith(name + " "):
        return False
    return not part[len(name) + 1:].startswith("I ")


def in_target(location: str) -> bool:
    """Unknown location: keep it (the user decides). Otherwise one of its parts must be a
    target municipality or county."""
    if not location:
        return True
    names = config.TARGET_MUNICIPALITIES | config.TARGET_COUNTIES
    parts = [p.strip() for p in re.split(r"[,/;]| OG ", location.upper()) if p.strip()]
    return any(_place_matches(p, n) for p in parts for n in names)


def _text_of(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def check_connection(days: int = 14) -> dict:
    """Setup check: log in, open the label, count Finn alert emails and ads of the last `days` days."""
    addr, pwd = os.getenv("GMAIL_ADDRESS", "").strip(), os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if not (addr and pwd):
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must both be set")
    mailbox = os.getenv("GMAIL_FINN_LABEL", "INBOX").strip() or "INBOX"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        try:
            m.login(addr, pwd)
        except imaplib.IMAP4.error:
            raise RuntimeError("Gmail refused the login: check the address and the app password") from None
        typ, _ = m.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Gmail label not found: {mailbox}")
        typ, data = m.search(None, f'(FROM "{ALERT_SENDER}" SINCE "{since}")')
        nums = data[0].split()
        ads = in_area = 0
        for num in nums:
            typ, msgdata = m.fetch(num, "(BODY.PEEK[])")
            for ad in parse_alert_text(_text_of(message_from_bytes(msgdata[0][1]))):
                ads += 1
                in_area += in_target(ad["location"])
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return dict(mailbox=mailbox, days=days, emails=len(nums), ads=ads, in_area=in_area)


def fetch_new_ads(con):
    addr, pwd = os.getenv("GMAIL_ADDRESS", "").strip(), os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if not (addr and pwd):
        log.info("Finn: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set, source skipped")
        return [], {}
    mailbox = os.getenv("GMAIL_FINN_LABEL", "INBOX").strip() or "INBOX"
    last = db.get_meta(con, "finn_last_success")
    days = DEFAULT_LOOKBACK_DAYS
    if last:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days + 2
    since = (datetime.now(timezone.utc) - timedelta(days=min(days, MAX_LOOKBACK_DAYS))).strftime("%d-%b-%Y")

    stats = dict(emails=0, ads=0, out_of_area=0, seen=0, new=0)
    new_ads = []
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        m.login(addr, pwd)
        typ, _ = m.select(f'"{mailbox}"', readonly=True)  # read-only: nothing is marked as read
        if typ != "OK":
            raise RuntimeError(f"Gmail label not found: {mailbox}")
        typ, data = m.search(None, f'(FROM "{ALERT_SENDER}" SINCE "{since}")')
        for num in data[0].split():
            typ, msgdata = m.fetch(num, "(BODY.PEEK[])")
            stats["emails"] += 1
            for ad in parse_alert_text(_text_of(message_from_bytes(msgdata[0][1]))):
                stats["ads"] += 1
                uuid = f"finn:{ad['code']}"
                if db.is_seen(con, uuid):
                    stats["seen"] += 1
                    continue
                if not in_target(ad["location"]):
                    stats["out_of_area"] += 1
                    continue
                record = dict(
                    uuid=uuid, date_modified=None, title=ad["title"], employer=ad["employer"],
                    municipal=ad["location"].upper() or None,
                    url=f"https://www.finn.no/{ad['code']}", application_url=None,
                    description=LIMITED_INFO, source="finn",
                )
                if db.insert_ad(con, record):
                    stats["new"] += 1
                    new_ads.append(record)
    finally:
        try:
            m.logout()
        except Exception:
            pass
    db.set_meta(con, "finn_last_success", db.now_iso())
    if config.FINN_FETCH_PAGES and new_ads:
        stats["pages"] = fetch_descriptions(con, new_ads)
    log.info("Finn: %s", stats)
    return new_ads, stats


# ---------- ad pages (only with Finn's written permission, FINN_FETCH_PAGES=true) ----------

def user_agent() -> str:
    if not config.FINN_CONTACT_EMAIL:
        raise RuntimeError("FINN_CONTACT_EMAIL is required to fetch Finn pages (Finn asks for a contact in the User-Agent)")
    return f"job-alerts/1.0 (personal job search tool; contact: {config.FINN_CONTACT_EMAIL})"


def _job_postings(node, depth=0):
    """Yield every schema.org JobPosting found in a JSON-LD structure, at any nesting
    (Finn wraps it: {"script:ld+json": {"@type": "JobPosting", ...}})."""
    if depth > 6:
        return
    if isinstance(node, list):
        for x in node:
            yield from _job_postings(x, depth + 1)
    elif isinstance(node, dict):
        types = node.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            yield node
            return
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _job_postings(value, depth + 1)


def extract_description(page_html: str) -> str | None:
    """The ad description from a Finn job page: the schema.org JobPosting embedded as JSON-LD."""
    from .nav import html_to_text
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                          page_html, flags=re.S | re.I):
        data = None
        for candidate in (raw, html.unescape(raw)):
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                continue
        if data is None:
            continue
        for posting in _job_postings(data):
            text = html_to_text(posting.get("description") or "")
            if len(text) > 50:
                return text
    return None


def page_url(ad) -> str:
    """The ad page itself. The alert link (finn.no/<code>) only redirects here, so asking for
    this address directly makes one request per ad instead of two."""
    return f"https://www.finn.no/job/ad/{ad['uuid'].removeprefix('finn:')}"


def fetch_descriptions(con, ads: list[dict]) -> dict:
    """Download each new ad page once, slowly, and store its description. Failures keep the preview."""
    stats = dict(fetched=0, failed=0, skipped=0)
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent(), "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8"})
    for i, ad in enumerate(ads):
        if i >= config.FINN_MAX_PAGES_PER_RUN:
            stats["skipped"] += 1
            continue
        if i:
            time.sleep(config.FINN_DELAY_S)
        try:
            r = session.get(page_url(ad), timeout=30)
            r.raise_for_status()
            text = extract_description(r.text)
        except requests.RequestException as e:
            log.warning("Finn page %s: %s", page_url(ad), e)
            text = None
        if not text:
            stats["failed"] += 1
            continue
        con.execute("UPDATE ads SET description=? WHERE uuid=?", (text, ad["uuid"]))
        con.commit()
        ad["description"] = text
        stats["fetched"] += 1
    return stats


def has_description(ad) -> bool:
    return ad["description"] not in (None, "", LIMITED_INFO)


def purge(con) -> int:
    """Delete stored Finn descriptions and summaries (to do when the job search ends)."""
    cur = con.execute(
        "UPDATE ads SET description=?, summary_fr=NULL WHERE source='finn' "
        "AND (description IS NOT ? OR summary_fr IS NOT NULL)", (LIMITED_INFO, LIMITED_INFO))
    con.commit()
    return cur.rowcount


def main() -> int:
    if "--purge" in sys.argv:
        n = purge(db.connect())
        print(f"Finn: descriptions and summaries deleted for {n} ad(s).")
        return 0
    print("Usage: python -m jobalerts.finn --purge")
    return 1


if __name__ == "__main__":
    sys.exit(main())
