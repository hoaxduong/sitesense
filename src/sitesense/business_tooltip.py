"""Compact hover cards shared by the individual business maps."""

from collections.abc import Mapping
from datetime import date
from typing import Any

TOOLTIP = {
    "html": (
        '<div style="font-size:15px;font-weight:600">{tooltip_name}</div>'
        '<div style="margin-top:8px;color:#59636c">{tooltip_label}</div>'
        '<div style="font-size:26px;font-weight:600;line-height:1.2">{tooltip_count}</div>'
        '<div style="margin-top:4px;color:#59636c">{tooltip_period}</div>'
        '<div style="display:{tooltip_projected_display};margin-top:8px">'
        '<div style="color:#71558a">Projected check-ins</div>'
        '<div style="font-size:20px;font-weight:600">{tooltip_projected_count}</div>'
        '<div style="color:#59636c">{tooltip_projected_period}</div></div>'
        '<div style="display:{tooltip_rating_display};margin-top:8px">'
        '<div style="color:#59636c">Rating (Yelp archive snapshot)</div>'
        '<div style="font-size:20px;font-weight:600">{tooltip_rating}</div></div>'
        '<div style="display:{tooltip_weather_display};margin-top:10px;padding-top:8px;'
        'border-top:1px solid #dce5df">'
        '<div style="font-weight:600">{tooltip_weather}</div>'
        '<div style="font-size:12px;color:#59636c">{tooltip_coverage}<br>'
        "{tooltip_station}<div>{tooltip_weather_dates}</div></div></div>"
        '<div style="margin-top:8px;font-size:12px;color:#59636c">{tooltip_hint}</div>'
    ),
    "style": {
        "transform": "none",
        "top": "12px",
        "left": "12px",
        "backgroundColor": "#ffffff",
        "color": "#192f29",
        "border": "1px solid #dce5df",
        "borderRadius": "8px",
        "boxShadow": "0 4px 16px rgba(25, 47, 41, 0.16)",
        "padding": "12px 14px",
        "fontFamily": "sans-serif",
        "fontSize": "13px",
        "lineHeight": "1.5",
        "width": "260px",
        "maxWidth": "calc(100% - 24px)",
        "overflowWrap": "anywhere",
        "boxSizing": "border-box",
    },
}


def period_label(start: date, end: date) -> str:
    return f"{start:%d %b %Y}" if start == end else f"{start:%d %b %Y} – {end:%d %b %Y}"


def fields(
    name: str,
    count: str,
    *,
    period: str | None = None,
    projected: bool = False,
    projected_count: str | None = None,
    projected_period: str | None = None,
    rating: str | None = None,
    weather: Mapping[str, Any] | None = None,
    weather_period: str | None = None,
) -> dict[str, str]:
    """Supply plain text; Streamlit escapes HTML-template substitutions in its frontend.

    Keep markup in TOOLTIP rather than a substituted field. Initialize every
    placeholder so missing weather never exposes template syntax.
    """
    observation = weather or {}
    return {
        "tooltip_name": name,
        "tooltip_label": "Projected check-ins" if projected else "Recorded check-ins",
        "tooltip_count": count,
        "tooltip_period": period or "",
        "tooltip_projected_display": "block" if projected_count is not None else "none",
        "tooltip_projected_count": projected_count or "",
        "tooltip_projected_period": projected_period or "",
        "tooltip_rating_display": "block" if rating is not None else "none",
        "tooltip_rating": rating or "",
        "tooltip_weather_display": "block" if weather else "none",
        "tooltip_weather": str(observation.get("weather_text", "Weather unavailable")),
        "tooltip_coverage": str(observation.get("weather_coverage", "No accepted observations")),
        "tooltip_station": str(observation.get("weather_station", "No mapped ACIS station"))
        + " · ACIS",
        "tooltip_weather_dates": f"Weather: {weather_period}"
        if weather_period and weather_period != period
        else "",
        "tooltip_hint": "" if projected else "Click to investigate",
    }
