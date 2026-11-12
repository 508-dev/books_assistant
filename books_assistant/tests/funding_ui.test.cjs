const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function context() {
	const ctx = {
		books_assistant: {},
		__: (s, args = []) => s.replace(/\{(\d+)\}/g, (_, n) => args[n]),
		format_currency: (v, currency) => `${currency} ${Number(v).toFixed(2)}`,
		frappe: {
			provide() {}, user_roles: ["Accounts Manager"],
			utils: {debounce: (fn) => fn, escape_html: (s) => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;")},
			ui: {form: {on() {}}},
		},
	};
	vm.createContext(ctx);
	for (const name of ["funding.js", "payment_entry.js"]) {
		vm.runInContext(fs.readFileSync(path.join(__dirname, "../public/js", name), "utf8"), ctx);
	}
	return ctx;
}

function report(overrides = {}) {
	return {currency: "USD", cash: {status: "unverified", reason: "missing_balance"},
		clients: [], issues: [], can_approve: false, ...overrides};
}

test("missing balance cannot look like approval", () => {
	const ctx = context();
	const html = ctx.books_assistant.funding_html(report());
	assert.match(html, /Funding unverified/);
	assert.match(html, /No bank-verified balance/);
	assert.doesNotMatch(html, /Cash sufficient/);
});

test("client uncertainty stays visible when cash is sufficient and values are escaped", () => {
	const ctx = context();
	const html = ctx.books_assistant.funding_html(report({cash: {status: "sufficient"}, can_approve: true,
		fronting_required: true, clients: [{bill: "<script>alert(1)</script>", status: "unverified"}]}));
	assert.match(html, /client funding unverified/);
	assert.match(html, /approver must record a reason/);
	assert.doesNotMatch(html, /<script>/);
	assert.match(html, /&lt;script>/);
});

test("failed request remains unverified", async () => {
	const ctx = context();
	ctx.frappe.call = async () => {throw new Error("offline");};
	const notices = [];
	const frm = {doc: {docstatus: 0, payment_type: "Pay", party_type: "Supplier", references: [{reference_doctype: "Purchase Invoice", reference_name: "PI-1"}]},
		is_new: () => false, is_dirty: () => false, set_intro: (s) => notices.push(s)};
	await ctx.books_assistant.show_fronting(frm);
	assert.match(notices.at(-1), /Funding unverified: unable/);
});

test("old asynchronous checks cannot overwrite the current form", async () => {
	const ctx = context();
	let finish;
	ctx.frappe.call = () => new Promise((resolve) => {finish = resolve;});
	const notices = [];
	const frm = {doc: {docstatus: 0, payment_type: "Pay", party_type: "Supplier", references: [{reference_doctype: "Purchase Invoice", reference_name: "PI-1"}]},
		is_new: () => false, is_dirty: () => false, set_intro: (s) => notices.push(s)};
	const pending = ctx.books_assistant.show_fronting(frm);
	frm.doc.payment_type = "Receive";
	await ctx.books_assistant.show_fronting(frm);
	finish({message: report()});
	await pending;
	assert.equal(notices.at(-1), "");
});

test("ordinary accounting users are directed to approval without reading finance APIs", async () => {
	const ctx = context();
	ctx.frappe.user_roles = ["Accounts User"];
	ctx.frappe.call = () => assert.fail("should not read finance APIs");
	let notice;
	const frm = {doc: {docstatus: 0, payment_type: "Pay", party_type: "Supplier", references: [{reference_doctype: "Purchase Invoice", reference_name: "PI-1"}]}, set_intro: (s) => {notice = s;}};
	await ctx.books_assistant.show_fronting(frm);
	assert.match(notice, /Accounts Manager must review/);
});

test("insufficient cash disables approval while client fronting requires a reason", async () => {
	for (const canApprove of [false, true]) {
		const ctx = context();
		let options, disabled = false;
		ctx.frappe.call = async () => ({message: report({can_approve: canApprove, fronting_required: true})});
		ctx.frappe.ui.Dialog = class {
			constructor(opts) {options = opts;}
			show() {}
			get_primary_btn() {return {prop: (_key, value) => {disabled = value;}};}
		};
		await ctx.books_assistant.review_funding({doc: {name: "PE-1"}, is_new: () => false, is_dirty: () => false});
		assert.equal(disabled, !canApprove);
		assert.equal(options.fields.find((f) => f.fieldname === "fronting_reason").reqd, true);
	}
});

test("an unsaved payment cannot be approved", async () => {
	const ctx = context();
	let notice;
	ctx.frappe.msgprint = (s) => {notice = s;};
	ctx.frappe.call = () => assert.fail("must save first");
	await ctx.books_assistant.review_funding({is_new: () => true});
	assert.match(notice, /Save the payment/);
});


test("dirty saved payment does not double-count its existing reservation", async () => {
    const ctx = context();
    let notice;
    ctx.frappe.call = () => assert.fail("dirty saved payments must not calculate a second payout");
    const frm = {doc: {name: "PE-1", docstatus: 0, payment_type: "Pay", party_type: "Supplier", references: [{reference_doctype: "Purchase Invoice", reference_name: "PI-1"}]},
        is_new: () => false, is_dirty: () => true, set_intro: (text) => {notice = text;}};
    await ctx.books_assistant.show_fronting(frm);
    assert.match(notice, /Save changes to refresh/);
    assert.match(notice, /reservations remain held/);
    assert.doesNotMatch(notice, /shortfall|blocked/);
});
