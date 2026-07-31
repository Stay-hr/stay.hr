"""Mixin for management commands that write to Channex."""

from __future__ import annotations

import logging
import sys

from django.core.management.base import CommandError

from apps.integrations.channex.outbound_guard import (
    can_write_to_channex,
    force_channex_write,
)

logger = logging.getLogger(__name__)


class ChannexWriteCommandMixin:
    """Add ``--force-channex-outbound`` and guard to Channex write commands.

    Usage::

        class Command(ChannexWriteCommandMixin, BaseCommand):
            def add_arguments(self, parser):
                super().add_arguments(parser)
                ...

            def handle(self, *args, **options):
                with self.channex_write_context(options):
                    ...  # actual work
    """

    def add_arguments(self, parser):  # type: ignore[override]
        super().add_arguments(parser)  # type: ignore[misc]
        parser.add_argument(
            "--force-channex-outbound",
            action="store_true",
            help=(
                "Override CHANNEX_OUTBOUND_ENABLED=false for this invocation. "
                "Use only when hel1 is offline."
            ),
        )

    def channex_write_context(self, options: dict):
        """Return a context manager that either forces write or raises."""
        force = options.get("force_channex_outbound", False)

        if force:
            sys.stderr.write(
                "WARNING: --force-channex-outbound active; "
                "writing to Channex despite CHANNEX_OUTBOUND_ENABLED=false.\n"
            )
            logger.warning(
                "channex force write via CLI",
                extra={"event": "channex_force_cli", "reason": "force_cli"},
            )
            return force_channex_write()

        if not can_write_to_channex():
            raise CommandError(
                "Channex write disabled (CHANNEX_OUTBOUND_ENABLED=false). "
                "Use --force-channex-outbound if hel1 is offline."
            )

        # Write already allowed (hel1 / flag true) — no-op context
        return _noop_context()


class _noop_context:
    """Minimal context manager that does nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
