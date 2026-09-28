"""Put /app on the path so `evals.*` resolves.

The eval package is mounted at /app/evals (docker-compose.yml) rather than living
under backend/, so it is importable exactly the way tests/backend/conftest.py makes
the backend modules importable — same insert, same reason.
"""
import sys

sys.path.insert(0, "/app")
