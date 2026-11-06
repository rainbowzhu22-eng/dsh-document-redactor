import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { RulesController } from '../rules-agent.js';
import { RedactorService, REVISION } from '../service.js';
import { makeRoute } from '../index.js';

const edit = {before:'PUBLIC-CODE',after:'REDACTED-CODE'};
async function fixture(fn) {
  const root = await mkdtemp(join(tmpdir(), 'redactor140-rules-'));
  await mkdir(join(root,'rules'));
  const path = join(root,'rules','脱敏规则清单.md');
  await writeFile(path,'PUBLIC-CODE\nuntouched');
  try { await fn(root,path); } finally { await rm(root,{recursive:true,force:true}); }
}
function controller(root, validate = async () => ({deleted:[]})) {
  return new RulesController(root, async () => ({summary:'fictional-change',edits:[edit]}), validate);
}
async function proposal(rules) { await rules.propose('fictional'); await rules.pending; return rules.snapshot(); }

test('明确放弃方案后重开不恢复，活动规则未改', () => fixture(async (root,path) => {
  const rules=controller(root); await proposal(rules); await rules.discard();
  const reopened=controller(root); await reopened.ready;
  assert.equal(reopened.snapshot().stage,'idle');
  assert.equal(await readFile(path,'utf8'),'PUBLIC-CODE\nuntouched');
  assert.deepEqual((await reopened.status()).history,[]);
}));

test('应用记录持久化，回退须确认，回退也保留可恢复记录', () => fixture(async (root,path) => {
  const rules=controller(root), plan=await proposal(rules);
  await rules.apply(plan.id);
  const reopened=controller(root); await reopened.ready;
  const history=(await reopened.status()).history;
  assert.equal(history.length,1);
  const rollback=await reopened.previewRollback(history[0].id);
  assert.equal(await readFile(path,'utf8'),'REDACTED-CODE\nuntouched');
  await assert.rejects(reopened.apply(rollback.id),{code:'ROLLBACK_CONFIRMATION'});
  await reopened.apply(rollback.id,[],true);
  assert.equal(await readFile(path,'utf8'),'PUBLIC-CODE\nuntouched');
  assert.equal((await reopened.status()).history[0].kind,'rollback');
}));

test('回退产生的自定义规则删除仍须精确确认', () => fixture(async (root,path) => {
  const rules=controller(root,async text=>({deleted:text.startsWith('PUBLIC')?['CUSTOM_CODE']:[]}));
  const plan=await proposal(rules);await rules.apply(plan.id);
  const rollback=await rules.previewRollback(plan.id);
  await assert.rejects(rules.apply(rollback.id,[],true),{code:'DELETION_CONFIRMATION'});
  assert.ok((await readFile(path,'utf8')).startsWith('REDACTED'));
  await rules.apply(rollback.id,['CUSTOM_CODE'],true);
  assert.ok((await readFile(path,'utf8')).startsWith('PUBLIC'));
}));

test('回退预览后外部修改规则时不覆盖外部内容', () => fixture(async (root,path) => {
  const rules=controller(root), plan=await proposal(rules);await rules.apply(plan.id);
  const rollback=await rules.previewRollback(plan.id);
  await writeFile(path,'external-update');
  await assert.rejects(rules.apply(rollback.id,[],true),{code:'RULES_CHANGED'});
  assert.equal(await readFile(path,'utf8'),'external-update');
}));

test('缺失或被篡改的备份不构成可回退版本', () => fixture(async (root,path) => {
  const rules=controller(root), plan=await proposal(rules);await rules.apply(plan.id);
  await writeFile(join(root,'rules','Backups','agent-'+plan.id+'.md'),'tampered');
  await assert.rejects(rules.previewRollback(plan.id),{code:'RULES_UNAVAILABLE'});
  assert.ok((await readFile(path,'utf8')).startsWith('REDACTED'));
}));

test('两个控制器并发应用只有一个写入成功', () => fixture(async (root,path) => {
  const a=controller(root), b=controller(root);
  const pa=await proposal(a),pb=await proposal(b);
  const results=await Promise.allSettled([a.apply(pa.id),b.apply(pb.id)]);
  assert.equal(results.filter(r=>r.status==='fulfilled').length,1);
  assert.equal((await a.status()).history.length,1);
  assert.ok((await readFile(path,'utf8')).startsWith('REDACTED'));
}));

test('已生效但未最终标记的预存记录可在重开后恢复', () => fixture(async (root,path) => {
  const rules=controller(root),plan=await proposal(rules);await rules.apply(plan.id);
  const historyPath=join(root,'rules','agent-history.json');
  const records=JSON.parse(await readFile(historyPath,'utf8'));records[0].status='prepared';
  await writeFile(historyPath,JSON.stringify(records));
  const reopened=controller(root);await reopened.ready;
  assert.equal((await reopened.status()).history[0].id,plan.id);
  const rollback=await reopened.previewRollback(plan.id);await reopened.apply(rollback.id,[],true);
  assert.ok((await readFile(path,'utf8')).startsWith('PUBLIC'));
}));

test('校验失败时规则与历史记录保持原样', () => fixture(async (root,path) => {
  const rules=controller(root),plan=await proposal(rules);
  rules.validate=async()=>{throw Object.assign(new Error('invalid'),{code:'INVALID_RULES'});};
  await assert.rejects(rules.apply(plan.id),{code:'INVALID_RULES'});
  assert.equal(await readFile(path,'utf8'),'PUBLIC-CODE\nuntouched');
  assert.deepEqual((await rules.status()).history,[]);
}));

test('准备阶段可见，过期回调不能覆盖停止后的状态',async()=>{
  let progress,finish;
  const service=new RedactorService({prepare:async(signal,onProgress)=>{progress=onProgress;return new Promise(resolve=>finish=resolve);}});
  service.start();progress('installing');assert.equal(service.snapshot().preparation,'installing');
  const stopping=service.stop();progress('validating');finish('unused');await stopping;
  assert.equal(service.snapshot().stage,'idle');assert.equal(service.snapshot().preparation,null);
});

test('控制接口只接收固定下载源与回退参数',async()=>{
  let source;
  const service={snapshot:()=>({stage:'idle'}),start:options=>{source=options.source;return{stage:'preparing'};}};
  const route=makeRoute(service);
  const request=payload=>new Request('http://localhost/api/document.redactor',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'client-request',method:'document.redactor',rpcId:'test',payload})});
  assert.equal((await route.fetch(request({action:'start',source:'tuna'}))).status,200);assert.equal(source,'tuna');
  for(const payload of [{action:'start',source:'http://bad.invalid'},{action:'rules-rollback-preview',id:'../private'},{action:'rules-apply',id:'0'.repeat(36),confirmedDeleted:[],confirmedRollback:'true'}]) {
    assert.equal((await route.fetch(request(payload))).status,400);
  }
});
