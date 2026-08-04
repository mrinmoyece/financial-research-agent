"""Application entrypoint — run with: uvicorn main:app --host 0.0.0.0 --port 8080"""
import logging

from src.api.server import app  # noqa: F401 — re-exported for uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
