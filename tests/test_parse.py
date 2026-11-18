"""気象庁データの変換（HA を使わない部分）のテスト."""
from datetime import date, datetime
import json
from pathlib import Path

import pytest

from custom_components.jma_weather.parse import (
    CLEAR_NIGHT,
    CLOUDY,
    FOG,
    LIGHTNING_RAINY,
    Observation,
    PARTLYCLOUDY,
    POURING,
    RAINY,
    SNOWY,
    SNOWY_RAINY,
    SUNNY,
    condition_for_code,
    current_condition,
    forecast_areas,
    parse_amedas_point,
    parse_forecast,
    point_file_names,
    temperature_stations,
)
from custom_components.jma_weather.telops import TELOPS

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("100", SUNNY),
        ("101", PARTLYCLOUDY),  # 晴時々曇
        ("111", PARTLYCLOUDY),  # 晴後曇
        ("102", RAINY),  # 晴一時雨（気象庁の分類は雨）
        ("104", SNOWY),  # 晴一時雪
        ("106", SNOWY_RAINY),  # 晴一時雨か雪
        ("108", LIGHTNING_RAINY),
        ("200", CLOUDY),
        ("201", PARTLYCLOUDY),  # 曇時々晴
        ("209", FOG),
        ("214", RAINY),  # 曇後雨
        ("300", RAINY),
        ("306", POURING),  # 大雨
        ("308", POURING),
        ("350", LIGHTNING_RAINY),
        ("329", SNOWY_RAINY),  # 雨一時みぞれ
        ("400", SNOWY),
        ("340", SNOWY_RAINY),  # 雪か雨
        ("450", SNOWY),  # 雪で雷を伴う
        ("999", None),
        ("", None),
        (None, None),
        ("199", SUNNY),  # 未知のコードは先頭の数字で
        ("299", CLOUDY),
    ],
)
def test_condition_for_code(code, expected):
    assert condition_for_code(code) == expected


def test_every_code_has_a_condition():
    for code in TELOPS:
        assert condition_for_code(code) is not None, code


def test_parse_forecast_single_area_11():
    data = load("forecast_270000.json")
    assert forecast_areas(data) == [("270000", "大阪府")]
    assert temperature_stations(data) == [("62078", "大阪")]
    fc = parse_forecast(data, "270000", "62078")
    assert fc.area_name == "大阪府"
    assert fc.report_time == datetime.fromisoformat("2026-11-17T11:00:00+09:00")
    days = {d.day: d for d in fc.days}
    assert min(days) == date(2026, 11, 17)
    assert max(days) == date(2026, 11, 24)
    today = days[date(2026, 11, 17)]
    assert today.code == "114"  # 晴後雨
    assert today.condition == RAINY
    assert today.text == "晴れ 後 くもり 夕方 から 雨"
    assert today.precipitation_probability == 80  # 12-18時 50, 18-24時 80
    assert today.temp_max == 19
    assert today.temp_min is None  # 11時の発表に今日の最低は無い（2番目の値は使わない）
    tomorrow = days[date(2026, 11, 18)]
    assert tomorrow.code == "203"
    assert tomorrow.precipitation_probability == 50  # 6時間ごと 50,20,50,50
    assert (tomorrow.temp_min, tomorrow.temp_max) == (14, 19)
    # 3日目は週間予報の降水確率
    day3 = days[date(2026, 11, 19)]
    assert day3.code == "201"
    assert day3.precipitation_probability == 20


def test_parse_forecast_weekly_area_differs():
    """北海道（石狩・空知・後志）は週間予報の地域が一次細分区域と違う."""
    data = load("forecast_016000.json")
    assert [c for c, _ in forecast_areas(data)] == ["016010", "016020", "016030"]
    fc = parse_forecast(data, "016020", "15356")
    assert fc.area_name == "空知地方"
    days = {d.day: d for d in fc.days}
    assert len(days) == 8
    # 週間予報の日も天気・降水確率がある
    last = days[max(days)]
    assert last.condition is not None
    assert last.precipitation_probability is not None
    # 3日予報の気温は指定の地点（岩見沢）
    short_temps = {a["area"]["code"]: a["temps"] for a in data[0]["timeSeries"][2]["areas"]}
    assert days[date(2026, 11, 17)].temp_max == float(short_temps["15356"][0])


