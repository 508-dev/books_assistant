// Runs inside the "Start Here" workspace block. `root_element` is the block's shadow root.
const WIKI = "https://wiki.508.dev/doc/";
const GUIDES = {
	timesheet: "8ccae9a2-7ea4-44df-86c4-4b619bbe8a05",
	pay_me: "866f2786-c58d-4d3f-97ea-6aac5d8959c3",
	pay_supplier: "06886828-70f4-460d-9c7f-612897202fa7",
	held: "e54398cd-83e2-447e-ab90-526b1cfd66bd",
	billing_run: "2dfd10a8-9f25-4117-ad39-26cd208b868f",
	email_invoice: "4f063ba7-9035-4fed-825b-40b75abd6a1a",
	bank_review: "479ef2c4-02c9-40ae-9a32-b2ca95e7c1d2",
	new_client: "91a4e2c1-74d0-4fa8-b589-4704c60cd079",
	new_project: "f4ffa035-38c0-4e21-a28d-e79797aac82d",
	new_engineer: "cb8429b4-bc57-4401-9816-7bd23dbf1be1",
	books_health: "6a579e45-ae72-4add-ad40-5d942faf4133",
	automation: "ebded2dc-5aad-467d-9344-ee296fddfd2e",
	routine: "ead44b82-c2f1-4cb6-bbb7-e2a3a931f946",
};

const has = (...roles) => roles.some((r) => frappe.user_roles.includes(r));
const ADMIN = ["Accounts Manager", "System Manager"];
const BILLING = ["Accounts Manager", "System Manager", "Billing Approver"];
const MANAGERS = ["Projects Manager", "Accounts Manager", "System Manager"];
const esc = frappe.utils.escape_html;

function ask(title, field, primary, then) {
	frappe.prompt([{ reqd: 1, ...field }], (values) => then(values[field.fieldname]), title, primary);
}

function my_employee(counts) {
	if (counts.employee) return Promise.resolve(counts.employee);
	return frappe.db
		.get_value("Employee", { user_id: frappe.session.user, status: "Active" }, "name")
		.then(({ message }) => message && message.name);
}

