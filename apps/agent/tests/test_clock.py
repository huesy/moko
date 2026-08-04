import datetime as dt

from moko.clock import Clock, now_utc, utc_isoformat


def test_now_utc_is_timezone_aware_utc():
    n = now_utc()
    assert n.tzinfo is not None
    assert n.utcoffset() == dt.timedelta(0)


def test_utc_isoformat_roundtrips():
    parsed = dt.datetime.fromisoformat(utc_isoformat())
    assert parsed.tzinfo is not None


def test_to_local_assumes_utc_for_naive():
    clock = Clock("Asia/Tokyo")  # fixed +09:00, no DST
    naive_utc_midnight = dt.datetime(2026, 6, 22, 0, 0, 0)
    local = clock.to_local(naive_utc_midnight)
    assert local.hour == 9
    assert local.utcoffset() == dt.timedelta(hours=9)


def test_to_utc_assumes_local_for_naive():
    clock = Clock("Asia/Tokyo")
    naive_local_9am = dt.datetime(2026, 6, 22, 9, 0, 0)
    as_utc = clock.to_utc(naive_local_9am)
    assert as_utc.hour == 0
    assert as_utc.utcoffset() == dt.timedelta(0)


def test_roundtrip_local_utc():
    clock = Clock("Europe/London")
    u = now_utc()
    assert abs((clock.to_utc(clock.to_local(u)) - u).total_seconds()) < 1
