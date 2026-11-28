"""Telegram notice to the admin when the computer restarts."""
from datetime import datetime

from jobalerts import boot_notice, profiles


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_back_up_as_soon_as_the_dashboard_answers():
    clock, sent = Clock(), []
    ups = iter([False, False, True])
    out = boot_notice.run(sent.append, lambda: next(ups), clock.sleep, clock,
                          boot=datetime(2026, 11, 28, 3, 12), unexpected=True)
    assert out == sent and len(sent) == 2
    assert sent[0].startswith("⚡") and "28.11 03:12" in sent[0] and sent[1].startswith("✅")


def test_asks_to_log_in_then_reports_when_back():
    clock, sent = Clock(), []
    up_at = boot_notice.LOGIN_GRACE_S + 600
    boot_notice.run(sent.append, lambda: clock.t >= up_at, clock.sleep, clock, unexpected=False)
    assert [m[0] for m in sent] == ["🔌", "⚠", "✅"] and "5 minutes" in sent[1]


def test_waits_for_the_network_then_gives_up_quietly():
    clock, tries = Clock(), []

    def offline(text):
        tries.append(text)
        if clock.t < 100:
            raise OSError("no network")
    sent = boot_notice.run(offline, lambda: True, clock.sleep, clock)
    assert len(tries) > 1 and len(sent) == 2  # sent once the network is back

    clock2 = Clock()

    def never(text):
        raise OSError("no network")
    assert boot_notice.run(never, lambda: True, clock2.sleep, clock2) == []
    assert clock2.t > boot_notice.NETWORK_WAIT_S


def test_stops_watching_after_a_few_hours():
    clock, sent = Clock(), []
    boot_notice.run(sent.append, lambda: False, clock.sleep, clock)
    assert len(sent) == 2 and clock.t >= boot_notice.LOGIN_GRACE_S + boot_notice.WATCH_S


def test_only_an_admin_bot_gets_it():
    data = profiles.load()
    data["bots"][0].owner = "u1"  # a user's bot
    profiles.save(data)
    assert boot_notice.admin_bot() is None
