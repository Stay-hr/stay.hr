from django.test import TestCase

from apps.communications.guest_language_constants import (
    CONVERSATION_UPDATE_THRESHOLD,
    MESSAGE_OVERRIDES_LLM_THRESHOLD,
)
from apps.communications.language_detection import detect


class LanguageDetectionMatrixTests(TestCase):
    """One realistic guest sentence per supported language."""

    CASES = {
        "sk": "Ďakujem, kde je izba?",
        "de": "Können wir spaeter ankommen?",
        "hr": "Možemo li doći kasnije večeras?",
        "es": "Hola, ¿dónde está el aparcamiento? Gracias",
        "fr": "Bonjour, nous voudrions une chambre, merci",
        "it": "Grazie, possiamo arrivare in sera?",
        "pl": "Dziękuję, czy możemy przyjechać później?",
        "ro": "Mulțumim, unde este camera? Sosire seara",
        "nl": "Dank u, waar is de kamer? Aankomst in de avond",
        "cs": "Děkuji, kde je pokoj? Příjezd večer",
        "pt": "Obrigado, onde fica o quarto? Chegada a noite",
        "hu": "Köszönöm, hol van a szoba? Érkezés este",
        "en": "Hello, what time can we check the room? Thank you",
    }

    def test_each_language_detected(self):
        for expected, text in self.CASES.items():
            with self.subTest(language=expected):
                self.assertEqual(detect(text).language, expected)


class LanguageDetectionTests(TestCase):
    def test_italian_message(self):
        result = detect("Grazie, possiamo arrivare in sera?")
        self.assertEqual(result.language, "it")
        self.assertGreaterEqual(result.confidence, 0.65)

    def test_german_message(self):
        result = detect("Können wir spaeter ankommen?")
        self.assertEqual(result.language, "de")
        self.assertGreaterEqual(result.confidence, 0.65)

    def test_croatian_message(self):
        result = detect("Možemo li doći kasnije večeras?")
        self.assertEqual(result.language, "hr")
        self.assertGreaterEqual(result.confidence, 0.65)

    def test_polish_message(self):
        result = detect("Dziękuję, czy możemy przyjechać później?")
        self.assertEqual(result.language, "pl")

    def test_stem_markers_still_match_inflected_words(self):
        # `dolaz` must keep matching `Dolazimo`, which exact-token sets would lose.
        self.assertEqual(detect("Dolazimo oko 19:30").language, "hr")

    def test_clear_english_scores_high(self):
        result = detect("See you tomorrow")
        self.assertEqual(result.language, "en")
        self.assertGreaterEqual(result.confidence, MESSAGE_OVERRIDES_LLM_THRESHOLD)

    def test_english_message_with_foreign_filler_word(self):
        result = detect("Hello, is it also possible to have a parking spot molimteh ? 😁")
        self.assertEqual(result.language, "en")
        self.assertGreaterEqual(result.confidence, MESSAGE_OVERRIDES_LLM_THRESHOLD)

    def test_single_signal_cannot_persist_conversation_language(self):
        for text in ("Thanks", "Danke", "Merci"):
            with self.subTest(text=text):
                self.assertLess(detect(text).confidence, CONVERSATION_UPDATE_THRESHOLD)

    def test_croatian_question_not_pulled_to_english_by_domain_noun(self):
        self.assertEqual(detect("Gdje je parking?").language, "hr")

    def test_no_marker_message_falls_back_to_english(self):
        result = detect("Ok 123")
        self.assertEqual(result.language, "en")
        self.assertLess(result.confidence, CONVERSATION_UPDATE_THRESHOLD)

    def test_unknown_empty(self):
        result = detect("")
        self.assertEqual(result.language, "unknown")
        self.assertEqual(result.confidence, 0.0)

    def test_unknown_emoji_only(self):
        result = detect("👍")
        self.assertEqual(result.language, "unknown")
        self.assertEqual(result.confidence, 0.0)
