"""Unit tests for the feeding-window scheduler logic.

Covers the time/timezone/day-of-week math without touching the DB or USB —
the goal is to prove that "Active + Mon..Sun + HH:MM" really does decide
open/closed correctly for a given wall-clock instant.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.scheduler import (
    _any_window_open,
    _is_inside_window,
    _send_food_gate,
)


def _w(start="14:39", end="14:40", days="0,1,2,3,4,5,6",
       tz="Europe/Brussels", active=True):
    """Build a duck-typed ScheduleWindow stand-in (only the attrs the
    scheduler reads — no DB session needed)."""
    return SimpleNamespace(
        start_local=start, end_local=end, days_of_week=days,
        timezone=tz, active=active,
    )


def _at(year, month, day, hour, minute, tz="Europe/Brussels"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)) \
        .astimezone(ZoneInfo("UTC"))


def test_inside_window_at_start_minute():
    # At 14:39 sharp, the 14:39–14:40 window is open.
    assert _is_inside_window(_w(), _at(2026, 6, 11, 14, 39)) is True


def test_outside_window_before_start():
    assert _is_inside_window(_w(), _at(2026, 6, 11, 14, 38)) is False


def test_outside_window_at_end_minute():
    # End is exclusive: at 14:40 sharp, the 14:39–14:40 window is closed.
    assert _is_inside_window(_w(), _at(2026, 6, 11, 14, 40)) is False


def test_outside_window_after_end():
    assert _is_inside_window(_w(), _at(2026, 6, 11, 15, 0)) is False


def test_day_of_week_filter():
    # Window only on Mondays (0). 2026-06-11 is Thursday (weekday=3).
    w = _w(days="0")
    assert _is_inside_window(w, _at(2026, 6, 11, 14, 39)) is False
    # 2026-06-08 is Monday.
    assert _is_inside_window(w, _at(2026, 6, 8, 14, 39)) is True


def test_inactive_window_never_opens():
    assert _is_inside_window(_w(active=False), _at(2026, 6, 11, 14, 39)) is False


def test_invalid_end_before_start_treated_closed():
    # The UI doesn't support cross-midnight windows; we treat them as closed.
    assert _is_inside_window(_w(start="14:40", end="14:39"),
                             _at(2026, 6, 11, 14, 40)) is False


def test_timezone_respected():
    # Window 14:39 local in Europe/Brussels === 12:39 UTC === 08:39 New York.
    # If "now" is 08:39 New York local (=== 12:39 UTC), Brussels clock is 14:39
    # so a Brussels-tz window should be OPEN.
    now_utc = _at(2026, 6, 11, 8, 39, tz="America/New_York")
    assert _is_inside_window(_w(tz="Europe/Brussels"), now_utc) is True
    # Same instant against a New_York-tz window with the same HH:MM is CLOSED.
    assert _is_inside_window(_w(tz="America/New_York"), now_utc) is False


def test_any_window_open_is_or_of_windows():
    w_open = _w(start="14:39", end="14:40")
    w_shut = _w(start="20:00", end="20:01")
    now = _at(2026, 6, 11, 14, 39)
    assert _any_window_open([w_shut, w_open], now) is True
    assert _any_window_open([w_shut], now) is False
    assert _any_window_open([], now) is False


def test_send_food_gate_writes_valid_command(tmp_path, monkeypatch):
    # Redirect the bridge command path to a tmp file and confirm we write a
    # JSON line of the exact shape the firmware expects.
    cmd_path = tmp_path / "cmd"
    import app.services.scheduler as scheduler_mod
    monkeypatch.setattr(scheduler_mod, "CMD_FILE", str(cmd_path))

    assert _send_food_gate(90) is True
    written = cmd_path.read_text().strip()
    assert written == '{"servo": "food", "angle": 90}'

    assert _send_food_gate(0) is True
    assert cmd_path.read_text().strip() == '{"servo": "food", "angle": 0}'
