from __future__ import annotations

import logging
from typing import Any

import httpx

from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError, ChannexWriteDisabled
from apps.integrations.channex.outbound_guard import (
    can_write_to_channex,
    record_channex_write_blocked,
)

logger = logging.getLogger(__name__)


class ChannexClient:
    def __init__(self, config: ChannexRuntimeConfig) -> None:
        self._config = config
        self._session = httpx.Client(
            timeout=30.0,
            headers={
                "user-api-key": config.api_key,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> ChannexClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self._config.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        method_upper = method.upper()
        if method_upper != "GET" and not can_write_to_channex():
            record_channex_write_blocked(
                method=method_upper, path=path, reason="feature_flag"
            )
            raise ChannexWriteDisabled(
                method=method_upper,
                path=path,
                reason="feature_flag",
            )

        try:
            response = self._session.request(method, self._url(path), **kwargs)
        except httpx.HTTPError as exc:
            raise ChannexApiError(f"Channex HTTP error: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:500]
            raise ChannexApiError(
                f"Channex {method} {path} failed ({response.status_code}): {body}",
                status_code=response.status_code,
            )

        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise ChannexApiError(f"Unexpected Channex response type for {path}")
        return data

    def get_booking_revision(self, revision_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/booking_revisions/{revision_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ChannexApiError(f"Booking revision {revision_id} not found in response")
        return data

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        """Fetch latest booking revision details by Channex booking UUID."""
        payload = self._request("GET", f"/bookings/{booking_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ChannexApiError(f"Booking {booking_id} not found in response")
        return data

    def list_bookings(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", "/bookings", params=params or {})

    def find_booking_by_ota_reservation_code(self, ota_reservation_code: str) -> dict[str, Any] | None:
        """Find a booking by OTA reservation code (e.g. Booking.com confirmation number)."""
        code = ota_reservation_code.strip()
        if not code:
            return None
        payload = self.list_bookings(
            params={
                "filter[ota_reservation_code]": code,
                "pagination[limit]": 1,
            }
        )
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def acknowledge_booking_revision(self, revision_id: str) -> None:
        self._request("POST", f"/booking_revisions/{revision_id}/ack")

    def list_booking_revisions_feed(self) -> list[str]:
        """Return non-acknowledged booking revision IDs (fallback for missed webhooks)."""
        payload = self._request("GET", "/booking_revisions/feed")
        revision_ids: list[str] = []
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                revision_ids.append(str(item["id"]))
        return revision_ids

    def get_availability(
        self,
        *,
        property_id: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """GET room-type availability for a property date range.

        Returns the Availability object: ``{room_type_id: {YYYY-MM-DD: avail}}``.
        """
        payload = self._request(
            "GET",
            "/availability",
            params={
                "filter[property_id]": property_id,
                "filter[date][gte]": date_from,
                "filter[date][lte]": date_to,
            },
        )
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ChannexApiError("Unexpected Channex availability response shape")
        return data

    def update_availability(self, values: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/availability", json={"values": values})

    def update_restrictions(self, values: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/restrictions", json={"values": values})

    def list_booking_messages(self, booking_id: str) -> dict[str, Any]:
        return self._request("GET", f"/bookings/{booking_id}/messages")

    def upload_attachment(
        self,
        *,
        file_bytes: bytes,
        file_name: str,
        file_type: str,
    ) -> str:
        import base64

        payload = self._request(
            "POST",
            "/attachments",
            json={
                "attachment": {
                    "file": base64.b64encode(file_bytes).decode("ascii"),
                    "file_name": file_name,
                    "file_type": file_type,
                }
            },
        )
        data = payload.get("data")
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        raise ChannexApiError("Channex attachment upload missing id.")

    def send_booking_message(
        self,
        booking_id: str,
        message: str = "",
        *,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        msg_body: dict[str, Any] = {}
        if attachment_id:
            msg_body["attachment_id"] = attachment_id
        text = (message or "").strip()
        if text:
            msg_body["message"] = text
        if not msg_body:
            raise ChannexApiError("Message body or attachment_id is required.")
        return self._request(
            "POST",
            f"/bookings/{booking_id}/messages",
            json={"message": msg_body},
        )

    def fetch_attachment_bytes(self, attachment_path: str) -> tuple[bytes, str]:
        """Download attachment; path may be relative (attachments/...) or absolute URL."""
        path = (attachment_path or "").strip()
        if not path:
            raise ChannexApiError("Empty attachment path.")
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self._config.base_url}/{path.lstrip('/')}"
        try:
            response = self._session.get(url)
        except httpx.HTTPError as exc:
            raise ChannexApiError(f"Channex attachment HTTP error: {exc}") from exc
        if response.status_code >= 400:
            raise ChannexApiError(
                f"Channex GET attachment failed ({response.status_code}): {response.text[:500]}"
            )
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return response.content, content_type

    def list_message_thread_messages(self, thread_id: str) -> dict[str, Any]:
        return self._request("GET", f"/message_threads/{thread_id}/messages")

    def list_reviews(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", "/reviews", params=params or {})

    def get_review(self, review_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/reviews/{review_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ChannexApiError(f"Review {review_id} not found in response")
        return payload

    def reply_to_review(self, review_id: str, reply: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/reviews/{review_id}/reply",
            json={"reply": {"reply": reply}},
        )

    def submit_guest_review(self, review_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/reviews/{review_id}/guest_review",
            json=payload,
        )

    def report_no_show(self, booking_id: str, *, waived_fees: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/bookings/{booking_id}/no_show",
            json={"no_show_report": {"waived_fees": waived_fees}},
        )

    def cancel_booking(self, booking_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Cancel via Booking CRS API (requires Booking CRS app on property)."""
        return self._request("PUT", f"/bookings/{booking_id}", json=payload)

    # --- Photos Collection (ADR 0015 Phase B) ---

    def upload_photo_file(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        content_type: str = "image/jpeg",
    ) -> str:
        """POST /photos/upload (multipart) → temporary URL for create_photo."""
        name = (filename or "photo.jpg").strip() or "photo.jpg"
        payload = self._request(
            "POST",
            "/photos/upload",
            files={"photo": (name, file_bytes, content_type)},
        )
        url = payload.get("url")
        if not url:
            raise ChannexApiError("Channex photo upload missing url")
        return str(url)

    def create_photo(
        self,
        *,
        property_id: str,
        url: str,
        room_type_id: str | None = None,
        position: int | None = None,
        description: str = "",
        kind: str = "photo",
        author: str = "",
    ) -> dict[str, Any]:
        """POST /photos — associate uploaded URL with property / room type."""
        photo_body: dict[str, Any] = {
            "property_id": property_id,
            "url": url,
            "kind": kind or "photo",
        }
        if room_type_id:
            photo_body["room_type_id"] = room_type_id
        if position is not None:
            photo_body["position"] = int(position)
        if description:
            photo_body["description"] = description
        if author:
            photo_body["author"] = author
        return self._request("POST", "/photos", json={"photo": photo_body})

    def get_photo(self, photo_id: str) -> dict[str, Any]:
        """GET /photos/:id — read capability (smoke / verify / drift)."""
        payload = self._request("GET", f"/photos/{photo_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ChannexApiError(f"Photo {photo_id} not found in response")
        return data

    def list_photos(
        self,
        *,
        property_id: str,
        room_type_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /photos — read capability only (smoke / drift / admin; not upload path)."""
        params: dict[str, Any] = {
            "filter[property_id]": property_id,
            "pagination[limit]": int(limit),
        }
        if room_type_id:
            params["filter[room_type_id]"] = room_type_id
        payload = self._request("GET", "/photos", params=params)
        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ChannexApiError("Unexpected Channex photos list response shape")
        return [row for row in data if isinstance(row, dict)]

    def update_photo(self, photo_id: str, **attrs: Any) -> dict[str, Any]:
        """PUT /photos/:id — position / metadata (not byte replace)."""
        return self._request(
            "PUT",
            f"/photos/{photo_id}",
            json={"photo": attrs},
        )

    def delete_photo(self, photo_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/photos/{photo_id}")

    @staticmethod
    def extract_photo_id(response: dict[str, Any]) -> str:
        data = response.get("data")
        if isinstance(data, dict):
            photo_id = data.get("id")
            if photo_id:
                return str(photo_id)
            attrs = data.get("attributes")
            if isinstance(attrs, dict) and attrs.get("id"):
                return str(attrs["id"])
        raise ChannexApiError("Channex create photo response missing id")

    @staticmethod
    def photo_attributes(photo_obj: dict[str, Any]) -> dict[str, Any]:
        """Normalize JSON:API photo resource to a flat attributes dict."""
        if not isinstance(photo_obj, dict):
            return {}
        attrs = photo_obj.get("attributes")
        if isinstance(attrs, dict):
            merged = dict(attrs)
            if photo_obj.get("id") and "id" not in merged:
                merged["id"] = photo_obj["id"]
            return merged
        return photo_obj

    @staticmethod
    def extract_task_ids(response: dict[str, Any]) -> list[str]:
        task_ids: list[str] = []
        for item in response.get("data") or []:
            if isinstance(item, dict) and item.get("type") == "task" and item.get("id"):
                task_ids.append(str(item["id"]))
        return task_ids
