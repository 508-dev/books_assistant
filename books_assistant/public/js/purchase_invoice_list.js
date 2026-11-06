// Adds "Pay selected bills" to the Purchase Invoice list, keeping ERPNext's own list settings.
(() => {
	const settings = (frappe.listview_settings["Purchase Invoice"] = frappe.listview_settings["Purchase Invoice"] || {});
	settings.add_fields = Array.from(new Set([...(settings.add_fields || []), "supplier"]));
	const previous_onload = settings.onload;
	settings.onload = function (listview) {
		if (previous_onload) previous_onload.apply(this, arguments);
		if (!frappe.user.has_role(["Accounts Manager", "System Manager"])) return;
		listview.page.add_actions_menu_item(
			__("Pay selected bills"),
			() => {
				const bills = listview.get_checked_items();
				const suppliers = [...new Set(bills.map((b) => b.supplier))];
				if (suppliers.length !== 1) {
					frappe.msgprint(__("Select bills for one supplier at a time."));
					return;
				}
				books_assistant.pay_supplier(
					suppliers[0],
					bills.map((b) => b.name)
				);
			},
			false
		);
	};
})();
