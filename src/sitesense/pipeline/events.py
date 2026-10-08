"""Turn the threshold table (config/weather_thresholds.csv) into daily event flags."""

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from sitesense.pipeline import config
from sitesense.pipeline.config import City

USABLE_STATUS = {"universal", "verified", "older_sheet", "older_names"}
EVENT_NAMES = {
    "heavy_rain": "Heavy rain/snow total",
    "ice_day": "Ice day",
    "heat": "Heat",
    "cold": "Cold",
    "snow": "Snow",
}
LEVEL_NAMES = {
    "moderate": "moderate",
    "heavy": "heavy",
    "very_heavy": "very heavy",
    "ice_day": "max temp < 0 °C",
    "advisory": "advisory criteria",
    "warning": "warning criteria",
    "code_blue": "Code Blue",
    "freeze_warning": "freeze criteria",
}


@dataclass(frozen=True)
class Rule:
    event_type: str
    threshold_level: str
    office: str
    variable: str
    operator: str
    value: str
    unit: str
    note: str
    status: str
    source_url: str

    @property
    def label(self) -> str:
        if self.variable == "precipitation_daily" and self.value == "p95_wet_days":
            core = "rain/snow > local wet-day p95"
        else:
            names = {
                "precipitation_daily": "rain/snow",
                "tmax_daily": "max temp",
                "heat_index": "heat index",
                "wind_chill": "wind chill",
                "wind_chill_or_temp": "wind chill or temp",
                "snowfall": "snowfall",
                "temperature": "min temp",
            }
            unit = {"C": "°C", "F": "°F", "in": "in", "mm": "mm/day"}.get(self.unit, self.unit)
            op = {">=": "≥", "<=": "≤"}.get(self.operator, self.operator)
            core = f"{names.get(self.variable, self.variable)} {op} {self.value} {unit}"
            seasonal = re.search(r"(\d+)-\d+ F .*through Jun 30; (\d+)-\d+ F from Jul 1", self.note)
            if seasonal:
                core = (
                    f"heat index ≥ {seasonal.group(1)} °F to 30 Jun, ≥ {seasonal.group(2)} °F after"
                )
            if "2 consecutive hours" in self.note:
                core += ", 2 h"
            also_temp = re.search(r"or (?:air )?temperature >= (\d+) F", self.note)
            if also_temp:
                core += f", or temp ≥ {also_temp.group(1)} °F"
            if "or precipitation with temperature" in self.note:
                core += ", or rain/snow at ≤ 32 °F"
        event = EVENT_NAMES.get(self.event_type, self.event_type)
        if self.event_type == "ice_day":
            return f"{event} ({core})"
        return f"{event}, {LEVEL_NAMES.get(self.threshold_level, self.threshold_level)} ({core})"


def load_rules(city: City, path: Path = config.THRESHOLDS_FILE) -> list[Rule]:
    """Universal rules plus the city's own rules (empty value = not available)."""
    table = pd.read_csv(path, dtype=str).fillna("")
    own = (table.city == city.name) & (table.state == city.state)
    rows = table[(table.city == "ALL") | own]
    fields = ("event", "level", "office", "variable", "operator", "value", "unit",
              "condition_note", "status", "source_url")  # fmt: skip
    return [Rule(*(str(r[f]) for f in fields)) for r in rows.to_dict("records")]


def _compare(values: pd.Series, operator: str, threshold: float | pd.Series) -> pd.Series:
    ops = {">=": values >= threshold, ">": values > threshold,
           "<=": values <= threshold, "<": values < threshold}  # fmt: skip
    return ops[operator]


def flags(rule: Rule, daily: pd.DataFrame, wet_p95: pd.Series) -> pd.Series | None:
    """Boolean flag per row of `daily` (one cell-day per row), or None when not evaluable."""
    if rule.status not in USABLE_STATUS or not rule.variable or not rule.value:
        return None
    note = rule.note
    if rule.variable == "precipitation_daily":
        if rule.value == "p95_wet_days":
            threshold: float | pd.Series = daily.weather_cell_id.map(wet_p95)
        else:
            threshold = float(rule.value)
        return _compare(daily.precip_mm, rule.operator, threshold)
    if rule.variable == "tmax_daily":
        return _compare(daily.tmax, rule.operator, float(rule.value))
    if rule.variable == "heat_index":
        sustained = "2 consecutive hours" in note
        values = daily.hi2h_f if sustained else daily.hi_max_f
        seasonal = re.search(r"(\d+)-\d+ F .*through Jun 30; (\d+)-\d+ F from Jul 1", note)
        if seasonal:
            early = daily.date.dt.month < 7
            threshold = pd.Series(
                np.where(early, float(seasonal.group(1)), float(seasonal.group(2))),
                index=daily.index,
            )
        else:
            threshold = float(rule.value)
        result = _compare(values, rule.operator, threshold)
        also_temp = re.search(r"or (?:air )?temperature >= (\d+) F", note)
        if also_temp:
            result = result | (daily.tmax_f >= float(also_temp.group(1)))
        return result
    if rule.variable in ("wind_chill", "wind_chill_or_temp"):
        result = _compare(daily.wc_min_f, rule.operator, float(rule.value))
        frozen = re.search(r"or precipitation with temperature <= (\d+) F", note)
        if frozen:
            result = result | (daily.frozen_precip_hours >= 1)
        return result
    if rule.variable == "temperature":
        return _compare(daily.tmin_f, rule.operator, float(rule.value))
    if rule.variable == "snowfall":
        if daily.snow_in.isna().all():
            return None
        return _compare(daily.snow_in, rule.operator, float(rule.value))
    return None
