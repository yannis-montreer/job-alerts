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
