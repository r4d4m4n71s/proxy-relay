"""Tests for proxy_relay.lang — country→language mapping."""
from __future__ import annotations


class TestGetLanguageForCountry:
    """Tests for get_language_for_country()."""

    def test_known_country_co_returns_spanish(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("co") == "es-419,es"

    def test_known_country_us_returns_english(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("us") == "en-US,en"

    def test_known_country_de_returns_german(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("de") == "de-DE,de"

    def test_known_country_br_returns_portuguese(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("br") == "pt-BR,pt"

    def test_unknown_country_returns_none(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("xx") is None

    def test_case_insensitive_upper(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("CO") == "es-419,es"

    def test_case_insensitive_mixed(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("Us") == "en-US,en"

    def test_gb_returns_british_english(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("gb") == "en-GB,en"

    def test_jp_returns_japanese(self):
        from proxy_relay.lang import get_language_for_country

        assert get_language_for_country("jp") == "ja-JP,ja"
