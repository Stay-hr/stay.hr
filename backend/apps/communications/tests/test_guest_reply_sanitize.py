from django.test import TestCase

from apps.communications.guest_reply_sanitize import strip_reply_boilerplate


class StripReplyBoilerplateTests(TestCase):
    def test_full_letter_reduced_to_body(self):
        text = (
            "Bok Antoine!\n\n"
            "Parkiranje je besplatno.\n\n"
            "Lijep pozdrav,\n"
            "Uzorita B&B\n\n"
            "Managed by stay.hr — https://stay.hr/"
        )
        self.assertEqual(
            strip_reply_boilerplate(text, property_name="Uzorita B&B"),
            "Parkiranje je besplatno.",
        )

    def test_property_name_kept_without_property_name_argument(self):
        # Without the name, the last line cannot be identified, so the sign-off
        # above it is not trailing either and both stay.
        text = "Parking is free.\n\nBest regards,\nUzorita B&B"
        self.assertEqual(strip_reply_boilerplate(text), text)

    def test_property_name_needs_exact_match(self):
        text = "Parking is free.\n\nUzorita B&B, Zadar"
        self.assertEqual(
            strip_reply_boilerplate(text, property_name="Uzorita B&B"),
            text,
        )

    def test_footer_removed_anywhere(self):
        text = "Managed by stay.hr — https://stay.hr/\n\nParking is free."
        self.assertEqual(strip_reply_boilerplate(text), "Parking is free.")

    def test_multiline_body_keeps_paragraphs(self):
        text = "Hello,\n\nParking is free.\n\nThe zone is unrestricted.\n\nBest regards,"
        self.assertEqual(
            strip_reply_boilerplate(text),
            "Parking is free.\n\nThe zone is unrestricted.",
        )

    def test_body_sentence_starting_like_greeting_is_kept(self):
        text = "Hello, we do have a free parking spot right in front of the house."
        self.assertEqual(strip_reply_boilerplate(text), text)

    def test_boilerplate_only_returns_empty(self):
        text = "Hi Antoine!\n\nBest regards,\nUzorita B&B\n\nManaged by stay.hr"
        self.assertEqual(strip_reply_boilerplate(text, property_name="Uzorita B&B"), "")

    def test_empty_input(self):
        self.assertEqual(strip_reply_boilerplate(""), "")
