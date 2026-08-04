"""Current weather for the companion's place — ambient context, like the time.

Uses **Open-Meteo**: free, no API key, no signup. Two endpoints — a geocoder
(city name → lat/lon) and a current-conditions forecast. We send only a *city*
(never device GPS), and the call is a plain read (no consent gate), degrading
silently to None on any hiccup so weather is seasoning, never a failure point.

The WMO weather code maps to both a human description (for conversation) and a
lighting/scene cue (for image prompts, so a rainy selfie actually looks rainy).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass

import httpx

from ..clock import now_utc
from ..log import get_logger

_GEO = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8.0
_log = get_logger("weather")

# Cache (in app_state) + place resolution live here, so weather owns its whole domain and
# both the chat header and image prompts share one implementation.
_CACHE_KEY = "weather_cache"
_TTL_MINUTES = 30
_FAIL_TTL_MINUTES = 5  # a failed lookup retries soon, not after a full success-TTL
# Place keys/relationships ranked by how "current" they are (lower = more current), so
# weather follows where the companion lives NOW, not an older hometown learned first.
_PLACE_ATTR_TIER = {"city": 0, "residence": 0, "location": 0, "town": 1, "hometown": 2}
_PLACE_REL_TIER = {"lives_in": 0, "resides_in": 0, "based_in": 1, "located_in": 2}

# WMO weather code → (short description, image lighting/scene cue). Grouped to the
# bands that actually read differently in a picture; unknown codes fall back to "".
_WMO: dict[int, tuple[str, str]] = {
    0: ("clear sky", "bright, clear light"),
    1: ("mainly clear", "bright, mostly clear light"),
    2: ("partly cloudy", "soft, partly-clouded light"),
    3: ("overcast", "flat, soft grey overcast light"),
    45: ("foggy", "soft, hazy fog"),
    48: ("freezing fog", "cold, icy haze"),
    51: ("light drizzle", "damp, soft grey light"),
    53: ("drizzle", "damp, grey light"),
    55: ("heavy drizzle", "wet, grey light"),
    56: ("freezing drizzle", "cold, wet grey light"),
    57: ("freezing drizzle", "cold, wet grey light"),
    61: ("light rain", "soft rain, wet light, a few drops on the window"),
    63: ("rain", "steady rain, grey wet light"),
    65: ("heavy rain", "heavy rain, dark wet light"),
    66: ("freezing rain", "icy rain, cold wet light"),
    67: ("freezing rain", "icy rain, cold wet light"),
    71: ("light snow", "soft snow falling, pale wintry light"),
    73: ("snow", "snow falling, soft wintry light"),
    75: ("heavy snow", "heavy snowfall, pale wintry light"),
    77: ("snow grains", "fine snow, pale wintry light"),
    80: ("rain showers", "passing rain showers, wet light"),
    81: ("rain showers", "rain showers, wet grey light"),
    82: ("heavy rain showers", "heavy showers, dark wet light"),
    85: ("snow showers", "snow showers, pale wintry light"),
    86: ("snow showers", "snow showers, pale wintry light"),
    95: ("a thunderstorm", "stormy, dramatic dark light"),
    96: ("a thunderstorm with hail", "stormy, dramatic dark light"),
    99: ("a thunderstorm with hail", "stormy, dramatic dark light"),
}


@dataclass
class Weather:
    place: str
    temp_c: float | None
    description: str  # e.g. "light rain"
    cue: str  # an image lighting/scene cue, e.g. "soft rain, wet light"
    is_day: bool

    def phrase(self) -> str:
        """A compact human line for conversation, e.g. 'light rain, 14°C'."""
        if self.temp_c is None:
            return self.description
        return f"{self.description}, {round(self.temp_c)}°C"


async def geocode(name: str) -> tuple[float, float, str] | None:
    """City name → (lat, lon, canonical place name), or None if not found/unreachable."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            r = await http.get(_GEO, params={"name": name, "count": 1})
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
    except (httpx.HTTPError, ValueError):
        _log.warning("geocode failed for %r", name, exc_info=True)
        return None
    if not results:
        return None
    top = results[0]
    label = ", ".join(p for p in (top.get("name"), top.get("country")) if p) or name
    return float(top["latitude"]), float(top["longitude"]), label


async def fetch_current(lat: float, lon: float, place: str) -> Weather | None:
    """Current conditions at a coordinate, or None on any failure (caller degrades)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            r = await http.get(_FORECAST, params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,is_day",
            })
            r.raise_for_status()
            cur = (r.json() or {}).get("current") or {}
    except (httpx.HTTPError, ValueError):
        _log.warning("weather fetch failed for %s", place, exc_info=True)
        return None
    code = int(cur.get("weather_code", -1))
    desc, cue = _WMO.get(code, ("", ""))
    if not desc:
        return None  # unknown/empty → no usable weather, stay silent
    temp = cur.get("temperature_2m")
    return Weather(
        place=place,
        temp_c=float(temp) if temp is not None else None,
        description=desc,
        cue=cue,
        is_day=bool(cur.get("is_day", 1)),
    )


async def resolve_place(store, config, companion_id: str) -> str | None:
    """The companion's city: the configured location first, else the most *current* place
    Moko has learned (a current-residence key beats a hometown; latest-learned wins within a
    tier), else a place they live in, else None — 'config, else learned, else off'."""
    if (config.location_name or "").strip():
        return config.location_name.strip()
    # Attributes come oldest-first; reverse → latest-first, then a *stable* sort by tier
    # keeps the latest within the most-current tier at the front.
    attrs = [a for a in await store.attributes_for(companion_id)
             if a.key in _PLACE_ATTR_TIER and (a.value or "").strip()]
    attrs.reverse()
    attrs.sort(key=lambda a: _PLACE_ATTR_TIER[a.key])
    if attrs:
        return attrs[0].value.strip()
    rels = [r for r in await store.relationships_for(companion_id) if r.type in _PLACE_REL_TIER]
    rels.sort(key=lambda r: _PLACE_REL_TIER[r.type])  # lives_in beats located_in
    for r in rels:
        other = await store.get_subject(r.to_id if r.from_id == companion_id else r.from_id)
        if other is not None and other.kind == "place":
            return other.name
    return None


async def current(store, config, companion_id: str) -> Weather | None:
    """Cached current weather for the companion's place, refreshed ~every 30 min (a failed
    lookup retries in ~5). None when there's no place or the lookup fails — weather is
    ambient seasoning, never required. Owns its own app_state cache."""
    place = await resolve_place(store, config, companion_id)
    if not place:
        return None
    cached = None
    raw = await store.get_state(_CACHE_KEY)
    if raw:
        try:
            cached = json.loads(raw)
        except (ValueError, TypeError):
            cached = None
    # Serve a fresh cache for the same place; a failed lookup (w is None) gets a short TTL so
    # one blip doesn't suppress weather for the full success window.
    if cached and cached.get("place") == place:
        try:
            age = (now_utc() - dt.datetime.fromisoformat(cached["at"])).total_seconds()
        except (ValueError, TypeError, KeyError):
            age = None
        if age is not None:
            w = cached.get("w")
            ttl = _TTL_MINUTES if w else _FAIL_TTL_MINUTES
            if age < ttl * 60:
                return Weather(**w) if w else None
    geo = await geocode(place)
    w = await fetch_current(*geo) if geo else None
    entry = {"at": now_utc().isoformat(), "place": place,
             "w": dataclasses.asdict(w) if w else None}
    try:
        await store.set_state(_CACHE_KEY, json.dumps(entry))
    except Exception:
        _log.debug("failed to cache weather", exc_info=True)
    return w
