import { appResource, accessResource, ACCESS_URI, APP_URI, LEGACY_APP_URI, LEGACY_APP_V3_URI, APP_MIME } from './widget-resource.mjs';
import { createInterface } from 'node:readline';
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { appendFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { ROOT } from './paths.mjs';
import { openWorkbench } from './workbench-server.mjs';
import { connectWorkbench } from './owner-client.mjs';

export const WORKBENCH_UI = { ui: { resourceUri: APP_URI }, workbuddy: { ui: { launchSurface: 'panel' } } };
export const TOOLS = [
  { name: 'redaction_open', _meta: WORKBENCH_UI, description: '打开本机完整文书脱敏工作台。用户在页面上传和复核 Word、PDF、TXT、Markdown、Excel，导出、还原或管理规则与历史。原文及映射留在本机页面，不要读取页面或处理目录。', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'redaction_status', description: '查询本机工作台的启动或环境准备状态。只返回状态，不返回文书、规则或映射。', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'redaction_close', description: '停止本会话打开的工作台服务。已保存的项目保存在本机。', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
];
TOOLS.push(
  { name: 'redaction_rules_context',
    description: '仅在用户已在本机工作台点击生成规则方案后读取本次规则指令。返回当前规则和用户主动输入的需求，不含文书、候选或映射。只依据返回数据生成JSON方案，再调用redaction_rules_submit。禁止读文件、终端、网络及其他工具。',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'redaction_rules_submit',
    description: '提交本次规则修改方案，供本机校验并显示差异。用户还须在工作台确认应用。本工具不能应用规则。',
    inputSchema: { type: 'object', required: ['id','plan'], additionalProperties: false,
      properties: {
        id: { type: 'string', pattern: '^[0-9a-f-]{36}$' },
        plan: { type: 'object', required: ['summary','edits'], additionalProperties: false,
          properties: { summary: { type: 'string', maxLength: 1000 },
            edits: { type: 'array', maxItems: 12, items: {
              type: 'object', required: ['before','after'], additionalProperties: false,
              properties: { before: { type: 'string', maxLength: 131072 },
                after: { type: 'string', maxLength: 131072 } } } } } },
      } } }
);


// WorkBuddy 5.11.3 advertises UI resources through its desktop host even when
// the model-facing MCP initialize request omits the UI extension capability.
// The exact vendor host marker distinguishes that path from ordinary CLI clients.
export function supportsEmbeddedUi(message, host = process.env.CODEBUDDY_HOST) {
  return Boolean(message.params?.capabilities?.extensions?.['io.modelcontextprotocol/ui']) ||
    host === 'workbuddy-desktop';
}

export function safeStatus(service) {
  const { stage, preparation, error } = service.snapshot();
  return { stage, preparation, error };
}

export async function dispatch(workbench, message, initialized, opener = openWorkbench, supportsUi = false) {
  if (message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
    throw Object.assign(Error('Invalid request'), { code: -32600 });
  }
  if (message.method === 'initialize') {
    return {
      protocolVersion: ['2024-11-05','2025-03-26','2025-06-18','2025-11-25'].includes(message.params?.protocolVersion)
        ? message.params.protocolVersion : '2025-11-25',
      capabilities: { tools: {}, resources: {} }, serverInfo: { name: 'workbuddy-document-redactor', version: '0.2.0' },
    };
  }
  if (message.method === 'ping') return {};
  if (!initialized) throw Object.assign(Error('Not initialized'), { code: -32000 });
  if (message.method === 'tools/list') return { tools: TOOLS };
  if (message.method === 'resources/list') return { resources: [{ uri: APP_URI, name: 'redaction-workbench', mimeType: APP_MIME }] };
  if (message.method === 'resources/read') {
    if (message.params?.uri === ACCESS_URI) return accessResource(await workbench.widget());
    if (![APP_URI, LEGACY_APP_URI, LEGACY_APP_V3_URI].includes(message.params?.uri)) throw Object.assign(Error('Unknown resource'), { code: -32602 });
    return appResource(await workbench.widget(), message.params.uri);
  }
  if (message.method !== 'tools/call') throw Object.assign(Error('Method not found'), { code: -32601 });
  const { name, arguments: args = {} } = message.params || {};
  const submission = name === 'redaction_rules_submit' && args && typeof args === 'object' &&
    !Array.isArray(args) && Object.keys(args).sort().join(',') === 'id,plan' &&
    typeof args.id === 'string' && /^[0-9a-f-]{36}$/.test(args.id);
  const ordinary = name !== 'redaction_rules_submit' && args && typeof args === 'object' &&
    !Array.isArray(args) && Object.keys(args).length === 0;
  if (!TOOLS.some(tool => tool.name === name) || (!ordinary && !submission)) {
    throw Object.assign(Error('Invalid arguments'), { code: -32602 });
  }
  try {
    if (workbench.refresh) await workbench.refresh();
    if (name === 'redaction_rules_context') {
      const context = await workbench.rulesContext();
      return { content: [{ type: 'text', text: JSON.stringify(context) }] };
    }
    if (submission) {
      const status = await workbench.submitRules(args.id, args.plan);
      return { content: [{ type: 'text', text: JSON.stringify(status) }] };
    }
    if (name === 'redaction_open') {
      if (supportsUi) return { content: [{ type: 'text', text: JSON.stringify(safeStatus(workbench.service)) }], _meta: WORKBENCH_UI };
      if (workbench.open) await workbench.open();
      else await opener(workbench.ticket());
    }
    if (name === 'redaction_close') await workbench.service.stop();
    return { content: [{ type: 'text', text: JSON.stringify(safeStatus(workbench.service)) }] };
  } catch {
    return { isError: true, content: [{ type: 'text', text: '无法操作本机工作台，请重新打开。' }] };
  }
}

export async function main() {
  const workbench = await connectWorkbench();
  let initialized = false;
  let supportsUi = false;
  const lines = createInterface({ input: process.stdin });
  for await (const line of lines) {
    let message;
    try {
      if (Buffer.byteLength(line) > 196608) break;
      message = JSON.parse(line);
      if (!message || Array.isArray(message) || typeof message !== 'object') throw Object.assign(Error(), { code: -32600 });
      if (!('id' in message)) {
        if (message.jsonrpc === '2.0' && message.method === 'notifications/initialized') initialized = true;
        continue;
      }
      if (message.method === 'initialize') supportsUi = supportsEmbeddedUi(message);
      const event = { method: ['initialize','ping','tools/list','tools/call','resources/list','resources/read'].includes(message.method) ? message.method : 'unsupported', pid: process.pid, ppid: process.ppid,
        desktop: process.env.CODEBUDDY_HOST === 'workbuddy-desktop' };
      if (message.method === 'initialize' ||
          (message.method === 'tools/call' && message.params?.name === 'redaction_open')) event.ui = supportsUi;
      if (message.method === 'tools/call') event.tool = TOOLS.some(tool => tool.name === message.params?.name) ? message.params.name : 'unsupported';
      await mkdir(ROOT, { recursive: true, mode: 0o700 });
      await appendFile(join(ROOT, 'protocol-events.jsonl'), JSON.stringify(event) + '\n', { mode: 0o600 });
      const result = await dispatch(workbench, message, initialized, openWorkbench, supportsUi);
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\n');
    } catch (error) {
      const code = error instanceof SyntaxError ? -32700 : error.code || -32603;
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0', id: message?.id ?? null,
        error: { code, message: 'Local workbench request failed' },
      }) + '\n');
    }
  }
  await workbench.close();
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  main().catch(() => { process.stderr.write('Local workbench stopped.\n'); process.exitCode = 1; });
}
