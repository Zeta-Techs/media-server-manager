from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

DEFAULT_TIMEZONE = os.environ.get("MSM_TIMEZONE", "Asia/Shanghai")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_schedule(schedule_type: str, value: str) -> None:
    value = (value or "").strip()
    if schedule_type == "interval":
        try:
            minutes = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("间隔分钟数必须是整数") from exc
        if minutes < 1:
            raise ValueError("间隔分钟数必须大于 0")
        return
    if schedule_type != "cron":
        raise ValueError("不支持的定时类型")
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ValueError("Cron 表达式需要 5 段，例如 30 22 * * *")


def next_run_at(
    schedule_type: str,
    value: str,
    base: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    validate_schedule(schedule_type, value)
    tz = ZoneInfo(timezone_name)
    current = (base or datetime.now(timezone.utc)).astimezone(tz)
    if schedule_type == "interval":
        result = current + timedelta(minutes=int(value))
    else:
        result = croniter(value, current).get_next(datetime)
        if result.tzinfo is None:
            result = result.replace(tzinfo=tz)
    return format_utc(result)


def describe_schedule(schedule_type: str, value: str) -> str:
    value = (value or "").strip()
    if schedule_type == "interval":
        try:
            minutes = int(value)
        except ValueError:
            return "间隔时间无效"
        if minutes % 60 == 0:
            return f"每 {minutes // 60} 小时执行一次"
        return f"每 {minutes} 分钟执行一次"
    if schedule_type != "cron":
        return value or "未设置"
    parts = value.split()
    if len(parts) != 5:
        return value or "Cron 未设置"
    minute, hour, day, month, weekday = parts
    time_text = f"{hour.zfill(2)}:{minute.zfill(2)}"
    weekdays = {
        "0": "周日",
        "7": "周日",
        "sun": "周日",
        "1": "周一",
        "mon": "周一",
        "2": "周二",
        "tue": "周二",
        "3": "周三",
        "wed": "周三",
        "4": "周四",
        "thu": "周四",
        "5": "周五",
        "fri": "周五",
        "6": "周六",
        "sat": "周六",
    }
    if day == "*" and month == "*" and weekday == "*":
        return f"每天 {time_text}"
    if day == "*" and month == "*" and weekday.lower() in weekdays:
        return f"每{weekdays[weekday.lower()]} {time_text}"
    if day != "*" and month == "*" and weekday == "*":
        return f"每月 {day} 日 {time_text}"
    return f"Cron: {value}"

