import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

test('document review and restore templates render safe export guidance', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const scripts = execFileSync('python3', ['-B', '-c', [
    'import json, re, sys',
    'sys.path.insert(0, "core/scripts")',
    'from contract_app_ui import build_review_page, build_restore_landing_page',
    'pages = [build_review_page("example", {"filename": "sample." + ext}) for ext in ("docx", "txt", "md")]',
    'pages.append(build_restore_landing_page("example", {"filename": "sample.docx", "mode": "restore"}))',
    'print(json.dumps([re.findall(r"<script>(.*?)</script>", page.decode(), re.S) for page in pages]))',
  ].join('\n')], { cwd: root, encoding: 'utf8' });
  for (const page of JSON.parse(scripts)) {
    const script = page.find(source => source.includes('function showExportProblem'));
    assert.ok(script);
    for (const source of page) new vm.Script(source);
    const helper = script.slice(script.indexOf('async function requestExportResult'),
      script.indexOf('function showExportProblem'));
    const start = script.indexOf('function showExportProblem');
    const renderer = script.slice(start, script.indexOf('\n}', start) + 2);
    const host = {
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
    };
    const response = {
      ok: false, stage: '检查 Word 内容控件', error: '<控件> 无法处理',
      location: {section: '页眉', part: 'word/header1.xml', kind: '内容控件', ordinal: 2},
      code: 'DOCX_CONTENT_CONTROL_UNSUPPORTED', advice: '在副本中改为普通文本后重新上传。',
    };
    const context = {
      host, document: { createElement: () => ({}) },
      fetch: async () => ({ok: false, json: async () => response}),
    };
    await vm.runInNewContext(helper + renderer +
      "\nrequestExportResult('/api/apply/example', {}, '生成脱敏文书').then(result => showExportProblem(host, result))", context);
    assert.deepEqual(host.children.map(child => child.textContent),
      ['检查 Word 内容控件 · <控件> 无法处理', '页眉 · word/header1.xml · 第 2 个内容控件',
        '在副本中改为普通文本后重新上传。', 'DOCX_CONTENT_CONTROL_UNSUPPORTED']);
  }
});
