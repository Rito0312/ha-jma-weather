"""気象庁のデータを Home Assistant の天気に変換する（HA に依存しない部分）."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any

from .telops import TELOPS

# Home Assistant の天気（weather の condition）
SUNNY = "sunny"
CLEAR_NIGHT = "clear-night"
PARTLYCLOUDY = "partlycloudy"
CLOUDY = "cloudy"
FOG = "fog"
RAINY = "rainy"
POURING = "pouring"
LIGHTNING_RAINY = "lightning-rainy"
SNOWY = "snowy"
SNOWY_RAINY = "snowy-rainy"

RAIN_LIKE = {RAINY, POURING, LIGHTNING_RAINY, SNOWY, SNOWY_RAINY}

# 大雨・暴風雨・強く降る
_POURING_CODES = {"306", "308", "328"}


def condition_for_code(code: str | None) -> str | None:
    """天気予報コードを HA の天気にする.

    分け方は気象庁自身の代表の天気（晴れ・くもり・雨・雪）に従い、
    その中を日本語の天気で細かくする（例: 晴一時雨は「雨」の仲間なので rainy）。
    """
    if not code:
        return None
    entry = TELOPS.get(str(code))
    if entry is None:
        group = {"1": "100", "2": "200", "3": "300", "4": "400"}.get(str(code)[:1])
        text = ""
        if group is None:
            return None
    else:
        group, text = entry
    if group == "100":
        return PARTLYCLOUDY if "曇" in text else SUNNY
    if group == "200":
        if text == "霧":
            return FOG
        return PARTLYCLOUDY if "晴" in text else CLOUDY
    if group == "300":
        if "雷" in text:
            return LIGHTNING_RAINY
        if "雪" in text or "みぞれ" in text:
            return SNOWY_RAINY
        if str(code) in _POURING_CODES:
            return POURING
        return RAINY
    if group == "400":
        if "雨" in text or "みぞれ" in text:
            return SNOWY_RAINY
        return SNOWY
    return None


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _find_area(areas: list[dict], code: str) -> dict | None:
    for area in areas:
        if area.get("area", {}).get("code") == code:
            return area
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in ("", None):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass
class ForecastDay:
    """1日分の予報."""

    day: date
    code: str | None = None
    condition: str | None = None
    text: str | None = None
    precipitation_probability: int | None = None
    temp_max: float | None = None
    temp_min: float | None = None


@dataclass
class Forecast:
    """府県天気予報（3日分と週間）を1つの地域についてまとめたもの."""

    report_time: datetime | None
    area_name: str | None
    days: list[ForecastDay] = field(default_factory=list)
    # 6時間ごとの降水確率 [(始まり, 終わり, %)]
    pop_blocks: list[tuple[datetime, datetime, int]] = field(default_factory=list)

    def pop_at(self, when: datetime) -> int | None:
        for start, end, pop in self.pop_blocks:
            if start <= when < end:
                return pop
        return None

    def get(self, day: date) -> ForecastDay | None:
        for item in self.days:
            if item.day == day:
                return item
        return None


def forecast_areas(data: list[dict]) -> list[tuple[str, str]]:
    """予報の地域（一次細分区域）の一覧 [(コード, 名前)]."""
    try:
        areas = data[0]["timeSeries"][0]["areas"]
    except (IndexError, KeyError, TypeError):
        return []
    return [(a["area"]["code"], a["area"]["name"]) for a in areas if "area" in a]


def temperature_stations(data: list[dict]) -> list[tuple[str, str]]:
    """予想気温の地点の一覧 [(アメダスのコード, 名前)]."""
    try:
        areas = data[0]["timeSeries"][2]["areas"]
    except (IndexError, KeyError, TypeError):
        return []
    return [(a["area"]["code"], a["area"]["name"]) for a in areas if "area" in a]


def parse_forecast(
    data: list[dict], area_code: str, temp_station: str | None = None
) -> Forecast:
    """府県天気予報の JSON から、地域の日ごとの予報を作る.

    - 天気: 3日分の予報があればそれ、無ければ週間予報
    - 降水確率: 6時間ごとの降水確率がある日はその最大、無ければ週間予報の値
    - 気温: temp_station（予想気温の地点）の値。3日分の予報の 00時=最低、09時=最高、無ければ週間予報
    """
    days: dict[date, ForecastDay] = {}
    pop_blocks: list[tuple[datetime, datetime, int]] = []

    def day_of(d: date) -> ForecastDay:
        if d not in days:
            days[d] = ForecastDay(day=d)
        return days[d]

    report_time = None
    area_name = None
    short = data[0] if data else {}
    weekly = data[1] if len(data) > 1 else {}
    try:
        report_time = _dt(short["reportDatetime"])
    except (KeyError, TypeError, ValueError):
        pass

    short_ts = short.get("timeSeries", []) if isinstance(short, dict) else []
    weekly_ts = weekly.get("timeSeries", []) if isinstance(weekly, dict) else []

    # 週間予報（地域が細かく分かれていない県もあるので、同じコードが無ければ最初の地域）
    if len(weekly_ts) > 0:
        ts = weekly_ts[0]
        areas = ts.get("areas", [])
        area = _find_area(areas, area_code) or (areas[0] if areas else None)
        if area:
            codes = area.get("weatherCodes", [])
            pops = area.get("pops", [])
            for i, t in enumerate(ts.get("timeDefines", [])):
                item = day_of(_dt(t).date())
                if i < len(codes) and codes[i]:
                    item.code = codes[i]
                if i < len(pops):
                    pop = _int_or_none(pops[i])
                    if pop is not None:
                        item.precipitation_probability = pop
    if len(weekly_ts) > 1:
        ts = weekly_ts[1]
        areas = ts.get("areas", [])
        area = (_find_area(areas, temp_station) if temp_station else None) or (
            areas[0] if areas else None
        )
        if area:
            maxs = area.get("tempsMax", [])
            mins = area.get("tempsMin", [])
            for i, t in enumerate(ts.get("timeDefines", [])):
                item = day_of(_dt(t).date())
                if i < len(maxs) and (v := _float_or_none(maxs[i])) is not None:
                    item.temp_max = v
                if i < len(mins) and (v := _float_or_none(mins[i])) is not None:
                    item.temp_min = v

    # 3日分の予報（週間予報より優先）
    if len(short_ts) > 0:
        ts = short_ts[0]
        area = _find_area(ts.get("areas", []), area_code)
        if area:
            area_name = area["area"].get("name")
            codes = area.get("weatherCodes", [])
            texts = area.get("weathers", [])
            for i, t in enumerate(ts.get("timeDefines", [])):
                item = day_of(_dt(t).date())
                if i < len(codes) and codes[i]:
                    item.code = codes[i]
                if i < len(texts) and texts[i]:
                    item.text = " ".join(texts[i].replace("　", " ").split())
    if len(short_ts) > 1:
        ts = short_ts[1]
        area = _find_area(ts.get("areas", []), area_code)
        if area:
            block_max: dict[date, int] = {}
            pops = area.get("pops", [])
            for i, t in enumerate(ts.get("timeDefines", [])):
                pop = _int_or_none(pops[i]) if i < len(pops) else None
                if pop is None:
                    continue
                start = _dt(t)
                pop_blocks.append((start, start + timedelta(hours=6), pop))
                d = start.date()
                block_max[d] = max(block_max.get(d, 0), pop)
            for d, pop in block_max.items():
                day_of(d).precipitation_probability = pop
    if len(short_ts) > 2:
        ts = short_ts[2]
        areas = ts.get("areas", [])
        area = (_find_area(areas, temp_station) if temp_station else None) or (
            areas[0] if areas else None
        )
        if area and report_time is not None:
            # 並びは発表時刻で決まる（気象庁の天気予報ページの読み方に合わせる）
            #   5時・11時: [今日の最高, (使わない), 明日の最低, 明日の最高]
            #   17時:      [明日の最低, 明日の最高]
            temps = [_float_or_none(v) for v in area.get("temps", [])]
            today = report_time.date()
            tomorrow = today + timedelta(days=1)

            def at(i: int) -> float | None:
                return temps[i] if i < len(temps) else None

            if report_time.hour >= 17:
                pairs = [(tomorrow, at(0), at(1))]
            else:
                pairs = [(today, None, at(0)), (tomorrow, at(2), at(3))]
            for d, low, high in pairs:
                item = day_of(d)
                if low is not None:
                    item.temp_min = low
                if high is not None:
                    item.temp_max = high

    for item in days.values():
        item.condition = condition_for_code(item.code)
        if item.text is None and item.code in TELOPS:
            item.text = TELOPS[item.code][1]

    return Forecast(
        report_time=report_time,
        area_name=area_name,
        days=[days[d] for d in sorted(days)],
        pop_blocks=pop_blocks,
    )


@dataclass
class Observation:
    """アメダスの10分ごとの観測値."""

    time: datetime
    temperature: float | None = None
    humidity: float | None = None
    wind_speed: float | None = None  # m/s
    wind_bearing: float | None = None  # 度。静穏は None
    precipitation_10m: float | None = None
    precipitation_1h: float | None = None
    sun_10m: float | None = None  # 分


def _obs_value(record: dict, key: str) -> float | None:
    value = record.get(key)
    if not isinstance(value, list) or not value:
        return None
    return _float_or_none(value[0])


def parse_amedas_point(data: dict) -> list[Observation]:
    """アメダスの地点データ（3時間分のファイル）を時刻順の観測値にする."""
    result: list[Observation] = []
    if not isinstance(data, dict):
        return result
    for key, record in data.items():
        if not isinstance(record, dict):
            continue
        try:
            when = datetime.strptime(key, "%Y%m%d%H%M%S")
        except ValueError:
            continue
        direction = _obs_value(record, "windDirection")
        wind = _obs_value(record, "wind")
        bearing = None
        if direction is not None and 1 <= direction <= 16:
            bearing = (direction % 16) * 22.5  # 16 は北（0度）
        result.append(
            Observation(
                time=when,
                temperature=_obs_value(record, "temp"),
                humidity=_obs_value(record, "humidity"),
                wind_speed=wind,
                wind_bearing=bearing if wind else None,
                precipitation_10m=_obs_value(record, "precipitation10m"),
                precipitation_1h=_obs_value(record, "precipitation1h"),
                sun_10m=_obs_value(record, "sun10m"),
            )
        )
    result.sort(key=lambda o: o.time)
    return result


def point_file_names(latest: datetime) -> list[str]:
    """最新時刻を含む3時間分のファイル名と、その前のファイル名（30分前までを読むため）."""
    names = []
    for when in (latest, latest - timedelta(minutes=30)):
        name = f"{when:%Y%m%d}_{when.hour // 3 * 3:02d}"
        if name not in names:
            names.append(name)
    return names


# 日照から天気を決める（直近30分の日照時間の割合）
SUNNY_RATIO = 0.7
PARTLY_RATIO = 0.2
# 太陽がこれより低いと、晴れていても日照が少なく出るので日照では判定しない
MIN_SUN_ELEVATION = 10.0
# 10分間の降水量がこれ以上なら激しい雨（10分で3.5mm ≒ 1時間に20mm 程度）
POURING_10M = 3.5


def current_condition(
    recent: list[Observation],
    today_condition: str | None,
    sun_elevation: float | None,
) -> tuple[str | None, str]:
    """今の天気と、その根拠.

    recent: 直近30分（3回分程度）の観測値、古い順
    today_condition: 今日の予報の天気（夜や日照が無い地点で使う）
    sun_elevation: 太陽の高度（度）
    """
    latest = recent[-1] if recent else None
    temp = latest.temperature if latest else None

    rain = [o.precipitation_10m for o in recent if o.precipitation_10m is not None]
    if any(v > 0 for v in rain):
        if temp is not None and temp <= 1.0:
            return SNOWY, "amedas_precipitation"
        if today_condition in (SNOWY, SNOWY_RAINY) and temp is not None and temp <= 3.0:
            return SNOWY_RAINY, "amedas_precipitation"
        if today_condition == LIGHTNING_RAINY:
            return LIGHTNING_RAINY, "amedas_precipitation"
        if rain and rain[-1] >= POURING_10M:
            return POURING, "amedas_precipitation"
        return RAINY, "amedas_precipitation"

    night = sun_elevation is not None and sun_elevation < 0

    sun = [o.sun_10m for o in recent if o.sun_10m is not None]
    if sun and sun_elevation is not None and sun_elevation >= MIN_SUN_ELEVATION:
        ratio = sum(sun) / (10.0 * len(sun))
        if ratio >= SUNNY_RATIO:
            return SUNNY, "amedas_sunshine"
        if ratio >= PARTLY_RATIO:
            return PARTLYCLOUDY, "amedas_sunshine"
        return CLOUDY, "amedas_sunshine"

    # 夜・朝夕・日照の観測が無い地点: 今日の予報から（今は降っていないので雨・雪はくもり）
    if today_condition is None:
        return None, "none"
    condition = today_condition
    if condition in RAIN_LIKE:
        condition = CLOUDY
    if condition == SUNNY and night:
        condition = CLEAR_NIGHT
    return condition, "forecast"


# ---- Open-Meteo（1時間ごとの予報と、気象庁のデータが取れないときの予備） ----

HAIL = "hail"


def condition_for_wmo(code: Any, is_day: Any = 1) -> str | None:
    """Open-Meteo の天気コード（WMO）を HA の天気にする（HA 標準の open_meteo と同じ分け方）."""
    c = _int_or_none(code)
    if c is None:
        return None
    if c in (0, 1):
        return SUNNY if is_day not in (0, False) else CLEAR_NIGHT
    if c == 2:
        return PARTLYCLOUDY
    if c == 3:
        return CLOUDY
    if c in (45, 48):
        return FOG
    if c in (51, 53, 55, 61, 63, 80, 81):
        return RAINY
    if c in (65, 82):
        return POURING
    if c in (56, 57, 66, 67):
        return SNOWY_RAINY
    if c in (71, 73, 75, 77, 85, 86):
        return SNOWY
    if c == 95:
        return LIGHTNING_RAINY
    if c in (96, 99):
        return HAIL
    return None


@dataclass
class OpenMeteoCurrent:
    time: datetime | None
    temperature: float | None
    humidity: float | None
    wind_speed: float | None  # m/s
    wind_bearing: float | None
    condition: str | None


@dataclass
class OpenMeteoHour:
    time: datetime
    condition: str | None
    temperature: float | None
    humidity: float | None
    precipitation: float | None
    precipitation_probability: int | None
    wind_speed: float | None  # m/s
    wind_bearing: float | None


@dataclass
class OpenMeteoDay:
    day: date
    condition: str | None
    temp_max: float | None
    temp_min: float | None
    precipitation_probability: int | None


@dataclass
class OpenMeteo:
    current: OpenMeteoCurrent | None
    hours: list[OpenMeteoHour]
    days: dict[date, OpenMeteoDay]


def _local_time(value: Any, tz) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=tz)


def _col(block: dict, key: str, i: int) -> Any:
    values = block.get(key)
    if isinstance(values, list) and i < len(values):
        return values[i]
    return None


def parse_open_meteo(data: Any, tz) -> OpenMeteo:
    """Open-Meteo の応答（timezone 指定・wind_speed_unit=ms）を読む. 時刻は tz の時刻."""
    if not isinstance(data, dict):
        raise ValueError("open-meteo: not an object")
    current = None
    cur = data.get("current")
    if isinstance(cur, dict):
        bearing = _float_or_none(cur.get("wind_direction_10m"))
        current = OpenMeteoCurrent(
            time=_local_time(cur.get("time"), tz),
            temperature=_float_or_none(cur.get("temperature_2m")),
            humidity=_float_or_none(cur.get("relative_humidity_2m")),
            wind_speed=_float_or_none(cur.get("wind_speed_10m")),
            wind_bearing=bearing,
            condition=condition_for_wmo(cur.get("weather_code"), cur.get("is_day", 1)),
        )
    hours: list[OpenMeteoHour] = []
    hourly = data.get("hourly")
    if isinstance(hourly, dict) and isinstance(hourly.get("time"), list):
        for i, t in enumerate(hourly["time"]):
            when = _local_time(t, tz)
            if when is None:
                continue
            hours.append(
                OpenMeteoHour(
                    time=when,
                    condition=condition_for_wmo(
                        _col(hourly, "weather_code", i), _col(hourly, "is_day", i)
                    ),
                    temperature=_float_or_none(_col(hourly, "temperature_2m", i)),
                    humidity=_float_or_none(_col(hourly, "relative_humidity_2m", i)),
                    precipitation=_float_or_none(_col(hourly, "precipitation", i)),
                    precipitation_probability=_int_or_none(
                        _col(hourly, "precipitation_probability", i)
                    ),
                    wind_speed=_float_or_none(_col(hourly, "wind_speed_10m", i)),
                    wind_bearing=_float_or_none(_col(hourly, "wind_direction_10m", i)),
                )
            )
    days: dict[date, OpenMeteoDay] = {}
    daily = data.get("daily")
    if isinstance(daily, dict) and isinstance(daily.get("time"), list):
        for i, t in enumerate(daily["time"]):
            try:
                d = date.fromisoformat(t)
            except (TypeError, ValueError):
                continue
            days[d] = OpenMeteoDay(
                day=d,
                condition=condition_for_wmo(_col(daily, "weather_code", i)),
                temp_max=_float_or_none(_col(daily, "temperature_2m_max", i)),
                temp_min=_float_or_none(_col(daily, "temperature_2m_min", i)),
                precipitation_probability=_int_or_none(
                    _col(daily, "precipitation_probability_max", i)
                ),
            )
    if current is None and not hours and not days:
        raise ValueError("open-meteo: no data")
    return OpenMeteo(current=current, hours=hours, days=days)


# ---- 天気分布予報（約5km四方のマス、3時間ごと） ----
#
# 気象庁の天気分布予報の地図用データ（GeoJSON）から、1つの地点を含むマスの値を読む。
# 実データで確認した決まり（2026-11-17）:
#   - targetTimes.json の basetime・validtime は UTC
#   - 天気（wm）と3時間降水量（r3）は「validtime までの3時間」の値
#     （地域時系列予報の 18:00〜21:00 の天気が validtime 21:00 のマスと一致）
#   - 気温（temp_point）は validtime の時点の値。マスの中心の点で、値は文字列の整数
#   - min_temp_point・max_temp_point はその日の朝の最低・日中の最高（validtime の日付の日）

GRID_CONDITIONS = {
    "Clear": SUNNY,
    "Cloudy": CLOUDY,
    "Rain": RAINY,
    "Rain/snow": SNOWY_RAINY,
    "Snow": SNOWY,
}

# 3時間降水量の区分 → 1時間あたりの目安（区分の中央を3で割る。20mm以上は 25mm として）
R3_PER_HOUR = {
    "1<=mm<5": 1.0,
    "5<=mm<10": 2.5,
    "10<=mm<15": 4.2,
    "15<=mm<20": 5.8,
    "20<=mm": 8.3,
}
# 3時間で20mm以上なら激しい雨
R3_POURING = "20<=mm"


def _point_in_rings(rings: Any, x: float, y: float) -> bool:
    """偶奇規則（穴・離れた複数のマスのどちらの書き方でも正しく判定できる）."""
    inside = False
    if not isinstance(rings, list):
        return False
    for ring in rings:
        if not isinstance(ring, list):
            continue
        for i in range(len(ring) - 1):
            try:
                x1, y1 = float(ring[i][0]), float(ring[i][1])
                x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
            except (TypeError, ValueError, IndexError):
                continue
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def grid_level(data: Any, latitude: float, longitude: float) -> str | None:
    """区分ごとの多角形（wm・r3）から、地点を含む区分の名前. どれにも入らなければ None."""
    if not isinstance(data, dict):
        raise ValueError("grid: not an object")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("grid: no features")
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        level = (feature.get("properties") or {}).get("level")
        coords = geometry.get("coordinates")
        if geometry.get("type") == "Polygon":
            polygons = [coords]
        elif geometry.get("type") == "MultiPolygon":
            polygons = coords if isinstance(coords, list) else []
        else:
            continue
        if any(_point_in_rings(p, longitude, latitude) for p in polygons):
            return level if isinstance(level, str) else None
    return None


# マスの大きさ（経度0.0625度・緯度0.05度）。最も近い点がこれより離れていれば使わない
_MAX_POINT_DISTANCE = 0.06


def grid_point_value(data: Any, latitude: float, longitude: float) -> float | None:
    """マスの中心の点（temp_point など）から、地点に最も近い点の値."""
    if not isinstance(data, dict):
        raise ValueError("grid: not an object")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("grid: no features")
    best = None
    best_d = None
    for feature in features:
        try:
            x, y = feature["geometry"]["coordinates"][:2]
            dx = (float(x) - longitude) / 1.25  # 経度の方がマスが大きい
            dy = float(y) - latitude
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = feature, d
    if best is None or best_d is None or best_d > _MAX_POINT_DISTANCE**2:
        return None
    return _float_or_none((best.get("properties") or {}).get("value"))


@dataclass
class GridStep:
    """validtime までの3時間（天気・降水量）と、validtime の時点の気温."""

    end: datetime
    condition: str | None = None
    rain_level: str | None = None
    temperature: float | None = None

    @property
    def start(self) -> datetime:
        return self.end - timedelta(hours=3)

    @property
    def precipitation_per_hour(self) -> float | None:
        if self.condition is None:
            return None
        return R3_PER_HOUR.get(self.rain_level or "", 0.0)

    def condition_at(self, when: datetime) -> str | None:
        """その時刻の天気（晴れは 6〜18時を昼として晴れ・夜の晴れに分ける. 気象庁のページと同じ）."""
        if self.condition == SUNNY and not 6 <= when.hour < 18:
            return CLEAR_NIGHT
        if self.condition == RAINY and self.rain_level == R3_POURING:
            return POURING
        return self.condition


@dataclass
class GridForecast:
    basetime: datetime
    steps: list[GridStep]
    daily_min: dict[date, float]
    daily_max: dict[date, float]

    def step_at(self, when: datetime) -> GridStep | None:
        for step in self.steps:
            if step.condition is not None and step.start <= when < step.end:
                return step
        return None

    def temperature_at(self, when: datetime) -> float | None:
        """3時間ごとの気温を直線でつないだ値."""
        points = [(s.end, s.temperature) for s in self.steps if s.temperature is not None]
        points.sort()
        for (t1, v1), (t2, v2) in zip(points, points[1:]):
            if t1 <= when <= t2:
                span = (t2 - t1).total_seconds()
                if span <= 0:
                    return v1
                return round(v1 + (v2 - v1) * (when - t1).total_seconds() / span, 1)
        return None


def parse_target_times(data: Any) -> tuple[str, list[tuple[str, list[str]]]]:
    """最新の basetime と、その validtime・要素の一覧（validtime 順）."""
    if not isinstance(data, list) or not data:
        raise ValueError("targetTimes: unexpected format")
    rows = [
        r for r in data
        if isinstance(r, dict) and isinstance(r.get("basetime"), str)
        and isinstance(r.get("validtime"), str) and isinstance(r.get("elements"), list)
    ]
    if not rows:
        raise ValueError("targetTimes: no rows")
    basetime = max(r["basetime"] for r in rows)
    steps = sorted(
        (r["validtime"], [e for e in r["elements"] if isinstance(e, str)])
        for r in rows if r["basetime"] == basetime
    )
    return basetime, steps


def utc_stamp(value: str) -> datetime:
    """targetTimes の時刻（UTC、YYYYmmddHHMMSS）."""
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
