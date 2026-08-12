from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.evisitor.client import EvisitorClient
from apps.integrations.evisitor.exceptions import (
    EvisitorApiError,
    EvisitorConfigError,
    EvisitorValidationError,
)
from apps.integrations.evisitor.mapper import build_check_in_payload
from apps.integrations.evisitor.resolver import get_evisitor_config_row, resolve_evisitor_config
from apps.integrations.evisitor.scope import build_config_scope, format_config_scope_label
from apps.integrations.evisitor.service import submit_guest_checkin
from apps.properties.models import Property
from apps.reservations.models import EvisitorGuestStatus, Guest
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Read-only eVisitor integration smoke test: config resolution, login, optional "
        "payload validation or guest submit. Full submit updates Guest.evisitor_status and "
        "calls the HTZ API — use test guests only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            default="demo",
            help="Tenant slug (default: demo).",
        )
        parser.add_argument(
            "--property-slug",
            default="",
            help="Optional property slug for config resolution.",
        )
        parser.add_argument(
            "--list-config",
            action="store_true",
            help="Resolve and print configuration only; no EvisitorClient.",
        )
        parser.add_argument(
            "--login-only",
            action="store_true",
            help="Config resolution plus login/logout; no guest steps.",
        )
        parser.add_argument(
            "--guest-id",
            type=int,
            default=None,
            help="Guest primary key for payload validation or submit.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --guest-id: build check-in payload only (no API submit).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit a single JSON object on stdout (no secrets or PII).",
        )

    def handle(self, *args, **options):
        use_json = options["json"]
        steps = {
            "config": False,
            "login": False,
            "payload": False,
            "submit": False,
        }
        result: dict[str, Any] = {
            "steps": steps,
            "recovered": False,
        }

        try:
            self._validate_flags(options)
            tenant, property_obj = self._load_tenant_and_property(options)

            row = get_evisitor_config_row(tenant, property_obj)
            if row is None:
                scope = property_obj.slug if property_obj else "tenant"
                raise _SmokeFailure(
                    exit_code=1,
                    reason="config_error",
                    message=(
                        f"Nema aktivne eVisitor IntegrationConfig za "
                        f"tenant={tenant.slug}, scope={scope}."
                    ),
                )

            scope = build_config_scope(row)
            result["config_scope"] = scope
            result["config_row_id"] = row.pk

            try:
                config = resolve_evisitor_config(tenant, property_obj)
            except EvisitorConfigError as exc:
                raise _SmokeFailure(
                    exit_code=1,
                    reason="config_error",
                    message=str(exc),
                ) from exc

            steps["config"] = True
            result.update(
                {
                    "env": config.env,
                    "enabled": config.enabled,
                    "base_url": config.base_url,
                    "facility_code": config.facility_code,
                }
            )

            if not use_json:
                self._print_config_human(scope, row, config)

            if options["list_config"]:
                self._finish_ok(result, use_json)
                return

            client = EvisitorClient(config)
            try:
                client.login()
                steps["login"] = True
                if not use_json:
                    self._write_human("✓ Login successful")
            except EvisitorApiError as exc:
                raise _SmokeFailure(
                    exit_code=2,
                    reason="login_failed",
                    message=str(exc),
                ) from exc
            except EvisitorConfigError as exc:
                raise _SmokeFailure(
                    exit_code=1,
                    reason="config_error",
                    message=str(exc),
                ) from exc
            finally:
                try:
                    client.logout()
                finally:
                    client.close()

            guest_id = options["guest_id"]
            if guest_id is None:
                self._finish_ok(result, use_json)
                return

            guest = self._load_guest(guest_id, tenant)

            if options["dry_run"]:
                try:
                    build_check_in_payload(guest, config=config)
                except EvisitorValidationError as exc:
                    raise _SmokeFailure(
                        exit_code=3,
                        reason="validation_failed",
                        message=str(exc),
                        field_errors=exc.field_errors,
                    ) from exc
                steps["payload"] = True
                if not use_json:
                    self._write_human("✓ Payload valid")
                self._finish_ok(result, use_json)
                return

            try:
                was_already_sent = guest.evisitor_status == EvisitorGuestStatus.SENT
                submission = submit_guest_checkin(guest)
            except EvisitorValidationError as exc:
                raise _SmokeFailure(
                    exit_code=3,
                    reason="validation_failed",
                    message=str(exc),
                    field_errors=exc.field_errors,
                ) from exc
            except EvisitorApiError as exc:
                raise _SmokeFailure(
                    exit_code=4,
                    reason="submit_failed",
                    message=str(exc),
                ) from exc

            steps["payload"] = True
            if was_already_sent:
                result["submit_skipped_reason"] = "already_sent"
                if not use_json:
                    self._write_human("✓ Submit skipped (already sent)")
            elif (submission.response_payload or {}).get("recovered"):
                result["recovered"] = True
                steps["submit"] = True
                if not use_json:
                    self._write_human("✓ Submit recovered (already registered)")
            else:
                steps["submit"] = True
                if not use_json:
                    self._write_human("✓ Submit successful")

            self._finish_ok(result, use_json)

        except _SmokeFailure as failure:
            steps.update(failure.steps or {})
            result["steps"] = steps
            self._finish_error(result, failure, use_json)
            raise SystemExit(failure.exit_code) from failure
        except CommandError as exc:
            self._finish_error(
                result,
                _SmokeFailure(exit_code=1, reason="invalid_flags", message=str(exc)),
                use_json,
            )
            raise SystemExit(1) from exc

    def _validate_flags(self, options):
        if options["list_config"] and (
            options["login_only"] or options["dry_run"] or options["guest_id"] is not None
        ):
            raise CommandError(
                "--list-config cannot be combined with --login-only, --dry-run, or --guest-id."
            )
        if options["dry_run"] and options["guest_id"] is None:
            raise CommandError("--dry-run requires --guest-id.")
        if options["login_only"] and options["guest_id"] is not None:
            raise CommandError("--login-only cannot be combined with --guest-id.")

    def _load_tenant_and_property(self, options):
        tenant = Tenant.objects.filter(slug=options["tenant_slug"]).first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {options['tenant_slug']!r}.")

        property_slug = (options["property_slug"] or "").strip()
        property_obj = None
        if property_slug:
            property_obj = Property.objects.filter(tenant=tenant, slug=property_slug).first()
            if property_obj is None:
                raise CommandError(
                    f"Property not found: {property_slug!r} (tenant {tenant.slug})."
                )
        return tenant, property_obj

    def _load_guest(self, guest_id: int, tenant: Tenant) -> Guest:
        guest = (
            Guest.objects.select_related("reservation", "reservation__property", "tenant")
            .filter(pk=guest_id)
            .first()
        )
        if guest is None:
            raise CommandError(f"Guest id={guest_id} not found.")
        if guest.tenant_id != tenant.pk:
            raise CommandError(
                f"Guest id={guest_id} belongs to tenant {guest.tenant.slug!r}, "
                f"not {tenant.slug!r}."
            )
        return guest

    def _print_config_human(self, scope, row, config):
        self.stderr.write("Resolved configuration\n")
        self.stderr.write(f"Scope: {format_config_scope_label(scope)}\n")
        self.stderr.write(f"Row ID: {row.pk}\n")
        self.stderr.write(f"Environment: {config.env}\n")
        self.stderr.write(f"Base URL: {config.base_url}\n")
        self.stderr.write(f"Facility: {config.facility_code}\n")
        self.stderr.write(f"Enabled: {'yes' if config.enabled else 'no'}\n\n")
        self._write_human("✓ Config resolved")

    def _write_human(self, message: str):
        self.stderr.write(f"{message}\n")

    def _finish_ok(self, result: dict[str, Any], use_json: bool):
        if use_json:
            payload = {"status": "ok", "exit_code": 0, **result}
            self.stdout.write(json.dumps(payload, ensure_ascii=False))
        else:
            self.stderr.write("\nSMOKE PASSED\n")

    def _finish_error(self, result: dict[str, Any], failure: _SmokeFailure, use_json: bool):
        if use_json:
            payload = {
                "status": "error",
                "exit_code": failure.exit_code,
                "reason": failure.reason,
                "message": failure.message,
                **result,
            }
            if failure.field_errors:
                payload["field_errors"] = failure.field_errors
            self.stdout.write(json.dumps(payload, ensure_ascii=False))
        else:
            self.stderr.write(self.style.ERROR(f"✗ {failure.reason.replace('_', ' ').title()}\n"))
            self.stderr.write(f"Reason: {failure.message}\n")
            if failure.field_errors:
                for field, error in failure.field_errors.items():
                    self.stderr.write(f"  {field}: {error}\n")


class _SmokeFailure(Exception):
    def __init__(
        self,
        *,
        exit_code: int,
        reason: str,
        message: str,
        field_errors: dict | None = None,
        steps: dict | None = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.reason = reason
        self.message = message
        self.field_errors = field_errors or {}
        self.steps = steps
