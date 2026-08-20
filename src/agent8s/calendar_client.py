from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional

import caldav

from .config import Config


class CalendarError(RuntimeError):
    pass


@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime
    end: Optional[datetime]
    location: str


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    raise CalendarError(f"unexpected date value: {value!r}")


def fetch_events(config: Config, start: datetime, end: datetime) -> list[CalendarEvent]:
    if not config.caldav_configured:
        raise CalendarError("Yandex Calendar is not configured (YANDEX_CALDAV_URL / _LOGIN / _PASSWORD)")

    try:
        client = caldav.DAVClient(url=config.caldav_url, username=config.caldav_login, password=config.caldav_password)
        calendar = caldav.Calendar(client=client, url=config.caldav_url)
        raw_events = calendar.search(start=start, end=end, event=True, expand=True)
    except caldav.lib.error.AuthorizationError as e:
        raise CalendarError(f"CalDAV auth failed: {e}") from e
    except caldav.lib.error.DAVError as e:
        raise CalendarError(f"CalDAV request failed: {e}") from e

    events: list[CalendarEvent] = []
    for raw in raw_events:
        component = raw.icalendar_component
        dtstart_prop = component.get("dtstart")
        if dtstart_prop is None:
            continue
        dtstart = _as_datetime(dtstart_prop.dt)

        dtend_prop = component.get("dtend")
        dtend = _as_datetime(dtend_prop.dt) if dtend_prop is not None else None

        events.append(
            CalendarEvent(
                uid=str(component.get("uid", "")),
                summary=str(component.get("summary", "(no title)")),
                start=dtstart,
                end=dtend,
                location=str(component.get("location", "") or ""),
            )
        )

    events.sort(key=lambda e: e.start)
    return events
