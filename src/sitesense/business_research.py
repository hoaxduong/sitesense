"""Bounded, reproducible individual-business experiments for research notebooks.

Weather models are retrospective conditional predictions. These local trusted artifacts
are deliberately separate from the app's full-cohort HGB serving bundle.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from sitesense import business_training, repository
from sitesense.business_forecast import TARGET, BusinessGroup
from sitesense.business_training import TrainingData
from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES, Catalog

ROOT = Path(__file__).resolve().parents[2]
MODEL_NAMES = ("overall_mean", "month_weekday_shrunk", "poisson", "hgb", "gam_poisson", "catboost")
CALENDAR_MODELS = MODEL_NAMES[:2]
WEATHER_COLUMNS = ["tmin_c", "tmax_c", "precipitation_mm"]
CODE_FILES = (
    "src/sitesense/business_research.py",
    "src/sitesense/business_training.py",
    "src/sitesense/business_forecast.py",
    "src/sitesense/repository.py",
    "src/sitesense/models.py",
)
PROTOCOL = {
    "validation": ["2017-01-01", "2019-12-31", "2020-01-01", "2020-12-31"],
    "test": ["2018-01-01", "2020-12-31", "2021-01-01", "2021-12-31"],
    "final": ["2019-01-01", "2021-12-31"],
    "cohort": "first_activity <= fit_start; training_checkins >= 20; rebuild_each_fit",
    "sampling": "sha256(seed/business_id), train-eligible IDs only; bounded within source cohort",
    "target": TARGET,
    "weighting": "equal business/calendar-day rows, integer counts; no cohort normalization",
    "weather": "same complete accepted ACIS station/day rows for every candidate; no imputation",
    "selection": "minimum 2020 Poisson deviance; model order breaks ties; frozen before 2021",
    "features": "business identity, month, weekday, accepted tmin/tmax C and precipitation mm",
    "excluded": "ZIP, snapshot rating/reviews/status, activity lags, future activity",
}


@dataclass(frozen=True)
class ExperimentConfig:
    city: str = "Philadelphia"
    state: str = "PA"
    category: str = "Restaurants"
    source_category: str | None = "Pizza"
    max_businesses: int = 64
    seed: int = 42

    def __post_init__(self) -> None:
        if (self.city, self.state) not in {(s.city, s.state) for s in CITY_SCOPES}:
            raise ValueError("Choose an exact imported canonical city/state.")
        if self.category not in BUSINESS_CATEGORY_LABELS:
            raise ValueError("Choose Restaurants or Day Spas as the parent category.")
        if not 1 <= self.max_businesses <= 128:
            raise ValueError("max_businesses must be between 1 and 128 (below HGB max_bins).")
        if self.source_category is not None and not self.source_category.strip():
            raise ValueError("A source category must be an exact nonempty source tag or None.")


@dataclass(frozen=True)
class ResearchSources:
    training: TrainingData
    catalog: Catalog
    weather: Any
    import_revision: str
    weather_source_sha256: str


@dataclass(frozen=True)
class RunResult:
    directory: Path
    metrics: Any
    predictions: Any
    business_metrics: Any
    ranking: Any
    manifest: dict[str, Any]


@dataclass
class FittedResearchModel:
    model_name: str
    estimator: Any
    groups: tuple[BusinessGroup, ...]
    business_ids: tuple[str, ...]
    weather_features: bool
    parameters: dict[str, Any]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_sha() -> str:
    return _digest({name: _file_sha(ROOT / name) for name in CODE_FILES})


def _frame_signature(frame: Any, columns: list[str]) -> str:
    selected = frame[columns].copy()
    selected = selected.sort_values(columns[:2]).reset_index(drop=True)
    return _digest(selected.to_json(orient="split", date_format="iso", double_precision=15))


def load_sources(config: ExperimentConfig) -> ResearchSources:
    """Read source observations with the same ACIS quality/units policy as the app."""
    revision = repository.get_import_revision()
    training = business_training.load_training_data()
    catalog = repository.load_catalog(config.city, config.state, config.category)
    stations = tuple(c for c in catalog.weather_cells if c.source_kind == "acis_station")
    reports = repository._load_station_reports(stations, date(2017, 1, 1), date(2021, 12, 31))
    weather = accepted_weather(reports)
    if repository.get_import_revision() != revision:
        raise ValueError("The import changed while reading research sources; retry.")
    return ResearchSources(
        training, catalog, weather, revision, _digest([asdict(r) for r in reports])
    )


def accepted_weather(reports: Any) -> Any:
    """Keep complete, coherent accepted station days; precipitation trace is 0 mm."""
    values: dict[tuple[str, date], dict[str, float]] = {}
    for report in reports:
        value = repository._accepted_station_value(report)
        if value is not None and report.variable in ("tmin", "tmax", "pcpn"):
            key = report.weather_cell_id, report.observation_date
            if report.variable in values.get(key, {}):
                raise ValueError("Duplicate station/date/variable weather observations.")
            values.setdefault(key, {})[report.variable] = value
    rows = [
        {
            "weather_cell_id": station,
            "day": day,
            "tmin_c": measured["tmin"],
            "tmax_c": measured["tmax"],
            "precipitation_mm": measured["pcpn"],
        }
        for (station, day), measured in sorted(values.items())
        if set(measured) == {"tmin", "tmax", "pcpn"} and measured["tmin"] <= measured["tmax"]
    ]
    return pd.DataFrame(rows, columns=["weather_cell_id", "day", *WEATHER_COLUMNS])


def _signatures(sources: ResearchSources) -> dict[str, str]:
    return {
        "source_signature": _digest(
            {
                "training": business_training._source_sha256(sources.training),
                "weather_reports": sources.weather_source_sha256,
                "accepted_weather": _frame_signature(
                    sources.weather, ["weather_cell_id", "day", *WEATHER_COLUMNS]
                ),
                "import_revision": sources.import_revision,
            }
        ),
        "catalog_signature": _digest(asdict(sources.catalog)),
    }


def select_groups(
    sources: ResearchSources, config: ExperimentConfig, start: date, end: date
) -> tuple[tuple[BusinessGroup, ...], int]:
    """Fit full city/parent calendar priors before selecting the deterministic sample."""
    fitted = business_training.fit(sources.training, start, end)
    eligible = [
        g
        for g in fitted.groups
        if (g.city, g.state, g.category) == (config.city, config.state, config.category)
        and (config.source_category is None or config.source_category in g.source_categories)
    ]
    selected = sorted(
        eligible,
        key=lambda g: hashlib.sha256(f"{config.seed}/{g.business_id}".encode()).hexdigest(),
    )[: config.max_businesses]
    if not selected:
        raise ValueError(
            "No businesses have training-only support in the selected exact source cohort."
        )
    return tuple(sorted(selected, key=lambda g: g.business_id)), len(eligible)


def build_panel(
    sources: ResearchSources, groups: tuple[BusinessGroup, ...], start: date, end: date
) -> Any:
    """One row per supported business/day; only declared imported coverage becomes zero."""
    data = sources.training
    if start < data.coverage_start or end > data.coverage_end or end < start:
        raise ValueError("Cannot zero-fill outside declared imported activity coverage.")
    identifiers = [g.business_id for g in groups]
    identifier_set = set(identifiers)
    panel = pd.MultiIndex.from_product(
        [identifiers, pd.date_range(start, end).date], names=["business_id", "day"]
    ).to_frame(index=False)
    observed = pd.DataFrame(
        [
            (r.business_id, r.day, r.checkins)
            for r in data.activity
            if r.business_id in identifier_set and start <= r.day <= end
        ],
        columns=["business_id", "day", "recorded_checkins"],
    )
    if observed.duplicated(["business_id", "day"]).any():
        raise ValueError("Duplicate business/day activity records.")
    for value in observed.recorded_checkins:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Recorded activity must be nonnegative integer check-ins.")
    panel = panel.merge(observed, on=["business_id", "day"], how="left", validate="one_to_one")
    panel["recorded_checkins"] = panel.recorded_checkins.fillna(0).astype("int64")
    stations = {b.business_id: b.weather_cell_id for b in sources.catalog.businesses}
    panel["weather_cell_id"] = panel.business_id.map(stations)
    panel = panel.merge(
        sources.weather, on=["weather_cell_id", "day"], how="left", validate="many_to_one"
    )
    panel["weather_available"] = panel[WEATHER_COLUMNS].notna().all(axis=1)
    panel["month"] = pd.to_datetime(panel.day).dt.month
    panel["weekday"] = pd.to_datetime(panel.day).dt.weekday
    return panel


def _model_input(frame: Any, name: str, identifiers: tuple[str, ...]) -> Any:
    if not set(frame.business_id).issubset(identifiers):
        raise ValueError("Inference supports existing fitted business IDs only.")
    columns = ["business_id", "month", "weekday", *WEATHER_COLUMNS]
    result = frame[columns].copy()
    if name == "hgb":
        result["business_id"] = result.business_id.map(
            {identifier: i for i, identifier in enumerate(identifiers)}
        )
    elif name == "catboost":
        for column in columns[:3]:
            result[column] = result[column].astype(str)
    return result


def fit_model(name: str, groups: tuple[BusinessGroup, ...], training: Any) -> FittedResearchModel:
    """Fit identity and transformations on training rows only, with fixed hyperparameters."""
    if name not in MODEL_NAMES:
        raise ValueError("Unknown research model.")
    identifiers = tuple(g.business_id for g in groups)
    if name in CALENDAR_MODELS:
        return FittedResearchModel(
            name, None, groups, identifiers, False, {"prior_business_days": 30}
        )
    frame = training.loc[training.weather_available].copy()
    if set(frame.business_id) != set(identifiers) or frame.recorded_checkins.sum() <= 0:
        raise ValueError(
            "Every sampled business needs accepted training weather; "
            "at least one check-in must be matched."
        )
    from sklearn.compose import ColumnTransformer  # type: ignore[import-untyped]
    from sklearn.ensemble import HistGradientBoostingRegressor  # type: ignore[import-untyped]
    from sklearn.linear_model import PoissonRegressor  # type: ignore[import-untyped]
    from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
    from sklearn.preprocessing import (  # type: ignore[import-untyped]
        OneHotEncoder,
        SplineTransformer,
        StandardScaler,
    )

    if name in ("poisson", "gam_poisson"):
        weather_transform = (
            StandardScaler()
            if name == "poisson"
            else SplineTransformer(n_knots=5, degree=3, include_bias=False, extrapolation="linear")
        )
        encoder = ColumnTransformer(
            [
                (
                    "identity_calendar",
                    OneHotEncoder(
                        categories=[list(identifiers), list(range(1, 13)), list(range(7))],
                        handle_unknown="error",
                    ),
                    ["business_id", "month", "weekday"],
                ),
                ("weather", weather_transform, WEATHER_COLUMNS),
            ]
        )
        estimator: Any = Pipeline(
            [
                ("features", encoder),
                ("regressor", PoissonRegressor(alpha=0.01, max_iter=300, tol=1e-6)),
            ]
        )
        parameters = {
            "alpha": 0.01,
            "max_iter": 300,
            "identity": "one_hot_business_id",
            "weather": "linear_standardized"
            if name == "poisson"
            else "additive_cubic_splines_5_knots",
        }
    elif name == "hgb":
        estimator = HistGradientBoostingRegressor(
            loss="poisson",
            max_iter=100,
            learning_rate=0.05,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            early_stopping=False,
            categorical_features=[0, 1, 2],
            max_bins=255,
            random_state=42,
        )
        parameters = estimator.get_params(deep=False)
    else:
        estimator = import_module("catboost").CatBoostRegressor(
            loss_function="Poisson",
            iterations=150,
            depth=6,
            learning_rate=0.05,
            random_seed=42,
            thread_count=2,
            verbose=False,
            allow_writing_files=False,
            cat_features=[0, 1, 2],
        )
        parameters = estimator.get_params()
    with threadpool_limits(limits=2):
        estimator.fit(_model_input(frame, name, identifiers), frame.recorded_checkins)
    return FittedResearchModel(name, estimator, groups, identifiers, True, parameters)


def predict(model: FittedResearchModel, frame: Any) -> Any:
    """Return expected individual daily counts, preserving business/day order and units."""
    if not set(frame.business_id).issubset(model.business_ids):
        raise ValueError("Inference supports existing fitted business IDs only.")
    if model.weather_features:
        if not frame.weather_available.all():
            raise ValueError(
                "Weather inference needs complete accepted observations or an explicit scenario."
            )
        with threadpool_limits(limits=2):
            values = np.asarray(
                model.estimator.predict(_model_input(frame, model.model_name, model.business_ids)),
                dtype=float,
            )
    else:
        groups = {g.business_id: g for g in model.groups}
        values = np.array(
            [
                groups[row.business_id].overall_rate
                if model.model_name == "overall_mean"
                else groups[row.business_id].rates[(row.day.month - 1) * 7 + row.day.weekday()]
                for row in frame.itertuples()
            ]
        )
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("The model produced invalid expected check-in counts.")
    return np.maximum(values, 1e-12)


def _scores(frame: Any) -> dict[str, float | int]:
    actual = frame.recorded_checkins.to_numpy(dtype=float)
    predicted = np.maximum(frame.predicted_checkins.to_numpy(dtype=float), 1e-12)
    terms = predicted - actual
    positive = actual > 0
    terms[positive] += actual[positive] * np.log(actual[positive] / predicted[positive])
    return {
        "businesses": int(frame.business_id.nunique()),
        "observations": len(frame),
        "poisson_deviance": float(max(0.0, 2 * terms.mean())),
        "mae": float(np.abs(actual - predicted).mean()),
        "recorded_checkins": int(actual.sum()),
        "predicted_checkins": float(predicted.sum()),
    }


def _assess(
    stage: str, models: dict[str, FittedResearchModel], assessment: Any
) -> tuple[Any, Any, Any, Any]:
    predictions, metrics, business_metrics, rankings = [], [], [], []
    matched = assessment.loc[assessment.weather_available]
    if matched.empty:
        raise ValueError("No accepted held-out weather rows; cannot compare weather models.")
    for name, model in models.items():
        populations = [("accepted_weather", matched)]
        if not model.weather_features:
            populations.insert(0, ("all_covered", assessment))
        for population, panel in populations:
            frame = panel[["business_id", "day", "recorded_checkins", "weather_available"]].copy()
            frame["predicted_checkins"] = predict(model, panel)
            frame = frame.assign(stage=stage, model=name, population=population)
            predictions.append(frame)
            keys = {"stage": stage, "model": name, "population": population}
            metrics.append({**keys, **_scores(frame)})
            totals = []
            for identifier, business in frame.groupby("business_id", sort=True):
                scores = _scores(business)
                business_metrics.append({**keys, "business_id": identifier, **scores})
                totals.append(
                    {
                        **keys,
                        "business_id": identifier,
                        "recorded_checkins": scores["recorded_checkins"],
                        "predicted_checkins": scores["predicted_checkins"],
                    }
                )
            ranking = pd.DataFrame(totals)
            # Break rank ties by business ID within the sampled exact source cohort.
            ranking = ranking.sort_values(
                ["recorded_checkins", "business_id"], ascending=[False, True]
            )
            ranking["observed_rank"] = np.arange(1, len(ranking) + 1)
            ranking = ranking.sort_values(
                ["predicted_checkins", "business_id"], ascending=[False, True]
            )
            ranking["predicted_rank"] = np.arange(1, len(ranking) + 1)
            rankings.append(ranking)
    return (
        pd.DataFrame(metrics),
        pd.concat(predictions, ignore_index=True),
        pd.DataFrame(business_metrics),
        pd.concat(rankings, ignore_index=True),
    )


def _panel_signature(frame: Any) -> str:
    return _frame_signature(
        frame, ["business_id", "day", "recorded_checkins", "weather_cell_id", *WEATHER_COLUMNS]
    )


def _compatible(parent: dict[str, Any], child: dict[str, Any]) -> None:
    for key in (
        "schema_version",
        "grain",
        "config",
        "protocol",
        "source_signature",
        "catalog_signature",
        "panel_signatures",
    ):
        if parent[key] != child[key]:
            raise ValueError(f"Incompatible parent experiment: {key} changed; rerun notebook 03.")


def run_experiment(
    config: ExperimentConfig,
    model_names: tuple[str, ...] | list[str],
    output_root: Path | str,
    parent_run: Path | str | None = None,
) -> RunResult:
    """Select on 2020, independently assess 2021, then refit a 2019–2021 final cohort."""
    names = tuple(model_names)
    if not names or len(set(names)) != len(names) or any(n not in MODEL_NAMES for n in names):
        raise ValueError("Supply distinct supported research model names.")
    sources = load_sources(config)
    signatures = _signatures(sources)
    panels, group_sets, stages, panel_signatures = {}, {}, {}, {}
    for stage in ("validation", "test", "final"):
        dates = [date.fromisoformat(d) for d in PROTOCOL[stage]]
        groups, eligible = select_groups(sources, config, dates[0], dates[1])
        train = build_panel(sources, groups, dates[0], dates[1])
        assess = build_panel(sources, groups, dates[2], dates[3]) if len(dates) == 4 else None
        panels[stage] = train, assess
        group_sets[stage] = groups
        stages[stage] = {
            "fit_start": str(dates[0]),
            "fit_end": str(dates[1]),
            "eligible_businesses": eligible,
            "sampled_businesses": len(groups),
            "business_ids": [g.business_id for g in groups],
            "training_days": len(train),
            "accepted_training_weather_days": int(train.weather_available.sum()),
            "assessment_days": len(assess) if assess is not None else 0,
            "accepted_assessment_weather_days": int(assess.weather_available.sum())
            if assess is not None
            else 0,
        }
        panel_signatures[stage] = _digest(
            {
                "train": _panel_signature(train),
                "assessment": _panel_signature(assess) if assess is not None else None,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "grain": "business_id_calendar_day",
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
        "config": asdict(config),
        "protocol": PROTOCOL,
        **signatures,
        "stages": stages,
        "panel_signatures": panel_signatures,
        "code_sha256": _code_sha(),
        "lock_sha256": _file_sha(ROOT / "uv.lock"),
        "import_revision": sources.import_revision,
        "model_names": list(names),
        "selection_population": "accepted_weather",
        "coverage_start": str(sources.training.coverage_start),
        "coverage_end": str(sources.training.coverage_end),
        "source_categories_overlap": True,
        "limitations": (
            "Bounded existing-business source-cohort sample; no marketwide/new-site/revenue/"
            "causal claims. Observed weather is retrospective; future weather is a scenario."
        ),
    }
    if parent_run is not None:
        parent = load_run(parent_run)
        _compatible(parent.manifest, manifest)
        manifest["parent_run"] = {
            "directory": str(parent.directory.resolve()),
            "manifest_sha256": _file_sha(parent.directory / "manifest.json"),
        }
    validation_models = {
        n: fit_model(n, group_sets["validation"], panels["validation"][0]) for n in names
    }
    validation = _assess("validation", validation_models, panels["validation"][1])
    candidates = validation[0].query("population == 'accepted_weather'")
    selected = min(
        names,
        key=lambda n: (
            float(candidates.loc[candidates.model == n, "poisson_deviance"].iloc[0]),
            names.index(n),
        ),
    )
    # Freeze the choice before any test metric or fit is consulted.
    manifest["selected_model"] = selected
    manifest["selection_frozen_before_test"] = True
    test_models = {n: fit_model(n, group_sets["test"], panels["test"][0]) for n in names}
    assessment = _assess("test", test_models, panels["test"][1])
    final_models = {n: fit_model(n, group_sets["final"], panels["final"][0]) for n in names}
    manifest["model_parameters"] = {n: final_models[n].parameters for n in names}
    # Recheck source revision and full fingerprints before making the run complete.
    if _signatures(load_sources(config)) != signatures:
        raise ValueError("Research sources changed during fitting; no complete run was written.")
    if manifest["code_sha256"] != _code_sha() or manifest["lock_sha256"] != _file_sha(
        ROOT / "uv.lock"
    ):
        raise ValueError("Code or lockfile changed during fitting; retry.")
    directory = Path(output_root).resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    tables = {
        name: pd.concat([validation[i], assessment[i]], ignore_index=True)
        for i, name in enumerate(("metrics", "predictions", "business_metrics", "ranking"))
    }
    payloads = []
    for name, table in tables.items():
        path = directory / f"{name}.csv"
        table.to_csv(path, index=False)
        payloads.append(path)
    snapshot = directory / "catalog.json"
    snapshot.write_text(
        json.dumps(
            [asdict(b) for b in sources.catalog.businesses],
            sort_keys=True,
            default=str,
            allow_nan=False,
        )
        + "\n"
    )
    payloads.append(snapshot)
    models_path = directory / "final_models.joblib"
    joblib.dump(final_models, models_path)
    payloads.append(models_path)
    manifest["payload_sha256"] = {p.name: _file_sha(p) for p in payloads}
    manifest["status"] = "complete"
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return RunResult(
        directory,
        tables["metrics"],
        tables["predictions"],
        tables["business_metrics"],
        tables["ranking"],
        manifest,
    )


def latest_run(root: Path | str) -> Path:
    """Find the latest complete new-schema run, without silently reusing legacy outputs."""
    candidates = []
    for path in Path(root).glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_text())
            if (
                manifest.get("schema_version") == 1
                and manifest.get("grain") == "business_id_calendar_day"
                and manifest.get("status") == "complete"
            ):
                candidates.append((manifest["created_at"], path.parent))
        except (OSError, ValueError, KeyError):
            continue
    if not candidates:
        raise ValueError(
            f"No complete business-level run under {root}; execute the upstream notebook first."
        )
    return max(candidates, key=lambda row: (row[0], str(row[1])))[1]


def load_run(path: Path | str, verify_source: bool = True) -> RunResult:
    """Verify code/lock/source/lineage and every payload before reading local run tables."""
    directory = Path(path).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest.get("schema_version") != 1
        or manifest.get("grain") != "business_id_calendar_day"
        or manifest.get("status") != "complete"
        or manifest.get("protocol") != PROTOCOL
    ):
        raise ValueError("A complete compatible business-level research manifest is required.")
    if manifest.get("code_sha256") != _code_sha() or manifest.get("lock_sha256") != _file_sha(
        ROOT / "uv.lock"
    ):
        raise ValueError("Research code or lockfile changed; rerun the source notebook.")
    required = {
        "metrics.csv",
        "predictions.csv",
        "business_metrics.csv",
        "ranking.csv",
        "catalog.json",
        "final_models.joblib",
    }
    if set(manifest.get("payload_sha256", {})) != required:
        raise ValueError("The complete run must declare every expected payload.")
    for name, digest in manifest["payload_sha256"].items():
        if _file_sha(directory / name) != digest:
            raise ValueError(f"Research payload changed: {name}.")
    config = ExperimentConfig(**manifest["config"])
    if verify_source:
        signatures = _signatures(load_sources(config))
        if any(manifest[key] != value for key, value in signatures.items()):
            raise ValueError("Imported source/catalog changed; rerun the source notebook.")
    lineage = manifest.get("parent_run")
    if lineage:
        parent_path = Path(lineage["directory"])
        if (
            parent_path.resolve() == directory
            or _file_sha(parent_path / "manifest.json") != lineage["manifest_sha256"]
        ):
            raise ValueError("The parent run changed or has invalid lineage.")
        parent = load_run(parent_path, verify_source=verify_source)
        _compatible(parent.manifest, manifest)
    tables = {
        name: pd.read_csv(directory / f"{name}.csv", dtype={"business_id": str})
        for name in ("metrics", "predictions", "business_metrics", "ranking")
    }
    return RunResult(
        directory,
        tables["metrics"],
        tables["predictions"],
        tables["business_metrics"],
        tables["ranking"],
        manifest,
    )


def load_models(run_directory: Path | str) -> dict[str, FittedResearchModel]:
    """Load only trusted locally generated, verified research estimator artifacts."""
    run = load_run(run_directory)
    models: dict[str, FittedResearchModel] = joblib.load(run.directory / "final_models.joblib")
    if set(models) != set(run.manifest["model_names"]):
        raise ValueError("Saved estimator names do not match the manifest.")
    return models


def recommend(
    run_directory: Path | str,
    day: date | str,
    model: str | None = None,
    weather: Any = None,
    top_n: int = 10,
) -> Any:
    """Rank existing final-fit businesses; future weather requires an explicit scenario.

    This is a conditional expected-count ranking of a bounded archived sample, not a
    recommendation that a new venue will succeed. Snapshot coordinates label listings.
    """
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if top_n < 1:
        raise ValueError("top_n must be positive.")
    run = load_run(run_directory)
    if day <= date.fromisoformat(run.manifest["protocol"]["final"][1]):
        raise ValueError(
            "Final-fit inference must be after the final training cutoff; "
            "use held-out predictions for historical comparisons."
        )
    models: dict[str, FittedResearchModel] = joblib.load(run.directory / "final_models.joblib")
    name = model or run.manifest["selected_model"]
    if name not in models:
        raise ValueError("This model is not present in the verified research run.")
    fitted = models[name]
    frame = pd.DataFrame({"business_id": fitted.business_ids, "day": day})
    frame["month"], frame["weekday"] = day.month, day.weekday()
    basis = "calendar_only"
    if fitted.weather_features:
        if weather is None:
            config = ExperimentConfig(**run.manifest["config"])
            catalog = repository.load_catalog(config.city, config.state, config.category)
            reports = repository._load_station_reports(
                tuple(c for c in catalog.weather_cells if c.source_kind == "acis_station"), day, day
            )
            observations = accepted_weather(reports)
            frame["weather_cell_id"] = frame.business_id.map(
                {b.business_id: b.weather_cell_id for b in catalog.businesses}
            )
            frame = frame.merge(
                observations, on=["weather_cell_id", "day"], how="left", validate="many_to_one"
            )
            basis = "accepted_observed_weather_retrospective"
        elif isinstance(weather, dict):
            if set(weather) != set(WEATHER_COLUMNS):
                raise ValueError(
                    f"Weather scenarios require exactly {WEATHER_COLUMNS} in C and mm."
                )
            for column in WEATHER_COLUMNS:
                frame[column] = weather[column]
            basis = "explicit_weather_scenario"
        else:
            if set(weather.columns) != {"business_id", *WEATHER_COLUMNS} or set(
                weather.business_id
            ) != set(fitted.business_ids):
                raise ValueError(
                    "A weather scenario table needs each fitted business exactly once "
                    "and the weather feature columns."
                )
            frame = frame.merge(weather, on="business_id", validate="one_to_one")
            basis = "explicit_weather_scenario"
        if (
            frame[WEATHER_COLUMNS].isna().any().any()
            or not np.isfinite(frame[WEATHER_COLUMNS].to_numpy(dtype=float)).all()
            or (frame.tmin_c > frame.tmax_c).any()
            or (frame.precipitation_mm < 0).any()
        ):
            raise ValueError(
                "Complete coherent weather is required; provide an explicit "
                "tmin/tmax C, precipitation mm scenario."
            )
        frame["weather_available"] = True
    frame["predicted_checkins"] = predict(fitted, frame)
    profiles = pd.DataFrame(json.loads((run.directory / "catalog.json").read_text()))
    profiles = profiles.rename(columns={"categories": "source_categories"})
    frame = frame.merge(
        profiles[["business_id", "name", "source_categories", "latitude", "longitude"]],
        on="business_id",
        validate="one_to_one",
    )
    frame["source_categories"] = frame.source_categories.map(
        lambda values: [v for v in values if v != run.manifest["config"]["category"]]
    )
    frame["model"], frame["weather_basis"] = name, basis
    return (
        frame[
            [
                "business_id",
                "name",
                "day",
                "source_categories",
                "latitude",
                "longitude",
                "predicted_checkins",
                "model",
                "weather_basis",
            ]
        ]
        .sort_values(["predicted_checkins", "business_id"], ascending=[False, True])
        .head(top_n)
        .reset_index(drop=True)
    )
