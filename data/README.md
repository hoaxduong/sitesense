# Dataset exploration

[The exploration notebook](../notebooks/01_yelp_dataset_exploration.ipynb) downloads the [provided Google Drive archive](https://drive.google.com/file/d/1cyir0oGMviwUjXPtMhvxpX27TQL2Lg6y/view?usp=sharing) to `data/raw/`. The archive is approximately 4.35 GB; allow approximately 5 GB of free disk space for it, working files, and small outputs.

From the repository root:

```sh
uv sync --frozen --group notebooks
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

Open `notebooks/01_yelp_dataset_exploration.ipynb` and run the cells in order. In VS Code, select `.venv/bin/python` as the kernel.

The ZIP contains a gzip-compressed `yelp_dataset.tar`. The notebook scans the nested TAR once, staging the complete business/check-in JSON and a bounded review prefix under `data/raw/yelp_exploration/`. It does not extract the full review or user tables. A cache manifest reuses staged files when the archive and review sample size match; ZIPs containing JSON directly are also supported.

The notebook summarizes business coverage and check-in activity, inspects the review prefix, and compares two seasonal baselines with a simple reference on a temporal holdout. The review prefix is not a representative random sample. Adjust the configuration cell to explore a different location or sample size.

To execute without opening Jupyter:

```sh
mkdir -p data/processed
uv run --frozen --group notebooks jupyter nbconvert \
  --to notebook --execute --ExecutePreprocessor.timeout=1800 \
  --output-dir=data/processed notebooks/01_yelp_dataset_exploration.ipynb
```

Downloaded archives and generated outputs stay local and are ignored by Git. Keep the source notebook free of executed outputs before committing. Executed notebook copies are optional review artifacts; remove them after inspecting them. You can also download an archive manually from the [Yelp Open Dataset page](https://www.yelp.com/dataset/download) and place it under `data/raw/`.

Retain the Yelp ZIP, staged business/check-in/review files, and staging manifest for offline reruns. The notebook checks the ZIP on every run and uses the manifest to reuse the staged files.

Check-ins are recorded activity, not unique customers or revenue. This archive does not supply a weather dataset; these experiments do not establish weather effects or support causal claims. Audit source coverage, missingness, and timestamp meaning before extending the analysis.

See [the climate dataset shortlist](../docs/research/climate-datasets.md) for historical weather sources, verified sample availability, and the acquisition plan for the Philadelphia pilot.

See [weather for every Yelp location](../docs/weather-data.md) for the hourly CSV schema, acquisition audits, and timezone alignment caveats. Local weather outputs live under `data/processed/yelp_weather/`.

Open [the weather notebook](../notebooks/02_yelp_weather_dataset_exploration.ipynb) to prepare the mapping and download ERA5 weather for all 150,346 business locations, then explore coverage, geographic and seasonal charts, and daily/monthly UTC summaries. Its **Download data** section reuses verified yearly files and fetches missing or corrupt partitions. All download and analysis code lives in the notebook and uses the same `notebooks` dependency group. The two weather exports are saved under `data/processed/yelp_weather_exploration/`.

The weather dataset keeps 14 annual files, the business mapping, cell table, scope/download manifests, and `ATTRIBUTION.txt`. Yelp analysis exports and the two daily/monthly weather summaries remain as useful notebook deliverables; rerunning the notebooks regenerates them.
