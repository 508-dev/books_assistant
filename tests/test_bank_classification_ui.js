const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../books_assistant/books_assistant/page/bank_review/bank_review.js'), 'utf8');

function fixture(message) {
    const calls = [], confirms = [], notices = [], rules = [];
    const frappe = {
        pages: { 'bank-review': {} },
        call: async (args) => { calls.push(args); return { message }; },
        confirm: (text, accept) => confirms.push({ text, accept }),
        msgprint: (text) => notices.push(text),
        utils: { escape_html: (text) => String(text).replaceAll('<', '&lt;').replaceAll('>', '&gt;') },
    };
    const context = { frappe, __: (text, args = []) => text.replace(/\{(\d+)\}/g, (_, n) => args[n]) };
    vm.createContext(context);
    vm.runInContext(source + '\nglobalThis.Review = BankReview;', context);
    const review = Object.create(context.Review.prototype);
    review.make_rule = (row) => rules.push(row);
    return { review, calls, confirms, notices, rules };
}

test('suggestion is an explicit reviewed rule prefill and never a booking call', async () => {
    const f = fixture({ status: 'suggested', account: 'Software', source: 'local', description: 'CURRENT VENDOR', keyword: 'CURRENT', bank_account: 'Current Bank', input_fingerprint: 'current-version' });
    await f.review.suggest_category({ bank_transaction: 'BT-1', keyword: 'GITHUB' });
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].method, 'books_assistant.bank_classification.suggest');
    assert.equal(f.calls[0].type, 'POST');
    assert.equal(f.calls[0].args.bank_transaction, 'BT-1');
    assert.equal(f.rules.length, 0);
    f.confirms[0].accept();
    assert.equal(f.rules[0].suggested_account, 'Software');
    assert.equal(f.rules[0].keyword, 'CURRENT');
    assert.equal(f.rules[0].description, 'CURRENT VENDOR');
    assert.equal(f.rules[0].bank_account, 'Current Bank');
    assert.equal(f.rules[0].classification_fingerprint, 'current-version');
    assert.equal(f.calls.length, 1);
});

test('abstention leaves the line unresolved and exposes the reason', async () => {
    const f = fixture({ status: 'abstain', reason_code: 'LOW_SIMILARITY' });
    await f.review.suggest_category({ bank_transaction: 'BT-1' });
    assert.equal(f.confirms.length, 0);
    assert.equal(f.rules.length, 0);
    assert.match(f.notices[0], /LOW_SIMILARITY/);
});

test('provider labels are escaped in the confirmation', async () => {
    const f = fixture({ status: 'suggested', account: '<script>', source: '<img>' });
    await f.review.suggest_category({ bank_transaction: 'BT-1' });
    assert.ok(!f.confirms[0].text.includes('<script>'));
    assert.ok(!f.confirms[0].text.includes('<img>'));
});

test('benchmark is a POST and its data is escaped', async () => {
    const f = fixture({ status: 'no_data', note: '<script>' });
    await f.review.benchmark_classifier();
    assert.equal(f.calls[0].method, 'books_assistant.bank_classification.benchmark');
    assert.equal(f.calls[0].type, 'POST');
    assert.ok(!f.notices[0].message.includes('<script>'));
    assert.equal(f.rules.length, 0);
});
