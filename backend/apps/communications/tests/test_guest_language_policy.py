from django.test import TestCase

from apps.communications.guest_language_context import LanguageMode, LanguageSource
from apps.communications.guest_language_policy import choose
from apps.communications.language_detection import DetectionResult


class GuestLanguagePolicyTests(TestCase):
    def test_reactive_message_beats_country(self):
        ctx = choose(
            mode=LanguageMode.REACTIVE,
            message_detection=DetectionResult(language="en", confidence=0.85),
            country_iso2="DE",
        )
        self.assertEqual(ctx.language, "en")
        self.assertEqual(ctx.source, LanguageSource.MESSAGE)

    def test_reactive_conversation_when_message_not_detectable(self):
        from apps.communications.conversation_language_store import StoredConversationLanguage

        ctx = choose(
            mode=LanguageMode.REACTIVE,
            conversation=StoredConversationLanguage(
                language="it",
                source=LanguageSource.MESSAGE,
                updated_at=None,
            ),
            country_iso2="DE",
        )
        self.assertEqual(ctx.language, "it")
        self.assertEqual(ctx.source, LanguageSource.CONVERSATION)

    def test_proactive_country_skips_message(self):
        ctx = choose(
            mode=LanguageMode.PROACTIVE,
            message_detection=DetectionResult(language="en", confidence=0.85),
            country_iso2="IT",
        )
        self.assertEqual(ctx.language, "it")
        self.assertEqual(ctx.source, LanguageSource.COUNTRY)

    def test_high_confidence_message_beats_llm_reply_language(self):
        ctx = choose(
            mode=LanguageMode.REACTIVE,
            reply_language="hr",
            message_detection=DetectionResult(language="en", confidence=0.9),
        )
        self.assertEqual(ctx.language, "en")
        self.assertEqual(ctx.source, LanguageSource.MESSAGE)

    def test_low_confidence_message_yields_to_llm_reply_language(self):
        ctx = choose(
            mode=LanguageMode.REACTIVE,
            reply_language="hr",
            message_detection=DetectionResult(language="en", confidence=0.7),
        )
        self.assertEqual(ctx.language, "hr")
        self.assertEqual(ctx.source, LanguageSource.REPLY_LANGUAGE)

    def test_override_wins(self):
        ctx = choose(
            mode=LanguageMode.REACTIVE,
            override="de",
            message_detection=DetectionResult(language="en", confidence=0.85),
            country_iso2="IT",
        )
        self.assertEqual(ctx.language, "de")
        self.assertEqual(ctx.source, LanguageSource.OVERRIDE)

    def test_country_it_maps_to_italian(self):
        ctx = choose(
            mode=LanguageMode.PROACTIVE,
            country_iso2="IT",
        )
        self.assertEqual(ctx.language, "it")

    def test_fallback_en(self):
        ctx = choose(mode=LanguageMode.PROACTIVE)
        self.assertEqual(ctx.language, "en")
        self.assertEqual(ctx.source, LanguageSource.FALLBACK)
