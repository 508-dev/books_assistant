frappe.provide("books_assistant");

// The client hasn't paid for some of these bills yet, so paying them means 508 fronts the money.
function fronting(warnings) {
	if (!warnings || !warnings.length) return "";
	const items = warnings.map((w) => `<li>${frappe.utils.escape_html(w)}</li>`).join("");
	return `<div class="alert alert-warning" style="font-size: 12px">
		<b>${__("The client hasn't paid for some of these yet. Paying now means 508 fronts the money.")}</b>
		<ul style="margin: 6px 0 0; padding-left: 18px">${items}</ul>
		<div style="margin-top: 6px">${__("If that's not intended, close this and use Pay selected bills in the Purchase Invoice list with only the bills to pay.")}</div>
	</div>`;
}
books_assistant.fronting_html = fronting;

// Shows what a supplier is owed, with any earlier credit, then applies the credit and opens a draft payment.
books_assistant.pay_supplier = function (supplier, invoices) {
	const selection = invoices === undefined ? { all_eligible: true } : { invoices };
	return frappe
		.call({ method: "books_assistant.pay_supplier.preview", type: "GET", args: { supplier, ...selection } })
		.then(({ message: p }) => {
			const esc = frappe.utils.escape_html;
			const money = (v) => format_currency(v, p.currency);
			const link = (doctype, name) =>
				`<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}">${esc(name)}</a>`;
			const held = p.held.length
				? `<p class="text-muted">${__("On hold, not included (paid once released):")}</p>
					<ul style="font-size: 12px">${p.held
						.map(
							(b) =>
								`<li>${link("Purchase Invoice", b.name)}, ${money(b.outstanding_amount)}: ${
									b.client_invoice
										? __("waiting for the client to pay {0}", [link("Sales Invoice", b.client_invoice)])
										: esc(b.hold_comment || __("on hold"))
								}</li>`
						)
						.join("")}</ul>`
				: "";
			if (!p.bills.length) {
				frappe.msgprint(__("Nothing is owed to {0} that can be paid now.", [supplier]) + held);
				return;
			}
			const bills = p.bills
				.map(
					(b) =>
						`<tr><td>${link("Purchase Invoice", b.name)}</td>
						<td>${esc(b.posting_date)}</td><td>${esc(b.project || "")}</td>
						<td class="text-right">${money(b.outstanding_amount)}</td></tr>`
				)
				.join("");
			const credit = p.credit_total
				? `<p>${__("{0} has {1} of earlier credit (overpayments or debit notes) not yet applied; {2} of it will be applied to these bills first.", [
						esc(supplier),
						money(p.credit_total),
						money(p.credit_to_apply),
				  ])}</p>`
				: "";
			let pending = null;
			let in_flight = false;
			let reviewing = false;
			const primary_label = p.to_pay > 0 ? __("Prepare payment") : __("Apply credit");
			const dialog = new frappe.ui.Dialog({
				title: __("Pay {0}", [supplier]),
				size: "large",
				fields: [
					{
						fieldtype: "HTML",
						options: `<table class="table table-sm" style="font-size: 12px">
							<thead><tr><th>${__("Bill")}</th><th>${__("Date")}</th><th>${__("Project")}</th>
							<th class="text-right">${__("Owed")}</th></tr></thead><tbody>${bills}</tbody>
							<tfoot><tr><th colspan="3">${__("Total owed")}</th><th class="text-right">${money(p.bills_total)}</th></tr></tfoot>
						</table>${credit}${held}${fronting(p.fronting)}
						<h4>${__("To pay: {0}", [money(p.to_pay)])}</h4>`,
					},
					{
						fieldname: "bank_account",
						label: __("Pay from"),
						fieldtype: "Link",
						options: "Account",
						default: p.bank_account,
						reqd: p.to_pay > 0,
						hidden: p.to_pay <= 0,
						get_query: () => ({ filters: { account_type: "Bank", is_group: 0 } }),
					},
				],
				primary_action_label: primary_label,
				primary_action: prepare_preview,
			});

			function send_pending() {
				if (!pending || in_flight) return;
				in_flight = true;
				return frappe.call({
					method: "books_assistant.pay_supplier.prepare",
					args: { preview_id: pending.preview_id, request_id: pending.request_id }, freeze: true,
				}).then(({ message }) => {
					in_flight = false;
					if (message?.status !== "prepared" || typeof message.operation_id !== "string") {
						frappe.msgprint(__("The result is uncertain. Use Retry result to retrieve the original operation before starting another payment."));
						return;
					}
					pending = null;
					dialog.hide();
					if (message.payment_entry) {
						const payment = frappe.model.sync(message.payment_entry)[0];
						frappe.set_route("Form", payment.doctype, payment.name);
						frappe.show_alert({ message: __("Check the payment, then save and submit."), indicator: "blue" });
					} else {
						frappe.show_alert({ message: __("Applied {0} of credit; nothing left to pay.", [money(message.credit_applied)]), indicator: "green" });
					}
				}, (error) => {
					in_flight = false;
					const code = error?.responseJSON?.error_code;
					if (["PREVIEW_EXPIRED", "PREVIEW_STALE"].includes(code)) {
						// The server checked the request ledger first and confirmed no execution.
						pending = null;
						dialog.set_primary_action(primary_label, prepare_preview);
					} else {
						// A timeout can follow a committed credit allocation. Preserve both IDs
						// until the original operation result has been retrieved.
						frappe.msgprint(__("The result is uncertain. Use Retry result to retrieve the original operation before starting another payment."));
					}
				});
			}

			function prepare_preview(values) {
				if (pending) return send_pending();
				if (reviewing) return;
				reviewing = true;
				return frappe.call({
					method: "books_assistant.pay_supplier.preview_preparation",
					args: { supplier, ...selection, company: p.company, bank_account: values.bank_account },
					freeze: true,
				}).then(({ message: frozen }) => {
					const reviewed = frozen.snapshot;
					const documents = reviewed.bills.map((b) => esc(b.name)).join(", ");
					const consequence = reviewed.to_pay > 0
						? __("Apply {0} of credit to {1}, then prepare an unsaved payment for {2} from {3}? This records credit allocations; no bank transfer is made.", [
							money(reviewed.credit_to_apply), documents, money(reviewed.to_pay), esc(reviewed.bank_account),
						])
						: __("Apply {0} of credit to {1}? No payment document or bank transfer will be created.", [money(reviewed.credit_to_apply), documents]);
					frappe.confirm(consequence, () => {
						reviewing = false;
						pending = { preview_id: frozen.preview_id, request_id: crypto.randomUUID() };
						dialog.set_primary_action(__("Retry result"), send_pending);
						return send_pending();
					}, () => { reviewing = false; });
				}, () => { reviewing = false; });
			}

			dialog.show();
		});
};
