// Run: node tests/test_clock_recovery.js [path/to/index.html]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(process.argv[2] || 'static/index.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
function functionSource(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  const body = script.indexOf('{', start);
  let depth = 1, end = body + 1;
  while (depth && end < script.length) {
    if (script[end] === '{') depth++;
    if (script[end] === '}') depth--;
    end++;
  }
  return (script.slice(start - 6, start) === 'async ' ? 'async ' : '') + script.slice(start, end);
}
async function main() {
  let requests = 0, release, timers = [];
  const ctx = vm.createContext({
    document: {visibilityState: 'visible'},
    navigator: {wakeLock: {request: async () => {
      requests++;
      return {addEventListener: (_, fn) => {release = fn;}};
    }}},
    setTimeout: (fn, ms) => {timers.push({fn, ms}); return timers.length;},
    clearTimeout: () => {}, console
  });
  vm.runInContext('let wakeLock=null,wakeLockPending=false,wakeLockRetryTimer=null;', ctx);
  vm.runInContext(functionSource('requestWakeLock'), ctx);
  await vm.runInContext('requestWakeLock()', ctx);
  assert.equal(requests, 1);
  release();
  assert.ok(timers.length, 'Released keep-awake lock must retry without a screen tap');
  await timers.shift().fn();
  assert.equal(requests, 2, 'Visible page should reacquire released lock');
  vm.runInContext('wakeLock=null', ctx);
  await Promise.all([vm.runInContext('requestWakeLock()', ctx), vm.runInContext('requestWakeLock()', ctx)]);
  assert.equal(requests, 3, 'Concurrent recovery events must not acquire duplicate locks');
  ctx.document.visibilityState = 'hidden';
  vm.runInContext('wakeLock=null', ctx);
  await vm.runInContext('requestWakeLock()', ctx);
  assert.equal(requests, 3, 'Hidden page must not acquire a lock');
  const handlers = {}, docHandlers = {};
  let painted = false;
  const recovery = vm.createContext({
    clearTimeout: () => {},
    $: () => ({classList: {remove() {}}}),
    activatePage() {}, render() {},
    safeClock: () => {painted = vm.runInContext('lastMinute === null', recovery);},
    requestWakeLock() {},
    document: {visibilityState: 'visible', addEventListener: (name, fn) => {docHandlers[name] = fn;}},
    window: {addEventListener: (name, fn) => {handlers[name] = fn;}}, console
  });
  vm.runInContext('let gestureEpoch=0,widgetTransitionTimer=null,drag=null,widgetBusy=false,suppressWidgetClick=false,pageDrag=null,pageBusy=false,pageIndex=0,lastMinute=123;', recovery);
  vm.runInContext(functionSource('recoverDashboard'), recovery);
  // Execute the real event registrations, not a duplicate of their behavior.
  const registrations = script.slice(script.indexOf("document.addEventListener('pointerdown',requestWakeLock"), script.indexOf('let weatherGesture=null;'));
  vm.runInContext(registrations, recovery);
  assert.equal(typeof handlers.focus, 'function', 'Focus must recover a clock whose timers were suspended');
  handlers.focus();
  assert.ok(painted, 'Resume must force a fresh nonanimated time repaint');
  painted = false;
  recovery.document.visibilityState = 'hidden';
  handlers.focus();
  assert.equal(painted, false, 'Hidden page focus must not recover the dashboard');
  console.log('Wake-lock release, concurrency, hidden-page, immediate resume repaint: PASS');
}
main().catch(error => {console.error(error); process.exitCode=1;});
