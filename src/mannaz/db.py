"""Minimalna obsługa połączenia z Postgresem — sekrety WYŁĄCZNIE ze środowiska
(nigdy nie logujemy/drukujemy wartości z .env)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def load_dotenv_into_environ(env_path: Path = DEFAULT_ENV_PATH) -> None:
    """Wczytuje KEY=VALUE z .env do os.environ, bez nadpisywania już ustawionych
    zmiennych i bez jakiegokolwiek logowania wartości."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def get_connection() -> psycopg.Connection:
    load_dotenv_into_environ()
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD nie jest ustawione w środowisku ani w .env — "
            "sprawdź M\\.env (nie loguj tu wartości)."
        )
    return psycopg.connect(
        host=os.environ.get("MANNAZ_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("MANNAZ_PG_PORT", "5433")),
        dbname=os.environ.get("MANNAZ_PG_DB", "mannaz"),
        user=os.environ.get("MANNAZ_PG_USER", "mannaz"),
        password=password,
    )
