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

UPDATE_INTERVAL = timedelta(minutes=5)
FORECAST_INTERVAL = timedelta(minutes=30)
OPEN_METEO_INTERVAL = timedelta(minutes=60)
# 観測がこれより古ければ使わない
OBSERVATION_MAX_AGE = timedelta(hours=2)
# 予報・Open-Meteo の取得に失敗しても、これより新しければ前回の値を使う
FORECAST_MAX_AGE = timedelta(hours=12)
REQUEST_TIMEOUT = 20

ATTRIBUTION = "気象庁（府県天気予報・アメダス）"
ATTRIBUTION_OPEN_METEO = "気象庁（府県天気予報・アメダス）, Open-Meteo（予想気温）"
