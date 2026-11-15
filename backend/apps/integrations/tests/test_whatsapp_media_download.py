from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

import hashlib
from io import BytesIO

from apps.integrations.whatsapp.media_download import (
    WhatsAppMediaError,
    WhatsAppMediaTooLarge,
    fetch_whatsapp_media,
    fetch_whatsapp_media_to_file,
)


class WhatsAppMediaDownloadTests(SimpleTestCase):
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_fetch_whatsapp_media_meta(self, mock_get):
        meta_response = MagicMock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "url": "https://lookaside.fbsbx.com/media.bin",
            "mime_type": "image/jpeg",
        }
        file_response = MagicMock()
        file_response.status_code = 200
        file_response.content = b"image-bytes"
        mock_get.side_effect = [meta_response, file_response]

        content, mime = fetch_whatsapp_media(
            media_id="media-123",
            access_token="meta-token",
        )

        self.assertEqual(content, b"image-bytes")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(mock_get.call_count, 2)
        self.assertIn("Bearer meta-token", mock_get.call_args_list[0].kwargs["headers"]["Authorization"])


class _StreamResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, chunk_size=None):
        yield from self._chunks


def _meta(**extra):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"url": "https://lookaside.fbsbx.com/m", "mime_type": "video/mp4", **extra}
    return response


class WhatsAppMediaStreamingTests(SimpleTestCase):
    @patch("apps.integrations.whatsapp.media_download.httpx.stream")
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_streams_and_verifies_sha_and_size(self, mock_get, mock_stream):
        data = b"abc" * 1000
        mock_get.return_value = _meta(sha256=hashlib.sha256(data).hexdigest().upper(), file_size=str(len(data)))
        mock_stream.return_value = _StreamResponse([data[:100], data[100:]])
        dest = BytesIO()
        result = fetch_whatsapp_media_to_file(media_id="m", dest=dest, max_bytes=10**6, access_token="t")
        self.assertEqual(dest.getvalue(), data)
        self.assertEqual((result.size, result.mime_type), (len(data), "video/mp4"))
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())

    @patch("apps.integrations.whatsapp.media_download.httpx.stream")
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_sha_mismatch_is_error(self, mock_get, mock_stream):
        mock_get.return_value = _meta(sha256="0" * 64)
        mock_stream.return_value = _StreamResponse([b"data"])
        with self.assertRaisesMessage(WhatsAppMediaError, "sha256 mismatch"):
            fetch_whatsapp_media_to_file(media_id="m", dest=BytesIO(), max_bytes=100, access_token="t")

    @patch("apps.integrations.whatsapp.media_download.httpx.stream")
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_declared_size_over_cap_fails_before_download(self, mock_get, mock_stream):
        mock_get.return_value = _meta(file_size=500)
        with self.assertRaises(WhatsAppMediaTooLarge):
            fetch_whatsapp_media_to_file(media_id="m", dest=BytesIO(), max_bytes=100, access_token="t")
        mock_stream.assert_not_called()

    @patch("apps.integrations.whatsapp.media_download.httpx.stream")
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_stream_over_cap_aborts(self, mock_get, mock_stream):
        mock_get.return_value = _meta()
        mock_stream.return_value = _StreamResponse([b"x" * 60, b"x" * 60])
        with self.assertRaises(WhatsAppMediaTooLarge):
            fetch_whatsapp_media_to_file(media_id="m", dest=BytesIO(), max_bytes=100, access_token="t")

    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_error_carries_status_and_retryability(self, mock_get):
        for status, retryable in ((404, False), (410, False), (503, True), (429, True), (401, True)):
            response = MagicMock(status_code=status, text="err")
            mock_get.return_value = response
            with self.assertRaises(WhatsAppMediaError) as ctx:
                fetch_whatsapp_media_to_file(media_id="m", dest=BytesIO(), max_bytes=0, access_token="t")
            self.assertEqual((ctx.exception.status_code, ctx.exception.retryable), (status, retryable))
