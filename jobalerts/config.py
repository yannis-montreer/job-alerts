"""Settings. Personal values live in .env (git-ignored); see .env.example."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "jobs.db"
LOG_DIR = ROOT / "logs"
PROFILE_PATH = ROOT / "profile.md"  # your search profile, copied from profile.example.md

# Who the ads are for, and the language of summaries and Telegram messages ("en" or "fr").
CANDIDATE_NAME = os.getenv("CANDIDATE_NAME", "").strip() or "the candidate"
LANGUAGE = (os.getenv("LANGUAGE", "en").strip().lower() or "en")

def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(os.getenv(name, "").strip() or default)))
    except ValueError:
        return default


# What reaches Telegram:
# - ads scored MESSAGE_MIN_SCORE or more: one message each, with Keep / Reject buttons
# - ads scored DIGEST_MIN_SCORE up to MESSAGE_MIN_SCORE - 1: one short daily list
# - NORWEGIAN_REQUIRED=reject drops ads that require Norwegian; "flag" sends them with a flag
MESSAGE_MIN_SCORE = _env_int("MESSAGE_MIN_SCORE", 4, 1, 5)
DIGEST_MIN_SCORE = _env_int("DIGEST_MIN_SCORE", 3, 1, 5)
NORWEGIAN_REQUIRED = "reject" if os.getenv("NORWEGIAN_REQUIRED", "").strip().lower() == "reject" else "flag"

# AI used to sort the ads: "claude" (Claude Code CLI, subscription login) or "gemini" (API key).
AI_PROVIDER = (os.getenv("AI_PROVIDER", "claude").strip().lower() or "claude")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-flash-latest"

# Finn ad pages: OFF by default. Finn's terms forbid automated access without written
# permission; turn this on only if Finn gave YOU written permission (see README).
FINN_FETCH_PAGES = os.getenv("FINN_FETCH_PAGES", "").strip().lower() in ("1", "true", "yes", "on")
FINN_CONTACT_EMAIL = os.getenv("FINN_CONTACT_EMAIL", "").strip()  # sent in the User-Agent, as Finn asks
FINN_DELAY_S = 5          # between two page requests (Finn asked for at least 2-5 s)
FINN_MAX_PAGES_PER_RUN = 30

NAV_BASE = "https://pam-stilling-feed.nav.no"
NAV_TOKEN = os.getenv("NAV_TOKEN", "").strip()

# Search window: 24 h, extended after missed runs (capped at 7 days).
WINDOW_HOURS = 24
MAX_CATCHUP_HOURS = 7 * 24


def _env_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


# Default area: Oslo + Akershus (19 municipalities, 2024 map) + a few neighbours.
# Override with TARGET_MUNICIPALITIES / TARGET_COUNTIES in .env (comma-separated, any case).
AKERSHUS = {
    "ASKER", "BÆRUM", "AURSKOG-HØLAND", "FROGN", "NESODDEN", "NORDRE FOLLO", "ÅS",
    "VESTBY", "ENEBAKK", "LILLESTRØM", "RÆLINGEN", "LØRENSKOG", "NITTEDAL",
    "GJERDRUM", "NES", "ULLENSAKER", "EIDSVOLL", "NANNESTAD", "HURDAL",
}
NEARBY = {"DRAMMEN", "LIER", "MOSS"}
TARGET_MUNICIPALITIES = _env_set("TARGET_MUNICIPALITIES", {"OSLO"} | AKERSHUS | NEARBY)
TARGET_COUNTIES = _env_set("TARGET_COUNTIES", {"OSLO", "AKERSHUS"})
