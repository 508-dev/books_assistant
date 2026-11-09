frappe.provide("books_assistant");

// Shows what a supplier is owed, with any earlier credit, then applies the credit and opens a draft payment.
books_assistant.pay_supplier = function (supplier, invoices) {
	frappe
		.call({ method: "books_assistant.pay_supplier.preview", args: { supplier, invoices: invoices || null } })
		.then(({ message: p }) => {
			const esc = frappe.utils.escape_html;
			const money = (v) => format_currency(v, "USD");
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
						</table>${credit}${held}
						<h4>${__("To pay: {0}", [money(p.to_pay)])}</h4>`,
					},
					{
						fieldname: "bank_account",
						label: __("Pay from"),
						fieldtype: "Link",
						options: "Account",
						default: p.bank_account,
						reqd: 1,
						get_query: () => ({ filters: { account_type: "Bank", is_group: 0 } }),
					},
				],
				primary_action_label: p.to_pay > 0 ? __("Prepare payment") : __("Apply credit"),
				primary_action: (values) =>
					frappe
						.call({
							method: "books_assistant.pay_supplier.prepare",
							args: { supplier, invoices: invoices || null, bank_account: values.bank_account },
							freeze: true,
						})
						.then(({ message }) => {
							dialog.hide();
							if (message.payment_entry) {
								const payment = frappe.model.sync(message.payment_entry)[0];
								frappe.set_route("Form", payment.doctype, payment.name);
								frappe.show_alert({
									message: __("Check the payment, then save and submit."),
									indicator: "blue",
								});
							} else {
								frappe.show_alert({
									message: __("Applied {0} of credit; nothing left to pay.", [money(message.credit_applied)]),
									indicator: "green",
								});
							}
						}),
			});
			dialog.show();
		});
};
