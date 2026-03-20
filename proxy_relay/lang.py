"""Browser language tag resolution by proxy country code.

Maps ISO alpha-2 country codes to BCP 47 Accept-Language strings used to
set the ``--lang`` Chromium flag.  This aligns ``navigator.language`` with
the proxy exit country, suppressing a common DataDome fingerprinting signal.
"""
from __future__ import annotations

from functools import lru_cache

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# Mapping of ISO alpha-2 country codes to BCP 47 Accept-Language strings
# suitable for Chromium's --lang flag.  The value includes the regional
# tag and a bare language fallback (e.g. "es-419,es" not just "es-419").
# Country set matches _COUNTRY_TIMEZONES in tz.py for consistency.
_COUNTRY_LANGUAGES: dict[str, str] = {
    "us": "en-US,en",
    "ca": "en-CA,en",
    "gb": "en-GB,en",
    "de": "de-DE,de",
    "fr": "fr-FR,fr",
    "nl": "nl-NL,nl",
    "es": "es-ES,es",
    "it": "it-IT,it",
    "se": "sv-SE,sv",
    "no": "nb-NO,nb",
    "dk": "da-DK,da",
    "fi": "fi-FI,fi",
    "pl": "pl-PL,pl",
    "br": "pt-BR,pt",
    "ar": "es-AR,es",
    "co": "es-419,es",
    "mx": "es-MX,es",
    "cl": "es-CL,es",
    "au": "en-AU,en",
    "nz": "en-NZ,en",
    "jp": "ja-JP,ja",
    "kr": "ko-KR,ko",
    "in": "en-IN,en",
    "sg": "en-SG,en",
    "za": "en-ZA,en",
    "il": "he-IL,he",
    "ae": "ar-AE,ar",
    "ru": "ru-RU,ru",
    "cn": "zh-CN,zh",
    "hk": "zh-HK,zh",
    "tw": "zh-TW,zh",
    "th": "th-TH,th",
    "my": "ms-MY,ms",
    "id": "id-ID,id",
    "ph": "en-PH,en",
    "pt": "pt-PT,pt",
    "ie": "en-IE,en",
    "at": "de-AT,de",
    "ch": "de-CH,de",
    "be": "nl-BE,nl",
}


@lru_cache(maxsize=64)
def get_language_for_country(country_code: str) -> str | None:
    """Return a BCP 47 Accept-Language string for a country code.

    Used by the ``browse`` and ``warmup`` commands to set ``--lang`` on
    the Chromium subprocess, aligning ``navigator.language`` with the
    proxy exit country to suppress DataDome fingerprint mismatch signals.

    Args:
        country_code: ISO alpha-2 country code (case-insensitive).

    Returns:
        Accept-Language string (e.g., ``"es-419,es"``), or ``None`` if
        the country is not in the lookup table.
    """
    lang = _COUNTRY_LANGUAGES.get(country_code.lower())
    if lang is not None:
        log.debug("Resolved language for %s: %s", country_code.upper(), lang)
    else:
        log.debug("No language mapping for country %r", country_code)
    return lang
