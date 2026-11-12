const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../books_assistant/books_assistant/page/bank_review/bank_review.js"), "utf8");

function harness({ approvalResponses = [], previewResponses = [] } = {}) {
	const calls = [], confirmations = [], notices = [], results = [], previews = new Map();
	let nextId = 0, nextPreview = 0, loads = 0;
	const context = {
		__: (value, args = []) => value.replace(/\{(\d+)\}/g, (_, n) => String(args[n])),
		crypto: { randomUUID: () => `REQUEST-${++nextId}` },
		frappe: {
			pages: { "bank-review": {} },
			confirm: (text, yes, no) => confirmations.push({ text, yes, no }),
			msgprint: (value) => notices.push(value),
			call: (request) => {
				calls.push(request);
				if (request.method.endsWith(".preview_approval")) {
					if (previewResponses.length) return previewResponses.shift();
					const preview_id = `PREVIEW-${++nextPreview}`;
					previews.set(preview_id, [...request.args.bank_transactions]);
					return Promise.resolve({ message: { preview_id } });
				}
				if (approvalResponses.length) {
					const next = approvalResponses.shift();
					return typeof next === "function" ? next(request) : next;
				}
				return Promise.resolve({ message: {
					status: "completed", operation_id: `OPERATION-${request.args.request_id}`,
					results: (previews.get(request.args.preview_id) || []).map((name) => ({
						name, ok: true, status: "approved", message: "Matched to recorded payment",
					})),
				} });
			},
		},
	};
	vm.runInNewContext(`${source}\nglobalThis.BankReview = BankReview;`, context);
	const page = { label: "Approve selected", set_primary_action(label, callback) { this.label = label; this.callback = callback; } };
	const review = Object.create(context.BankReview.prototype);
	review.page = page;
	review.pending_approval = null;
	review.approval_in_flight = false;
	review.reviewing_approval = false;
	review.bank_account = { get_value: () => "BANK" };
	review.rows = [{ bank_transaction: "BT-1", fingerprint: "FINGERPRINT-1" }];
	review.selected = () => ["BT-1"];
	review.show_results = (value) => results.push(value);
	review.load = () => { loads++; return Promise.resolve(); };
	return { review, page, calls, confirmations, notices, results, get loads() { return loads; } };
}

const callsTo = (h, method) => h.calls.filter((call) => call.method.endsWith(`.${method}`));
const plain = (value) => JSON.parse(JSON.stringify(value));

test("lost approval response retries the original IDs even after selection and bank filter change", async () => {
	const h = harness({ approvalResponses: [() => Promise.reject({ status: 0 })] });
	await h.review.approve();
	await h.confirmations[0].yes();
	assert.equal(h.page.label, "Retry result");
	assert.equal(h.loads, 0);
	h.review.rows = [];
	h.review.selected = () => [];
	h.review.bank_account.get_value = () => "OTHER-BANK";
	await h.review.approve();
	assert.equal(callsTo(h, "preview_approval").length, 1);
	const requests = callsTo(h, "approve");
	assert.equal(requests.length, 2);
	assert.deepEqual(plain(requests[0].args), plain(requests[1].args));
	assert.equal(h.review.pending_approval, null);
	assert.equal(h.page.label, "Approve selected");
	assert.equal(h.loads, 1);
	assert.match(h.notices[0], /uncertain/);
});

test("double clicks cannot create a second preview, request ID, or execution", async () => {
	let finishPreview, finishApproval;
	const preview = new Promise((resolve) => { finishPreview = resolve; });
	const approval = new Promise((resolve) => { finishApproval = resolve; });
	const h = harness({ previewResponses: [preview], approvalResponses: [approval] });
	const opening = h.review.approve();
	await h.review.approve();
	assert.equal(callsTo(h, "preview_approval").length, 1);
	finishPreview({ message: { preview_id: "PREVIEW-1" } });
	await opening;
	const executing = h.confirmations[0].yes();
	await h.confirmations[0].yes();
	await h.review.approve();
	assert.equal(callsTo(h, "approve").length, 1);
	finishApproval({ message: { status: "completed", operation_id: "OPERATION-1",
		results: [{ name: "BT-1", ok: true, status: "approved", message: "Matched" }] } });
	await executing;
	assert.equal(h.review.pending_approval, null);
	assert.equal(h.loads, 1);
});

