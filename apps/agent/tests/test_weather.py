"""Ambient weather (Open-Meteo) — the keyless geocode + current-conditions wire shapes,
the WMO→description/cue mapping, the 'config-else-learned' place resolver, and the cache
that keeps it to one fetch per ~30 min. Network is faked at the httpx seam; the brain-level
resolver fakes the two weather functions."""

import httpx
import pytest

import moko.tools.weather as wx
from moko.agent import Brain
from moko.config.config import Config
from moko.llm.base import LLMResponse
from moko.store import Store
from moko.tools.weather import Weather, fetch_current, geocode


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(wx.httpx, "AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))


def _cfg(**kw) -> Config:
    base = dict(companion_name="Jordan", timezone="Asia/Tokyo",
                sleep_start="23:00", sleep_end="07:00", proactivity="moderate")
    base.update(kw)
    return Config(**base)


class _LLM:
    model = "fake"

    async def converse(self, *a, **k):
        return LLMResponse("ok")


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


# ── the tool (Open-Meteo wire shapes) ───────────────────────────────────────────
async def test_geocode_then_fetch(monkeypatch):
    def handler(req):
        if "geocoding-api" in req.url.host:
            return httpx.Response(200, json={"results": [
                {"latitude": 34.65, "longitude": 133.9, "name": "Okayama", "country": "Japan"}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 14.2, "weather_code": 61, "is_day": 1}})

    _mock(monkeypatch, handler)
    geo = await geocode("Okayama")
    assert geo[0] == 34.65 and "Okayama" in geo[2] and "Japan" in geo[2]
    w = await fetch_current(*geo)
    assert w.description == "light rain" and round(w.temp_c) == 14
    assert "rain" in w.cue  # an image lighting/scene cue
    assert w.phrase() == "light rain, 14°C"  # the compact conversation line


async def test_fetch_unknown_code_is_silent(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"current": {"weather_code": 999}}))
    assert await fetch_current(1.0, 2.0, "X") is None  # unknown code → no usable weather


async def test_geocode_no_results(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    assert await geocode("Nowhereville") is None


async def test_network_failure_degrades_to_none(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500, text="down"))
    assert await geocode("Tokyo") is None
    assert await fetch_current(1.0, 2.0, "Tokyo") is None


# ── the place resolver (config, else learned, else off) ─────────────────────────
async def test_place_prefers_config(store):
    brain = Brain(store, _LLM(), _cfg(location_name="Kyoto"))
    assert await brain._companion_place() == "Kyoto"


async def test_place_falls_back_to_a_learned_city(store):
    brain = Brain(store, _LLM(), _cfg())  # no configured location
    cid, _ = await brain._ensure_actors()
    src = await store.add_source("user_stated")
    await store.set_attribute(cid, "city", "Okayama", source_id=src.id)
    assert await brain._companion_place() == "Okayama"


async def test_place_falls_back_to_a_lived_in_place(store):
    brain = Brain(store, _LLM(), _cfg())
    cid, _ = await brain._ensure_actors()
    src = await store.add_source("user_stated")
    nara = await store.add_subject("place", "Nara")
    await store.add_relationship(cid, nara.id, "lives_in", source_id=src.id)
    assert await brain._companion_place() == "Nara"


async def test_no_place_means_no_weather(store, monkeypatch):
    brain = Brain(store, _LLM(), _cfg())  # nothing configured or learned
    called = {"n": 0}

    async def fake_geo(name):
        called["n"] += 1
        return None

    monkeypatch.setattr(wx, "geocode", fake_geo)
    assert await brain._current_weather() is None
    assert called["n"] == 0  # didn't even try to geocode — no place to look up


# ── caching (one fetch per ~30 min) ─────────────────────────────────────────────
async def test_weather_is_cached(store, monkeypatch):
    brain = Brain(store, _LLM(), _cfg(location_name="Okayama"))
    calls = {"geo": 0, "fetch": 0}

    async def fake_geo(name):
        calls["geo"] += 1
        return (1.0, 2.0, "Okayama")

    async def fake_fetch(lat, lon, place):
        calls["fetch"] += 1
        return Weather("Okayama", 14.0, "light rain", "soft rain, wet light", True)

    monkeypatch.setattr(wx, "geocode", fake_geo)
    monkeypatch.setattr(wx, "fetch_current", fake_fetch)

    w1 = await brain._current_weather()
    w2 = await brain._current_weather()
    assert w1.description == w2.description == "light rain"
    assert calls["fetch"] == 1 and calls["geo"] == 1  # second call served from cache


# ── woven into the conversation header ───────────────────────────────────────────
def test_system_prompt_carries_weather():
    from moko.agent.persona import system_prompt
    from moko.store.graph import GraphSlice
    sp = system_prompt(_cfg(), GraphSlice(), now="Mon", weather="light rain, 14°C")
    assert "light rain, 14°C" in sp.text()


async def test_place_prefers_current_city_over_hometown(store):
    brain = Brain(store, _LLM(), _cfg())
    cid, _ = await brain._ensure_actors()
    src = await store.add_source("user_stated")
    await store.set_attribute(cid, "hometown", "Sapporo", source_id=src.id)
    await store.set_attribute(cid, "city", "Okayama", source_id=src.id)
    assert await brain._companion_place() == "Okayama"  # current city beats hometown


def test_scene_context_night_names_weather_not_daytime_light(store):
    brain = Brain(store, _LLM(), _cfg())
    night = Weather("Okayama", 10.0, "light rain", "soft rain, wet light", is_day=False)
    assert "weather: light rain" in brain.studio._scene_context(night)  # weather named at night
    day = Weather("Okayama", 20.0, "light rain", "soft rain, wet light", is_day=True)
    assert "soft rain, wet light" in brain.studio._scene_context(day)  # daytime cue used as-is
