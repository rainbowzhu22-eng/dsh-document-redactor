import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const root = fileURLToPath(new URL('../', import.meta.url));
const source = process.argv[2] || '.';
const html = execFileSync('python3', ['-B', '-c', [
  'import sys',
  'sys.path.insert(0, sys.argv[1] + "/core/scripts")',
  'from contract_pdf_ui import page',
  'sys.stdout.buffer.write(page("fictional-session"))',
].join('\n'), source], { cwd: root, encoding: 'utf8' });
const script = html.match(/<script>([\s\S]+)<\/script>/)[1];

function review(exportResponse = async () => ({ok: true, url: '/download/fictional'})) {
  const nodes = new Map();
  const document = {
    getElementById(id) { return nodes.get(id); },
    createElement() { return element(); },
  };
  function element(id) {
    const attributes = new Map();
    return {
      id, style: {}, dataset: {}, hidden: true, children: [],
      focus() { document.activeElement = this; },
      setAttribute(key, value) { attributes.set(key, value); },
      getAttribute(key) { return attributes.get(key); },
      removeAttribute(key) { attributes.delete(key); },
      replaceChildren(...children) { this.children = children; },
      append(child) { this.children.push(child); if (child.id) nodes.set(child.id, child); },
    };
  }
  for (const [, id] of html.matchAll(/\bid="([\w-]+)"/g)) nodes.set(id, element(id));
  const strokes = [];
  const canvas = {
    setPointerCapture() {},
    getContext() {
      return {
        clearRect() {}, fillRect() {}, setLineDash() {},
        strokeRect(...rect) { strokes.push(rect); },
      };
    },
  };
  const sheets = ['source', 'preview'].map((id, index) => {
    const image = { style: {} };
    const pane = {
      clientWidth: 524 - index * 84, scrollTop: 0, scrollLeft: 0,
      getBoundingClientRect() { return { left: 0, top: 0 }; },
      addEventListener(event, handler) { this[event] = handler; },
    };
    const sheet = nodes.get(id);
    Object.assign(sheet, {
      closest: () => pane,
      querySelector: tag => tag === 'img' ? image : index === 0 ? canvas : null,
      getBoundingClientRect: () => ({
        left: 12 - pane.scrollLeft, top: (index === 0 ? 145 : 65) - pane.scrollTop,
        width: parseFloat(image.style.width), height: parseFloat(image.style.height),
      }),
    });
    return { image, pane };
  });
  canvas.getBoundingClientRect = () => nodes.get('source').getBoundingClientRect();

  const requests = [];
  let observeResize;
  const context = vm.createContext({
    document, setInterval() {}, location: {},
    getComputedStyle: () => ({ paddingLeft: '12px', paddingRight: '12px' }),
    ResizeObserver: class {
      constructor(handler) { observeResize = handler; }
      observe() {}
    },
    fetch(path, options) {
      requests.push({ path, body: options.body ? JSON.parse(options.body) : undefined });
      if (path.startsWith('/api/pdf/export/')) {
        return exportResponse().then(body => ({ ok: body.ok, json: async () => body }));
      }
      return new Promise(() => {});
    },
  });
  const run = code => vm.runInContext(code, context);
  run(script);
  run(`p={stage:'review',revision:7,pages:[
    {page:1,width:800,height:1200}, {page:2,width:1000,height:800}],
    candidates:[{id:'c1',page:1,bbox:[30,40,80,70]}],
    regions:[{id:'r1',page:1,bbox:[100,100,200,150]}]};
    renderCandidates=()=>{};
    setSheet('source','blob:source',p.pages[0]);setSheet('preview','blob:preview',p.pages[0]);`);
  for (const { image } of sheets) image.onload();
  const input = nodes.get('zoominput');
  const submit = value => {
    input.value = value;
    nodes.get('zoomform').onsubmit({ preventDefault() {} });
  };
  const widths = () => sheets.map(({ image }) => image.style.width);
  const click = id => nodes.get(id).onclick();
  function pointer(event, x, y) {
    const rect = canvas.getBoundingClientRect();
    canvas[event]({
      clientX: rect.left + x * rect.width / 800,
      clientY: rect.top + y * rect.height / 1200,
      pointerId: 1,
    });
  }
  return { document, nodes, run, sheets, requests, strokes, input, submit, widths, click, observeResize, pointer };
}

test('applies custom percentages and shared shortcuts without network requests', () => {
  const ui = review();
  const initialRequests = ui.requests.length;
  ui.submit(' 83% ');
  assert.deepEqual(ui.widths(), ['664px', '664px']);
  ui.submit('117');
  assert.deepEqual(ui.widths(), ['936px', '936px']);
  ui.click('zoomin');
  assert.equal(ui.input.value, '142');
  ui.click('zoomout');
  assert.equal(ui.input.value, '117');
  ui.click('zoomreset');
  assert.deepEqual(ui.widths(), ['800px', '800px']);
  assert.equal(ui.requests.length, initialRequests);
});

