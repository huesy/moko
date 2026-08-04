"""System-timezone detection that pre-selects the wizard's (filterable) zone list."""

from zoneinfo import available_timezones

from moko.config import regions


def test_detect_timezone_is_valid_or_none():
    tz = regions.detect_timezone()
    assert tz is None or tz in available_timezones()


def test_detect_timezone_reads_the_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert regions.detect_timezone() == "Europe/Paris"


def test_detect_timezone_ignores_a_bogus_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Not/AZone")
    tz = regions.detect_timezone()
    assert tz is None or tz in available_timezones()  # falls back to /etc, never the junk
