import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)


def init_db(conn):
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with conn.transaction():
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
