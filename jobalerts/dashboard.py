"""Local setup and status dashboard.

  python -m jobalerts.dashboard            opens http://127.0.0.1:8765 in your browser
                                           (or a free port if 8765 is taken)
  python -m jobalerts.dashboard --port N   prefer another port

  python -m jobalerts.dashboard --service  always-on mode (scheduled task at logon): fixed port
                                           DASHBOARD_PORT (default 8787), no window, no browser

It only listens on 127.0.0.1 (this computer). Every API call must carry a random token that
is generated at start and embedded in the page, and the Host header is checked, so other
websites open in your browser cannot use it.

Phone access: `tailscale serve` forwards https://<your-pc>.<tailnet>.ts.net:<port> to it. That
host name must be set as DASHBOARD_REMOTE_HOST, and requests that come through it need a
login with the dashboard password (a session cookie, 30 days). The same host can also be put on
Tailscale Funnel (public, e.g. port 10000): then the password is the only lock, so it must be
long; every remote login and every lock-out is reported on Telegram, and the always-on mode is
served by waitress rather than Flask's development server.
"""
import argparse
import base64
import hashlib
import html as htmllib
import importlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from flask import Flask, abort, g, jsonify, redirect, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import config, cvdocs, db, finn, linkedin, listener, preferences, profiles, telegram, texts, triage, users

ROOT = config.ROOT
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PROFILE_EXAMPLE = ROOT / "profile.example.md"
PAGE = Path(__file__).with_name("dashboard.html")
PAGE_FR = Path(__file__).with_name("dashboard_fr.json")
LOG_FILE = config.LOG_DIR / "jobalerts.log"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_PORT = 8765
SERVICE_PORT = 8787  # always-on mode; DASHBOARD_PORT in .env overrides it
STATIC = Path(__file__).with_name("static")
MIN_PASSWORD = 12  # the dashboard may be public (Tailscale Funnel)
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"}
TAILSCALE = [shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"]

SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD", "NAV_TOKEN", "GEMINI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
PLAIN_KEYS = {"CANDIDATE_NAME", "LANGUAGE", "TELEGRAM_CHAT_ID", "GMAIL_ADDRESS", "GMAIL_FINN_LABEL",
              "TARGET_MUNICIPALITIES", "TARGET_COUNTIES", "AI_PROVIDER", "GEMINI_MODEL",
              "FINN_FETCH_PAGES", "FINN_CONTACT_EMAIL",
              "NORWEGIAN_REQUIRED", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE",
              "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN", "GMAIL_LINKEDIN_LABEL", "DASHBOARD_LANGUAGE"}
# Settings of the selected profile (profiles.json) or of its bot, not of .env.
PROFILE_KEYS = {"CANDIDATE_NAME", "LANGUAGE", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE", "NORWEGIAN_REQUIRED",
                "GMAIL_FINN_LABEL", "FINN_SUBJECT", "GMAIL_LINKEDIN_LABEL", "TELEGRAM_CHAT_ID", "PROFILE_NAME", "BOT", "BOT_NAME"}
BOOL_KEYS = {"FINN_FETCH_PAGES", "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN"}
AREA_GROUPS = {
    "Oslo": ["OSLO"],
    "Akershus": sorted(config.AKERSHUS),
    "Nearby": sorted(config.NEARBY),
}
DEFAULT_MUNICIPALITIES = {"OSLO"} | config.AKERSHUS | config.NEARBY
DEFAULT_COUNTIES = {"OSLO", "AKERSHUS"}

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=12 * 1024 * 1024, SESSION_COOKIE_NAME="jobalerts_session", SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
TOKEN = secrets.token_urlsafe(24)
_login_failures: dict[str, list[float]] = {}  # username -> times of wrong passwords
PORT = DEFAULT_PORT
_run = {"proc": None, "mode": None, "started": None}


# ---------- security: local, or your Tailscale address with a password; token required ----------

def service_port() -> int:
    raw = os.getenv("DASHBOARD_PORT", "").strip()
    return int(raw) if raw.isdigit() else SERVICE_PORT


def _setting(key: str) -> str:
    """Read from .env each time: another dashboard window may have changed it."""
    return (env_values().get(key) or os.getenv(key, "")).strip()


def remote_host() -> str:
    return _setting("DASHBOARD_REMOTE_HOST").lower()


def is_local() -> bool:
    return request.host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")


def is_remote() -> bool:
    host = remote_host()
    return bool(host) and request.host.lower().split(":")[0] == host


def password_hash() -> str:
    raw = _setting("DASHBOARD_PASSWORD_HASH")
    try:
        return base64.urlsafe_b64decode(raw.encode()).decode() if raw else ""
    except ValueError:
        return ""


def _fingerprint(h: str) -> str:  # changing the password logs every phone out
    return hashlib.sha256(h.encode()).hexdigest()[:24]


def account() -> dict | None:
    """Who uses the dashboard: the admin (on this computer, or with the admin password) or a user
    (users.json), or None when a remote visitor is not logged in. Changing a password logs out
    the sessions made with the old one."""
    if is_local():
        return dict(role="admin", id=users.ADMIN, username=users.ADMIN)
    auth = str(session.get("auth", ""))
    uid = session.get("uid") or users.ADMIN  # sessions from before users existed are the admin's
    if uid == users.ADMIN:
        h = password_hash()
        if h and secrets.compare_digest(auth, _fingerprint(h)):
            return dict(role="admin", id=uid, username=users.ADMIN)
        return None
    u = users.get(uid)
    if u and u.password_hash and secrets.compare_digest(auth, _fingerprint(u.password_hash)):
        return dict(role="user", id=u.id, username=u.username, profiles=u.profile_ids(), language=u.language,
                    must_change=u.must_change)
    return None


def logged_in() -> bool:
    return account() is not None


def is_user() -> bool:
    a = getattr(g, "account", None)
    return bool(a) and a["role"] == "user"


# What a user (not the admin) may call; everything else is the admin's. A user works only on the
# profiles the admin assigned to them.
USER_ROUTES = {
    ("GET", "/"), ("GET", "/logout"), ("GET", "/api/account"), ("POST", "/api/account"),
    ("GET", "/api/settings"), ("POST", "/api/settings"), ("POST", "/api/bots"), ("GET", "/api/status"),
    ("POST", "/api/telegram/detect"), ("POST", "/api/telegram/test"), ("POST", "/api/language"),
    ("GET", "/api/cv"), ("POST", "/api/cv/upload"), ("POST", "/api/cv/facts"), ("POST", "/api/cv/layout"),
    ("POST", "/api/cv/layout/reset"), ("GET", "/api/cv/preview"), ("GET", "/api/cv/original"),
    ("GET", "/api/applications/file"), ("GET", "/api/preferences"), ("POST", "/api/preferences"),
    ("GET", "/api/feedback"), ("GET", "/api/profile"), ("POST", "/api/profile"),
    ("GET", "/api/schedule"), ("POST", "/api/schedule"), ("POST", "/api/run"), ("GET", "/api/run"),
    ("GET", "/api/history"),
}
MUST_CHANGE_ROUTES = {("GET", "/"), ("GET", "/logout"), ("GET", "/api/account"), ("POST", "/api/account")}
NO_PROFILE_ROUTES = {("GET", "/"), ("GET", "/logout"), ("GET", "/api/account"), ("POST", "/api/account"),
                     ("GET", "/api/settings")}


def _deny(message: str):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=message), 403
    abort(403)


