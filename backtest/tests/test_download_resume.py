# Unit tests for mark-download resume skip/refetch rules.

from __future__ import annotations

import sqlite3

from backtest.download_option_marks import (
    EDGE_TOL_SEC,
    classify_resume_decision,
    detail_with_win_tag,
    parse_win_tag,
    progress_status,
)


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE download_progress (
            symbol TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            n_rows INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def test_tagged_window_covers_skips() -> None:
    w0, w1 = 1_000_000, 1_010_000
    detail = detail_with_win_tag("ok", w0, w1)
    assert parse_win_tag(detail) == (w0, w1)
    prev = ("done", 100, detail)
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=w0 + 100,
        w1=w1 - 100,
        mn=None,
        mx=None,
        is_short_dated=True,
    )
    assert decision == "skip_same_window"


def test_short_done_late_min_end_ok_skips() -> None:
    w0, w1 = 1_000_000, 1_010_000
    mn = w0 + EDGE_TOL_SEC + 86400  # listed days after tail w0
    mx = w1 - 60
    prev = ("done", 50, "legacy_no_win_tag")
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=w0,
        w1=w1,
        mn=mn,
        mx=mx,
        is_short_dated=True,
    )
    assert decision == "skip_short_done_end_ok"


def test_short_done_end_missing_refetches() -> None:
    w0, w1 = 1_000_000, 1_010_000
    mx = w1 - EDGE_TOL_SEC - 3600
    prev = ("done", 50, "ok")
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=w0,
        w1=w1,
        mn=w0,
        mx=mx,
        is_short_dated=True,
    )
    assert decision == "refetch_short_end_missing"


def test_long_edges_ok_skips() -> None:
    w0, w1 = 1_000_000, 1_010_000
    mn = w0
    mx = w1 - 60
    prev = ("done", 50, "ok")
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=w0,
        w1=w1,
        mn=mn,
        mx=mx,
        is_short_dated=False,
    )
    assert decision == "skip_long_edges_ok"


def test_long_start_missing_refetches() -> None:
    w0, w1 = 1_000_000, 1_010_000
    mn = w0 + EDGE_TOL_SEC + 3600
    mx = w1 - 60
    prev = ("done", 10, "legacy_no_win_tag")
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=w0,
        w1=w1,
        mn=mn,
        mx=mx,
        is_short_dated=False,
    )
    assert decision == "refetch_long_start_missing"


def test_status_error_retries() -> None:
    prev = ("error", 0, "status=429")
    decision = classify_resume_decision(
        force=False,
        prev=prev,
        w0=1,
        w1=2,
        mn=None,
        mx=None,
        is_short_dated=True,
    )
    assert decision == "retry_error"


def test_progress_status_roundtrip() -> None:
    conn = _mem_db()
    conn.execute(
        """
        INSERT INTO download_progress(symbol, status, n_rows, detail, updated_at)
        VALUES (?,?,?,?,?)
        """,
        ("X", "done", 1, "win=1-2|ok", "t"),
    )
    conn.commit()
    assert progress_status(conn, "X") == ("done", 1, "win=1-2|ok")
    conn.close()
