"""Profiles (one per candidate or alert) and Telegram bots, stored in profiles.json (git-ignored).

A profile has its own candidate name, profile text, preferences, area, Telegram settings,
Gmail labels for the alert emails, and the bot that sends its ads. Bots are listed apart
(name, chat id) so several profiles can share one; their tokens stay in .env.

Without profiles.json, a single profile "p1" and a single bot "b1" are built from .env
(CANDIDATE_NAME, LANGUAGE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID...): the setup before profiles.

The profile being worked on is per thread (profiles.use(p)), so the Telegram listener can
serve several bots at once and the dashboard can show any profile.
"""
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

PATH = config.ROOT / "profiles.json"
PROFILES_DIR = config.ROOT / "profiles"
DEFAULT_PROFILE_ID = "p1"
DEFAULT_BOT_ID = "b1"
ID_RE = re.compile(r"[pb]\d{1,3}")


@dataclass
class Bot:
    id: str
    name: str = ""
    chat_id: str = ""
    token_env: str = ""
    owner: str = ""  # "" = the admin's bot; otherwise the id of the user who added it

    @property
    def admin_owned(self) -> bool:
        return not self.owner or self.owner == "admin"

    @property
    def token(self) -> str:
        return os.getenv(self.token_env, "").strip()

    def public(self) -> dict:
        return dict(id=self.id, name=self.name, chat_id=self.chat_id, token_set=bool(self.token),
                    owner=self.owner or "admin")


@dataclass
class Profile:
    id: str
    name: str = ""
    candidate: str = ""
    language: str = "en"
    message_min: int = 4
    digest_min: int = 3
    norwegian: str = "flag"
    municipalities: list = field(default_factory=list)  # empty = default area
    counties: list = field(default_factory=list)
    finn_label: str = ""
    linkedin_label: str = ""
    finn_subject: str = ""      # only the Finn alert emails whose subject contains this ("" = all)
    bot: str = DEFAULT_BOT_ID
    send_time: str = ""         # "HH:MM" the profile's ads are sent every day ("" = not set yet)
    profile_file: str = ""      # relative to the project folder
    preferences_file: str = ""

    @property
    def candidate_name(self) -> str:
        return self.candidate.strip() or "the candidate"

    @property
    def profile_path(self) -> Path:
        return config.ROOT / self.profile_file if self.profile_file else PROFILES_DIR / self.id / "profile.md"

    @property
    def preferences_path(self) -> Path:
        return config.ROOT / self.preferences_file if self.preferences_file else PROFILES_DIR / self.id / "preferences.json"

    @property
    def target_municipalities(self) -> set:
        return {m.upper() for m in self.municipalities} or ({"OSLO"} | config.AKERSHUS | config.NEARBY)

    @property
    def target_counties(self) -> set:
        return {c.upper() for c in self.counties} or {"OSLO", "AKERSHUS"}

    @property
    def finn_mailbox(self) -> str:
        return self.finn_label.strip() or "INBOX"

    def wants_finn_email(self, subject: str) -> bool:
        """This profile's Finn alert, by its subject (e.g. "Nye annonser: Job alert Anna")."""
        f = " ".join(self.finn_subject.split()).casefold()
        return not f or f in " ".join((subject or "").split()).casefold()

    @property
    def linkedin_mailbox(self) -> str:
        return self.linkedin_label.strip() or "INBOX"


# ---------- loading and saving ----------

def _rel(path) -> str:
    """Path relative to the project folder, or absolute when it is on another drive."""
    try:
        return os.path.relpath(path, config.ROOT)
    except ValueError:
        return str(path)


def _legacy() -> dict:
    """The single profile and bot of a setup without profiles.json, from .env (read now)."""
    from . import preferences
    p = Profile(
        id=DEFAULT_PROFILE_ID, name=config.CANDIDATE_NAME if config.CANDIDATE_NAME != "the candidate" else "",
        candidate=config.CANDIDATE_NAME if config.CANDIDATE_NAME != "the candidate" else "",
        language=config.LANGUAGE, message_min=config.MESSAGE_MIN_SCORE, digest_min=config.DIGEST_MIN_SCORE,
        norwegian=config.NORWEGIAN_REQUIRED,
        municipalities=sorted(config.TARGET_MUNICIPALITIES), counties=sorted(config.TARGET_COUNTIES),
        finn_label=os.getenv("GMAIL_FINN_LABEL", "").strip(), linkedin_label=os.getenv("GMAIL_LINKEDIN_LABEL", "").strip(),
        bot=DEFAULT_BOT_ID,
        profile_file=_rel(config.PROFILE_PATH),
        preferences_file=_rel(preferences.PATH),
    )
    b = Bot(id=DEFAULT_BOT_ID, name="", chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(), token_env="TELEGRAM_BOT_TOKEN")
    return {"profiles": [p], "bots": [b]}


def _from_json(data: dict) -> dict:
    pf = {f for f in Profile.__dataclass_fields__}
    bf = {f for f in Bot.__dataclass_fields__}
    profiles = [Profile(**{k: v for k, v in p.items() if k in pf}) for p in data.get("profiles") or []]
    bots = [Bot(**{k: v for k, v in b.items() if k in bf}) for b in data.get("bots") or []]
    return {"profiles": profiles, "bots": bots}


def load() -> dict:
    """{"profiles": [Profile], "bots": [Bot]}: from profiles.json, or from .env without it."""
    if PATH.exists():
        try:
            data = _from_json(json.loads(PATH.read_text(encoding="utf-8")))
            if data["profiles"]:
                return data
        except (ValueError, TypeError):
            pass
    return _legacy()


def save(data: dict):
    out = {"profiles": [asdict(p) for p in data["profiles"]], "bots": [asdict(b) for b in data["bots"]]}
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PATH)


def all_profiles() -> list[Profile]:
    return load()["profiles"]


def all_bots() -> list[Bot]:
    return load()["bots"]


def get(profile_id: str) -> Profile | None:
    return next((p for p in all_profiles() if p.id == profile_id), None)


def get_bot(bot_id: str) -> Bot | None:
    return next((b for b in all_bots() if b.id == bot_id), None)


def bot_of(profile: Profile) -> Bot:
    bot = get_bot(profile.bot)
    if bot is None:
        raise RuntimeError(f"Profile {profile.name or profile.id}: its Telegram bot is not set (dashboard, Profiles)")
    return bot


def issue(profile: Profile) -> str:
    """What is missing for this profile to receive ads, or "" when it is ready."""
    bot = get_bot(profile.bot)
    if bot is None:
        return "no Telegram bot chosen"
    if not bot.token:
        return "bot token missing"
    if not str(bot.chat_id).strip():
        return "chat id missing"
    if not profile.profile_path.exists():
        return "profile text missing"
    return ""


def admin_bots() -> list[Bot]:
    """The admin's bots that are set up (token and chat): admin notices go to the first one."""
    return [b for b in all_bots() if b.admin_owned and b.token and str(b.chat_id).strip()]


def profiles_of(bot: Bot) -> list[Profile]:
    return [p for p in all_profiles() if p.bot == bot.id]


def next_id(prefix: str, existing) -> str:
    n = 1
    ids = {x.id for x in existing}
    while f"{prefix}{n}" in ids:
        n += 1
    return f"{prefix}{n}"


# ---------- the profile being worked on (per thread) ----------

_local = threading.local()


def current() -> Profile:
    p = getattr(_local, "profile", None)
    return p if p is not None else all_profiles()[0]


@contextmanager
def use(profile: Profile):
    previous = getattr(_local, "profile", None)
    _local.profile = profile
    try:
        yield profile
    finally:
        _local.profile = previous