def ensure_secret_key():
    if not app.secret_key:
        key = os.getenv("DASHBOARD_SECRET", "").strip()
        if not key:
            key = secrets.token_urlsafe(32)
            write_env({"DASHBOARD_SECRET": key})
        app.secret_key = key


@app.before_request
def _guard():
    local, remote = is_local(), is_remote()
    if not (local or remote):
        abort(403)  # blocks DNS-rebinding tricks from other websites
    if remote:
        ensure_secret_key()
    g.account = account()
    if remote and request.path not in PUBLIC_PATHS and g.account is None:
        if request.path.startswith("/api/"):
            abort(401)
        return redirect("/login")
    if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
        abort(403)
    # The profile picked in the page (each request works on it).
    pid = request.headers.get("X-Profile", "")
    if is_user() and request.path not in PUBLIC_PATHS:
        key = (request.method, request.path)
        if key not in USER_ROUTES:
            return _deny("Only the admin can do this")
        if g.account.get("must_change") and key not in MUST_CHANGE_ROUTES:
            return _deny("Choose your own password first (My account)")
        own = g.account["profiles"]
        pid = pid or (own[0] if own else "")
        if pid and pid not in own:
            return _deny("This profile is not yours")
        if not pid and key not in NO_PROFILE_ROUTES:
            return _deny("No alert is assigned to you yet: ask the admin")
    profiles._local.profile = profiles.get(pid) if pid else None


@app.teardown_request
def _profile_done(exc=None):
    profiles._local.profile = None


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    if is_remote():
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def notice_bots() -> list:
    """The admin's bots that can receive security notices (the first one is used)."""
    return profiles.admin_bots()


def alert_owner(text: str):
    """A short security notice on Telegram (first bot), in the background; never fails."""
    def send():
        try:
            bots = notice_bots()
            if bots:
                telegram.api("sendMessage", bot=bots[0], chat_id=bots[0].chat_id, text=text)
        except Exception:
            pass
    threading.Thread(target=send, daemon=True).start()


def _client() -> str:
    """Where a remote request comes from, as told by Tailscale (Funnel/Serve set this header)."""
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("Tailscale-User-Login", "") or "unknown")


class UserError(Exception):
    pass


@app.errorhandler(UserError)
def _user_error(e):
    return jsonify(ok=False, error=str(e)), 400


@app.errorhandler(Exception)
def _any_error(e):
    if hasattr(e, "code") and isinstance(getattr(e, "code"), int):  # HTTP errors (403...)
        return jsonify(ok=False, error=str(e)), e.code
    return jsonify(ok=False, error=f"{type(e).__name__}: {telegram_safe(str(e))}"), 500


def telegram_safe(text: str) -> str:
    return listener.safe(text)


# ---------- .env ----------

def _quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise UserError("Values must fit on one line")
    if value == "" or re.fullmatch(r"[A-Za-z0-9_.:@,/+\-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(updates: dict):
    """Set keys in .env, keeping every other line and comment."""
    if not ENV_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_PATH) if ENV_EXAMPLE.exists() else ENV_PATH.write_text("", encoding="utf-8")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates and m.group(1) not in done:
            lines[i] = f"{m.group(1)}={_quote(updates[m.group(1)])}"
            done.add(m.group(1))
    lines += [f"{k}={_quote(v)}" for k, v in updates.items() if k not in done]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reload_settings()


def reload_settings():
    load_dotenv(ENV_PATH, override=True)
    importlib.reload(config)  # same module object: every importer sees the new values


def env_values() -> dict:
    return {k: (v or "") for k, v in dotenv_values(ENV_PATH).items()} if ENV_PATH.exists() else {}


