"""Workbook review page for the local application."""
import json

from contract_ui_notices import EXPORT_FEEDBACK_JS


def page(sid, payload):
    data = json.dumps(dict(payload, sid=sid), ensure_ascii=True).replace("<", "\\u003c")
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Excel 脱敏复核</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f9;color:#243342;font:15px/1.55 system-ui,sans-serif}
main{max-width:1080px;margin:auto;padding:24px}h1{font-size:25px;margin:12px 0}
button,select,input{font:inherit}button,select{padding:6px 10px;border:1px solid #b6acc7;border-radius:5px;background:white;color:#372656}
button{cursor:pointer}button:disabled{opacity:.5;cursor:default}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0}
table{border-collapse:collapse;width:100%;background:white;table-layout:fixed}
th,td{text-align:left;vertical-align:top;padding:8px;border:1px solid #d4dbe2;overflow-wrap:anywhere;white-space:pre-wrap}
th:first-child{width:7em}textarea{width:100%;min-height:3em;font:inherit;resize:vertical}
#status{min-height:1.5em;color:#9b1b1b}#download[hidden],#formula[hidden]{display:none}
.primary{background:#5b4085;color:white}.muted{color:#536279}.review-footer{margin-top:20px}
@media(max-width:600px){main{padding:12px}th:first-child{width:4em}th,td{padding:4px}}
</style></head><body><main>
<a href="/">首页</a> · <a href="/history">本机项目</a>
<h1>Excel 脱敏复核</h1><p id="filename"></p>
<p>请选择要替换的单元格，也可以直接输入自定义替换内容。请核对所有工作表、批注、表名和未命中的内容。导出会移除原文档属性。</p>
<p id="formula" class="muted" hidden></p>
<label><input type="checkbox" id="amountMode">金额脱敏并保留公式</label>
<div id="amountOptions" hidden>
<p class="muted">选择要隐藏的金额。导出会使用演示数值重新计算，并将联动金额显示为「已脱敏」。</p>
<div class="toolbar"><label for="amountColumn">当前工作表的金额列</label><select id="amountColumn"></select>
<button id="selectAmountColumn" type="button">添加整列</button>
<button id="clearAmount" type="button">清空选择</button>
<button id="analyzeAmount" type="button">分析金额范围</button><span id="amountCount"></span></div>
<div id="amountStatus" role="status"></div>
<details id="amountDetails" hidden><summary>查看受影响的位置</summary><div id="amountCells"></div></details>
</div>
<div class="toolbar"><label for="sheet">工作表</label><select id="sheet"></select>
<label><input type="checkbox" id="suggestions">仅显示识别项</label>
<button id="applySuggestions">应用当前工作表的识别建议</button></div>
<table><thead><tr><th>位置</th><th>原内容</th><th>替换内容</th></tr></thead><tbody id="rows"></tbody></table>
<div class="toolbar"><button id="prev">上一页</button><span id="counter"></span><button id="next">下一页</button></div>
<div class="review-footer"><label><input type="checkbox" id="ack">我已复核所有工作表（含隐藏表）、公式及批注</label>
<div class="toolbar"><button class="primary" id="export" disabled>导出脱敏 XLSX</button><a id="download" hidden>下载脱敏工作簿</a></div>
<div id="status" role="status"></div></div>
<script type="application/json" id="payload">''' + data + '''</script>
<script>
''' + EXPORT_FEEDBACK_JS + '''
const payload=JSON.parse(document.getElementById('payload').textContent);
const edits=new Map(),size=50;
let index=0,busy=false,amountPlan=null;
const selectedAmounts=new Map();
const plannedAmounts=new Set();
const byId=id=>document.getElementById(id);
const sheet=byId('sheet'),formulas=byId('formula'),ack=byId('ack'),exportButton=byId('export'),download=byId('download');
const amountMode=byId('amountMode'),quantityColumn=byId('amountColumn');
const pair=field=>[field.sheet,field.cell??field.address];
const key=coords=>JSON.stringify(coords);
function selectedField(field){return amountMode.checked&&plannedAmounts.has(key(pair(field)))}
function updateExport(){exportButton.disabled=!ack.checked||busy||(amountMode.checked&&!amountPlan)}
async function requestExport(body){
  const result=await requestExportResult('/api/xlsx/export/'+payload.sid,{
    method:'POST',headers:{'Content-Type':'application/json','X-Local-Project':'1'},
    body:JSON.stringify(body),
  },body.plan_only?'分析 Excel 金额联动范围':'生成脱敏 Excel');
  if(!result.ok){
    const error=Error(result.error||'操作失败');
    error.details=result;
    throw error;
  }
  return result;
}
function locateField(field){
  sheet.value=field.sheet;
  byId('suggestions').checked=false;
  index=Math.floor(visibleFields().indexOf(field)/size);
  render();
  const row=[...byId('rows').children].find(entry=>entry.dataset.cell===key(pair(field)));
  if(row){
    row.tabIndex=-1;
    row.focus();
    row.scrollIntoView({block:'center'});
  }
}
function reportError(host,error){
  const details=error.details||{};
  const message=document.createElement('strong');
  message.textContent=(details.stage?details.stage+' · ':'')+error.message;
  host.replaceChildren(message);
  if(details.advice){
    const advice=document.createElement('p');
    advice.textContent=details.advice;
    host.append(advice);
  }
  const location=details.cell;
  if(!location)return;
  const field=payload.fields.find(item=>item.sheet===location.sheet && pair(item)[1]===location.address);
  if(!field)return;
  const locate=document.createElement('button');
  locate.type='button';
  locate.textContent='定位 '+location.sheet+'!'+location.address;
  locate.onclick=()=>locateField(field);
  host.append(locate);
  if(details.code==='XLSX_AMOUNT_USE_DEMO'&&field.amountCell){
    const useDemo=document.createElement('button');
    useDemo.type='button';
    useDemo.textContent='将此格改用演示金额';
    useDemo.onclick=()=>{
      edits.delete(field.id);
      const coords=pair(field);
      selectedAmounts.set(key(coords),coords);
      amountMode.checked=true;
      amountMode.onchange();
      locateField(field);
      byId('analyzeAmount').focus();
    };
    host.append(useDemo);
  }
}
byId('filename').textContent=payload.filename+' · '+payload.policyLabel;
if(payload.formula_count){
  formulas.hidden=false;
  formulas.textContent=payload.formula_count+' 条公式。请核对表达式和保存的结果，导出清除旧缓存，由表格应用打开后重算。';
}
for(const name of payload.sheets){const option=new Option(payload.hidden_sheets.includes(name)?name+"（隐藏）":name,name);sheet.add(option);}
function invalidate(){
  ack.checked=false;
  exportButton.disabled=true;
  download.hidden=true;
  byId('status').replaceChildren();
}
function clearPlan(){
  amountPlan=null;
  plannedAmounts.clear();
  byId('amountStatus').textContent='';
  byId('amountDetails').hidden=true;
  invalidate();
  render();
}
function rowFor(field){
  const row=document.createElement('tr');
  row.dataset.cell=key(pair(field));
  const position=row.insertCell(),original=row.insertCell(),target=row.insertCell();
  position.textContent=field.address;
  original.textContent=field.text;
  if(amountMode.checked&&field.amountCell){
    const coords=pair(field),id=key(coords);
    const label=document.createElement('label');
    const box=document.createElement('input');
    box.type='checkbox';
    box.checked=selectedAmounts.has(id);
    box.setAttribute('aria-label',coords[1]+' 加入金额脱敏');
    box.onchange=()=>{
      if(box.checked)selectedAmounts.set(id,coords);else selectedAmounts.delete(id);
      clearPlan();
    };
    label.append(box,selectedField(field)?' 金额将使用演示数值':' 选入金额脱敏');
    target.append(label,document.createElement('br'));
  }
  if(field.readonly){target.append('保留公式结构');return row}
  if(selectedField(field)){
    if(edits.has(field.id)){
      const restore=document.createElement('button');
      restore.textContent='撤销文字替换';
      restore.onclick=()=>{edits.delete(field.id);invalidate();render()};
      target.append(restore);
    }
    return row;
  }
  const input=document.createElement('textarea');
  input.value=edits.get(field.id)??field.text;
  input.setAttribute('aria-label',field.address+' 替换内容');
  input.oninput=()=>{
    if(input.value===field.text)edits.delete(field.id);else edits.set(field.id,input.value);
    invalidate();
  };
  target.append(input);
  if(field.suggestion){
    const apply=document.createElement('button');
    apply.type='button';
    apply.textContent='使用建议';
    apply.onclick=()=>{input.value=field.suggestion;input.oninput()};
    target.append(apply);
  }
  return row;
}
function visibleFields(){
  return payload.fields.filter(field=>field.sheet===sheet.value&&(!byId('suggestions').checked||field.suggestion));
}
function render(){
  const filtered=visibleFields();
  const pages=Math.max(1,Math.ceil(filtered.length/size));
  const entries=filtered.slice(index*size,(index+1)*size);
  const columns=new Set(payload.fields.filter(field=>field.sheet===sheet.value&&field.amountCell)
    .map(field=>pair(field)[1].replace(/[0-9]+$/,'')));
  const last=quantityColumn.value;
  quantityColumn.replaceChildren(...[...columns].sort().map(column=>new Option(column,column)));
  if(columns.has(last))quantityColumn.value=last;
  byId('selectAmountColumn').disabled=!columns.size||busy;
  byId('analyzeAmount').disabled=!selectedAmounts.size||busy;
  byId('amountCount').textContent='已选 '+selectedAmounts.size+' 格';
  byId('rows').replaceChildren(...entries.map(rowFor));
  byId('counter').textContent=filtered.length?('第 '+(index+1)+' / '+pages+' 页'):'当前没有可显示的单元格';
  byId('prev').disabled=index===0;
  byId('next').disabled=index+1>=pages;
}
sheet.onchange=byId('suggestions').onchange=()=>{index=0;render()};
byId('prev').onclick=()=>{index--;render()};
byId('next').onclick=()=>{index++;render()};
byId('applySuggestions').onclick=()=>{
  for(const field of payload.fields){
    if(field.sheet===sheet.value&&field.suggestion&&!edits.has(field.id)&&!selectedField(field))edits.set(field.id,field.suggestion);
  }
  invalidate();
  render();
};
amountMode.onchange=()=>{
  byId('amountOptions').hidden=!amountMode.checked;
  clearPlan();
};
byId('selectAmountColumn').onclick=()=>{
  const column=quantityColumn.value;
  for(const field of payload.fields){
    if(field.sheet!==sheet.value||!field.amountCell)continue;
    const coords=pair(field);
    if(coords[1].replace(/[0-9]+$/,'')===column)selectedAmounts.set(key(coords),coords);
  }
  clearPlan();
};
byId('clearAmount').onclick=()=>{selectedAmounts.clear();clearPlan()};
byId('analyzeAmount').onclick=async()=>{
  if(busy||!selectedAmounts.size)return;
  clearPlan();
  busy=true;
  render();
  updateExport();
  const snapshot=JSON.stringify([...selectedAmounts.values()]);
  try{
    const response=await requestExport({amount_targets:JSON.parse(snapshot),plan_only:true});
    if(!amountMode.checked||snapshot!==JSON.stringify([...selectedAmounts.values()]))return;
    amountPlan=response.amount_plan;
    const locations=[...amountPlan.inputs,...amountPlan.formulas];
    for(const coords of locations)plannedAmounts.add(key(coords));
    byId('amountStatus').textContent='将替换 '+amountPlan.inputs.length+' 个金额输入，并联动 '+amountPlan.formulas.length+' 个公式结果。';
    byId('amountCells').textContent=locations.map(coords=>coords.join('!')).join('、');
    byId('amountDetails').hidden=false;
  }catch(error){reportError(byId('amountStatus'),error)}
  finally{busy=false;render();updateExport()}
};
ack.onchange=updateExport;
exportButton.onclick=async()=>{
  if(exportButton.disabled)return;
  busy=true;
  updateExport();
  download.hidden=true;
  const status=byId('status');
  status.textContent='';
  try{
    const body={edits:Object.fromEntries(edits),acknowledged:true};
    if(amountMode.checked){
      body.amount_targets=[...selectedAmounts.values()];
      body.amount_plan_id=amountPlan.id;
    }
    const result=await requestExport(body);
    if(!ack.checked)return;
    download.href=result.url;
    download.hidden=false;
  }catch(error){reportError(status,error)}
  finally{busy=false;updateExport()}
};
render();
</script></main></body></html>''').encode("utf-8")
