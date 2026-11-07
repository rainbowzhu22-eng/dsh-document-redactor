window.__ModuleLoader__.load({ id: 'dsh-document-redactor', factory: function(require) {
  const exports = {}, React = require('react'), h = React.createElement;
  const labels = { idle: '尚未打开', preparing: '正在准备本机环境…', starting: '正在启动…', ready: '本机运行中', stopping: '正在停止…', error: '打开未完成' };
  const errors = { STOP_FAILED: '停止未完成，请重试停止；已保存的项目仍保留。', PYTHON_NOT_FOUND: '未找到 Python 3.9 或更新版本。请安装 Python 后重试。', PREPARATION_FAILED: '环境准备失败，请检查网络和本机安装权限后重试。', START_FAILED: '本机服务未能启动，请重试。', SERVICE_STOPPED: '本机服务已停止，可重新打开，已保存的项目仍保留。' };
  errors.PREPARATION_TIMEOUT = '准备已达到20分钟上限。已保留安装进度和缓存；可切换下载源后重试，或查看数据目录中的 environment.log。';
  const preparationLabels = { checking:'检查已有环境', environment:'准备 Python 环境', installing:'下载并安装依赖（慢网可能需要数分钟）', validating:'检查解析与 OCR 能力', ready:'环境检查完成' };
  const css = `
  .redactor-app{--rd-bg:#eef1f8;--rd-surface:#fff;--rd-surface-soft:#f7f9fd;--rd-line:#e2e7f1;--rd-ink:#0d1626;--rd-ink-2:#39465a;--rd-sub:#6b7a90;--rd-accent:#3355ff;--rd-accent-line:#c9d5ff;height:100%;display:flex;flex-direction:column;min-width:0;background:var(--rd-bg);color:var(--rd-ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,"Segoe UI",sans-serif;font-size:14.5px;line-height:1.72;-webkit-font-smoothing:antialiased}
  .redactor-header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 24px;padding-inline-start:max(24px,calc(var(--dsh-frame-leading-clearance,0px) + 16px));border-bottom:1px solid var(--rd-line);background:rgba(255,255,255,.92);flex:none}
  .redactor-header h2{margin:0;font-size:15px;font-weight:650;line-height:1.3;letter-spacing:-.015em}.redactor-header [role=status]{font-size:12.5px;color:#0f9d58}.redactor-spacer{flex:1}
  .redactor-btn{display:inline-flex;align-items:center;justify-content:center;padding:9px 13px;border:1px solid var(--rd-line);border-radius:9px;background:var(--rd-surface-soft);color:var(--rd-ink-2);font-family:inherit;font-size:13.5px;font-weight:560;cursor:pointer;text-decoration:none;line-height:1.4;white-space:nowrap}
  .redactor-btn:hover{border-color:#c8d3ea;color:var(--rd-ink);background:#fff}.redactor-btn:disabled{opacity:.5;cursor:default;box-shadow:none}
  .redactor-primary{padding:12px 20px;border-radius:11px;font-size:15px;font-weight:640;background:linear-gradient(135deg,#3b5bff,#5a48f5);color:#fff;border-color:transparent;box-shadow:0 6px 18px #3b5bff30}.redactor-primary:hover{background:linear-gradient(135deg,#3b5bff,#5a48f5);color:#fff;border-color:transparent}.redactor-link{border:0;background:none;color:var(--rd-sub);padding:6px 0}.redactor-link:hover{background:none}
  .redactor-body{flex:1;min-height:0;display:flex;position:relative;overflow:auto}.redactor-body iframe{border:0;flex:1;min-width:0;height:100%;background:var(--rd-bg)}
  .redactor-start{max-width:640px;margin:64px auto;padding:26px}.redactor-start h1{font-size:27px;font-weight:680;line-height:1.3}.redactor-start p{color:var(--rd-sub);line-height:1.72;font-size:14.5px}
  .redactor-alert{font-size:13.5px;line-height:1.72;color:#d92d20;margin:0;padding:10px 24px;background:#fef3f2}
  .redactor-faq{margin:24px 0;padding:18px 20px;border:1px solid #e2e7f1;border-radius:14px;background:#fff;color:#39465a;font-size:13.5px;line-height:1.72;min-width:0}.redactor-faq h3{margin:0 0 12px;font-size:16px;color:#0d1626}.redactor-faq details+details{border-top:1px solid #e2e7f1;margin-top:12px;padding-top:12px}.redactor-faq summary{cursor:pointer;font-weight:600;color:#0d1626}.redactor-faq p,.redactor-start .redactor-faq p{font-size:13.5px;margin:10px 0;color:#6b7a90}.redactor-faq ol{padding-left:22px;margin:12px 0}.redactor-faq li{margin:6px 0}.redactor-faq textarea{display:block;width:100%;box-sizing:border-box;margin:12px 0;padding:12px;border:1px solid #c9d5ff;border-radius:10px;resize:vertical;min-height:154px;background:#f7f9fd;color:#39465a;font:inherit;line-height:1.72;overflow-wrap:anywhere}.redactor-faq textarea:focus{outline:3px solid #3355ff1c;border-color:#3355ff}.redactor-faq [role=status]{min-height:1.72em}.redactor-faq .redactor-btn{white-space:normal;text-align:center}.redactor-faq code{overflow-wrap:anywhere}
  .redactor-drawer{width:400px;max-width:45%;flex:none;border-left:1px solid var(--rd-line);background:var(--rd-surface);overflow-y:auto;padding:22px;box-sizing:border-box}
  .redactor-drawer h3{font-size:18px;font-weight:640;line-height:1.4;margin:0}.redactor-drawer h4{font-size:16px;font-weight:640;line-height:1.5;margin:20px 0 8px}
  .redactor-drawer p{font-size:13.5px;line-height:1.72;color:var(--rd-sub);margin:12px 0}.redactor-drawer label{display:block;margin:18px 0 8px;font-size:14.5px;font-weight:560;line-height:1.72;color:var(--rd-ink-2)}
  .redactor-drawer textarea{width:100%;min-height:140px;resize:vertical;padding:11px 13px;border:1px solid var(--rd-line);border-radius:11px;font:inherit;font-size:14.5px;line-height:1.72;box-sizing:border-box;color:var(--rd-ink);background:var(--rd-surface-soft)}
  .redactor-drawer textarea:focus{outline:3px solid #3355ff1c;border-color:var(--rd-accent);background:#fff}.redactor-drawer .redactor-primary{width:100%;margin-top:12px}
  .redactor-diff{font-size:12.5px;border:1px solid var(--rd-line);border-radius:11px;overflow:hidden;margin:12px 0}.redactor-diff pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:11px 13px;max-height:170px;overflow:auto;font:12.5px/1.72 ui-monospace,SFMono-Regular,Menlo,monospace}
  .redactor-diff del,.redactor-diff ins{text-decoration:none;display:block}.redactor-diff del{background:#fef3f2;color:#9b3128}.redactor-diff ins{background:#eefaf2;color:#167444}
  .redactor-drawer .redactor-success{padding:14px;background:#eefaf2;border:1px solid #c7ecd7;border-radius:11px;color:#116b3f;font-size:14.5px;line-height:1.72}
  .redactor-more{position:relative}.redactor-more summary{font-size:13.5px;color:var(--rd-sub);cursor:pointer}.redactor-menu{position:absolute;right:0;top:28px;z-index:20;min-width:160px;padding:8px;background:#fff;border:1px solid var(--rd-line);border-radius:11px;box-shadow:0 8px 28px #0f1a3217;display:grid;gap:6px}
  @media(max-width:850px){.redactor-drawer{position:absolute;right:0;top:0;bottom:0;width:400px;max-width:92%;box-shadow:-8px 0 30px #0f1a3217}.redactor-header{padding:12px 16px;padding-inline-start:max(16px,calc(var(--dsh-frame-leading-clearance,0px) + 16px));gap:10px}}
  .redactor-help{width:min(760px,calc(100vw - 40px));max-height:calc(100vh - 56px);overflow:auto;padding:24px;border:1px solid #e2e7f1;border-radius:20px;background:#fff;color:#0d1626;box-shadow:0 24px 60px #0f1a3224;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-size:14.5px;line-height:1.72}
  .redactor-help::backdrop{background:#0f1a326b;backdrop-filter:blur(3px)}
  .redactor-help>header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px}.redactor-help h2{margin:0;font-size:20px;font-weight:650;line-height:1.4}.redactor-help-intro{margin:0 0 18px;color:#6b7a90;font-size:13.5px}
  .redactor-community{display:flex;align-items:center;justify-content:space-between;gap:24px;background:#f7f9fd;border:1px solid #e2e7f1;border-radius:16px;padding:20px}.redactor-community-copy{flex:1;min-width:0}.redactor-community h3{font-size:16px;font-weight:640;margin:0 0 10px;color:#0d1626}.redactor-community p{font-size:13.5px;line-height:1.72;color:#6b7a90;margin:8px 0}.redactor-community a{color:#3355ff;text-decoration:none}.redactor-community a:hover{text-decoration:underline}
  .redactor-community figure{margin:0;flex:none;width:220px;text-align:center}.redactor-community img{display:block;width:220px;height:220px;object-fit:contain;background:#fff;border-radius:9px}.redactor-community figcaption{color:#6b7a90;font-size:12.5px;margin-top:8px}.redactor-help-notes{padding-top:12px;color:#6b7a90;font-size:12.5px;line-height:1.72}.redactor-help-notes p{margin:8px 0}
  @media(max-width:620px){.redactor-help{padding:18px}.redactor-community{flex-direction:column;align-items:stretch;padding:16px;gap:16px}.redactor-community figure{align-self:center}.redactor-help h2{font-size:18px}}

  `;

  // Community update controller adapted from dsh-dpia-workbench (Apache-2.0).
  // COMMUNITY-CORE-BEGIN

 function createCommunityController(options={}){
  const endpoint='https://xingchendata.com/community/redaction.json';
  const fetcher=options.fetch||globalThis.fetch,now=options.now||Date.now;
  const later=options.setTimeout||setTimeout,cancel=options.clearTimeout||clearTimeout;
  const onChange=options.onChange||(()=>{});
  let running=false,visible=options.visible!==false,serial=0,request=null,refreshTimer=null,expiryTimer=null;
  let current={status:'loading'};
  function publish(value){current=value;onChange(value);}
  function clearTimers(){cancel(refreshTimer);cancel(expiryTimer);refreshTimer=expiryTimer=null;}
  function abortRequest(){if(request){request.abort.abort();request.finish();cancel(request.timer);request=null;}}
  function parse(data){
   if(data?.schemaVersion!==1||typeof data.revision!=='string'||!data.revision||data.revision.length>100||!Array.isArray(data.groups)||data.groups.length>12)throw Error('INVALID_CONFIG');
   const ids=new Set();
   const groups=data.groups.map(group=>{
    if(!group||typeof group.id!=='string'||!/^[a-z0-9][a-z0-9-]{0,63}$/.test(group.id)||ids.has(group.id)||typeof group.name!=='string'||!group.name.trim()||group.name.length>100||!['open','full','paused'].includes(group.state))throw Error('INVALID_GROUP');
    ids.add(group.id);
    if(typeof group.expiresAt!=='string'||!/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(group.expiresAt)||!Number.isFinite(Date.parse(group.expiresAt)))throw Error('INVALID_EXPIRY');
    const image=new URL(group.image);
    if(image.origin!=='https://xingchendata.com'||image.username||image.password||image.hash||!/^\/community\/images\/[a-zA-Z0-9_-]+\.(png|jpe?g|webp)$/.test(image.pathname))throw Error('INVALID_IMAGE');
    return {id:group.id,name:group.name,state:group.state,image:image.href,expiresAt:group.expiresAt,expires:Date.parse(group.expiresAt)};
   });
   const group=groups.find(item=>item.state==='open'&&item.expires>now());
   return group?{status:'ready',revision:data.revision,group}:{status:'unavailable',revision:data.revision};
  }
  async function boundedJson(response){
   if(!response.ok||response.redirected||!(response.headers.get('content-type')||'').toLowerCase().includes('application/json'))throw Error('CONFIG_HTTP_ERROR');
   if(Number(response.headers.get('content-length'))>65536)throw Error('CONFIG_TOO_LARGE');
   const reader=response.body?.getReader();if(!reader)throw Error('EMPTY_CONFIG');
   const decoder=new TextDecoder();let count=0,text='';
   try{for(;;){const part=await reader.read();if(part.done)break;count+=part.value.byteLength;if(count>65536){await reader.cancel();throw Error('CONFIG_TOO_LARGE');}text+=decoder.decode(part.value,{stream:true});}text+=decoder.decode();}
   finally{reader.releaseLock();}
   return JSON.parse(text);
  }
  function schedule(){cancel(refreshTimer);if(running&&visible)refreshTimer=later(()=>{void refresh();},300000);}
  async function refresh(){
   if(!running||!visible)return;
   const ticket=++serial;clearTimers();abortRequest();publish({status:'loading'});
   const abort=new AbortController();let rejectRequest;
   const interrupted=new Promise((_,reject)=>{rejectRequest=reject;});
   const active={abort,finish:()=>rejectRequest(Error('CONFIG_CANCELLED')),timer:null};
   active.timer=later(()=>{abort.abort();rejectRequest(Error('CONFIG_TIMEOUT'));},8000);request=active;
   try{
    const response=await Promise.race([Promise.resolve().then(()=>fetcher(endpoint+'?t='+now()+'-'+ticket,{cache:'no-store',credentials:'omit',mode:'cors',redirect:'error',referrerPolicy:'no-referrer',headers:{Accept:'application/json'},signal:abort.signal})),interrupted]);
    const data=await Promise.race([boundedJson(response),interrupted]);
    if(!running||!visible||ticket!==serial)return;
    const next=parse(data);publish(next);
    if(next.status==='ready')expiryTimer=later(()=>{if(running&&visible&&ticket===serial){publish({status:'loading'});void refresh();}},Math.min(next.group.expires-now(),2147483647));
   }catch(error){if(running&&visible&&ticket===serial)publish({status:'error'});}
   finally{cancel(active.timer);if(request===active)request=null;if(ticket===serial)schedule();}
  }
  return {
   start(){if(running)return;running=true;return refresh();},
   refresh,
   setVisible(value){const next=Boolean(value);if(visible===next)return;visible=next;if(!running)return;if(!next){serial++;clearTimers();abortRequest();publish({status:'loading'});}else return refresh();},
   imageFailed(image){if(running&&current.status==='ready'&&current.group.image===image){cancel(expiryTimer);publish({status:'error'});}},
   stop(){running=false;serial++;clearTimers();abortRequest();},
   state:()=>current
  };
 }
 
  // COMMUNITY-CORE-END
  function CommunityContact() {
    const [value,setValue]=React.useState({status:'loading'});
    const controller=React.useRef(null),container=React.useRef(null);
    React.useEffect(()=>{
      const doc=document,win=window;let intersecting=true;
      const ctrl=createCommunityController({onChange:setValue,visible:!doc.hidden});controller.current=ctrl;
      const visibility=()=>ctrl.setVisible(intersecting&&!doc.hidden);
      const focus=()=>{if(intersecting&&!doc.hidden)void ctrl.refresh();};
      const observer=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{intersecting=entries.some(entry=>entry.isIntersecting);visibility();}):null;
      if(observer&&container.current)observer.observe(container.current);
      doc.addEventListener('visibilitychange',visibility);win.addEventListener('focus',focus);void ctrl.start();
      return()=>{observer?.disconnect();doc.removeEventListener('visibilitychange',visibility);win.removeEventListener('focus',focus);ctrl.stop();controller.current=null;};
    },[]);
    const ready=value.status==='ready';
    const status=ready?'扫码加群，交流使用问题与功能建议。':value.status==='loading'?'正在获取最新群二维码…':value.status==='unavailable'?'新的入群入口正在准备，可稍后再查看或访问工具官网。':'群二维码暂时无法加载，可重试或访问工具官网。';
    return h('section',{className:'redactor-community',ref:container,'aria-label':'脱敏工作台技术交流群'},
      h('div',{className:'redactor-community-copy'},h('h3',null,ready?value.group.name:'脱敏工作台技术交流群'),h('p',{role:'status','aria-live':'polite'},status),
        value.status==='error'&&h('button',{type:'button',className:'redactor-btn',onClick:()=>controller.current?.refresh()},'重试加载'),
        h('p',null,h('a',{href:'https://xingchendata.com/dsh-document-redactor.html#community',target:'_blank',rel:'noopener noreferrer'},'访问工具官网（帮助与反馈） ↗'))),
      ready&&h('figure',null,h('img',{key:value.group.image,src:value.group.image,alt:value.group.name+'二维码',width:220,height:220,referrerPolicy:'no-referrer',onError:()=>controller.current?.imageFailed(value.group.image)}),h('figcaption',null,'微信群 · 产品交流与反馈')));
  }
  // INITIALIZATION-HELP-BEGIN
  const INITIALIZATION_PROMPT = '请排查本机“文书脱敏”插件为什么初始化失败。先只读排查，不安装依赖、不修改文件。请确认实际安装的插件版本、系统和 Python/pip 版本，定位插件实际数据目录中的 environment.log；如果是服务启动失败，再检查 service.log，不要只查当前工作区。请把最近一次失败的关键日志原文（错误附近最多40行）发出来，保留错误码、包名与版本，并说明失败在哪一步、建议怎样修复。如涉及依赖冲突，请列出冲突包及约束，不要直接判定为误报。请勿读取或输出文书、规则和映射内容；输出前隐去账号、密钥、代理密码和个人路径信息。';

  async function copyInitializationPrompt(field, clipboard = globalThis.navigator?.clipboard) {
    try {
      if (!clipboard?.writeText) throw new Error('CLIPBOARD_UNAVAILABLE');
      await clipboard.writeText(INITIALIZATION_PROMPT);
      return 'copied';
    } catch {
      if (!field) return 'unavailable';
      field.focus();
      field.select();
      return 'selected';
    }
  }
  // INITIALIZATION-HELP-END

  function InitializationHelp({failed = false}) {
    const field = React.useRef(null);
    const [copyState, setCopyState] = React.useState('');
    return h('section', {className:'redactor-faq', 'aria-label':'常见问题与排查'},
      h('h3', null, '常见问题与排查'),
      h('details', {open:failed},
        h('summary', null, '初始化失败或打不开，怎样让 DSH 帮我排查？'),
        h('ol', null,
          h('li', null, '点击下方“复制排查指令”。'),
          h('li', null, '点击左上角“返回 DSH”，新建一个会话（任务）。'),
          h('li', null, '粘贴指令并发送，让 DSH 查找本机插件的失败日志。'),
          h('li', null, '将返回的插件版本、操作步骤和关键日志发给维护者。')),
        h('textarea', {ref:field, readOnly:true, value:INITIALIZATION_PROMPT, rows:7, 'aria-label':'可复制的初始化失败排查指令'}),
        h('button', {type:'button', className:'redactor-btn', onClick:async()=>setCopyState(await copyInitializationPrompt(field.current))}, copyState==='copied'?'已复制，可返回 DSH':'复制排查指令'),
        h('p', {role:'status', 'aria-live':'polite'}, copyState==='copied'?'复制成功。返回 DSH，新建会话后粘贴发送。':copyState==='selected'?'自动复制未成功，已选中文字；请按 Ctrl+C（Mac 按 Command+C）复制。':copyState==='unavailable'?'自动复制暂不可用，请手动选中上方指令复制。':'这段指令只排查和提取日志，不会要求 DSH 自动修复。')),
      h('details', null, h('summary', null, '下载依赖很慢或超时，怎么办？'),
        h('p', null, '在未打开工具的页面找到“依赖下载源”，改选“清华 TUNA 镜像”或“PyPI 官方源”，再点“重试打开”。不同网络可用的源不同；1.4.0 需要手动切换，会保留已安装依赖和缓存。'),
        h('p', null, '切源后仍失败，请按上面的步骤提取日志。不要反复卸载插件或清空文书数据。')),
      h('details', null, h('summary', null, '添加插件时提示“无法识别包名或地址”，怎么办？'),
        h('p', null, '在线安装须粘贴完整的 TGZ 下载地址，从 https:// 开始，到 .tgz 结束。也可先下载 TGZ，再填本机文件的完整路径；不要只填文件名。Windows“复制文件地址”带出的最外层双引号需要删掉，路径里的空格保留。')),
      h('details', null, h('summary', null, '反馈时需要发什么？'),
        h('p', null, '请发插件版本、系统、失败步骤、错误附近的日志原文及必要截图。日志可能含个人路径或账号，请先遮盖；无需发送真实文书、规则全文或映射文件。')));
  }

  function CommunityHelp({onClose}) {
    const dialog=React.useRef(null);
    React.useEffect(()=>{const previous=document.activeElement;dialog.current?.showModal();return()=>{if(dialog.current?.open)dialog.current.close();previous?.focus?.();};},[]);
    return h('dialog',{ref:dialog,className:'redactor-help','aria-labelledby':'redactor-help-title',onCancel:event=>{event.preventDefault();onClose();},onClick:event=>{if(event.target===event.currentTarget){const box=event.currentTarget.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)onClose();}}},
      h('header',null,h('h2',{id:'redactor-help-title'},'帮助与反馈'),h('button',{type:'button',className:'redactor-btn',onClick:onClose,'aria-label':'关闭帮助与反馈'},'关闭 ×')),
      h('p',{className:'redactor-help-intro'},'上传文书 → 复核敏感内容 → 导出脱敏文件。识别有遗漏时，可补字段、框选遮盖，或让 Agent 调整通用规则。'),
      h(InitializationHelp),
      h(CommunityContact),
      h('div',{className:'redactor-help-notes'},h('p',null,'反馈问题时，请说明插件版本、操作步骤和报错；截图请先遮盖敏感内容，避免发送真实文书与映射。'),h('p',null,'群码从官网自动更新。获取时不附带您的文书、规则或对话。')));
  }

  function Workbench({ rpc, controls }) {
    const [state, setState] = React.useState({ stage: 'idle' });
    const [error, setError] = React.useState('');
    const [downloadSource, setDownloadSource] = React.useState('saved');
    const [drawer, setDrawer] = React.useState(false);
    const [help, setHelp] = React.useState(false);
    const [requestText, setRequestText] = React.useState('');
    const [rules, setRules] = React.useState({ stage: 'idle' });
    const [ruleError, setRuleError] = React.useState('');
    const [deletionConfirmed, setDeletionConfirmed] = React.useState(false);
    const [applying, setApplying] = React.useState(false);
    const [rollbackConfirmed, setRollbackConfirmed] = React.useState(false);
    const frame = React.useRef(null), mounted = React.useRef(false);
    const request = React.useCallback(async (action, options = {}) => {
      try { const value = await rpc({ action, ...options }); if (mounted.current) { setState(value); setError(''); } }
      catch (cause) { if (mounted.current) setError(cause.message || '无法连接插件，请刷新DSH后重试。'); }
    }, [rpc]);
    React.useEffect(() => {
      mounted.current = true;
      const wasOpen = !document.querySelector('[data-sidebar-collapsed]');
      if (wasOpen) controls.toggleSidebar();
      request('status');
      return () => { mounted.current = false; if (wasOpen && document.querySelector('[data-sidebar-collapsed]')) controls.toggleSidebar(); };
    }, [request, controls]);
    React.useEffect(() => {
      if (['idle','error'].includes(state.stage)) return;
      const timer = setInterval(() => request('status'), state.stage === 'ready' ? 5000 : 1200);
      return () => clearInterval(timer);
    }, [state.stage, request]);
    React.useEffect(() => {
      if (!state.url) return;
      const receive = event => {
        if (event.source !== frame.current?.contentWindow || event.origin !== new URL(state.url).origin) return;
        if (event.data?.type === 'dsh-redactor:rules') { setDrawer(true); setRuleError(''); }
      };
      window.addEventListener('message', receive); return () => window.removeEventListener('message', receive);
    }, [state.url]);
    React.useEffect(() => {
      if (!drawer || state.stage !== 'ready') return;
      let live = true;
      const refresh = async () => { try { const value = await rpc({action:'rules-status'}); if (live) setRules(value); } catch (cause) { if(live) setRuleError(cause.message); } };
      refresh(); const timer = setInterval(refresh, 1500);
      return () => { live = false; clearInterval(timer); };
    }, [drawer, state.stage, rpc]);
    const generate = async () => {
      setRuleError(''); setDeletionConfirmed(false); setRollbackConfirmed(false);
      try { setRules(await rpc({ action:'rules-propose', request:requestText })); }
      catch (cause) { setRuleError(cause.message); }
    };
    const discardRules = async () => {
      setApplying(true); setRuleError('');
      try { setRules(await rpc({action:'rules-discard'})); setDeletionConfirmed(false); setRollbackConfirmed(false); }
      catch(cause) { setRuleError(cause.message); }
      finally { setApplying(false); }
    };
    const previewRollback = async id => {
      setApplying(true); setRuleError(''); setDeletionConfirmed(false); setRollbackConfirmed(false);
      try { setRules(await rpc({action:'rules-rollback-preview',id})); }
      catch(cause) { setRuleError(cause.message); }
      finally { setApplying(false); }
    };
    const applyRules = async () => {
      setApplying(true); setRuleError('');
      try {
        const value = await rpc({action:'rules-apply',id:rules.id,confirmedDeleted:deletionConfirmed ? rules.deleted || [] : [],confirmedRollback:rules.kind==='rollback' && rollbackConfirmed});
        setRules(value);
        frame.current?.contentWindow.postMessage({ type:'dsh-redactor:rules-applied' }, new URL(state.url).origin);
      } catch (cause) { setRuleError(cause.message); }
      finally { setApplying(false); }
    };
    const btn = (text, onClick, disabled, primary) => h('button',{type:'button',className:'redactor-btn'+(primary?' redactor-primary':''),onClick,disabled},text);
    const busy = ['preparing','starting','stopping'].includes(state.stage);
    return h('section',{className:'redactor-app','aria-label':'文书脱敏工作台'},
      h('style',null,css),
      help && h(CommunityHelp,{onClose:()=>setHelp(false)}),
      h('header',{className:'redactor-header'},
        h('button',{className:'redactor-btn redactor-link',onClick:controls.back},'← 返回DSH'),
        h('h2',null,'文书脱敏'),h('span',{role:'status'},labels[state.stage] || '状态未知'),h('span',{className:'redactor-spacer'}),
        state.stage === 'ready' && btn('历史记录',()=>{frame.current.src=state.url+'history';setDrawer(false);}),
        state.stage === 'ready' && btn('识别规则',()=>setDrawer(true)),
        btn('帮助与反馈',()=>{setDrawer(false);setHelp(true);}),
        !['ready','preparing','starting','stopping'].includes(state.stage) && btn(state.error === 'STOP_FAILED'?'重试停止':state.stage === 'error'?'重试打开':'打开脱敏工具',()=>request(state.error === 'STOP_FAILED'?'stop':'start',downloadSource === 'saved' ? {} : {source:downloadSource}),false,true),
        ['preparing','starting'].includes(state.stage) && btn('取消准备',()=>request('stop')),
        state.stage === 'ready' && h('details',{className:'redactor-more'},h('summary',null,'更多'),h('div',{className:'redactor-menu'},
          btn('停止本机工具',()=>request('stop'))))),
      (error || state.error) && h('p',{role:'alert',className:'redactor-alert'},error || errors[state.error] || '操作未完成，请重试。'),
      h('div',{className:'redactor-body'},
        state.stage === 'ready' ? h('iframe',{ref:frame,src:state.url,title:'本机文书脱敏与复核',referrerPolicy:'no-referrer',sandbox:'allow-scripts allow-same-origin allow-downloads allow-forms allow-popups allow-modals'}) :
          h('div',{className:'redactor-start'},h('h1',null,'上传文书，确认后再导出'),h('p',null,'1 上传文书　→　2 复核敏感内容　→　3 导出脱敏文件'),h('p',null,'文书在本机处理。支持Word、PDF、TXT、Markdown。自动识别可能漏项，外发前请核对最终文件。'),
            h('label',null,'依赖下载源　',h('select',{value:downloadSource,disabled:busy,onChange:event=>setDownloadSource(event.target.value),'aria-label':'依赖下载源'},
              h('option',{value:'saved'},'沿用上次设置（首次使用系统默认）'),h('option',{value:'default'},'系统默认源'),h('option',{value:'pypi'},'PyPI 官方源'),h('option',{value:'tuna'},'清华 TUNA 镜像'))),
            h('p',null,'仅首次准备或修复依赖时使用；不会修改系统 pip 设置，也不向下载源发送文书。'),
            h('p',{role:'status'},busy?(preparationLabels[state.preparation] || labels[state.stage]):'点击右上角“打开脱敏工具”开始。准备失败可切换下载源重试，已安装依赖与下载缓存会保留。'),h(InitializationHelp,{failed:state.stage==='error'}),h(CommunityContact)),
        drawer && state.stage === 'ready' && h('aside',{className:'redactor-drawer','aria-label':'Agent识别规则助手'},
          h('div',{style:{display:'flex',alignItems:'center',justifyContent:'space-between',gap:10}},h('h3',null,'调整识别规则'),btn('收起',()=>setDrawer(false))),
          h('p',null,'本份文书只漏一处，可在复核页直接补字段或框选。需要后续文书也自动识别时，再修改通用规则。'),
          h('label',{htmlFor:'redactor-rule-request'},'您希望怎样识别或处理？'),
          h('textarea',{id:'redactor-rule-request',value:requestText,maxLength:3000,onChange:event=>setRequestText(event.target.value),placeholder:'例如：把形如 BATCH-123456 的批号识别为敏感编号，替换为代号。请使用虚构示例。',disabled:rules.stage==='generating'||applying}),
          h('p',null,'生成时会把当前规则全文和您填写的需求提交给DSH配置的模型；使用云端模型时需要联网。上传文书、候选原文与映射不会自动附带。您写进规则或需求的内容也会提交给模型，请使用虚构示例。生成后先查看改动，再应用。'),
          rules.stage === 'generating' ? h('div',null,h('p',{role:'status'},'Agent正在生成修改方案…'),btn('取消生成',async()=>{try{setRules(await rpc({action:'rules-cancel'}));}catch(cause){setRuleError(cause.message);}})) :
            btn(rules.stage === 'preview'?'重新生成方案':'让 Agent 生成修改方案',generate,!requestText.trim()||applying,true),
          (ruleError || rules.message && ['error','cancelled'].includes(rules.stage)) && h('p',{role:'alert',style:{color:'#a32626'}},ruleError || rules.message),
          rules.stage === 'preview' && h('div',null,
            h('h4',null,rules.kind === 'rollback'?'核对回退差异':'查看改动'),h('p',null,rules.summary),
            ...rules.edits.map((edit,i)=>h('div',{className:'redactor-diff',key:i},h('del',null,h('pre',null,'修改前\n'+edit.before)),h('ins',null,h('pre',null,'修改后\n'+edit.after)))),
            rules.deleted?.length > 0 && h('label',null,h('input',{type:'checkbox',checked:deletionConfirmed,onChange:event=>setDeletionConfirmed(event.target.checked)}),'确认删除这些自定义规则：'+rules.deleted.join('、')),
            rules.kind === 'rollback' && h('label',null,h('input',{type:'checkbox',checked:rollbackConfirmed,onChange:event=>setRollbackConfirmed(event.target.checked)}),'确认恢复到所选记录变更前的完整规则，撤销此后的修改'),
            h('p',null,'已通过规则格式校验。应用后会保存原规则备份；新规则对后续上传生效，当前文书需要重新上传识别。'),
            btn(applying?'正在应用…':rules.kind==='rollback'?'确认回退规则':'应用规则',applyRules,applying || rules.deleted?.length > 0 && !deletionConfirmed || rules.kind==='rollback' && !rollbackConfirmed,true),
            btn('放弃方案',discardRules,applying)),
          rules.stage === 'idle' && rules.message && h('p',{role:'status'},rules.message),
          rules.stage === 'applied' && h('div',{className:'redactor-success',role:'status'},h('strong',null,'规则已应用'),h('p',null,rules.message),btn('去重新上传识别',()=>{frame.current.src=state.url;setDrawer(false);},false,true)),
          h('details',null,h('summary',null,'规则应用记录与回退'),h('p',null,'最近100条记录。回退前先核对差异；“恢复最近有效配置”用于配置错误恢复，与撤销一次业务改动不同。历史版本仅有备份但无应用记录的，不会伪造为可回退记录。'),
            !(rules.history||[]).length && h('p',null,'暂无可回退的应用记录。'),
            ...(rules.history||[]).map(record=>h('div',{key:record.id,className:'redactor-diff'},h('p',null,(record.kind==='rollback'?'回退 · ':'应用 · ')+new Date(record.createdAt).toLocaleString()+' · '+record.summary),btn('查看变更前版本',()=>previewRollback(record.id),applying||rules.stage==='generating'))))))
      );
  }
  exports.inject = ['slots','connection','layout'];
  exports.apply = function(ctx) {
    const rpc = async payload => {
      const result = await ctx.connection.rpc.call('/api','document.redactor',payload);
      if (!result.ok) throw new Error(result.error?.message || '无法连接本机工具。');
      return result.value;
    };
    const controls = { toggleSidebar:()=>ctx.layout.toggleSidebar(), back:()=>ctx.layout.selectPanel(null) };
    ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'document-redactor',inject:()=>({rpc,controls})},Workbench));
    ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:'document-redactor',label:'文书脱敏',order:35},()=>h('span',{'aria-hidden':true},'▧')));
  };
  return exports;
} });