def _flag(env: dict, key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _split(raw: str) -> set[str]:
    return {x.strip().upper() for x in (raw or "").split(",") if x.strip()}


# ---------- checks ----------

def claude_status() -> dict:
    from . import claude_auth
    return claude_auth.status(triage.CLAUDE_EXE)


def powershell(command: str, timeout: int = 60) -> str:
    full = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ErrorActionPreference = 'Stop'; " + command
    # Bypass for this process only: the tool's own .ps1 scripts are not signed.
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode != 0:
        raise UserError((proc.stderr or proc.stdout).strip()[:400] or "PowerShell failed")
    return proc.stdout


def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def list_tasks() -> list[dict]:
    """Scheduled tasks that run jobalerts from this folder, whatever their name."""
    out = powershell(
        "$root = " + _ps_str(str(ROOT)) + "; "
        "$list = @(Get-ScheduledTask | Where-Object { @($_.Actions | Where-Object { "
        "$_.Arguments -like '*jobalerts.run*' -and $_.WorkingDirectory -eq $root }).Count -gt 0 } | ForEach-Object { "
        "$i = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath; $t = $_.Triggers | Select-Object -First 1; "
        "[pscustomobject]@{ name = $_.TaskName; path = $_.TaskPath; args = $_.Actions[0].Arguments; state = [string]$_.State; "
        "start = $(if ($t) { ([datetime]$t.StartBoundary).ToString('HH:mm') } else { '' }); "
        "every = $(if ($t -and $t.Repetition.Interval) { [string]$t.Repetition.Interval } else { '' }); "
        "next = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }); "
        "last = $(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' }); "
        "result = $i.LastTaskResult } }); ConvertTo-Json -InputObject $list -Compress"
    )
    data = json.loads(out or "[]")
    for t in data:
        args = t.get("args") or ""
        m = re.search(r"--profile\s+(\S+)", args)
        t["profile"] = m.group(1) if m else None
        t["kind"] = "catch-up" if "--collect-only" in args else ("profile" if m else "full run")
    return data


# ---------- schedule: one task per profile (its message time), plus the shared backup click check ----------

TASK_PREFIX = "JobAlerts"
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def schedule_task(name: str, arguments: str = "", at: str = "07:30", every_minutes: int = 0,
                  for_hours: float = 0, limit_minutes: int = 30, remove: bool = False) -> str:
    script = ROOT / "schedule_task.ps1"
    cmd = f"& {_ps_str(str(script))} -Name {_ps_str(name)}"
    if remove:
        return powershell(cmd + " -Remove", timeout=60)
    cmd += (f" -Arguments {_ps_str(arguments)} -At {_ps_str(at)} -EveryMinutes {int(every_minutes)}"
            f" -ForHours {float(for_hours)} -LimitMinutes {int(limit_minutes)}")
    return powershell(cmd, timeout=60)


def effective_time(p, legacy_start: str = "") -> str:
    return p.send_time or legacy_start or "07:30"


def schedule_state(tasks=None) -> dict:
    tasks = list_tasks() if tasks is None else tasks
    legacy = [t for t in tasks if t["kind"] == "full run"]
    by_profile = {t["profile"]: t for t in tasks if t["kind"] == "profile"}
    catchup = next((t for t in tasks if t["kind"] == "catch-up"), None)
    rows = []
    for p in profiles.all_profiles():
        task = by_profile.get(p.id) or (legacy[0] if legacy else None)
        rows.append(dict(id=p.id, name=p.name or p.id, time=effective_time(p, legacy[0]["start"] if legacy else ""),
                         scheduled=bool(task), next=(task or {}).get("next", ""), last=(task or {}).get("last", ""),
                         result=(task or {}).get("result")))
    return dict(profiles=rows, catchup=catchup and {k: catchup[k] for k in ("start", "every", "next", "last", "result")},
                dashboard=service_running())


def ensure_profile_tasks():
    """One task per profile at its message time; the old all-profiles morning task is removed."""
    tasks = list_tasks()
    legacy = [t for t in tasks if t["kind"] == "full run"]
    legacy_start = legacy[0]["start"] if legacy else ""
    data = profiles.load()
    for p in data["profiles"]:
        p.send_time = effective_time(p, legacy_start)
    profiles.save(data)
    by_profile = {t["profile"]: t for t in tasks if t["kind"] == "profile"}
    for p in data["profiles"]:
        task = by_profile.get(p.id)
        if not task or task["start"] != p.send_time:
            if task and task["name"] != f"{TASK_PREFIX}-{p.id}":
                schedule_task(task["name"], remove=True)
            schedule_task(f"{TASK_PREFIX}-{p.id}", f"-m jobalerts.run --profile {p.id}", p.send_time)
    ids = {p.id for p in data["profiles"]}
    for t in tasks:
        if t["kind"] == "full run" or (t["kind"] == "profile" and t["profile"] not in ids):
            schedule_task(t["name"], remove=True)


def log_tail(n: int = 60) -> list[str]:
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 64_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return [telegram_safe(line) for line in lines[-n:]]


def last_runs() -> list[str]:
    return [line for line in log_tail(400) if "===" in line or "failed" in line or "échoué" in line][-6:]


def db_stats() -> dict:
    """Numbers for the selected profile."""
    pid = profiles.current().id
    con = db.connect()
    q = lambda sql: con.execute(sql, {"p": pid}).fetchone()[0]  # noqa: E731
    last = "d.id=(SELECT MAX(id) FROM decisions WHERE uuid=d.uuid AND profile_id=:p)"
    try:
        return dict(
            ads=q("SELECT count(*) FROM matches WHERE profile_id=:p"),
            sorted=q("SELECT count(*) FROM matches WHERE profile_id=:p AND triaged_at IS NOT NULL"),
            relevant=q("SELECT count(*) FROM matches WHERE profile_id=:p AND relevant=1"),
            sent=q("SELECT count(*) FROM matches WHERE profile_id=:p AND sent_at IS NOT NULL"),
            kept=q(f"SELECT count(DISTINCT uuid) FROM decisions d WHERE profile_id=:p AND decision='keep' AND {last}"),
            rejected=q(f"SELECT count(DISTINCT uuid) FROM decisions d WHERE profile_id=:p AND decision='reject' AND {last}"),
            last_nav=db.get_meta(con, "nav_last_success"),
        )
    finally:
        con.close()


def profile_issue(p) -> str:
    return profiles.issue(p)


def profile_state() -> dict:
    p = profiles.current().profile_path
    if not p.exists():
        return dict(exists=False, is_template=True)
    text = p.read_text(encoding="utf-8")
    tmpl = PROFILE_EXAMPLE.read_text(encoding="utf-8") if PROFILE_EXAMPLE.exists() else ""
    return dict(exists=True, is_template=text.strip() == tmpl.strip(), chars=len(text))


# ---------- API ----------

@app.get("/")
def page():
    html = PAGE.read_text(encoding="utf-8")
    ui = env_values().get("DASHBOARD_LANGUAGE", "").strip().lower()
    if is_user() and g.account.get("language"):
        ui = g.account["language"]
    me = json.dumps(dict(role=g.account["role"], username=g.account["username"],
                         must_change=bool(g.account.get("must_change"))) if g.account else {})
    fr = json.dumps(json.loads(PAGE_FR.read_text(encoding="utf-8")), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("{{TOKEN}}", TOKEN).replace("{{UI_LANG}}", ui if ui in ("en", "fr") else "en").replace("{{FR}}", fr)
    html = html.replace("{{REMOTE}}", "true" if is_remote() else "false").replace("{{ME}}", me.replace("</", "<\\/"))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


USER_VALUE_KEYS = ("CANDIDATE_NAME", "LANGUAGE", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE", "NORWEGIAN_REQUIRED",
                   "TELEGRAM_CHAT_ID", "PROFILE_NAME", "BOT", "BOT_NAME")
USER_SETTING_KEYS = {"CANDIDATE_NAME", "LANGUAGE", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE", "NORWEGIAN_REQUIRED", "area"}


@app.get("/api/settings")
def get_settings():
    if is_user() and not g.account["profiles"]:
        return jsonify(ok=True, me=dict(role="user", username=g.account["username"]), values={}, secrets_set={},
                       area=None, env_exists=True, profile="", profiles=[], bots=[])
    env = env_values()
    munis = _split(env.get("TARGET_MUNICIPALITIES")) or DEFAULT_MUNICIPALITIES
    counties = _split(env.get("TARGET_COUNTIES")) or DEFAULT_COUNTIES
    known = {m for g in AREA_GROUPS.values() for m in g}
    p = profiles.current()
    bot = profiles.get_bot(p.bot)
    munis, counties = p.target_municipalities, p.target_counties
    values = {k: env.get(k, "") for k in PLAIN_KEYS}
    values.update(CANDIDATE_NAME=p.candidate, LANGUAGE=p.language, MESSAGE_MIN_SCORE=str(p.message_min),
                  DIGEST_MIN_SCORE=str(p.digest_min), NORWEGIAN_REQUIRED=p.norwegian,
                  GMAIL_FINN_LABEL=p.finn_label, FINN_SUBJECT=p.finn_subject, GMAIL_LINKEDIN_LABEL=p.linkedin_label,
                  TELEGRAM_CHAT_ID=bot.chat_id if bot else "", PROFILE_NAME=p.name, BOT=p.bot,
                  BOT_NAME=bot.name if bot else "")
    secrets_set = {k: bool(env.get(k)) for k in SECRET_KEYS}
    secrets_set["TELEGRAM_BOT_TOKEN"] = bool(bot and bot.token)
    if is_user():  # only this user's profiles and bot, no machine settings
        own = set(g.account["profiles"])
        return jsonify(
            ok=True, me=dict(role="user", username=g.account["username"]),
            values={k: values[k] for k in USER_VALUE_KEYS},
            secrets_set={"TELEGRAM_BOT_TOKEN": secrets_set["TELEGRAM_BOT_TOKEN"]},
            area=dict(groups=AREA_GROUPS, selected=sorted(munis & known), extra=sorted(munis - known),
                      counties=sorted(counties)),
            env_exists=True, profile=p.id,
            profiles=[dict(id=x.id, name=x.name or x.id, issue=profile_issue(x)) for x in profiles.all_profiles() if x.id in own],
            bots=[bot.public()] if bot else [],
        )
    return jsonify(
        me=dict(role="admin", username=users.ADMIN),
        ok=True,
        values=values,
        secrets_set=secrets_set,
        area=dict(groups=AREA_GROUPS, selected=sorted(munis & known), extra=sorted(munis - known),
                  counties=sorted(counties)),
        env_exists=ENV_PATH.exists(),
        profile=p.id,
        profiles=[dict(id=x.id, name=x.name or x.id, issue=profile_issue(x)) for x in profiles.all_profiles()],
        bots=[b.public() for b in profiles.all_bots()],
    )


def _user_settings(body: dict) -> dict:
    """What a user may change: their profile's settings, and a bot that is not set up yet
    (token, chat, name). Unchanged values of other fields are ignored; changed ones refused."""
    p = profiles.current()
    bot = profiles.get_bot(p.bot)
    ready = bool(bot and bot.token and str(bot.chat_id).strip())
    current = dict(PROFILE_NAME=p.name, BOT=p.bot, BOT_NAME=bot.name if bot else "",
                   TELEGRAM_CHAT_ID=str(bot.chat_id).strip() if bot else "")
    locked = "This bot is already set up: only the admin can change it"
    out = {}
    for k, v in body.items():
        value = str(v or "").strip() if k != "area" else v
        if k in USER_SETTING_KEYS:
            out[k] = v
        elif k == "TELEGRAM_BOT_TOKEN":
            if value:
                if bot is None:
                    raise UserError("Add a bot to this profile first (+ New bot)")
                if bot.token:
                    raise UserError(locked)
                out[k] = value
        elif k in ("TELEGRAM_CHAT_ID", "BOT_NAME") and value != current[k]:
            if bot is None:
                raise UserError("Add a bot to this profile first (+ New bot)")
            if (k == "TELEGRAM_CHAT_ID" and current[k]) or ready:
                raise UserError(locked)
            out[k] = value
        elif k in current and value == current[k]:
            continue
        else:
            raise UserError("Only the admin can change this setting")
    return out


@app.post("/api/settings")
def save_settings():
    body = request.get_json(force=True) or {}
    if is_user():
        body = _user_settings(body)
    updates = {}
    for k in (PLAIN_KEYS | PROFILE_KEYS) - {"TARGET_MUNICIPALITIES", "TARGET_COUNTIES"}:
        if k in body:
            updates[k] = str(body[k]).strip()
    for k in ("LANGUAGE", "DASHBOARD_LANGUAGE"):
        if k in updates and updates[k] not in ("en", "fr"):
            raise UserError("Language must be en or fr")
    if "AI_PROVIDER" in updates and updates["AI_PROVIDER"] not in ("claude", "gemini"):
        raise UserError("AI must be claude or gemini")
    if updates.get("GEMINI_MODEL") and not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", updates["GEMINI_MODEL"]):
        raise UserError("Gemini model id looks wrong (e.g. gemini-flash-latest)")
    if "NORWEGIAN_REQUIRED" in updates and updates["NORWEGIAN_REQUIRED"] not in ("flag", "reject"):
        raise UserError("Norwegian-required ads: flag or reject")
    for k in ("MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE"):
        if k in updates and updates[k] not in ("1", "2", "3", "4", "5"):
            raise UserError("Scores go from 1 to 5")
    if "MESSAGE_MIN_SCORE" in updates and "DIGEST_MIN_SCORE" in updates \
            and int(updates["DIGEST_MIN_SCORE"]) > int(updates["MESSAGE_MIN_SCORE"]):
        raise UserError("The digest minimum must not be above the message minimum")
    for k in BOOL_KEYS & set(updates):
        updates[k] = "true" if updates[k].lower() in ("1", "true", "yes", "on") else "false"
    if updates.get("FINN_CONTACT_EMAIL") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", updates["FINN_CONTACT_EMAIL"]):
        raise UserError("The Finn contact email looks wrong")
    if updates.get("FINN_FETCH_PAGES") == "true":
        email = updates.get("FINN_CONTACT_EMAIL", env_values().get("FINN_CONTACT_EMAIL", ""))
        if not email:
            raise UserError("Fetching Finn pages needs a contact email (Finn asks for it in the User-Agent)")
    if updates.get("TELEGRAM_CHAT_ID") and not re.fullmatch(r"-?\d+", updates["TELEGRAM_CHAT_ID"]):
        raise UserError("The chat id is a number (use 'Detect' in the Telegram section)")
    for k in SECRET_KEYS:  # empty field = keep the current value; "clear" list = remove it
        if str(body.get(k) or "").strip():
            updates[k] = str(body[k]).strip()
    for k in body.get("clear") or []:
        if k in SECRET_KEYS:
            updates[k] = ""
    if "CLAUDE_CODE_OAUTH_TOKEN" in updates:  # a yearly token from `claude setup-token`: its date, for the reminders
        tok = updates["CLAUDE_CODE_OAUTH_TOKEN"]
        if tok and (len(tok) < 20 or re.search(r"\s", tok)):
            raise UserError("This does not look like a Claude token: copy the whole line that starts with sk-ant-")
        updates["CLAUDE_TOKEN_CREATED"] = datetime.now().date().isoformat() if tok else ""
    area = None
    if "area" in body:
        munis = {m.strip().upper() for m in body["area"].get("municipalities") or [] if m.strip()}
        munis |= _split(body["area"].get("extra", ""))
        counties = {c.strip().upper() for c in body["area"].get("counties") or [] if c.strip()}
        if not munis:
            raise UserError("Pick at least one municipality")
        area = ([] if munis == DEFAULT_MUNICIPALITIES else sorted(munis),
                [] if counties == DEFAULT_COUNTIES else sorted(counties))
    saved = save_profile_settings({k: updates.pop(k) for k in list(updates) if k in PROFILE_KEYS},
                                  updates.pop("TELEGRAM_BOT_TOKEN", None), area)
    if updates:
        write_env(updates)
    return jsonify(ok=True, saved=sorted(updates) + saved)


def save_profile_settings(changes: dict, token: str | None, area) -> list:
    """Save the selected profile's settings (and its bot's) in profiles.json, and the bot token in .env."""
    if not changes and token is None and area is None:
        return []
    data = profiles.load()  # without profiles.json: the profile from .env, saved from now on
    pid = profiles.current().id
    p = next(x for x in data["profiles"] if x.id == pid)
    if "BOT" in changes:
        if changes["BOT"] and not any(b.id == changes["BOT"] for b in data["bots"]):
            raise UserError("Unknown bot")
        p.bot = changes.pop("BOT")
    if "PROFILE_NAME" in changes:
        name = changes.pop("PROFILE_NAME")[:40]
        if not name:
            raise UserError("The profile needs a name")
        if any(x.name.lower() == name.lower() and x.id != p.id for x in data["profiles"]):
            raise UserError("Another profile already has this name")
        p.name = name
    fields = dict(CANDIDATE_NAME="candidate", LANGUAGE="language", NORWEGIAN_REQUIRED="norwegian",
                  GMAIL_FINN_LABEL="finn_label", FINN_SUBJECT="finn_subject", GMAIL_LINKEDIN_LABEL="linkedin_label")
    for key, attr in fields.items():
        if key in changes:
            setattr(p, attr, changes.pop(key))
    for key, attr in (("MESSAGE_MIN_SCORE", "message_min"), ("DIGEST_MIN_SCORE", "digest_min")):
        if key in changes:
            setattr(p, attr, int(changes.pop(key)))
    if p.digest_min > p.message_min:
        raise UserError("The digest minimum must not be above the message minimum")
    if area is not None:
        p.municipalities, p.counties = area
    bot = next((b for b in data["bots"] if b.id == p.bot), None)
    if "BOT_NAME" in changes or "TELEGRAM_CHAT_ID" in changes or token is not None:
        if bot is None:
            raise UserError("Choose or create a bot for this profile first")
        if "BOT_NAME" in changes:
            bot.name = changes.pop("BOT_NAME")[:40]
        if "TELEGRAM_CHAT_ID" in changes:
            bot.chat_id = changes.pop("TELEGRAM_CHAT_ID")
    profiles.save(data)
    if token is not None:
        write_env({bot.token_env: token})
    return ["profile"]


@app.post("/api/profiles")
def create_profile():
    """A new profile (another candidate or alert), with default settings and no bot yet."""
    name = str((request.get_json(force=True) or {}).get("name", "")).strip()[:40]
    if not name:
        raise UserError("The profile needs a name")
    data = profiles.load()
    if any(x.name.lower() == name.lower() for x in data["profiles"]):
        raise UserError("Another profile already has this name")
    p = profiles.Profile(id=profiles.next_id("p", data["profiles"]), name=name, candidate=name, bot="",
                         language=data["profiles"][0].language)
    data["profiles"].append(p)
    profiles.save(data)
    _sync_tasks_if_per_profile()
    return jsonify(ok=True, id=p.id)


def _sync_tasks_if_per_profile():
    """When profiles already have their own tasks, a new or deleted profile gets / loses its own."""
    try:
        if any(t["kind"] == "profile" for t in list_tasks()):
            ensure_profile_tasks()
    except Exception as e:  # the profile change is saved anyway; the Schedule page can retry
        print(f"schedule update failed: {str(e)[:200]}")


@app.post("/api/profiles/delete")
def delete_profile():
    """Remove a profile from the list. Its files and history stay on disk."""
    pid = str((request.get_json(force=True) or {}).get("id", ""))
    data = profiles.load()
    if len(data["profiles"]) < 2:
        raise UserError("The last profile cannot be deleted")
    if not any(x.id == pid for x in data["profiles"]):
        raise UserError("Unknown profile")
    data["profiles"] = [x for x in data["profiles"] if x.id != pid]
    profiles.save(data)
    _sync_tasks_if_per_profile()
    return jsonify(ok=True)


@app.post("/api/bots")
def create_bot():
    """A new bot entry for the selected profile; its token goes to .env (BOT_<ID>_TOKEN)."""
    name = str((request.get_json(force=True) or {}).get("name", "")).strip()[:40]
    if is_user() and profiles.get_bot(profiles.current().bot):
        raise UserError("This profile already has a bot: only the admin can change it")
    data = profiles.load()
    bot = profiles.Bot(id=profiles.next_id("b", data["bots"]), name=name,
                       owner=g.account["id"] if is_user() else "")
    bot.token_env = f"BOT_{bot.id.upper()}_TOKEN"
    data["bots"].append(bot)
    pid = profiles.current().id
    next(x for x in data["profiles"] if x.id == pid).bot = bot.id
    profiles.save(data)
    return jsonify(ok=True, id=bot.id)


def _bot():
    return profiles.get_bot(profiles.current().bot)


def _listener_active() -> bool:
    try:
        con = db.connect()
        try:
            return bool(_bot()) and listener.active(con, _bot().id)
        finally:
            con.close()
    except Exception:
        return False


@app.get("/api/status")
def status():
    env = env_values()
    try:
        tasks = list_tasks()
        tasks_error = None
    except Exception as e:
        tasks, tasks_error = [], str(e)[:300]
    running = _run["proc"] is not None and _run["proc"].poll() is None
    provider = (env.get("AI_PROVIDER") or "claude").lower()
    if is_user():  # this user's profile only: nothing about the machine, the AI or other people
        return jsonify(
            ok=True,
            telegram=dict(token=bool(_bot() and _bot().token), chat_id=bool(_bot() and str(_bot().chat_id).strip()),
                          listener=_listener_active()),
            profile=profile_state(), name=bool(profiles.current().candidate),
            current=dict(id=profiles.current().id, name=profiles.current().name or profiles.current().id,
                         issue=profile_issue(profiles.current())),
            tasks=[], tasks_error=tasks_error,
            schedule=_for_viewer(schedule_state(tasks)) if not tasks_error else None,
            db=db_stats(), last_runs=[], run=dict(running=running, mode=_run["mode"], started=_run["started"]),
        )
    return jsonify(
        ok=True,
        ai=dict(provider=provider, gemini_key=bool(env.get("GEMINI_API_KEY")),
                gemini_model=env.get("GEMINI_MODEL") or "gemini-flash-latest"),
        claude=claude_status(),
        telegram=dict(token=bool(_bot() and _bot().token), chat_id=bool(_bot() and str(_bot().chat_id).strip()),
                      listener=_listener_active()),
        gmail=dict(configured=bool(env.get("GMAIL_ADDRESS") and env.get("GMAIL_APP_PASSWORD"))),
        sources=dict(nav=_flag(env, "SOURCE_NAV", True), finn=_flag(env, "SOURCE_FINN", True),
                     linkedin=_flag(env, "SOURCE_LINKEDIN", False)),
        profile=profile_state(),
        name=bool(profiles.current().candidate),
        current=dict(id=profiles.current().id, name=profiles.current().name or profiles.current().id,
                     issue=profile_issue(profiles.current())),
        tasks=tasks, tasks_error=tasks_error,
        schedule=schedule_state(tasks) if not tasks_error else None,
        db=db_stats(), last_runs=last_runs(),
        run=dict(running=running, mode=_run["mode"], started=_run["started"]),
        remote=remote_state(),
    )


@app.post("/api/telegram/detect")
def telegram_detect():
    reload_settings()
    me = telegram.api("getMe")
    chats = {}
    # Without an offset, getUpdates does not consume anything: the run still sees every click.
    for u in telegram.api("getUpdates", timeout=0, allowed_updates=["message", "callback_query"]):
        m = u.get("message") or {}
        if m.get("chat"):
            chats[m["chat"]["id"]] = dict(id=m["chat"]["id"], type=m["chat"].get("type"),
                                          name=" ".join(filter(None, [m["from"].get("first_name"), m["from"].get("last_name")])),
                                          username=m["from"].get("username"))
    return jsonify(ok=True, bot=me.get("username"), chats=list(chats.values()))


@app.post("/api/telegram/test")
def telegram_test():
    reload_settings()
    telegram.send_text("✅ Job alerts: test message from the dashboard. Telegram works.")
    return jsonify(ok=True)


@app.post("/api/language")
def set_language():
    """Dashboard and/or Telegram language. A new Telegram language is confirmed in Telegram."""
    body = request.get_json(force=True) or {}
    updates = {}
    for key, field in (("LANGUAGE", "alerts"), ("DASHBOARD_LANGUAGE", "dashboard")):
        if field in body:
            value = str(body[field]).strip().lower()
            if value not in ("en", "fr"):
                raise UserError("Language must be en or fr")
            updates[key] = value
    if not updates:
        raise UserError("Nothing to change")
    if is_user() and "DASHBOARD_LANGUAGE" in updates:  # each user has their own dashboard language
        users.update(g.account["id"], language=updates.pop("DASHBOARD_LANGUAGE"))
    if "LANGUAGE" in updates:
        save_profile_settings({"LANGUAGE": updates.pop("LANGUAGE")}, None, None)
        profiles._local.profile = profiles.get(profiles.current().id)  # reload: new language
        updates["_alerts"] = "1"
    if updates.keys() - {"_alerts"}:
        write_env({k: v for k, v in updates.items() if k != "_alerts"})
    sent, error = None, None
    if "_alerts" in updates:
        try:
            telegram.send_text(texts.t("language_set"))
            sent = True
        except Exception as e:  # the language is saved anyway
            sent, error = False, telegram_safe(str(e))
    return jsonify(ok=True, telegram_sent=sent, telegram_error=error)


# ---------- phone access (Tailscale) ----------

LOGIN_TEXTS = {
    "en": dict(title="Job Alerts: log in", user="Username", label="Password", button="Log in",
               wrong="Wrong password.", wait="Too many attempts: wait 10 minutes.",
               unset="No password is set yet: on the computer, open the dashboard, page Phone access."),
    "fr": dict(title="Alertes emploi : connexion", user="Nom d'utilisateur", label="Mot de passe", button="Se connecter",
               wrong="Mot de passe incorrect.", wait="Trop d'essais : attends 10 minutes.",
               unset="Aucun mot de passe n'est défini : sur l'ordinateur, ouvre le tableau de bord, page Accès mobile."),
}
LOGIN_PAGE = """<!doctype html><html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%(title)s</title>
<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#2f6fdb">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2330;--line:#e2e5eb;--accent:#2f6fdb;--bad:#c53030}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--panel:#1c2028;--text:#e6e9ef;--line:#2c323d;--accent:#6b9bff;--bad:#ff7b72}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(360px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:20px;margin:0 0 16px} label{display:block;font-weight:600;margin-bottom:6px}
input{width:100%%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
button{margin-top:14px;width:100%%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600}
p{color:var(--bad);margin:12px 0 0}</style></head><body>
<form method="post" action="/login"><h1>%(title)s</h1>
<label for="un">%(user)s</label><input id="un" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" autofocus required>
<label for="pw" style="margin-top:12px">%(label)s</label><input id="pw" name="password" type="password" autocomplete="current-password" required>
<button type="submit">%(button)s</button>%(note)s</form></body></html>"""


def _login_texts() -> tuple[str, dict]:
    ui = (env_values().get("DASHBOARD_LANGUAGE") or "en").lower()
    ui = ui if ui in LOGIN_TEXTS else "en"
    return ui, LOGIN_TEXTS[ui]


def _login_page(error: str = "", status: int = 200):
    ui, tx = _login_texts()
    note = tx["unset"] if not password_hash() else error
    e = htmllib.escape
    page = LOGIN_PAGE % dict(lang=ui, title=e(tx["title"]), user=e(tx["user"]), label=e(tx["label"]), button=e(tx["button"]),
                             note=f"<p>{e(note)}</p>" if note else "")
    return page, status, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not is_remote():
        return redirect("/")
    if request.method == "GET":
        return _login_page()
    _, tx = _login_texts()
    now = time.time()
    name = request.form.get("username", "").strip().lower()[:30] or users.ADMIN
    for k in list(_login_failures):
        _login_failures[k] = [x for x in _login_failures[k] if now - x < 600]
    fails = _login_failures.setdefault(name, [])
    if len(fails) >= 5 or sum(map(len, _login_failures.values())) >= 20:
        return _login_page(tx["wait"], 429)
    password = request.form.get("password", "")
    if name == users.ADMIN:
        uid, h = users.ADMIN, password_hash()
        ok = bool(h) and check_password_hash(h, password)
    else:
        u = users.by_username(name)
        uid, h = (u.id, u.password_hash) if u else ("", "")
        ok = bool(u) and u.check(password)
        if not u:
            check_password_hash(generate_password_hash("x"), password)  # same delay as a real check
    if ok:
        fails.clear()
        session.clear()
        session.permanent = True
        session["uid"] = uid
        session["auth"] = _fingerprint(h)
        alert_owner(f"🔐 Dashboard: {name} logged in from {_client()} ({datetime.now():%d.%m %H:%M}). "
                    "Not expected? Change that password, or turn the public access off.")
        return redirect("/")
    fails.append(now)
    if len(fails) == 5:
        alert_owner(f"⚠️ Dashboard: 5 wrong passwords for \"{name}\" in 10 minutes (last from {_client()}). "
                    "Logins for this name are paused for 10 minutes.")
    time.sleep(1)
    return _login_page(tx["wrong"] if password_hash() else "", 401)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login" if is_remote() else "/")


def tailscale_name() -> str:
    """This computer's Tailscale name (e.g. my-pc.tail1234.ts.net), or "" without Tailscale."""
    try:
        out = subprocess.run(TAILSCALE + ["status", "--json"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return (json.loads(out).get("Self", {}).get("DNSName") or "").rstrip(".").lower()
    except Exception:
        return ""


def service_running() -> bool:
    if PORT == service_port():
        return True
    try:
        with socket.create_connection(("127.0.0.1", service_port()), timeout=1):
            return True
    except OSError:
        return False


def remote_state() -> dict:
    host = remote_host()
    return dict(host=host, password_set=bool(password_hash()), port=service_port(), service=service_running(),
                url=f"https://{host}:{service_port()}/" if host else "")


@app.get("/api/remote")
def get_remote():
    return jsonify(ok=True, suggested_host=tailscale_name(), **remote_state())


@app.post("/api/remote")
def save_remote():
    body = request.get_json(force=True) or {}
    updates = {}
    if "host" in body:
        host = str(body["host"]).strip().lower().removeprefix("https://").split("/")[0].split(":")[0]
        if host in ("127.0.0.1", "localhost"):
            raise UserError("Use the Tailscale address, not this computer's local address")
        if host and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
            raise UserError("The Tailscale address looks wrong (e.g. my-pc.tail1234.ts.net)")
        updates["DASHBOARD_REMOTE_HOST"] = host
    if body.get("password"):
        pw = str(body["password"])
        if len(pw) < MIN_PASSWORD:
            raise UserError("The password needs at least 12 characters")
        updates["DASHBOARD_PASSWORD_HASH"] = base64.urlsafe_b64encode(generate_password_hash(pw).encode()).decode()
    if not updates:
        raise UserError("Nothing to save")
    write_env(updates)
    return jsonify(ok=True, **remote_state())


@app.post("/api/remote/service")
def start_service():
    """Create (or update) the always-on dashboard task and start it."""
    if not PYTHONW.exists():
        raise UserError(f"Virtual environment not found: {PYTHONW}")
    script = ROOT / "setup_dashboard_task.ps1"
    out = powershell(f"& {_ps_str(str(script))} -Prefix {_ps_str(TASK_PREFIX)}", timeout=60)
    return jsonify(ok=True, output=out.strip(), **remote_state())


@app.post("/api/remote/serve")
def tailscale_serve():
    """Share the always-on dashboard on this computer's Tailscale address (Serve: private to the tailnet)."""
    port = service_port()
    try:
        proc = subprocess.run(TAILSCALE + ["serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"],
                              capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise UserError("Tailscale is not installed on this computer") from None
    if proc.returncode != 0:
        raise UserError(f"tailscale serve failed: {(proc.stdout + proc.stderr).strip()[:400]}")
    host = remote_host() or tailscale_name()
    if host and not remote_host():
        write_env({"DASHBOARD_REMOTE_HOST": host})
    return jsonify(ok=True, output=proc.stdout.strip()[:400], **remote_state())


# ---------- installable app (PWA) ----------

@app.get("/manifest.webmanifest")
def manifest():
    data = dict(name="Job Alerts", short_name="Job Alerts", start_url="/", scope="/", display="standalone",
                background_color="#14171c", theme_color="#2f6fdb",
                icons=[{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                       {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}])
    return json.dumps(data), 200, {"Content-Type": "application/manifest+json"}


@app.get("/sw.js")
def service_worker():
    # Installable app, no offline copy: every page is always fetched from the computer.
    js = ("self.addEventListener('install', () => self.skipWaiting());\n"
          "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\n"
          "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));\n")
    return js, 200, {"Content-Type": "text/javascript; charset=utf-8"}


@app.get("/icon-192.png")
@app.get("/icon-512.png")
@app.get("/apple-touch-icon.png")
def icon():
    name = "icon-512.png" if request.path == "/icon-512.png" else "icon-192.png"
    return send_file(STATIC / name, mimetype="image/png")


# ---------- CV and application documents ----------

@app.get("/api/cv")
def cv_state():
    return jsonify(ok=True, **cvdocs.state())


@app.post("/api/cv/upload")
def cv_upload():
    """Store the selected profile's CV, then read its facts (one AI call, about a minute)."""
    f = request.files.get("file")
    if f is None or not f.filename:
        raise UserError("Choose a PDF or Word file first")
    try:
        info = cvdocs.save_upload(f.filename, f.read())
    except ValueError as e:
        raise UserError(str(e)) from None
    cvdocs.build_facts()
    return jsonify(ok=True, photo=info["photo"], **cvdocs.state())


@app.post("/api/cv/facts")
def cv_facts():
    if cvdocs.original() is None:
        raise UserError("Upload a CV first")
    cvdocs.build_facts()
    return jsonify(ok=True, **cvdocs.state())


@app.post("/api/cv/layout")
def cv_layout():
    """The AI looks at the uploaded PDF and writes a layout copying it (a few minutes)."""
    try:
        cvdocs.build_template()
    except ValueError as e:
        raise UserError(str(e)) from None
    return jsonify(ok=True, **cvdocs.state())


@app.post("/api/cv/layout/reset")
def cv_layout_reset():
    cvdocs.reset_template()
    return jsonify(ok=True, **cvdocs.state())


@app.get("/api/cv/preview")
def cv_preview():
    try:
        pdf = cvdocs.preview()
    except ValueError as e:
        raise UserError(str(e)) from None
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name="CV preview.pdf")


@app.get("/api/cv/original")
def cv_original():
    src = cvdocs.original()
    if src is None:
        raise UserError("No CV uploaded")
    return send_file(src, as_attachment=True, download_name=cvdocs.state()["filename"] or src.name)


@app.get("/api/applications/file")
def application_file():
    """A PDF made for a kept ad (adapted CV or cover letter), for the selected profile."""
    uuid, name = request.args.get("uuid", ""), request.args.get("name", "")
    match = [p for p in cvdocs.documents(uuid=uuid) if p.name == name]
    if not match:
        raise UserError("Document not found")
    return send_file(match[0], mimetype="application/pdf", as_attachment=True, download_name=match[0].name)


@app.post("/api/gmail/test")
def gmail_test():
    reload_settings()
    return jsonify(ok=True, result=finn.check_connection())


@app.get("/api/preferences")
def get_preferences():
    return jsonify(ok=True, preferences=preferences.load(), levels=list(preferences.LEVELS))


@app.post("/api/preferences")
def save_preferences():
    data = preferences.save((request.get_json(force=True) or {}).get("preferences") or {})
    return jsonify(ok=True, preferences=data, prompt=preferences.prompt_section(data))


@app.get("/api/feedback")
def feedback():
    """Every ad the user clicked or replied to, newest first (the examples the AI learns from)."""
    con = db.connect()
    rows = con.execute(
        "SELECT a.title, a.employer, a.url, a.source, "
        "(SELECT decision FROM decisions WHERE uuid=a.uuid AND profile_id=:p ORDER BY id DESC LIMIT 1) AS decision, "
        "(SELECT group_concat(text, ' / ') FROM notes WHERE uuid=a.uuid AND profile_id=:p) AS notes "
        "FROM ads a LEFT JOIN matches m ON m.uuid=a.uuid AND m.profile_id=:p "
        "WHERE EXISTS (SELECT 1 FROM decisions WHERE uuid=a.uuid AND profile_id=:p) "
        "OR EXISTS (SELECT 1 FROM notes WHERE uuid=a.uuid AND profile_id=:p) ORDER BY m.sent_at DESC",
        {"p": profiles.current().id},
    ).fetchall()
    con.close()
    return jsonify(ok=True, rows=[dict(r) for r in rows])


@app.post("/api/linkedin/test")
def linkedin_test():
    reload_settings()
    return jsonify(ok=True, result=linkedin.check_connection())


@app.post("/api/finn/purge")
def finn_purge():
    from . import db
    con = db.connect()
    try:
        return jsonify(ok=True, count=finn.purge(con))
    finally:
        con.close()


@app.post("/api/gemini/test")
def gemini_test():
    reload_settings()
    return jsonify(ok=True, result=triage.check_gemini())


@app.get("/api/profile")
def get_profile():
    p = profiles.current().profile_path
    if p.exists():
        return jsonify(ok=True, text=p.read_text(encoding="utf-8"), from_template=False)
    tmpl = PROFILE_EXAMPLE.read_text(encoding="utf-8") if PROFILE_EXAMPLE.exists() else ""
    return jsonify(ok=True, text=tmpl, from_template=True)


@app.post("/api/profile")
def save_profile():
    text = (request.get_json(force=True) or {}).get("text", "")
    if len(text.strip()) < 50:
        raise UserError("The profile looks empty: describe your experience and target roles")
    path = profiles.current().profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return jsonify(ok=True)


@app.post("/api/claude/login")
def claude_login():
    exe = triage.CLAUDE_EXE
    if exe.exists():
        cmd = f"& {_ps_str(str(exe))} auth login"
    else:  # official installer, then login
        cmd = ("irm https://claude.ai/install.ps1 | iex; "
               f"& {_ps_str(str(exe))} auth login")
    subprocess.Popen(["powershell", "-NoExit", "-Command", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
    return jsonify(ok=True)


def _for_viewer(state: dict) -> dict:
    """A user sees only their own profiles' times, nothing about the background tasks."""
    if is_user():
        own = set(g.account["profiles"])
        state = dict(state, profiles=[r for r in state["profiles"] if r["id"] in own], catchup=None)
    return state


@app.post("/api/claude/token")
def claude_token():
    """Opens a window running `claude setup-token`: a browser login, then a yearly token to copy."""
    exe = triage.CLAUDE_EXE
    if not exe.exists():
        raise UserError("Claude Code is not installed: click Log in first")
    cmd = (f"& {_ps_str(str(exe))} setup-token; "
           "Write-Host ''; Write-Host 'Copy the whole token above (it starts with sk-ant-), paste it in the dashboard, AI page, then close this window.'")
    subprocess.Popen(["powershell", "-NoExit", "-Command", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
    return jsonify(ok=True)


@app.get("/api/schedule")
def get_schedule():
    return jsonify(ok=True, **_for_viewer(schedule_state()))


@app.post("/api/schedule")
def save_schedule():
    """The selected profile's message time."""
    at = str((request.get_json(force=True) or {}).get("time", "")).strip()
    if not HHMM.fullmatch(at):
        raise UserError("Time must be HH:MM")
    if not PYTHONW.exists():
        raise UserError(f"Virtual environment not found: {PYTHONW}")
    data = profiles.load()
    pid = profiles.current().id
    next(x for x in data["profiles"] if x.id == pid).send_time = at
    profiles.save(data)
    ensure_profile_tasks()
    return jsonify(ok=True, **_for_viewer(schedule_state()))


@app.post("/api/schedule/catchup")
def save_catchup():
    """The backup click check, shared by every profile (when the always-on dashboard is not running)."""
    body = request.get_json(force=True) or {}
    start = str(body.get("start", "07:40")).strip()
    if not HHMM.fullmatch(start):
        raise UserError("Time must be HH:MM")
    hours = float(body.get("hours", 2.85))
    if not 0.25 <= hours <= 23:
        raise UserError("The click check window must be between 0.25 and 23 hours")
    name = f"{TASK_PREFIX}-CatchUp"
    for t in list_tasks():
        if t["kind"] == "catch-up" and t["name"] != name:
            schedule_task(t["name"], remove=True)
    schedule_task(name, "-m jobalerts.run --collect-only", start, every_minutes=10, for_hours=hours, limit_minutes=5)
    return jsonify(ok=True, **schedule_state())


@app.post("/api/tasks/remove")
def remove_task():
    name = str((request.get_json(force=True) or {}).get("name", ""))
    match = [t for t in list_tasks() if t["name"] == name]
    if not match:
        raise UserError("Only tasks of this tool, in this folder, can be removed here")
    powershell(f"Unregister-ScheduledTask -TaskName {_ps_str(name)} -TaskPath {_ps_str(match[0]['path'])} -Confirm:$false")
    return jsonify(ok=True)


@app.post("/api/run")
def start_run():
    mode = (request.get_json(force=True) or {}).get("mode", "full")
    if _run["proc"] is not None and _run["proc"].poll() is None:
        raise UserError("A run is already in progress")
    args = [str(PYTHONW if PYTHONW.exists() else sys.executable), "-m", "jobalerts.run"]
    if is_user():  # a user runs only their own profile
        if mode != "full":
            raise UserError("Only the admin can do this")
        args += ["--profile", profiles.current().id]
    if mode == "collect":
        args.append("--collect-only")
    _run.update(proc=subprocess.Popen(args, cwd=str(ROOT)), mode=mode,
                started=datetime.now().strftime("%H:%M:%S"))
    return jsonify(ok=True)


@app.get("/api/run")
def run_state():
    p = _run["proc"]
    running = p is not None and p.poll() is None
    return jsonify(ok=True, running=running, mode=_run["mode"], started=_run["started"],
                   exit_code=None if (p is None or running) else p.returncode,
                   log=[] if is_user() else log_tail(40))  # the log is about everyone


# ---------- accounts ----------

@app.get("/api/account")
def get_account():
    a = g.account or {}
    return jsonify(ok=True, role=a.get("role"), username=a.get("username"), must_change=bool(a.get("must_change")))


@app.post("/api/account")
def change_my_password():
    """A user changes their own password (the admin's is on the Phone access page)."""
    if not is_user():
        raise UserError("The admin password is changed on the Phone access page")
    body = request.get_json(force=True) or {}
    u = users.get(g.account["id"])
    if not u.check(str(body.get("current", ""))):
        raise UserError("The current password is wrong")
    try:
        u = users.update(u.id, password=str(body.get("new", "")), by_admin=False)
    except ValueError as e:
        raise UserError(str(e)) from None
    session["auth"] = _fingerprint(u.password_hash)  # stay logged in here; other devices are logged out
    return jsonify(ok=True)


@app.get("/api/bots/overview")
def bots_overview():
    """Every Telegram bot: who owns it, its state, its profiles and the users of those profiles."""
    all_users = users.load()
    names = {u.id: u.username for u in all_users}
    notices = (notice_bots() or [None])[0]
    con = db.connect()
    try:
        rows = []
        for b in profiles.all_bots():
            used_by = profiles.profiles_of(b)
            ids = {p.id for p in used_by}
            handle = ""
            if b.token:
                try:
                    handle = telegram.api("getMe", _timeout=5, bot=b).get("username") or ""
                except Exception:
                    handle = ""
            rows.append(dict(
                id=b.id, name=b.name or b.id, handle=handle,
                owner="admin" if b.admin_owned else names.get(b.owner, b.owner),
                admin=b.admin_owned, token=bool(b.token), chat=bool(str(b.chat_id).strip()),
                listening=listener.active(con, b.id), notices=bool(notices and notices.id == b.id),
                profiles=[dict(id=p.id, name=p.name or p.id) for p in used_by],
                users=sorted({u.username for u in all_users if ids & set(u.profile_ids())}),
            ))
    finally:
        con.close()
    no_bot = [dict(id=p.id, name=p.name or p.id) for p in profiles.all_profiles() if not profiles.get_bot(p.bot)]
    return jsonify(ok=True, bots=rows, no_bot=no_bot)


@app.get("/api/users")
def list_users():
    return jsonify(ok=True, users=[u.public() for u in users.load()],
                   profiles=[dict(id=p.id, name=p.name or p.id) for p in profiles.all_profiles()])


@app.post("/api/users")
def create_user():
    body = request.get_json(force=True) or {}
    try:
        u = users.create(str(body.get("username", "")), str(body.get("password", "")), body.get("profiles") or [])
    except ValueError as e:
        raise UserError(str(e)) from None
    return jsonify(ok=True, user=u.public())


@app.post("/api/users/update")
def update_user():
    body = request.get_json(force=True) or {}
    try:
        u = users.update(str(body.get("id", "")), profile_ids=body.get("profiles"),
                         password=str(body["password"]) if body.get("password") else None)
    except ValueError as e:
        raise UserError(str(e)) from None
    return jsonify(ok=True, user=u.public())


@app.post("/api/users/delete")
def delete_user():
    try:
        users.delete(str((request.get_json(force=True) or {}).get("id", "")))
    except ValueError as e:
        raise UserError(str(e)) from None
    return jsonify(ok=True)


@app.get("/api/history")
def history():
    con = db.connect()
    rows = con.execute(
        "SELECT a.uuid, a.title, a.employer, a.municipal, m.score, m.summary, a.source, a.url, m.sent_at, m.norwegian_required, "
        "(SELECT decision FROM decisions WHERE uuid=a.uuid AND profile_id=:p ORDER BY id DESC LIMIT 1) AS decision, "
        "(SELECT group_concat(text, ' / ') FROM notes WHERE uuid=a.uuid AND profile_id=:p) AS notes "
        "FROM matches m JOIN ads a ON a.uuid=m.uuid WHERE m.profile_id=:p AND m.sent_at IS NOT NULL "
        "ORDER BY m.sent_at DESC LIMIT 100",
        {"p": profiles.current().id},
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["docs"] = [p.name for p in cvdocs.documents(uuid=r["uuid"])]
        out.append(d)
    return jsonify(ok=True, rows=out)


def _free_port(preferred: int) -> int:
    """The preferred port if nothing uses it, otherwise any free port."""
    for port in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("No free local port found")


def _is_dashboard(port: int) -> bool:
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/manifest.webmanifest", timeout=2)
        return r.ok and "Job Alerts" in r.text
    except Exception:
        return False


def main(argv=None) -> int:
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--service", action="store_true", help="always-on mode: fixed port, no window, no browser")
    args = ap.parse_args(argv)
    if args.service:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(config.LOG_DIR / "dashboard.log", "a", encoding="utf-8", buffering=1)
        PORT = service_port()
        if _free_port(PORT) != PORT:
            print(f"{datetime.now():%Y-%m-%d %H:%M} port {PORT} already in use: not starting")
            return 1
        print(f"{datetime.now():%Y-%m-%d %H:%M} dashboard service on http://127.0.0.1:{PORT}/")
        import logging
        logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no line per request
        listener.Listener().start()  # Telegram taps, acknowledged within seconds
    else:
        if not args.no_browser and _is_dashboard(service_port()):  # the always-on dashboard is running
            webbrowser.open(f"http://127.0.0.1:{service_port()}/")
            print("The always-on dashboard is already running: opened it in your browser.")
            return 0
        PORT = _free_port(args.port)
        url = f"http://127.0.0.1:{PORT}/"
        print(f"Job alerts dashboard: {url}\nKeep this window open while you use it; close it to stop.")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, (url,)).start()
    ensure_secret_key()  # Flask opens the session before any request handler runs
    if args.service:
        try:
            from waitress import serve  # sturdier than Flask's development server when public
            serve(app, host="127.0.0.1", port=PORT, threads=8, ident="")
            return 0
        except ImportError:
            print("waitress not installed: using Flask's server (pip install -r requirements.txt)")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
