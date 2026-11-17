"""取得の調整（アメダス5分ごと、気象庁の予報30分ごと、Open-Meteo 30分ごと）."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import JmaApi, JmaError
from .const import (
    CONF_AREA,
    CONF_OFFICE,
    CONF_STATION,
    CONF_TEMP_STATION,
    DOMAIN,
    FORECAST_INTERVAL,
    FORECAST_MAX_AGE,
    OBSERVATION_MAX_AGE,
    OPEN_METEO_INTERVAL,
    UPDATE_INTERVAL,
)
from .parse import Forecast, Observation, OpenMeteo, parse_forecast, parse_open_meteo

_LOGGER = logging.getLogger(__name__)

SOURCE_AMEDAS = "アメダス"
SOURCE_FORECAST = "気象庁の天気予報"
SOURCE_OPEN_METEO = "Open-Meteo"


@dataclass
class JmaData:
    observations: list[Observation]
    forecast: Forecast | None
    open_meteo: OpenMeteo | None
    problems: list[str] = field(default_factory=list)


class JmaCoordinator(DataUpdateCoordinator[JmaData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: JmaApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.api = api
        self._observations: list[Observation] = []
        self._forecast: Forecast | None = None
        self._forecast_at: datetime | None = None
        self._om: OpenMeteo | None = None
        self._om_at: datetime | None = None
        self._failing: dict[str, str] = {}

    @property
    def settings(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    def _problem(self, source: str, message: str) -> None:
        if source not in self._failing:
            _LOGGER.warning("%s: %s", source, message)
        else:
            _LOGGER.debug("%s: %s", source, message)
        self._failing[source] = message

    def _recovered(self, source: str) -> None:
        if self._failing.pop(source, None) is not None:
            _LOGGER.info("%s: 回復しました", source)

    async def _update_observations(self, station: str) -> None:
        try:
            observations = await self.api.observations(station)
        except JmaError as err:
            self._problem(SOURCE_AMEDAS, f"取得に失敗しました（{err}）")
            return
        self._observations = observations
        if not any(o.temperature is not None for o in observations):
            # 欠測（保守など）か、データの形式が変わったか
            self._problem(
                SOURCE_AMEDAS,
                f"地点 {station} の気温がありません（欠測、またはデータの形式が変わった可能性）",
            )
        else:
            self._recovered(SOURCE_AMEDAS)

    async def _update_forecast(self, office: str, area: str, temp_station: str | None) -> None:
        try:
            raw = await self.api.forecast(office)
        except JmaError as err:
            self._problem(SOURCE_FORECAST, f"取得に失敗しました（{err}）")
            return
        try:
            forecast = parse_forecast(raw, area, temp_station)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as err:
            self._problem(SOURCE_FORECAST, f"読み取れません（データの形式が変わった可能性: {err!r}）")
            return
        if forecast.area_name is None or not any(d.condition for d in forecast.days):
            self._problem(
                SOURCE_FORECAST,
                f"地域 {area} の予報が見つかりません（データの形式・地域コードが変わった可能性）",
            )
            return
        self._forecast = forecast
        self._forecast_at = dt_util.utcnow()
        self._recovered(SOURCE_FORECAST)

    async def _update_open_meteo(self) -> None:
        try:
            raw = await self.api.open_meteo(self.hass.config.latitude, self.hass.config.longitude)
        except JmaError as err:
            self._problem(SOURCE_OPEN_METEO, f"取得に失敗しました（{err}）")
            return
        try:
            om = parse_open_meteo(raw, dt_util.get_default_time_zone())
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as err:
            self._problem(SOURCE_OPEN_METEO, f"読み取れません（{err!r}）")
            return
        self._om = om
        self._om_at = dt_util.utcnow()
        self._recovered(SOURCE_OPEN_METEO)

    async def _async_update_data(self) -> JmaData:
        now = dt_util.utcnow()
        s = self.settings

        await self._update_observations(s[CONF_STATION])
        # 成功してから一定時間は取り直さない（失敗中は5分ごとに試す）
        if self._forecast_at is None or now - self._forecast_at >= FORECAST_INTERVAL:
            await self._update_forecast(s[CONF_OFFICE], s[CONF_AREA], s.get(CONF_TEMP_STATION))
        if self._om_at is None or now - self._om_at >= OPEN_METEO_INTERVAL:
            await self._update_open_meteo()

        observations = [o for o in self._observations if now - o.time <= OBSERVATION_MAX_AGE]
        forecast = self._forecast if (
            self._forecast_at and now - self._forecast_at <= FORECAST_MAX_AGE
        ) else None
        om = self._om if self._om_at and now - self._om_at <= FORECAST_MAX_AGE else None

        if not any(o.temperature is not None for o in observations) and forecast is None and om is None:
            raise UpdateFailed("アメダス・気象庁の天気予報・Open-Meteo のどれも取得できません")
        return JmaData(
            observations=observations,
            forecast=forecast,
            open_meteo=om,
            problems=[f"{k}: {v}" for k, v in self._failing.items()],
        )
