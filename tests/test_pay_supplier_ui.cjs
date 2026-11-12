const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../books_assistant/public/js/pay_supplier.js"), "utf8");

function harness({ count = 1, creditOnly = false, prepareResponses = [] } = {}) {
	const calls = [], dialogs = [], confirmations = [], notices = [];
	const bills = Array.from({ length: count }, (_, i) => ({ name: `BILL-${i + 1}`, outstanding_amount: 100 }));
	const view = { company: "Co-op", currency: "USD", bills, held: [], fronting: [],
		bills_total: count * 100, credit_total: creditOnly ? count * 100 : 0,
		credit_to_apply: creditOnly ? count * 100 : 0, to_pay: creditOnly ? 0 : count * 100,
		bank_account: creditOnly ? null : "BANK", funding: creditOnly ? null : {cash: {status: "unverified"}} };
	let nextId = 0, previews = 0;
	const context = { books_assistant: {}, crypto: { randomUUID: () => `REQUEST-${++nextId}` },
		__: (value, args = []) => value.replace(/\{(\d+)\}/g, (_, n) => String(args[n])),
		format_currency: (value, currency) => `${currency} ${value}` };
	context.frappe = {
		db: {get_value: async () => ({message: {name: "BANK-ACCOUNT"}})},
		provide() {}, utils: { escape_html: (value) => String(value) }, router: { slug: (value) => value.toLowerCase().replaceAll(" ", "-") },
		ui: { Dialog: class {
			constructor(options) { this.options = options; this.fields_dict = {funding: {$wrapper: {text() {}, html() {}, empty() {}}}}; dialogs.push(this); }
            get_value() { return view.bank_account; }
			set_primary_action(label, callback) { this.options.primary_action_label = label; this.options.primary_action = callback; }
			show() {} hide() { this.hidden = true; }
		} },
		confirm: (text, yes, no) => confirmations.push({ text, yes, no }),
		msgprint: (text) => notices.push(text), show_alert() {}, set_route() {},
		model: { sync: (value) => [value] },
		call: (request) => {
			calls.push(request);
			if (request.method === "books_assistant.funding.check") return Promise.resolve({message: view.funding});
			if (request.method.endsWith(".preview")) return Promise.resolve({ message: view });
			if (request.method.endsWith(".preview_preparation")) {
				return Promise.resolve({ message: { preview_id: `PREVIEW-${++previews}`, snapshot: view } });
			}
			const response = prepareResponses.shift();
			if (response instanceof Promise) return response;
			if (response?.error) return Promise.reject(response.error);
			return Promise.resolve({ message: response || { status: "prepared", operation_id: "OPERATION-1", payment_entry: null, credit_applied: view.credit_to_apply } });
		},
	};
	context.books_assistant.funding_html = () => "<p>Funding review</p>";
	vm.runInNewContext(source, context);
	return { context, calls, dialogs, confirmations, notices };
}

const callsTo = (h, method) => h.calls.filter((call) => call.method.endsWith(`.${method}`));

test("supplier-wide requests omit invoices in both read and freeze", async () => {
	const h = harness({ count: 101 });
	await h.context.books_assistant.pay_supplier("Supplier");
	const read = callsTo(h, "preview")[0];
	assert.equal(read.args.all_eligible, true);
	assert.equal(Object.hasOwn(read.args, "invoices"), false);
	await h.dialogs[0].options.primary_action({ bank_account: "BANK" });
	const freeze = callsTo(h, "preview_preparation")[0];
	assert.equal(freeze.args.all_eligible, true);
	assert.equal(Object.hasOwn(freeze.args, "invoices"), false);
	assert.match(h.confirmations[0].text, /BILL-101/);
});

test("explicit selections, including empty selections, remain explicit", async () => {
	for (const invoices of [[], ["BILL-1"]]) {
		const h = harness();
		await h.context.books_assistant.pay_supplier("Supplier", invoices);
		assert.equal(callsTo(h, "preview")[0].args.invoices, invoices);
		assert.equal(Object.hasOwn(callsTo(h, "preview")[0].args, "all_eligible"), false);
		await h.dialogs[0].options.primary_action({ bank_account: "BANK" });
		assert.equal(callsTo(h, "preview_preparation")[0].args.invoices, invoices);
		assert.equal(Object.hasOwn(callsTo(h, "preview_preparation")[0].args, "all_eligible"), false);
	}
});

