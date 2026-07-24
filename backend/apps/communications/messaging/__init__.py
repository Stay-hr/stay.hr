"""Messaging Orchestration Engine (ADR 0010).

New automated outbound must go through Trigger → MessageDefinition → Dispatcher.
Direct provider integrations for new flows are not permitted.

Import models from ``apps.communications.messaging.models`` (or
``apps.communications.models``) and intents from
``apps.communications.messaging.intents``.

Core engine modules (import explicitly after Django setup)::

    apps.communications.messaging.triggers
    apps.communications.messaging.context
    apps.communications.messaging.definitions
    apps.communications.messaging.skip_rules
    apps.communications.messaging.results
    apps.communications.messaging.dispatcher
    apps.communications.messaging.scheduler
    apps.communications.messaging.schedule_settings
    apps.communications.messaging.flags
    apps.communications.messaging.validation
    apps.communications.messaging.bootstrap
"""
