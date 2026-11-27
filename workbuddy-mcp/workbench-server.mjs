import { saveExport, openExport } from './native-export.mjs';
import { createServer, request as httpRequest } from 'node:http';
import { spawn } from 'node:child_process';
import { readFile, appendFile } from 'node:fs/promises';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { join } from 'node:path';
import { RedactorService, RulesController, makeRoute } from './engine.mjs';
import { WorkBuddyRuntime } from './runtime-adapter.mjs';
import { createRulesGenerator } from './rules-workbuddy.mjs';
import { RulesBroker } from './rules-broker.mjs';
import { ADAPTER, ROOT } from './paths.mjs';

process.env.PYTHONDONTWRITEBYTECODE = '1';

function equal(a, b) {
  return typeof a === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) &&
    timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

export async function createWorkbench({ runtime = new WorkBuddyRuntime(ROOT), generate, port = 0 } = {}) {
  const service = new RedactorService(runtime);
  const broker = new RulesBroker();
  const rules = new RulesController(runtime.root, generate ?? createRulesGenerator(broker.request.bind(broker)));
  const route = makeRoute(service, rules);
  const tickets = new Map();
  const cookie = randomBytes(32).toString('hex');
  const ownerToken = randomBytes(32).toString('hex');
  let origin;
  let diagnosticCount = 0;
  const server = createServer(async (request, response) => {
    const fail = code => { response.writeHead(code, { 'cache-control': 'no-store' }); response.end(); };
    try {
      if (request.headers.host !== new URL(origin).host) return fail(403);
      const url = new URL(request.url, origin);
      if (url.origin !== origin) return fail(403);
      response.setHeader('cache-control', 'no-store');
      response.setHeader('referrer-policy', 'no-referrer');
      response.setHeader('x-content-type-options', 'nosniff');
      if (url.pathname === '/owner' && request.method === 'POST') {
        if (!equal(request.headers['x-workbench-owner'], ownerToken)) return fail(403);
        let raw = '';
        for await (const chunk of request) {
          raw += chunk;
          if (Buffer.byteLength(raw) > 196608) return fail(413);
        }
        const command = JSON.parse(raw);
        const ordinary = ['open', 'status', 'stop', 'rules-context', 'ui-ticket'].includes(command.action) &&
          Object.keys(command).join(',') === 'action';
        const submission = command.action === 'rules-submit' &&
          Object.keys(command).sort().join(',') === 'action,id,plan';
        if (!ordinary && !submission) return fail(400);
        if (command.action === 'ui-ticket') {
          response.setHeader('content-type', 'application/json');
          response.end(JSON.stringify({ url: makeTicket(true), origin }));
          return;
        }
        if (command.action === 'rules-context') {
          response.setHeader('content-type', 'application/json');
          response.end(JSON.stringify(broker.context()));
          return;
        }
        if (submission) {
          response.setHeader('content-type', 'application/json');
          response.end(JSON.stringify(broker.submit(command.id, command.plan)));
          return;
        }
        if (command.action === 'open') await openWorkbench(makeTicket());
        if (command.action === 'stop') await service.stop();
        const { stage, preparation, error } = service.snapshot();
        response.setHeader('content-type', 'application/json');
        response.end(JSON.stringify({ stage, preparation, error }));
        return;
      }
      if (url.pathname.startsWith('/ticket/') && request.method === 'GET') {
        const ticket = url.pathname.slice(8);
        if (!tickets.has(ticket)) return fail(403);
        const embedded = tickets.get(ticket);
        tickets.delete(ticket);
        response.writeHead(303, { location: '/workbench',
          'set-cookie': 'redactor=' + cookie + '; HttpOnly; Path=/' +
            (embedded ? '; SameSite=None; Secure; Partitioned' : '; SameSite=Strict') });
        response.end();
        return;
      }
      const supplied = request.headers.cookie?.split(';').map(item => item.trim())
        .find(item => item.startsWith('redactor='))?.slice(9);
      const diagnostic = reason => diagnosticCount++ >= 50 ? Promise.resolve() : appendFile(join(runtime.root, 'access-diagnostics.jsonl'), JSON.stringify({reason,route:url.pathname==='/api/document.redactor'?'rpc':'other',method:request.method,origin:request.headers.origin===origin?'local':request.headers.origin==='null'?'opaque':request.headers.origin?'other':'absent',site:['same-origin','same-site','cross-site','none'].includes(request.headers['sec-fetch-site'])?request.headers['sec-fetch-site']:'absent',cookiePresent:Boolean(supplied)})+'\n',{mode:0o600}).catch(()=>{});
      if (!equal(supplied, cookie)) { await diagnostic('session'); return fail(403); }
      const sameOrigin = request.headers.origin === origin ||
        (!request.headers.origin && request.headers['sec-fetch-site'] === 'same-origin');
      if (!['GET', 'HEAD'].includes(request.method) && !sameOrigin) { await diagnostic('origin'); return fail(403); }
      if(url.pathname==='/native-export'&&request.method==='POST'){
        let raw='';for await(const chunk of request){raw+=chunk;if(Buffer.byteLength(raw)>4096)return fail(413);}
        const command=JSON.parse(raw);let result;
        if(command.action==='save'&&Object.keys(command).sort().join(',')==='action,name,sid')result=await saveExport(runtime.root,command.sid,command.name);
        else if(command.action==='open'&&Object.keys(command).sort().join(',')==='action,alias')result=await openExport(runtime.root,command.alias);
        else return fail(400);
        response.setHeader('content-type','application/json');response.end(JSON.stringify(result));return;
      }
      if (request.url === '/workbench' || ['/assets/workbench.js','/assets/native-rules.js'].includes(request.url)) {
        if (request.method !== 'GET') return fail(405);
        const script = request.url.endsWith('.js');
        response.setHeader('content-type', script ? 'text/javascript; charset=utf-8' : 'text/html; charset=utf-8');
        response.end(await readFile(join(ADAPTER, 'web', script ? request.url.split('/').pop() : 'index.html')));
        return;
      }
      if (request.url === '/api/document.redactor' && request.method === 'POST') {
        const body = [];
        let size = 0;
        for await (const chunk of request) {
          size += chunk.length;
          if (size > 16384) return fail(413);
          body.push(chunk);
        }
        const result = await route.fetch(new Request(origin + request.url, {
          method: 'POST', headers: { 'content-type': request.headers['content-type'] || '' },
          body: Buffer.concat(body),
        }));
        const resultBody = await result.text();
        let value;
        try {
          value = JSON.parse(resultBody);
          if (value.result?.value?.url) value.result.value.url = origin + '/';
        } catch {}
        response.writeHead(result.status, Object.fromEntries(result.headers));
        response.end(value ? JSON.stringify(value) : resultBody);
        return;
      }
      if (service.stage !== 'ready') return fail(503);
      const upstream = new URL(service.url);
      const headers = { ...request.headers, host: upstream.host };
      delete headers.cookie;
      if (headers.origin) headers.origin = upstream.origin;
      const proxy = httpRequest(new URL(url.pathname + url.search, upstream), {
        method: request.method, headers,
      }, result => {
        const safeHeaders = { ...result.headers };
        delete safeHeaders['set-cookie'];
        if (safeHeaders.location?.startsWith(upstream.origin)) {
          safeHeaders.location = safeHeaders.location.slice(upstream.origin.length);
        }
        response.writeHead(result.statusCode, safeHeaders);
        result.pipe(response);
      });
      proxy.on('error', () => { if (!response.headersSent) fail(502); else response.destroy(); });
      request.on('aborted', () => proxy.destroy());
      request.pipe(proxy);
    } catch { if (!response.headersSent) fail(500); else response.destroy(); }
  });
  server.requestTimeout = 130000;
  server.headersTimeout = 15000;
  await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  function makeTicket(embedded = false) {
    const ticket = randomBytes(32).toString('hex');
    if (tickets.size >= 10) tickets.clear();
    tickets.set(ticket, embedded);
    return origin + '/ticket/' + ticket;
  }
  return {
    service, rules, broker, origin, ownerToken,
    ticket: makeTicket,
    async close() {
      await rules.dispose();
      await service.stop();
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    },
  };
}

export async function openWorkbench(url) {
  await new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/open', [url], { stdio: 'ignore' });
    child.once('error', reject);
    child.once('close', code => code === 0 ? resolve() : reject(Error('OPEN_FAILED')));
  });
}
