import sqlite3
import tempfile
import unittest
from pathlib import Path

from bumparr import config, db


class DatabaseContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.db")
        self.addCleanup(setattr, config, "DB_PATH", self.original)
        db.init_db()

    def test_connection_commits_and_closes(self):
        with db.conn() as connection:
            connection.execute(
                "INSERT INTO playables (id,type,duration) VALUES ('x','video',1)")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with db.conn() as check:
            self.assertEqual(check.execute(
                "SELECT COUNT(*) FROM playables WHERE id='x'").fetchone()[0], 1)

    def test_exception_rolls_back_and_closes(self):
        connection = None
        with self.assertRaises(RuntimeError):
            with db.conn() as connection:
                connection.execute(
                    "INSERT INTO playables (id,type,duration) VALUES ('x','video',1)")
                raise RuntimeError("rollback")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with db.conn() as check:
            self.assertEqual(check.execute(
                "SELECT COUNT(*) FROM playables WHERE id='x'").fetchone()[0], 0)

    def test_init_enables_wal(self):
        with db.conn() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
