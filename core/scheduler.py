from datetime import time as dtime
from typing import List


def build_clock_times(start: dtime, end: dtime, step_minutes: int) -> List[str]:
    """Build a list of HH:MM strings between start and end inclusive."""
    if step_minutes <= 0:
        raise ValueError("step_minutes must be > 0")

    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if start_minutes > end_minutes:
        return []

    times: List[str] = []
    for minute_value in range(start_minutes, end_minutes + 1, step_minutes):
        hh, mm = divmod(minute_value, 60)
        times.append(f"{hh:02d}:{mm:02d}")
    return times
