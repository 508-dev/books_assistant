frappe.ui.form.on("Supplier", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role(["Accounts Manager", "System Manager"])) return;
		frm.add_custom_button(__("Pay what we owe"), () => books_assistant.pay_supplier(frm.doc.name), __("Create"));
	},
});
