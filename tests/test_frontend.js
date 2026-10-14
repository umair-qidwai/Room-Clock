// Deterministic regression checks: node tests/test_frontend.js [test-name]
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('static/index.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(source);
function extract(name) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name);
  let end = source.indexOf('{', start) + 1, depth = 1;
  while (depth && end < source.length) {
    if (source[end] === '{') depth++;
    if (source[end] === '}') depth--;
    end++;
  }
  return (source.slice(start - 6, start) === 'async ' ? 'async ' : '') + source.slice(start, end);
}
function base(names, extra = {}) {
  const events = [], timers = [];
  const c = vm.createContext({Date, Intl, AbortController, Number, Promise,
    console: {error() {}, warn() {}}, events, timers,
    setTimeout: (fn, ms) => {timers.push({fn, ms}); return timers.length;}, clearTimeout() {},
    $: () => ({classList: {add() {}, remove() {}}, dataset: {}}),
    updateLight() {}, render() {}, activatePage() {}, clearStoredFlow() {}, toast() {},
    showWelcome(kind) {events.push(kind); vm.runInContext('flowKind=' + JSON.stringify(kind) + ';flowBusy=false;flowShownDate=flowDate()', c);},
    showMorningWeather() {vm.runInContext("flowKind='weather';flowBusy=false", c);},
    setFlowVisible() {vm.runInContext('flowKind=null;flowBusy=false', c);}, restoreFlow() {return false;},
    serverNow: () => new Date('2026-10-14T20:30:00'), flowDate: () => '2026-10-14', nightCycleDate:()=> '2026-10-14',
    currentDailyOccurrence: e => e ? new Date(e.state) : null, evaluateScheduledFlow() {}, ...extra});
  vm.runInContext(`let data={date:'2026-10-14',morning_done:false,night_done:false,morning_automation_configured:true,night_automation_configured:true,isha_iqama:{state:'2026-10-14T20:00:00'},shuruq:{state:'2026-10-14T07:00:00'}},flowKind=null,flowBusy=false,flowShownDate=null,morningNoonAttemptDate=null,flowMutation=0,scheduledFlowSuppressedDate=null,flowReconciledDate=null,refreshInFlight=false,serverClockOffset=0,widgetBusy=false,drag=null,pageBusy=false,pageDrag=null,lastWidgetRefresh=Date.now(),lastWeatherFetch=Date.now(),morningWeatherExpires=0,morningWeatherTimer=null,hourlyWeatherExpires=0,gestureEpoch=0;const ISHA_OFFSET_MS=1200000,REQUEST_TIMEOUT_MS=${source.match(/REQUEST_TIMEOUT_MS=(\d+)/)[1]};`, c);
  for (const n of names) vm.runInContext(extract(n), c);
  return c;
}
const tests = {};
const settle = () => new Promise(setImmediate);
const policy = () => ['timeParts','clockZone','shouldAutoRunMorningAtNoon','runMorningAtNoon','evaluateScheduledFlow', ...['requestJSON','runRoutine'].filter(n => source.includes('function '+n+'('))];
tests.noon = async () => {
  let posts = 0;
  const c = base(policy(), {fetch: async () => {posts++; throw Error('offline');}});
  vm.runInContext("evaluateScheduledFlow(new Date('2026-10-14T12:00:00'))", c); await settle();
  vm.runInContext("evaluateScheduledFlow(new Date('2026-10-14T20:30:00'))", c); await settle();
  assert.equal(c.events.at(-1), 'night', 'Failed noon attempt must not hide due night welcome');
  assert.equal(posts, 1, 'Uncertain morning POST must not retry automatically');
};
tests.deadline = async () => {
  for (const bodyHangs of [false, true]) {
    const c = base(['refresh', ...['requestJSON'].filter(n => source.includes('function '+n+'('))], {
      fetch: () => bodyHangs ? Promise.resolve({ok:true,json:()=>new Promise(()=>{})}) : new Promise(()=>{})});
    const pending = vm.runInContext('refresh()', c); await settle();
    assert.ok(c.timers.length, 'State fetch and body need a bounded deadline');
    await c.timers[0].fn(); await pending;
    assert.equal(vm.runInContext('refreshInFlight', c), false, 'Deadline releases refresh lock');
  }
};
// Advance deadlines and response delivery without wall-clock waits.
function fakeTimers() {
  let now=0, next=0;
  const pending=new Map();
  return {
    setTimeout(fn,ms){const id=++next;pending.set(id,{fn,at:now+ms});return id;},
    clearTimeout(id){pending.delete(id);},
    async advance(ms){
      const target=now+ms;
      while(true){
        const due=[...pending].filter(([,t])=>t.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];
        if(!due)break;
        now=due[1].at;pending.delete(due[0]);due[1].fn();await settle();
      }
      now=target;await settle();
    },
    get size(){return pending.size;}
  };
}
tests.slowPartialState = async () => {
  for(const delay of [9500,10000]){
    const timers=fakeTimers();let signal,lightUpdates=0;
    const c=base(['refresh','requestJSON'],{setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout,
      updateLight(){lightUpdates++;},fetch:(_url,options)=>{
        signal=options.signal;
        return new Promise(resolve=>timers.setTimeout(()=>resolve({ok:true,json:async()=>({
          date:'2026-10-14',night_date:'2026-10-14',room:{state:'on'},
          shuruq:{state:'2026-10-14T07:15:00-04:00'},usage:{codex:{used:12}},
          errors:{weather:'Integration unavailable'}
        })}),delay));
      }});
    const pending=vm.runInContext('refresh()',c);
    await timers.advance(delay);await pending;
    assert.equal(vm.runInContext('data.room?.state',c),'on','A slow failed integration must not discard healthy room state');
    assert.equal(vm.runInContext('data.usage.codex.used',c),12);
    assert.equal(vm.runInContext('data.shuruq.state',c),'2026-10-14T07:15:00-04:00');
    assert.equal(vm.runInContext('data.errors.weather',c),'Integration unavailable');
    assert.equal(lightUpdates,1);assert.equal(signal.aborted,false);
    assert.equal(vm.runInContext('refreshInFlight',c),false);
    assert.equal(timers.size,0,'Settled state cancels its request deadline');
  }
};
tests.hungStateBudget = async () => {
  for(const bodyHangs of [false,true]){
    const timers=fakeTimers();let signal;
    const c=base(['refresh','requestJSON'],{setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout,
      fetch:(_url,options)=>{signal=options.signal;return bodyHangs?Promise.resolve({ok:true,json:()=>new Promise(()=>{})}):new Promise(()=>{});}});
    const pending=vm.runInContext('refresh()',c);await settle();
    await timers.advance(10000);
    assert.equal(vm.runInContext('refreshInFlight',c),true,'State deadline must exceed the backend integration budget');
    await timers.advance(5000);await pending;
    assert.equal(signal.aborted,true);assert.equal(vm.runInContext('refreshInFlight',c),false);
    assert.equal(timers.size,0);
  }
};
tests.lightConfirmationBudget = async () => {
  const timers=fakeTimers();let signal,errors=0,now=0;
  const c=base(['confirmLight','requestJSON'],{setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout,
    Date:{now:()=>now},showLightError(){errors++;},fetch:(_url,options)=>{signal=options.signal;return new Promise(()=>{});}});
  const pending=vm.runInContext("confirmLight('on',0)",c);
  now=4999;await timers.advance(4999);assert.equal(signal.aborted,false);
  now=5000;await timers.advance(1);await pending;
  assert.equal(signal.aborted,true);assert.equal(errors,1);assert.equal(timers.size,0);
};
tests.mutation = async () => {
  for (const during of [false, true]) {
    let getResolve, postResolve;
    const c = base(['refresh','continueFlow', ...['requestJSON','runRoutine'].filter(n => source.includes('function '+n+'('))], {
      fetch: url => new Promise(resolve => {if(url==='/api/state')getResolve=resolve;else postResolve=resolve;})});
    vm.runInContext("flowKind='morning'", c);
    let get, post;
    if (during) {post=vm.runInContext('continueFlow()', c); get=vm.runInContext('refresh()', c);}
    else {get=vm.runInContext('refresh()', c); post=vm.runInContext('continueFlow()', c);}
    postResolve({ok:true,json:async()=>({morning_weather_until:1792000000})}); await post;
    getResolve({ok:true,json:async()=>({morning_done:false})}); await get;
    assert.equal(vm.runInContext('data.morning_done', c), true, 'Stale GET must not undo successful routine');
  }
};
tests.rollover = async () => {
  const c = base(policy(), {flowDate:()=> '2026-10-15', fetch:async()=>{throw Error('unexpected POST');}});
  vm.runInContext("flowKind='night';flowShownDate='2026-10-14';data.date='2026-10-15';data.shuruq.state='2026-10-15T07:00:00';data.isha_iqama.state='2026-10-15T20:00:00'", c);
  vm.runInContext("evaluateScheduledFlow(new Date('2026-10-15T07:01:00'))", c);
  assert.equal(c.events.at(-1), 'morning', 'Prior-day welcome must release fresh sunrise schedule');
};
tests.settlement = async () => {
  const el=()=>({classList:{add(){},remove(){},toggle(){}},style:{},setAttribute(){},removeAttribute(){},contains:()=>false});
  const pages=[el(),el()], timers=[],frames=[];
  const c=vm.createContext({pages,performance:{now:()=>100},requestAnimationFrame:f=>frames.push(f),setTimeout:(fn,ms)=>{timers.push(fn);return timers.length;},clearTimeout(){},$:()=>el(),document:{activeElement:null,body:el()},movePageDrag(){},pageDistance:()=>500,swipeCommits:()=>true,activatePage(){},render(){},fitTime(){},fitGauges(){},safeClock(){},requestWakeLock(){},evaluateScheduledFlow(){}});
  vm.runInContext('let gestureEpoch=0,pageIndex=1,pageBusy=false,pageDrag={id:1,pane:{hasPointerCapture:()=>false},dy:1000,startT:90,moved:true,current:pages[1],neighbor:pages[0]},widgetTransitionTimer=null,drag=null,widgetBusy=false,suppressWidgetClick=false,lastMinute=null;',c);
  vm.runInContext(extract('finishPageDrag')+'\n'+extract('recoverDashboard'),c);
  vm.runInContext('finishPageDrag({pointerId:1});recoverDashboard()',c);
  for(const f of frames)f();for(const f of timers)f();
  assert.equal(vm.runInContext('pageIndex',c),1,'Old settlement must not flip recovered page');
  assert.equal(pages[1].style.transform,undefined,'Old animation frame must not move recovered page');
};
tests.time = async () => {
  let painted;
  const c=base(['clock','serverNow',...['timeParts','clockZone','localDateKey','flowDate'].filter(n=>source.includes('function '+n+'('))], {document:{querySelectorAll:()=>[]},paintTime:v=>painted=v});
  vm.runInContext("data.timezone='America/New_York';serverClockOffset=Date.parse('2026-10-15T03:10:00Z')-Date.now();let lastMinute=null",c);
  vm.runInContext('clock()',c);
  assert.equal(painted,'11:10','Clock display must use server time and configured timezone');
};
tests.cached = async () => {
  let evaluated=0;
  const c=base(['safeClock'],{clock(){},evaluateScheduledFlow(){evaluated++;}});
  vm.runInContext('safeClock()',c);
  assert.equal(evaluated,1,'Every clock tick must evaluate cached schedule without GET');
};
tests.malformed = async () => {
  const c=base(['scheduledTime']);
  for(const state of ['25:99','12:80','bad','unknown','unavailable','']) {
    c.entity={state};assert.equal(vm.runInContext('scheduledTime(entity)',c),null,'Reject malformed '+state);
  }
};
tests.calendar = async () => {
  const c=base(['clockZone','timeParts','localDateKey','serverNow','flowDate','zonedTime','scheduledTime','currentDailyOccurrence','previousDate',...['nightCycleDate'].filter(n=>source.includes('function '+n+'('))]);
  vm.runInContext("data.timezone='America/New_York';serverClockOffset=Date.parse('2026-10-15T04:30:00Z')-Date.now();data.night_date='2026-10-14'",c);
  assert.equal(vm.runInContext('flowDate()',c),'2026-10-15');
  c.entity={state:'2026-10-15T20:00:00-04:00'};
  const night=vm.runInContext("currentDailyOccurrence(entity,typeof nightCycleDate==='function'?nightCycleDate():flowDate()).toISOString()",c);
  assert.equal(night,'2026-10-15T00:00:00.000Z','After midnight use prior evening iqama cycle');
  c.entity={state:'2026-10-14T07:15:00-04:00'};
  assert.equal(vm.runInContext('currentDailyOccurrence(entity).toISOString()',c),'2026-10-15T11:15:00.000Z','Cached previous day sunrise follows the new calendar day');
  c.entity={state:'07:15'};
  assert.equal(vm.runInContext('scheduledTime(entity).toISOString()',c),'2026-10-15T11:15:00.000Z');
};
tests.hourlyZone = async () => {
  const c=base(['hourlyItems',...['clockZone','timeParts','localDateKey'].filter(n=>source.includes('function '+n+'('))],{serverNow:()=>new Date('2026-10-15T03:30:00Z')});
  vm.runInContext("data.timezone='America/New_York';data.weather_hourly=[{datetime:'2026-10-15T03:45:00Z'},{datetime:'2026-10-15T04:00:00Z'}]",c);
  assert.equal(vm.runInContext('hourlyItems().length',c),1,'Today weather ends at configured timezone midnight');
};
tests.resumeDeadline = async () => {
  for(const kind of ['weather','hourly']){
    let dismissed=0;
    const c=base(policy(),{dismissWeather(){dismissed++;vm.runInContext('flowKind=null',c);}});
    vm.runInContext(`flowKind='${kind}';morningWeatherExpires=hourlyWeatherExpires=Date.parse('2026-10-14T10:00:00');data.morning_automation_configured=data.night_automation_configured=false`,c);
    vm.runInContext("evaluateScheduledFlow(new Date('2026-10-14T10:01:00'))",c);
    assert.equal(dismissed,1,'Resume must settle elapsed '+kind+' deadline immediately');
  }
};
tests.completed = async () => {
  const c=base(['refresh',...['requestJSON','mergeState'].filter(n=>source.includes('function '+n+'('))],{fetch:async()=>({ok:true,json:async()=>({date:'2026-10-14',morning_done:false,night_done:false})})});
  vm.runInContext('data.morning_done=true;data.night_done=true',c);
  await vm.runInContext('refresh()',c);
  assert.equal(vm.runInContext('data.morning_done&&data.night_done',c),true,'A same-cycle GET cannot revert completion');
};
tests.widgetSettlement = async () => {
  const el=()=>({classList:{add(){},remove(){},toggle(){}},style:{},hasPointerCapture:()=>false});
  const timers=[],frames=[],rail=el(),current=el(),neighbor=el();
  const c=base(['finishWidgetDrag','swipeCommits','recoverDashboard'],{setTimeout:fn=>{timers.push(fn);return timers.length;},requestAnimationFrame:fn=>frames.push(fn),$:()=>rail,performance:{now:()=>100},dragDistance:()=>500,widgetList:()=>['usage','summary'],safeClock(){},requestWakeLock(){}});
  c.current=current;c.neighbor=neighbor;
  vm.runInContext('let pageIndex=1,widgetTransitionTimer=null,lastMinute=null,manualIndex=null,manualUntil=0,suppressWidgetClick=false;drag={id:1,startY:0,startT:90,dy:-1000,moved:true,index:0,current,neighbor};',c);
  vm.runInContext('finishWidgetDrag({pointerId:1,clientY:-1000});recoverDashboard()',c);
  for(const f of frames)f();for(const f of timers)f();
  assert.equal(vm.runInContext('manualIndex',c),null,'Old widget settlement must not change recovered widget');
  assert.equal(current.style.transform,undefined,'Old widget frame must not move recovered widget');
};
tests.sunriseOverNight = async () => {
  const c=base([...policy(),'clockZone','timeParts','localDateKey','serverNow','flowDate','previousDate','nightCycleDate','zonedTime','scheduledTime','currentDailyOccurrence']);
  vm.runInContext("data.timezone='America/New_York';serverClockOffset=Date.parse('2026-10-15T11:01:00Z')-Date.now();data.date='2026-10-15';data.night_date='2026-10-14';data.shuruq.state='2026-10-16T07:00:00-04:00';data.isha_iqama.state='2026-10-15T20:00:00-04:00';flowKind='night';flowShownDate='2026-10-14'",c);
  vm.runInContext('evaluateScheduledFlow()',c);
  assert.equal(c.events.at(-1),'morning','Fresh sunrise replaces an uncompleted previous night, before noon');
};
tests.cachedRollover = async () => {
  const c=base([...policy(),'clockZone','timeParts','localDateKey','serverNow','flowDate','previousDate','nightCycleDate','zonedTime','scheduledTime','currentDailyOccurrence']);
  vm.runInContext("data.timezone='America/New_York';serverClockOffset=Date.parse('2026-10-15T11:01:00Z')-Date.now();data.date='2026-10-14';data.night_date='2026-10-14';data.morning_done=true;data.night_done=true;data.shuruq.state='2026-10-15T07:00:00-04:00';data.isha_iqama.state='2026-10-15T20:00:00-04:00'",c);
  vm.runInContext('evaluateScheduledFlow()',c);
  assert.equal(c.events.at(-1),'morning','Cached completed yesterday state must allow fresh sunrise');
  assert.equal(vm.runInContext('data.night_done',c),true,'After midnight preserve previous evening completion until noon');
};
tests.weatherParity = async () => {
  const grid={scrollLeft:0},overlay={dataset:{},setAttribute(){},querySelector:()=>grid},saved=[];
  const c=base(['openHourlyWeather','showMorningWeather',...['showWeather'].filter(n=>source.includes('function '+n+'('))],{
    $:sel=>sel.includes('hourly-grid')?grid:overlay,hourlyWeatherCard:()=>'<div class="hourly-grid">forecast</div>',
    saveFlow:(...args)=>saved.push(args),serverNow:()=>new Date(1000),dismissWeather(){},finishMorningWeather(){}});
  vm.runInContext('const MORNING_WEATHER_MS=120000;',c);
  vm.runInContext("flowShownDate='yesterday'",c);
  vm.runInContext('openHourlyWeather()',c);const manual=overlay.innerHTML;
  assert.equal(vm.runInContext('flowShownDate',c),null,'A manual view must not inherit a prior welcome date');
  assert.equal(c.timers.at(-1).ms,120000);
  grid.scrollLeft=75;vm.runInContext('openHourlyWeather(hourlyWeatherExpires)',c);
  assert.equal(grid.scrollLeft,75);
  vm.runInContext('showMorningWeather()',c);
  assert.equal(overlay.innerHTML,manual,'Both entry paths use identical weather layout');
  assert.deepEqual(saved.at(-1),['weather',121000]);
  assert.equal(c.timers.at(-1).ms,120000);
};
tests.noonReload = async () => {
  const storage=new Map();let posts=0;
  const c=base(policy(),{localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},fetch:async()=>{posts++;throw Error('outcome unknown');}});
  vm.runInContext("const FLOW_STORAGE='room-clock-flow-v1';",c);
  await vm.runInContext('runMorningAtNoon()',c);
  vm.runInContext('morningNoonAttemptDate=null',c);
  await vm.runInContext('runMorningAtNoon()',c);
  assert.equal(posts,1,'Reload must not automatically repeat uncertain noon POST');
};
tests.cachedBeforeSunrise = async () => {
  const c=base([...policy(),'clockZone','timeParts','localDateKey','serverNow','flowDate','previousDate','nightCycleDate','zonedTime','scheduledTime','currentDailyOccurrence']);
  vm.runInContext("data.timezone='America/New_York';serverClockOffset=Date.parse('2026-10-15T05:00:00Z')-Date.now();data.date='2026-10-14';data.night_date='2026-10-14';data.morning_done=data.night_done=true;data.shuruq.state='2026-10-14T07:00:00-04:00';data.isha_iqama.state='2026-10-14T20:00:00-04:00'",c);
  vm.runInContext('evaluateScheduledFlow()',c);
  assert.equal(c.events.length,0,'Cached old sunrise must not trigger before this day sunrise');
};
tests.routineParity = async () => {
  for(const [kind,automatic] of [['morning',false],['morning',true],['night',false]]){
    let posts=0,resolve,page=null;
    const c=base(['runRoutine','requestJSON'],{activatePage:i=>page=i,fetch:()=>{posts++;return new Promise(r=>resolve=r);}});
    c.kind=kind;c.automatic=automatic;
    const first=vm.runInContext('runRoutine(kind,automatic)',c);
    await vm.runInContext('runRoutine(kind,automatic)',c);
    assert.equal(posts,1,'Routine contacts are single-flight');
    resolve({ok:true,json:async()=>({morning_weather_until:1792000000})});await first;
    assert.equal(vm.runInContext('data[kind+"_done"]',c),true);
    if(kind==='morning'&&!automatic)assert.equal(vm.runInContext('flowKind',c),'weather');
    else assert.equal(page,1,'Night and noon success use clock-only');
  }
};
tests.lightParity = async () => {
  let posts=0,gets=0;
  const c=base(['toggleLight','confirmLight','requestJSON'],{fetch:async(url)=>{
    if(url==='/api/room/toggle'){posts++;return {ok:true,json:async()=>({requested_state:'on'})};}
    gets++;return {ok:true,json:async()=>({room:{state:'on'}})};
  },showLightError:()=>assert.fail('Unexpected light error')});
  vm.runInContext("let pendingExpected=null,lightError=false;data.room={state:'off'}",c);
  await vm.runInContext('toggleLight()',c);await settle();
  assert.equal(posts,1);assert.equal(gets,1);
  assert.equal(vm.runInContext('pendingExpected',c),null);
  assert.equal(vm.runInContext('data.room.state',c),'on');
};
tests.weatherKeepsContact = async () => {
  const c=base(policy());
  vm.runInContext("flowKind='hourly';hourlyWeatherExpires=Date.parse('2026-10-14T20:32:00');morningNoonAttemptDate=flowDate()",c);
  vm.runInContext("evaluateScheduledFlow(new Date('2026-10-14T20:30:00'))",c);
  assert.equal(vm.runInContext('flowKind',c),'hourly','Due night notice must not interrupt weather browsing');
};
async function main() {
  for (const [name, test] of Object.entries(tests)) {
    if (process.argv[2] && process.argv[2] !== name) continue;
    await test(); console.log(name + ': PASS');
  }
}
main().catch(e => {console.error(e); process.exitCode = 1;});
