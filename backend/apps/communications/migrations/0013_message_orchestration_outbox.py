# Messaging Orchestration Engine outbox (ADR 0010 Phase 2)

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0012_guestoutbound_unique_constraints"),
        ("reservations", "0031_guestportalaccess"),
        ("tenants", "0016_tenant_is_system"),
    ]

    operations = [
        migrations.CreateModel(
            name="MessageDispatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "definition_key",
                    models.CharField(
                        db_index=True,
                        help_text=(
                            "MessageDefinition key, e.g. CHECKIN_INFO, "
                            "CHECKIN_LINK, WELCOME."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "plan_key",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "ReminderPlan / plan source key that "
                            "materialized this dispatch."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "trigger",
                    models.CharField(
                        choices=[
                            ("TIME", "Time"),
                            ("CRON", "Cron"),
                            ("MANUAL", "Manual"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "correlation_id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        help_text=(
                            "End-to-end correlation across "
                            "materialization and attempts."
                        ),
                    ),
                ),
                (
                    "replay_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("MANUAL", "Manual"),
                            ("PROVIDER_OUTAGE", "Provider outage"),
                            ("BUGFIX", "Bugfix"),
                            ("SUPPORT", "Support"),
                        ],
                        default="",
                        help_text=(
                            "Required on MANUAL replay; optional for "
                            "other lineage creates."
                        ),
                        max_length=32,
                    ),
                ),
                (
                    "due_at",
                    models.DateTimeField(
                        db_index=True,
                        help_text="UTC due timestamp frozen at create.",
                    ),
                ),
                (
                    "timezone",
                    models.CharField(
                        help_text=(
                            "IANA timezone frozen at create "
                            "(e.g. Europe/Zagreb)."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "local_due_at",
                    models.DateTimeField(
                        help_text=(
                            "Local wall-clock due time frozen at create; "
                            "interpret with timezone. Not recomputed if "
                            "property timezone changes later."
                        ),
                    ),
                ),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "schedule_strategy",
                    models.CharField(
                        choices=[
                            ("FIXED_TIME", "Fixed time"),
                            ("FIRST_AFTER", "First after"),
                            ("IMMEDIATE", "Immediate"),
                        ],
                        default="FIXED_TIME",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("planned", "Planned"),
                            ("queued", "Queued"),
                            ("dispatching", "Dispatching"),
                            ("delivered", "Delivered"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="planned",
                        max_length=16,
                    ),
                ),
                (
                    "archived_at",
                    models.DateTimeField(
                        blank=True,
                        help_text=(
                            "Soft-archive only; dispatches are never "
                            "hard-deleted."
                        ),
                        null=True,
                    ),
                ),
                (
                    "policy_version",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "Frozen channel-policy version when bound "
                            "(hash/semver + definition)."
                        ),
                        max_length=128,
                    ),
                ),
                ("rendered_body", models.TextField(blank=True, default="")),
                ("rendered_subject", models.TextField(blank=True, default="")),
                ("language", models.CharField(blank=True, default="", max_length=8)),
                (
                    "template_version",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("render_context", models.JSONField(blank=True, default=dict)),
                (
                    "render_checksum",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "SHA-256 hex of normalized rendered body "
                            "(+ subject)."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "recipient_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("booker", "Booker"),
                            ("guest", "Guest"),
                            ("custom", "Custom"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                (
                    "recipient_email",
                    models.EmailField(blank=True, default="", max_length=254),
                ),
                (
                    "recipient_phone",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "recipient_booking_thread_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "Booking.com / channel messaging thread id "
                            "when applicable."
                        ),
                        max_length=128,
                    ),
                ),
                ("fallback_used", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "parent_dispatch",
                    models.ForeignKey(
                        blank=True,
                        help_text="Lineage for replay / resend / follow-up.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="child_dispatches",
                        to="communications.messagedispatch",
                    ),
                ),
                (
                    "reservation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="message_dispatches",
                        to="reservations.reservation",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Message dispatch",
                "verbose_name_plural": "Message dispatches",
                "ordering": ["due_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="MessageDeliveryAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        help_text=(
                            "Provider channel, e.g. booking, email, whatsapp."
                        ),
                        max_length=16,
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "Registered provider name from ProviderRegistry."
                        ),
                        max_length=64,
                    ),
                ),
                ("attempt_number", models.PositiveSmallIntegerField(default=1)),
                ("success", models.BooleanField(default=False)),
                (
                    "duration_ms",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text=(
                            "Wall-clock duration; dispatcher may fill if "
                            "provider omits."
                        ),
                        null=True,
                    ),
                ),
                (
                    "error_category",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("NETWORK", "Network"),
                            ("AUTH", "Auth"),
                            ("VALIDATION", "Validation"),
                            ("RATE_LIMIT", "Rate limit"),
                            ("PROVIDER", "Provider"),
                            ("UNKNOWN", "Unknown"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                ("retryable", models.BooleanField(default=False)),
                (
                    "error_code",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                (
                    "provider_message_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Provider message id, e.g. Meta wamid.",
                        max_length=128,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dispatch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attempts",
                        to="communications.messagedispatch",
                    ),
                ),
                (
                    "outbound_message",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            "Timeline-compatible GuestOutboundMessage "
                            "written for this attempt."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="messaging_delivery_attempts",
                        to="communications.guestoutboundmessage",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Message delivery attempt",
                "verbose_name_plural": "Message delivery attempts",
                "ordering": ["dispatch_id", "attempt_number", "id"],
            },
        ),
        migrations.CreateModel(
            name="MessageDispatchEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("DISPATCH_CREATED", "Dispatch created"),
                            ("RENDERED", "Rendered"),
                            ("CHANNEL_SELECTED", "Channel selected"),
                            ("FALLBACK", "Fallback"),
                            ("DELIVERED", "Delivered"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                            ("SKIPPED", "Skipped"),
                            ("REPLAYED", "Replayed"),
                        ],
                        max_length=32,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "attempt",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="communications.messagedeliveryattempt",
                    ),
                ),
                (
                    "dispatch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="communications.messagedispatch",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Message dispatch event",
                "verbose_name_plural": "Message dispatch events",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="messagedispatch",
            index=models.Index(
                fields=["tenant", "status", "due_at"],
                name="msg_dispatch_claim_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedispatch",
            index=models.Index(
                fields=["tenant", "reservation", "definition_key"],
                name="msg_dispatch_dedupe_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedispatch",
            index=models.Index(
                fields=["tenant", "definition_key", "status"],
                name="msg_dispatch_def_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedeliveryattempt",
            index=models.Index(
                fields=["tenant", "dispatch", "attempt_number"],
                name="msg_attempt_dispatch_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedeliveryattempt",
            index=models.Index(
                fields=["provider", "provider_message_id"],
                name="msg_attempt_provider_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedispatchevent",
            index=models.Index(
                fields=["tenant", "dispatch", "created_at"],
                name="msg_event_dispatch_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messagedispatchevent",
            index=models.Index(
                fields=["tenant", "event_type", "created_at"],
                name="msg_event_type_idx",
            ),
        ),
    ]
