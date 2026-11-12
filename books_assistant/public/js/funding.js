frappe.provide("books_assistant");

books_assistant.funding_html = function (report) {
	const esc = frappe.utils.escape_html;
	const cash = report.cash;
	const money = (v) => esc(format_currency(v, report.currency));
	const reasons = {
		missing_balance: __("No bank-verified balance has been recorded."),
		stale_balance: __("The bank balance is too old. Check the bank and record a new snapshot."),
		future_balance: __("The bank balance timestamp is in the future."),
		cash_shortfall: __("This payout would leave a funding shortfall."),
	};
	const issues = {
		ineligible_bill: __("A selected bill is held, settled, cancelled, or otherwise ineligible."),
		missing_bills: __("Select the bills this payout covers."),
		unsupported_currency: __("This currency requires manual funding review."),
		unsupported_payment_allocation: __("Review the taxes, deductions, currencies, and bill allocations; they must match the transfer."),
		allocation_exceeds_outstanding: __("A bill allocation exceeds the amount still owed."),
		approval_changed_release_required: __("The approved payment changed. Release its unsent reservation and review funding again."),
	};
	const title = cash.status === "sufficient" && !report.issues.length
		? __("Cash sufficient based on the recorded bank balance")
		: cash.status === "blocked" ? __("Insufficient cash — payout approval blocked") : __("Funding unverified");
	const details = cash.snapshot ? `<div>${__("Bank balance:")} ${money(cash.available_balance)}<br>
		${__("Other commitments:")} ${money(cash.pending_outflows)}<br>
		${__("Reserved payouts:")} ${money(cash.reserved)}<br>
		${__("Minimum buffer:")} ${money(cash.minimum_buffer)}<br>
		${__("Available for this payout:")} ${money(cash.available_for_payout)}<br>
		${__("This payout:")} ${money(cash.amount)}<br>
		${__("After payout and buffer:")} ${money(cash.after_payout)}<br>
		${__("Shortfall:")} ${money(cash.shortfall)}<br>
		${__("Bank checked at:")} ${esc(cash.balance_as_of)}</div>` : "";
	const clients = report.clients.filter((c) => c.status !== "received").map((c) => {
		const invoices = (c.invoices || []).map((i) => `${esc(i.invoice)} (${esc(i.status)}, ${money(i.outstanding)} ${__("outstanding")})`).join(", ");
		return `<li>${esc(c.bill)}: ${invoices || __("No exact client invoice link; client funding unverified.")}</li>`;
	}).join("");
	const approval = report.approval ? `<p>${__("Reservation:")} ${esc(report.approval.name)} (${esc(report.approval.status)})</p>` : "";
	return `<div class="alert alert-${report.can_approve && !report.fronting_required ? "info" : "warning"}">
		<b>${title}</b><p>${esc(reasons[cash.reason] || "")}</p>${details}
		${clients ? `<p><b>${__("Paying now fronts or may front client funds. An approver must record a reason.")}</b></p><ul>${clients}</ul>` : ""}
		${report.issues.length ? `<p>${__("Needs review:")} ${report.issues.map((code) => esc(issues[code] || __("This payment requires manual review."))).join(" ")}</p>` : ""}${approval}
		<p>${__("This check does not send money. Record bank evidence and approve funding before transferring in the bank portal.")}</p>
	</div>`;
};

books_assistant.review_funding = async function (frm) {
	if (frm.is_new() || frm.is_dirty()) {
		frappe.msgprint(__("Save the payment before checking and approving funding."));
		return;
	}
	const { message: report } = await frappe.call({
		method: "books_assistant.funding.check_payment", args: { payment_entry: frm.doc.name },
	});
	const dialog = new frappe.ui.Dialog({
		title: __("Review payout funding"), size: "large",
		fields: [
			{ fieldtype: "HTML", options: books_assistant.funding_html(report) },
			{ fieldname: "fronting_reason", fieldtype: "Small Text", label: __("Reason for paying before verified client receipt"),
				default: report.approval && report.approval.fronting_reason, hidden: !report.fronting_required, reqd: report.fronting_required && !(report.approval && report.approval.fronting_reason) },
		],
		primary_action_label: __("Approve and reserve funds"),
		primary_action: async (values) => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("The payment changed. Save it and review funding again."));
				return;
			}
			const { message } = await frappe.call({
				method: "books_assistant.funding.approve", type: "POST", freeze: true,
				args: { payment_entry: frm.doc.name, review_token: report.review_token, fronting_reason: values.fronting_reason },
			});
			dialog.hide();
			frappe.msgprint(__("Funds reserved under {0}. No money has been sent. Recheck funding immediately before the bank transfer.", [message.reservation]));
			frm.reload_doc();
		},
	});
	dialog.show();
	if (!report.can_approve) dialog.get_primary_btn().prop("disabled", true);
	if (report.approval) {
		dialog.add_custom_action(__("Release unsent reservation"), () => {
			frappe.prompt({fieldname: "reason", fieldtype: "Small Text", label: __("Confirm this payout has not been sent; give the reason for release"), reqd: 1}, async ({ reason }) => {
				await frappe.call({method: "books_assistant.funding.release", type: "POST",
					args: {reservation: report.approval.name, reason}, freeze: true});
				dialog.hide();
				frm.reload_doc();
			});
		});
	}
};
