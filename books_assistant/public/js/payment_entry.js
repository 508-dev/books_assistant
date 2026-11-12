frappe.provide("books_assistant");

books_assistant.show_fronting = frappe.utils.debounce(async function (frm) {
	const request = (frm._funding_request || 0) + 1;
	frm._funding_request = request;
	const refs = (frm.doc.references || []).filter((r) => r.reference_doctype === "Purchase Invoice" && r.reference_name);
	if (frm.doc.docstatus !== 0 || frm.doc.payment_type !== "Pay" || frm.doc.party_type !== "Supplier" || !refs.length) {
		frm.set_intro("");
		return;
	}
	if (!frappe.user_roles.some((r) => ["Accounts Manager", "System Manager"].includes(r))) {
		frm.set_intro(__("An Accounts Manager must review payout funding before money is sent."), "orange");
		return;
	}
	frm.set_intro(__("Funding unverified: checking…"), "orange");
	try {
		let report;
		if (!frm.is_new() && !frm.is_dirty()) {
			({ message: report } = await frappe.call({method: "books_assistant.funding.check_payment", args: {payment_entry: frm.doc.name}}));
		} else {
			const { message: bank } = await frappe.db.get_value("Bank Account", {account: frm.doc.paid_from, is_company_account: 1}, "name");
			if (!bank || !bank.name) throw new Error("Missing bank account");
			({ message: report } = await frappe.call({method: "books_assistant.funding.check", args: {
				bank_account: bank.name, amount: frm.doc.paid_amount || 0, supplier: frm.doc.party,
				invoices: refs.map((r) => r.reference_name),
			}}));
		}
		if (request === frm._funding_request) frm.set_intro(books_assistant.funding_html(report), "orange");
	} catch (error) {
		if (request === frm._funding_request) frm.set_intro(__("Funding unverified: unable to complete the check. Review funding before sending money."), "orange");
	}
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
		if (frm.doc.docstatus === 0 && frm.doc.payment_type === "Pay" && frm.doc.party_type === "Supplier"
			&& frappe.user_roles.some((r) => ["Accounts Manager", "System Manager"].includes(r))) {
			frm.add_custom_button(__("Review payout funding"), () => books_assistant.review_funding(frm));
			frm.add_custom_button(__("Record bank balance"), () => frappe.new_doc("Bank Funding Snapshot"));
		}
	},
	paid_from(frm) { books_assistant.sync_company_bank(frm); books_assistant.show_fronting(frm); },
	paid_to(frm) { books_assistant.sync_company_bank(frm); books_assistant.show_fronting(frm); },
	paid_amount: (frm) => books_assistant.show_fronting(frm),
	party: (frm) => books_assistant.show_fronting(frm),
	party_bank_account: (frm) => books_assistant.show_fronting(frm),
	company: (frm) => books_assistant.show_fronting(frm),
	payment_type: (frm) => books_assistant.show_fronting(frm),
	references_remove: (frm) => books_assistant.show_fronting(frm),
});

frappe.ui.form.on("Payment Entry Reference", {
	reference_name: (frm) => books_assistant.show_fronting(frm),
	allocated_amount: (frm) => books_assistant.show_fronting(frm),
});
