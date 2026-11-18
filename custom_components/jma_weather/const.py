"""定数."""
from datetime import timedelta

DOMAIN = "jma_weather"

CONF_OFFICE = "office"
CONF_AREA = "area"
CONF_STATION = "station"
CONF_TEMP_SOURCE = "temp_source"
CONF_TEMP_STATION = "temp_station"

TEMP_SOURCE_OPEN_METEO = "open_meteo"
TEMP_SOURCE_JMA = "jma"

BASE = "https://www.jma.go.jp/bosai"
URL_AREA = f"{BASE}/common/const/area.json"
URL_AMEDAS_TABLE = f"{BASE}/amedas/const/amedastable.json"
URL_AMEDAS_LATEST = f"{BASE}/amedas/data/latest_time.txt"
URL_AMEDAS_POINT = BASE + "/amedas/data/point/{station}/{name}.json"
URL_FORECAST = BASE + "/forecast/data/forecast/{office}.json"
URL_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
URL_GRID_TIMES = f"{BASE}/jmatile/data/wdist/targetTimes.json"
URL_GRID = BASE + "/jmatile/data/wdist/{basetime}/none/{validtime}/surf/{element}/data.geojson"

UPDATE_INTERVAL = timedelta(minutes=5)
FORECAST_INTERVAL = timedelta(minutes=30)
OPEN_METEO_INTERVAL = timedelta(minutes=30)
# 天気分布予報の発表（5・11・17時）を確認する間隔
GRID_INTERVAL = timedelta(minutes=15)
# 観測がこれより古ければ使わない
OBSERVATION_MAX_AGE = timedelta(hours=2)
# 予報・Open-Meteo の取得に失敗しても、これより新しければ前回の値を使う
FORECAST_MAX_AGE = timedelta(hours=12)
REQUEST_TIMEOUT = 20
