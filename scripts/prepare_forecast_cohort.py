"""Prepare exact cohort membership from an explicit original training run.

This copies only the two reference CSVs and their original parent manifest.
It verifies their hash chain against the current model and never trains a model.
"""

import argparse
from pathlib import Path

from sitesense.forecast import MODEL_DIR
from sitesense.forecast_cohort import prepare_reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    try:
        prepare_reference(args.run_directory, args.model_directory)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Cohort reference verification failed: {type(error).__name__}.\n")
    print("Verified model cohort reference prepared; model weights unchanged.")


if __name__ == "__main__":
    main()
