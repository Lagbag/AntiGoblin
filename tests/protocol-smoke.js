#!/usr/bin/env node
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const appPath = path.join(__dirname, '..', 'ui', 'xkeen-manager', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');

const parserStart = source.indexOf('function createDefaultProxyConfig() {');
const parserEnd = source.indexOf('// Build xray outbound `streamSettings`', parserStart);
const builderStart = source.indexOf('function usesSingboxProxy(config) {');
const builderEnd = source.indexOf('function extractProxyConfig(doc) {', builderStart);
if (parserStart < 0 || parserEnd < 0 || builderStart < 0 || builderEnd < 0) {
  throw new Error('failed to locate protocol function regions in app.js');
}

const names = [
  'createDefaultProxyConfig', 'normalizeProxyConfig',
  'parseVlessUri', 'parseVmessUri', 'parseHysteria2Uri',
  'safeDecodeURIComponent', 'decodeBase64UrlText', 'queryValue', 'queryBool',
  'parseBandwidthMbps', 'parseGenericShareUrl', 'commonTlsConfig', 'shareSecret',
  'parseTrojanUri', 'parseShadowsocksUri', 'parseTuicUri', 'parseAnyTlsUri',
  'parseSocksUri', 'parseHttpProxyUri', 'parseSshUri', 'parseNaiveUri',
  'parseHysteriaUri', 'parseProxyUri', 'singboxEndpointToProxyConfig', 'singboxOutboundToProxyConfig',
  'parseSingboxJsonSubscription', 'parseSubscriptionText',
  'usesSingboxProxy', 'buildSingboxTls', 'buildSingboxV2rayTransport',
  'buildSingboxProxyEndpoint', 'buildSingboxProxyOutbound'
];
const program = source.slice(parserStart, parserEnd) + '\n\n' +
  source.slice(builderStart, builderEnd) + '\n\n' +
  `globalThis.api = { ${names.join(', ')} };`;
const context = vm.createContext({
  URL,
  URLSearchParams,
  atob,
  console
});
vm.runInContext(program, context, { filename: 'app-protocol-functions.js' });
const api = context.api;

const b64url = (text) => Buffer.from(text).toString('base64').replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
const vmess = Buffer.from(JSON.stringify({
  v: '2', ps: 'vmess-test', add: 'vm.example', port: '443', id: '11111111-1111-1111-1111-111111111111',
  aid: '0', net: 'ws', type: 'none', host: 'cdn.example', path: '/ws', tls: 'tls', sni: 'vm.example'
})).toString('base64');

const cases = [
  ['vless', 'vless://11111111-1111-1111-1111-111111111111@vl.example:443?security=reality&type=tcp&pbk=pub&sni=front.example&sid=01#vless'],
  ['vmess', `vmess://${vmess}`],
  ['hysteria2', 'hysteria2://secret@hy2.example:443?sni=hy2.example&obfs=salamander&obfs-password=mask&upmbps=20&downmbps=100#hy2'],
  ['hysteria', 'hysteria://secret@hy.example:443?peer=hy.example&upmbps=20&downmbps=100&obfs=mask#hy1'],
  ['trojan', 'trojan://secret@tr.example:443?security=tls&sni=tr.example&type=ws&path=%2Fws&host=cdn.example#trojan'],
  ['shadowsocks', `ss://${b64url('aes-256-gcm:secret')}@ss.example:8388#ss`],
  ['tuic', 'tuic://11111111-1111-1111-1111-111111111111:secret@tuic.example:443?sni=tuic.example&congestion_control=bbr&udp_relay_mode=native#tuic'],
  ['anytls', 'anytls://secret@any.example:443?sni=any.example#anytls'],
  ['socks', 'socks5://user:pass@socks.example:1080#socks'],
  ['http', 'https://user:pass@proxy.example:8443#https-connect'],
  ['ssh', 'ssh://root:secret@ssh.example:22#ssh'],
  ['naive', 'naive+https://user:secret@naive.example:443?sni=naive.example#naive']
];

for (const [protocol, uri] of cases) {
  const parsed = api.parseProxyUri(uri);
  assert.equal(parsed.ok, true, `${protocol}: ${parsed.error || 'parse failed'}`);
  assert.equal(parsed.config.protocol, protocol);
  assert.ok(parsed.config.address);
  assert.ok(parsed.config.port > 0);
  if (!['vless', 'vmess'].includes(protocol)) {
    const outbound = api.buildSingboxProxyOutbound(api.normalizeProxyConfig(parsed.config));
    assert.equal(outbound.tag, 'proxy');
    const expectedType = protocol === 'shadowsocks' ? 'shadowsocks' : protocol;
    assert.equal(outbound.type, expectedType);
    assert.equal(outbound.server, parsed.config.address);
    assert.equal(outbound.server_port, parsed.config.port);
  }
}

const sub = api.parseSubscriptionText(cases.map(([, uri]) => uri).join('\n'));
assert.equal(sub.errors.length, 0, JSON.stringify(sub.errors));
assert.equal(sub.configs.length, cases.length);
assert.deepEqual(Array.from(sub.configs, (x) => x.protocol), cases.map(([p]) => p));

const hy2 = api.buildSingboxProxyOutbound(api.normalizeProxyConfig(api.parseProxyUri(cases[2][1]).config));
assert.equal(hy2.tls.certificate_pin_sha256, undefined, 'obsolete TLS pin field must not be emitted');
assert.equal(hy2.obfs.type, 'salamander');
assert.equal(hy2.up_mbps, 20);
assert.equal(hy2.down_mbps, 100);

const hiddifyJson = JSON.stringify({
  outbounds: [
    { type: 'direct', tag: 'direct' },
    {
      type: 'vless',
      tag: 'hiddify-xhttp',
      server: 'xhttp.example',
      server_port: 443,
      uuid: '22222222-2222-2222-2222-222222222222',
      flow: '',
      tls: {
        enabled: true,
        server_name: 'front.example',
        utls: { enabled: true, fingerprint: 'chrome' },
        reality: { enabled: true, public_key: 'provider-key', short_id: 'ab' }
      },
      transport: {
        type: 'xhttp',
        path: '/x',
        mode: 'stream-one',
        extra: { xPaddingBytes: '100-1000' }
      }
    }
  ]
});
const jsonSub = api.parseSubscriptionText(hiddifyJson);
assert.equal(jsonSub.errors.length, 0, JSON.stringify(jsonSub.errors));
assert.equal(jsonSub.configs.length, 1);
assert.equal(jsonSub.configs[0].engine, 'singbox');
assert.equal(jsonSub.configs[0].protocol, 'vless');
assert.equal(api.usesSingboxProxy(jsonSub.configs[0]), true, 'raw sing-box vless must stay on sing-box');
const rawOutbound = api.buildSingboxProxyOutbound(jsonSub.configs[0]);
assert.equal(rawOutbound.tag, 'proxy');
assert.equal(rawOutbound.transport.type, 'xhttp');
assert.equal(rawOutbound.transport.mode, 'stream-one');
assert.equal(rawOutbound.transport.extra.xPaddingBytes, '100-1000');

const linkedJson = JSON.stringify({
  outbounds: [{ type: 'trojan', tag: 'linked', server: 'tr.example', server_port: 443, password: 'x', detour: 'warp' }]
});
const linkedSub = api.parseSubscriptionText(linkedJson);
assert.equal(linkedSub.configs.length, 0);
assert.equal(linkedSub.errors.length, 1);

const directDetourJson = JSON.stringify({
  outbounds: [
    { type: 'direct', tag: 'provider-direct' },
    { type: 'trojan', tag: 'tr', server: 'tr.example', server_port: 443, password: 'x', detour: 'provider-direct', tls: { enabled: true, server_name: 'tr.example' } }
  ]
});
const directDetourSub = api.parseSubscriptionText(directDetourJson);
assert.equal(directDetourSub.errors.length, 0, JSON.stringify(directDetourSub.errors));
assert.equal(directDetourSub.configs.length, 1);
assert.equal(directDetourSub.configs[0].singboxOutbound.detour, 'direct');

const wireguardJson = JSON.stringify({
  endpoints: [
    {
      type: 'wireguard',
      tag: 'wg-provider',
      address: ['10.88.0.2/32'],
      private_key: 'private-key',
      peers: [
        {
          address: 'wg.example',
          port: 51820,
          public_key: 'public-key',
          allowed_ips: ['0.0.0.0/0', '::/0']
        }
      ]
    }
  ]
});
const wireguardSub = api.parseSubscriptionText(wireguardJson);
assert.equal(wireguardSub.errors.length, 0, JSON.stringify(wireguardSub.errors));
assert.equal(wireguardSub.configs.length, 1);
assert.equal(wireguardSub.configs[0].protocol, 'wireguard');
assert.equal(wireguardSub.configs[0].address, 'wg.example');
assert.equal(wireguardSub.configs[0].port, 51820);
assert.equal(api.usesSingboxProxy(wireguardSub.configs[0]), true);
const wgEndpoint = api.buildSingboxProxyEndpoint(wireguardSub.configs[0]);
assert.equal(wgEndpoint.tag, 'proxy');
assert.equal(wgEndpoint.peers[0].address, 'wg.example');
assert.equal(wgEndpoint.peers[0].port, 51820);

console.log(`PASS: parsed and built ${cases.length} URI protocols + raw Hiddify/sing-box outbounds/endpoints`);
