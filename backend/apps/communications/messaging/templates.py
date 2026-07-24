"""Versioned render + checksum (ADR 0010 §4.D)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch


def normalize_render_text(body: str, subject: str = "") -> str:
    """Normalize for stable checksum across platforms."""
    body_n = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    subject_n = (subject or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{subject_n}\n{body_n}"


def compute_render_checksum(body: str, subject: str = "") -> str:
    """SHA-256 hex of normalized rendered body (+ subject)."""
    payload = normalize_render_text(body, subject)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RenderSnapshot:
    body: str
    subject: str
    language: str
    template_version: str
    render_context: dict[str, Any]
    checksum: str


Renderer = Callable[
    [MessageDispatch, TriggerContext, Mapping[str, Any]],
    tuple[str, str, dict[str, Any]],
]
"""(dispatch, ctx, base_context) -> (body, subject, context_updates)."""


class TemplateRegistry:
    """Maps renderer_key → callable; template_version keys must be unique."""

    def __init__(self) -> None:
        self._renderers: dict[str, Renderer] = {}
        self._template_versions: dict[str, str] = {}
        # template_version -> renderer_key (for duplicate detection)

    def clear(self) -> None:
        self._renderers.clear()
        self._template_versions.clear()

    def register(
        self,
        *,
        renderer_key: str,
        template_version: str,
        renderer: Renderer,
    ) -> None:
        if not renderer_key:
            raise ValueError("renderer_key is required")
        if not template_version:
            raise ValueError(
                f"template_version required for renderer {renderer_key!r}"
            )
        if renderer_key in self._renderers:
            raise ValueError(f"Duplicate renderer_key: {renderer_key!r}")
        existing = self._template_versions.get(template_version)
        if existing is not None and existing != renderer_key:
            raise ValueError(
                f"Duplicate template_version {template_version!r} "
                f"(already used by {existing!r})"
            )
        self._renderers[renderer_key] = renderer
        self._template_versions[template_version] = renderer_key

    def get_renderer(self, renderer_key: str) -> Renderer:
        try:
            return self._renderers[renderer_key]
        except KeyError as exc:
            raise KeyError(f"Unknown renderer: {renderer_key!r}") from exc

    def has_renderer(self, renderer_key: str) -> bool:
        return renderer_key in self._renderers

    def has_template_version(self, template_version: str) -> bool:
        return template_version in self._template_versions

    def renderer_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._renderers))

    def template_versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._template_versions))

    def render(
        self,
        *,
        renderer_key: str,
        template_version: str,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
        language: str = "",
        base_context: Mapping[str, Any] | None = None,
    ) -> RenderSnapshot:
        renderer = self.get_renderer(renderer_key)
        merged: dict[str, Any] = {}
        if isinstance(dispatch.render_context, dict):
            merged.update(dispatch.render_context)
        if base_context:
            merged.update(dict(base_context))
        if ctx.extras:
            merged.update(dict(ctx.extras))
        body, subject, updates = renderer(dispatch, ctx, merged)
        if updates:
            merged.update(updates)
        checksum = compute_render_checksum(body, subject)
        return RenderSnapshot(
            body=body or "",
            subject=subject or "",
            language=language or str(merged.get("language") or ""),
            template_version=template_version,
            render_context=merged,
            checksum=checksum,
        )


template_registry = TemplateRegistry()
