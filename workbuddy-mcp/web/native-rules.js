(() => {
  if(window.parent!==window)window.parent.postMessage({type:'redactor-local-ready'},'*');
  const instruction = '请处理本机工作台已批准的规则方案。仅调用 document-redactor 的 redaction_rules_context，按返回的systemPrompt和prompt生成最多12处最小修改，再调用redaction_rules_submit提交JSON方案。不要读文件、使用终端、访问网页或调用其他工具。提交后停下，等待我在本机页面核对并确认应用。';
  const insert = () => {
    const drawer = document.querySelector('.redactor-drawer');
    if (!drawer || drawer.querySelector('#native-rules-instructions')) return;
    const panel = document.createElement('section');
    panel.id = 'native-rules-instructions';
    panel.style.cssText = 'padding:12px;border:1px solid #c9d5ff;border-radius:8px;margin:12px 0';
    const text = document.createElement('p');
    text.textContent = '先准备好 WorkBuddy 会话，在下方填写需求并点击生成，再回到 WorkBuddy 发送规则任务。方案返回后，本页会显示差异，等待您确认应用。规则任务等待时间为2分钟。';
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '复制 WorkBuddy 规则任务';
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(instruction);
        button.textContent = '已复制，请在 WorkBuddy 中发送';
      } catch {
        const field = document.createElement('textarea');
        field.value = instruction;
        field.readOnly = true;
        panel.append(field);
        field.select();
      }
    });
    panel.append(text, button);
    const header = drawer.querySelector('header') || drawer.firstElementChild;
    header.after(panel);
  };
  new MutationObserver(insert).observe(document.body, { childList: true, subtree: true });
  insert();
})();

// In WorkBuddy's native app panel the host supplies expand/back controls.
// Keep the released document workbench and remove only redundant framing.
(() => {
  if (window.parent === window) return;
  document.documentElement.dataset.workbuddyEmbedded = 'true';
  const style = document.createElement('style');
  style.textContent = `
    html[data-workbuddy-embedded] .redactor-header{gap:10px;padding:10px 18px;background:#fff}
    html[data-workbuddy-embedded] .redactor-header>.redactor-link{display:none}
    html[data-workbuddy-embedded] .redactor-header h2{font-size:16px;letter-spacing:-.02em}
    html[data-workbuddy-embedded] .redactor-header [role=status]{font-size:12px}
    html[data-workbuddy-embedded] .redactor-body{overflow:hidden}
    html[data-workbuddy-embedded] .redactor-body iframe{display:block}
    html[data-workbuddy-embedded] .redactor-header .redactor-btn{padding:8px 12px}
    @media(max-width:700px){html[data-workbuddy-embedded] .redactor-header{gap:8px;padding:10px 12px}html[data-workbuddy-embedded] .redactor-header .redactor-btn{padding:7px 9px}}
  `;
  document.body.append(style);
})();

// Save completed exports to the private local export directory; never send bytes to the host/model.
(() => {
  const attach=iframe=>{
    if(iframe.dataset.redactorDownloadHook)return;iframe.dataset.redactorDownloadHook='1';
    const loaded=()=>{try{
      const child=iframe.contentWindow,doc=child.document;
      if(child.location.origin!==location.origin||doc.documentElement.dataset.redactorDownloadHook)return;
      doc.documentElement.dataset.redactorDownloadHook='1';
      doc.addEventListener('click',async event=>{
        const anchor=event.target.closest?.('a');if(!anchor||event.defaultPrevented||window.parent===window)return;
        const target=new URL(anchor.href,location.href),match=target.pathname.match(/^\/(?:history\/)?download\/([a-f0-9]{12})\/(.+)$/);
        if(target.origin!==location.origin||!match)return;
        const name=decodeURIComponent(match[2]);if(!/\.(docx|pdf|txt|md|xlsx|json)$/i.test(name)||/[\\/\r\n]/.test(name))return;
        event.preventDefault();
        let notice=doc.getElementById('redactor-native-save');
        if(!notice){notice=doc.createElement('div');notice.id='redactor-native-save';notice.style.cssText='position:fixed;top:16px;left:16px;right:16px;z-index:99999;background:#fff;border:2px solid #5b4085;padding:16px;color:#222;font-size:16px';doc.body.append(notice);}
        notice.textContent='正在保存到本机导出目录…';
        try{
          const response=await fetch('/native-export',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'save',sid:match[1],name})});if(!response.ok)throw Error();
          const result=await response.json();notice.textContent='已保存到本机导出目录：'+result.alias+' ';
          const open=doc.createElement('button');open.textContent='打开文件';open.addEventListener('click',async()=>{const r=await fetch('/native-export',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'open',alias:result.alias})});if(!r.ok)notice.textContent='打开失败，文件仍保存在本机导出目录。';});notice.append(open);
          const close=doc.createElement('button');close.textContent='关闭提示';close.addEventListener('click',()=>notice.remove());notice.append(close);
        }catch{notice.textContent='保存失败，正式导出件仍保存在历史记录中，请重试。';}
      });
    }catch{}};iframe.addEventListener('load',loaded);loaded();
  };
  const scan=()=>document.querySelectorAll('iframe').forEach(attach);new MutationObserver(scan).observe(document.body,{childList:true,subtree:true});scan();
})();
