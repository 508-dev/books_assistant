# Payout funding UI previews

These are Chrome captures with **synthetic sample data**, not screenshots from a running ERPNext site. The warning and approval content comes directly from `books_assistant/public/js/funding.js`, including the disabled approval button. The balance form uses the checked-in Bank Funding Snapshot DocType fields. A lightweight HTML shell supplies layout; actual ERPNext theme, form lifecycle, permissions, and database behavior still need staging verification.

| Preview | What to review |
|---|---|
| [Insufficient cash](funding-shortfall.png) | A $6,000 payout has only $4,000 available after commitments, reservations, and the buffer. Approval is disabled. |
| [Client funding exception](funding-fronting.png) | Cash is sufficient, but the client invoice is unpaid. A reason is required before approving an advance. |
| [Bank evidence](funding-snapshot.png) | A recorded balance, freshness timestamp, pending commitments, buffer, evidence notes, and explicit reflection of reservations. |

To regenerate with Python, Playwright, and an existing Chrome/Chromium installation:

```bash
python tools/capture_funding_screenshots.py
```

The script serves the repository on an ephemeral localhost port, opens `tools/funding_preview.html`, checks that the shortfall blocks approval, captures all three states, and stops the browser/server. It does not contact the ERP or any bank and does not install dependencies.
