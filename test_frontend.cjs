'use strict';
// Exercise actual page functions with a small DOM/canvas stand-in, no browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Parse the whole page, including its polling code.
const functions = script.slice(script.indexOf('function connectionPeaks('), script.indexOf('function render('));
const format = script.split('\n').find(line => line.startsWith('const format='));
const nodes = new Map();
const drawing = new Proxy({}, {get: () => () => {}, set: () => true});
function element() {
  return {
    children: [], textContent: '', className: '',
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    getBoundingClientRect() { return {width: 1000, height: 250}; },
    getContext() { return drawing; }
  };
}
const view = {
  now: 1000, window: 120, paused: false, hover: null, links: [],
  history: [
    {t: 600, down: 900, up: 200, links: [{id: 'wan-a', name: 'WAN A', down: 300, up: 55}]},
    {t: 990, down: 280, up: 65, links: [{id: 'wan-a', name: 'WAN A', down: 180, up: 20}, {id: 'wan-b', name: 'WAN B', down: 100, up: 65}]},
    {t: 1000, down: null, up: null, links: [{id: 'wan-a', name: 'WAN A', down: 0, up: 0}]}
  ]
};
const context = vm.createContext({
  view, document: {createElement: element}, window: {devicePixelRatio: 1},
  $: id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); }
});
vm.runInContext(format + '\n' + functions, context);
const run = code => vm.runInContext(code, context);
assert.equal(run("connectionPeaks().get('wan-a').down"), 180);
assert.equal(run("connectionPeaks().get('wan-b').up"), 65);
run("chart('trafficChart',['down','up'],['green','purple'],'Mbps')");
assert.match(nodes.get('peak').textContent, /280[.,]0 Mbps.*65[.,]0 Mbps/);
view.window = 900;
assert.equal(run("connectionPeaks().get('wan-a').down"), 300);
run("chart('trafficChart',['down','up'],['green','purple'],'Mbps')");
assert.match(nodes.get('peak').textContent, /900[.,]0 Mbps.*200[.,]0 Mbps/);
view.window = 120;
view.links = [
  {id: 'wan-a', name: 'WAN A', connected: true, down: 0, up: 0, ping: 50},
  {id: 'unused', name: 'Unused', connected: false, down: 0, up: 0, ping: null}
];
run('renderLinks()');
const cards = JSON.stringify(nodes.get('links'));
assert.match(cards, /180[.,]0 Mbps/);
assert.match(cards, /65[.,]0 Mbps/);
assert.match(cards, /No fresh data/);
assert.match(cards, /Disconnected/);
assert.match(cards, /— Mbps/);
assert.equal(nodes.get('links').children.length, 3);
view.paused = true;
view.pauseEnd = 1000;
view.pausedHistory = view.history.slice();
view.now = 2000;
view.history = [{t: 2000, down: 900, up: 800, links: [{id: 'wan-a', down: 900, up: 800}]}];
assert.equal(run("connectionPeaks().get('wan-a').down"), 180);
view.paused = false;
assert.equal(run("connectionPeaks().get('wan-a').down"), 900);
view.history = [{t: 2000, down: 0, up: null, links: [{id: 'wan-a', down: 0, up: null}]}];
assert.equal(run("connectionPeaks().get('wan-a').down"), 0);
assert.equal(run("connectionPeaks().get('wan-a').up"), null);
run("chart('trafficChart',['down','up'],['green','purple'],'Mbps')");
assert.match(nodes.get('peak').textContent, /0[.,]0 Mbps.*— Mbps/);
view.history = [{t: 2000}];
assert.equal(run('connectionPeaks().size'), 0);
console.log('Frontend checks passed: selected windows, combined/link peaks, idle/disconnected cards, pause, and unavailable samples.');
