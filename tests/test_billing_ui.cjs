const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function refresh(items) {
    const handlers = {};
    const dialogs = [];
    const buttons = new Map();
    const rendered = {};
    const context = {
        __: (text, values = []) => text.replace(/\{(\d+)\}/g, (_, index) => values[index]),
        format_currency: (amount) => String(amount),
        frappe: {
            user_roles: ["System Manager"],
            ui: {
                form: { on: (doctype, methods) => { handlers[doctype] = methods; } },
                Dialog: class {
                    constructor(options) { dialogs.push(options); }
                    show() {}
                },
            },
            utils: { escape_html: (value) => String(value) },
        },
    };
    vm.createContext(context);
    const source = fs.readFileSync(path.join(__dirname, "../books_assistant/books_assistant/doctype/billing_run/billing_run.js"), "utf8");
    vm.runInContext(source, context);
    const frm = {
        doc: { name: "BR-1", status: "Prepared", currency: "USD", items },
        is_new: () => false,
        add_custom_button: (label, action) => { buttons.set(label, action); return { addClass() {} }; },
        dashboard: { set_headline: (text) => { rendered.headline = text; } },
        set_df_property() {},
        get_field: () => ({ $wrapper: { html: (html) => { rendered.html = html; } } }),
    };
    handlers["Billing Run"].refresh(frm);
    return { buttons, dialogs, rendered };
}

test("approval and leave-out choices include only pending or failed projects", () => {
    const { buttons, dialogs, rendered } = refresh([
        { project: "PENDING", item_status: "pending" },
        { project: "FAILED", item_status: "failed" },
        { project: "LEGACY", item_status: "review_required" },
        { project: "APPROVED", item_status: "approved" },
    ]);
    buttons.get("Approve and Send")();
    const selection = dialogs[0].fields.find((field) => field.fieldname === "projects");
    assert.deepEqual(Array.from(selection.options, (option) => option.value), ["PENDING", "FAILED"]);
    assert.ok(selection.options.every((option) => option.checked === 1));
    assert.match(rendered.html, /LEGACY/);
    assert.match(rendered.html, /Requires System Manager review/);
    assert.equal(buttons.has("Discard Drafts"), false);
});

test("a run containing only unknown legacy state remains visible without action buttons", () => {
    const { buttons, rendered } = refresh([{ project: "LEGACY", item_status: "review_required", outcome: "" }]);
    assert.equal(buttons.has("Approve and Send"), false);
    assert.equal(buttons.has("Discard Drafts"), false);
    assert.match(rendered.html, /LEGACY/);
    assert.match(rendered.headline, /require System Manager review/);
});
