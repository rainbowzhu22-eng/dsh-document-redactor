export const APP_URI = 'ui://document-redactor/workbench-v4';
export const LEGACY_APP_URI = 'ui://document-redactor/workbench-v2';
export const LEGACY_APP_V3_URI = 'ui://document-redactor/workbench-v3';
export const ACCESS_URI = 'redactor-local://workbench/access';
export const APP_MIME = 'text/html;profile=mcp-app';

function localOrigin(origin) {
  const value = new URL(origin);
  if (value.protocol !== 'http:' || value.hostname !== '127.0.0.1' || value.origin !== origin)
    throw Error('INVALID_WIDGET_TARGET');
}

export function accessResource({ url, origin }) {
  localOrigin(origin);
  const target = new URL(url);
  if (target.origin !== origin || target.username || target.password ||
      !/^\/ticket\/[a-f0-9]{64}$/.test(target.pathname) || target.search || target.hash)
    throw Error('INVALID_WIDGET_TARGET');
  return { contents: [{ uri: ACCESS_URI, mimeType: 'application/json', text: JSON.stringify({ url, origin }) }] };
}

// Cached HTML has no credentials. Each mount asks the host for a fresh UI-only resource.
export function appResource({ origin }, uri = APP_URI) {
  localOrigin(origin);
  return { contents: [{ uri, mimeType: APP_MIME,
    _meta: { ui: { prefersBorder: false, csp: {
      frameDomains: [origin], resourceDomains: [], connectDomains: [],
    } } },
    text: `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>文书脱敏工作台</title><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#eef1f8}iframe{display:block;width:100%;height:100vh;border:0}#status{position:absolute;inset:0;z-index:2;display:grid;place-items:center;margin:0;padding:24px;background:#f7f9fd;color:#39465a;font:14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}#status[hidden]{display:none}</style></head><body><p id="status" role="status">正在打开文书脱敏…</p><iframe title="本机文书脱敏与复核工作台"></iframe><script>
      const expectedOrigin=${JSON.stringify(origin)}, frame=document.querySelector('iframe'), status=document.getElementById('status');
      const send=(method,params,id)=>parent.postMessage({jsonrpc:'2.0',method,params,...(id?{id}:{})},'*');
      let ready=false;
      window.addEventListener('message',event=>{
        if(event.source===frame.contentWindow&&event.origin===expectedOrigin&&event.data?.type==='redactor-local-ready'){
          ready=true;status.hidden=true;return;
        }
        if(event.source!==window.parent)return;
        const m=event.data;
        if(m?.id==='redactor-ui-init'&&m.result){
          send('ui/notifications/initialized');send('ui/notifications/size-changed',{height:790});
          send('resources/read',{uri:${JSON.stringify(ACCESS_URI)}},'redactor-access');
        }
        if(m?.id==='redactor-access'){
          try{
            const access=JSON.parse(m.result.contents[0].text),target=new URL(access.url);
            if(target.origin!==expectedOrigin||target.username||target.password||!/^\\/ticket\\/[a-f0-9]{64}$/.test(target.pathname)||target.search||target.hash)throw Error();
            frame.src=target.href;
          }catch{status.textContent='连接本机工作台失败，请重新打开。';}
        }
      });
      setTimeout(()=>{if(!ready)status.textContent='本机页面尚未就绪，请重新打开工作台。';},15000);
      send('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'document-redactor',version:'0.4.0'},appCapabilities:{}},'redactor-ui-init');
    </script></body></html>` }] };
}
