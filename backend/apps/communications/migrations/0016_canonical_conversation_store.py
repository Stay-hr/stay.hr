import django.db.models.deletion
from django.db import migrations, models

import apps.communications.models


TENANT_MATCH_TRIGGERS = """
CREATE OR REPLACE FUNCTION communications_conversation_tenant_matches_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM reservations_reservation AS r
    WHERE r.id = NEW.reservation_id
      AND r.tenant_id = NEW.tenant_id
  ) THEN
    RAISE EXCEPTION 'conversation tenant_id must equal reservation.tenant_id'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER communications_conversation_tenant_matches_reservation_trg
BEFORE INSERT OR UPDATE OF tenant_id, reservation_id
ON communications_conversation
FOR EACH ROW
EXECUTE FUNCTION communications_conversation_tenant_matches_reservation();

CREATE OR REPLACE FUNCTION communications_guestmessage_tenant_matches_conversation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM communications_conversation AS c
    WHERE c.id = NEW.conversation_id
      AND c.tenant_id = NEW.tenant_id
  ) THEN
    RAISE EXCEPTION 'guestmessage tenant_id must equal conversation.tenant_id'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER communications_guestmessage_tenant_matches_conversation_trg
BEFORE INSERT OR UPDATE OF tenant_id, conversation_id
ON communications_guestmessage
FOR EACH ROW
EXECUTE FUNCTION communications_guestmessage_tenant_matches_conversation();

CREATE OR REPLACE FUNCTION communications_guestmessagesource_tenant_matches_message()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM communications_guestmessage AS m
    INNER JOIN communications_conversation AS c ON c.id = m.conversation_id
    WHERE m.id = NEW.message_id
      AND m.tenant_id = NEW.tenant_id
      AND c.tenant_id = NEW.tenant_id
  ) THEN
    RAISE EXCEPTION 'guestmessagesource tenant_id must equal guestmessage → conversation tenant'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER communications_guestmessagesource_tenant_matches_message_trg
BEFORE INSERT OR UPDATE OF tenant_id, message_id
ON communications_guestmessagesource
FOR EACH ROW
EXECUTE FUNCTION communications_guestmessagesource_tenant_matches_message();
"""

