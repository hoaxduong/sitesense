"""Check Streamlit server startup and execute the real app without external services."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def wait_until_healthy(url: str, process: subprocess.Popen[str], output: TextIO) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200 and response.read() == b"ok":
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.2)
    output.seek(0)
    raise RuntimeError(f"Streamlit did not become healthy:\n{output.read()[-12_000:]}")


def main() -> None:
    port = available_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                "app.py",
                "--server.address=127.0.0.1",
                f"--server.port={port}",
                "--server.headless=true",
                "--browser.gatherUsageStats=false",
            ],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_until_healthy(f"{base_url}/_stcore/health", process, output)
            with urlopen(base_url, timeout=5) as response:
                assert response.status == 200
            with patch.dict(os.environ):
                os.environ.pop("DATABASE_URL", None)
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10).run()
                for page, title in (
                    ("location_map", "Location map"),
                    ("compare_sites", "Compare businesses"),
                    ("business_analysis", "Business analysis"),
                ):
                    app.switch_page(f"src/sitesense/app_pages/{page}.py").run()
                    assert not app.exception, [error.message for error in app.exception]
                    assert app.header[0].value == title
                    assert app.info[0].value == "Database not configured"
                    assert not app.metric
                app.switch_page("src/sitesense/app_pages/about_models.py").run()
                assert not app.exception, [error.message for error in app.exception]
                assert app.header[0].value == "About our models"
                assert not app.selectbox
                app.switch_page("src/sitesense/app_pages/about_database.py").run()
                assert not app.exception, [error.message for error in app.exception]
                assert app.header[0].value == "About our database"
                assert not app.selectbox
            assert process.poll() is None
            print("Smoke passed: server, three exploration pages, both guides, and safe setup.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
