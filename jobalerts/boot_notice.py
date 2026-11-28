"""Telegram notice to the admin when the computer restarts (e.g. after a power cut).

Run at Windows startup, before anyone logs in, by the task <Prefix>-BootNotice (as SYSTEM, see
setup_boot_notice.ps1). It tells the admin the computer is back, whether the shutdown was
unexpected, then whether the always-on dashboard (which needs a Windows login) is running:
"back up" as soon as it answers, or "log in" if it is still down after a few minutes.

    python -m jobalerts.boot_notice          # what the startup task runs
    python -m jobalerts.boot_notice --test   # send the notices now, to check the setup
"""
import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import requests

from . import config, profiles, telegram, texts
from .listener import safe as telegram_safe

log = logging.getLogger("boot_notice")

NETWORK_WAIT_S = 15 * 60  # Telegram may be unreachable for a while after a power cut
LOGIN_GRACE_S = 5 * 60    # automatic login normally starts the dashboard well within this
WATCH_S = 3 * 3600        # after the "log in" notice, report when the dashboard is back


def boot_time() -> datetime:
    uptime_ms = ctypes.windll.kernel32.GetTickCount64() if sys.platform == "win32" else 0
    return datetime.now() - timedelta(milliseconds=uptime_ms)


def unexpected_shutdown(boot: datetime) -> bool:
    """Windows logs Kernel-Power event 41 at startup when it was not shut down cleanly."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-WinEvent -MaxEvents 1 -FilterHashtable @{LogName='System'; "
             "ProviderName='Microsoft-Windows-Kernel-Power'; Id=41} -ErrorAction Stop).TimeCreated.ToString('o')"],
            capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        when = datetime.fromisoformat(out[:26]) if out else None
    except Exception:
        return False
    return bool(when) and abs((when - boot).total_seconds()) < 15 * 60


def dashboard_up() -> bool:
    try:
        port = int(os.getenv("DASHBOARD_PORT", "").strip() or 8787)
        return requests.get(f"http://127.0.0.1:{port}/manifest.webmanifest", timeout=5).ok
    except Exception:
        return False


def admin_bot():
    bots = profiles.admin_bots()
    return bots[0] if bots else None


def run(send, is_up, sleep=time.sleep, clock=time.monotonic, boot=None, unexpected=False) -> list[str]:
    """The notices, in order; `send` raises while Telegram is unreachable. Returns what was sent."""
    sent = []
    boot = boot or datetime.now()
    first = texts.t("boot_unexpected" if unexpected else "boot_restarted", time=f"{boot:%d.%m %H:%M}")
    start = clock()
    while True:  # wait for the network
        try:
            send(first)
            sent.append(first)
            break
        except Exception as e:
            if clock() - start > NETWORK_WAIT_S:
                log.error("Telegram unreachable for %d min: %s", NETWORK_WAIT_S // 60, telegram_safe(str(e)))
                return sent
            sleep(20)
    deadline, warned = clock() + LOGIN_GRACE_S, False
    while True:
        if is_up():
            _try(send, texts.t("boot_back", time=f"{datetime.now():%H:%M}"), sent)
            return sent
        if clock() >= deadline:
            if warned:
                return sent
            _try(send, texts.t("boot_login", minutes=LOGIN_GRACE_S // 60), sent)
            deadline, warned = clock() + WATCH_S, True
        sleep(15)


def _try(send, text: str, sent: list):
    try:
        send(text)
        sent.append(text)
    except Exception as e:
        log.error("Telegram: %s", telegram_safe(str(e)))

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send the notices now")
    args = ap.parse_args(argv)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=config.LOG_DIR / "boot.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = admin_bot()
    if bot is None:
        log.error("No admin Telegram bot with a token and a chat: nothing sent")
        return 1
    owner = next(iter(profiles.profiles_of(bot)), None)
    with profiles.use(owner or profiles.all_profiles()[0]):  # the admin's Telegram language
        boot = boot_time()
        unexpected = unexpected_shutdown(boot)
        log.info("Started (boot %s, unexpected=%s, test=%s)", f"{boot:%Y-%m-%d %H:%M}", unexpected, args.test)
        sent = run(lambda text: telegram.send_text(("🧪 " if args.test else "") + text, bot=bot),
                   dashboard_up, boot=boot, unexpected=unexpected)
    log.info("Sent %d notice(s)", len(sent))
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
