export const REVISION = 'r99';

export function validateReady(value) {
  const url = new URL(value?.url);
  if (value.type !== 'ready' || value.revision !== REVISION ||
      url.protocol !== 'http:' || url.hostname !== '127.0.0.1' ||
      !url.port || Number(url.port) < 1 || Number(url.port) > 65535 ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('INVALID_READY');
  }
  return url.href;
}

export class RedactorService {
  constructor(runtime) {
    this.runtime = runtime;
    this.stage = 'idle';
    this.url = null;
    this.error = null;
    this.generation = 0;
    this.worker = null;
    this.abort = null;
    this.pending = Promise.resolve();
    this.stopping = null;
  }

  snapshot() {
    return { stage: this.stage, url: this.url, error: this.error, revision: REVISION };
  }

  start() {
    if (this.error === 'STOP_FAILED') return this.snapshot();
    if (['preparing', 'starting', 'ready', 'stopping'].includes(this.stage)) return this.snapshot();
    const generation = ++this.generation;
    this.abort = new AbortController();
    const signal = this.abort.signal;
    this.stage = 'preparing';
    this.error = null;
    this.url = null;
    this.pending = (async () => {
      let worker;
      try {
        const python = await this.runtime.prepare(signal);
        if (generation !== this.generation) return;
        this.stage = 'starting';
        worker = this.runtime.launch(python, signal);
        this.worker = worker;
        const ready = await worker.ready;
        const url = validateReady(ready);
        if (generation !== this.generation) {
          await worker.stop();
          return;
        }
        this.url = url;
        this.stage = 'ready';
        worker.closed.then(() => {
          if (generation === this.generation && this.stage === 'ready') {
            this.stage = 'error';
            this.url = null;
            this.error = 'SERVICE_STOPPED';
          }
        });
      } catch (error) {
        if (worker) await worker.stop().catch(() => {});
        if (generation !== this.generation) return;
        const wasPreparing = this.stage === 'preparing';
        this.stage = 'error';
        this.url = null;
        this.error = error.code === 'PYTHON_NOT_FOUND' ? 'PYTHON_NOT_FOUND' :
          wasPreparing ? 'PREPARATION_FAILED' : 'START_FAILED';
      }
    })();
    return this.snapshot();
  }

  stop() {
    if (this.stopping) return this.stopping;
    this.stopping = this.stopOwned().finally(() => { this.stopping = null; });
    return this.stopping;
  }

  async stopOwned() {
    ++this.generation;
    this.stage = 'stopping';
    this.url = null;
    const worker = this.worker;
    try {
      this.abort?.abort();
      if (worker) await worker.stop();
      await this.pending;
      this.worker = null;
      this.stage = 'idle';
      this.error = null;
      return this.snapshot();
    } catch (error) {
      this.stage = 'error';
      this.error = 'STOP_FAILED';
      throw error;
    }
  }
}
