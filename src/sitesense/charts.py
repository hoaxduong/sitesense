"""Matplotlib figures for the pages; no Streamlit imports.

Colours: the theme green carries single series and positive values, orange marks
negative values or a highlighted item, and multi-area charts use a fixed categorical order.
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure

from sitesense.analysis import MONTHS, WEEKDAYS

PRIMARY = "#26745a"
NEGATIVE = "#eb6834"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
INK = "#192f29"
MUTED = "#6b7570"
GRID = "#e1e0d9"
BAND = "#f3d9c8"
SEQUENTIAL = LinearSegmentedColormap.from_list("sitesense", ["#eef4f1", "#7fb3a0", "#134a39"])


def _figure(width: float = 7.0, height: float = 3.4) -> tuple[Figure, Axes]:
    figure = Figure(figsize=(width, height), dpi=110, layout="constrained")
    axes = figure.subplots()
    axes.spines[["top", "right"]].set_visible(False)
    axes.spines[["left", "bottom"]].set_color(GRID)
    axes.tick_params(colors=MUTED, labelsize=8, length=0)
    axes.grid(axis="y", color=GRID, linewidth=0.6)
    axes.set_axisbelow(True)
    return figure, axes


def contribution_bars(contribution: pd.DataFrame) -> Figure:
    """Diverging horizontal bars: factor points vs the metro average."""
    table = contribution.iloc[::-1]
    figure, axes = _figure(5.0, 2.8)
    colors = [PRIMARY if p >= 0 else NEGATIVE for p in table.points]
    axes.barh(table.label, table.points, color=colors, height=0.55)
    axes.axvline(0, color=MUTED, linewidth=0.8)
    axes.grid(axis="y", visible=False)
    axes.grid(axis="x", color=GRID, linewidth=0.6)
    limit = max(5.0, float(table.points.abs().max()) * 1.35)
    axes.set_xlim(-limit, limit)
    for y, points in enumerate(table.points):
        text = f"{points:+.1f}".replace("-", "−")
        axes.text(points, y, f" {text}" if points >= 0 else f"{text} ", va="center",
                  ha="left" if points >= 0 else "right", fontsize=8, color=INK)  # fmt: skip
    axes.set_xlabel("Points vs metro average  (− lowers · raises +)", fontsize=8, color=MUTED)
    axes.tick_params(axis="y", labelsize=9, colors=INK)
    return figure


def weekday_hour_heatmap(matrix: pd.DataFrame, first_hour: int = 6) -> Figure:
    """Check-ins by weekday (rows) and hour (columns); darker = busier."""
    data = matrix.loc[:, first_hour:23]
    figure, axes = _figure(7.5, 2.9)
    axes.grid(False)
    image = axes.imshow(data.to_numpy(), aspect="auto", cmap=SEQUENTIAL)
    axes.set_yticks(range(7), WEEKDAYS)
    axes.set_xticks(range(data.shape[1]), [f"{h:02d}" for h in data.columns])
    axes.xaxis.tick_top()
    for spine in axes.spines.values():
        spine.set_visible(False)
    bar = figure.colorbar(image, ax=axes, fraction=0.025, pad=0.01)
    bar.set_ticks([])
    bar.outline.set_visible(False)
    bar.set_label("busier →", fontsize=8, color=MUTED)
    return figure


def area_lines(
    table: pd.DataFrame,
    labels: Sequence[str],
    ticks: Sequence[str],
    ylabel: str,
    reference: float | None = None,
) -> Figure:
    """One line per area (`table` columns) over the rows of `table`."""
    figure, axes = _figure(6.5, 3.2)
    x = np.arange(len(table))
    for slot, (column, label) in enumerate(zip(table.columns, labels, strict=True)):
        axes.plot(x, table[column], color=SERIES[slot % 4], linewidth=2, marker="o",
                  markersize=3.5, label=label)  # fmt: skip
    if reference is not None:
        axes.axhline(reference, color=MUTED, linewidth=0.8, linestyle=":")
    else:
        axes.set_ylim(bottom=0)
    axes.set_xticks(x, ticks)
    axes.set_ylabel(ylabel, fontsize=8, color=MUTED)
    axes.legend(frameon=False, fontsize=8, ncols=min(4, len(labels)), loc="upper left")
    return figure


def index_lines(index: pd.DataFrame, labels: Sequence[str]) -> Figure:
    """Monthly index lines (year average = 100), one per area in `index` columns."""
    table = index.reindex(range(1, 13))
    return area_lines(table, labels, [m[0] for m in MONTHS], "Index (year avg = 100)", 100)


def share_bars(share: pd.Series, highlight: int = 2020) -> Figure:
    """Area share of metro check-ins by year, with one highlighted year."""
    figure, axes = _figure(5.0, 3.2)
    years = [int(y) for y in share.index]
    colors = [NEGATIVE if y == highlight else PRIMARY for y in years]
    bars = axes.bar([str(y) for y in years], share.to_numpy(), color=colors, width=0.7)
    axes.bar_label(bars, labels=[f"{v:.1f}%" for v in share], fontsize=7, color=INK, padding=2)
    axes.set_ylabel("% of metro check-ins", fontsize=8, color=MUTED)
    axes.set_ylim(0, float(share.max()) * 1.2)
    return figure


def effect_bars(effects: pd.DataFrame) -> Figure:
    """% change on event days with 95% CI whiskers; needs label, effect_pct, ci_low, ci_high."""
    table = effects.iloc[::-1]
    figure, axes = _figure(5.6, 0.55 * len(table) + 1.0)
    y = np.arange(len(table))
    colors = [PRIMARY if e >= 0 else NEGATIVE for e in table.effect_pct]
    axes.barh(y, table.effect_pct, color=colors, height=0.5)
    errors = np.vstack([table.effect_pct - table.ci_low, table.ci_high - table.effect_pct])
    axes.errorbar(table.effect_pct, y, xerr=errors, fmt="none", ecolor=INK, elinewidth=1,
                  capsize=3)  # fmt: skip
    axes.axvline(0, color=MUTED, linewidth=0.8)
    axes.set_yticks(y, table.label)
    axes.tick_params(axis="y", labelsize=8, colors=INK)
    axes.grid(axis="y", visible=False)
    axes.grid(axis="x", color=GRID, linewidth=0.6)
    axes.set_xlabel("Change in check-ins vs normal days (%)", fontsize=8, color=MUTED)
    return figure


def anomaly_scatter(points: pd.DataFrame) -> Figure:
    """Weekly temperature anomaly vs check-in change, with a quadratic fitted trend."""
    figure, axes = _figure(5.6, 3.3)
    x, y = points.temp_anomaly_c.to_numpy(), points.checkin_change_pct.to_numpy()
    axes.scatter(x, y, s=14, color=PRIMARY, alpha=0.7, linewidths=0, label="Week")
    if len(x) >= 3:
        line = np.linspace(x.min(), x.max(), 100)
        axes.plot(line, np.polyval(np.polyfit(x, y, 2), line), color=NEGATIVE, linewidth=2,
                  label="Fitted trend")  # fmt: skip
    axes.axhline(0, color=MUTED, linewidth=0.8, linestyle=":")
    axes.axvline(0, color=MUTED, linewidth=0.8, linestyle=":")
    axes.set_xlabel("Temperature vs normal for the month (°C)", fontsize=8, color=MUTED)
    axes.set_ylabel("Check-in change vs normal (%)", fontsize=8, color=MUTED)
    axes.legend(frameon=False, fontsize=8, loc="lower center")
    return figure


def forecast_chart(history: pd.DataFrame, forecast: pd.DataFrame, scenario_label: str) -> Figure:
    """Weekly history, typical forecast (dashed), scenario line and its 80% band."""
    figure, axes = _figure(8.5, 3.6)
    h = np.arange(-len(history) + 1, 1)
    f = forecast.week_offset.to_numpy()
    axes.axvspan(0, f.max() + 0.5, color="#eef2f0", linewidth=0)
    axes.plot(h, history.checkins, color=INK, linewidth=1.8, label="Actual")
    joint = np.concatenate([[0], f])
    last = float(history.checkins.iloc[-1])
    axes.fill_between(joint, np.concatenate([[last], forecast.scenario_lo]),
                      np.concatenate([[last], forecast.scenario_hi]), color=BAND, linewidth=0,
                      label="80% range")  # fmt: skip
    axes.plot(joint, np.concatenate([[last], forecast.yhat]), color=PRIMARY, linewidth=1.8,
              linestyle="--", label="Typical season")  # fmt: skip
    if not np.allclose(forecast.scenario, forecast.yhat):
        axes.plot(joint, np.concatenate([[last], forecast.scenario]), color=NEGATIVE,
                  linewidth=2, label=scenario_label)  # fmt: skip
    axes.axvline(0, color=MUTED, linewidth=0.8, linestyle=":")
    ticks = [int(h[0]), int(h[len(h) // 2]), 0, *(int(w) for w in f if w % 4 == 0)]
    axes.set_xticks(ticks, [f"W{t}" if t < 0 else ("Now" if t == 0 else f"W+{t}") for t in ticks])
    axes.set_ylabel("Check-ins per week", fontsize=8, color=MUTED)
    axes.set_ylim(bottom=0)
    axes.legend(frameon=False, fontsize=8, ncols=4, loc="upper left")
    return figure


def driver_bars(drivers: pd.DataFrame) -> Figure:
    """Horizontal bars of forecast driver shares (%)."""
    table = drivers.sort_values("share_pct")
    figure, axes = _figure(4.6, 2.4)
    bars = axes.barh(table.driver, table.share_pct, color=PRIMARY, height=0.5)
    axes.bar_label(bars, labels=[f"{v:.0f}%" for v in table.share_pct], fontsize=8, padding=3)
    axes.grid(axis="y", visible=False)
    axes.set_xlim(0, float(table.share_pct.max()) * 1.25)
    axes.set_xticks([])
    axes.tick_params(axis="y", labelsize=9, colors=INK)
    return figure


def grouped_bars(scores: pd.DataFrame) -> Figure:
    """Grouped bars: rows are dimensions, columns are areas, values 0-100."""
    figure, axes = _figure(8.0, 3.3)
    count = scores.shape[1]
    width = 0.8 / count
    x = np.arange(len(scores))
    for slot, column in enumerate(scores.columns):
        bars = axes.bar(x + (slot - (count - 1) / 2) * width, scores[column], width * 0.92,
                        color=SERIES[slot % 4], label=str(column))  # fmt: skip
        axes.bar_label(bars, labels=[f"{v:.0f}" for v in scores[column]], fontsize=7, padding=2)
    axes.set_xticks(x, scores.index)
    axes.tick_params(axis="x", labelsize=9, colors=INK)
    axes.set_ylim(0, 115)
    axes.set_yticks([0, 25, 50, 75, 100])
    axes.legend(frameon=False, fontsize=8, ncols=count, loc="upper right")
    return figure
