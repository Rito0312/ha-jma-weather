"""HA と組み合わせた動作のテスト（pytest-homeassistant-custom-component）."""
from datetime import timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma_weather.api import JmaError
from custom_components.jma_weather.const import (
    CONF_AREA,
    CONF_OFFICE,
    CONF_STATION,
    CONF_TEMP_SOURCE,
    CONF_TEMP_STATION,
    DOMAIN,
)
from custom_components.jma_weather.parse import parse_amedas_point

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

FIXTURES = Path(__file__).parent / "fixtures"
JST = timezone(timedelta(hours=9))
NOW = "2026-11-17T03:35:00+00:00"  # 日本時間 12:35


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def observations():
    obs = parse_amedas_point(load("amedas_14163_20261117_09.json")) + parse_amedas_point(
        load("amedas_14163_20261117_12.json")
    )
    for o in obs:
        o.time = o.time.replace(tzinfo=JST)
    return obs


OM = load("open_meteo_sapporo.json")


def om_day(i):
    return OM["daily"]["temperature_2m_max"][i], OM["daily"]["temperature_2m_min"][i]


AREA_TABLE = {
    "offices": {
        "016000": {"name": "石狩・空知・後志地方"},
        "270000": {"name": "大阪府"},
    }
}
AMEDAS_TABLE = {
    # 自宅（札幌付近に設定）から近い順: 札幌 → 石狩。雨量だけの地点は候補に出ない
    "14163": {"elems": "11111111", "lat": [43, 3.6], "lon": [141, 19.7], "kjName": "札幌"},
    "14121": {"elems": "11112010", "lat": [43, 12.6], "lon": [141, 22.5], "kjName": "石狩"},
    "14999": {"elems": "01000000", "lat": [43, 3.6], "lon": [141, 19.7], "kjName": "雨量だけ"},
}


@pytest.fixture
async def setup_home(hass: HomeAssistant):
    await hass.config.async_update(latitude=43.06, longitude=141.33, time_zone="Asia/Tokyo")


@pytest.fixture
def api():
    with patch("custom_components.jma_weather.JmaApi") as cls:
        mock = AsyncMock()
        mock.observations.return_value = observations()
        mock.forecast.return_value = load("forecast_016000.json")
        mock.open_meteo.return_value = OM
        cls.return_value = mock
        yield mock


def entry(**options):
    return MockConfigEntry(
        domain=DOMAIN,
        title="空知地方・札幌",
        unique_id="016020_14163",
        data={CONF_OFFICE: "016000", CONF_AREA: "016020", CONF_STATION: "14163"},
        options={CONF_TEMP_SOURCE: "open_meteo", CONF_TEMP_STATION: "15356", **options},
    )


async def _setup(hass, e):
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass):
    states = hass.states.async_all("weather")
    assert len(states) == 1
    return states[0].entity_id


async def _daily(hass, entity_id):
    resp = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"},
        blocking=True, return_response=True,
    )
    return resp[entity_id]["forecast"]


async def _forecast(hass, entity_id, kind):
    resp = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": entity_id, "type": kind},
        blocking=True, return_response=True,
    )
    return resp[entity_id]["forecast"]


@pytest.mark.freeze_time(NOW)
async def test_entity_normal(hass: HomeAssistant, setup_home, api):
    e = entry()
    await _setup(hass, e)
    assert e.state is ConfigEntryState.LOADED
    entity_id = _entity_id(hass)
    st = hass.states.get(entity_id)
    raw = load("amedas_14163_20261117_12.json")["20261117123000"]
    assert st.attributes["temperature"] == raw["temp"][0]
    assert st.attributes["humidity"] == raw["humidity"][0]
    # 風速は HA の天気の既定の単位（km/h）で出る
    assert st.attributes["wind_speed_unit"] == "km/h"
    assert st.attributes["wind_speed"] == round(raw["wind"][0] * 3.6, 2)
    assert st.attributes["observation_time"] == "2026-11-17T12:30:00+09:00"
    assert st.attributes["observation_source"] == "amedas"
    assert st.attributes["daily_source"] == "jma"
    assert st.attributes["hourly_source"] == "open_meteo"
    assert st.attributes["forecast_area"] == "空知地方"
    assert st.attributes["today_weather"]
    assert st.attributes["problems"] == []
    assert "気象庁" in st.attributes["attribution"] and "Open-Meteo" in st.attributes["attribution"]
    # 昼なので日照から判定する（太陽高度の計算が動いていること）
    assert st.attributes["condition_source"] == "amedas_sunshine"
    assert st.state in {"sunny", "partlycloudy", "cloudy"}
    api.open_meteo.assert_awaited_once_with(43.06, 141.33)

    fc = await _daily(hass, entity_id)
    assert len(fc) == 8
    assert fc[0]["datetime"] == "2026-11-17T00:00:00+09:00"
    assert (fc[0]["temperature"], fc[0]["templow"]) == om_day(0)
    assert fc[7]["temperature"] == om_day(7)[0]
    assert all("condition" in d for d in fc)
    assert all("precipitation_probability" in d for d in fc[1:])

    hourly = await _forecast(hass, entity_id, "hourly")
    assert len(hourly) == 48
    assert hourly[0]["datetime"] == "2026-11-17T12:00:00+09:00"
    i = OM["hourly"]["time"].index("2026-11-17T12:00")
    assert hourly[0]["temperature"] == OM["hourly"]["temperature_2m"][i]
    assert hourly[0]["humidity"] == OM["hourly"]["relative_humidity_2m"][i]
    assert "precipitation" in hourly[0] and "precipitation_probability" in hourly[0]