def test_parse_forecast_17_report():
    data = load("forecast_270000.json")
    data[0]["reportDatetime"] = "2026-11-17T17:00:00+09:00"
    data[0]["timeSeries"][2]["timeDefines"] = [
        "2026-11-18T00:00:00+09:00",
        "2026-11-18T09:00:00+09:00",
    ]
    data[0]["timeSeries"][2]["areas"][0]["temps"] = ["12", "20"]
    fc = parse_forecast(data, "270000", "62078")
    days = {d.day: d for d in fc.days}
    assert days[date(2026, 11, 17)].temp_max is None
    assert (days[date(2026, 11, 18)].temp_min, days[date(2026, 11, 18)].temp_max) == (12, 20)


def test_parse_forecast_tolerates_broken_parts():
    data = load("forecast_270000.json")
    data[0]["timeSeries"][1]["areas"][0]["pops"] = ["", "x"]
    data[1]["timeSeries"] = []
    fc = parse_forecast(data, "270000", None)
    today = fc.get(date(2026, 11, 17))
    assert today.precipitation_probability is None
    assert today.condition == RAINY
    assert parse_forecast([], "270000").days == []


def test_parse_amedas_point():
    obs = parse_amedas_point(load("amedas_14163_20261117_12.json"))
    assert [o.time for o in obs] == sorted(o.time for o in obs)
    last = obs[-1]
    assert last.time == datetime(2026, 11, 17, 12, 30)
    raw = load("amedas_14163_20261117_12.json")["20261117123000"]
    assert last.temperature == raw["temp"][0]
    assert last.humidity == raw["humidity"][0]
    assert last.wind_speed == raw["wind"][0]
    assert last.sun_10m == raw["sun10m"][0]


def test_parse_amedas_point_missing_values():
    """欠測中の地点は値の無い記録だけが届く."""
    obs = parse_amedas_point(load("amedas_missing.json"))
    assert len(obs) == 4
    assert all(o.temperature is None and o.humidity is None for o in obs)


def test_wind_bearing():
    rec = {"temp": [10.0, 0], "wind": [2.0, 0]}
    for direction, bearing in [(16, 0.0), (4, 90.0), (8, 180.0), (12, 270.0), (1, 22.5)]:
        obs = parse_amedas_point({"20261117120000": {**rec, "windDirection": [direction, 0]}})
        assert obs[0].wind_bearing == bearing
    calm = parse_amedas_point({"20261117120000": {**rec, "wind": [0.0, 0], "windDirection": [0, 0]}})
    assert calm[0].wind_bearing is None
    missing = parse_amedas_point({"20261117120000": {"temp": [None, 5]}, "bad": {}})
    assert len(missing) == 1 and missing[0].temperature is None


def test_point_file_names():
    assert point_file_names(datetime(2026, 11, 17, 12, 20)) == ["20261117_12", "20261117_09"]
    assert point_file_names(datetime(2026, 11, 17, 13, 50)) == ["20261117_12"]
    assert point_file_names(datetime(2026, 11, 17, 0, 10)) == ["20261117_00", "20261116_21"]


def _obs(minute, rain=0.0, sun=None, temp=10.0):
    return Observation(
        time=datetime(2026, 11, 17, 12, minute), temperature=temp,
        precipitation_10m=rain, sun_10m=sun,
    )


