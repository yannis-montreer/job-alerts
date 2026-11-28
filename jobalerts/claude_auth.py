"""The Claude login used for sorting and application kits, and Telegram alerts to the admin.

Two ways to be logged in:
- a long-lived token from `claude setup-token` (about a year), saved in .env as
  CLAUDE_CODE_OAUTH_TOKEN with its date in CLAUDE_TOKEN_CREATED. Recommended: unattended runs no
  longer depend on the browser login, which expires every few weeks.
- the browser login of `claude auth login` (what `claude auth status` reports).

The admin's bot is warned 7, 3 and 1 day(s) before the expiry, and at once (once a day) when a
call fails because Claude is not logged in.
"""
import json
import logging
import os
import re
import subprocess
from datetime import date, datetime, timedelta, timezone

from dotenv import dotenv_values

from . import config, db, profiles, telegram, texts

log = logging.getLogger(__name__)

TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
CREATED_KEY = "CLAUDE_TOKEN_CREATED"
TOKEN_DAYS = 365
WARN_DAYS = (7, 3, 1)
AUTH_ERROR = re.compile(r"not logged in|/login|log ?in again|invalid api key|oauth|token (has )?expired|"
                        r"authentication|unauthori[sz]ed|\b401\b", re.I)


def _env_file() -> dict:
    path = config.ROOT / ".env"
    return {k: (v or "") for k, v in dotenv_values(path).items()} if path.exists() else {}


def token() -> str:
    """Read from .env each time: the always-on dashboard sees a new token without a restart."""
    return (_env_file().get(TOKEN_KEY) or os.getenv(TOKEN_KEY, "")).strip()


def token_created() -> date | None:
    raw = (_env_file().get(CREATED_KEY) or os.getenv(CREATED_KEY, "")).strip()
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def cli_env() -> dict:
    """Environment for the claude command: never an API key (the subscription is used), and the
    long-lived token when there is one."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", TOKEN_KEY)}
    if token():
        env[TOKEN_KEY] = token()
    return env


def status(exe=None) -> dict:
    """installed, logged_in, mode ("token" or "login"), expires (ISO), days_left, plan, path."""
    from . import triage
    exe = exe or triage.CLAUDE_EXE
    if not exe.exists():
        return dict(installed=False, logged_in=False, mode="", path=str(exe))
    try:
        out = subprocess.run([str(exe), "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                             timeout=60, env=cli_env(),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        info = json.loads(out)
    except Exception as e:
        return dict(installed=True, logged_in=False, mode="token" if token() else "login",
                    error=str(e)[:200], path=str(exe))
    if token():
        created = token_created()
        exp = datetime.combine(created + timedelta(days=TOKEN_DAYS), datetime.min.time(), timezone.utc) if created else None
        mode = "token"
    else:
        raw = info.get("loginExpiresAt")
        exp = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        mode = "login"
    days = (exp - datetime.now(timezone.utc)).days if exp else None
    return dict(installed=True, logged_in=bool(info.get("loggedIn")), mode=mode,
                expires=exp.isoformat() if exp else None, days_left=days,
                plan=info.get("subscriptionType"), path=str(exe))


def is_auth_error(text: str) -> bool:
    return bool(AUTH_ERROR.search(text or ""))


def link() -> str:
    """Where the admin fixes it: the dashboard's AI page."""
    env = _env_file()
    base = (env.get("DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
    if not base:
        host = (env.get("DASHBOARD_REMOTE_HOST") or "").strip()
        base = f"https://{host}:{env.get('DASHBOARD_PORT') or 8787}" if host else "http://127.0.0.1:8787"
    return base + "/#ai"


def notify_admin(con, key: str, text_key: str, **kw) -> bool:
    """One message to the admin's bot, at most once per `key`. Returns whether it was sent."""
    meta = f"admin_notice:{key}"
    if db.get_meta(con, meta):
        return False
    bots = profiles.admin_bots()
    if not bots:
        log.warning("No admin bot set up: %s not sent", text_key)
        return False
    bot = bots[0]
    owner = next(iter(profiles.profiles_of(bot)), None)
    try:
        with profiles.use(owner or profiles.all_profiles()[0]):
            telegram.send_text(texts.t(text_key, link=link(), **kw), bot=bot)
    except Exception as e:
        log.error("Admin notice %s not sent: %s", text_key, str(e)[:200])
        return False
    db.set_meta(con, meta, db.now_iso())
    return True


def check_expiry(con, st: dict | None = None):
    """Warn the admin before the login or token expires, and when Claude is logged out."""
    if config.AI_PROVIDER != "claude":
        return
    st = st or status()
    if not st.get("installed"):
        return
    if not st.get("logged_in"):
        notify_admin(con, f"claude_out:{date.today()}", "claude_logged_out")
        return
    left = st.get("days_left")
    if left is None:
        return
    due = [d for d in WARN_DAYS if left <= d]
    if due:
        notify_admin(con, f"claude_exp:{st['mode']}:{(st.get('expires') or '')[:10]}:{min(due)}",
                     "claude_token_expiry" if st["mode"] == "token" else "claude_login_expiry", days=max(left, 0))


def report_failure(con, error: str):
    """After a failed AI call: if it is a login problem, tell the admin at once (once a day)."""
    if config.AI_PROVIDER == "claude" and is_auth_error(error):
        notify_admin(con, f"claude_failed:{date.today()}", "claude_failed")