@pytest.mark.freeze_time(NOW)
async def test_entity_jma_temperatures(hass: HomeAssistant, setup_home, api):
    await _setup(hass, entry(temp_source="jma"))
    fc = await _daily(hass, _entity_id(hass))
    short = {a["area"]["code"]: a["temps"] for a in load("forecast_016000.json")[0]["timeSeries"][2]["areas"]}
    assert fc[0]["temperature"] == float(short["15356"][0])  # 岩見沢
    assert fc[0].get("templow") is None


@pytest.mark.freeze_time(NOW)
async def test_amedas_down_uses_open_meteo(hass: HomeAssistant, setup_home, api):
    api.observations.side_effect = JmaError("down")
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.state != "unavailable"
    assert st.attributes["temperature"] == OM["current"]["temperature_2m"]
    assert st.attributes["humidity"] == OM["current"]["relative_humidity_2m"]
    assert st.attributes["observation_source"] == "open_meteo"
    assert st.attributes["condition_source"] == "open_meteo"
    assert st.attributes["daily_source"] == "jma"
    assert any(p.startswith("アメダス") for p in st.attributes["problems"])


@pytest.mark.freeze_time(NOW)
async def test_missing_station_values(hass: HomeAssistant, setup_home, api, caplog):
    obs = parse_amedas_point(load("amedas_missing.json"))
    for o in obs:
        o.time = o.time.replace(tzinfo=JST)
    api.observations.return_value = obs
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["observation_source"] == "open_meteo"
    assert "気温がありません" in caplog.text


@pytest.mark.freeze_time(NOW)
async def test_forecast_down_uses_open_meteo(hass: HomeAssistant, setup_home, api):
    api.forecast.side_effect = JmaError("down")
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["daily_source"] == "open_meteo"
    assert st.attributes["observation_source"] == "amedas"
    fc = await _daily(hass, _entity_id(hass))
    assert len(fc) == 10
    assert (fc[0]["temperature"], fc[0]["templow"]) == om_day(0)
    assert fc[0]["precipitation_probability"] == OM["daily"]["precipitation_probability_max"][0]


@pytest.mark.freeze_time(NOW)
async def test_forecast_format_changed(hass: HomeAssistant, setup_home, api, caplog):
    """形式が変わって地域が見つからないときは警告して Open-Meteo の予報を使う."""
    broken = load("forecast_016000.json")
    for ts in broken[0]["timeSeries"]:
        for a in ts["areas"]:
            a["area"]["code"] = "999999"
    api.forecast.return_value = broken
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["daily_source"] == "open_meteo"
    assert "形式" in caplog.text


@pytest.mark.freeze_time(NOW)
async def test_open_meteo_down_falls_back_to_jma(hass: HomeAssistant, setup_home, api):
    """Open-Meteo が取れないときは気象庁の予想気温を使い、1時間ごとの予報は無し."""
    api.open_meteo.side_effect = JmaError("down")
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert "Open-Meteo" not in st.attributes["attribution"]
    assert st.attributes["hourly_source"] is None
    fc = await _daily(hass, _entity_id(hass))
    assert len(fc) == 8
    short = {a["area"]["code"]: a["temps"] for a in load("forecast_016000.json")[0]["timeSeries"][2]["areas"]}
    assert fc[0]["temperature"] == float(short["15356"][0])
    assert await _forecast(hass, _entity_id(hass), "hourly") == []


