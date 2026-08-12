from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


_INDEX_SCAN_MARGIN = timedelta(days=1)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TimeRange:
    key: str
    start: datetime | None
    end: datetime
    prev_start: datetime | None
    prev_end: datetime | None

    @property
    def start_iso(self) -> str | None:
        return _utc(self.start).isoformat() if self.start else None

    @property
    def end_iso(self) -> str:
        return _utc(self.end).isoformat()


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_range(
    range_key: str,
    *,
    custom_start: str | None = None,
    custom_end: str | None = None,
    now: datetime | None = None,
) -> TimeRange:
    end = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    key = (range_key or "30d").strip().lower()

    if key == "custom":
        if not custom_start or not custom_end:
            raise ValueError("custom range requires start and end")
        start = _parse_iso(custom_start)
        end = _parse_iso(custom_end)
        if end <= start:
            raise ValueError("custom end must be after start")
        delta = end - start
        return TimeRange(
            key="custom",
            start=start,
            end=end,
            prev_start=start - delta,
            prev_end=start,
        )

    if key == "all":
        return TimeRange(key="all", start=None, end=end, prev_start=None, prev_end=None)

    days_map = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
    if key not in days_map:
        raise ValueError(f"unsupported range: {range_key}")
    days = days_map[key]
    start = end - timedelta(days=days)
    prev_end = start
    prev_start = start - timedelta(days=days)
    return TimeRange(
        key=key,
        start=start,
        end=end,
        prev_start=prev_start,
        prev_end=prev_end,
    )


def range_params(tr: TimeRange) -> dict[str, Any]:
    return {
        "range": tr.key,
        "start": tr.start_iso,
        "end": tr.end_iso,
    }


def session_time_clause(
    tr: TimeRange,
    *,
    start_param: str = "start",
    end_param: str = "end",
    alias: str = "s",
) -> tuple[str, dict[str, Any]]:
    """Offset-safe session time filter with an index-backed candidate scan.

    With a start bound, NULL started_at is excluded (empty string fails >= start).
    With only an end bound (range=all), NULL started_at is included.
    The one-day lexical window covers every valid ISO offset; julianday applies
    the exact instant comparison after idx_sessions_started narrows candidates.
    """
    end = _utc(tr.end)
    end_scan_param = f"{end_param}_scan"
    params: dict[str, Any] = {
        end_param: end.isoformat(),
        end_scan_param: (end + _INDEX_SCAN_MARGIN).isoformat(),
    }
    col = f"{alias}.started_at"
    if tr.start is not None:
        start = _utc(tr.start)
        start_scan_param = f"{start_param}_scan"
        params[start_param] = start.isoformat()
        params[start_scan_param] = (
            start - _INDEX_SCAN_MARGIN
        ).isoformat()
        clause = (
            f"{col} >= :{start_scan_param} "
            f"AND {col} < :{end_scan_param} "
            f"AND julianday({col}) >= julianday(:{start_param}) "
            f"AND julianday({col}) < julianday(:{end_param})"
        )
    else:
        clause = (
            f"({col} IS NULL OR ("
            f"{col} < :{end_scan_param} "
            f"AND julianday({col}) < julianday(:{end_param})"
            f"))"
        )
    return clause, params
