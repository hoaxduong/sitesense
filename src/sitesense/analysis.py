"""Activity aggregations used by the pages: pure pandas, no Streamlit imports.

Inputs are the serving tables: `activity_daily` (sparse: days without check-ins are absent)
and `activity_profile` (check-ins per area, year, weekday and local hour).
"""

from datetime import date

import numpy as np
import pandas as pd

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def label_of_max(series: pd.Series) -> int:
    """Integer index label of the largest value (first one on ties)."""
    return int(series.index[int(np.argmax(series.to_numpy()))])


def label_of_min(series: pd.Series) -> int:
    """Integer index label of the smallest value (first one on ties)."""
    return int(series.index[int(np.argmin(series.to_numpy()))])


def fill_daily(activity: pd.DataFrame, area_ids: list[str], start: date, end: date) -> pd.DataFrame:
    """Long table (area_id, obs_date, checkins) with explicit zero days within [start, end]."""
    dates = pd.date_range(start, end, freq="D")
    wide = activity.pivot_table(
        index="obs_date", columns="area_id", values="checkins", aggfunc="sum"
    )
    wide = wide.reindex(index=dates, columns=area_ids, fill_value=0).fillna(0)
    long = wide.stack().reset_index()
    long.columns = pd.Index(["obs_date", "area_id", "checkins"])
    return long


def weekday_hour(profile: pd.DataFrame, area_id: str, years: range) -> pd.DataFrame:
    """7 x 24 matrix of check-ins (rows Mon-Sun, columns hours 0-23) for the given years."""
    rows = profile[(profile.area_id == area_id) & profile.year.isin(list(years))]
    table = rows.pivot_table(index="weekday", columns="hour", values="checkins", aggfunc="sum")
    return table.reindex(index=range(7), columns=range(24), fill_value=0).fillna(0)


def monthly_index(daily: pd.DataFrame) -> pd.DataFrame:
    """Average daily check-ins per month, indexed so each area's year average = 100.

    `daily` must be zero-filled (see fill_daily). Rows are months 1-12, columns area ids.
    """
    by_month = daily.assign(month=daily.obs_date.dt.month).pivot_table(
        index="month", columns="area_id", values="checkins", aggfunc="mean"
    )
    return by_month / by_month.mean() * 100


def share_by_year(daily: pd.DataFrame, area_id: str) -> pd.Series:
    """The area's share (%) of check-ins across all areas in `daily`, per year."""
    years = daily.obs_date.dt.year
    totals = daily.groupby(years).checkins.sum()
    own = daily[daily.area_id == area_id]
    return (own.groupby(own.obs_date.dt.year).checkins.sum() / totals * 100).dropna()


def summary(
    daily: pd.DataFrame, profile: pd.DataFrame, area_ids: list[str], years: range
) -> pd.DataFrame:
    """One row per area: weekly average, weekday/weekend share, busiest day and peaks."""
    rows = []
    for area_id in area_ids:
        days = daily[daily.area_id == area_id]
        total = float(days.checkins.sum())
        if days.empty or total == 0:
            continue
        weekday_totals = (
            days.groupby(days.obs_date.dt.weekday).checkins.sum().reindex(range(7), fill_value=0)
        )
        weekend = float(weekday_totals.loc[[5, 6]].sum()) / total
        index = monthly_index(days)[area_id]
        hours = weekday_hour(profile, area_id, years).sum()
        peak_hour = label_of_max(hours) if hours.sum() else 12
        rows.append(
            {
                "area_id": area_id,
                "avg_week": total / (len(days) / 7),
                "weekday_share": 1 - weekend,
                "weekend_share": weekend,
                "busiest_day": WEEKDAY_NAMES[label_of_max(weekday_totals)],
                "busiest_day_share": float(weekday_totals.max()) / total,
                "peak_hour": f"{peak_hour:02d}–{(peak_hour + 1) % 24:02d}h",
                "peak_month": MONTHS[label_of_max(index) - 1],
                "low_month": MONTHS[label_of_min(index) - 1],
                "season_ratio": float(index.max() / index.min())
                if index.min() > 0
                else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def share_change(daily: pd.DataFrame, area_id: str) -> float | None:
    """Relative change in the area's share between the last two complete years in `daily`."""
    days = daily.groupby(daily.obs_date.dt.year).obs_date.nunique()
    share = share_by_year(daily, area_id)
    complete = share[share.index.isin(days[days >= 365].index)]
    if len(complete) < 2 or complete.iloc[-2] == 0:
        return None
    return float(complete.iloc[-1] / complete.iloc[-2] - 1)


def peak_season(index: pd.Series) -> str:
    """Three-month window with the highest index, e.g. 'Apr – Jun'."""
    values = list(index.reindex(range(1, 13), fill_value=0))
    best = max(range(12), key=lambda m: sum(values[(m + k) % 12] for k in range(3)))
    return f"{MONTHS[best]} – {MONTHS[(best + 2) % 12]}"
