// Optional frontend checks: node tests/test_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/index.html', 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Parse the entire frontend without touching real devices.
const fn = script.match(/function autoKind\(\)\{[^\n]+/)[0];
const context = {data: {morning_done: false, night_done: false}};
vm.createContext(context);
vm.runInContext(fn, context);
assert.equal(vm.runInContext('autoKind()', context), 'usage');
context.data.fajr = {state: 'invalid'};
context.data.isha = {state: 'invalid'};
assert.equal(vm.runInContext('autoKind()', context), 'usage');
context.data.fajr = {state: new Date(Date.now() - 3600000).toISOString()};
context.data.isha = {state: new Date(Date.now() + 3600000).toISOString()};
assert.equal(vm.runInContext('autoKind()', context), 'usage');
context.data.morning_done = true;
assert.equal(vm.runInContext('autoKind()', context), 'usage');
context.data.isha = {state: new Date(Date.now() - 1000).toISOString()};
assert.equal(vm.runInContext('autoKind()', context), 'usage');
context.data.night_done = true;
assert.equal(vm.runInContext('autoKind()', context), 'usage');
assert.ok(!script.includes('boys_room'));
assert.ok(script.includes('/api/room/toggle'));
console.log('Frontend parsing and widget selection: PASS');