@pytest.mark.freeze_time("2026-11-17T23:00:00+00:00")  # 日本時間 18日 8:00
async def test_past_days_are_dropped(hass: HomeAssistant, setup_home, api):
    await _setup(hass, entry())
    fc = await _daily(hass, _entity_id(hass))
    assert fc[0]["datetime"] == "2026-11-18T00:00:00+09:00"


async def test_all_down_retries(hass: HomeAssistant, setup_home, api):
    api.observations.side_effect = JmaError("down")
    api.forecast.side_effect = JmaError("down")
    api.open_meteo.side_effect = JmaError("down")
    e = entry()
    await _setup(hass, e)
    assert e.state is ConfigEntryState.SETUP_RETRY


async def test_config_flow(hass: HomeAssistant, setup_home):
    with patch("custom_components.jma_weather.config_flow.JmaApi") as cls:
        mock = AsyncMock()
        mock.area_table.return_value = AREA_TABLE
        mock.forecast.return_value = load("forecast_016000.json")
        mock.amedas_table.return_value = AMEDAS_TABLE
        cls.return_value = mock
        with patch("custom_components.jma_weather.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "user"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_OFFICE: "016000"}
            )
            assert result["step_id"] == "details"
            schema = result["data_schema"].schema
            station_key = next(k for k in schema if k == CONF_STATION)
            assert station_key.default() == "14163"  # 一番近い地点
            options = [o["value"] for o in schema[station_key].config["options"]]
            assert options == ["14163", "14121"]  # 気温の無い地点は出ない
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_AREA: "016020", CONF_STATION: "14121", CONF_TEMP_SOURCE: "jma",
                 CONF_TEMP_STATION: "15356"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["title"] == "空知地方・石狩"
            assert result["data"] == {CONF_OFFICE: "016000", CONF_AREA: "016020", CONF_STATION: "14121"}
            assert result["options"] == {CONF_TEMP_SOURCE: "jma", CONF_TEMP_STATION: "15356"}

            # 同じ地域・地点は二重に登録しない
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_OFFICE: "016000"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_AREA: "016020", CONF_STATION: "14121", CONF_TEMP_SOURCE: "jma",
                 CONF_TEMP_STATION: "15356"},
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "already_configured"


async def test_config_flow_cannot_connect(hass: HomeAssistant, setup_home):
    with patch("custom_components.jma_weather.config_flow.JmaApi") as cls:
        mock = AsyncMock()
        mock.area_table.return_value = AREA_TABLE
        mock.forecast.side_effect = JmaError("down")
        cls.return_value = mock
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OFFICE: "016000"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.freeze_time(NOW)
async def test_options_flow(hass: HomeAssistant, setup_home, api):
    e = entry()
    await _setup(hass, e)
    with patch("custom_components.jma_weather.config_flow.JmaApi") as cls:
        mock = AsyncMock()
        mock.forecast.return_value = load("forecast_016000.json")
        mock.amedas_table.return_value = AMEDAS_TABLE
        cls.return_value = mock
        result = await hass.config_entries.options.async_init(e.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_STATION: "14121", CONF_TEMP_SOURCE: "jma", CONF_TEMP_STATION: "14163"},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert e.options[CONF_STATION] == "14121"
    api.observations.assert_awaited_with("14121")


async def test_update_intervals(hass: HomeAssistant, setup_home, api, freezer):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.move_to(NOW)
    await _setup(hass, entry())
    assert api.observations.await_count == 1
    assert api.forecast.await_count == 1
    assert api.open_meteo.await_count == 1

    async def step(minutes):
        freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    await step(5)  # アメダスだけ
    assert (api.observations.await_count, api.forecast.await_count, api.open_meteo.await_count) == (2, 1, 1)
    # 予報の取得に失敗すると、次の5分後に取り直す
    api.forecast.side_effect = JmaError("down")
    await step(26)
    failed = api.forecast.await_count
    assert failed == 2
    api.forecast.side_effect = None
    await step(5)
    assert api.forecast.await_count == failed + 1
    await step(5)
    assert api.forecast.await_count == failed + 1  # 成功後30分は取り直さない
    assert api.open_meteo.await_count == 2  # Open-Meteo は30分ごと（31分の時点で1回）
