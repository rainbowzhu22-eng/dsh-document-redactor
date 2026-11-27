import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { LocalRuntime } from './engine.mjs';
import { ADAPTER, PACKAGE } from './paths.mjs';

export class WorkBuddyRuntime extends LocalRuntime {
  launch(python, signal) {
    const child = spawn(python, ['-B', join(ADAPTER, 'serve_workbench.py'), this.root, PACKAGE], {
      stdio: ['pipe', 'pipe', 'ignore'], detached: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1' },
    });
    let forceTimer;
    const cancel = () => {
      if (child.exitCode !== null || child.signalCode !== null || !child.pid) return;
      child.stdin.end();
      forceTimer ??= setTimeout(() => {
        try { process.kill(-child.pid, 'SIGTERM'); } catch {}
      }, 4000);
    };
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted) cancel();
    const closed = new Promise(resolve => {
      child.once('close', () => {
        clearTimeout(forceTimer);
        signal.removeEventListener('abort', cancel);
        resolve();
      });
      child.once('error', () => resolve());
    });
    const ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cancel(); reject(Error('START_TIMEOUT')); }, 45000);
      const lines = createInterface({ input: child.stdout });
      lines.on('line', line => {
        if (line.length > 512) return;
        try {
          const event = JSON.parse(line);
          if (event.type === 'ready') { clearTimeout(timer); resolve(event); }
        } catch {}
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('close', () => {
        clearTimeout(timer); lines.close(); reject(Error('SERVICE_STOPPED'));
      });
    });
    return { ready, closed, stop: async () => { cancel(); await closed; } };
  }
}
