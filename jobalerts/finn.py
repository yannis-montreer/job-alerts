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
import json
import logging
import re
import sys
import time

import requests

from . import config, db, mail, profiles
from .mail import in_target  # noqa: F401  (also used by tests)

log = logging.getLogger(__name__)

ALERT_SENDER = "agent@finn.no"
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


def check_connection(days: int = 14, profile=None) -> dict:
    """Setup check: count Finn alert emails and ads of the last `days` days, for a profile."""
    p = profile or profiles.current()
    box = p.finn_mailbox
    found = mail.alert_messages(ALERT_SENDER, box, mail.days_ago(days))
    texts = [text for subject, text in found if p.wants_finn_email(subject)]
    ads = [ad for text in texts for ad in parse_alert_text(text)]
    return dict(mailbox=box, days=days, emails=len(texts), other=len(found) - len(texts), ads=len(ads),
                in_area=sum(in_target(ad["location"], p) for ad in ads), subject=p.finn_subject)


def fetch_new_ads(con, profile_list=None):
    """Read each profile's Finn mailbox (label); hand each ad to the profiles reading that
    mailbox whose area it is in. An ad already known is still handed to a profile that does
    not have it yet (e.g. the same ad in two profiles' alerts)."""
    if not config.SOURCE_FINN:
        return [], {}
    if not mail.credentials():
        log.info("Finn: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set, source skipped")
        return [], {}
    stats = dict(emails=0, other_alerts=0, ads=0, out_of_area=0, seen=0, new=0)
    new_ads = []
    for box, group in mail.by_mailbox(profile_list or profiles.all_profiles(), "finn_mailbox").items():
        key = f"finn_last_success:{box}"
        for subject, text in mail.alert_messages(ALERT_SENDER, box, mail.since_for(con, key, "finn_last_success")):
            readers = [p for p in group if p.wants_finn_email(subject)]  # whose alert this is
            if not readers:
                stats["other_alerts"] += 1
                continue
            stats["emails"] += 1
            for ad in parse_alert_text(text):
                stats["ads"] += 1
                uuid = f"finn:{ad['code']}"
                wanted = [p for p in readers if in_target(ad["location"], p)]
                if db.is_seen(con, uuid):
                    stats["seen"] += 1
                    for p in wanted:
                        db.add_match(con, p.id, uuid)
                    continue
                if not wanted:
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
                for p in wanted:
                    db.add_match(con, p.id, uuid)
        db.set_meta(con, key, db.now_iso())
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
    con.execute("UPDATE matches SET summary=NULL WHERE uuid LIKE 'finn:%' AND summary IS NOT NULL")
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
