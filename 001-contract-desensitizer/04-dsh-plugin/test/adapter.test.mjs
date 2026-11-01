import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { RedactorService, validateReady } from '../service.js';
import { LocalRuntime, dataRoot, findPython } from '../runtime.js';
import { makeRoute, apply, inject } from '../index.js';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const ready = { type: 'ready', revision: 'r99', url: 'http://127.0.0.1:12345/' };
test('macOS首次准备选择可安装独立扩展的系统Python', { skip: process.platform !== 'darwin' }, async () => {
  const python = await findPython(undefined, { ...process.env, DSH_REDACTOR_PYTHON: '' });
  assert.ok(python.startsWith('/'));
  assert.ok(!python.includes('DeepSeek Harness.app/'));
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function worker(value = ready) {
  const closed = deferred();
  return { ready: Promise.resolve(value), closed: closed.promise, stopCount: 0,
    stop() { this.stopCount++; closed.resolve(); return Promise.resolve(); },
    exit() { closed.resolve(); } };
}

test('只接受本机固定就绪记录，数据目录独立于代码', () => {
  assert.equal(validateReady(ready), ready.url);
  for (const url of ['https://example.com/', 'http://localhost:12345/', 'http://127.0.0.1:1/?path=/private', 'http://user@127.0.0.1:3/']) {
    assert.throws(() => validateReady({ ...ready, url }));
  }
  assert.throws(() => validateReady({ ...ready, revision: 'r93' }));
  assert.equal(dataRoot({ DSH_HOME: '/fixture/dsh' }), '/fixture/dsh/document-redactor');
});

test('并发启动只准备和启动一个进程', async () => {
  let preparations = 0, launches = 0;
  const service = new RedactorService({ prepare: async () => { preparations++; return 'fake-python'; }, launch: () => { launches++; return worker(); } });
  service.start(); service.start(); service.start();
  await service.pending;
  assert.equal(service.snapshot().stage, 'ready');
  assert.equal(preparations, 1); assert.equal(launches, 1);
  await service.stop();
});

test('准备失败可见且可以重试，不返回底层路径或报错原文', async () => {
  let fail = true;
  const service = new RedactorService({ prepare: async () => { if (fail) throw new Error('/private/customer/secret.txt'); return 'fake'; }, launch: () => worker() });
  service.start(); await service.pending;
  assert.equal(service.snapshot().error, 'PREPARATION_FAILED');
  assert.ok(!JSON.stringify(service.snapshot()).includes('/private'));
  fail = false; service.start(); await service.pending;
  assert.equal(service.snapshot().stage, 'ready');
  await service.stop();
});

test('准备期间重复停止合并为一次，迟到结果不能重新启动', async () => {
  const preparation = deferred(); let launches = 0;
  const service = new RedactorService({ prepare: () => preparation.promise, launch: () => { launches++; return worker(); } });
  service.start();
  const stopped = service.stop();
  assert.equal(service.stop(), stopped);
  assert.equal(service.start().stage, 'stopping');
  preparation.resolve('fake'); await stopped;
  assert.equal(launches, 0); assert.equal(service.snapshot().stage, 'idle');
});

test('伪造就绪地址会清理本插件子进程', async () => {
  const owned = worker({ ...ready, url: 'https://untrusted.example/' });
  const service = new RedactorService({ prepare: async () => 'fake', launch: () => owned });
  service.start(); await service.pending;
  assert.equal(service.snapshot().stage, 'error');
  assert.equal(service.snapshot().url, null); assert.equal(owned.stopCount, 1);
});

test('服务意外退出后不保留可用状态，停止只作用于自有进程', async () => {
  const owned = worker();
  const service = new RedactorService({ prepare: async () => 'fake', launch: () => owned });
  service.start(); await service.pending; owned.exit(); await owned.closed; await Promise.resolve();
  assert.equal(service.snapshot().error, 'SERVICE_STOPPED');
  assert.equal(service.snapshot().url, null);
  await service.stop(); assert.equal(owned.stopCount, 1);
});

test('停止失败保留进程引用并提示重试，不允许另起服务', async () => {
  let fail = true, launches = 0;
  const owned = worker();
  const stop = owned.stop.bind(owned);
  owned.stop = async () => { if (fail) throw Object.assign(new Error('kill denied'), { code: 'EPERM' }); return stop(); };
  const service = new RedactorService({ prepare: async () => 'fake', launch: () => { launches++; return owned; } });
  service.start(); await service.pending;
  await assert.rejects(service.stop());
  assert.equal(service.snapshot().error, 'STOP_FAILED');
  assert.equal(service.worker, owned);
  service.start(); assert.equal(launches, 1);
  fail = false; await service.stop();
  assert.equal(service.snapshot().stage, 'idle'); assert.equal(service.worker, null);
});

test('真实宿主运行时停止与取消合并，不能卡在停止中', { timeout: 15000 }, async () => {
  assert.ok(process.env.DSH_TEST_PYTHON);
  const data = await mkdtemp(join(tmpdir(), 'dsh-stop-directed-'));
  const runtime = new LocalRuntime(data);
  runtime.prepare = async () => process.env.DSH_TEST_PYTHON;
  const service = new RedactorService(runtime);
  try {
    service.start(); await service.pending;
    assert.equal(service.snapshot().stage, 'ready');
    const url = service.snapshot().url;
    const first = service.stop(); assert.equal(first, service.stop());
    await first;
    assert.equal(service.snapshot().stage, 'idle');
    await assert.rejects(fetch(url + 'health', { signal: AbortSignal.timeout(1000) }));
  } finally { await service.stop(); await rm(data, { recursive: true, force: true }); }
});

test('宿主管道关闭后本机工具自行退出，不能遗留监听端口', { timeout: 10000 }, async () => {
  assert.ok(process.env.DSH_TEST_PYTHON);
  const data = await mkdtemp(join(tmpdir(), 'dsh-owner-exit-'));
  const child = spawn(process.env.DSH_TEST_PYTHON,
    [join(root, 'runtime.py'), 'serve', '--data-root', data],
    { stdio: ['pipe', 'pipe', 'pipe'] });
  const closed = new Promise(resolve => child.once('exit', resolve));
  const lines = createInterface({ input: child.stdout });
  let deadline;
  try {
    const value = await new Promise((resolve, reject) => {
      deadline = setTimeout(() => reject(new Error('NO_READY')), 5000);
      child.once('error', reject);
      lines.on('line', line => { try { const value = JSON.parse(line); if (value.type === 'ready') resolve(value); } catch {} });
    });
    clearTimeout(deadline);
    const url = validateReady(value);
    assert.equal((await fetch(url + 'health')).status, 200);
    child.stdin.end();
    await Promise.race([closed, new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('OWNER_EXIT_NOT_HANDLED')), 3000); })]);
    assert.equal(child.exitCode, 0);
    await assert.rejects(fetch(url + 'health', { signal: AbortSignal.timeout(1000) }));
  } finally {
    clearTimeout(deadline); lines.close();
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    await closed; await rm(data, { recursive: true, force: true });
  }
});

