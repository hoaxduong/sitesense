"""Daily station weather for the Location map time chart (read-only, no schema changes).

Reuses the repository's station report loader and its report-quality policy unchanged, so
values match the Weather observations screen. Only the daily arrangement is new.
"""

from datetime import date

from sitesense import repository
from sitesense.models import WeatherCell

DailyWeather = dict[str, dict[date, dict[str, float]]]


def load_daily_station_weather(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date
) -> DailyWeather:
    """Accepted values per station, report date and variable (tmin/tmax in °C, pcpn in mm).

    Missing or rejected reports are absent, never zero. As in the station summaries, a day
    whose minimum exceeds its maximum keeps neither temperature.
    """
    if end_date < start_date:
        raise ValueError("The end date must be on or after the start date.")
    stations = tuple(cell for cell in cells if cell.source_kind == "acis_station")
    if not stations:
        return {}
    daily: DailyWeather = {}
    for report in repository._load_station_reports(stations, start_date, end_date):
        value = repository._accepted_station_value(report)
        if value is not None:
            daily.setdefault(report.weather_cell_id, {}).setdefault(report.observation_date, {})[
                report.variable
            ] = value
    for days in daily.values():
        for measured in days.values():
            if "tmin" in measured and "tmax" in measured and measured["tmin"] > measured["tmax"]:
                measured.pop("tmin")
                measured.pop("tmax")
    return daily
