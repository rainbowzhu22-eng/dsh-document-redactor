"""Single-document PDF review interface (simplified)."""
import json

from contract_ui_notices import EXPORT_REVIEW_NOTICE, EXPORT_UNRECOGNIZED_NOTE


def page(sid):
    sid_json = json.dumps(sid)
    # 页面模板必须用 raw f-string：非 raw 时 JS 里的 \n / \t 会先被 Python 实例化，
    # 生成未闭合的 JS 字符串字面量（整段脚本 SyntaxError、页面永不刷新）。参见 H4。
    return rf'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PDF 核对</title>
<style>
*{{box-sizing:border-box}}body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;margin:0;color:#243342;background:#f5f7fa}}
header{{background:white;border-bottom:1px solid #ddd;padding:12px 20px;display:flex;justify-content:space-between;gap:12px;align-items:center}}
button,a.button{{border:1px solid #ccd5dd;background:#fff;border-radius:7px;padding:7px 12px;cursor:pointer;color:#184d7c;text-decoration:none;font:inherit}}
button.primary{{background:#1265a8;color:white;border-color:#1265a8}}button:disabled{{opacity:.5;cursor:default}}
main{{display:grid;grid-template-columns:225px minmax(320px,1fr) minmax(320px,1fr);gap:12px;padding:12px;height:calc(100vh - 64px)}}
aside,.pane{{background:white;border:1px solid #dde3e8;border-radius:9px;overflow:auto;padding:12px}}
.pane h3{{position:sticky;top:-12px;background:white;margin:-12px -12px 12px;padding:12px;border-bottom:1px solid #ddd;z-index:2}}
.sheet{{position:relative;display:inline-block;transform-origin:top left;line-height:0}}
.sheet img{{max-width:none;display:block}}.sheet canvas{{position:absolute;inset:0;cursor:crosshair}}
.pagebtn{{display:flex;align-items:center;gap:8px;width:100%;text-align:left;margin:5px 0}}.pagebtn img{{width:34px;height:46px;object-fit:contain;border:1px solid #ddd;background:white}}.pagebtn.active{{background:#e7f2fb}}
.dot{{width:9px;height:9px;border-radius:50%;flex:none}}.dot.ok{{background:#0a8b5f}}.dot.warn{{background:#d98200}}.dot.busy{{background:#88939d}}
.item{{border-bottom:1px solid #e8e8e8;padding:8px 0;word-break:break-all}}.muted{{color:#617384;font-size:13px}}
.toolbar{{display:flex;gap:7px;flex-wrap:wrap;align-items:center}}#notice{{padding:8px;margin:8px 0;background:#eef6fd;border-radius:6px}}
#error{{color:#a32e2e;white-space:pre-wrap}}#work{{display:none}}
#pagewarn{{white-space:pre-wrap;color:#8a4b00;background:#fff6e6;border-radius:6px;padding:8px;margin:8px 0}}
.alert.warn{{color:#70470a;background:#fff6e6;border:1px solid #efd08a;border-radius:6px;padding:9px;margin:8px 0;font-weight:700}}
.alert.info{{color:#184d7c;background:#eef6fd;border:1px solid #b8d8f0;border-radius:6px;padding:9px;margin:8px 0}}
.notice-secondary{{display:block;color:#617384;font-size:12px;line-height:1.5;margin-top:3px}}
.seg{{display:inline-flex;border:1px solid #ccd5dd;border-radius:7px;overflow:hidden;vertical-align:middle}}
.seg button{{border:0;border-radius:0;padding:3px 9px;font-size:13px}}.seg button.on{{background:#1265a8;color:white}}
.red{{border-left:3px solid #c0392b;padding-left:8px}}
#exportstatus{{white-space:pre-wrap;margin:8px 0;color:#617384}}
#exportconfirm{{display:none;color:#70470a;background:#fff6e6;border:1px solid #efd08a;border-radius:8px;padding:10px;margin:8px 0}}
#exportconfirm ul{{margin:6px 0 8px 18px}}
details{{margin:8px 0}}details summary{{cursor:pointer;color:#184d7c}}
@media(max-width:1100px){{main{{grid-template-columns:210px minmax(0,1fr) minmax(0,1fr)}}aside,.pane{{min-width:0}}}}
@media(max-width:960px){{header{{flex-wrap:wrap}}main{{grid-template-columns:minmax(0,1fr);height:auto}}.pane{{max-height:85vh}}}}
</style></head><body>
<header><div><a href="/">首页</a>　<strong>PDF 核对</strong>　<span id="filename"></span></div><div class="toolbar"><a class="button" href="/history">本机项目</a><button id="original">取回原始 PDF</button><button id="relink">重新关联原件</button><input id="relinkFile" type="file" accept=".pdf" hidden></div></header>
<main><aside><div id="status">正在读取项目…</div>
<div id="notice">在本机识别文字，优先替换为代号；原件不会被修改。</div>
<div id="error"></div><div id="pagewarn"></div>
<div id="pages"></div>
<h4>本页候选</h4><div id="reditems"></div><div id="items"></div>
<details><summary>高级</summary>
<div class="toolbar" style="margin:8px 0"><button id="cancel">取消识别</button><button id="retry">重试处理</button><button id="retryPage">重试本页识别</button><button id="syncCase">重新检查同案规则</button></div>
<h4>补充敏感原文</h4>
<input id="termLabel" placeholder="代号名称，如 项目名称" style="width:100%"><input id="termValues" placeholder="原文；别名以分号分隔" style="width:100%"><label><input id="termCase" type="checkbox">同案文书共用</label><button id="termAdd">查找并加入</button>
</details></aside>
<section class="pane"><h3>原页 · 点击候选切换处理；拖动框选遮盖</h3><div class="toolbar"><button id="zoomout">－</button><span id="zoomlabel">100%</span><button id="zoomin">＋</button><button id="undo" title="撤销最近一次更改">撤销最近更改</button></div>
<p class="muted">印章、签名、漏识别文字请在原页直接拖动框选；已有遮盖可移动、调整或删除。</p>
<div id="source" class="sheet"><img alt="原页"><canvas></canvas></div></section>
<section class="pane"><h3>导出预览</h3>
<div id="preview" class="sheet"><img alt="预览页"><canvas></canvas></div>
<p class="alert info" id="exportdisclaimer"><span>{EXPORT_REVIEW_NOTICE}<small class="notice-secondary">{EXPORT_UNRECOGNIZED_NOTE}</small></span></p>
<div class="toolbar" style="margin-top:14px"><button id="prev">上一页</button><button id="next">下一页</button><button class="primary" id="export">导出脱敏 PDF</button></div>
<p id="exportstatus"></p>
<p id="exportnotes" class="muted" style="white-space:pre-wrap"></p>
<div id="exportconfirm"></div>
<details><summary>导出说明</summary><p class="muted">代号覆盖原文字像素，短框依次使用短中文类型或字母加编号（如机构O1、姓名P1）。PDF导出将每页转换为图片，不保留文本层，不生成映射文件，无法用工具还原；可点击链接会被移除。批注及批注人名称可能保留，请逐页核对。检出的待处置项须先处理或明确保留；未识别内容仍可能按原文保留，请人工核对。</p></details>
</section></main>
<script>
const sid={sid_json};let p=null,cur=1,zoom=1,imgUrl='',previewUrl='',drag=null,drawMode='',activeRegion=null,renderId=0,thumbTasks={{}},warnOpen=false;
const $=id=>document.getElementById(id);
const candidateBoxes=a=>Array.isArray(a.bboxes)&&a.bboxes.length?a.bboxes:[a.bbox];
async function api(path,body){{const r=await fetch(path,{{method:body?'POST':'GET',headers:{{'Content-Type':'application/json','X-Local-Project':'1'}},body:body?JSON.stringify(body):undefined}});const j=await r.json();if(!r.ok||j.ok===false)throw Error(j.error||'操作失败');return j;}}
function err(e){{let message=String(e.message||e);if(/Failed to fetch|NetworkError|Load failed/i.test(message))message='本地脱敏服务连接中断。请重新运行工具；启动工具后如果出现黑色窗口，请保持开启；关闭后将无法正常使用。';$('error').textContent=message}}
function setProject(next){{p=next;$('error').textContent=p.error||'';}}
function reviewState(a,collection='candidates'){{return (p.review_evaluation?.states||[]).find(s=>s.candidate_ref.collection===collection&&s.candidate_ref.id===a.id)?.state||'pending'}}
function stateText(st){{return ({{'redact':'已替换','redact-box':'已按框替换','covered':'已遮盖','manual-covered':'已遮盖','keep':'已保留','policy-excluded':'按策略保留','optional':'待人工处理','pending':'待人工处理'}})[st]||st}}
async function refresh(){{try{{let j=await api('/api/pdf/status/'+sid);setProject(j.project);$('filename').textContent=p.filename;const done=p.pages.filter(x=>x.reviewed).length;
$('status').textContent=`${{p.stage==='processing'?'扫描中，请耐心等待':p.stage==='review'?((p.review_evaluation?.pending_occurrences||0)?'扫描完成，请先处理待确认项':'扫描完成，可导出'):p.stage==='exported'?'已导出':p.stage}} · 已处理 ${{p.done}}/${{p.pages.length}} 页`;
renderList();$('cancel').style.display=p.stage==='processing'?'':'none';$('retry').style.display=['failed','cancelled'].includes(p.stage)?'':'none';$('syncCase').style.display=p.case_rules_pending?'':'none';
if(p.stage==='processing'){{setTimeout(refresh,850);return}}
if(p.stage==='failed'||p.stage==='cancelled'){{renderPolicy();await show(cur);return}}
await show(cur);renderPolicy();}}catch(e){{err(e)}}}}
function renderPolicy(){{if(!p)return;let label=p.policy_id==='privacy_first'?'脱敏交易要素':'保留交易要素';let state=p.policy_id==='privacy_first'?'金额、日期默认脱敏':'金额、日期默认保留';$('notice').textContent='在本机识别文字，优先替换为代号；原件不会被修改。当前策略：'+label+'，'+state+'。上传后不能切换策略。';}}
function pageDot(x){{if(x.stage!=='ready')return 'busy';if(x.ocr==='failed'||x.ocr==='empty'||x.ocr_supplement==='failed'||x.ocr_supplement==='empty')return 'warn';let stats=x.ocr_line_stats||{{}};if(Object.values(stats).some(v=>v>0)||x.dropped_lines>0)return 'warn';return 'ok'}}
function renderList(){{$('pages').replaceChildren();p.pages.forEach(x=>{{let b=document.createElement('button');b.className='pagebtn'+(x.page===cur?' active':'');if(x.stage==='ready'){{let im=document.createElement('img');im.alt='第 '+x.page+' 页缩略图';b.append(im);if(!thumbTasks[x.page])thumbTasks[x.page]=fetch('/api/pdf/thumb/'+sid+'/'+x.page,{{headers:{{'X-Local-Project':'1'}}}}).then(r=>{{if(!r.ok)throw Error('缩略图失败');return r.blob()}}).then(blob=>URL.createObjectURL(blob)).catch(()=>null);thumbTasks[x.page].then(url=>{{if(url&&im.isConnected)im.src=url}})}}
let dot=document.createElement('span');dot.className='dot '+pageDot(x);b.append(dot,document.createTextNode(`第 ${{x.page}} 页${{x.ocr==='failed'?' · 识别失败':x.ocr==='empty'?' · 未识别文字':''}}`));b.onclick=()=>show(x.page);$('pages').append(b)}})}}
async function show(n){{if(!p||n<1||n>p.pages.length)return;cur=n;renderList();let x=p.pages[n-1];if(x.stage!=='ready')return;let id=++renderId;try{{const [r,s]=await Promise.all([fetch('/api/pdf/page/'+sid+'/'+n,{{headers:{{'X-Local-Project':'1'}}}}),fetch('/api/pdf/preview/'+sid+'/'+n,{{headers:{{'X-Local-Project':'1'}}}})]);if(!r.ok||!s.ok)throw Error('页面图像尚未准备');let a=URL.createObjectURL(await r.blob()),b=URL.createObjectURL(await s.blob());if(id!==renderId){{URL.revokeObjectURL(a);URL.revokeObjectURL(b);return}}
if(imgUrl)URL.revokeObjectURL(imgUrl);if(previewUrl)URL.revokeObjectURL(previewUrl);imgUrl=a;previewUrl=b;for(let key of ['source','preview']){{let box=$(key),im=box.querySelector('img'),cv=box.querySelector('canvas');im.src=key==='source'?a:b;im.onload=()=>{{im.style.width=Math.round(x.width*zoom)+'px';im.style.height=Math.round(x.height*zoom)+'px';cv.width=im.clientWidth;cv.height=im.clientHeight;draw()}}}}
updateWarn();draw();}}catch(e){{err(e)}}}}
function updateWarn(){{if(!p)return;let x=p.pages[cur-1];if(!x)return;let w=[];if(x.ocr==='failed')w.push('本页识别失败：'+(x.error||'未能识别出文字')+'。未识别内容将按原文导出，建议重试本页识别或人工检查。');if(x.ocr==='empty')w.push('本页未识别到文字，请确认确无敏感内容。');if(x.ocr_supplement==='failed'||x.ocr_supplement==='empty')w.push('本页未取得补充OCR结果，请人工检查文字层可能未覆盖的可见内容。');
let suppressedRows=(p.scan_stats&&p.scan_stats.below_display_floor_by_page)||[],suppressed=suppressedRows.find(row=>Number(row.page)===cur);if(suppressed&&suppressed.count)w.push('本页另有 '+suppressed.count+' 条低置信命中未列入候选清单。');
let stats=x.ocr_line_stats||{{}};let dropped=x.dropped_lines||0;let lineIssues=Object.values(stats).reduce((a,v)=>a+(v||0),0);if(lineIssues||dropped)w.push('本页有 '+(lineIssues+dropped)+' 行OCR结果未完整纳入，可能存在漏检，建议人工检查。');
let el=$('pagewarn');if(!w.length){{el.textContent='';el.style.display='none';return}}
if(!warnOpen){{el.textContent='本页有 '+w.length+' 条机器提示，点击查看';el.style.display='block';el.style.cursor='pointer';el.onclick=()=>{{warnOpen=true;updateWarn()}};return}}
el.style.cursor='default';el.onclick=null;el.textContent=w.join('\n');el.style.display='block'}}
function segButton(label,on,onclick){{let b=document.createElement('button');b.textContent=label;if(on)b.className='on';b.onclick=e=>{{e.stopPropagation();onclick()}};return b}}
function renderCandidates(){{if(!p)return;
let red=$('reditems');red.replaceChildren();
let pendingUn=pendingUnlocated();let pendingC=pendingCandidates();
if(pendingUn.length||pendingC.length){{let h=document.createElement('div');h.className='muted';h.style.marginBottom='6px';h.textContent='需你确认（'+(pendingUn.length+pendingC.length)+' 处，请先处理或明确保留）';red.append(h)}}
for(let a of pendingUn){{let row=document.createElement('div');row.className='item red';let location=a.page?'第'+a.page+'页':'页码未知';row.append(document.createTextNode(location+' · '+a.label+' · '+a.value+' '));if(Number.isInteger(a.page)){{let go=segButton('去定位',false,async()=>{{await show(a.page);let el=$('locationStatus');if(!el){{el=document.createElement('div');el.id='locationStatus';$('source').parentNode.insertBefore(el,$('source'))}}el.textContent='正在定位：'+a.label+' · '+a.value+'。请在原页拖动框选完整区域。';pendingUn.current=a;activeUnlocatedId=a.id;}});row.append(go)}}
let keep=segButton('保留',a.status==='keep',()=>change('unlocated-keep',{{page:Number.isInteger(a.page)?a.page:cur,id:a.id}}));row.append(keep);red.append(row)}}
for(let a of pendingC){{let row=document.createElement('div');row.className='item red';row.append(document.createTextNode('第'+a.page+'页 · '+a.label+' · '+a.value+' 尚未处置 '));if((p.review_evaluation?.states||[]).some(s=>s.candidate_ref.collection==='candidates'&&s.candidate_ref.id===a.id&&s.can_auto_redact))row.append(segButton('自动处理',false,()=>change('candidate',{{page:a.page,id:a.id,selected:true}})));let go=segButton('去定位',false,async()=>{{await show(a.page);let el=$('locationStatus');if(!el){{el=document.createElement('div');el.id='locationStatus';$('source').parentNode.insertBefore(el,$('source'))}}el.textContent='正在定位：'+a.label+' · '+a.value+'。请在原页拖动框选完整区域。';activeUnlocatedId=a.id;activeCandidateRef={{collection:'candidates',id:a.id}};}});row.append(go);let keep=segButton('保留',reviewState(a)==='keep',()=>change('candidate',{{page:a.page,id:a.id,selected:false}}));row.append(keep);red.append(row)}}
if(!pendingUn.length&&!pendingC.length){{let ok=document.createElement('div');ok.className='muted';ok.textContent='无待确认项。';red.append(ok)}}
let items=p.candidates.filter(a=>a.page===cur&&reviewState(a)!=='pending');let host=$('items');host.replaceChildren();
let groups={{}};for(let a of items){{(groups[a.label]=groups[a.label]||[]).push(a)}}
for(let [label,group] of Object.entries(groups)){{let all=segButton('本页全部'+label+'（'+group.length+'）',false,()=>change('type-toggle',{{page:cur,label,selected:!group.every(x=>reviewState(x)==='redact'||reviewState(x)==='redact-box')}}));let div=document.createElement('div');div.className='item';div.append(all);host.append(div);
for(let a of group){{let st=reviewState(a);let row=document.createElement('div');row.className='item';let auto=st==='redact'||st==='redact-box';let cover=st==='covered'||st==='manual-covered';let kept=st==='keep';
let seg=document.createElement('span');seg.className='seg';seg.append(segButton('自动',auto,()=>change('candidate',{{page:cur,id:a.id,selected:true}})));seg.append(segButton('遮盖',cover,()=>coverOne(a)));seg.append(segButton('保留',kept,()=>change('candidate',{{page:cur,id:a.id,selected:false}})));
let hint='';if(a.entity_confidence!=null&&a.entity_confidence<0.50)hint=' · 实体把握较低，建议核对';if(a.auto_applied==='ocr_low')hint+=' · 机器按OCR低分自动按框处理';if(a.auto_applied==='coarse_geometry')hint+=' · 机器按行级框自动处理，可能含同行少量正文';
row.append(seg,document.createTextNode(` ${{a.value}} → ${{a.placeholder}} · ${{stateText(st)}}${{hint}}`));host.append(row)}}}}
for(let a of p.regions.filter(a=>a.page===cur)){{let div=document.createElement('item');div.className='item';div.append(document.createTextNode('手工遮盖 '));let b=document.createElement('button');b.textContent='删除';b.onclick=()=>change('region-delete',{{page:cur,id:a.id}});div.append(b);host.append(div)}}
updateSummary()}}
function pendingCandidates(){{return (p?.candidates||[]).filter(a=>reviewState(a)==='pending')}}
function pendingUnlocated(){{return (p?.unlocated_candidates||[]).filter(a=>reviewState(a,'unlocated_candidates')==='pending')}}
function coverOne(a){{change('region-from-candidates',{{page:cur,candidate_refs:[{{collection:'candidates',id:a.id}}]}})}}
function updateSummary(){{if(!p)return;let el=$('exportstatus');let ev=p.review_evaluation||{{}};let states=ev.states||[];let count=st=>states.filter(s=>s.state===st).length;let red=count('pending');let replaced=count('redact'),boxed=count('redact-box'),covered=count('covered')+count('manual-covered');let preserved=count('keep')+count('policy-excluded')+count('optional');let automatic=states.filter(s=>s.auto_applied&&['redact','redact-box'].includes(s.state)).length;
let line1='本次处置：替换 '+replaced+' 处、按框处理 '+boxed+' 处、人工遮盖 '+covered+' 处；机器自动处理 '+automatic+' 处；另有 '+preserved+' 处候选保留原文';
let line2=red?('待确认 '+red+' 处，请先处理或明确保留'):'无待确认项';
$('export').disabled=p.stage==='processing'||red>0;
el.textContent=p.stage==='processing'?'扫描中，完成后可导出。':red?line1+'；'+line2+'。':p.stage==='exported'?'已导出。外发文件不含原件与映射表。':line1+'；'+line2+'。点「导出脱敏 PDF」即可。'}}
function draw(){{if(!p)return;let x=p.pages[cur-1],w=x.width,h=x.height,c=$('source').querySelector('canvas'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);let sx=c.width/w,sy=c.height/h;if(!sx||!sy)return;
for(let a of p.candidates.filter(a=>a.page===cur)){{let state=reviewState(a),applied=['redact','redact-box','covered','manual-covered'].includes(state);ctx.strokeStyle=applied?'#0a8b5f':['keep','policy-excluded'].includes(state)?'#88939d':'#dc8b19';ctx.setLineDash(applied?[]:[5,4]);ctx.lineWidth=2;for(let b of candidateBoxes(a))ctx.strokeRect(b[0]*sx,b[1]*sy,(b[2]-b[0])*sx,(b[3]-b[1])*sy)}}
ctx.setLineDash([]);for(let a of p.regions.filter(a=>a.page===cur)){{let b=a.bbox;ctx.fillStyle='rgba(0,0,0,.3)';ctx.fillRect(b[0]*sx,b[1]*sy,(b[2]-b[0])*sx,(b[3]-b[1])*sy);ctx.strokeStyle='#0a8b5f';ctx.strokeRect(b[0]*sx,b[1]*sy,(b[2]-b[0])*sx,(b[3]-b[1])*sy)}}
if(drag){{ctx.strokeStyle='#d60';ctx.lineWidth=2;ctx.strokeRect(drag[0]*sx,drag[1]*sy,(drag[2]-drag[0])*sx,(drag[3]-drag[1])*sy)}}
renderCandidates()}}
async function change(action,fields){{try{{await api('/api/pdf/update/'+sid,{{action,revision:p.revision,...fields}});let j=await api('/api/pdf/status/'+sid);setProject(j.project);await show(cur);}}catch(e){{err(e);let j=await api('/api/pdf/status/'+sid).catch(()=>null);if(j)setProject(j.project)}}}}
function pos(e){{let c=$('source').querySelector('canvas'),r=c.getBoundingClientRect(),x=p.pages[cur-1];return [Math.max(0,Math.min(x.width,(e.clientX-r.left)*x.width/r.width)),Math.max(0,Math.min(x.height,(e.clientY-r.top)*x.height/r.height))]}}
let canvas=$('source').querySelector('canvas'),start=null,base=null,activeUnlocatedId=null,activeCandidateRef=null;
canvas.onpointerdown=e=>{{if(!p||p.stage==='processing')return;let q=pos(e);activeRegion=p.regions.find(a=>a.page===cur&&q[0]>=a.bbox[0]&&q[0]<=a.bbox[2]&&q[1]>=a.bbox[1]&&q[1]<=a.bbox[3]);if(activeRegion){{base=activeRegion.bbox.slice();start=q;drawMode=(Math.abs(q[0]-base[2])<10/zoom&&Math.abs(q[1]-base[3])<10/zoom)?'resize':'move';drag=base.slice()}}
else{{let hit=p.candidates.find(a=>a.page===cur&&candidateBoxes(a).some(b=>q[0]>=b[0]&&q[0]<=b[2]&&q[1]>=b[1]&&q[1]<=b[3]));if(hit){{change('candidate',{{page:cur,id:hit.id,selected:!(reviewState(hit)==='redact'||reviewState(hit)==='redact-box')}});return}}
drawMode='add';drag=[q[0],q[1],q[0],q[1]]}}canvas.setPointerCapture(e.pointerId)}};
canvas.onpointermove=e=>{{if(!drag)return;let q=pos(e),pg=p.pages[cur-1];if(drawMode==='add'){{drag[2]=q[0];drag[3]=q[1]}}else if(drawMode==='resize'){{drag[2]=Math.max(base[0]+2,q[0]);drag[3]=Math.max(base[1]+2,q[1])}}else{{let dx=q[0]-start[0],dy=q[1]-start[1];dx=Math.max(-base[0],Math.min(pg.width-base[2],dx));dy=Math.max(-base[1],Math.min(pg.height-base[3],dy));drag=[base[0]+dx,base[1]+dy,base[2]+dx,base[3]+dy]}}draw()}};
canvas.onpointerup=e=>{{if(!drag)return;let b=[Math.min(drag[0],drag[2]),Math.min(drag[1],drag[3]),Math.max(drag[0],drag[2]),Math.max(drag[1],drag[3])],mode=drawMode,id=activeRegion&&activeRegion.id,resolvesId=activeUnlocatedId&&null;drag=null;activeRegion=null;drawMode='';
if(b[2]-b[0]<=2||b[3]-b[1]<=2){{draw();return}}
let payload={{page:cur,bbox:b}};if(activeUnlocatedId)payload.resolves_id=activeUnlocatedId;else if(activeCandidateRef)payload.candidate_ref=activeCandidateRef;
if(activeUnlocatedId||activeCandidateRef){{let el=$('locationStatus');if(el)el.textContent='';activeUnlocatedId=null;activeCandidateRef=null}}
change(mode==='add'?'region-add':'region-update',payload)}};
$('undo').onclick=()=>change('undo',{{page:cur}});
$('prev').onclick=()=>show(cur-1);$('next').onclick=()=>show(cur+1);
$('cancel').onclick=async()=>{{try{{await api('/api/pdf/control/'+sid,{{action:'cancel'}});await refresh()}}catch(e){{err(e)}}}};
$('retry').onclick=async()=>{{try{{await api('/api/pdf/control/'+sid,{{action:'retry'}});await refresh()}}catch(e){{err(e)}}}};
$('retryPage').onclick=()=>change('retry-page',{{page:cur}});
$('syncCase').onclick=async()=>{{try{{let j=await api('/api/pdf/sync-case/'+sid,{{}});setProject(j.project);await refresh()}}catch(e){{err(e)}}}};
$('termAdd').onclick=async()=>{{try{{let label=$('termLabel').value.trim(),values=$('termValues').value.split(/[;；]/).map(x=>x.trim()).filter(Boolean);let j=await api('/api/pdf/term/'+sid,{{term:{{label,values}},scope:$('termCase').checked?'case':'document'}});setProject(j.project);$('termValues').value='';$('termLabel').value='';await refresh()}}catch(e){{err(e)}}}};
function setZoom(z){{zoom=Math.max(.5,Math.min(2,z));$('zoomlabel').textContent=Math.round(zoom*100)+'%';show(cur)}}$('zoomin').onclick=()=>setZoom(zoom+.25);$('zoomout').onclick=()=>setZoom(zoom-.25);
async function doExport(confirm){{try{{if(pendingCandidates().length||pendingUnlocated().length)throw Error('请先处理全部待确认项或明确保留，再导出');$('exportconfirm').style.display='none';let j=await api('/api/pdf/export/'+sid,{{revision:p.revision,confirm:!!confirm}});if(j.needs_confirm){{renderExportConfirm(j.red||[],j.soft||[]);return}}
$('exportstatus').textContent='导出完成。外发文件不含原件与映射表。';
$('exportnotes').textContent=(j.notes&&j.notes.length)?('导出提示（无需处理）：\n'+j.notes.join('\n')):'';
location.href=j.url;await refresh()}}catch(e){{err(e);$('exportstatus').textContent=''}}}}
function renderExportConfirm(red,notes){{let box=$('exportconfirm');box.replaceChildren();let t=document.createElement('div');t.innerHTML='<b>导出前请知悉</b>';box.append(t);let ul=document.createElement('ul');red.forEach(s=>{{let li=document.createElement('li');li.textContent=s;ul.append(li)}});box.append(ul);if(notes&&notes.length){{let g=document.createElement('div');g.className='muted';g.style.marginTop='6px';g.textContent='以下为非阻断提示，无需处理：';let ul2=document.createElement('ul');notes.forEach(s=>{{let li=document.createElement('li');li.textContent=s;ul2.append(li)}});g.append(ul2);box.append(g)}}let row=document.createElement('div');row.className='toolbar';let go=document.createElement('button');go.className='primary';go.textContent='我已知悉，仍要导出';go.onclick=()=>doExport(true);let back=document.createElement('button');back.textContent='返回处理';back.onclick=()=>box.style.display='none';row.append(go,back);box.append(row);box.style.display='block'}}
$('export').onclick=()=>doExport(false);
$('original').onclick=async()=>{{try{{const r=await fetch('/api/pdf/original/'+sid,{{headers:{{'X-Local-Project':'1'}}}});if(!r.ok)throw Error((await r.json()).error||'原件不可用');const a=document.createElement('a'),u=URL.createObjectURL(await r.blob());a.href=u;a.download='原始PDF.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(u),30000)}}catch(e){{err(e)}}}};
$('relink').onclick=()=>$('relinkFile').click();
$('relinkFile').onchange=async()=>{{try{{let file=$('relinkFile').files[0];if(!file)return;let fd=new FormData();fd.append('file',file);let r=await fetch('/api/pdf/relink/'+sid,{{method:'POST',headers:{{'X-Local-Project':'1'}},body:fd}}),j=await r.json();if(!r.ok)throw Error(j.error||'关联失败');$('error').textContent='原件校验通过，已重新关联'}}catch(e){{err(e)}}}};
const refreshWithLanguage=refresh;
refresh=async function(){{await refreshWithLanguage();if(!p)return;let warning=document.getElementById('englishwarn');if(p.english_dominant){{if(!warning){{warning=document.createElement('div');warning.id='englishwarn';warning.className='alert warn';warning.innerHTML='<b>检测到以英文为主的PDF。英文规则仍有覆盖边界，请核对姓名、地址、电话、编号、银行信息及金额。</b>';document.querySelector('aside').insertBefore(warning,document.getElementById('error'));}}}}else if(warning)warning.remove();}};
let healthFailures=0;
setInterval(async function(){{if(document.hidden)return;try{{let r=await fetch('/health',{{cache:'no-store'}});if(!r.ok)throw Error('offline');let j=await r.json();if(j.service!=='contract_app_server')throw Error('offline');healthFailures=0;}}catch(e){{healthFailures++;if(healthFailures>=2&&!$('error').textContent)err(Error('Failed to fetch'));}}}},12000);
refresh();
</script></body></html>'''.encode('utf-8')
