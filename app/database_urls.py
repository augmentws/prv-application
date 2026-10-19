from urllib.parse import quote


def build_postgres_url(*, user: str, password: str, host: str, port: int, database: str) -> str:
    """Build a SQLAlchemy URL; the user and password are percent-encoded so special characters are safe."""

    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{database}"
    )
