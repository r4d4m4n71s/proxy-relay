"""Timezone vs proxy country mismatch detection.

Warns when the local system timezone does not match the expected timezone
for the proxy's exit country. This is a heuristic anti-leak measure —
websites can detect timezone mismatches via JavaScript's
``Intl.DateTimeFormat().resolvedOptions().timeZone``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import lru_cache

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# Mapping of ISO alpha-2 country codes to expected UTC offset ranges.
# Each entry is (min_offset_hours, max_offset_hours) inclusive.
# Countries spanning multiple timezones use the broadest range.
_COUNTRY_UTC_OFFSETS: dict[str, tuple[float, float]] = {
    "us": (-10.0, -5.0),   # Hawaii to Eastern
    "ca": (-8.0, -3.5),    # Pacific to Newfoundland
    "gb": (0.0, 1.0),      # GMT/BST
    "de": (1.0, 2.0),      # CET/CEST
    "fr": (1.0, 2.0),
    "nl": (1.0, 2.0),
    "es": (1.0, 2.0),
    "it": (1.0, 2.0),
    "se": (1.0, 2.0),
    "no": (1.0, 2.0),
    "dk": (1.0, 2.0),
    "fi": (2.0, 3.0),
    "pl": (1.0, 2.0),
    "br": (-5.0, -2.0),    # Acre to Fernando de Noronha
    "ar": (-3.0, -3.0),
    "co": (-5.0, -5.0),
    "mx": (-8.0, -5.0),    # Pacific to Eastern
    "cl": (-5.0, -3.0),
    "au": (8.0, 11.0),     # Perth to Lord Howe
    "nz": (12.0, 13.0),
    "jp": (9.0, 9.0),
    "kr": (9.0, 9.0),
    "in": (5.5, 5.5),
    "sg": (8.0, 8.0),
    "za": (2.0, 2.0),
    "il": (2.0, 3.0),
    "ae": (4.0, 4.0),
    "ru": (2.0, 12.0),     # Kaliningrad to Kamchatka
    "cn": (8.0, 8.0),
    "hk": (8.0, 8.0),
    "tw": (8.0, 8.0),
    "th": (7.0, 7.0),
    "my": (8.0, 8.0),
    "id": (7.0, 9.0),      # WIB to WIT
    "ph": (8.0, 8.0),
    "pt": (0.0, 1.0),      # WET/WEST
    "ie": (0.0, 1.0),      # GMT/IST
    "at": (1.0, 2.0),
    "ch": (1.0, 2.0),
    "be": (1.0, 2.0),
}

# Mapping of ISO alpha-2 country codes to representative IANA timezone names.
# Used to set the TZ environment variable on the Chromium subprocess so that
# JavaScript Intl.DateTimeFormat() reports a timezone consistent with the
# proxy exit IP.  For multi-timezone countries, the capital/most-populated
# zone is chosen.
_COUNTRY_TIMEZONES: dict[str, str] = {
    "us": "America/New_York",
    "ca": "America/Toronto",
    "gb": "Europe/London",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "nl": "Europe/Amsterdam",
    "es": "Europe/Madrid",
    "it": "Europe/Rome",
    "se": "Europe/Stockholm",
    "no": "Europe/Oslo",
    "dk": "Europe/Copenhagen",
    "fi": "Europe/Helsinki",
    "pl": "Europe/Warsaw",
    "br": "America/Sao_Paulo",
    "ar": "America/Argentina/Buenos_Aires",
    "co": "America/Bogota",
    "mx": "America/Mexico_City",
    "cl": "America/Santiago",
    "au": "Australia/Sydney",
    "nz": "Pacific/Auckland",
    "jp": "Asia/Tokyo",
    "kr": "Asia/Seoul",
    "in": "Asia/Kolkata",
    "sg": "Asia/Singapore",
    "za": "Africa/Johannesburg",
    "il": "Asia/Jerusalem",
    "ae": "Asia/Dubai",
    "ru": "Europe/Moscow",
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "tw": "Asia/Taipei",
    "th": "Asia/Bangkok",
    "my": "Asia/Kuala_Lumpur",
    "id": "Asia/Jakarta",
    "ph": "Asia/Manila",
    "pt": "Europe/Lisbon",
    "ie": "Europe/Dublin",
    "at": "Europe/Vienna",
    "ch": "Europe/Zurich",
    "be": "Europe/Brussels",
}


@lru_cache(maxsize=64)
def get_timezone_for_country(country_code: str) -> str | None:
    """Return a representative IANA timezone for a country code.

    Used by the ``browse`` command to set the ``TZ`` environment variable
    on the Chromium subprocess, preventing JavaScript timezone fingerprinting.

    Args:
        country_code: ISO alpha-2 country code (case-insensitive).

    Returns:
        IANA timezone string (e.g., ``"Europe/Berlin"``), or None if the
        country is not in the lookup table.
    """
    tz = _COUNTRY_TIMEZONES.get(country_code.lower())
    if tz is not None:
        log.debug("Resolved timezone for %s: %s", country_code.upper(), tz)
    else:
        log.debug("No timezone mapping for country %r", country_code)
    return tz


def get_local_utc_offset_hours() -> float:
    """Return the local system's current UTC offset in hours.

    Accounts for DST by using the system's current UTC offset rather
    than a static timezone definition.

    Returns:
        UTC offset in hours (e.g., -5.0 for EST, 1.0 for CET).
    """
    # Use time.timezone/altzone which reflect the C library's view
    if time.daylight and time.localtime().tm_isdst:
        offset_secs = -time.altzone
    else:
        offset_secs = -time.timezone
    return offset_secs / 3600.0


def check_timezone_mismatch(country_code: str) -> bool:
    """Check if the local timezone matches the proxy exit country.

    Compares the local system UTC offset against the expected range for
    the given country. Returns True if there is a mismatch (which could
    be detected by JavaScript-based fingerprinting).

    Args:
        country_code: ISO alpha-2 country code (case-insensitive).

    Returns:
        True if there is a timezone mismatch, False if it matches or
        the country is not in the lookup table.
    """
    lower = country_code.lower()

    if lower not in _COUNTRY_UTC_OFFSETS:
        log.debug(
            "Country %r not in timezone lookup table, skipping check",
            country_code,
        )
        return False

    min_offset, max_offset = _COUNTRY_UTC_OFFSETS[lower]
    local_offset = get_local_utc_offset_hours()

    mismatch = local_offset < min_offset or local_offset > max_offset

    if mismatch:
        log.warning(
            "Timezone mismatch detected: local UTC%+.1f is outside "
            "expected range UTC%+.1f to UTC%+.1f for country %r. "
            "Websites may detect this via JavaScript timezone APIs.",
            local_offset,
            min_offset,
            max_offset,
            country_code.upper(),
        )
    else:
        log.debug(
            "Timezone OK: local UTC%+.1f within range UTC%+.1f to UTC%+.1f for %s",
            local_offset,
            min_offset,
            max_offset,
            country_code.upper(),
        )

    return mismatch
