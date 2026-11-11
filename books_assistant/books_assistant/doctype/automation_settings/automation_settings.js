// The top of the page shows what the automations are doing and what needs a person; the switches below turn
// each automation off without a deploy.
frappe.ui.form.on("Automation Settings", {
	refresh(frm) {
		const s = frm.doc.__onload && frm.doc.__onload.status;
		if (!s) return;
		const esc = frappe.utils.escape_html;
		const money = (v) => format_currency(v, "USD");
		const link = (doctype, name) =>
			`<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}">${esc(name)}</a>`;
		const list = (route, filters) => `/app/${route}?${new URLSearchParams(filters).toString()}`;
		const item = (ok, text) =>
			`<li style="margin-bottom: 6px"><span class="indicator-pill ${ok ? "green" : "orange"}" style="margin-right: 6px">${
				ok ? __("OK") : __("Check")
			}</span>${text}</li>`;

		const rows = [
			item(
				!s.bank_lines,
				__("{0} bank line(s) waiting in <a href='/app/bank-review'>Bank Review</a> ({1} bank rule(s) on).", [
					s.bank_lines,
					s.bank_rules,
				])
			),
			item(
				true,
				s.held_bills
					? __("{0} engineer/referral bill(s) on hold ({1}), waiting for {2} client invoice(s) to be paid. <a href='{3}'>See them</a>.", [
							s.held_bills,
							money(s.held_amount),
							s.held_waiting_on,
							list("purchase-invoice", { on_hold: 1, docstatus: 1 }),
					  ])
					: __("No bills on hold.")
			),
			item(
				!s.unsent_invoices.length,
				s.unsent_invoices.length
					? __("{0} invoice(s) from the last 30 days were never emailed: {1}", [
							s.unsent_invoices.length,
							s.unsent_invoices.slice(0, 10).map((n) => link("Sales Invoice", n)).join(", ") +
								(s.unsent_invoices.length > 10 ? "…" : ""),
					  ])
					: __("Every invoice from the last 30 days was emailed.")
			),
			item(
				!s.failed_emails.length,
				s.failed_emails.length
					? __("{0} invoice email(s) failed or bounced: {1}", [
							s.failed_emails.length,
							s.failed_emails
								.map((f) => `${link("Sales Invoice", f.reference_name)} → ${esc(f.recipients)} (${esc(f.delivery_status)})`)
								.join("; "),
					  ])
					: __("No bounced or failed invoice emails in the last 30 days.")
			),
			item(
				s.digest_on,
				s.digest_on
					? s.last_digest
						? __("Weekly books health email last queued {0} ({1}).", [
								frappe.datetime.comment_when(s.last_digest.creation),
								esc(s.last_digest.status),
						  ])
						: __("Weekly books health email is on; none sent yet.")
					: __("Weekly books health email is off (no recipients below).")
			),
		];
		frm.get_field("status_html").$wrapper.html(`<ul class="list-unstyled" style="font-size: 13px">${rows.join("")}</ul>`);
	},
});
