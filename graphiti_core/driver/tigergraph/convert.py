"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Conversions between Graphiti's models and TigerGraph attributes.

TigerGraph has no NULL and its DATETIME keeps seconds only, while Graphiti orders facts by microsecond timestamps and
treats ``None`` as meaningful (an edge that has not expired). Every Graphiti datetime is therefore stored as a
fixed-width UTC string, ``'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'``, and ``None`` as the empty string. Fixed-width strings
order lexicographically the way the instants order in time, so a range filter in GSQL is a plain string comparison.
"""

import json
from datetime import datetime, timezone
from typing import Any

DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%f+00:00'
# The smallest and largest strings a filter can compare against, for an open end of a range
MIN_DATETIME = '0000-01-01T00:00:00.000000+00:00'
MAX_DATETIME = '9999-12-31T23:59:59.999999+00:00'


def dt_to_str(value: datetime | str | None) -> str:
    """A datetime as its stored string; a naive datetime is taken as UTC; ``None`` is ``''``."""
    if value is None or value == '':
        return ''
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(DATETIME_FORMAT)


def str_to_dt(value: str | None) -> datetime | None:
    """The datetime of a stored string (aware, UTC); ``''`` and ``None`` give ``None``."""
    if not value:
        return None
    return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=timezone.utc)


def str_to_dt_required(value: str | None) -> datetime:
    parsed = str_to_dt(value)
    if parsed is None:
        raise ValueError('a required datetime attribute is empty')
    return parsed


def dumps(value: dict[str, Any] | None) -> str:
    """A dict of free-form attributes as the JSON string stored in a STRING attribute (``''`` when empty)."""
    return json.dumps(value, default=str, separators=(',', ':')) if value else ''


def loads(value: str | None) -> dict[str, Any]:
    return json.loads(value) if value else {}


def float_list(value: list[float] | None) -> list[float]:
    """A vector for an upsert; an absent embedding is an empty list."""
    return [float(x) for x in value] if value else []


def chunked(items: list[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
