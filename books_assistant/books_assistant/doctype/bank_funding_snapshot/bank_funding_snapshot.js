frappe.ui.form.on("Bank Funding Snapshot", {
	refresh(frm) {
		frm.set_query("bank_account", () => ({filters: {is_company_account: 1}}));
		frm.set_query("reservation", "reflected_reservations", () => ({filters: {
			bank_account: frm.doc.bank_account, status: ["in", ["Active", "Invalid"]],
		}}));
		frm.set_intro(__("Read the available balance in your bank portal. Include only commitments not already deducted by the bank or reserved here. Mark a reservation reflected only after its transfer is deducted in this balance."), "orange");
	},
});
