"""Activity aggregations used by the pages: pure pandas, no Streamlit imports."""

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


def daily(activity: pd.DataFrame) -> pd.DataFrame:
    """Check-ins per area and local date."""
    return activity.groupby(["area_id", "obs_date"], as_index=False).agg(
        checkins=("checkins", "sum")
    )


def weekday_hour(activity: pd.DataFrame, area_id: str) -> pd.DataFrame:
    """7 x 24 matrix of total check-ins (rows Mon-Sun, columns hours 0-23)."""
    rows = activity[activity.area_id == area_id]
    table = rows.assign(weekday=rows.obs_date.dt.weekday).pivot_table(
        index="weekday", columns="hour", values="checkins", aggfunc="sum", fill_value=0
    )
    return table.reindex(index=range(7), columns=range(24), fill_value=0)


def monthly_index(activity: pd.DataFrame) -> pd.DataFrame:
    """Average daily check-ins per calendar month, indexed so each area's year average = 100.

    Rows are months 1-12, columns are area ids.
    """
    days = daily(activity)
    days["month"] = days.obs_date.dt.month
    by_month = days.pivot_table(index="month", columns="area_id", values="checkins", aggfunc="mean")
    return by_month / by_month.mean() * 100


def share_by_year(activity: pd.DataFrame, area_id: str) -> pd.Series:
    """The area's share (%) of all check-ins in the metro and category, per year."""
    years = activity.obs_date.dt.year
    totals = activity.groupby(years).checkins.sum()
    area = activity[activity.area_id == area_id]
    area_totals = area.groupby(area.obs_date.dt.year).checkins.sum()
    return (area_totals / totals * 100).dropna()


def summary(activity: pd.DataFrame, area_ids: list[str]) -> pd.DataFrame:
    """One row per area: weekly average, weekday/weekend share, busiest day, peaks."""
    rows = []
    for area_id in area_ids:
        rows_area = activity[activity.area_id == area_id]
        days = daily(rows_area)
        if days.empty:
            continue
        total = float(days.checkins.sum())
        weekday_totals = days.groupby(days.obs_date.dt.weekday).checkins.sum()
        weekend = float(weekday_totals.reindex([5, 6], fill_value=0).sum()) / total if total else 0
        index = monthly_index(rows_area)[area_id]
        hours = rows_area.groupby("hour").checkins.sum()
        peak_hour = label_of_max(hours)
        busiest = label_of_max(weekday_totals)
        rows.append(
            {
                "area_id": area_id,
                "avg_week": total / (len(days) / 7),
                "weekday_share": 1 - weekend,
                "weekend_share": weekend,
                "busiest_day": WEEKDAY_NAMES[busiest],
                "busiest_day_share": float(weekday_totals.max()) / total if total else 0,
                "peak_hour": f"{peak_hour:02d}–{(peak_hour + 1) % 24:02d}h",
                "peak_month": MONTHS[label_of_max(index) - 1],
                "low_month": MONTHS[label_of_min(index) - 1],
                "season_ratio": float(index.max() / index.min()),
            }
        )
    return pd.DataFrame(rows)


def share_change(activity: pd.DataFrame, area_id: str) -> float | None:
    """Relative change in the area's metro share between the last two complete years.

    `activity` must hold every area in the metro, so shares are of the metro total.
    """
    days = activity.groupby(activity.obs_date.dt.year).obs_date.nunique()
    share = share_by_year(activity, area_id)
    complete = share[share.index.isin(days[days >= 365].index)]
    if len(complete) < 2 or complete.iloc[-2] == 0:
        return None
    return float(complete.iloc[-1] / complete.iloc[-2] - 1)


def peak_season(index: pd.Series) -> str:
    """Three-month window with the highest index, e.g. 'Apr – Jun'."""
    values = list(index.reindex(range(1, 13), fill_value=0))
    best = max(range(12), key=lambda m: sum(values[(m + k) % 12] for k in range(3)))
    return f"{MONTHS[best]} – {MONTHS[(best + 2) % 12]}"