function sections(counts) {
	const n = (key) => counts[key];
	const run = counts.open_billing_run;
	return [
		{
			title: __("Your time and pay"),
			show: true,
			tiles: [
				{
					emoji: "🕒",
					title: __("Log my hours"),
					sub: __("Start a timesheet for yourself on one of your projects."),
					guide: "timesheet",
					go: () =>
						my_employee(counts).then((employee) => {
							if (!employee) {
								frappe.msgprint(__("Your login isn't linked to an Employee record yet, so you can't log hours. Ask an admin to set it up."));
								return;
							}
							frappe.new_doc("Timesheet", { employee });
						}),
				},
				{
					emoji: "📋",
					title: __("My timesheets"),
					sub: __("See, finish or submit your timesheets. Submit each month's as soon as the month ends."),
					badge: n("my_draft_timesheets") ? __("{0} draft", [n("my_draft_timesheets")]) : null,
					guide: "timesheet",
					go: () =>
						my_employee(counts).then((employee) => {
							frappe.route_options = employee ? { employee } : {};
							frappe.set_route("List", "Timesheet");
						}),
				},
				{
					emoji: "💸",
					title: __("Where's my pay?"),
					sub: __("How your invoice to the co-op is made, and how to check whether the client has paid."),
					guide: "pay_me",
					go: () => window.open(WIKI + GUIDES.pay_me, "_blank"),
				},
			],
		},
		{
			title: __("Projects and people"),
			show: has(...MANAGERS),
			tiles: [
				{
					emoji: "👥",
					title: __("Log hours for another engineer"),
					sub: __("Start a timesheet on someone else's behalf."),
					guide: "timesheet",
					go: () =>
						ask(
							__("Log hours for another engineer"),
							{ fieldname: "employee", fieldtype: "Link", options: "Employee", label: __("Engineer"),
							  get_query: () => ({ filters: { status: "Active" } }) },
							__("Start timesheet"),
							(employee) => frappe.new_doc("Timesheet", { employee })
						),
				},
				{
					emoji: "🏢",
					title: __("Add a client"),
					sub: __("A new customer, including where their invoices should be emailed."),
					guide: "new_client",
					show: has("Accounts Manager", "System Manager", "Sales Manager", "Sales User"),
					go: () => frappe.new_doc("Customer"),
				},
				{
					emoji: "📁",
					title: __("Add a client project"),
					sub: __("A new project for a client: hourly or retainer, rates and referrers."),
					guide: "new_project",
					go: () => frappe.new_doc("Project"),
				},
				{
					emoji: "🧑‍💻",
					title: __("Set up a new engineer"),
					sub: __("Create their login, then use Setup as Engineer on it."),
					guide: "new_engineer",
					show: has("System Manager"),
					go: () => {
						frappe.new_doc("User");
						frappe.show_alert({ message: __("Save the new user, then click Setup as Engineer."), indicator: "blue" }, 10);
					},
				},
			],
		},
		{
			title: __("Paying people"),
			show: has(...ADMIN),
			tiles: [
				{
					emoji: "💰",
					title: __("Pay all of a supplier's bills"),
					sub: __("Everything we owe one engineer or supplier, in one payment."),
					badge: n("bills_to_pay") ? __("{0} bills ready", [n("bills_to_pay")]) : null,
					guide: "pay_supplier",
					go: () =>
						ask(
							__("Pay all of a supplier's bills"),
							{ fieldname: "supplier", fieldtype: "Link", options: "Supplier", label: __("Who are you paying?") },
							__("Continue"),
							(supplier) => books_assistant.pay_supplier(supplier)
						),
				},
				{
					emoji: "☑️",
					title: __("Pay some of a supplier's bills"),
					sub: __("Pick which of their bills to pay, then Actions → Pay selected bills."),
					guide: "pay_supplier",
					go: () =>
						ask(
							__("Pay some of a supplier's bills"),
							{ fieldname: "supplier", fieldtype: "Link", options: "Supplier", label: __("Who are you paying?") },
							__("Show their bills"),
							(supplier) => {
								frappe.route_options = { supplier, docstatus: 1, outstanding_amount: [">", 0] };
								frappe.set_route("List", "Purchase Invoice");
								frappe.show_alert({ message: __("Tick the bills to pay, then Actions → Pay selected bills."), indicator: "blue" }, 12);
							}
						),
				},
				{
					emoji: "⏸️",
					title: __("Bills waiting for clients"),
					sub: __("Engineer and referral bills on hold until the client pays."),
					badge: n("held_bills") ? __("{0} on hold", [n("held_bills")]) : null,
					calm: true,
					guide: "held",
					go: () => {
						frappe.route_options = { on_hold: 1, docstatus: 1, outstanding_amount: [">", 0] };
						frappe.set_route("List", "Purchase Invoice");
					},
				},
			],
		},
		{
			title: __("Billing clients"),
			show: has(...BILLING),
			tiles: [
				{
					emoji: "🧾",
					title: run ? __("Continue this month's billing") : __("Run monthly billing"),
					sub: run
						? __("{0} is {1}. Pick up where it left off.", [run.name, run.status.toLowerCase()])
						: __("Make last month's client invoices and engineer bills from timesheets."),
					badge: run ? __("In progress") : null,
					guide: "billing_run",
					go: () => (run ? frappe.set_route("Form", "Billing Run", run.name) : frappe.new_doc("Billing Run")),
				},
				{
					emoji: "✉️",
					title: __("Email or re-send an invoice"),
					sub: __("Open a client invoice, then click Email Invoice."),
					guide: "email_invoice",
					go: () =>
						ask(
							__("Email or re-send an invoice"),
							{ fieldname: "invoice", fieldtype: "Link", options: "Sales Invoice", label: __("Invoice"),
							  get_query: () => ({ filters: { docstatus: 1, is_return: 0 } }) },
							__("Open it"),
							(invoice) => {
								frappe.set_route("Form", "Sales Invoice", invoice);
								frappe.show_alert({ message: __("Click Email Invoice at the top right."), indicator: "blue" }, 10);
							}
						),
				},
				{
					emoji: "📬",
					title: __("Unpaid client invoices"),
					sub: __("Invoices clients haven't paid yet, oldest first."),
					badge: n("unpaid_invoices") ? __("{0} unpaid", [n("unpaid_invoices")]) : null,
					calm: true,
					guide: "routine",
					go: () => {
						frappe.route_options = { docstatus: 1, outstanding_amount: [">", 0] };
						frappe.set_route("List", "Sales Invoice");
					},
				},
			],
		},
		{
			title: __("Bank and books"),
			show: has(...ADMIN),
			tiles: [
				{
					emoji: "🏦",
					title: __("Review bank transactions"),
					sub: __("Approve the suggested match for each new bank line."),
					badge: n("bank_lines") ? __("{0} waiting", [n("bank_lines")]) : null,
					guide: "bank_review",
					go: () => frappe.set_route("bank-review"),
				},
				{
					emoji: "🩺",
					title: __("Books health check"),
					sub: __("Anything in the books that looks wrong, and what to do about it."),
					guide: "books_health",
					go: () => frappe.set_route("query-report", "Books Health"),
				},
				{
					emoji: "⚙️",
					title: __("Automation settings"),
					sub: __("What the automations are doing, and a switch for each."),
					guide: "automation",
					show: has("System Manager", "Accounts Manager", "Billing Approver"),
					go: () => frappe.set_route("Form", "Automation Settings"),
				},
			],
		},
	];
}

function render(counts) {
	const container = root_element.querySelector(".start-sections");
	container.innerHTML = "";
	for (const section of sections(counts)) {
		const tiles = section.tiles.filter((t) => t.show !== false);
		if (!section.show || !tiles.length) continue;
		const el = document.createElement("div");
		el.className = "start-section";
		el.innerHTML = `<h5>${esc(section.title)}</h5><div class="start-grid"></div>`;
		const grid = el.querySelector(".start-grid");
		for (const tile of tiles) {
			const button = document.createElement("button");
			button.type = "button";
			button.className = "start-tile";
			button.innerHTML = `
				${tile.badge ? `<span class="badge-count ${tile.calm ? "calm" : ""}">${esc(tile.badge)}</span>` : ""}
				<span class="emoji" aria-hidden="true">${tile.emoji}</span>
				<span class="title">${esc(tile.title)}</span>
				<span class="sub">${esc(tile.sub)}</span>
				<span class="how"><a href="${WIKI + GUIDES[tile.guide]}" target="_blank" rel="noopener">${esc(__("How?"))}</a></span>`;
			button.addEventListener("click", (event) => {
				if (event.target.closest("a")) return; // the How? link opens the guide instead
				tile.go();
			});
			grid.appendChild(button);
		}
		container.appendChild(el);
	}
}

render({});
frappe
	.call({ method: "books_assistant.start_page.counts", type: "GET" })
	.then(({ message }) => render(message || {}))
	.catch(() => {});
