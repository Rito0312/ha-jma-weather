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


# ---- 天気分布予報（自宅のマスだけの小さなデータを作る） ----
HOME = (43.06, 141.33)
GRID_BASE = "20261117020000"  # 11時発表（UTC）
# validtime（UTC）: 日本時間 12時〜翌日24時
GRID_VALID = [f"202611{d:02d}{h:02d}0000" for d, h in
              [(17, 3), (17, 6), (17, 9), (17, 12), (17, 15), (17, 18), (17, 21),
               (18, 0), (18, 3), (18, 6), (18, 9), (18, 12), (18, 15)]]
GRID_TIMES = [
    {"basetime": GRID_BASE, "validtime": v, "member": "none",
     "elements": ["r3", "temp_point", "wm"]
     + (["min_temp_point"] if v == "20261118000000" else [])
     + (["max_temp_point"] if v == "20261118090000" else [])}
    for v in GRID_VALID
] + [{"basetime": "20261116200000", "validtime": "20261117030000", "member": "none",
      "elements": ["wm"]}]  # 古い発表は使わない
# 日本時間 12〜15時は雨（10〜15mm）、15〜18時はくもり、それ以降は晴れ
GRID_WM = {"20261117060000": "Rain", "20261117090000": "Cloudy"}
GRID_R3 = {"20261117060000": "10<=mm<15"}


def _square(level):
    lat, lon = HOME
    ring = [[lon - 0.03, lat + 0.025], [lon + 0.03, lat + 0.025], [lon + 0.03, lat - 0.025],
            [lon - 0.03, lat - 0.025], [lon - 0.03, lat + 0.025]]
    far = [[r[0] + 1, r[1]] for r in ring]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
         "properties": {"level": "Snow"}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [far]},
         "properties": {"level": "Cloudy" if level != "Cloudy" else "Rain"}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
         "properties": {"level": level}},
    ]}


def _points(value):
    lat, lon = HOME
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon + 0.5, lat]},
         "properties": {"value": "99"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon + 0.01, lat + 0.01]},
         "properties": {"value": str(value)}},
    ]}


def grid_temp(validtime):
    return GRID_VALID.index(validtime)  # 0, 1, 2, ... ℃


async def grid_layer(basetime, validtime, element):
    import gzip

    assert basetime == GRID_BASE
    if element == "wm":
        data = _square(GRID_WM.get(validtime, "Clear"))
    elif element == "r3":
        # 1mm 未満の所は区分の多角形に入らない
        data = _square(GRID_R3[validtime]) if validtime in GRID_R3 else {"type": "FeatureCollection", "features": []}
    elif element == "temp_point":
        data = _points(grid_temp(validtime))
    elif element == "min_temp_point":
        data = _points(-3)
    else:
        data = _points(21)
    return gzip.compress(json.dumps(data).encode())


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
        mock.grid_times.return_value = GRID_TIMES
        mock.grid_layer.side_effect = grid_layer
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
    assert st.attributes["hourly_source"] == "jma_grid+open_meteo"
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
    # 今日は天気分布予報に最高・最低が無いので Open-Meteo、明日は自宅のマスの値
    assert (fc[0]["temperature"], fc[0]["templow"]) == om_day(0)
    assert (fc[1]["temperature"], fc[1]["templow"]) == (21, -3)
    assert fc[7]["temperature"] == om_day(7)[0]
    assert all("condition" in d for d in fc)
    assert all("precipitation_probability" in d for d in fc[1:])

    hourly = await _forecast(hass, entity_id, "hourly")
    assert len(hourly) == 48
    by_time = {h["datetime"][:16]: h for h in hourly}
    i = OM["hourly"]["time"].index("2026-11-17T12:00")
    h12 = by_time["2026-11-17T12:00"]
    # 12〜15時: 自宅のマスは雨・10〜15mm/3時間、気温は 12時 0℃ と 15時 1℃ の間
    assert h12["condition"] == "rainy"
    assert h12["precipitation"] == 4.2
    assert h12["temperature"] == 0.0
    assert by_time["2026-11-17T13:00"]["temperature"] == 0.3
    # 湿度・風は Open-Meteo
    assert h12["humidity"] == OM["hourly"]["relative_humidity_2m"][i]
    # 降水確率は気象庁の6時間ごと（空知地方 12〜18時）
    pops = {a["area"]["code"]: a["pops"] for a in load("forecast_016000.json")[0]["timeSeries"][1]["areas"]}
    assert h12["precipitation_probability"] == int(pops["016020"][0])
    # 15〜18時はくもり、降水量 0
    assert by_time["2026-11-17T16:00"]["condition"] == "cloudy"
    assert by_time["2026-11-17T16:00"]["precipitation"] == 0.0
    # 晴れは 18時から夜の晴れ、6時から昼の晴れ
    assert by_time["2026-11-17T19:00"]["condition"] == "clear-night"
    assert by_time["2026-11-18T07:00"]["condition"] == "sunny"
    # 天気分布予報の範囲（翌日24時）より後は Open-Meteo
    j = OM["hourly"]["time"].index("2026-11-19T01:00")
    assert by_time["2026-11-19T01:00"]["temperature"] == OM["hourly"]["temperature_2m"][j]


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


@pytest.mark.freeze_time("2026-11-17T11:00:00+00:00")  # 日本時間 20時（夜）
async def test_night_condition_from_home_cell(hass: HomeAssistant, setup_home, api):
    """夜で降っていなければ、自宅のマスの今の3時間の天気（晴れ → 夜の晴れ）."""
    api.observations.return_value = [o for o in observations() if o.precipitation_10m in (0, None)]
    for o in api.observations.return_value:
        o.time = o.time + timedelta(hours=7, minutes=30)  # 20時ごろの観測にする
        o.precipitation_10m = 0.0
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["condition_source"] == "jma_grid"
    assert st.state == "clear-night"


@pytest.mark.freeze_time(NOW)
async def test_grid_down(hass: HomeAssistant, setup_home, api, caplog):
    api.grid_layer.side_effect = JmaError("down")
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["hourly_source"] == "open_meteo"
    assert any(p.startswith("天気分布予報") for p in st.attributes["problems"])
    fc = await _daily(hass, _entity_id(hass))
    assert (fc[1]["temperature"], fc[1]["templow"]) == om_day(1)


@pytest.mark.freeze_time(NOW)
async def test_grid_home_outside(hass: HomeAssistant, setup_home, api, caplog):
    """自宅がどの区分にも入らない（形式の変化・範囲外）なら使わずに警告."""
    async def far(basetime, validtime, element):
        import gzip
        return gzip.compress(json.dumps({"type": "FeatureCollection", "features": []}).encode())
    api.grid_layer.side_effect = far
    await _setup(hass, entry())
    st = hass.states.get(_entity_id(hass))
    assert st.attributes["hourly_source"] == "open_meteo"
    assert "形式が変わった可能性" in caplog.text


async def test_grid_fetched_once_per_issue(hass: HomeAssistant, setup_home, api, freezer):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.move_to(NOW)
    await _setup(hass, entry())
    layers = api.grid_layer.await_count
    assert layers == 13 * 3 + 2
    for _ in range(4):  # 20分
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert api.grid_times.await_count == 2  # 15分ごとに発表を確認
    assert api.grid_layer.await_count == layers  # 同じ発表は取り直さない
