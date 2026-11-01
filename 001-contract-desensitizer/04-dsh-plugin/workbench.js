(() => {
  'use strict';
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
  const requestRules = () => window.parent.postMessage({ type: 'dsh-redactor:rules' }, '*');
  const rulesButton = () => { const b = el('button', '让 Agent 调整识别规则', 'btn sm dsh-rule-trigger'); b.type = 'button'; b.onclick = requestRules; return b; };
  const communityCard = () => {
    const card=el('section','','card dsh-community');card.setAttribute('aria-label','帮助与反馈');
    const title=el('h2','帮助与反馈'),name=el('p','脱敏工作台技术交流群','dsh-community-name');
    const status=el('p','正在获取最新群二维码…');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const figure=el('figure'),retry=el('button','重试加载','btn sm');retry.type='button';retry.hidden=true;
    const link=el('a','访问工具官网（帮助与反馈） ↗');link.href='https://xingchendata.com/dsh-document-redactor.html#community';link.target='_blank';link.rel='noopener noreferrer';
    card.append(title,name,status,figure,retry,link);
    let intersecting=true,stopped=false;
    const ctrl=createCommunityController({visible:!document.hidden,onChange(value){
      figure.replaceChildren();retry.hidden=value.status!=='error';
      status.textContent=value.status==='ready'?'扫码加群，交流使用问题与功能建议。':value.status==='loading'?'正在获取最新群二维码…':value.status==='unavailable'?'新的入群入口正在准备，可稍后再查看或访问工具官网。':'群二维码暂时无法加载，可重试或访问工具官网。';
      name.textContent=value.status==='ready'?value.group.name:'脱敏工作台技术交流群';
      if(value.status==='ready'){
        const image=el('img');image.alt=value.group.name+'二维码';image.width=180;image.height=180;image.referrerPolicy='no-referrer';
        image.onerror=()=>ctrl.imageFailed(value.group.image);image.src=value.group.image;figure.append(image);
      }
    }});
    const visibility=()=>ctrl.setVisible(intersecting&&!document.hidden);
    const focus=()=>{if(intersecting&&!document.hidden)void ctrl.refresh();};
    const observer=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{intersecting=entries.some(entry=>entry.isIntersecting);visibility();}):null;
    retry.onclick=()=>ctrl.refresh();observer?.observe(card);
    const stop=()=>{if(stopped)return;stopped=true;observer?.disconnect();document.removeEventListener('visibilitychange',visibility);window.removeEventListener('focus',focus);ctrl.stop();};
    document.addEventListener('visibilitychange',visibility);window.addEventListener('focus',focus);window.addEventListener('pagehide',stop,{once:true});
    window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();},{once:true});
    void ctrl.start();return card;
  };
  document.body.classList.add('dsh-workbench');
  // Navigation messages carry no document text, filename, candidates or mapping.
  window.parent.postMessage({ type: 'dsh-redactor:page', page: location.pathname === '/' ? 'upload' : /^\/pdf\/|^\/review/.test(location.pathname) ? 'review' : 'other' }, '*');
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.type !== 'dsh-redactor:rules-applied') return;
    const old = document.getElementById('dsh-rules-applied'); if (old) old.remove();
    const notice = el('div', '', 'dsh-rule-notice'); notice.id = 'dsh-rules-applied'; notice.setAttribute('role', 'status');
    notice.append(el('span', '规则已更新。请重新上传当前文书，以新规则重新识别；已有复核选择与导出文件不会自动改变。'));
    const go = el('a', '重新上传识别', 'btn primary sm'); go.href = '/'; notice.append(go);
    (document.querySelector('main') || document.body).prepend(notice);
    notice.scrollIntoView({ block: 'start', behavior: 'smooth' });
  });
  const form = document.getElementById('upload-form');
  if (form) {
    document.body.classList.add('dsh-upload');
    const grid = form.querySelector('.grid'), stack = grid?.firstElementChild;
    if (!stack) return;
    const intro = document.querySelector('.hero');
    intro.querySelector('h1').textContent = '文书脱敏';
    intro.querySelector('p').textContent = '上传文书，在本机识别敏感内容；逐项复核后，导出可对外使用的脱敏稿。';
    intro.querySelector('.eyebrow').textContent = '本机处理 · 人工复核后导出';
    const cards = Array.from(stack.children);
    const rulesCard = cards.find(card => card.querySelector('#rules-file-status'));
    const modeCard = document.getElementById('mode-picker')?.closest('.card');
    const uploadCard = document.getElementById('drop')?.closest('.card');
    const submitCard = document.getElementById('submit-btn')?.closest('.card');
    modeCard?.classList.add('dsh-mode-card');
    modeCard?.querySelector('h2')?.remove();
    document.querySelectorAll('#mode-picker .mode').forEach(node => {
      node.setAttribute('role', 'button'); node.tabIndex = 0;
      node.setAttribute('aria-label', node.dataset.mode === 'redact' ? '脱敏文书' : '还原已有文书');
      node.querySelector('.name').textContent = node.dataset.mode === 'redact' ? '脱敏文书' : '还原已有文书';
      node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); node.click(); } };
      node.addEventListener('click', () => {
        const restore = node.dataset.mode === 'restore';
        intro.querySelector('h1').textContent = restore ? '文书还原' : '文书脱敏';
        intro.querySelector('p').textContent = restore ? '上传脱敏稿并提供对应映射，恢复文字内容。PDF原件请从历史记录取回。' : '上传文书，在本机识别敏感内容；逐项复核后，导出可对外使用的脱敏稿。';
        document.querySelectorAll('.dsh-guidance .olist li>span:last-child').forEach((span, i) => { span.textContent = (restore ? ['上传已有的脱敏文书','提供对应的原文映射文件','核对并下载还原文书'] : ['上传需要脱敏的原始文书','复核识别结果，补充遗漏内容','确认后导出脱敏文件'])[i]; });
        document.querySelectorAll('#mode-picker .mode').forEach(mode => mode.setAttribute('aria-pressed', String(mode === node)));
      });
      node.setAttribute('aria-pressed', String(node.classList.contains('active')));
    });
    uploadCard?.classList.add('dsh-upload-card');
    if (uploadCard) {
      uploadCard.querySelector('h2').textContent = '上传需要处理的文书';
      const drop = document.getElementById('drop');
      drop.setAttribute('role', 'button'); drop.setAttribute('aria-label', '选择或拖入文书'); drop.tabIndex = 0;
      drop.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); document.getElementById('file-input').click(); } });
      const note = document.getElementById('local-service-note'); if (note) note.hidden = true;
      const help = document.getElementById('uploadHelp'); if (help) help.textContent = '支持 Word、PDF、TXT、Markdown。自动识别后由您逐项确认。';
      const advanced = el('details', '', 'dsh-advanced'); advanced.append(el('summary', '同一案件的多份文书（可选）'));
      const caseField = document.getElementById('case-name')?.closest('label');
      const explanation = caseField?.nextElementSibling;
      if (caseField) advanced.append(caseField);
      if (explanation?.tagName === 'P') advanced.append(explanation);
      uploadCard.append(advanced);
      const select = document.getElementById('redaction-policy');
      if (select) {
        select.options[0].textContent = '请选择：金额、日期是否也要隐藏';
        select.options[1].textContent = '隐藏金额、日期'; select.options[2].textContent = '保留金额、日期';
      }
      if (typeof LABEL !== 'undefined') { LABEL.redact = '开始识别'; LABEL.restore = '上传并还原'; syncFileMode(); }
    }
    // The next action stays beside the upload fields, rather than in a distant card.
    if (uploadCard && submitCard) { while (submitCard.firstChild) uploadCard.append(submitCard.firstChild); submitCard.remove(); }
    const optionalCase = uploadCard?.querySelector('.dsh-advanced');
    if (optionalCase) uploadCard.append(optionalCase);
    const ruleRow = el('div', '', 'card dsh-rule-row');
    const copy = el('div'); copy.append(el('h2', '识别规则'), el('p', '默认规则可直接使用。复核发现漏识别或误识别时，再让 Agent 调整通用规则。'));
    ruleRow.append(copy, rulesButton());
    const more = el('details', '', 'dsh-advanced dsh-more'); more.append(el('summary', '识别范围、使用说明与高级设置'));
    if (rulesCard) {
      // Keep the fixed editor/recovery controls; replace the old copy-and-paste AI flow.
      rulesCard.querySelector('details')?.remove(); more.append(rulesCard);
    }
    const side = grid.children[1];
    if (side) {
      const guide = side.querySelector('.olist')?.closest('.card');
      if (guide) {
        guide.querySelector('h2').textContent = '使用流程';
        guide.querySelectorAll('.olist li').forEach((item, i) => { if (i > 2) item.remove(); });
        guide.querySelectorAll('.olist li>span:last-child').forEach((span,i) => { span.textContent = ['上传需要脱敏的原始文书','复核识别结果，补充遗漏内容','确认后导出脱敏文件'][i]; });
      }
      Array.from(side.children).forEach(card => { if (card !== guide) more.append(card); });
      side.classList.add('dsh-guidance');
      if (guide) side.append(guide);
      side.append(communityCard(),ruleRow);
    } else stack.append(ruleRow);
    stack.append(more);
  } else if (/^\/pdf\/|^\/review/.test(location.pathname)) {
    document.body.classList.add('dsh-review');
    const bar = el('div', '', 'dsh-review-help');
    const copy = el('span', '本份文书漏项，可直接补字段或框选遮盖。希望以后也能自动识别？');
    bar.append(copy, rulesButton());
    const target = document.querySelector('aside') || document.querySelector('main');
    if (target) target.prepend(bar);
  }
})();
