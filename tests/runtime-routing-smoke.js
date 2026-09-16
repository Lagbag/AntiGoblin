#!/usr/bin/env node
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const appPath = path.join(__dirname, '..', 'ui', 'xkeen-manager', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');

function sliceFunction(name, nextMarker) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(nextMarker, start);
  if (start < 0 || end < 0) throw new Error(`cannot locate ${name}`);
  return source.slice(start, end);
}

const program = [
  sliceFunction('buildRoutingDocument', 'function updateGroup('),
  sliceFunction('createDefaultAutoSelectConfig', 'function createDefaultProxyConfig('),
  sliceFunction('uniq', 'function formatMessage('),
  'globalThis.api = { buildRoutingDocument, createDefaultAutoSelectConfig, normalizeAutoSelectConfig, uniq };'
].join('\n\n');

const context = vm.createContext({ console });
vm.runInContext(program, context, { filename: 'runtime-routing-functions.js' });
const api = context.api;

const doc = api.buildRoutingDocument({
  domainStrategy: 'AsIs',
  fallbackOutbound: 'vless-reality',
  groups: [
    { enabled: true, outboundTag: 'vless-reality', domains: ['2ip.io', 'api.ipify.org'], cidrs: [] },
    { enabled: true, outboundTag: 'vless-reality', domains: ['chatgpt.com', 'openai.com', 'claude.ai'], cidrs: [] }
  ]
});
assert.equal(doc.routing.domainStrategy, 'AsIs');
assert.equal(doc.routing.rules.at(-1).outboundTag, 'vless-reality');
assert.deepEqual(Array.from(doc.routing.rules.at(-1).inboundTag), ['redirect']);
assert.equal(Object.hasOwn(doc.routing.rules.at(-1), 'domain'), false);
assert.equal(Object.hasOwn(doc.routing.rules.at(-1), 'ip'), false);

const defaults = api.createDefaultAutoSelectConfig();
assert.equal(defaults.enabled, true);
assert.equal(defaults.intervalSec, 300);
assert.equal(defaults.minImprovementMs, 0);
assert.equal(defaults.version, 2);

const migrated = api.normalizeAutoSelectConfig({ enabled: true, intervalSec: 300, minImprovementMs: 8 });
assert.equal(migrated.version, 2);
assert.equal(migrated.minImprovementMs, 0, 'v1 8ms default should migrate to strict-lowest mode');
const explicitV2 = api.normalizeAutoSelectConfig({ version: 2, enabled: true, intervalSec: 300, minImprovementMs: 25 });
assert.equal(explicitV2.minImprovementMs, 25, 'v2 user threshold should be preserved');

console.log('PASS: catch-all VPN routing + auto-select v2 defaults/migration');
