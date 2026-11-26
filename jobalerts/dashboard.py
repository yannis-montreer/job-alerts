"""Local setup and status dashboard.

  python -m jobalerts.dashboard            opens http://127.0.0.1:8765 in your browser
                                           (or a free port if 8765 is taken)
  python -m jobalerts.dashboard --port N   prefer another port

It only listens on 127.0.0.1 (this computer). Every API call must carry a random token that
is generated at start and embedded in the page, and the Host header is checked, so other
websites open in your browser cannot use it. Close the console window to stop it.
"""
import argparse
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
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from flask import Flask, abort, jsonify, request

from . import config, finn, linkedin, preferences, telegram, triage

ROOT = config.ROOT
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PROFILE_EXAMPLE = ROOT / "profile.example.md"
PAGE = Path(__file__).with_name("dashboard.html")
LOG_FILE = config.LOG_DIR / "jobalerts.log"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_PORT = 8765

SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD", "NAV_TOKEN", "GEMINI_API_KEY"}
PLAIN_KEYS = {"CANDIDATE_NAME", "LANGUAGE", "TELEGRAM_CHAT_ID", "GMAIL_ADDRESS", "GMAIL_FINN_LABEL",
              "TARGET_MUNICIPALITIES", "TARGET_COUNTIES", "AI_PROVIDER", "GEMINI_MODEL",
              "FINN_FETCH_PAGES", "FINN_CONTACT_EMAIL",
              "NORWEGIAN_REQUIRED", "MESSAGE_MIN_SCORE", "DIGEST_MIN_SCORE",
              "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN", "GMAIL_LINKEDIN_LABEL"}
BOOL_KEYS = {"FINN_FETCH_PAGES", "SOURCE_NAV", "SOURCE_FINN", "SOURCE_LINKEDIN"}
AREA_GROUPS = {
    "Oslo": ["OSLO"],
    "Akershus": sorted(config.AKERSHUS),
    "Nearby": sorted(config.NEARBY),
}
DEFAULT_MUNICIPALITIES = {"OSLO"} | config.AKERSHUS | config.NEARBY
DEFAULT_COUNTIES = {"OSLO", "AKERSHUS"}

app = Flask(__name__)
TOKEN = secrets.token_urlsafe(24)
PORT = DEFAULT_PORT
_run = {"proc": None, "mode": None, "started": None}


# ---------- security: local only, token required ----------

@app.before_request
def _guard():
    if request.host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        abort(403)  # blocks DNS-rebinding tricks from other websites
    if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
        abort(403)


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
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
    return html.replace("{{TOKEN}}", TOKEN), 200, {"Content-Type": "text/html; charset=utf-8"}


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
    if "LANGUAGE" in updates and updates["LANGUAGE"] not in ("en", "fr"):
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
    """Every ad Sven clicked or replied to, newest first (the examples the AI learns from)."""
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


def main(argv=None) -> int:
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    PORT = _free_port(args.port)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Job alerts dashboard: {url}\nKeep this window open while you use it; close it to stop.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
