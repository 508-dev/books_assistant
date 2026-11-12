frappe.ui.form.on("Billing Run", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.period_start) {
			// Last month by default: runs happen early in the month for the month before.
			const first = moment(frappe.datetime.get_today()).subtract(1, "month").startOf("month");
			frm.set_value("period_start", first.format("YYYY-MM-DD"));
			frm.set_value("period_end", first.clone().endOf("month").format("YYYY-MM-DD"));
		}
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const roles = frappe.user_roles;
		const can_approve = roles.includes("Billing Approver") || roles.includes("System Manager");
		const pending = (frm.doc.items || []).filter((r) => ["pending", "failed"].includes(r.item_status));
		const review_required = (frm.doc.items || []).filter((r) => r.item_status === "review_required");

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Prepare Drafts"), () => call(frm, "prepare", __("Preparing drafts…"))).addClass(
				"btn-primary"
			);
			frm.dashboard.set_headline(
				__("Prepare drafts the client invoices and engineer bills for timesheets ending in this period.")
			);
		}
		if (frm.doc.status === "Prepared") {
			if (can_approve && pending.length) {
				frm.add_custom_button(__("Approve and Send"), () => approve(frm, pending)).addClass("btn-primary");
			}
			if (!review_required.length && !(frm.doc.items || []).some((r) => r.item_status === "approved")) {
				frm.add_custom_button(__("Discard Drafts"), () =>
					frappe.confirm(
						__("Delete this run's draft invoices and bills, so it can be prepared again?"),
						() => call(frm, "discard_drafts", __("Discarding…"))
					)
				);
			}
			const issues = pending.filter((r) => r.issues).length;
			const failed = pending.filter((r) => r.item_status === "failed").length;
			frm.dashboard.set_headline(
				[
					__("{0} project(s) waiting for approval.", [pending.length]),
					issues ? __("{0} have something to check, listed above the table.", [issues]) : "",
					failed ? __("{0} failed to approve; see Outcome.", [failed]) : "",
					review_required.length ? __("{0} project(s) require System Manager review before further actions.", [review_required.length]) : "",
				].join(" "),
				issues || failed || review_required.length ? "orange" : "blue"
			);
		}
		if (frm.doc.status === "Approved") {
			frm.dashboard.set_headline(
				__("Done. Engineer bills are on hold until each client pays, then released automatically."),
				"green"
			);
		}
		frm.set_df_property("items", "cannot_add_rows", true);
		render_issues(frm);
	},
});

frappe.ui.form.on("Billing Run Item", {
	form_render(frm, cdt, cdn) {
		frm.fields_dict.items.grid.grid_rows_by_docname[cdn].toggle_editable("issues", false);
	},
});

function call(frm, method, freeze_message) {
	frm.call({ doc: frm.doc, method, freeze: true, freeze_message }).then(() => frm.reload_doc());
}

// Each project's issues and outcome in full, above the table.
function render_issues(frm) {
	const esc = frappe.utils.escape_html;
	const rows = (frm.doc.items || []).filter((r) => r.issues || r.outcome || r.item_status === "review_required");
	const html = rows
		.map((r) => {
			const failed = r.item_status === "failed";
			const repair = r.item_status === "review_required" ? `<div class="text-warning">${esc(__("Requires System Manager review; excluded from approval and leave-out selections."))}</div>` : "";
			const outcome = r.outcome
				? `<div class="${failed ? "text-danger" : "text-success"}">${esc(r.outcome)}</div>`
				: "";
			const issues = (r.issues || "")
				.split("\n")
				.filter(Boolean)
				.map((i) => `<li>${esc(i)}</li>`)
				.join("");
			return `<div style="margin-bottom: 10px"><b>${esc(r.project)}</b> · ${esc(r.customer || __("internal"))}
				${repair}${outcome}${issues ? `<ul style="margin: 4px 0 0; padding-left: 18px">${issues}</ul>` : ""}</div>`;
		})
		.join("");
	frm.get_field("issues_html").$wrapper.html(
		html ? `<div class="small" style="margin-bottom: 12px">${html}</div>` : ""
	);
}

