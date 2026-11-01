import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import vm from 'node:vm';
import { applyEdits, RulesController, runRulesAgent } from '../rules-agent.js';
import { makeRoute } from '../index.js';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const edit = { before: 'old rule', after: 'new rule' };
async function fixture(fn) {
  const dir = await mkdtemp(join(tmpdir(), 'redactor-interaction-'));
  await mkdir(join(dir,'rules')); await writeFile(join(dir,'rules','脱敏规则清单.md'), 'old rule\nuntouched');
  try { await fn(dir); } finally { await rm(dir,{recursive:true,force:true}); }
}
test('规则补丁只定位唯一原文，保留其余内容，拒绝歧义与空改动', () => {
  assert.equal(applyEdits('old rule\nuntouched',[edit]),'new rule\nuntouched');
  for (const edits of [[],[{before:'missing',after:'x'}],[{before:'old',after:'old'}]]) assert.throws(()=>applyEdits('old rule',edits));
  assert.throws(()=>applyEdits('old rule old rule',[edit]));
});
test('生成方案不改生效规则，应用前校验，原规则备份，重复应用幂等', async () => fixture(async dir => {
  let calls=0,checks=0;
  const rules=new RulesController(dir,async()=>{calls++;return{summary:'test',edits:[edit]};},async()=>{checks++;return{deleted:[]};});
  const state=await rules.propose('虚构需求'); await rules.pending;
  assert.equal(calls,1);assert.equal(rules.snapshot().stage,'preview');
  assert.equal(await readFile(join(dir,'rules','脱敏规则清单.md'),'utf8'),'old rule\nuntouched');
  await rules.apply(state.id);await rules.apply(state.id);
  assert.equal(checks,2);assert.equal(await readFile(join(dir,'rules','脱敏规则清单.md'),'utf8'),'new rule\nuntouched');
  assert.equal((await readdir(join(dir,'rules','Backups'))).length,1);
}));
test('外部规则变化时拒绝覆盖，保留外部修改',async()=>fixture(async dir=>{
  const rules=new RulesController(dir,async()=>({edits:[edit]}),async()=>({deleted:[]}));
  const state=await rules.propose('虚构需求');await rules.pending;
  await writeFile(join(dir,'rules','脱敏规则清单.md'),'external change');
  await assert.rejects(rules.apply(state.id),{code:'RULES_CHANGED'});
  assert.equal(await readFile(join(dir,'rules','脱敏规则清单.md'),'utf8'),'external change');
}));
test('删除自定义规则须精确确认，未应用方案重开可恢复，不重复模型生成',async()=>fixture(async dir=>{
  const check=async()=>({deleted:['DIY_BATCH']});
  const rules=new RulesController(dir,async()=>({edits:[edit]}),check);
  const state=await rules.propose('删除虚构规则');await rules.pending;
  const reopened=new RulesController(dir,()=>{throw new Error('Must not regenerate');},check);
  await reopened.ready;assert.equal(reopened.snapshot().id,state.id);
  await assert.rejects(reopened.apply(state.id),{code:'DELETION_CONFIRMATION'});
  await reopened.apply(state.id,['DIY_BATCH']);assert.equal(reopened.snapshot().stage,'applied');
}));
test('取消生成后迟到模型结果不能应用，也不能覆盖新方案',async()=>fixture(async dir=>{
  let complete;const deferred=new Promise(resolve=>complete=resolve);
  const rules=new RulesController(dir,()=>deferred,async()=>({deleted:[]}));
  const state=await rules.propose('虚构需求');rules.cancel();complete({edits:[edit]});await rules.pending;
  assert.equal(rules.snapshot().stage,'cancelled');await assert.rejects(rules.apply(state.id));
  assert.equal(await readFile(join(dir,'rules','脱敏规则清单.md'),'utf8'),'old rule\nuntouched');
}));
test('调用真实DSH Agent接口契约：无工具能力、仅规则与需求、收集结果并释放',async()=>{
  let listener,guard,payload,options,disposed=false,restricted;
  const agent={session:{append(type,data){assert.equal(type,'session/title');assert.equal(data.source.kind,'user');}},followup(value){payload=value;listener(agent.session,{type:'assistant/message',data:{message:{content:[{type:'text',text:JSON.stringify({summary:'test',edits:[edit]})}]}}});},async whenIdle(){},cancel(){}};
  const ctx={agentDefaultModel:{currentSelection:()=>({provider:'fixture',model:'fixture-model'})},agents:{async create(value){options=value;await value.setup({tools:{restrict(x){restricted=x;},guard(x){guard=x;}},on(name,fn){if(name==='session/event')listener=fn;}},agent);return{agent,async dispose(){disposed=true;}};}}};
  const plan=await runRulesAgent(ctx,'old rule','虚构需求',new AbortController().signal);
  assert.deepEqual(restricted,{allow:[]});assert.ok(guard({}));assert.equal(options.agentOptions.maxTokens,4096);
  assert.equal(payload.role,'user');assert.equal(payload.content.length,1);assert.ok(payload.content[0].text.includes('虚构需求'));assert.deepEqual(plan.edits,[edit]);assert.ok(disposed);
});
test('规则RPC只接受限定动作，不接受路径、原文或任意执行，异常不泄漏本机路径',async()=>{
  let requests=0;const rules={ready:Promise.resolve(),async propose(text){requests++;return{stage:'generating'};},snapshot(){return{stage:'idle'};}};
  const route=makeRoute({snapshot:()=>({stage:'ready'})},rules);
  const req=payload=>new Request('http://localhost/api/document.redactor',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'client-request',method:'document.redactor',rpcId:'fixture',payload})});
  assert.equal((await route.fetch(req({action:'rules-propose',request:'虚构需求'}))).status,200);
  for(const payload of [{action:'rules-propose',request:'test',path:'/private'},{action:'rules-propose',request:'test',document:'secret'},{action:'exec'}])assert.equal((await route.fetch(req(payload))).status,400);
  rules.propose=async()=>{throw new Error('/private/secret');};
  const failed=await(await route.fetch(req({action:'rules-propose',request:'test'}))).text();assert.ok(!failed.includes('/private'));assert.equal(requests,1);
});
test('原R99引擎逐文件摘要保留，客户端只通过宿主layout API注册页面',async()=>{
  const baseline=JSON.parse(await readFile(join(root,'core/BASELINE.json'),'utf8'));
  for(const [rel,hash] of Object.entries(baseline.files))assert.equal(createHash('sha256').update(await readFile(join(root,'core',rel))).digest('hex'),hash,rel);
  let client;vm.runInNewContext(await readFile(join(root,'client.js'),'utf8'),{window:{__ModuleLoader__:{load(x){client=x.factory(()=>({createElement(){}}));}}}});
  const entries=[];client.apply({layout:{toggleSidebar(){},selectPanel(){}},connection:{rpc:{}},slots:{inject(_s,f){f();},register(x){entries.push(x);}}});
  assert.deepEqual(entries.map(x=>x.name),['main','sidebar.panellist']);assert.ok(client.inject.includes('layout'));
});
