from django.test import SimpleTestCase

from apps.integrations.whatsapp.media_download import extract_media_descriptor, extract_media_from_message


class MediaDescriptorTests(SimpleTestCase):
    def test_image_with_caption(self):
        d = extract_media_descriptor(
            {"type": "image", "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "Soba", "sha256": "ABC"}}
        )
        self.assertEqual((d.kind, d.media_id, d.caption, d.sha256), ("image", "m1", "Soba", "abc"))

    def test_document_keeps_filename(self):
        d = extract_media_descriptor(
            {"type": "document", "document": {"id": "m2", "mime_type": "application/pdf", "filename": "../x.pdf"}}
        )
        self.assertEqual((d.kind, d.filename), ("document", "../x.pdf"))

    def test_video_audio_voice_sticker(self):
        kinds = {
            "video": {"type": "video", "video": {"id": "v"}},
            "audio": {"type": "audio", "audio": {"id": "a"}},
            "voice": {"type": "audio", "audio": {"id": "a", "voice": True}},
            "sticker": {"type": "sticker", "sticker": {"id": "s"}},
        }
        for expected, payload in kinds.items():
            self.assertEqual(extract_media_descriptor(payload).kind, expected)

    def test_non_media_returns_none(self):
        for payload in (
            {"type": "text", "text": {"body": "hi"}},
            {"type": "interactive", "interactive": {}},
            {"type": "image"},
            {"type": "image", "image": {}},
            {},
        ):
            self.assertIsNone(extract_media_descriptor(payload))

    def test_legacy_extractor_unchanged(self):
        self.assertEqual(
            extract_media_from_message({"type": "image", "image": {"id": "m1", "mime_type": "image/png", "caption": "c"}}),
            ("m1", "image/png", "c"),
        )
        self.assertEqual(extract_media_from_message({"type": "video", "video": {"id": "v"}}), ("", "", ""))
