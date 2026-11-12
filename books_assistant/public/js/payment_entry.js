// On a draft payment to a supplier, say before any money is sent if the client hasn't paid for the bills yet.
// (erp_rules says the same when the payment is submitted, which is after the bank transfer.)
frappe.provide("books_assistant");

books_assistant.show_fronting = frappe.utils.debounce(function (frm) {
	const refs = (frm.doc.references || []).filter((r) => r.reference_doctype === "Purchase Invoice" && r.reference_name);
	if (frm.doc.docstatus !== 0 || frm.doc.payment_type !== "Pay" || !refs.length) {
		frm.set_intro("");
		return;
	}
	frappe
		.call({
			method: "books_assistant.pay_supplier.payment_warnings",
			type: "GET",
			args: { references: refs.map((r) => ({ reference_doctype: r.reference_doctype, reference_name: r.reference_name })) },
		})
		.then(({ message }) => {
			frm.set_intro("");
			if (message && message.length) {
				frm.set_intro(
					`<b>${__("Before you send this money:")}</b><br>` +
						message.map((m) => frappe.utils.escape_html(m)).join("<br><br>"),
					"orange"
				);
			}
		});
}, 300);

// ERPNext fills Company Bank Account with the company default (Chase) even when the money moves through
// another bank. Keep it in line with the bank account the payment actually uses.
books_assistant.sync_company_bank = function (frm) {
	if (frm.doc.docstatus !== 0) return;
	const gl = frm.doc.payment_type === "Receive" ? frm.doc.paid_to : frm.doc.paid_from;
	if (!gl) return;
	frappe.db.get_value("Bank Account", { account: gl, is_company_account: 1 }, "name").then(({ message }) => {
		const name = message && message.name;
		if (name && name !== frm.doc.bank_account) frm.set_value("bank_account", name);
	});
};

frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		books_assistant.show_fronting(frm);
		if (frm.is_new()) books_assistant.sync_company_bank(frm);
	},
	paid_from: (frm) => books_assistant.sync_company_bank(frm),
	paid_to: (frm) => books_assistant.sync_company_bank(frm),
	references_remove: (frm) => books_assistant.show_fronting(frm),
});

frappe.ui.form.on("Payment Entry Reference", {
	reference_name: (frm) => books_assistant.show_fronting(frm),
});