DROP_TENANT_TRIGGERS = """
DROP TRIGGER IF EXISTS communications_guestmessagesource_tenant_matches_message_trg
  ON communications_guestmessagesource;
DROP FUNCTION IF EXISTS communications_guestmessagesource_tenant_matches_message();
DROP TRIGGER IF EXISTS communications_guestmessage_tenant_matches_conversation_trg
  ON communications_guestmessage;
DROP FUNCTION IF EXISTS communications_guestmessage_tenant_matches_conversation();
DROP TRIGGER IF EXISTS communications_conversation_tenant_matches_reservation_trg
  ON communications_conversation;
DROP FUNCTION IF EXISTS communications_conversation_tenant_matches_reservation();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0015_postcheckinsendclaim"),
        ("integrations", "0027_whatsappmessage_source_received_at"),
        ("reservations", "0036_guestcheckinsession_last_distributed_from"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="Conversation",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reservation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation",
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
                "verbose_name": "Conversation",
                "verbose_name_plural": "Conversations",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "reservation"),
                        name="communications_conversation_tenant_reservation_uniq",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="GuestMessage",
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
                    "direction",
                    models.CharField(
                        choices=[
                            ("inbound", "Inbound"),
                            ("outbound", "Outbound"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("email", "Email"),
                            ("whatsapp", "WhatsApp"),
                            ("booking", "Booking.com"),
                        ],
                        max_length=16,
                    ),
                ),
                ("body", models.TextField(blank=True, default="")),
                (
                    "media_file",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to=apps.communications.models.guest_message_media_upload_to,
                    ),
                ),
                ("occurred_at", models.DateTimeField()),
                (
                    "delivery_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("sent", "Sent"),
                            ("delivered", "Delivered"),
                            ("read", "Read"),
                            ("failed", "Failed"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                ("is_visible", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="communications.conversation",
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
                "verbose_name": "Guest message",
                "verbose_name_plural": "Guest messages",
                "ordering": ["occurred_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="GuestMessageSource",
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
                    "provider",
                    models.CharField(
                        choices=[
                            ("channex", "Channex"),
                            ("waba", "WhatsApp Cloud API"),
                            ("imap", "IMAP"),
                            ("smtp", "SMTP"),
                            ("stay_outbound", "Stay outbound"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "provider_message_id",
                    models.CharField(
                        blank=True,
                        help_text="Stable provider id when supplied. NULL if omitted; never empty string.",
                        max_length=255,
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "channex_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="guest_message_sources",
                        to="integrations.channexmessage",
                    ),
                ),
                (
                    "inbound_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="guest_message_sources",
                        to="communications.guestinboundmessage",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sources",
                        to="communications.guestmessage",
                    ),
                ),
                (
                    "outbound_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="guest_message_sources",
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
                (
                    "whatsapp_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="guest_message_sources",
                        to="integrations.whatsappmessage",
                    ),
                ),
            ],
            options={
                "verbose_name": "Guest message source",
                "verbose_name_plural": "Guest message sources",
            },
        ),
        migrations.AddIndex(
            model_name="guestmessage",
            index=models.Index(
                fields=["conversation", "occurred_at"],
                name="comm_gm_occurred_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="guestmessage",
            index=models.Index(
                fields=["conversation", "is_visible"],
                name="comm_gm_visible_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="guestmessagesource",
            index=models.Index(
                fields=["tenant", "provider", "provider_message_id"],
                name="comm_gms_provider_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("channex_message__isnull", False),
                        ("inbound_message__isnull", True),
                        ("outbound_message__isnull", True),
                        ("whatsapp_message__isnull", True),
                    ),
                    models.Q(
                        ("channex_message__isnull", True),
                        ("inbound_message__isnull", True),
                        ("outbound_message__isnull", True),
                        ("whatsapp_message__isnull", False),
                    ),
                    models.Q(
                        ("channex_message__isnull", True),
                        ("inbound_message__isnull", False),
                        ("outbound_message__isnull", True),
                        ("whatsapp_message__isnull", True),
                    ),
                    models.Q(
                        ("channex_message__isnull", True),
                        ("inbound_message__isnull", True),
                        ("outbound_message__isnull", False),
                        ("whatsapp_message__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="guestmessagesource_exactly_one_raw_fk",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.CheckConstraint(
                condition=models.Q(("provider_message_id", ""), _negated=True),
                name="guestmessagesource_provider_message_id_not_blank",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.UniqueConstraint(
                condition=models.Q(("provider_message_id__isnull", False)),
                fields=("tenant", "provider", "provider_message_id"),
                name="guestmessagesource_unique_provider_message_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.UniqueConstraint(
                condition=models.Q(("channex_message__isnull", False)),
                fields=("channex_message",),
                name="guestmessagesource_unique_channex_message",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.UniqueConstraint(
                condition=models.Q(("whatsapp_message__isnull", False)),
                fields=("whatsapp_message",),
                name="guestmessagesource_unique_whatsapp_message",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.UniqueConstraint(
                condition=models.Q(("inbound_message__isnull", False)),
                fields=("inbound_message",),
                name="guestmessagesource_unique_inbound_message",
            ),
        ),
        migrations.AddConstraint(
            model_name="guestmessagesource",
            constraint=models.UniqueConstraint(
                condition=models.Q(("outbound_message__isnull", False)),
                fields=("outbound_message",),
                name="guestmessagesource_unique_outbound_message",
            ),
        ),
        migrations.RunSQL(TENANT_MATCH_TRIGGERS, DROP_TENANT_TRIGGERS),
    ]
