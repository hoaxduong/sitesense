"""Pipeline settings. Provisional choices that await a team decision are marked."""

import os
from dataclasses import dataclass
from pathlib import Path

# Project root holding data/ and config/; set SITESENSE_ROOT when the package is installed.
ROOT = Path(os.environ.get("SITESENSE_ROOT") or Path(__file__).resolve().parents[3])
RAW_DIR = ROOT / "data/raw/yelp_exploration"
ARCHIVE = ROOT / "data/raw/Yelp-JSON.zip"
WEATHER_DIR = ROOT / "data/processed/yelp_weather"
SNOW_DIR = ROOT / "data/processed/yelp_weather_snow"
INTERIM_DIR = ROOT / "data/interim"
THRESHOLDS_FILE = ROOT / "config/weather_thresholds.csv"


@dataclass(frozen=True)
class City:
    name: str
    state: str
    timezone: str
    station: str  # GHCN-Daily airport station, for validation only

    @property
    def metro(self) -> str:
        return f"{self.name}, {self.state}"


# D5 (MVP cities) is pending: every candidate is built and the app lets users switch.
CITIES = (
    City("Philadelphia", "PA", "America/New_York", "USW00013739"),
    City("New Orleans", "LA", "America/Chicago", "USW00012916"),
    City("Indianapolis", "IN", "America/Indiana/Indianapolis", "USW00093819"),
    City("Tampa", "FL", "America/New_York", "USW00012842"),
    City("Nashville", "TN", "America/Chicago", "USW00013897"),
)
# D6b: Restaurants has the most check-ins in every candidate city; others are for the filter.
CATEGORIES = ("Restaurants", "Coffee & Tea", "Food", "Bars")

CHECKIN_TIMEZONE = "UTC"  # D7 pending; strong evidence for UTC (T020 report, section 7)
METHOD_VERSION = "t020-v1"

ACTIVITY_START = "2012-01-01"
EFFECT_START, EFFECT_END = "2015-01-01", "2020-02-29"  # pre-COVID comparison window
FACTOR_START, FACTOR_END = "2017-01-01", "2019-12-31"  # recent pre-COVID years
SEASON_START, SEASON_END = "2015-01-01", "2019-12-31"
CLIMATE_START, CLIMATE_END = "2010-01-01", "2021-12-31"  # base period for wet-day p95
TYPICAL_YEARS = (2016, 2017, 2018, 2019)
TREND_YEARS = range(2017, 2022)

MIN_BUSINESSES = 5  # candidate area = ZIP code with at least this many businesses
COMPETITOR_RADIUS_KM = 1.0
MIN_EVENTS = 20
BASELINE_WEEKS = 4
BOOTSTRAP = 1000
SEED = 20261024
SNOW_CM_PER_MM_SWE = 0.7  # Open-Meteo convention; stations show about 8:1 snow:water
WET_DAY_MM = 1.0
