"""One run of the pipeline.

  python -m jobalerts.run                 full run (clicks -> NAV + Finn -> sorting -> send)
  python -m jobalerts.run --collect-only  catch-up run: only reads Telegram clicks and replies
  add --console to also log to the console

On failure: full log in logs/jobalerts.log, error message on Telegram, exit code 1.
Nothing is lost: unsorted or unsent ads stay in the database and are picked up at the next
run, and the NAV window goes back to the last success.
"""
import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone

from . import config, db, finn, nav, telegram, triage
from .texts import t

LOGIN_WARN_DAYS = 5


def setup_logging(console: bool):
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "jobalerts.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def scrub(text: str) -> str:
    """Remove the Telegram token from any message (requests errors contain the URL)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return text.replace(token, "***") if token else text


def check_claude_login():
    """Warn (once a day) when the CLI subscription login expires soon."""
    if config.AI_PROVIDER != "claude":
        return
    try:
        out = subprocess.run([str(triage.CLAUDE_EXE), "auth", "status"], capture_output=True,
                             text=True, encoding="utf-8", timeout=60).stdout
        info = json.loads(out)
    except Exception as e:  # optional information: never fail for this
        logging.warning("auth status unreadable: %s", e)
        return
    exp = info.get("loginExpiresAt")
    if not info.get("loggedIn") or not exp:
        return
    left = datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)
    if left < timedelta(days=LOGIN_WARN_DAYS):
        con = db.connect()
        today = datetime.now().date().isoformat()
        if db.get_meta(con, "login_warned") != today:
            telegram.send_text(t("login_expiry", days=max(left.days, 0)))
            db.set_meta(con, "login_warned", today)


def run(collect_only: bool) -> int:
    con = db.connect()

    # 1. Clicks and replies from the last 24 h, BEFORE sorting (they feed the examples).
    telegram.collect_updates(con)
    if collect_only:
        return 0

    # 2. New NAV ads.
    since = nav.compute_since(con)
    logging.info("NAV window since %s", since.isoformat())
    new_ads, _ = nav.fetch_new_ads(con, since)
    db.set_meta(con, "nav_last_success", db.now_iso())

    # 2b. Finn via alert emails: a failure here must not block NAV or sorting.
    finn_error = None
    try:
        finn_ads, _ = finn.fetch_new_ads(con)
        new_ads += finn_ads
    except Exception as e:
        finn_error = scrub(str(e))
        logging.error("Finn failed:\n%s", scrub(traceback.format_exc()))

    # 3. Sorting: a single AI call (also picks up ads left unsorted).
    triage.triage(con)

    # 4. Send relevant ads (also picks up ads left unsent).
    sent = telegram.send_offers(con)
    if sent == 0:
        telegram.send_text(t("none_today", n=len(new_ads)))

    if finn_error:
        telegram.send_text(t("finn_failed", err=finn_error[:400]))
    check_claude_login()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--console", action="store_true", help="also log to the console")
    args = ap.parse_args(argv)
    setup_logging(args.console)
    mode = "catch-up" if args.collect_only else "full"
    logging.info("=== %s run ===", mode)
    try:
        code = run(args.collect_only)
        logging.info("=== %s run done ===", mode)
        return code
    except Exception as e:
        logging.error("%s run failed:\n%s", mode, scrub(traceback.format_exc()))
        try:
            label = t("mode_collect") if args.collect_only else t("mode_full")
            telegram.send_text(t("run_failed", mode=label, err=scrub(str(e))[:600]))
        except Exception as e2:
            logging.error("Could not send the error message: %s", scrub(str(e2)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
