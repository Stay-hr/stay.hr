"""v1 live message definition keys / ReminderPlan intents (ADR 0010)."""

from __future__ import annotations

from django.db import models


class MessageDefinitionKey(models.TextChoices):
    """Canonical definition keys for MessageDispatch.definition_key."""

    CHECKIN_INFO = "CHECKIN_INFO", "Check-in info"
    CHECKIN_LINK = "CHECKIN_LINK", "Check-in link"
    WELCOME = "WELCOME", "Welcome"


PRE_ARRIVAL_INTENTS: list[str] = [
    MessageDefinitionKey.CHECKIN_INFO,
    MessageDefinitionKey.CHECKIN_LINK,
]

WELCOME_INTENTS: list[str] = [
    MessageDefinitionKey.WELCOME,
]