test('rejects invalid input without changing the last applied zoom', () => {
  const ui = review();
  ui.submit('83');
  for (const value of ['', 'abc', '49', '201', '1e2', '83.5']) {
    ui.submit(value);
    assert.equal(ui.input.getAttribute('aria-invalid'), 'true', value);
    assert.equal(ui.nodes.get('zoomerror').hidden, false);
    assert.deepEqual(ui.widths(), ['664px', '664px']);
  }
  ui.input.onkeydown({ key: 'Escape' });
  assert.equal(ui.input.value, '83');
  assert.equal(ui.nodes.get('zoomerror').hidden, true);
  ui.submit('50％');
  assert.equal(ui.input.getAttribute('aria-invalid'), undefined);
  assert.deepEqual(ui.widths(), ['400px', '400px']);
});

test('fits the narrower pane and tracks container and page size changes', () => {
  const ui = review();
  ui.click('zoomfit');
  assert.equal(ui.nodes.get('zoomfit').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(ui.widths(), ['416px', '416px']);
  ui.sheets[1].pane.clientWidth = 344;
  ui.observeResize();
  assert.deepEqual(ui.widths(), ['320px', '320px']);
  ui.run(`cur=2;setSheet('source','blob:source2',p.pages[1]);setSheet('preview','blob:preview2',p.pages[1]);`);
  for (const { image } of ui.sheets) image.onload();
  assert.deepEqual(ui.widths(), ['320px', '320px']);
  assert.equal(ui.input.value, '32');
  ui.submit('83');
  ui.observeResize();
  assert.deepEqual(ui.widths(), ['830px', '830px']);
  assert.equal(ui.nodes.get('zoomfit').getAttribute('aria-pressed'), 'false');
});

test('keeps the visible page position on zoom and mirrors scrolling in both directions', () => {
  const ui = review();
  const [source, preview] = ui.sheets.map(({ pane }) => pane);
  source.scrollTop = 345;
  source.scrollLeft = 112;
  source.scroll();
  assert.equal(preview.scrollTop, 265);
  assert.equal(preview.scrollLeft, 112);
  ui.submit('150');
  assert.equal(source.scrollTop, 445);
  assert.equal(source.scrollLeft, 162);
  assert.equal(preview.scrollTop, 365);
  preview.scroll();
  preview.scrollTop = 425;
  preview.scrollLeft = 204;
  preview.scroll();
  assert.equal(source.scrollTop, 505);
  assert.equal(source.scrollLeft, 204);
});

test('keeps candidate hits, OCR markers and add/move/resize in page coordinates', () => {
  const ui = review();
  ui.submit('117');
  ui.run('ocrSelection={page:1,bbox:[20,60,70,90]};draw(false)');
  assert.ok(Math.abs(parseFloat(ui.nodes.get('ocrfocus').style.top) - 70.2) < 0.001);
  assert.ok(ui.strokes.some(rect => Math.abs(rect[0] - 35.1) < 0.001));
  ui.pointer('onpointerdown', 50, 55);
  assert.deepEqual(ui.requests.at(-1).body, { action: 'candidate', revision: 7, page: 1, id: 'c1', selected: true });
  function drag(from, to) {
    ui.pointer('onpointerdown', ...from);
    ui.pointer('onpointermove', ...to);
    ui.pointer('onpointerup', ...to);
    const { bbox, ...fields } = ui.requests.at(-1).body;
    return { ...fields, bbox: bbox.map(Math.round) };
  }
  assert.deepEqual(drag([250, 250], [300, 310]),
    { action: 'region-add', revision: 7, page: 1, bbox: [250, 250, 300, 310] });
  assert.deepEqual(drag([150, 120], [175, 130]),
    { action: 'region-update', revision: 7, page: 1, id: 'r1', bbox: [125, 110, 225, 160] });
  assert.deepEqual(drag([197, 147], [240, 185]),
    { action: 'region-update', revision: 7, page: 1, id: 'r1', bbox: [100, 100, 240, 185] });
});

test('sends the same export request before and after a zoom change', async () => {
  const ui = review();
  ui.run('refresh=async()=>{}');
  await ui.click('export');
  ui.submit('83');
  await ui.click('export');
  const exports = ui.requests.filter(({ path }) => path.startsWith('/api/pdf/export/'));
  assert.equal(exports.length, 2);
  assert.deepEqual(exports[0], exports[1]);
  assert.deepEqual(exports[0].body, { revision: 7, confirm: false });
});

test('shows PDF export pages and keeps a connection error visible', async () => {
  let disconnected = false;
  const ui = review(async () => {
    if (disconnected) throw Error('offline');
    return {ok: false, stage: '检查 PDF 复核状态', code: 'PDF_DISPOSITION_REQUIRED',
      error: '有 1 处敏感内容尚未处理。', advice: '处理或明确保留后再导出。', location: {pages: [2]}};
  });
  await ui.click('export');
  const panel = ui.nodes.get('exportgate');
  assert.equal(panel.style.display, 'block');
  assert.deepEqual(panel.children.map(child => child.textContent), [
    '检查 PDF 复核状态 · 有 1 处敏感内容尚未处理。', '第 2 页', '处理或明确保留后再导出。',
    'PDF_DISPOSITION_REQUIRED', '前往第 2 页',
  ]);
  ui.run('show = pageNumber => { cur = pageNumber }');
  panel.children.at(-1).onclick();
  assert.equal(ui.run('cur'), 2);
  disconnected = true;
  await ui.click('export');
  ui.run('syncExportGate()');
  assert.equal(panel.style.display, 'block');
  assert.match(panel.children[0].textContent, /连接中断/);
  assert.equal(panel.children.at(-1).textContent, 'EXPORT_CONNECTION_LOST');
});
