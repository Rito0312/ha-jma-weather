"""取得の調整（アメダス5分ごと、予報30分ごと、Open-Meteo 60分ごと）."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import JmaApi, JmaError  # noqa: F401 (JmaApi は型のため)
from .const import (
    CONF_AREA,
    CONF_OFFICE,
    CONF_STATION,
    CONF_TEMP_SOURCE,
    CONF_TEMP_STATION,
    DOMAIN,
    FORECAST_INTERVAL,
    FORECAST_MAX_AGE,
    OBSERVATION_MAX_AGE,
    OPEN_METEO_INTERVAL,
    TEMP_SOURCE_OPEN_METEO,
    UPDATE_INTERVAL,
)
from .parse import Forecast, Observation, parse_forecast

_LOGGER = logging.getLogger(__name__)


@dataclass
class JmaData:
    observations: list[Observation]
    forecast: Forecast | None
    open_meteo: dict | None


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
        self._forecast_raw: list[dict] | None = None
        self._forecast_at: datetime | None = None
        self._om: dict | None = None
        self._om_at: datetime | None = None
        self._failing: set[str] = set()

    @property
    def settings(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    def _problem(self, source: str, err: Exception) -> None:
        if source not in self._failing:
            self._failing.add(source)
            _LOGGER.warning("%s の取得に失敗しました: %s", source, err)
        else:
            _LOGGER.debug("%s の取得に失敗しました: %s", source, err)

    def _recovered(self, source: str) -> None:
        if source in self._failing:
            self._failing.discard(source)
            _LOGGER.info("%s の取得が回復しました", source)

    async def _async_update_data(self) -> JmaData:
        now = dt_util.utcnow()
        s = self.settings

        try:
            self._observations = await self.api.observations(s[CONF_STATION])
            self._recovered("アメダス")
        except JmaError as err:
            self._problem("アメダス", err)

        # 成功してから一定時間は取り直さない（失敗中は5分ごとに試す）
        if self._forecast_at is None or now - self._forecast_at >= FORECAST_INTERVAL:
            try:
                self._forecast_raw = await self.api.forecast(s[CONF_OFFICE])
                self._forecast_at = now
                self._recovered("天気予報")
            except JmaError as err:
                self._problem("天気予報", err)

        if s.get(CONF_TEMP_SOURCE, TEMP_SOURCE_OPEN_METEO) == TEMP_SOURCE_OPEN_METEO:
            if self._om_at is None or now - self._om_at >= OPEN_METEO_INTERVAL:
                try:
                    self._om = await self.api.open_meteo_daily(
                        self.hass.config.latitude, self.hass.config.longitude
                    )
                    self._om_at = now
                    self._recovered("Open-Meteo")
                except JmaError as err:
                    self._problem("Open-Meteo", err)

        observations = [
            o for o in self._observations if now - o.time <= OBSERVATION_MAX_AGE
        ]
        forecast = None
        if self._forecast_raw is not None and self._forecast_at and (
            now - self._forecast_at <= FORECAST_MAX_AGE
        ):
            try:
                forecast = parse_forecast(
                    self._forecast_raw, s[CONF_AREA], s.get(CONF_TEMP_STATION)
                )
            except (KeyError, TypeError, ValueError, IndexError) as err:
                self._problem("天気予報（解析）", err)
        om = None
        if self._om is not None and self._om_at and now - self._om_at <= FORECAST_MAX_AGE:
            om = self._om

        if not observations and forecast is None:
            raise UpdateFailed("アメダスと天気予報のどちらも取得できません")
        return JmaData(observations=observations, forecast=forecast, open_meteo=om)
