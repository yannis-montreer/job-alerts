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
login with the dashboard password (a session cookie, 30 days). Never use Tailscale Funnel
for the dashboard: Funnel makes it public.
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
from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import config, finn, linkedin, preferences, telegram, texts, triage

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
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"}
TAILSCALE = [shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"]

SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD", "NAV_TOKEN", "GEMINI_API_KEY"}
PLAIN_KEYS = {"CANDIDATE_NAME", "LANGUAGE", "TELEGRAM_CHAT_ID", "GMAIL_ADDRESS", "GMAIL_FINN_LABEL",
              "TARGET_MUNICIPALITIES", "TARGET_COUNTIES", "AI_PROVIDER", "GEMINI_MODEL",
              "FINN_FETCH_PAGES", "FINN_CONTACT_EMAIL",
              "NORWEGIAN_REQUIRED", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE",
              "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN", "GMAIL_LINKEDIN_LABEL", "DASHBOARD_LANGUAGE"}
BOOL_KEYS = {"FINN_FETCH_PAGES", "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN"}
AREA_GROUPS = {
    "Oslo": ["OSLO"],
    "Akershus": sorted(config.AKERSHUS),
    "Nearby": sorted(config.NEARBY),
}
DEFAULT_MUNICIPALITIES = {"OSLO"} | config.AKERSHUS | config.NEARBY
DEFAULT_COUNTIES = {"OSLO", "AKERSHUS"}

app = Flask(__name__)
app.config.update(SESSION_COOKIE_NAME="jobalerts_session", SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
TOKEN = secrets.token_urlsafe(24)
_login_failures: list[float] = []
PORT = DEFAULT_PORT
_run = {"proc": None, "mode": None, "started": None}


# ---------- security: local, or your Tailscale address with a password; token required ----------

def service_port() -> int:
    raw = os.getenv("DASHBOARD_PORT", "").strip()
    return int(raw) if raw.isdigit() else SERVICE_PORT


def remote_host() -> str:
    return os.getenv("DASHBOARD_REMOTE_HOST", "").strip().lower()


def is_local() -> bool:
    return request.host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")


def is_remote() -> bool:
    host = remote_host()
    return bool(host) and request.host.lower().split(":")[0] == host


def password_hash() -> str:
    raw = os.getenv("DASHBOARD_PASSWORD_HASH", "").strip()
    try:
        return base64.urlsafe_b64decode(raw.encode()).decode() if raw else ""
    except ValueError:
        return ""


def _fingerprint(h: str) -> str:  # changing the password logs every phone out
    return hashlib.sha256(h.encode()).hexdigest()[:24]


def logged_in() -> bool:
    h = password_hash()
    return bool(h) and secrets.compare_digest(str(session.get("auth", "")), _fingerprint(h))


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
        if request.path not in PUBLIC_PATHS and not logged_in():
            if request.path.startswith("/api/"):
                abort(401)
            return redirect("/login")
    if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
        abort(403)


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    return resp


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
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return text.replace(tok, "***") if tok else text


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
    exe = triage.CLAUDE_EXE
    if not exe.exists():
        return dict(installed=False, logged_in=False, path=str(exe))
    try:
        out = subprocess.run([str(exe), "auth", "status"], capture_output=True, text=True,
                             encoding="utf-8", timeout=60).stdout
        info = json.loads(out)
    except Exception as e:
        return dict(installed=True, logged_in=False, error=str(e)[:200], path=str(exe))
    exp = info.get("loginExpiresAt")
    days = None
    if exp:
        days = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
    return dict(installed=True, logged_in=bool(info.get("loggedIn")), expires=exp, days_left=days,
                plan=info.get("subscriptionType"), path=str(exe))


def powershell(command: str, timeout: int = 60) -> str:
    full = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ErrorActionPreference = 'Stop'; " + command
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
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
        t["kind"] = "catch-up" if "--collect-only" in (t.get("args") or "") else "full run"
    return data


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
    if not config.DB_PATH.exists():
        return {}
    con = sqlite3.connect(config.DB_PATH)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    try:
        return dict(
            ads=q("SELECT count(*) FROM ads"),
            sorted=q("SELECT count(*) FROM ads WHERE triaged_at IS NOT NULL"),
            relevant=q("SELECT count(*) FROM ads WHERE relevant=1"),
            sent=q("SELECT count(*) FROM ads WHERE sent_at IS NOT NULL"),
            kept=q("SELECT count(DISTINCT uuid) FROM decisions d WHERE decision='keep' AND id=(SELECT MAX(id) FROM decisions WHERE uuid=d.uuid)"),
            rejected=q("SELECT count(DISTINCT uuid) FROM decisions d WHERE decision='reject' AND id=(SELECT MAX(id) FROM decisions WHERE uuid=d.uuid)"),
            last_nav=q("SELECT value FROM meta WHERE key='nav_last_success'"),
        )
    finally:
        con.close()


def profile_state() -> dict:
    p = config.PROFILE_PATH
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
    fr = json.dumps(json.loads(PAGE_FR.read_text(encoding="utf-8")), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("{{TOKEN}}", TOKEN).replace("{{UI_LANG}}", ui if ui in ("en", "fr") else "en").replace("{{FR}}", fr)
    html = html.replace("{{REMOTE}}", "true" if is_remote() else "false")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/api/settings")
def get_settings():
    env = env_values()
    munis = _split(env.get("TARGET_MUNICIPALITIES")) or DEFAULT_MUNICIPALITIES
    counties = _split(env.get("TARGET_COUNTIES")) or DEFAULT_COUNTIES
    known = {m for g in AREA_GROUPS.values() for m in g}
    return jsonify(
        ok=True,
        values={k: env.get(k, "") for k in PLAIN_KEYS},
        secrets_set={k: bool(env.get(k)) for k in SECRET_KEYS},
        area=dict(groups=AREA_GROUPS, selected=sorted(munis & known), extra=sorted(munis - known),
                  counties=sorted(counties)),
        env_exists=ENV_PATH.exists(),
    )


@app.post("/api/settings")
def save_settings():
    body = request.get_json(force=True) or {}
    updates = {}
    for k in PLAIN_KEYS - {"TARGET_MUNICIPALITIES", "TARGET_COUNTIES"}:
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
    if "area" in body:
        munis = {m.strip().upper() for m in body["area"].get("municipalities") or [] if m.strip()}
        munis |= _split(body["area"].get("extra", ""))
        counties = {c.strip().upper() for c in body["area"].get("counties") or [] if c.strip()}
        if not munis:
            raise UserError("Pick at least one municipality")
        updates["TARGET_MUNICIPALITIES"] = "" if munis == DEFAULT_MUNICIPALITIES else ",".join(sorted(munis))
        updates["TARGET_COUNTIES"] = "" if counties == DEFAULT_COUNTIES else ",".join(sorted(counties))
    write_env(updates)
    return jsonify(ok=True, saved=sorted(updates))


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
    return jsonify(
        ok=True,
        ai=dict(provider=provider, gemini_key=bool(env.get("GEMINI_API_KEY")),
                gemini_model=env.get("GEMINI_MODEL") or "gemini-flash-latest"),
        claude=claude_status(),
        telegram=dict(token=bool(env.get("TELEGRAM_BOT_TOKEN")), chat_id=bool(env.get("TELEGRAM_CHAT_ID"))),
        gmail=dict(configured=bool(env.get("GMAIL_ADDRESS") and env.get("GMAIL_APP_PASSWORD"))),
        sources=dict(nav=_flag(env, "SOURCE_NAV", True), finn=_flag(env, "SOURCE_FINN", True),
                     linkedin=_flag(env, "SOURCE_LINKEDIN", False)),
        profile=profile_state(),
        name=bool(env.get("CANDIDATE_NAME")),
        tasks=tasks, tasks_error=tasks_error,
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
    write_env(updates)
    sent, error = None, None
    if "LANGUAGE" in updates:
        try:
            telegram.send_text(texts.t("language_set"))
            sent = True
        except Exception as e:  # the language is saved anyway
            sent, error = False, telegram_safe(str(e))
    return jsonify(ok=True, telegram_sent=sent, telegram_error=error)


# ---------- phone access (Tailscale) ----------

LOGIN_TEXTS = {
    "en": dict(title="Job Alerts: log in", label="Dashboard password", button="Log in",
               wrong="Wrong password.", wait="Too many attempts: wait 10 minutes.",
               unset="No password is set yet: on the computer, open the dashboard, page Phone access."),
    "fr": dict(title="Alertes emploi : connexion", label="Mot de passe du tableau de bord", button="Se connecter",
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
<label for="pw">%(label)s</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">%(button)s</button>%(note)s</form></body></html>"""


def _login_texts() -> tuple[str, dict]:
    ui = (env_values().get("DASHBOARD_LANGUAGE") or "en").lower()
    ui = ui if ui in LOGIN_TEXTS else "en"
    return ui, LOGIN_TEXTS[ui]


def _login_page(error: str = "", status: int = 200):
    ui, tx = _login_texts()
    note = tx["unset"] if not password_hash() else error
    e = htmllib.escape
    page = LOGIN_PAGE % dict(lang=ui, title=e(tx["title"]), label=e(tx["label"]), button=e(tx["button"]),
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
    _login_failures[:] = [x for x in _login_failures if now - x < 600]
    if len(_login_failures) >= 5:
        return _login_page(tx["wait"], 429)
    h = password_hash()
    if h and check_password_hash(h, request.form.get("password", "")):
        _login_failures.clear()
        session.clear()
        session.permanent = True
        session["auth"] = _fingerprint(h)
        return redirect("/")
    _login_failures.append(now)
    time.sleep(1)
    return _login_page(tx["wrong"] if h else "", 401)


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
        if len(pw) < 8:
            raise UserError("The password needs at least 8 characters")
        updates["DASHBOARD_PASSWORD_HASH"] = base64.urlsafe_b64encode(generate_password_hash(pw).encode()).decode()
    if not updates:
        raise UserError("Nothing to save")
    write_env(updates)
    return jsonify(ok=True, **remote_state())


def _task_prefix() -> str:
    names = [t["name"] for t in list_tasks()]
    return names[0].rsplit("-", 1)[0] if names and "-" in names[0] else "JobAlerts"


@app.post("/api/remote/service")
def start_service():
    """Create (or update) the always-on dashboard task and start it."""
    if not PYTHONW.exists():
        raise UserError(f"Virtual environment not found: {PYTHONW}")
    script = ROOT / "setup_dashboard_task.ps1"
    out = powershell(f"& {_ps_str(str(script))} -Prefix {_ps_str(_task_prefix())}", timeout=60)
    return jsonify(ok=True, output=out.strip(), **remote_state())


@app.post("/api/remote/serve")
def tailscale_serve():
    """Share the always-on dashboard on this computer's Tailscale address (Serve: private, never Funnel)."""
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
    if not config.DB_PATH.exists():
        return jsonify(ok=True, rows=[])
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT a.title, a.employer, a.url, a.source, "
        "(SELECT decision FROM decisions WHERE uuid=a.uuid ORDER BY id DESC LIMIT 1) AS decision, "
        "(SELECT group_concat(text, ' / ') FROM notes WHERE uuid=a.uuid) AS notes "
        "FROM ads a WHERE EXISTS (SELECT 1 FROM decisions WHERE uuid=a.uuid) "
        "OR EXISTS (SELECT 1 FROM notes WHERE uuid=a.uuid) ORDER BY a.sent_at DESC"
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
    p = config.PROFILE_PATH
    if p.exists():
        return jsonify(ok=True, text=p.read_text(encoding="utf-8"), from_template=False)
    tmpl = PROFILE_EXAMPLE.read_text(encoding="utf-8") if PROFILE_EXAMPLE.exists() else ""
    return jsonify(ok=True, text=tmpl, from_template=True)


@app.post("/api/profile")
def save_profile():
    text = (request.get_json(force=True) or {}).get("text", "")
    if len(text.strip()) < 50:
        raise UserError("The profile looks empty: describe your experience and target roles")
    config.PROFILE_PATH.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
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


@app.post("/api/tasks")
def create_tasks():
    body = request.get_json(force=True) or {}
    times = {k: str(body.get(k, d)).strip() for k, d in (("morning", "07:30"), ("catchup_start", "07:40"))}
    for k, v in times.items():
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise UserError(f"Time must be HH:MM ({k})")
    hours = float(body.get("catchup_hours", 2.85))
    if not 0.25 <= hours <= 23:
        raise UserError("Catch-up window must be between 0.25 and 23 hours")
    prefix = str(body.get("prefix") or "JobAlerts").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", prefix):
        raise UserError("Task name prefix: letters, digits, - and _ only")
    if not PYTHONW.exists():
        raise UserError(f"Virtual environment not found: {PYTHONW}")
    script = ROOT / "setup_tasks.ps1"
    out = powershell(f"& {_ps_str(str(script))} -Prefix {_ps_str(prefix)} -MorningTime {_ps_str(times['morning'])} "
                     f"-CatchUpStart {_ps_str(times['catchup_start'])} -CatchUpHours {hours}", timeout=120)
    return jsonify(ok=True, output=out.strip())


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
                   exit_code=None if (p is None or running) else p.returncode, log=log_tail(40))


@app.get("/api/history")
def history():
    if not config.DB_PATH.exists():
        return jsonify(ok=True, rows=[])
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT a.uuid, a.title, a.employer, a.municipal, a.score, a.source, a.url, a.sent_at, a.norwegian_required, "
        "(SELECT decision FROM decisions WHERE uuid=a.uuid ORDER BY id DESC LIMIT 1) AS decision, "
        "(SELECT group_concat(text, ' / ') FROM notes WHERE uuid=a.uuid) AS notes "
        "FROM ads a WHERE a.sent_at IS NOT NULL ORDER BY a.sent_at DESC LIMIT 100"
    ).fetchall()
    con.close()
    return jsonify(ok=True, rows=[dict(r) for r in rows])


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
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
