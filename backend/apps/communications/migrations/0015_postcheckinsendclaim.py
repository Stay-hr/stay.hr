from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0014_messagedispatchevent_deferred"),
        ("reservations", "0036_guestcheckinsession_last_distributed_from"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="PostCheckinSendClaim",
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
                ("claim_key", models.CharField(max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reservation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="post_checkin_send_claims",
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
                "verbose_name": "Post-checkin send claim",
                "verbose_name_plural": "Post-checkin send claims",
            },
        ),
        migrations.AddConstraint(
            model_name="postcheckinsendclaim",
            constraint=models.UniqueConstraint(
                fields=("claim_key",),
                name="communications_postcheckin_claim_key_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="postcheckinsendclaim",
            index=models.Index(
                fields=["reservation", "status"],
                name="communicati_postchk_rs_idx",
            ),
        ),
    ]
