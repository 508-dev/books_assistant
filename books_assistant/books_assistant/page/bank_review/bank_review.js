frappe.pages["bank-review"].on_page_load = (wrapper) => {
	new BankReview(wrapper);
};

const CONFIDENCE = {
	high: { label: __("Confident"), color: "green" },
	check: { label: __("Check"), color: "orange" },
	none: { label: __("Needs you"), color: "gray" },
};

class BankReview {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Bank Review"),
			single_column: true,
		});
		this.page.set_primary_action(__("Approve selected"), () => this.approve(), "check");
		this.page.add_button(__("Select confident"), () => this.select_confident());
		this.page.add_button(__("Refresh"), () => this.load());
		this.page.add_menu_item(__("Learn rules and bank names from history"), () => this.learn());
		this.page.add_menu_item(__("Benchmark local category classifier"), () => this.benchmark_classifier());
		this.page.add_menu_item(__("Bank Rules"), () => frappe.set_route("List", "Bank Rule"));
		this.page.add_menu_item(__("Bank Reconciliation Tool"), () =>
			frappe.set_route("Form", "Bank Reconciliation Tool")
		);
		this.bank_account = this.page.add_field({
			fieldname: "bank_account",
			label: __("Bank Account"),
			fieldtype: "Link",
			options: "Bank Account",
			get_query: () => ({ filters: { is_company_account: 1 } }),
			change: () => this.load(),
		});
		this.$body = $('<div class="bank-review"></div>').appendTo(this.page.main);
		this.$body.on("click", ".make-rule", (e) => {
			const row = this.rows.find((r) => r.bank_transaction === e.currentTarget.dataset.name);
			this.make_rule(row);
		});
		this.$body.on("click", ".suggest-category", (e) => {
            const row = this.rows.find((r) => r.bank_transaction === e.currentTarget.dataset.name);
            this.suggest_category(row);
        });
		this.$body.on("change", ".select-all", (e) =>
			this.$body.find(".row-check:not(:disabled)").prop("checked", e.target.checked)
		);
		this.load();
	}

	load() {
		frappe
			.call({
				method: "books_assistant.bank_review.get_queue",
				type: "GET",
				args: { bank_account: this.bank_account.get_value() || null },
				freeze: true,
				freeze_message: __("Working out suggestions…"),
			})
			.then(({ message }) => this.render(message || []));
	}

	render(rows) {
		this.rows = rows;
		if (!rows.length) {
			this.$body.html(
				`<div class="text-muted text-center" style="padding: 60px">${__("Every bank line is reconciled.")}</div>`
			);
			return;
		}
		const counts = { high: 0, check: 0, none: 0 };
		rows.forEach((r) => counts[r.confidence]++);
		const esc = frappe.utils.escape_html;
		const money = (v) => (v ? format_currency(v, "USD") : "");
		const body = rows
			.map((r) => {
				const c = CONFIDENCE[r.confidence];
				const action =
					r.kind === "none"
						? `<button class="btn btn-xs btn-default make-rule" data-name="${esc(r.bank_transaction)}">${__("Make rule")}</button>
                           <button class="btn btn-xs btn-default suggest-category" data-name="${esc(r.bank_transaction)}">${__("Suggest category")}</button>`
						: "";
				return `<tr data-confidence="${r.confidence}">
					<td><input type="checkbox" class="row-check" data-name="${esc(r.bank_transaction)}" ${r.kind === "none" ? "disabled" : ""}></td>
					<td class="text-nowrap">${esc(r.date)}</td>
					<td class="text-nowrap"><a href="/app/bank-transaction/${encodeURIComponent(r.bank_transaction)}">${esc(r.bank_account)}</a></td>
					<td>${esc(r.description || "")}</td>
					<td class="text-right text-nowrap text-success">${money(r.deposit)}</td>
					<td class="text-right text-nowrap">${money(r.withdrawal)}</td>
					<td>${esc(r.summary)}</td>
					<td class="text-nowrap"><span class="indicator-pill ${c.color}">${c.label}</span></td>
					<td>${action}</td>
				</tr>`;
			})
			.join("");
		this.$body.html(`
			<p class="text-muted" style="margin: 12px 0">
				${__("{0} lines: {1} confident, {2} to check, {3} need you. Review these lines before approving.", [
					rows.length,
					counts.high,
					counts.check,
					counts.none,
				])}
			</p>
			<div class="table-responsive"><table class="table table-sm table-hover" style="font-size: 12px">
				<thead><tr>
					<th><input type="checkbox" class="select-all"></th>
					<th>${__("Date")}</th><th>${__("Bank")}</th><th>${__("Description")}</th>
					<th class="text-right">${__("In")}</th><th class="text-right">${__("Out")}</th>
					<th>${__("Suggestion")}</th><th></th><th></th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table></div>`);
	}

	selected() {
		return this.$body
			.find(".row-check:checked")
			.map((_i, el) => el.dataset.name)
			.get();
	}

	select_confident() {
		this.$body.find("tr[data-confidence='high'] .row-check").prop("checked", true);
	}

	approve() {
		const names = this.selected();
		if (!names.length) {
			frappe.msgprint(__("Select the lines to approve first."));
			return;
		}
		const selected = this.rows.filter((row) => names.includes(row.bank_transaction));
		const expected_fingerprints = Object.fromEntries(selected.map((row) => [row.bank_transaction, row.fingerprint]));
		frappe.call({
			method: "books_assistant.bank_review.preview_approval",
			args: { bank_transactions: names, expected_fingerprints, bank_account: this.bank_account.get_value() || null },
			freeze: true,
		}).then(({ message: frozen }) => {
			const request_id = crypto.randomUUID();
			frappe.confirm(
				__("Approve {0} line(s)? Each one creates or links the entry shown in its suggestion.", [names.length]),
				() => frappe.call({
					method: "books_assistant.bank_review.approve",
					args: { preview_id: frozen.preview_id, request_id },
					freeze: true, freeze_message: __("Approving…"),
				}).then(({ message }) => {
					this.show_results(message.results || []);
					this.load();
				})
			);
		});
	}

	show_results(results) {
		const failed = results.filter((r) => !r.ok);
		const esc = frappe.utils.escape_html;
		if (!failed.length) {
			frappe.show_alert({ message: __("Approved {0} line(s)", [results.length]), indicator: "green" });
			return;
		}
		frappe.msgprint({
			title: __("{0} of {1} approved", [results.length - failed.length, results.length]),
			indicator: "orange",
			message: `<p>${__("These were not booked:")}</p><ul>${failed
				.map((r) => `<li><b>${esc(r.name)}</b>: ${esc(r.message)}</li>`)
				.join("")}</ul>`,
		});
	}

	make_rule(row) {
		const dialog = new frappe.ui.Dialog({
			title: __("Make a rule"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${frappe.utils.escape_html(row.description || "")}</p><p>${__("Bank")}: ${frappe.utils.escape_html(row.bank_account || "")}</p>`,
				},
				{
					fieldname: "keyword",
					label: __("Words to match"),
					fieldtype: "Data",
					default: row.keyword,
					reqd: 1,
					description: __("Any bank line containing these words gets the same suggestion."),
				},
				{
					fieldname: "account",
					label: __("Book to account"),
					fieldtype: "Link",
					options: "Account",
                    default: row.suggested_account || null,
					reqd: 1,
					get_query: () => ({
						filters: { is_group: 0, account_type: ["not in", ["Cash", "Receivable", "Payable"]] },
					}),
				},
			],
			primary_action_label: __("Create rule"),
			primary_action: (values) =>
				frappe
					.call({
						method: "books_assistant.bank_review.create_rule",
						args: { bank_transaction: row.bank_transaction, ...values,
                            ...(row.classification_fingerprint ? { expected_fingerprint: row.classification_fingerprint } : {}) },
					})
					.then(() => {
						dialog.hide();
						frappe.show_alert({ message: __("Rule created"), indicator: "green" });
						this.load();
					}),
		});
		dialog.show();
	}


    async suggest_category(row) {
        const { message } = await frappe.call({
            method: "books_assistant.bank_classification.suggest",
            type: "POST", args: { bank_transaction: row.bank_transaction },
            freeze: true, freeze_message: __("Checking historical categories…"),
        });
        if (message.status !== "suggested" || !message.account) {
            frappe.msgprint(__("No category suggestion: {0}. Review this line manually.", [message.reason_code]));
            return;
        }
        const esc = frappe.utils.escape_html;
        frappe.confirm(
            __("Suggested category: {0}. Source: {1}. Review a rule for future matching lines? This does not book the transaction or authorize automatic booking.",
                [esc(message.account), esc(message.source)]),
            () => this.make_rule({ ...row, description: message.description, keyword: message.keyword,
                bank_account: message.bank_account, suggested_account: message.account,
                classification_fingerprint: message.input_fingerprint }),
        );
    }

    async benchmark_classifier() {
        const { message } = await frappe.call({
            method: "books_assistant.bank_classification.benchmark", type: "POST",
            freeze: true, freeze_message: __("Replaying historical classifications locally…"),
        });
        frappe.msgprint({ title: __("Local classifier benchmark"),
            message: `<pre>${frappe.utils.escape_html(JSON.stringify(message, null, 2))}</pre>`, wide: true });
    }

	learn() {
		frappe.confirm(
			__("Create rules and record bank names from past reconciliations? Existing rules and names are kept."),
			() =>
				frappe
					.call({
						method: "books_assistant.bank_review.learn_from_history",
						freeze: true,
						freeze_message: __("Reading past reconciliations…"),
					})
					.then(({ message }) => {
						frappe.msgprint(message);
						this.load();
					})
		);
	}
}
