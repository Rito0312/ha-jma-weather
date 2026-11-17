"""気象庁・Open-Meteo からの取得."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import aiohttp

from .const import (
    REQUEST_TIMEOUT,
    URL_AMEDAS_LATEST,
    URL_AMEDAS_POINT,
    URL_AMEDAS_TABLE,
    URL_AREA,
    URL_FORECAST,
    URL_OPEN_METEO,
)
from .parse import Observation, parse_amedas_point, point_file_names

OPEN_METEO_CURRENT = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,weather_code,is_day"
)
OPEN_METEO_HOURLY = (
    "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability,"
    "weather_code,wind_speed_10m,wind_direction_10m,is_day"
)
OPEN_METEO_DAILY = (
    "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"
)

# 応答の上限（予報は数KB、area.json・amedastable.json は約250KB）
_MAX_BYTES = 4 * 1024 * 1024

_JST = timezone(timedelta(hours=9))


class JmaError(Exception):
    """取得に失敗した."""


class JmaApi:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get(self, url: str, params: dict | None = None, text: bool = False) -> Any:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        raise JmaError(f"{url}: HTTP {resp.status}")
                    # read(n) は届いた分しか返さないので、最後まで読む
                    chunks = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > _MAX_BYTES:
                            raise JmaError(f"{url}: response too large")
                        chunks.append(chunk)
                    body = b"".join(chunks)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise JmaError(f"{url}: {err!r}") from err
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError as err:
            raise JmaError(f"{url}: not UTF-8") from err
        if text:
            return decoded
        try:
            return json.loads(decoded)
        except ValueError as err:
            raise JmaError(f"{url}: invalid JSON") from err

    async def area_table(self) -> dict:
        data = await self._get(URL_AREA)
        if not isinstance(data, dict) or "offices" not in data:
            raise JmaError("area.json: unexpected format")
        return data

    async def amedas_table(self) -> dict:
        data = await self._get(URL_AMEDAS_TABLE)
        if not isinstance(data, dict):
            raise JmaError("amedastable.json: unexpected format")
        return data

    async def forecast(self, office: str) -> list[dict]:
        data = await self._get(URL_FORECAST.format(office=office))
        if not isinstance(data, list) or not data:
            raise JmaError(f"forecast {office}: unexpected format")
        return data

    async def observations(self, station: str) -> list[Observation]:
        """最新の観測時刻を含む直近の観測値（古い順）."""
        latest_text = (await self._get(URL_AMEDAS_LATEST, text=True)).strip()
        try:
            latest = datetime.fromisoformat(latest_text)
        except ValueError as err:
            raise JmaError(f"latest_time.txt: {latest_text[:40]!r}") from err
        # ファイル名は日本時間
        local = latest.replace(tzinfo=None) if latest.utcoffset() is None else (
            latest.astimezone(_JST).replace(tzinfo=None)
        )
        result: dict[datetime, Observation] = {}
        names = point_file_names(local)
        for i, name in enumerate(names):
            try:
                data = await self._get(URL_AMEDAS_POINT.format(station=station, name=name))
            except JmaError:
                if i == 0:
                    raise
                continue
            for obs in parse_amedas_point(data):
                if obs.time <= local:
                    result[obs.time] = obs
        if not result:
            raise JmaError(f"amedas {station}: no observations")
        observations = [result[t] for t in sorted(result)]
        for obs in observations:
            obs.time = obs.time.replace(tzinfo=_JST)
        return observations

    async def open_meteo(self, latitude: float, longitude: float) -> Any:
        """地点の現在・1時間ごと・日ごとの予報（日本時間、風速 m/s）. 読むのは parse_open_meteo."""
        return await self._get(
            URL_OPEN_METEO,
            params={
                "latitude": f"{latitude:.4f}",
                "longitude": f"{longitude:.4f}",
                "current": OPEN_METEO_CURRENT,
                "hourly": OPEN_METEO_HOURLY,
                "daily": OPEN_METEO_DAILY,
                "timezone": "Asia/Tokyo",
                "wind_speed_unit": "ms",
                "forecast_days": "10",
            },
        )
