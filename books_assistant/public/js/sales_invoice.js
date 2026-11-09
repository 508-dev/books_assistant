// "Email Invoice": ERPNext's email dialog, prefilled with the customer's invoice addresses, the message from
// Automation Settings and the customer's (or default) PDF layout.
frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.is_return) return;
		frm.add_custom_button(__("Email Invoice"), () => books_assistant.email_invoice(frm));
	},
});

frappe.provide("books_assistant");

books_assistant.email_invoice = function (frm) {
	frappe
		.call({ method: "books_assistant.invoice_email.email_defaults", args: { sales_invoice: frm.doc.name } })
		.then(({ message: d }) => {
			if (!d.recipients) {
				frappe.show_alert({
					message: __("{0} has no invoice email address. Add one under Invoice Emails on the customer.", [
						frm.doc.customer,
					]),
					indicator: "orange",
				});
			}
			const composer = new frappe.views.CommunicationComposer({
				doc: frm.doc,
				frm,
				recipients: d.recipients,
				cc: d.cc,
				subject: d.subject,
				message: d.message,
				attach_document_print: true,
			});
			if (d.print_format) {
				const field = composer.dialog.fields_dict.select_print_format;
				if (![...field.input.options].some((o) => o.value === d.print_format)) {
					$(field.input).add_options([d.print_format]);
				}
				$(field.input).val(d.print_format);
				composer.guess_language();
			}
		});
};
