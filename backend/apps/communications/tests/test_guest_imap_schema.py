"""Schema/defaults for the PR-2 fields (migrations are exercised by the test DB build itself)."""

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.communications.models import GuestImapIssue
from apps.tenants.models import Tenant, TenantReceptionSettings


class SchemaTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="schema", name="Schema")

    def test_uidvalidity_defaults_to_null_and_holds_a_32_bit_unsigned_value(self):
        s = TenantReceptionSettings.objects.create(tenant=self.tenant)
        self.assertIsNone(s.guest_imap_uidvalidity)
        s.guest_imap_uidvalidity = 4294967295  # max 32-bit UIDVALIDITY: needs a big integer column
        s.save()
        s.refresh_from_db()
        self.assertEqual(s.guest_imap_uidvalidity, 4294967295)

    def test_existing_rows_keep_their_cursor_and_get_a_null_uidvalidity(self):
        s = TenantReceptionSettings.objects.create(tenant=self.tenant, guest_imap_last_uid=1121)
        s.refresh_from_db()
        self.assertEqual((s.guest_imap_last_uid, s.guest_imap_uidvalidity), (1121, None))

    def test_issue_ledger_is_unique_per_tenant_uidvalidity_and_uid(self):
        GuestImapIssue.objects.create(tenant=self.tenant, uid=5, uidvalidity=1, kind="fetch_failed")
        with self.assertRaises(IntegrityError), transaction.atomic():
            GuestImapIssue.objects.create(tenant=self.tenant, uid=5, uidvalidity=1, kind="parse_failed")
        GuestImapIssue.objects.create(tenant=self.tenant, uid=5, uidvalidity=2, kind="fetch_failed")  # new mailbox generation
        self.assertEqual(GuestImapIssue.objects.count(), 2)

    def test_issue_defaults(self):
        i = GuestImapIssue.objects.create(tenant=self.tenant, uid=1, kind="fetch_failed")
        self.assertEqual((i.status, i.attempts, i.uidvalidity, i.error_type), ("open", 0, 0, ""))
