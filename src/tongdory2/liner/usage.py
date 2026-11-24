"""기록된 API 호출과 기존 응답 캐시 사용을 집계한다."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class RunUsage:
    external: int = 0
    cached: int = 0
    unreported: int = 0


def has_api_history(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_calls'").fetchone() is not None


def run_usage(conn: sqlite3.Connection, run_id: str) -> RunUsage:
    if not has_api_history(conn):
        return RunUsage()
    row = conn.execute("""
        SELECT COUNT(*) - COUNT(CASE WHEN cost_kind='CACHED' THEN 1 END),
               COUNT(CASE WHEN cost_kind='CACHED' THEN 1 END),
               COUNT(CASE WHEN cost_kind='NONE' THEN 1 END)
        FROM api_calls WHERE run_id=? AND endpoint != '/credits/balance'
    """, (run_id,)).fetchone()
    return RunUsage(*row)
