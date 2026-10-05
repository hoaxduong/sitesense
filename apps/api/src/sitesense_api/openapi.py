"""Export the API contract without starting a server."""

import json
from pathlib import Path

from sitesense_api.application import create_app
from sitesense_api.config import Settings


def main() -> None:
    app = create_app(Settings(environment="test", _env_file=None))
    Path("openapi.json").write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
