"""Optional longer explanations from a local Ollama server; no Streamlit imports.

Never called automatically: the page asks for it only when the user presses a button, and
the template explanation stays the default.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2"


class LocalModelUnavailable(RuntimeError):
    """Ollama is not running, not reachable, or returned no text."""


def generate(prompt: str, timeout: float = 60) -> str:
    """Return Ollama's completion for `prompt` (SITESENSE_OLLAMA_URL / _MODEL override)."""
    url = os.environ.get("SITESENSE_OLLAMA_URL", DEFAULT_URL).rstrip("/") + "/api/generate"
    model = os.environ.get("SITESENSE_OLLAMA_MODEL", DEFAULT_MODEL)
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = str(json.load(response).get("response", "")).strip()
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise LocalModelUnavailable(
            f"Ollama is not available at {url.rsplit('/api', 1)[0]} (model {model})."
        ) from error
    if not text:
        raise LocalModelUnavailable("Ollama returned an empty response.")
    return text


def ranking_prompt(sentence: str, contributions: str) -> str:
    return (
        "You explain a retail site ranking to a small-business owner in 3-4 plain sentences. "
        "Use only the facts below. Check-ins are a proxy for customer activity, not sales; "
        "do not claim that weather causes changes.\n"
        f"Summary: {sentence}\nFactor contributions in points vs the metro average:\n"
        f"{contributions}"
    )