test('控制接口限制方法、体积、动作和参数，并继承宿主连接入口', async () => {
  const calls = [];
  const service = { snapshot: () => ({ stage: 'idle' }), start: () => { calls.push('start'); return { stage: 'preparing' }; }, stop: async () => ({ stage: 'idle' }) };
  const route = makeRoute(service);
  const envelope = payload => ({ type: 'client-request', method: 'document.redactor', rpcId: 'test-rpc', payload });
  const request = body => new Request('http://localhost/api/document.redactor', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  let response = await route.fetch(request(envelope({ action: 'start' })));
  const result = await response.json(); assert.equal(result.rpcId, 'test-rpc'); assert.equal(result.result.value.stage, 'preparing');
  assert.equal((await route.fetch(request(envelope({ action: 'start', path: '/arbitrary' })))).status, 400);
  assert.equal((await route.fetch(request(envelope({ action: 'exec' })))).status, 400);
  assert.equal((await route.fetch(request({ value: 'x'.repeat(17000) }))).status, 413);
  assert.equal((await route.fetch(new Request('http://localhost/', { method: 'POST', body: '{}' }))).status, 415);
  assert.equal(calls.length, 1); assert.deepEqual(inject, ['connection', 'agents', 'agentDefaultModel']);
  let registered, dispose;
  apply({ connection: { fetch: { register(value) { registered = value; } } }, on(event, callback) { assert.equal(event, 'dispose'); dispose = callback; } });
  assert.equal(registered.path, '/api/document.redactor'); await dispose();
});

test('客户端注册侧栏与主面板，不向聊天或模型注册读取原文的工具', async () => {
  let client;
  vm.runInNewContext(await readFile(join(root, 'client.js'), 'utf8'), { window: { __ModuleLoader__: { load(config) { client = config.factory(name => { assert.equal(name, 'react'); return { createElement() {} }; }); } } } });
  const entries = [];
  client.apply({ connection: { rpc: { call() { throw new Error('No RPC on registration'); } } }, slots: { inject(slot, fn) { fn(); }, register(entry) { entries.push(entry); } } });
  assert.deepEqual(entries.map(x => x.name), ['main', 'sidebar.panellist']);
  assert.equal(entries[1].label, '文书脱敏');
});

test('真实R99服务就绪、首页及停止契约，无安装或模型调用', { timeout: 20000 }, async () => {
  assert.ok(process.env.DSH_TEST_PYTHON, '测试须明确传入既有R99环境的Python');
  const data = await mkdtemp(join(tmpdir(), 'dsh-redactor-smoke-'));
  let owned;
  try {
    owned = new LocalRuntime(data).launch(process.env.DSH_TEST_PYTHON);
    const value = await owned.ready;
    const url = validateReady(value);
    const health = await (await fetch(url + 'health')).json(); assert.equal(health.revision, 'r99');
    const html = await (await fetch(url)).text(); assert.ok(html.includes('打开规则文件'));
    await owned.stop(); await owned.closed;
    await assert.rejects(fetch(url + 'health', { signal: AbortSignal.timeout(1000) }));
  } finally { if (owned) await owned.stop(); await rm(data, { recursive: true, force: true }); }
});
