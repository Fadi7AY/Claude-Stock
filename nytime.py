"""New York wall-clock time without needing a timezone database (works on Windows too)."""
import datetime as dt


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def utc_offset_hours(utc):
    """-4 during US daylight time (2nd Sunday of March to 1st Sunday of November), else -5."""
    start = dt.datetime.combine(_nth_sunday(utc.year, 3, 2), dt.time(7, 0))   # 02:00 EST
    end = dt.datetime.combine(_nth_sunday(utc.year, 11, 1), dt.time(6, 0))    # 02:00 EDT
    return -4 if start <= utc < end else -5


def now_ny():
    """Naive datetime holding the current New York wall-clock time."""
    utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return utc + dt.timedelta(hours=utc_offset_hours(utc))


def ny_day_start_utc_ms(day):
    """Epoch milliseconds of 00:00 New York time on `day` (YYYY-MM-DD)."""
    d = dt.datetime.fromisoformat(day)
    utc = d + dt.timedelta(hours=-utc_offset_hours(d + dt.timedelta(hours=5)))
    return int((utc - dt.datetime(1970, 1, 1)).total_seconds() * 1000)


def regular_hours(now):
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 0)
