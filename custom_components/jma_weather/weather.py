"""天気エンティティ."""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from homeassistant.components.weather import (
    Forecast as HAForecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

from .const import (
    ATTRIBUTION,
    ATTRIBUTION_OPEN_METEO,
    CONF_TEMP_SOURCE,
    DOMAIN,
    TEMP_SOURCE_OPEN_METEO,
)
from .coordinator import JmaCoordinator
from .parse import current_condition


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
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY

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

    @property
    def _uses_open_meteo(self) -> bool:
        return (
            self.coordinator.settings.get(CONF_TEMP_SOURCE, TEMP_SOURCE_OPEN_METEO)
            == TEMP_SOURCE_OPEN_METEO
        )

    @property
    def _open_meteo_in_use(self) -> bool:
        data = self.coordinator.data
        return self._uses_open_meteo and data is not None and data.open_meteo is not None

    @property
    def attribution(self) -> str:
        return ATTRIBUTION_OPEN_METEO if self._open_meteo_in_use else ATTRIBUTION

    @property
    def _latest(self):
        """気温のある最新の観測（最新の記録は値がまだそろっていないことがある）."""
        obs = self.coordinator.data.observations if self.coordinator.data else []
        for o in reversed(obs):
            if o.temperature is not None:
                return o
        return None

    def _recent(self):
        """最新の観測から30分以内の観測（古い順）."""
        obs = self.coordinator.data.observations if self.coordinator.data else []
        latest = self._latest
        if latest is None:
            return []
        return [
            o for o in obs
            if o.time <= latest.time and (latest.time - o.time).total_seconds() < 30 * 60
        ]

    def _today(self):
        data = self.coordinator.data
        if not data or not data.forecast:
            return None
        return data.forecast.get(dt_util.now().date())

    def _sun_elevation(self) -> float | None:
        try:
            location, elevation = get_astral_location(self.hass)
            return float(location.solar_elevation(dt_util.utcnow(), elevation))
        except Exception:  # noqa: BLE001 - 太陽の計算に失敗しても天気は出す
            return None

    def _current(self) -> tuple[str | None, str]:
        today = self._today()
        return current_condition(
            self._recent(), today.condition if today else None, self._sun_elevation()
        )

    @property
    def condition(self) -> str | None:
        return self._current()[0]

    @property
    def native_temperature(self) -> float | None:
        latest = self._latest
        return latest.temperature if latest else None

    @property
    def humidity(self) -> float | None:
        latest = self._latest
        return latest.humidity if latest else None

    @property
    def native_wind_speed(self) -> float | None:
        latest = self._latest
        return latest.wind_speed if latest else None

    @property
    def wind_bearing(self) -> float | None:
        latest = self._latest
        return latest.wind_bearing if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"condition_source": self._current()[1]}
        latest = self._latest
        if latest:
            attrs["observation_time"] = latest.time.isoformat()
            attrs["precipitation_10m"] = latest.precipitation_10m
            attrs["precipitation_1h"] = latest.precipitation_1h
            attrs["sunshine_10m"] = latest.sun_10m
        data = self.coordinator.data
        if data and data.forecast:
            if data.forecast.report_time:
                attrs["forecast_report_time"] = data.forecast.report_time.isoformat()
            attrs["forecast_area"] = data.forecast.area_name
            today = self._today()
            if today:
                attrs["today_weather"] = today.text
        return attrs

    @callback
    def _async_forecast_daily(self) -> list[HAForecast] | None:
        data = self.coordinator.data
        if not data or not data.forecast:
            return None
        today = dt_util.now().date()
        tz = dt_util.get_default_time_zone()
        # Open-Meteo が取れないときは気象庁の予想気温
        use_om = self._open_meteo_in_use
        result: list[HAForecast] = []
        for day in data.forecast.days:
            if day.day < today or day.condition is None:
                continue
            if use_om:
                high, low = data.open_meteo.get(day.day, (None, None))
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
        return result
