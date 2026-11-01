import { RedactorService } from './service.js';
import { LocalRuntime } from './runtime.js';
import { RulesController, runRulesAgent } from './rules-agent.js';

export const name = 'document-redactor';
export const inject = ['connection', 'agents', 'agentDefaultModel'];

export function makeRoute(service, rules) {
  return { path: '/api/document.redactor', methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      if (request.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') {
        return new Response('JSON required', { status: 415 });
      }
      const reader = request.body?.getReader();
      const chunks = [];
      let size = 0;
      if (reader) for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 16384) { await reader.cancel(); return new Response('Too large', { status: 413 }); }
        chunks.push(value);
      }
      let body;
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { return new Response('Invalid JSON', { status: 400 }); }
      const payload = body?.payload;
      const action = payload?.action;
      const validPayload = payload && (
        ['status', 'start', 'stop', 'rules-status', 'rules-cancel'].includes(action) && Object.keys(payload).join(',') === 'action' ||
        action === 'rules-propose' && Object.keys(payload).sort().join(',') === 'action,request' && typeof payload.request === 'string' && payload.request.length <= 3000 ||
        action === 'rules-apply' && Object.keys(payload).sort().join(',') === 'action,confirmedDeleted,id' && typeof payload.id === 'string' && /^[0-9a-f-]{36}$/.test(payload.id) && Array.isArray(payload.confirmedDeleted) && payload.confirmedDeleted.length <= 100 && payload.confirmedDeleted.every(x => typeof x === 'string' && /^[A-Z0-9_]{1,80}$/.test(x)));
      if (body?.type !== 'client-request' || body.method !== 'document.redactor' ||
          typeof body.rpcId !== 'string' || body.rpcId.length > 200 ||
          !validPayload) {
        return new Response('Invalid request', { status: 400 });
      }
      let result;
      try {
        let value;
        if (action.startsWith('rules-')) {
          if (!rules || service.snapshot().stage !== 'ready') throw new Error('NOT_READY');
          await rules.ready;
          value = action === 'rules-status' ? rules.snapshot() : action === 'rules-propose' ? await rules.propose(payload.request) : action === 'rules-cancel' ? rules.cancel() : await rules.apply(payload.id, payload.confirmedDeleted);
        } else value = action === 'status' ? service.snapshot() : action === 'start' ? service.start() : await service.stop();
        result = { ok: true, value };
      } catch (cause) {
        const safe = ['INVALID_REQUEST','NO_PLAN','RULES_CHANGED','DELETION_CONFIRMATION','INVALID_RULES','CHECK_FAILED','RULES_UNAVAILABLE'].includes(cause.code);
        result = { ok: false, error: { code: safe ? 'redactor/' + cause.code : 'redactor/CONTROL_FAILED', message: safe ? cause.message : '本机工具操作失败，请先打开工具后重试。', details: {} } };
      }
      return Response.json({ type: 'server-response', rpcId: body.rpcId, result },
        { headers: { 'cache-control': 'no-store' } });
    } };
}

export function apply(ctx) {
  const runtime = new LocalRuntime();
  const service = new RedactorService(runtime);
  const rules = new RulesController(runtime.root, (text, request, signal) => runRulesAgent(ctx, text, request, signal));
  // The exact route inherits DSH Connection authentication and Host/Origin checks.
  ctx.connection.fetch.register(makeRoute(service, rules));
  ctx.on('dispose', async () => { await rules.dispose(); await service.stop(); });
}
