"""設定の画面."""
from __future__ import annotations

import math
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import JmaApi, JmaError
from .const import (
    CONF_AREA,
    CONF_OFFICE,
    CONF_STATION,
    CONF_TEMP_SOURCE,
    CONF_TEMP_STATION,
    DOMAIN,
    TEMP_SOURCE_JMA,
    TEMP_SOURCE_OPEN_METEO,
)
from .parse import forecast_areas, temperature_stations

# 候補に出すアメダス地点の数（近い順）
_STATION_CHOICES = 20


def _deg(value: Any) -> float | None:
    try:
        return float(value[0]) + float(value[1]) / 60.0
    except (TypeError, ValueError, IndexError):
        return None


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_stations(table: dict, latitude: float, longitude: float) -> list[tuple[str, str, float]]:
    """気温を観測しているアメダス地点を近い順に [(コード, 名前, 距離km)]."""
    result = []
    for code, info in table.items():
        if not isinstance(info, dict):
            continue
        elems = str(info.get("elems", ""))
        if not elems.startswith("1"):  # 1文字目が気温
            continue
        lat, lon = _deg(info.get("lat")), _deg(info.get("lon"))
        if lat is None or lon is None:
            continue
        result.append(
            (code, str(info.get("kjName", code)), _distance_km(latitude, longitude, lat, lon))
        )
    result.sort(key=lambda x: x[2])
    return result


def _select(options: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _temp_source_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[TEMP_SOURCE_OPEN_METEO, TEMP_SOURCE_JMA],
            translation_key=CONF_TEMP_SOURCE,
            mode=SelectSelectorMode.LIST,
        )
    )


async def _details(
    hass: HomeAssistant, office: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str, float]]]:
    api = JmaApi(async_get_clientsession(hass))
    forecast = await api.forecast(office)
    table = await api.amedas_table()
    stations = nearest_stations(table, hass.config.latitude, hass.config.longitude)
    return forecast_areas(forecast), temperature_stations(forecast), stations[:_STATION_CHOICES]


class JmaWeatherConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._office: str | None = None
        self._areas: list[tuple[str, str]] = []
        self._temp_stations: list[tuple[str, str]] = []
        self._stations: list[tuple[str, str, float]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JmaWeatherOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._office = user_input[CONF_OFFICE]
            try:
                self._areas, self._temp_stations, self._stations = await _details(
                    self.hass, self._office
                )
            except JmaError:
                errors["base"] = "cannot_connect"
            else:
                if not self._areas or not self._stations:
                    errors["base"] = "no_data"
                else:
                    return await self.async_step_details()
        try:
            table = await JmaApi(async_get_clientsession(self.hass)).area_table()
        except JmaError:
            return self.async_abort(reason="cannot_connect")
        offices = [
            SelectOptionDict(value=code, label=f"{info.get('name', code)}（{code}）")
            for code, info in sorted(table["offices"].items())
            if isinstance(info, dict)
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_OFFICE): _select(offices)}),
            errors=errors,
        )

    async def async_step_details(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(f"{user_input[CONF_AREA]}_{user_input[CONF_STATION]}")
            self._abort_if_unique_id_configured()
            area_name = dict(self._areas).get(user_input[CONF_AREA], user_input[CONF_AREA])
            station_name = {c: n for c, n, _ in self._stations}.get(
                user_input[CONF_STATION], user_input[CONF_STATION]
            )
            return self.async_create_entry(
                title=f"{area_name}・{station_name}",
                data={
                    CONF_OFFICE: self._office,
                    CONF_AREA: user_input[CONF_AREA],
                    CONF_STATION: user_input[CONF_STATION],
                },
                options={
                    CONF_TEMP_SOURCE: user_input[CONF_TEMP_SOURCE],
                    CONF_TEMP_STATION: user_input.get(CONF_TEMP_STATION),
                },
            )
        schema = {
            vol.Required(CONF_AREA, default=self._areas[0][0]): _select(
                [SelectOptionDict(value=c, label=n) for c, n in self._areas]
            ),
            vol.Required(CONF_STATION, default=self._stations[0][0]): _select(
                [
                    SelectOptionDict(value=c, label=f"{n}（{d:.1f} km）")
                    for c, n, d in self._stations
                ]
            ),
            vol.Required(CONF_TEMP_SOURCE, default=TEMP_SOURCE_OPEN_METEO): _temp_source_selector(),
        }
        if self._temp_stations:
            schema[vol.Required(CONF_TEMP_STATION, default=self._temp_stations[0][0])] = _select(
                [SelectOptionDict(value=c, label=n) for c, n in self._temp_stations]
            )
        return self.async_show_form(step_id="details", data_schema=vol.Schema(schema))


class JmaWeatherOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        current = {**entry.data, **entry.options}
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        try:
            _, temp_stations, stations = await _details(self.hass, entry.data[CONF_OFFICE])
        except JmaError:
            return self.async_abort(reason="cannot_connect")
        station_options = [
            SelectOptionDict(value=c, label=f"{n}（{d:.1f} km）") for c, n, d in stations
        ]
        if current[CONF_STATION] not in {c for c, _, _ in stations}:
            station_options.insert(
                0, SelectOptionDict(value=current[CONF_STATION], label=current[CONF_STATION])
            )
        schema = {
            vol.Required(CONF_STATION, default=current[CONF_STATION]): _select(station_options),
            vol.Required(
                CONF_TEMP_SOURCE, default=current.get(CONF_TEMP_SOURCE, TEMP_SOURCE_OPEN_METEO)
            ): _temp_source_selector(),
        }
        if temp_stations:
            default = current.get(CONF_TEMP_STATION) or temp_stations[0][0]
            schema[vol.Required(CONF_TEMP_STATION, default=default)] = _select(
                [SelectOptionDict(value=c, label=n) for c, n in temp_stations]
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
