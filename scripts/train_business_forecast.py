"""Fit an individual-business calendar projection artifact outside Streamlit."""

import argparse
import json
import sys
from pathlib import Path

import psycopg

from sitesense.business_forecast import MODEL_DIR
from sitesense.business_training import train_from_database
from sitesense.database import ConfigurationError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    try:
        manifest = train_from_database(args.output)
    except (ConfigurationError, psycopg.Error, OSError, ValueError):
        print(
            "Business training failed. Check database access and complete imported "
            "2017–2021 coverage.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "model_name",
                    "training_start",
                    "training_end",
                    "final_businesses",
                    "validation_metrics",
                    "assessment_metrics",
                    "coverage_end",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
