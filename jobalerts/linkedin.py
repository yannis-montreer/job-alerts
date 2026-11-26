"""LinkedIn via its job alert emails, read from Gmail over IMAP (SOURCE_LINKEDIN=true).

Only the alert emails received in your own mailbox are read. No linkedin.com page is ever
downloaded: LinkedIn's terms forbid scraping. Only title, company, location and link are known,
so these ads are sorted on that and arrive with a "limited preview" note.

The parser follows the usual plain-text layout of LinkedIn job alerts: for each job, a few lines
(title, company, location, sometimes badges) and a link to linkedin.com/.../jobs/view/<id>.
"""
import logging
import re

from . import config, db, mail
from .mail import in_target

log = logging.getLogger(__name__)

ALERT_SENDER = "jobalerts-noreply@linkedin.com"
LIMITED_INFO = (
    "(LinkedIn ad: the description is not available. Only the title, company and location are "
    "known, from the alert email.)"
)
JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
NOISE_RE = re.compile(
    r"https?://|^view job|^see all jobs|apply|applicant|connection|alumni|recruiting|promoted|"
    r"^[-=_*·•\s]+$|^\d+\s+(?:new\s+)?jobs?\b|job alert|^unsubscribe|^manage", re.I)
COUNTRY_RE = re.compile(r",?\s*(?:Norway|Norge|Noreg)\s*$", re.I)


def parse_alert_text(text: str) -> list[dict]:
    """Extract (id, title, company, location) for each job of a LinkedIn alert email."""
    lines = [l.strip() for l in text.splitlines()]
    ads, seen, block = [], set(), []
    for line in lines:
        m = JOB_RE.search(line)
        if not m:
            if line and not NOISE_RE.search(line):
                block.append(line)
            elif line and re.match(r"^[-=_]{5,}$", line):
                block = []  # separator between jobs
            continue
        job_id = m.group(1)
        if job_id not in seen and len(block) >= 2:
            if len(block) >= 3:
                title, company, location = block[-3], block[-2], block[-1]
            else:
                title, company, location = block[-2], block[-1], ""
            seen.add(job_id)
            ads.append(dict(id=job_id, title=title, company=company, location=location))
        block = []
    return ads


def clean_location(location: str) -> str:
    """"Oslo, Oslo, Norway" -> "Oslo, Oslo"; "Norway" alone (remote) -> "" (unknown, kept)."""
    loc = re.sub(r"\s*\((?:hybrid|on-site|remote|hybrid work)\)\s*$", "", location or "", flags=re.I)
    return COUNTRY_RE.sub("", loc).strip(" ,")


def check_connection(days: int = 14) -> dict:
    box = mail.mailbox("GMAIL_LINKEDIN_LABEL")
    texts = mail.alert_texts(ALERT_SENDER, box, mail.days_ago(days))
    ads = [ad for text in texts for ad in parse_alert_text(text)]
    return dict(mailbox=box, days=days, emails=len(texts), ads=len(ads),
                in_area=sum(in_target(clean_location(ad["location"])) for ad in ads))


def fetch_new_ads(con):
    if not config.SOURCE_LINKEDIN:
        return [], {}
    if not mail.credentials():
        log.info("LinkedIn: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set, source skipped")
        return [], {}
    texts = mail.alert_texts(ALERT_SENDER, mail.mailbox("GMAIL_LINKEDIN_LABEL"),
                             mail.since_for(con, "linkedin_last_success"))
    stats = dict(emails=len(texts), ads=0, out_of_area=0, seen=0, new=0)
    new_ads = []
    for text in texts:
        for ad in parse_alert_text(text):
            stats["ads"] += 1
            uuid = f"linkedin:{ad['id']}"
            location = clean_location(ad["location"])
            if db.is_seen(con, uuid):
                stats["seen"] += 1
                continue
            if not in_target(location):
                stats["out_of_area"] += 1
                continue
            record = dict(
                uuid=uuid, date_modified=None, title=ad["title"], employer=ad["company"],
                municipal=location.upper() or None,
                url=f"https://www.linkedin.com/jobs/view/{ad['id']}/", application_url=None,
                description=LIMITED_INFO, source="linkedin",
            )
            if db.insert_ad(con, record):
                stats["new"] += 1
                new_ads.append(record)
    db.set_meta(con, "linkedin_last_success", db.now_iso())
    log.info("LinkedIn: %s", stats)
    return new_ads, stats
