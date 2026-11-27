"""Shared Gmail (IMAP) reading for the alert-email sources (Finn, LinkedIn), and the area filter
for their free-text locations. Mail is opened read-only: nothing is marked as read."""
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

from . import db, profiles

IMAP_HOST = "imap.gmail.com"
DEFAULT_LOOKBACK_DAYS = 3
MAX_LOOKBACK_DAYS = 14


def credentials():
    addr, pwd = os.getenv("GMAIL_ADDRESS", "").strip(), os.getenv("GMAIL_APP_PASSWORD", "").strip()
    return (addr, pwd) if addr and pwd else None


def since_for(con, meta_key: str, fallback_key: str | None = None) -> str:
    """IMAP date to search from: a few days back, or back to the last success after missed runs."""
    days = DEFAULT_LOOKBACK_DAYS
    last = db.get_meta(con, meta_key) or (db.get_meta(con, fallback_key) if fallback_key else None)
    if last:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days + 2
    return (datetime.now(timezone.utc) - timedelta(days=min(days, MAX_LOOKBACK_DAYS))).strftime("%d-%b-%Y")


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")


def text_of(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def alert_texts(sender: str, box: str, since: str) -> list[str]:
    """Plain-text bodies of the emails from `sender` since `since`, read-only."""
    creds = credentials()
    if not creds:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must both be set")
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        try:
            m.login(*creds)
        except imaplib.IMAP4.error:
            raise RuntimeError("Gmail refused the login: check the address and the app password") from None
        typ, _ = m.select(f'"{box}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Gmail label not found: {box}")
        typ, data = m.search(None, f'(FROM "{sender}" SINCE "{since}")')
        texts = []
        for num in data[0].split():
            typ, msgdata = m.fetch(num, "(BODY.PEEK[])")
            texts.append(text_of(message_from_bytes(msgdata[0][1])))
        return texts
    finally:
        try:
            m.logout()
        except Exception:
            pass


def by_mailbox(profile_list, attr: str) -> dict:
    """{mailbox: [profiles reading it]} for the given mailbox attribute (finn_mailbox...)."""
    out = {}
    for p in profile_list:
        out.setdefault(getattr(p, attr), []).append(p)
    return out


# ---------- area filter for free-text locations ----------

def _place_matches(part: str, name: str) -> bool:
    """`part` is the place `name` itself, or `name` followed by a qualifier ("Oslo sentrum",
    "Nes på Romerike"). Not "<name> i <somewhere>": that is another place with the same
    first word (e.g. "Nes i Ådal" in Buskerud is not Nes in Akershus)."""
    if part == name:
        return True
    if not part.startswith(name + " "):
        return False
    return not part[len(name) + 1:].startswith("I ")


def in_target(location: str, profile=None) -> bool:
    """Unknown location: keep it (the user decides). Otherwise one of its parts must be a
    target municipality or county of the profile (default: the current one)."""
    if not location:
        return True
    p = profile or profiles.current()
    names = p.target_municipalities | p.target_counties
    parts = [p.strip() for p in re.split(r"[,/;]| OG ", location.upper()) if p.strip()]
    return any(_place_matches(p, n) for p in parts for n in names)
