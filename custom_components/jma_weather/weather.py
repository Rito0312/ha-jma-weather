"""天気エンティティ.

項目ごとに、気象庁のデータが取れなければ Open-Meteo を使う。
どちらから来たかは属性（*_source）で分かる。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.weather import (
    Forecast as HAForecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPrecipitationDepth, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

from .const import CONF_TEMP_SOURCE, DOMAIN, TEMP_SOURCE_OPEN_METEO
from .coordinator import JmaCoordinator
from .parse import Observation, current_condition

# 1時間ごとの予報は48時間分
HOURLY_HOURS = 48
# Open-Meteo の「現在」がこれより古ければ使わない
OPEN_METEO_CURRENT_MAX_AGE = timedelta(hours=2)

SRC_AMEDAS = "amedas"
SRC_JMA = "jma"
SRC_OPEN_METEO = "open_meteo"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: JmaCoordinator = entry.runtime_data
    async_add_entities([JmaWeather(coordinator, entry)])


class JmaWeather(SingleCoordinatorWeatherEntity[JmaCoordinator]):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(self, coordinator: JmaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="気象庁",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.jma.go.jp/bosai/forecast/",
        )

    # ---- 元データ ----

    @property
    def _data(self):
        return self.coordinator.data

    def _latest(self) -> Observation | None:
        """気温のある最新の観測（最新の記録は値がまだそろっていないことがある）."""
        obs = self._data.observations if self._data else []
        for o in reversed(obs):
            if o.temperature is not None:
                return o
        return None

    def _recent(self) -> list[Observation]:
        """最新の観測から30分以内の観測（古い順）."""
        latest = self._latest()
        if latest is None:
            return []
        return [
            o for o in self._data.observations
            if o.time <= latest.time and (latest.time - o.time).total_seconds() < 30 * 60
        ]

    def _om_current(self):
        om = self._data.open_meteo if self._data else None
        cur = om.current if om else None
        if cur is None:
            return None
        if cur.time is not None and dt_util.utcnow() - cur.time > OPEN_METEO_CURRENT_MAX_AGE:
            return None
        return cur

    def _today_condition(self) -> str | None:
        today = dt_util.now().date()
        data = self._data
        if data and data.forecast and (d := data.forecast.get(today)) and d.condition:
            return d.condition
        if data and data.open_meteo and (d := data.open_meteo.days.get(today)):
            return d.condition
        return None

    def _sun_elevation(self) -> float | None:
        try:
            location, elevation = get_astral_location(self.hass)
            return float(location.solar_elevation(dt_util.utcnow(), elevation))
        except Exception:  # noqa: BLE001 - 太陽の計算に失敗しても天気は出す
            return None

    def _current(self) -> tuple[str | None, str]:
        if self._latest() is not None:
            condition, how = current_condition(
                self._recent(), self._today_condition(), self._sun_elevation()
            )
            if how != "none":
                return condition, how
        cur = self._om_current()
        if cur and cur.condition:
            return cur.condition, SRC_OPEN_METEO
        return None, "none"

    def _observed(self, attr: str) -> tuple[Any, str | None]:
        latest = self._latest()
        if latest is not None:
            return getattr(latest, attr), SRC_AMEDAS
        cur = self._om_current()
        if cur is not None:
            return getattr(cur, attr), SRC_OPEN_METEO
        return None, None

    # ---- 現在 ----

    @property
    def condition(self) -> str | None:
        return self._current()[0]

    @property
    def native_temperature(self) -> float | None:
        return self._observed("temperature")[0]

    @property
    def humidity(self) -> float | None:
        return self._observed("humidity")[0]

    @property
    def native_wind_speed(self) -> float | None:
        return self._observed("wind_speed")[0]

    @property
    def wind_bearing(self) -> float | None:
        return self._observed("wind_bearing")[0]

    @property
    def attribution(self) -> str:
        sources = []
        data = self._data
        if self._latest() is not None or (data and data.forecast):
            sources.append("気象庁（府県天気予報・アメダス）")
        if data and data.open_meteo:
            sources.append("Open-Meteo")
        return ", ".join(sources) or "気象庁"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        attrs: dict[str, Any] = {
            "condition_source": self._current()[1],
            "observation_source": self._observed("temperature")[1],
            "daily_source": self._daily_source(),
            "hourly_source": SRC_OPEN_METEO if self._hourly() else None,
        }
        latest = self._latest()
        if latest:
            attrs["observation_time"] = latest.time.isoformat()
            attrs["precipitation_10m"] = latest.precipitation_10m
            attrs["precipitation_1h"] = latest.precipitation_1h
            attrs["sunshine_10m"] = latest.sun_10m
        if data and data.forecast:
            if data.forecast.report_time:
                attrs["forecast_report_time"] = data.forecast.report_time.isoformat()
            attrs["forecast_area"] = data.forecast.area_name
            today = data.forecast.get(dt_util.now().date())
            if today:
                attrs["today_weather"] = today.text
        attrs["problems"] = list(data.problems) if data else []
        return attrs

    # ---- 予報 ----

    def _daily_source(self) -> str | None:
        data = self._data
        if data and data.forecast and data.forecast.days:
            return SRC_JMA
        if data and data.open_meteo and data.open_meteo.days:
            return SRC_OPEN_METEO
        return None

    def _uses_open_meteo_temps(self) -> bool:
        return (
            self.coordinator.settings.get(CONF_TEMP_SOURCE, TEMP_SOURCE_OPEN_METEO)
            == TEMP_SOURCE_OPEN_METEO
        )

    def _daily(self) -> list[HAForecast] | None:
        data = self._data
        if not data:
            return None
        today = dt_util.now().date()
        tz = dt_util.get_default_time_zone()
        om_days = data.open_meteo.days if data.open_meteo else {}
        result: list[HAForecast] = []
        source = self._daily_source()
        if source == SRC_JMA:
            for day in data.forecast.days:
                if day.day < today or day.condition is None:
                    continue
                om = om_days.get(day.day)
                # 予想気温: Open-Meteo（自宅の地点）。取れなければ気象庁の地点の値
                if self._uses_open_meteo_temps() and om is not None:
                    high, low = om.temp_max, om.temp_min
                else:
                    high, low = day.temp_max, day.temp_min
                item: HAForecast = {
                    "datetime": datetime.combine(day.day, time(0), tz).isoformat(),
                    "condition": day.condition,
                    "native_temperature": high,
                    "native_templow": low,
                }
                if day.precipitation_probability is not None:
                    item["precipitation_probability"] = day.precipitation_probability
                result.append(item)
        elif source == SRC_OPEN_METEO:
            for d in sorted(om_days):
                om = om_days[d]
                if d < today or om.condition is None:
                    continue
                item = {
                    "datetime": datetime.combine(d, time(0), tz).isoformat(),
                    "condition": om.condition,
                    "native_temperature": om.temp_max,
                    "native_templow": om.temp_min,
                }
                if om.precipitation_probability is not None:
                    item["precipitation_probability"] = om.precipitation_probability
                result.append(item)
        return result or None

    def _hourly(self) -> list[HAForecast] | None:
        data = self._data
        if not data or not data.open_meteo:
            return None
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        result: list[HAForecast] = []
        for h in data.open_meteo.hours:
            if h.time < start or h.condition is None:
                continue
            item: HAForecast = {
                "datetime": h.time.isoformat(),
                "condition": h.condition,
                "native_temperature": h.temperature,
                "humidity": h.humidity,
                "native_precipitation": h.precipitation,
                "native_wind_speed": h.wind_speed,
                "wind_bearing": h.wind_bearing,
            }
            if h.precipitation_probability is not None:
                item["precipitation_probability"] = h.precipitation_probability
            result.append(item)
            if len(result) >= HOURLY_HOURS:
                break
        return result or None

    @callback
    def _async_forecast_daily(self) -> list[HAForecast] | None:
        return self._daily()

    @callback
    def _async_forecast_hourly(self) -> list[HAForecast] | None:
        return self._hourly()
