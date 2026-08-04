# Invoice guest portal

Public invoice portal is the **canonical guest entry point**. The PDF endpoint is a document representation used by the portal, not a public entry point intended for outbound messaging.

## Guest flow

1. Guest receives email / message with a single `invoice_url`
2. Opens the portal (`GET /api/v1/public/invoices/{token}/`)
3. Views the fiscal invoice in-browser
4. Optionally downloads PDF (`GET …/pdf/`) or prints (`window.print`)

## Ownership

| Layer | Responsibility |
|-------|----------------|
| Guest portal | UX wrapper (summary, Download, Print, 404) |
| `render_invoice_html()` / `billing/invoice.html` | Legal invoice layout (single source for HTML and PDF) |
| Checkout / fiscalize | Unchanged — issue and fiscalize remain separate |

Outbound templates must pass only `invoice_url`. Do not put absolute PDF URLs in email or WhatsApp messages.

## Code

| Path | Role |
|------|------|
| `apps/api/billing_views.py` — `PublicInvoiceHtmlView` | Portal + friendly 404 |
| `apps/api/billing_views.py` — `PublicInvoicePdfView` | PDF download (portal action) |
| `billing/invoice_guest_portal.html` | Portal chrome |
| `billing/invoice.html` | Legal document (PDF + portal body) |
| `communications/invoice_email.py` | Builds portal URL only |

Vanity URL (`stay.hr/invoices/{token}`) is deferred; token stays the same.