test("uncertain prepare response retries original preview and request without freezing again", async () => {
	const h = harness({ creditOnly: true, prepareResponses: [{ error: { status: 0 } }] });
	await h.context.books_assistant.pay_supplier("Supplier");
	const dialog = h.dialogs[0];
	await dialog.options.primary_action({});
	await h.confirmations[0].yes();
	assert.equal(dialog.hidden, undefined);
	assert.equal(dialog.options.primary_action_label, "Retry result");
	await dialog.options.primary_action({});
	const executions = callsTo(h, "prepare");
	assert.equal(executions.length, 2);
	assert.deepEqual(JSON.parse(JSON.stringify(executions[0].args)), JSON.parse(JSON.stringify(executions[1].args)));
	assert.equal(callsTo(h, "preview_preparation").length, 1);
	assert.equal(dialog.hidden, true);
	assert.match(h.notices[0], /uncertain/);
});

test("a second click while execution is pending does not send another request", async () => {
	let finish;
	const response = new Promise((resolve) => { finish = resolve; });
	const h = harness({ prepareResponses: [response] });
	await h.context.books_assistant.pay_supplier("Supplier", ["BILL-1"]);
	const dialog = h.dialogs[0];
	await dialog.options.primary_action({ bank_account: "BANK" });
	const executing = h.confirmations[0].yes();
	await dialog.options.primary_action({});
	assert.equal(callsTo(h, "prepare").length, 1);
	finish({ message: { status: "prepared", operation_id: "OPERATION-1", payment_entry: null, credit_applied: 0 } });
	await executing;
	assert.equal(dialog.hidden, true);
});

test("authoritative stale response permits a fresh reviewed operation", async () => {
	const h = harness({ prepareResponses: [{ error: { responseJSON: { error_code: "PREVIEW_STALE" } } }] });
	await h.context.books_assistant.pay_supplier("Supplier", ["BILL-1"]);
	const dialog = h.dialogs[0];
	await dialog.options.primary_action({ bank_account: "BANK" });
	await h.confirmations[0].yes();
	assert.equal(dialog.options.primary_action_label, "Prepare payment");
	await dialog.options.primary_action({ bank_account: "BANK" });
	await h.confirmations[1].yes();
	assert.equal(callsTo(h, "preview_preparation").length, 2);
	assert.notEqual(callsTo(h, "prepare")[0].args.request_id, callsTo(h, "prepare")[1].args.request_id);
});

test("credit-only review does not require or describe a bank account", async () => {
	const h = harness({ creditOnly: true });
	await h.context.books_assistant.pay_supplier("Supplier");
	const dialog = h.dialogs[0];
	const field = dialog.options.fields.find((field) => field.fieldname === "bank_account");
	assert.equal(field.reqd, false);
	assert.equal(field.hidden, true);
	await dialog.options.primary_action({});
	assert.match(h.confirmations[0].text, /No payment document or bank transfer/);
	assert.doesNotMatch(h.confirmations[0].text, /null|undefined/);
});


test("malformed success response retains original identifiers until a valid result arrives", async () => {
	const h = harness({ prepareResponses: [{}] });
	await h.context.books_assistant.pay_supplier("Supplier");
	const dialog = h.dialogs[0];
	await dialog.options.primary_action({ bank_account: "BANK" });
	await h.confirmations[0].yes();
	assert.equal(dialog.hidden, undefined);
	await dialog.options.primary_action({});
	const executions = callsTo(h, "prepare");
	assert.equal(executions.length, 2);
	assert.equal(executions[0].args.preview_id, executions[1].args.preview_id);
	assert.equal(executions[0].args.request_id, executions[1].args.request_id);
	assert.equal(callsTo(h, "preview_preparation").length, 1);
	assert.equal(dialog.hidden, true);
});


test("credit-only review makes no bank funding request", async () => {
    const h = harness({creditOnly: true});
    await h.context.books_assistant.pay_supplier("Supplier");
    await h.dialogs[0].options.primary_action({});
    assert.equal(h.calls.some((call) => call.method === "books_assistant.funding.check"), false);
    assert.doesNotMatch(h.confirmations[0].text, /Funding review/);
});

test("payout preparation includes the frozen funding review", async () => {
    const h = harness();
    await h.context.books_assistant.pay_supplier("Supplier");
    await h.dialogs[0].options.primary_action({bank_account: "BANK"});
    assert.match(h.confirmations[0].text, /Funding review/);
});