test("malformed successful responses retain identifiers until a complete result arrives", async () => {
	for (const response of [undefined, {}, { message: {} },
		{ message: { status: "completed", operation_id: "OPERATION", results: [] } },
		{ message: { status: "completed", operation_id: "OPERATION", results: [null] } },
		{ message: { status: "completed", operation_id: "OPERATION", results: [{ name: "OTHER", ok: true, status: "approved", message: "Wrong line" }] } },
		{ message: { status: "completed", operation_id: "OPERATION", results: [{ name: "BT-1", ok: false, status: "failed", message: "Contradictory batch status" }] } },
		{ message: { status: "partial_failure", operation_id: "OPERATION", results: [{ name: "BT-1", ok: true, status: "approved", message: "Contradictory batch status" }] } },
	]) {
		const h = harness({ approvalResponses: [() => Promise.resolve(response)] });
		await h.review.approve();
		await h.confirmations[0].yes();
		assert.ok(h.review.pending_approval);
		assert.equal(h.review.approval_in_flight, false);
		assert.equal(h.results.length, 0);
		await h.review.approve();
		const requests = callsTo(h, "approve");
		assert.deepEqual(plain(requests[0].args), plain(requests[1].args));
		assert.equal(callsTo(h, "preview_approval").length, 1);
		assert.equal(h.review.pending_approval, null);
		assert.equal(h.results.length, 1);
	}
});

test("a verified result clears pending state and a later selection gets a fresh operation", async () => {
	const h = harness();
	await h.review.approve();
	await h.confirmations[0].yes();
	assert.equal(h.review.pending_approval, null);
	assert.equal(h.page.label, "Approve selected");
	h.review.rows = [{ bank_transaction: "BT-2", fingerprint: "FINGERPRINT-2" }];
	h.review.selected = () => ["BT-2"];
	await h.review.approve();
	await h.confirmations[1].yes();
	const requests = callsTo(h, "approve");
	assert.equal(requests.length, 2);
	assert.notEqual(requests[0].args.request_id, requests[1].args.request_id);
	assert.notEqual(requests[0].args.preview_id, requests[1].args.preview_id);
	assert.equal(h.loads, 2);
	assert.equal(h.results[1][0].name, "BT-2");
});

test("only authoritative stale or expired errors permit a fresh approval", async () => {
	for (const error_code of ["PREVIEW_STALE", "PREVIEW_EXPIRED"]) {
		const h = harness({ approvalResponses: [() => Promise.reject({ responseJSON: { error_code } })] });
		await h.review.approve();
		await h.confirmations[0].yes();
		assert.equal(h.review.pending_approval, null);
		assert.equal(h.page.label, "Approve selected");
		assert.equal(h.loads, 1);
		await h.review.approve();
		await h.confirmations[1].yes();
		assert.equal(callsTo(h, "preview_approval").length, 2);
		assert.notEqual(callsTo(h, "approve")[0].args.request_id, callsTo(h, "approve")[1].args.request_id);
	}
});

test("permission or unclassified errors keep the original pending operation", async () => {
	const h = harness({ approvalResponses: [() => Promise.reject({ responseJSON: { error_code: "PREVIEW_FORBIDDEN" } })] });
	await h.review.approve();
	await h.confirmations[0].yes();
	assert.ok(h.review.pending_approval);
	assert.equal(h.page.label, "Retry result");
	assert.equal(h.loads, 0);
	await h.review.approve();
	assert.equal(callsTo(h, "preview_approval").length, 1);
	assert.deepEqual(plain(callsTo(h, "approve")[0].args), plain(callsTo(h, "approve")[1].args));
});

test("a recorded partial failure is a known result and can be reviewed afresh", async () => {
	const h = harness({ approvalResponses: [() => Promise.resolve({ message: {
		status: "partial_failure", operation_id: "OPERATION-1",
		results: [{ name: "BT-1", ok: false, status: "failed", message: "Account requires correction" }],
	} })] });
	await h.review.approve();
	await h.confirmations[0].yes();
	assert.equal(h.review.pending_approval, null);
	assert.equal(h.results[0][0].ok, false);
	assert.equal(h.loads, 1);
});

test("declining confirmation never generates an execution ID", async () => {
	const h = harness();
	await h.review.approve();
	h.confirmations[0].no();
	assert.equal(h.review.reviewing_approval, false);
	assert.equal(h.review.pending_approval, null);
	assert.equal(callsTo(h, "approve").length, 0);
	await h.review.approve();
	await h.confirmations[1].yes();
	assert.equal(callsTo(h, "approve")[0].args.request_id, "REQUEST-1");
});

test("a failed or malformed preview allows another review without sending an approval", async () => {
	for (const response of [() => Promise.reject({ status: 0 }), () => Promise.resolve(undefined)]) {
		const h = harness({ previewResponses: [response()] });
		await h.review.approve();
		assert.equal(h.review.reviewing_approval, false);
		assert.equal(h.review.pending_approval, null);
		assert.equal(callsTo(h, "approve").length, 0);
		await h.review.approve();
		await h.confirmations[0].yes();
		assert.equal(callsTo(h, "approve").length, 1);
	}
});
