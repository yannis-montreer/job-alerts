"""One run of the pipeline.

  python -m jobalerts.run                 full run (clicks -> NAV, Finn, LinkedIn -> sorting -> send)
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
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import claude_auth, config, db, finn, kit, linkedin, listener, nav, profiles, telegram, triage
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
    """Warn the admin before the Claude login or token expires (see claude_auth)."""
    try:
        con = db.connect()
        try:
            claude_auth.check_expiry(con)
        finally:
            con.close()
    except Exception as e:  # optional information: never fail for this
        logging.warning("Claude login check failed: %s", e)


def collect_clicks(con):
    """Clicks and replies of every bot, BEFORE sorting (they feed the examples). A bot that the
    always-on dashboard listens to is left to it: two readers at once would make Telegram
    refuse one of them (and the listener already sends the kits)."""
    all_profiles = profiles.all_profiles()
    for bot in profiles.all_bots():
        owners = [p for p in all_profiles if p.bot == bot.id]
        if not owners:
            continue
        if listener.active(con, bot.id):
            logging.info("Telegram clicks (bot %s): handled by the always-on listener", bot.id)
            continue
        try:
            telegram.collect_updates(con, bot=bot)
        except Exception:
            logging.error("Telegram clicks (bot %s) failed:\n%s", bot.id, scrub(traceback.format_exc()))
            continue
        for p in owners:
            with profiles.use(p):
                send_kits(con)


def run_profile(con, p) -> int:
    """Sort and send for one profile. Returns how many ads it analysed."""
    with profiles.use(p):
        analysed = triage.triage(con)
        sent = telegram.send_offers(con)
        if sent == 0:
            telegram.send_text(t("none_today", n=analysed))
        return analysed


def run(collect_only: bool, profile_id: str | None = None) -> int:
    """collect_only: only Telegram clicks. profile_id: sort and send for this profile only
    (each profile has its own scheduled time); new ads are still fetched for every profile."""
    con = db.connect()

    # 1. Clicks and replies, for every bot.
    collect_clicks(con)
    if collect_only:
        return 0

    # 2. New ads, fetched once for all profiles; each ad is handed to the profiles it may suit.
    if config.SOURCE_NAV:
        since = nav.compute_since(con)
        logging.info("NAV window since %s", since.isoformat())
        nav.fetch_new_ads(con, since)
        db.set_meta(con, "nav_last_success", db.now_iso())

    # 2b. Alert-email sources: a failure here must not block the other sources or sorting.
    source_errors = []
    for name, module in (("Finn", finn), ("LinkedIn", linkedin)):
        try:
            module.fetch_new_ads(con)
        except Exception as e:
            source_errors.append((name, scrub(str(e))))
            logging.error("%s failed:\n%s", name, scrub(traceback.format_exc()))

    # 3-4. Per profile: one AI call to sort its new ads, then send them with its bot.
    #      A profile that fails (missing profile text, bot not set up...) does not stop the others.
    failed = []
    selected = [p for p in profiles.all_profiles() if profile_id in (None, p.id)]
    if profile_id and not selected:
        raise RuntimeError(f"Unknown profile: {profile_id}")
    for p in selected:
        missing = profiles.issue(p)
        if missing:  # a profile being set up in the dashboard: not an error
            logging.info("Profile %s skipped: %s", p.name or p.id, missing)
            continue
        try:
            run_profile(con, p)
        except Exception as e:
            failed.append(p)
            logging.error("Profile %s failed:\n%s", p.name or p.id, scrub(traceback.format_exc()))
            claude_auth.report_failure(con, str(e))
            try:
                with profiles.use(p):
                    telegram.send_text(t("run_failed", mode=p.name or p.id, err=scrub(str(e))[:600]))
            except Exception:
                pass

    # Source errors and the login warning: once per bot.
    ready = [p for p in selected if not profiles.issue(p)]
    for bot_id in dict.fromkeys(p.bot for p in ready):
        owner = next(p for p in ready if p.bot == bot_id)
        try:
            with profiles.use(owner):
                for name, err in source_errors:
                    telegram.send_text(t("source_failed", source=name, err=err[:400]))
        except Exception:
            logging.error("Could not send the source errors (bot %s)", bot_id)
    check_claude_login()
    return 1 if failed else 0


def send_kits(con):
    """Application kits for new Keep clicks. A failure here never stops the run."""
    try:
        kit.send_kits(con)
    except Exception:
        logging.error("Application kits failed:\n%s", scrub(traceback.format_exc()))


class RunLock:
    """One full run at a time: profiles scheduled at the same minute run one after the other
    (sources are fetched once, Finn pages are never requested twice)."""
    def __init__(self, wait_s: int = 1500, poll_s: float = 5):
        self.path = config.ROOT / "data" / "run.lock"
        self.wait_s, self.poll_s = wait_s, poll_s
        self.fh = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.time() + self.wait_s
        while True:
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("Another run has been going on for too long")
                time.sleep(self.poll_s)

    def __exit__(self, *exc):
        import msvcrt
        try:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.fh.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--console", action="store_true", help="also log to the console")
    ap.add_argument("--profile", help="sort and send for this profile only (e.g. p2)")
    args = ap.parse_args(argv)
    setup_logging(args.console)
    mode = "catch-up" if args.collect_only else ("full" + (f" {args.profile}" if args.profile else ""))
    logging.info("=== %s run ===", mode)
    try:
        if args.collect_only:
            code = run(True)
        else:
            with RunLock():
                code = run(False, args.profile)
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
