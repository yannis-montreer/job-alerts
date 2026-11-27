"""Always-on Telegram listener, run inside the always-on dashboard (dashboard --service).

Telegram drops a button tap after a couple of minutes if the bot does not acknowledge it, so
the scheduled click collection (every 10 minutes) loses taps. The listener long-polls Telegram
all day: each tap is stored and acknowledged within seconds, the button shows the ✔ at once,
and the application kit for a Keep follows right away.

While it runs, it writes a heartbeat in the database; the scheduled runs then leave Telegram
to it (two readers at once would make Telegram refuse one of them).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, kit, telegram

log = logging.getLogger(__name__)
WAIT_S = 50              # long-polling wait per request
HEARTBEAT = "tg_listener_seen"
ACTIVE_FOR = timedelta(seconds=WAIT_S + 70)


def active(con) -> bool:
    """True if a listener has polled Telegram recently."""
    seen = db.get_meta(con, HEARTBEAT)
    if not seen:
        return False
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(seen) < ACTIVE_FOR
    except ValueError:
        return False


class Listener:
    def __init__(self):
        self.stop = threading.Event()
        self.kits_due = threading.Event()
        self.last_error = None
        self.last_poll = None

    def start(self):
        threading.Thread(target=self._poll_loop, name="telegram-listener", daemon=True).start()
        threading.Thread(target=self._kit_loop, name="application-kits", daemon=True).start()
        self.kits_due.set()  # kits left over from before

    def _poll_loop(self):
        con = db.connect()
        delay = 5
        while not self.stop.is_set():
            try:
                token, chat = telegram._creds()
                if not (token and chat):
                    self.stop.wait(60)  # not set up yet
                    continue
                db.set_meta(con, HEARTBEAT, db.now_iso())
                stats = telegram.collect_updates(con, wait=WAIT_S)
                self.last_poll, self.last_error, delay = db.now_iso(), None, 5
                if stats.get("decisions"):
                    self.kits_due.set()
            except Exception as e:
                self.last_error = str(e)[:300]
                log.error("Telegram listener: %s", telegram_safe(self.last_error))
                self.stop.wait(delay)
                delay = min(delay * 2, 300)

    def _kit_loop(self):
        con = db.connect()
        while not self.stop.is_set():
            # Kits right after a Keep, and a retry every 30 minutes for failed ones.
            self.kits_due.wait(1800)
            self.kits_due.clear()
            try:
                kit.send_kits(con)
            except Exception as e:
                log.error("Application kits: %s", telegram_safe(str(e)[:300]))


def telegram_safe(text: str) -> str:
    token, _ = telegram._creds()
    return text.replace(token, "***") if token else text


def run_forever():  # python -m jobalerts.listener (for testing without the dashboard)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    listener = Listener()
    listener.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    from . import config  # noqa: F401  (loads .env)
    run_forever()
