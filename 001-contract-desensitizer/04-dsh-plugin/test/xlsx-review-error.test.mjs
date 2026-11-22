import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

test('a blocked payroll export explains the dependency and offers a local recovery', () => {
  const scripts = fileURLToPath(new URL('../core/scripts', import.meta.url));
  const html = execFileSync('python3', ['-B', '-c',
    'import sys;sys.path.insert(0,sys.argv[1]);from contract_xlsx_ui import page;print(page("example",{}).decode())',
    scripts], { encoding: 'utf8' });
  const js = html.match(/<script>([\s\S]+?)<\/script>/)[1];
  new vm.Script(js);
  const helper = js.slice(js.indexOf('function reportError'), js.indexOf("byId('filename').textContent"));
  const field = { id: 1, sheet: '工资明细', address: 'D2', amountCell: true };
  const coords = [field.sheet, field.address];
  const edits = new Map([[1, '[金额]'], [2, '[员工]']]);
  const selectedAmounts = new Map();
  const host = {
    children: [],
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
  };
  const visited = [];
  const amountMode = { checked: false, onchange() { visited.push('enabled'); } };
  const context = {
    document: { createElement(tag) { return { tag }; } },
    payload: { fields: [field] },
    pair: item => [item.sheet, item.address],
    key: JSON.stringify,
    locateField: item => visited.push(item.address),
    byId: id => ({ focus() { visited.push(id); } }),
    edits, selectedAmounts, amountMode, host,
    error: Object.assign(Error('工资明细!L2 的数值计算引用了 工资明细!D2。'), {
      details: { code: 'XLSX_AMOUNT_USE_DEMO', cell: { sheet: '工资明细', address: 'D2' },
        advice: '改用演示金额，或撤销该格的文字替换。' },
    }),
  };
  vm.runInNewContext(helper + '\nreportError(host,error);', context);
  assert.deepEqual(host.children.map(child => child.textContent), [
    '工资明细!L2 的数值计算引用了 工资明细!D2。', '改用演示金额，或撤销该格的文字替换。',
    '定位 工资明细!D2', '将此格改用演示金额',
  ]);
  host.children[2].onclick();
  assert.deepEqual(visited, ['D2']);
  host.children[3].onclick();
  assert.equal(edits.has(1), false);
  assert.equal(edits.get(2), '[员工]');
  assert.equal(amountMode.checked, true);
  assert.deepEqual(selectedAmounts.get(JSON.stringify(coords)), coords);
  assert.deepEqual(visited, ['D2', 'enabled', 'D2', 'analyzeAmount']);
});