def test_current_condition_rain():
    assert current_condition([_obs(0, 0.5), _obs(10), _obs(20)], SUNNY, 30) == (
        RAINY, "amedas_precipitation")
    assert current_condition([_obs(20, 4.0)], CLOUDY, 30)[0] == POURING
    assert current_condition([_obs(20, 0.5, temp=0.5)], RAINY, 30)[0] == SNOWY
    assert current_condition([_obs(20, 0.5, temp=2.5)], SNOWY_RAINY, 30)[0] == SNOWY_RAINY
    assert current_condition([_obs(20, 0.5, temp=5)], SNOWY_RAINY, 30)[0] == RAINY
    assert current_condition([_obs(20, 0.5)], LIGHTNING_RAINY, -5)[0] == LIGHTNING_RAINY


def test_current_condition_sunshine():
    full = [_obs(0, sun=10), _obs(10, sun=9), _obs(20, sun=10)]
    assert current_condition(full, RAINY, 40) == (SUNNY, "amedas_sunshine")
    part = [_obs(0, sun=2), _obs(10, sun=3), _obs(20, sun=4)]
    assert current_condition(part, SUNNY, 40)[0] == PARTLYCLOUDY
    none = [_obs(0, sun=0), _obs(10, sun=0), _obs(20, sun=1)]
    assert current_condition(none, SUNNY, 40)[0] == CLOUDY
    # 太陽が低いときは日照で決めない
    assert current_condition(none, SUNNY, 5) == (SUNNY, "forecast")


def test_current_condition_from_forecast():
    dry = [_obs(20)]
    assert current_condition(dry, SUNNY, -10) == (CLEAR_NIGHT, "forecast")
    assert current_condition(dry, RAINY, -10) == (CLOUDY, "forecast")
    assert current_condition(dry, PARTLYCLOUDY, -10) == (PARTLYCLOUDY, "forecast")
    assert current_condition(dry, SUNNY, None) == (SUNNY, "forecast")
    assert current_condition([], None, 10) == (None, "none")


from datetime import timedelta, timezone  # noqa: E402

from custom_components.jma_weather.parse import (  # noqa: E402
    condition_for_wmo,
    parse_open_meteo,
)

JST = timezone(timedelta(hours=9))


@pytest.mark.parametrize(
    ("code", "is_day", "expected"),
    [
        (0, 1, SUNNY), (1, 1, SUNNY), (0, 0, CLEAR_NIGHT), (2, 1, PARTLYCLOUDY),
        (3, 0, CLOUDY), (45, 1, FOG), (61, 1, RAINY), (65, 1, POURING),
        (66, 1, SNOWY_RAINY), (73, 1, SNOWY), (95, 1, LIGHTNING_RAINY), (99, 1, "hail"),
        (None, 1, None), (12, 1, None),
    ],
)
def test_condition_for_wmo(code, is_day, expected):
    assert condition_for_wmo(code, is_day) == expected


def test_parse_open_meteo():
    raw = load("open_meteo_sapporo.json")
    om = parse_open_meteo(raw, JST)
    assert om.current.temperature == raw["current"]["temperature_2m"]
    assert om.current.time.tzinfo is not None
    assert len(om.hours) == len(raw["hourly"]["time"])
    assert om.hours[0].time == datetime(2026, 11, 17, 0, 0, tzinfo=JST)
    assert len(om.days) == 10
    d0 = om.days[date(2026, 11, 17)]
    assert d0.temp_max == raw["daily"]["temperature_2m_max"][0]
    assert d0.precipitation_probability == raw["daily"]["precipitation_probability_max"][0]


def test_parse_open_meteo_broken():
    with pytest.raises(ValueError):
        parse_open_meteo([], JST)
    with pytest.raises(ValueError):
        parse_open_meteo({"error": True, "reason": "x"}, JST)
    om = parse_open_meteo({"hourly": {"time": ["2026-11-17T00:00", "bad"], "temperature_2m": [1.0]}}, JST)
    assert len(om.hours) == 1 and om.hours[0].condition is None


from custom_components.jma_weather.api import JmaError, load_grid  # noqa: E402
from custom_components.jma_weather.parse import (  # noqa: E402
    GridForecast,
    GridStep,
    grid_level,
    grid_point_value,
    parse_target_times,
    utc_stamp,
)


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _poly(level, *rings):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": list(rings)},
            "properties": {"level": level}}


