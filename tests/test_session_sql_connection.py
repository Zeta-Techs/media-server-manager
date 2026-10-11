from __future__ import annotations

from pathlib import Path

from media_server_manager.infrastructure.db.legacy_sql import connect_session


def test_session_sql_connection_preserves_dbapi_write_and_fetch(tmp_path: Path) -> None:
    database = tmp_path / "session.sqlite3"

    with connect_session(database) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)")
        cursor = connection.execute("INSERT INTO records(value) VALUES (?)", ("first",))
        assert cursor.lastrowid == 1
        connection.executemany("INSERT INTO records(value) VALUES (?)", [("second",), ("third",)])

    with connect_session(database) as connection:
        rows = connection.execute("SELECT id, value FROM records ORDER BY id").fetchall()
        assert [dict(row) for row in rows] == [
            {"id": 1, "value": "first"},
            {"id": 2, "value": "second"},
            {"id": 3, "value": "third"},
        ]


def test_session_sql_connection_rolls_back_on_error(tmp_path: Path) -> None:
    database = tmp_path / "rollback.sqlite3"

    with connect_session(database) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)")

    try:
        with connect_session(database) as connection:
            connection.execute("INSERT INTO records(value) VALUES (?)", ("discarded",))
            raise RuntimeError("abort transaction")
    except RuntimeError:
        pass

    with connect_session(database) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM records").fetchone()["count"] == 0
