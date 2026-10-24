from datetime import date, datetime, time, timedelta, timezone
import re

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
PERIOD_LABELS = {"daily": "日报", "weekly": "周报", "monthly": "月报"}


def parse_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期应为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期无效") from exc


def timestamp(day: date) -> int:
    return int(datetime.combine(day, time.min, SHANGHAI).timestamp())


def period_range(period: str, anchor: str) -> tuple[date, date]:
    """Return a local calendar range, with exclusive end."""
    day = parse_date(anchor)
    if period == "daily":
        start, end = day, day + timedelta(days=1)
    elif period == "weekly":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif period == "monthly":
        start = day.replace(day=1)
        end = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    else:
        raise ValueError("报告周期须为 daily、weekly 或 monthly")
    return start, end