function approve(frm, pending) {
	const invoices = pending.filter((r) => r.sales_invoice).length;
	const bills = pending.reduce((n, r) => n + (r.engineer_bills || "").split(/\s+/).filter(Boolean).length, 0);
	const with_issues = pending.filter((r) => r.issues);
	const dialog = new frappe.ui.Dialog({
		title: __("Approve and Send"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"This submits {0} client invoice(s), emailing each to the customer, and {1} engineer bill(s) plus any referral fees, held until the client pays.",
					[invoices, bills]
				)}</p>${
					with_issues.length
						? `<p class="text-warning">${__("{0} project(s) have issues listed. Untick any you want to leave for now.", [
								with_issues.length,
						  ])}</p>`
						: ""
				}`,
			},
			{
				fieldname: "projects",
				fieldtype: "MultiCheck",
				label: __("Projects"),
				options: pending.map((r) => ({
					label: `${r.project} · ${r.customer || __("internal")} · ${format_currency(r.client_amount, frm.doc.currency)}${
						r.issues ? " ⚠" : ""
					}`,
					value: r.project,
					checked: 1,
				})),
			},
		],
		secondary_action_label: __("Leave Out Ticked"),
		secondary_action() {
			const projects = dialog.get_value("projects");
			if (!projects.length) return;
			frappe.confirm(
				__("Delete the drafts for {0} and leave them out of this run? Use this when they were billed by hand.", [
					projects.join(", "),
				]),
				() => {
					dialog.hide();
					frm.call({ doc: frm.doc, method: "leave_out", args: { projects }, freeze: true }).then(() =>
						frm.reload_doc()
					);
				}
			);
		},
		primary_action_label: __("Review selected"),
		primary_action(values) {
			if (!values.projects.length) return;
			dialog.hide();
			frappe.call({
				method: "books_assistant.billing_api.preview_billing_approval",
				args: { run_id: frm.doc.name, projects: values.projects, company: frm.doc.company },
				freeze: true,
			}).then(({ message: preview }) => confirm_preview(frm, preview));
		},
	});
	dialog.show();
}


function confirm_preview(frm, preview) {
	const esc = frappe.utils.escape_html;
	const snapshot = preview.snapshot;
	const rows = snapshot.projects.map((row) => {
		const email = row.email_on_approval
			? __("Queue invoice email to {0}; CC {1}", [row.recipients.join(", "), row.cc.join(", ") || "—"])
			: __("No invoice email will be queued.");
		return `<tr><td>${esc(row.project)}</td><td>${esc(row.sales_invoice || "—")}</td>
			<td>${format_currency(row.client_amount, snapshot.currency)}</td>
			<td>${format_currency(row.engineer_cost, snapshot.currency)}<div class="small">${esc(row.engineer_bills.join(", ") || "—")}</div></td>
			<td>${format_currency(row.referral_cost, snapshot.currency)}</td>
			<td class="${row.current_margin_warning ? "text-warning" : ""}">${esc(String(row.margin_pct))}%${row.current_margin_warning ? ` ${esc(__("Below 5% target"))}` : ""}</td>
			<td>${esc(email)}</td></tr>`;
	}).join("");
	// Keep one key for this reviewed action; a retry must not create a second operation.
	const request_id = crypto.randomUUID();
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm reviewed billing"),
		size: "extra-large",
		fields: [{ fieldtype: "HTML", options: `<p>${esc(__(
			"This submits the invoices and bills below, creates applicable referral bills, and applies the configured client-payment holds. Changed records require a new review."
		))}</p><p>${esc(__("Selected-document margin uses only the listed client invoice, engineer bills, and its applicable referral fees. Earlier invoices and bills are excluded. Preparation warnings on the run describe its earlier preparation state."))}</p>
			<table class="table table-sm"><thead><tr><th>${__("Project")}</th><th>${__("Client invoice")}</th>
			<th>${__("Client amount")}</th><th>${__("Engineer cost / bills")}</th><th>${__("Referral cost")}</th>
			<th>${__("Selected-document margin")}</th><th>${__("Email")}</th></tr></thead><tbody>${rows}</tbody></table>` }],
		primary_action_label: __("Approve billing"),
		primary_action() {
			frappe.call({
				method: "books_assistant.billing_api.approve_billing",
				args: { preview_id: preview.preview_id, request_id },
				freeze: true,
				freeze_message: __("Submitting reviewed billing…"),
			}).then(() => { dialog.hide(); frm.reload_doc(); });
		},
	});
	dialog.show();
}
