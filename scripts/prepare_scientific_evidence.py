"""Prepare verified HGB forward-time evaluation evidence, without retraining."""

import argparse
from pathlib import Path

from sitesense.forecast import MODEL_DIR
from sitesense.scientific_evidence import prepare_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    try:
        prepare_evaluation(args.run_directory, args.model_directory)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Evaluation verification failed: {type(error).__name__}.\n")
    print("Verified saved backtest evidence prepared; no model or database changes.")


if __name__ == "__main__":
    main()
