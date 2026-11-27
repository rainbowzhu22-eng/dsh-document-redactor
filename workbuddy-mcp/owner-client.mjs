import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { ADAPTER, HOME } from './paths.mjs';

export async function control(action, stateFile = join(HOME, 'workbench-state.json')) {
  const state = JSON.parse(await readFile(stateFile, 'utf8'));
  const url = new URL(state.origin);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' ||
      !/^[0-9a-f]{64}$/.test(state.ownerToken)) throw Error('INVALID_LOCAL_SERVICE');
  const response = await fetch(new URL('/owner', url), {
    method: 'POST', headers: { 'x-workbench-owner': state.ownerToken },
    body: JSON.stringify(typeof action === 'string' ? { action } : action), signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw Error('LOCAL_CONTROL_FAILED');
  const value = await response.json();
  return value;
}

export async function connectWorkbench() {
  try { await control('status'); }
  catch {
    const child = spawn(process.execPath, [join(ADAPTER, 'run_local.mjs')], {
      detached: true, stdio: 'ignore', env: process.env,
    });
    child.unref();
    let ready = false;
    for (let attempt = 0; attempt < 50; attempt++) {
      try { await control('status'); ready = true; break; }
      catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    if (!ready) throw Error('LOCAL_WORKBENCH_START_FAILED');
  }
  let state = await control('status');
  return {
    service: {
      snapshot: () => state,
      async stop() { state = await control('stop'); return state; },
    },
    async refresh() { state = await control('status'); },
    async open() { state = await control('open'); },
    async widget() { return control('ui-ticket'); },
    async rulesContext() { return control('rules-context'); },
    async submitRules(id, plan) { return control({ action: 'rules-submit', id, plan }); },
    async close() {},
  };
}