SQ = [[135.0, 34.7], [135.1, 34.7], [135.1, 34.6], [135.0, 34.6], [135.0, 34.7]]
SQ2 = [[136.0, 34.7], [136.1, 34.7], [136.1, 34.6], [136.0, 34.6], [136.0, 34.7]]


def test_grid_level():
    data = _fc(_poly("Snow"), _poly("Cloudy", SQ2), _poly("Rain", SQ2, SQ))
    assert grid_level(data, 34.65, 135.05) == "Rain"  # 離れた2つ目のマス
    assert grid_level(data, 34.65, 136.05) == "Cloudy"
    assert grid_level(data, 34.65, 135.5) is None
    with pytest.raises(ValueError):
        grid_level([], 0, 0)
    with pytest.raises(ValueError):
        grid_level({"type": "x"}, 0, 0)


def test_grid_point_value():
    pts = _fc(
        {"geometry": {"coordinates": [135.531, 34.675]}, "properties": {"value": "11"}},
        {"geometry": {"coordinates": [135.594, 34.675]}, "properties": {"value": "12"}},
        {"geometry": {"coordinates": "bad"}, "properties": {}},
    )
    assert grid_point_value(pts, 34.66, 135.52) == 11
    assert grid_point_value(pts, 34.67, 135.58) == 12
    assert grid_point_value(pts, 35.5, 135.52) is None  # 近い点が無い


def test_load_grid():
    import gzip

    assert load_grid(gzip.compress(b'{"a": 1}')) == {"a": 1}
    assert load_grid(b'{"a": 1}') == {"a": 1}
    with pytest.raises(JmaError):
        load_grid(b"\x1f\x8bbroken")
    with pytest.raises(JmaError):
        load_grid(gzip.compress(b"x" * (33 * 1024 * 1024)))


def test_parse_target_times():
    data = [
        {"basetime": "20261117020000", "validtime": "20261117060000", "elements": ["wm"]},
        {"basetime": "20261117080000", "validtime": "20261117120000", "elements": ["wm", "r3"]},
        {"basetime": "20261117080000", "validtime": "20261117090000", "elements": ["temp_point"]},
        "junk",
    ]
    base, steps = parse_target_times(data)
    assert base == "20261117080000"
    assert steps == [("20261117090000", ["temp_point"]), ("20261117120000", ["wm", "r3"])]
    assert utc_stamp(base) == datetime(2026, 11, 17, 8, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_target_times({})


def test_grid_forecast_lookup():
    t = datetime(2026, 11, 17, 18, tzinfo=JST)
    steps = [
        GridStep(end=t, condition=None, temperature=9.0),  # 最初は気温だけ
        GridStep(end=t + timedelta(hours=3), condition=SUNNY, temperature=12.0),
        GridStep(end=t + timedelta(hours=6), condition=RAINY, rain_level="20<=mm", temperature=6.0),
    ]
    g = GridForecast(basetime=t, steps=steps, daily_min={}, daily_max={})
    assert g.step_at(t - timedelta(hours=1)) is None
    s1 = g.step_at(t + timedelta(hours=1))
    assert s1.condition_at(t + timedelta(hours=1)) == CLEAR_NIGHT
    assert s1.precipitation_per_hour == 0.0
    s2 = g.step_at(t + timedelta(hours=4))
    assert s2.condition_at(t + timedelta(hours=4)) == POURING
    assert s2.precipitation_per_hour == 8.3
    assert g.temperature_at(t + timedelta(hours=1)) == 10.0
    assert g.temperature_at(t + timedelta(hours=4)) == 10.0
    assert g.temperature_at(t + timedelta(hours=7)) is None
    day = GridStep(end=datetime(2026, 11, 18, 9, tzinfo=JST), condition=SUNNY)
    assert day.condition_at(datetime(2026, 11, 18, 7, tzinfo=JST)) == SUNNY
