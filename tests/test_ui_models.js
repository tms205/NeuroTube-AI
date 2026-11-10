const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

const html = readFileSync(join(__dirname, '../gui_templates/index.html'), 'utf8');
function uiFunction(name) {
  const start = html.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `Missing UI function ${name}`);
  const end = html.slice(start + 1).search(/\n(?:async )?function /);
  return html.slice(start, end < 0 ? html.indexOf('</script>', start) : start + 1 + end);
}

function setup() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, style: {}, options: [],
      add(option) { this.options.push(option); },
      replaceChildren() { this.options = []; this.value = ''; },
    });
    return elements.get(id);
  };
  const calls = [];
  const logs = [];
  const running = [];
  const context = vm.createContext({
    document: { getElementById: element },
    Option: function (text, value) { Object.assign(this, { text, value }); },
    fetch: async (url, options) => {
      calls.push({ url, body: options && JSON.parse(options.body) });
      return { ok: true, json: async () => ({ ok: true, gemini_model: 'gemini-3.8-flash' }) };
    },
    _videoType: 'normal', clearConsole() {}, setRunning: value => running.push(value),
    _log: (...args) => logs.push(args), startSSE() {},
    _modelSelectionVersion: 0, _modelLoadRequest: 0,
  });
  element('panel-manual').style.display = 'none';
  element('s-model').value = 'gemini-3.8-flash';
  return { context, element, calls, logs, running };
}

test('Generate saves and submits the visible model without clicking Save Settings', async () => {
  const { context, calls } = setup();
  vm.runInContext(uiFunction('startGeneration'), context);
  await vm.runInContext('startGeneration()', context);
  assert.deepEqual(calls.map(call => call.url), ['/api/save-settings', '/api/run']);
  assert.equal(calls[0].body.gemini_model, 'gemini-3.8-flash');
  assert.equal(calls[1].body.gemini_model, 'gemini-3.8-flash');
});

test('Generate does not run when saving the selected model fails', async () => {
  const { context, calls, running } = setup();
  context.fetch = async url => {
    calls.push({ url });
    return { ok: false, json: async () => ({ ok: false, error: 'Save failed' }) };
  };
  vm.runInContext(uiFunction('startGeneration'), context);
  await vm.runInContext('startGeneration()', context);
  assert.deepEqual(calls.map(call => call.url), ['/api/save-settings']);
  assert.equal(running.at(-1), false);
});

test('A delayed models response preserves a newer user selection', async () => {
  const { context, element } = setup();
  let resolve;
  context.fetch = () => new Promise(done => { resolve = done; });
  vm.runInContext(uiFunction('loadModels'), context);
  const pending = vm.runInContext("loadModels(false, 'gemini-3.6-flash')", context);
  element('s-model').value = 'gemini-3.8-flash';
  context._modelSelectionVersion++;
  resolve({ ok: true, json: async () => ({ models: ['gemini-3.6-flash', 'gemini-3.8-flash'] }) });
  await pending;
  assert.equal(element('s-model').value, 'gemini-3.8-flash');
});

test('Model list failure still displays and keeps the saved selection', async () => {
  const { context, element } = setup();
  context.fetch = async () => { throw new Error('Offline'); };
  vm.runInContext(uiFunction('loadModels'), context);
  await vm.runInContext("loadModels(false, 'gemini-3.8-flash')", context);
  assert.equal(element('s-model').value, 'gemini-3.8-flash');
  assert.match(element('generation-model').textContent || '', /gemini-3\.8-flash/);
});
