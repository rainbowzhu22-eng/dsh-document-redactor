import { mkdir, writeFile, rm, open, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createWorkbench, openWorkbench } from './workbench-server.mjs';
import { HOME } from './paths.mjs';

await mkdir(HOME, { recursive: true, mode: 0o700 });
const lockPath = join(HOME, 'workbench.lock');
let lock;
try {
  lock = await open(lockPath, 'wx', 0o600);
} catch (error) {
  if (error.code !== 'EEXIST') throw error;
  const pid = Number(await readFile(lockPath, 'utf8'));
  try {
    process.kill(pid, 0);
    process.exit(0);
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
  await rm(lockPath);
  lock = await open(lockPath, 'wx', 0o600);
}
await lock.writeFile(String(process.pid));
await lock.close();
let workbench;
const statePath = join(HOME, 'workbench-state.json');
const shutdown = async () => {
  await workbench?.close();
  await rm(statePath, { force: true });
  await rm(lockPath, { force: true });
  process.exit();
};
process.once('SIGINT', shutdown);
process.once('SIGTERM', shutdown);
try {
  const portPath = join(HOME, 'workbench-port.json');
  let port = 0;
  try { port = JSON.parse(await readFile(portPath, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (!Number.isInteger(port) || port < 0 || port > 65535 || (port > 0 && port < 1024)) throw Error('INVALID_PORT');
  workbench = await createWorkbench({ port });
  await writeFile(portPath, JSON.stringify(Number(new URL(workbench.origin).port)), { mode: 0o600 });
  await writeFile(statePath, JSON.stringify({
    pid: process.pid, url: workbench.ticket(), origin: workbench.origin, ownerToken: workbench.ownerToken,
  }), { mode: 0o600 });
  if (process.argv.includes('--open')) await openWorkbench(workbench.ticket());
  console.log('本机文书脱敏工作台已启动');
} catch {
  await rm(lockPath, { force: true });
  process.stderr.write('本机工作台启动失败。\n');
  process.exitCode = 1;
}
