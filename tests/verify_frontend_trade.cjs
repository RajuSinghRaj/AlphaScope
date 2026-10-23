const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("static/js/app.js", "utf8");
const start = source.indexOf("function applyStreamEvent(event) {");
const end = source.indexOf("\nfunction connectMarketStream()", start);
assert.notEqual(start, -1, "frontend trade handler exists");
assert.notEqual(end, -1, "frontend trade handler is bounded");

const series = [];
const chart = { config: { type: "line" }, data: { datasets: [{ data: series }] }, update(mode) { this.updateMode = mode; } };
const state = { symbol: "AAPL", market: "us", currentPrice: 0, chart };
const elements = { stockPrice: { textContent: "" }, updateBadge: { textContent: "" }, statusMessage: { textContent: "" } };
const context = {
    state,
    elements,
    formatPrice: (price, currency) => `${currency} ${price.toFixed(2)}`,
    event: JSON.parse(fs.readFileSync(0, "utf8")),
    Number,
    Date,
};
vm.runInNewContext(`${source.slice(start, end)}\napplyStreamEvent(event);`, context);

assert.ok(state.currentPrice > 0, "last trade reaches app state");
assert.match(elements.stockPrice.textContent, /^USD /, "last trade updates displayed price");
assert.equal(elements.updateBadge.textContent, "Stream trade");
assert.equal(series.length, 1, "trade is appended to chart series");
assert.equal(chart.updateMode, "none", "chart updates without full animation");
console.log("frontend_trade_update: PASS");
