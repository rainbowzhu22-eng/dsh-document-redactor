import { spawn } from 'node:child_process';
import { createWriteStream } from 'node:fs';
import { mkdir, open, access } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const PACKAGE = dirname(fileURLToPath(import.meta.url));
const terminationLevel = new WeakMap();

function terminateOwned(child, force = false) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (terminationLevel.get(child) === true || (!force && terminationLevel.has(child))) return;
  if (process.platform === 'win32') {
    const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'],
      { shell: false, windowsHide: true, stdio: 'ignore' });
    killer.on('error', () => {});
  } else {
    try { process.kill(-child.pid, force ? 'SIGKILL' : 'SIGTERM'); }
    catch (error) {
      if (error.code === 'EPERM') child.kill(force ? 'SIGKILL' : 'SIGTERM');
      else if (error.code !== 'ESRCH') throw error;
    }
  }
  terminationLevel.set(child, force);
}

export function dataRoot(env = process.env) {
  return env.DSH_REDACTOR_DATA_DIR || join(env.DSH_HOME || join(homedir(), '.dsh'), 'document-redactor');
}

function command(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    const { signal, timeout, ...spawnOptions } = options;
    if (signal?.aborted) { reject(new Error('ABORTED')); return; }
    const child = spawn(executable, args, { shell: false, windowsHide: true,
      detached: process.platform !== 'win32', ...spawnOptions });
    const cancel = () => terminateOwned(child);
    signal?.addEventListener('abort', cancel, { once: true });
    const timer = timeout && setTimeout(cancel, timeout);
    const cleanup = () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); };
    let stdout = '';
    child.stdout?.on('data', chunk => { if (stdout.length < 8192) stdout += chunk.toString(); });
    child.once('error', error => { cleanup(); reject(error); });
    child.once('exit', code => { cleanup(); code === 0 && !signal?.aborted ? resolve(stdout.trim()) : reject(new Error('COMMAND_FAILED')); });
  });
}

export async function findPython(signal, env = process.env) {
  const suffix = process.platform === 'win32' ? ['python', 'python.exe'] : ['python', 'bin', 'python3'];
  const candidates = [
    env.DSH_REDACTOR_PYTHON,
    // macOS Desktop's hardened Python cannot load independently installed wheels.
    // Prefer the system interpreter; keep the signed application runtime untouched.
    ...(process.platform === 'darwin' ? ['/usr/bin/python3', 'python3'] : []),
    join(dirname(process.execPath), '..', 'primary-runtime', 'dependencies', ...suffix),
    process.resourcesPath && join(process.resourcesPath, 'runtime', 'primary-runtime', 'dependencies', ...suffix),
    join(homedir(), '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', ...suffix),
    ...(process.platform === 'darwin' ? ['/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/python/bin/python3'] : []),
    process.platform === 'win32' ? 'python' : 'python3',
  ].filter(Boolean);
  for (const executable of [...new Set(candidates)]) {
    if (signal?.aborted) throw new Error('ABORTED');
    try {
      return await command(executable, ['-c', 'import sys; assert sys.version_info >= (3,9); print(sys.executable)'],
        { signal, timeout: 8000, stdio: ['ignore', 'pipe', 'ignore'], env });
    } catch { /* Try the next fixed interpreter; no shell or client-supplied command. */ }
  }
  throw Object.assign(new Error('PYTHON_NOT_FOUND'), { code: 'PYTHON_NOT_FOUND' });
}

export class LocalRuntime {
  constructor(root = dataRoot()) { this.root = root; }

  async prepare(signal) {
    await access(join(PACKAGE, 'core', 'scripts', 'contract_app_server.py'));
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const log = await open(join(this.root, 'environment.log'), 'a', 0o600);
    try {
      const python = await findPython(signal);
      await command(python, [join(PACKAGE, 'runtime.py'), 'prepare', '--data-root', this.root],
        { signal, timeout: 240000, stdio: ['ignore', log.fd, log.fd] });
    } finally { await log.close(); }
    return join(this.root, 'environment', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  }

  launch(python, signal) {
    const child = spawn(python, [join(PACKAGE, 'runtime.py'), 'serve', '--data-root', this.root],
      { shell: false, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'] });
    const log = createWriteStream(join(this.root, 'service.log'), { flags: 'a', mode: 0o600 });
    log.on('error', () => terminateOwned(child));
    child.stderr.pipe(log);
    const cancel = () => terminateOwned(child);
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) cancel();
    let timer;
    let exited = false;
    const closed = new Promise(resolve => {
      child.once('exit', () => { exited = true; resolve(); });
      child.once('error', () => { exited = true; log.end(); resolve(); });
    });
    const ready = new Promise((resolve, reject) => {
      const lines = createInterface({ input: child.stdout });
      timer = setTimeout(() => reject(new Error('READY_TIMEOUT')), 45000);
      lines.on('line', line => {
        if (line.length > 8192) return;
        try {
          const value = JSON.parse(line);
          if (value.type === 'ready') { clearTimeout(timer); resolve(value); }
        } catch { log.write(line + '\n'); }
      });
      child.once('error', reject);
      child.once('exit', () => reject(new Error('START_FAILED')));
      closed.then(() => { clearTimeout(timer); lines.close(); signal?.removeEventListener('abort', cancel); });
    });
    return { ready, closed, stop: async () => {
      clearTimeout(timer);
      if (exited) return;
      terminateOwned(child);
      let killTimer;
      await Promise.race([closed, new Promise(resolve => {
        killTimer = setTimeout(() => { if (!exited) terminateOwned(child, true); resolve(); }, 4000);
      })]);
      clearTimeout(killTimer);
    } };
  }
}
