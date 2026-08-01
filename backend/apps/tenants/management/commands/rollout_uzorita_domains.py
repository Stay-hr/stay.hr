import httpx
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from apps.api.site_context_views import SiteContextView
from apps.properties.models import Property
from apps.tenants.cloudflare.client import CloudflareAPIError
from apps.tenants.cloudflare.dns import provision_platform_dns, provision_tenant_domain_dns
from apps.tenants.middleware import TenantHostMiddleware
from apps.tenants.models import Tenant, TenantDomain


class Command(BaseCommand):
    help = (
        "Seed Uzorita TenantDomain records, provision DNS, verify endpoints, "
        "and mark domains verified when checks pass."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-dns",
            action="store_true",
            help="Skip Cloudflare DNS provisioning.",
        )
        parser.add_argument(
            "--skip-verify",
            action="store_true",
            help="Skip HTTP checks and do not update is_verified.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print actions without writing DNS or database verification flags.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skip_dns = options["skip_dns"]
        skip_verify = options["skip_verify"]

        tenant = Tenant.objects.filter(slug="uzorita").first()
        if tenant is None:
            raise CommandError("Tenant 'uzorita' not found — run migrate_uzorita_legacy first.")

        property_obj = Property.objects.filter(tenant=tenant, slug="uzorita").first()
        if property_obj is None:
            raise CommandError("Property 'uzorita' not found for tenant uzorita.")

        hub_domain, _ = TenantDomain.objects.update_or_create(
            domain="uzorita.stay.hr",
            defaults={
                "tenant": tenant,
                "property": None,
                "domain_type": TenantDomain.DomainType.STAY_SUBDOMAIN,
                "is_primary": False,
            },
        )
        booking_domain, _ = TenantDomain.objects.update_or_create(
            domain="uzorita-sibenik.stay.hr",
            defaults={
                "tenant": tenant,
                "property": property_obj,
                "domain_type": TenantDomain.DomainType.STAY_SUBDOMAIN,
                "is_primary": True,
            },
        )
        legacy_domain, _ = TenantDomain.objects.update_or_create(
            domain="booking.uzorita.hr",
            defaults={
                "tenant": tenant,
                "property": property_obj,
                "domain_type": TenantDomain.DomainType.CUSTOM_DOMAIN,
                "is_primary": False,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "TenantDomain records ready: "
                f"{booking_domain.domain} (primary booking), "
                f"{hub_domain.domain} (hub), {legacy_domain.domain} (legacy custom)",
            )
        )

        if not skip_dns:
            try:
                if dry_run:
                    platform_records = provision_platform_dns(dry_run=True)
                    for line in platform_records:
                        self.stdout.write(f"Would provision platform DNS: {line}")
                    for domain in (hub_domain, booking_domain):
                        message = provision_tenant_domain_dns(domain, dry_run=True)
                        self.stdout.write(f"Would provision tenant DNS: {message}")
                    self.stdout.write(
                        f"Would skip DNS for legacy custom domain: {legacy_domain.domain}",
                    )
                else:
                    platform_records = provision_platform_dns()
                    for line in platform_records:
                        self.stdout.write(f"Platform DNS: {line}")
                    for domain in (hub_domain, booking_domain):
                        message = provision_tenant_domain_dns(domain)
                        self.stdout.write(f"Tenant DNS: {message}")
                    self.stdout.write(
                        f"Skipped DNS for legacy custom domain: {legacy_domain.domain}",
                    )
            except CloudflareAPIError as exc:
                raise CommandError(str(exc)) from exc

        if skip_verify:
            self.stdout.write("Skipping HTTP verification.")
            return

        checks = [
            ("https://uzorita-sibenik.stay.hr/", {}),
            ("https://uzorita.stay.hr/p/uzorita/", {}),
        ]

        all_ok = True
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for url, headers in checks:
                try:
                    response = client.get(url, headers=headers)
                    code = str(response.status_code)
                except httpx.HTTPError as exc:
                    code = f"error: {exc}"
                    all_ok = False
                    self.stdout.write(self.style.ERROR(f"GET {url} -> {code}"))
                    continue

                ok = code == "200"
                all_ok = all_ok and ok
                status = self.style.SUCCESS if ok else self.style.ERROR
                host_note = f" Host={headers['Host']}" if headers else ""
                self.stdout.write(status(f"GET {url}{host_note} -> {code} (expected 200)"))

        if dry_run:
            self.stdout.write("Dry-run: not updating is_verified.")
            return

        if not all_ok:
            raise CommandError(
                "One or more verification checks failed — is_verified left unchanged.",
            )

        updated = TenantDomain.objects.filter(
            pk__in=[hub_domain.pk, booking_domain.pk],
        ).update(is_verified=True)
        self.stdout.write(
            self.style.SUCCESS(f"Marked {updated} TenantDomain record(s) as verified."),
        )

        try:
            request = RequestFactory().get(
                "/api/v1/public/site-context/",
                HTTP_HOST="uzorita-sibenik.stay.hr",
            )
            TenantHostMiddleware(lambda req: req)(request)
            response = SiteContextView.as_view()(request)
            code = str(response.status_code)
            ok = code == "200"
            status = self.style.SUCCESS if ok else self.style.ERROR
            self.stdout.write(
                status(
                    "site-context Host=uzorita-sibenik.stay.hr -> "
                    f"{code} (expected 200)"
                ),
            )
            if not ok:
                raise CommandError("site-context check failed after verification.")
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"site-context check failed: {exc}") from exc
