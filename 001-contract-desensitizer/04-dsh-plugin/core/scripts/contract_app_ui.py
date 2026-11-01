"""合同脱敏 / 还原 —— Web UI 模板层（纯标准库，无第三方依赖）。

把所有 HTML / CSS / JS 模板集中在这里，与 HTTP 处理逻辑（contract_app_server.py）
解耦，这样调整视觉设计时不必触碰服务端行为。

约定：
- 所有模板都是「纯字符串 + __TOKEN__ 占位」，使用 str.replace 注入，
  不使用 str.format / f-string —— 避免与 CSS/JS 里的大量花括号打架。
"""
from __future__ import annotations

import html as _html
import json
import urllib.parse
from typing import Any, Dict

from contract_ui_notices import EXPORT_REVIEW_NOTICE, EXPORT_UNRECOGNIZED_NOTE

APP_TITLE = "文书脱敏 / 还原"
APP_SUBTITLE = "Legal Document Redactor"
APP_VERSION = "1.3.0-dev"

esc = _html.escape


# =============================================================== 图标 =========

BRAND_MARK = """<svg viewBox="0 0 24 24" width="21" height="21" fill="none" aria-hidden="true">
<path d="M12 2.4 4.7 5.2v5.4c0 4.7 3.05 9.05 7.3 10.9 4.25-1.85 7.3-6.2 7.3-10.9V5.2L12 2.4Z" fill="#fff" fill-opacity=".22"/>
<path d="M12 2.4 4.7 5.2v5.4c0 4.7 3.05 9.05 7.3 10.9 4.25-1.85 7.3-6.2 7.3-10.9V5.2L12 2.4Z" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>
<path d="m8.7 11.9 2.35 2.35 4.4-4.55" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<defs><linearGradient id="g" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
<stop stop-color="#3b5bff"/><stop offset="1" stop-color="#7a4dff"/></linearGradient></defs>
<rect width="32" height="32" rx="9" fill="url(#g)"/>
<path d="M16 5.5 8.6 8.4v6.1c0 5.2 3.4 10 7.4 12 4-2 7.4-6.8 7.4-12V8.4L16 5.5Z" fill="#fff" fill-opacity=".2"/>
<path d="M16 5.5 8.6 8.4v6.1c0 5.2 3.4 10 7.4 12 4-2 7.4-6.8 7.4-12V8.4L16 5.5Z" stroke="#fff" stroke-width="1.8" stroke-linejoin="round" fill="none"/>
<path d="m12.2 15.9 2.6 2.6 5-5.2" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>"""

HERO_ART = """<svg class="hero-art" viewBox="0 0 360 210" fill="none" aria-hidden="true">
<defs>
<linearGradient id="ha-page" x1="0" y1="0" x2="0" y2="1">
<stop stop-color="#ffffff"/><stop offset="1" stop-color="#f4f6fd"/></linearGradient>
<linearGradient id="ha-acc" x1="0" y1="0" x2="1" y2="1">
<stop stop-color="#3b5bff"/><stop offset="1" stop-color="#7a4dff"/></linearGradient>
</defs>
<rect x="18" y="16" width="140" height="180" rx="14" fill="url(#ha-page)" stroke="#dfe4f2"/>
<rect x="36" y="38" width="72" height="9" rx="4.5" fill="#c9d2e6"/>
<rect x="36" y="60" width="104" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="36" y="76" width="88" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="36" y="100" width="60" height="12" rx="6" fill="#fcdada"/>
<rect x="100" y="100" width="40" height="12" rx="6" fill="#e4e9f5"/>
<rect x="36" y="126" width="104" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="36" y="142" width="44" height="12" rx="6" fill="#fde8c8"/>
<rect x="84" y="142" width="56" height="12" rx="6" fill="#e4e9f5"/>
<rect x="36" y="168" width="96" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="196" y="16" width="146" height="180" rx="14" fill="url(#ha-page)" stroke="#cfd8f7"/>
<rect x="214" y="38" width="72" height="9" rx="4.5" fill="#c9d2e6"/>
<rect x="214" y="60" width="110" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="214" y="76" width="92" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="214" y="100" width="62" height="12" rx="6" fill="#dbe3ff"/>
<rect x="280" y="100" width="40" height="12" rx="6" fill="#e4e9f5"/>
<rect x="214" y="126" width="110" height="7" rx="3.5" fill="#e4e9f5"/>
<rect x="214" y="142" width="46" height="12" rx="6" fill="#dbe3ff"/>
<rect x="264" y="142" width="56" height="12" rx="6" fill="#e4e9f5"/>
<rect x="214" y="168" width="98" height="7" rx="3.5" fill="#e4e9f5"/>
<circle cx="180" cy="106" r="20" fill="url(#ha-acc)"/>
<path d="M172 106h14m0 0-5.5-5.5M186 106l-5.5 5.5" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


# =============================================================== CSS ==========

CSS = r"""
:root{
  --bg:#eef1f8;
  --surface:#ffffff;
  --surface-2:#f7f9fd;
  --line:#e2e7f1;
  --line-soft:#eef1f7;
  --ink:#0d1626;
  --ink-2:#39465a;
  --sub:#6b7a90;
  --faint:#9aa7bb;
  --acc:#3355ff;
  --acc-d:#2441d8;
  --acc-2:#7a4dff;
  --acc-soft:#eef2ff;
  --acc-line:#c9d5ff;
  --ok:#0f9d58;
  --ok-soft:#eefaf2;
  --warn:#c47d0a;
  --warn-soft:#fff8e8;
  --danger:#d92d20;
  --danger-soft:#fef3f2;
  --hi:#e5484d;
  --md:#dd9207;
  --lo:#0d9bc0;
  --r:16px;
  --r-sm:11px;
  --sh-1:0 1px 2px rgba(15,26,50,.05), 0 2px 8px rgba(15,26,50,.04);
  --sh-2:0 8px 28px rgba(15,26,50,.09), 0 2px 6px rgba(15,26,50,.04);
  --sh-3:0 24px 60px rgba(15,26,50,.14);
  --ff:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,"Segoe UI",sans-serif;
  --fm:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;min-height:100vh;color:var(--ink);font-family:var(--ff);
  font-size:14.5px;line-height:1.72;background:var(--bg);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
}
body::before{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(920px 520px at 6% -10%, #dde5ff 0%, rgba(221,229,255,0) 62%),
    radial-gradient(780px 520px at 98% 2%, #e9dcff 0%, rgba(233,220,255,0) 58%),
    linear-gradient(180deg,#f4f6fc 0%, #e9edf7 100%);
}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:var(--fm);font-size:.86em;background:#f2f5fb;border:1px solid var(--line-soft);
  padding:1px 6px;border-radius:6px;color:var(--ink-2)}
.grow{flex:1}
.muted{color:var(--sub)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

/* ------------------------------------------------------------- 顶栏 */
.topbar{position:sticky;top:0;z-index:60;
  background:rgba(255,255,255,.80);backdrop-filter:saturate(180%) blur(14px);
  -webkit-backdrop-filter:saturate(180%) blur(14px);
  border-bottom:1px solid rgba(226,231,241,.85)}
.topbar-in{max-width:1260px;margin:0 auto;padding:11px 26px;display:flex;align-items:center;gap:16px}
.brand{display:flex;align-items:center;gap:11px;color:var(--ink);text-decoration:none}
.brand:hover{text-decoration:none}
.brand .mark{width:37px;height:37px;border-radius:12px;display:grid;place-items:center;flex:none;
  background:linear-gradient(135deg,#3b5bff,#7a4dff);box-shadow:0 7px 18px rgba(59,91,255,.30)}
.brand-txt{display:flex;flex-direction:column;line-height:1.22}
.brand-txt b{font-size:15px;font-weight:650;letter-spacing:-.015em}
.brand-txt i{font-style:normal;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.topbar-mid{display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden}
.topbar-act{margin-left:auto;display:flex;align-items:center;gap:9px}
.live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--sub);
  background:#fff;border:1px solid var(--line);padding:5px 12px;border-radius:999px}
.live .pulse{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(15,157,88,.5);
  animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(15,157,88,.45)}70%{box-shadow:0 0 0 7px rgba(15,157,88,0)}100%{box-shadow:0 0 0 0 rgba(15,157,88,0)}}

/* ------------------------------------------------------------- 布局 */
.shell{max-width:1260px;margin:0 auto;padding:26px}
.page-head{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:24px;line-height:1.3;font-weight:680;letter-spacing:-.022em;margin:0 0 4px}
h2{font-size:15px;font-weight:640;letter-spacing:-.01em;margin:0 0 12px;display:flex;align-items:center;gap:9px}
h3{font-size:13px;font-weight:600;margin:0}
.step-n{width:22px;height:22px;border-radius:7px;display:grid;place-items:center;font-size:12px;font-weight:700;
  background:var(--acc-soft);color:var(--acc);flex:none}
.meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--sub);font-size:12.5px}
.pill{display:inline-flex;align-items:center;gap:6px;background:#fff;border:1px solid var(--line);
  color:var(--ink-2);border-radius:999px;padding:3px 11px;font-size:12px;max-width:420px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill.ghost{background:var(--surface-2);color:var(--sub)}
.pill.acc{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc)}
/* 历史记录页 */
.histrow{display:flex;align-items:center;gap:14px;padding:13px 2px;border-bottom:1px solid var(--line-soft);flex-wrap:wrap}
.histrow:last-child{border-bottom:0}
.hist-main{min-width:0;flex:1 1 320px}
.hist-name{font-size:13.5px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hist-sub{font-size:12px;color:var(--faint);margin-top:4px;display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.hist-act{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.hist-empty{padding:28px 4px;color:var(--sub);font-size:13px;text-align:center}
.modal-backdrop{position:fixed;inset:0;z-index:100;display:grid;place-items:center;padding:20px;background:rgba(15,26,50,.45)}
.modal-backdrop[hidden]{display:none}
.modal{width:min(680px,100%);max-height:calc(100vh - 40px);overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:22px;box-shadow:var(--sh-3)}
.modal h2{margin:0 0 12px;font-size:18px}
.modal p{margin:0 0 14px;white-space:pre-wrap}
.modal-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}
.modal-error{color:var(--danger);background:var(--danger-soft);border:1px solid #f2b8b5;border-radius:8px;padding:9px}

.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:20px 22px;margin-bottom:18px;box-shadow:var(--sh-1)}
.card.flat{box-shadow:none;background:var(--surface-2)}
.card.tight{padding:14px 16px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}
.grid > .col{min-width:0}
.stack > *:last-child{margin-bottom:0}

/* ------------------------------------------------------------- 英雄区 */
.hero{position:relative;overflow:hidden;border-radius:20px;padding:30px 34px;margin-bottom:20px;
  background:linear-gradient(120deg,#151d38 0%,#22306b 46%,#3a2a72 100%);
  color:#f6f8ff;box-shadow:var(--sh-2);display:flex;align-items:center;gap:24px}
.hero::after{content:"";position:absolute;right:-70px;top:-110px;width:330px;height:330px;border-radius:50%;
  background:radial-gradient(circle at 30% 30%,rgba(122,77,255,.62),rgba(122,77,255,0) 68%)}
.hero-copy{position:relative;z-index:1;flex:1;min-width:0}
.eyebrow{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;color:#b9c6ff;background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:4px 12px;margin-bottom:14px}
.hero h1{font-size:27px;color:#fff;margin:0 0 10px}
.hero p{margin:0;color:#c3ccea;font-size:14px;max-width:640px}
.hero-art{position:relative;z-index:1;width:290px;flex:none;filter:drop-shadow(0 18px 30px rgba(0,0,0,.28))}

/* ------------------------------------------------------------- 按钮 */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;
  font-family:inherit;font-size:13.5px;font-weight:560;line-height:1;color:var(--ink-2);
  background:#fff;border:1px solid var(--line);border-radius:var(--r-sm);
  padding:11px 17px;cursor:pointer;white-space:nowrap;
  transition:transform .12s ease,border-color .15s ease,box-shadow .15s ease,background .15s ease,color .15s ease}
.btn[hidden]{display:none!important}
.btn:hover{border-color:#c8d3ea;color:var(--ink);box-shadow:var(--sh-1);text-decoration:none}
.btn:active{transform:translateY(1px)}
.btn.primary{background:linear-gradient(135deg,#3b5bff,#5a48f5);color:#fff;border-color:transparent;
  box-shadow:0 6px 18px rgba(59,91,255,.30)}
.btn.primary:hover{color:#fff;box-shadow:0 10px 26px rgba(59,91,255,.38);border-color:transparent}
.btn.ok{background:linear-gradient(135deg,#12a860,#0f9d58);color:#fff;border-color:transparent;
  box-shadow:0 6px 18px rgba(15,157,88,.26)}
.btn.ok:hover{color:#fff;border-color:transparent}
.btn.warn{background:#fff;color:var(--warn);border-color:#f0dcae}
.btn.warn:hover{background:var(--warn-soft);color:var(--warn)}
.btn.ghost{background:var(--surface-2)}
.btn.sm{padding:8px 13px;font-size:12.5px;border-radius:9px}
.btn.lg{padding:15px 30px;font-size:15px;border-radius:13px;font-weight:640}
.btn.block{width:100%}
.btn:disabled,.btn[disabled]{opacity:.5;cursor:not-allowed;box-shadow:none;transform:none}

/* ------------------------------------------------------------- 模式选择 */
.modes{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.mode{position:relative;border:1.5px solid var(--line);border-radius:14px;padding:18px 18px 16px;
  background:#fff;cursor:pointer;transition:border-color .16s,box-shadow .16s,transform .16s}
.mode:hover{border-color:#c2cff2;box-shadow:var(--sh-1)}
.mode .ic{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-size:19px;
  background:var(--surface-2);border:1px solid var(--line-soft);margin-bottom:11px}
.mode .name{font-size:15px;font-weight:650;margin-bottom:5px;display:flex;align-items:center;gap:8px}
.mode .desc{color:var(--sub);font-size:12.8px;line-height:1.68}
.mode.active{border-color:var(--acc);background:linear-gradient(180deg,#f7f9ff,#fff);
  box-shadow:0 0 0 4px rgba(51,85,255,.11),var(--sh-2)}
.mode.active .ic{background:var(--acc-soft);border-color:var(--acc-line)}
.mode.active::after{content:"✓";position:absolute;top:13px;right:14px;width:20px;height:20px;border-radius:50%;
  background:var(--acc);color:#fff;font-size:12px;font-weight:700;display:grid;place-items:center}

/* ------------------------------------------------------------- 上传区 */
.drop{position:relative;border:2px dashed #ccd6ea;border-radius:16px;background:var(--surface-2);
  padding:38px 24px;text-align:center;cursor:pointer;transition:.16s}
.drop:hover{border-color:var(--acc);background:var(--acc-soft)}
.drop.drag{border-color:var(--acc);background:var(--acc-soft);box-shadow:inset 0 0 0 4px rgba(51,85,255,.08)}
.drop input[type=file]{display:none}
.drop .ic{width:52px;height:52px;margin:0 auto 12px;border-radius:16px;display:grid;place-items:center;
  background:#fff;border:1px solid var(--line);box-shadow:var(--sh-1);font-size:23px}
.drop .t{font-size:15.5px;font-weight:620;margin:0 0 4px}
.drop .h{color:var(--sub);font-size:12.6px;margin:0}
.picked{display:flex;align-items:center;gap:10px;margin-top:14px;padding:11px 14px;border-radius:12px;
  background:var(--ok-soft);border:1px solid #c7ecd7;color:#116b3f;font-size:13px}
.picked .x{margin-left:auto;cursor:pointer;color:#5b8f74;font-size:15px;line-height:1}

/* ------------------------------------------------------------- 文本 / 列表 */
.olist{margin:0;padding:0;list-style:none;display:grid;gap:11px}
.olist li{display:flex;gap:10px;font-size:13px;color:var(--ink-2);line-height:1.7}
.olist .n{width:20px;height:20px;flex:none;border-radius:6px;background:var(--acc-soft);color:var(--acc);
  font-size:11.5px;font-weight:700;display:grid;place-items:center;margin-top:2px}
.tlist{margin:0;padding:0;list-style:none;display:grid;gap:8px}
.tlist li{position:relative;padding-left:18px;font-size:12.8px;color:var(--sub);line-height:1.68}
.tlist li::before{content:"";position:absolute;left:4px;top:9px;width:5px;height:5px;border-radius:50%;background:#bfcbe4}
.kv{display:grid;gap:9px}
.kv .row{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12.8px}
.kv .row b{font-weight:600;color:var(--ink)}
.stat{font-size:12.5px;color:var(--sub)}
.stat b{color:var(--acc);font-weight:640}

/* ------------------------------------------------------------- 提示条 */
.alert{padding:12px 15px;border-radius:12px;font-size:13px;margin:0 0 14px;
  display:flex;gap:9px;align-items:flex-start;line-height:1.66}
.alert.ok{background:var(--ok-soft);color:#11603a;border:1px solid #c7ecd7}
.alert.warn{background:var(--warn-soft);color:#7a4d05;border:1px solid #f0dcae}
.alert.err{background:var(--danger-soft);color:#8f1d15;border:1px solid #f7d3cf}
.alert.info{background:var(--acc-soft);color:#233a9e;border:1px solid var(--acc-line)}
.alert .i{flex:none;font-size:14px;line-height:1.5}
.notice-primary{font-size:13px;font-weight:560;line-height:1.65;margin-top:8px;color:var(--ink)}
.notice-secondary{display:block;font-size:12px;font-weight:400;line-height:1.55;margin-top:3px;color:var(--sub)}

/* ------------------------------------------------------------- 对比视图 */
.toolbar{position:sticky;top:59px;z-index:40;background:rgba(255,255,255,.94);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border:1px solid var(--line);border-radius:var(--r);padding:13px 16px;margin-bottom:16px;box-shadow:var(--sh-1)}
.tb-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tb-label{font-size:12.5px;color:var(--sub);font-weight:600}
.seg{display:inline-flex;background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg-btn{border:0;background:transparent;font-family:inherit;font-size:12.8px;font-weight:560;color:var(--sub);
  padding:6px 14px;border-radius:8px;cursor:pointer;transition:.14s}
.seg-btn:hover{color:var(--ink)}
.seg-btn.on{background:#fff;color:var(--ink);box-shadow:var(--sh-1)}
.seg-btn.on[data-sev=high]{color:#fff;background:var(--hi);box-shadow:none}
.seg-btn.on[data-sev=medium]{color:#fff;background:var(--md);box-shadow:none}
.seg-btn.on[data-sev=low]{color:#fff;background:var(--lo);box-shadow:none}
.seg-btn.on[data-sev=all]{color:#fff;background:#3a4763;box-shadow:none}
/* 类型面板：已移出吸顶工具栏（见下方 HTML），改为独立卡片。
   限高 + 内部滚动 → 它只会把下方内容"推开"，不会"盖住"
   「添加漏识别的字段」卡片。 */
.card.types-card{margin:0 0 14px;padding:15px 17px;display:none}
.card.types-card.open{display:block;max-height:min(38vh,300px);overflow:auto;
  overscroll-behavior:contain;padding-right:14px}
.card.types-card .cat:first-child{margin-top:0}
.cat{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--faint);font-weight:600;
  letter-spacing:.04em;text-transform:uppercase;margin:14px 0 8px}
.cat::after{content:"";flex:1;height:1px;background:var(--line-soft)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;
  padding:5px 13px;font-size:12.8px;cursor:pointer;user-select:none;background:#fff;
  color:var(--ink-2);transition:.14s}
.chip:hover{border-color:#c8d3ea;box-shadow:var(--sh-1)}
.chip .dot{width:8px;height:8px;border-radius:50%;flex:none}
.chip .cnt{font-family:var(--fm);font-size:11px;color:var(--faint)}
.chip input{display:none}
.chip.on[data-sev=high]{border-color:#f5b5b7;background:#fff5f5;color:#a3272b}
.chip.on[data-sev=medium]{border-color:#f0dcae;background:#fffaef;color:#7d5607}
.chip.on[data-sev=low]{border-color:#b6e2ef;background:#f1fbfe;color:#0a6a83}
.chip.off{opacity:.42;background:var(--surface-2)}
.chip.off .cnt{text-decoration:line-through}

/* ------------------------------------------- 自选字段入口
   只保留一处：类型面板末尾的虚线芯片（＋ 添加自选字段）。
   看类型列表的时刻，正是产生"我要脱敏的东西不在里头"念头的时刻；
   工具栏不再放同名按钮，避免窄屏换行把吸顶条顶高。 */
.chip.addchip{font-family:inherit;font-weight:600;color:var(--acc);
  border-color:var(--acc-line);border-style:dashed;background:var(--acc-soft)}
.chip.addchip:hover{background:var(--acc);color:#fff;border-color:var(--acc);border-style:solid}
.card.termcard{border-color:var(--acc-line);background:linear-gradient(180deg,#f7f9ff,#fff)}
.card.termcard h2{margin-bottom:8px}
.card.termcard.flash{animation:termflash 1.5s ease-out}
@keyframes termflash{
  0%{box-shadow:0 0 0 0 rgba(51,85,255,.34),var(--sh-1)}
  60%{box-shadow:0 0 0 10px rgba(51,85,255,0),var(--sh-1)}
  100%{box-shadow:0 0 0 0 rgba(51,85,255,0),var(--sh-1)}
}

.compare{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
.pane{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  box-shadow:var(--sh-1);overflow:hidden;display:flex;flex-direction:column}
.pane-head{display:flex;align-items:center;gap:9px;padding:12px 16px;font-size:12.5px;font-weight:600;
  color:var(--sub);background:var(--surface-2);border-bottom:1px solid var(--line-soft);
  position:sticky;top:0;z-index:2}
.pane-head .sw{width:9px;height:9px;border-radius:3px;background:#c9d2e6}
.pane-head .sw.alt{background:linear-gradient(135deg,#3b5bff,#7a4dff)}
.pane-body{padding:16px 18px;height:calc(100vh - 320px);min-height:340px;overflow:auto;
  white-space:pre-wrap;word-break:break-word;font-size:13.2px;line-height:1.95;scroll-behavior:auto}
.pane-body::-webkit-scrollbar{width:11px}
.pane-body::-webkit-scrollbar-thumb{background:#ccd5e6;border-radius:8px;border:3px solid #fff}
.pane-body::-webkit-scrollbar-thumb:hover{background:#b3c0d8}
.doc .blk{margin-top:13px}
.doc .blk:first-child{margin-top:0}
.doc .loc{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;font-weight:600;color:var(--acc);
  background:var(--acc-soft);border:1px solid var(--acc-line);border-radius:7px;
  padding:2px 9px;margin:0 7px 0 0;vertical-align:1px;white-space:nowrap}
.doc .para{display:block}
mark{padding:1px 3px;border-radius:4px;color:var(--ink);cursor:pointer;box-decoration-break:clone;
  -webkit-box-decoration-break:clone;transition:filter .12s}
mark:hover{filter:brightness(.94)}
mark.high{background:#ffd4d6;box-shadow:inset 0 -2px 0 #f2a1a5}
mark.medium{background:#ffe9b8;box-shadow:inset 0 -2px 0 #ecc76e}
mark.low{background:#c6ecf7;box-shadow:inset 0 -2px 0 #8fd4e6}
mark.ph{background:linear-gradient(135deg,#dbe3ff,#e6dcff);border:1px dashed #a9b8f5;color:#2f3f9e;
  font-family:var(--fm);font-size:.9em}
.off{background:#f1f4f9;color:#a3aec2;text-decoration:line-through;border-radius:4px;padding:1px 3px;cursor:pointer}
.off:hover{background:#e8edf6}
.empty{padding:56px 20px;text-align:center;color:var(--sub);font-size:13.5px}
.empty .big{font-size:34px;margin-bottom:10px}

/* ------------------------------------------------------------- 底部操作条 */
.actionbar{position:sticky;bottom:0;z-index:45;margin-top:18px;
  background:rgba(255,255,255,.94);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border:1px solid var(--line);border-radius:var(--r);padding:14px 18px;box-shadow:0 -6px 24px rgba(15,26,50,.07)}
.ab-row{display:flex;align-items:center;gap:11px;flex-wrap:wrap}
.out-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:11px;padding-top:11px;
  border-top:1px dashed var(--line)}
.chk{display:inline-flex;align-items:center;gap:8px;font-size:12.6px;color:var(--sub);cursor:pointer;
  user-select:none;-webkit-user-select:none;padding:7px 11px;border:1px solid var(--line);
  border-radius:10px;background:#fff;transition:border-color .15s,background .15s}
.chk:hover{border-color:#c8d3ea}
.chk input{width:15px;height:15px;accent-color:var(--acc);cursor:pointer;margin:0}
.chk b{color:var(--ink);font-weight:620}
.chk.locked{background:var(--surface-2);color:var(--faint);cursor:default}
.folder{display:inline-flex;align-items:center;gap:6px;font-size:12.3px;color:var(--faint);line-height:1.5}
.folder.on{color:#0f9d58}
.folder code{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:6px;
  padding:1px 6px;font-size:11.8px;color:var(--ink);font-family:var(--mono,ui-monospace,SFMono-Regular,Menlo,monospace)}
.result{margin-top:12px}
.result:empty{display:none}
.result .saved{display:flex;flex-direction:column;gap:5px;margin-top:8px}
.result .saved .ln{font-size:12.6px;color:var(--sub);line-height:1.6}
.result .saved .ln b{color:var(--ink);font-weight:620}
.result .saved .saved-muted{color:var(--sub)}
.result .saved .saved-emphasis{color:var(--ink)}
.result .saved-warning-muted,.result .saved-warning-emphasis{font-size:12.5px;line-height:1.6;margin-top:8px}
.result .saved-warning-muted{color:var(--sub)}
.result .saved-warning-emphasis{color:var(--ink)}

/* ------------------------------------------- v1.3.0「低置信可见可勾」＋ 导出前清单
   术语：界面统一用「把握」，不出现「置信度」与原始分数（分数只进 .redaction.log）。 */
mark.lowconf{background:#eef1f7;box-shadow:inset 0 -2px 0 #cfd8e6;border:1px dashed #b9c4d8}
.off.lowconf{text-decoration:none;color:#7c8798}
.card.lowcard{border-color:#f0dcae;background:linear-gradient(180deg,#fffdf6,#fff)}
.card.lowcard h2{margin-bottom:6px}
.lc-row{display:flex;align-items:flex-start;gap:9px;padding:8px 2px;border-top:1px dashed var(--line)}
.lc-row:first-child{border-top:0}
.lc-row .n{font-family:var(--fm);font-size:11px;color:var(--faint);flex:none;min-width:16px;text-align:right;padding-top:2px}
.lc-row .b{flex:1;min-width:0}
.lc-row .t{font-size:12.6px;color:var(--ink);line-height:1.6;word-break:break-all}
.lc-row .m{font-size:11.6px;color:var(--faint);margin-top:2px}
.lc-badge{display:inline-block;font-size:10.5px;font-weight:600;color:#7d5607;background:#fff6df;
  border:1px solid #f0dcae;border-radius:6px;padding:0 6px;margin-right:6px;vertical-align:1px}
.manifest{margin-top:10px;border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden}
.manifest .head{padding:11px 15px;font-weight:640;font-size:13px;background:var(--surface-2);
  border-bottom:1px solid var(--line-soft)}
.manifest .seg{padding:11px 15px;border-bottom:1px solid var(--line-soft)}
.manifest .seg:last-child{border-bottom:0}
.manifest .seg .st{font-size:12.4px;font-weight:640;margin-bottom:7px}
.manifest .seg.hard .st{color:#a3272b}
.manifest .seg.soft .st{color:#7d5607}
.manifest .li{font-size:12.4px;color:#000;font-weight:400;line-height:1.75;display:flex;gap:7px}
.manifest .li .d{flex:none}
.manifest .li b{color:#000;font-weight:400}
.manifest .li strong{color:#000;font-weight:700}
.manifest .acts{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
.manifest .lead-blank{font-size:12.6px;color:var(--sub);line-height:1.7;margin-top:4px}
.manifest .unscanned-title,.manifest .unscanned .li{font-size:12px;color:var(--sub);font-weight:400;line-height:1.6}
.manifest .unscanned .li{margin-top:3px}

/* ------------------------------------------------------------- 页脚 */
.foot{max-width:1260px;margin:0 auto;padding:8px 26px 34px;display:flex;gap:10px;flex-wrap:wrap;
  align-items:center;color:var(--faint);font-size:11.8px}
.foot b{color:var(--sub);font-weight:600}

/* ------------------------------------------------------------- 启动/退出屏 */
.splash{min-height:100vh;display:grid;place-items:center;padding:24px}
.splash .box{width:min(460px,92vw);text-align:center;background:#fff;border:1px solid var(--line);
  border-radius:22px;padding:42px 36px;box-shadow:var(--sh-3)}
.splash .mark{width:62px;height:62px;border-radius:19px;margin:0 auto 18px;display:grid;place-items:center;
  background:linear-gradient(135deg,#3b5bff,#7a4dff);box-shadow:0 14px 30px rgba(59,91,255,.32)}
.splash h1{font-size:20px;margin:0 0 6px}
.splash p{margin:6px 0;color:var(--sub);font-size:13px}
.ring{width:46px;height:46px;margin:22px auto 4px;border-radius:50%;
  border:3px solid #e6ebf6;border-top-color:var(--acc);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.track{height:6px;background:#eef1f8;border-radius:999px;overflow:hidden;margin:18px 0 0}
.track .fill{height:100%;width:34%;border-radius:999px;background:linear-gradient(90deg,#3b5bff,#7a4dff);
  animation:slide 1.5s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(320%)}}
.splash .tip{margin-top:16px;color:var(--faint);font-size:11.8px}
.bye{min-height:100vh;display:grid;place-items:center;text-align:center;color:var(--sub)}
.bye h1{color:var(--ink)}

/* ------------------------------------------------------------- 响应式 */
@media(max-width:1040px){
  .grid{grid-template-columns:1fr}
  .hero-art{display:none}
  .compare{grid-template-columns:1fr}
  .pane-body{height:auto;max-height:52vh}
}
@media(max-width:720px){
  .shell{padding:16px}
  .topbar-in{padding:10px 16px}
  .modes{grid-template-columns:1fr}
  .hero{padding:24px}
  .hero h1{font-size:22px}
  .foot{padding:8px 16px 26px}
}
"""


# =============================================================== 页面骨架 =====

HTML_HEAD = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>__TITLE__</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
__CSS__
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-in">
    <a class="brand" href="/" title="回到首页">
      <span class="mark">__MARK__</span>
      <span class="brand-txt"><b>文书脱敏 / 还原</b><i>__SUBTITLE__</i></span>
    </a>
    <div class="topbar-mid">__MID__</div>
    <div class="topbar-act">
      <span class="live"><span class="pulse"></span>本机运行中</span>
      <a class="btn sm ghost" href="/history" title="查看本机留存的历次处理记录">历史记录</a>
      <button class="btn sm ghost" onclick="quitApp()" title="停止本地服务">退出</button>
    </div>
  </div>
</header>
<main class="shell">
__BODY__
</main>
<footer class="foot">
  <span>首次安装依赖需要联网，安装完成后文书处理不联网；所有解析与改写均在本机完成，文件不外传</span>
  <span>·</span>
  <span>文书脱敏 / 还原 <b>v__VERSION__</b></span>
</footer>
<script>
async function quitApp(){
  if(!confirm('退出后本地服务会停止，需要重新双击应用才能再次使用。确定退出？')) return;
  try{ await fetch('/api/shutdown',{method:'POST'}); }catch(e){}
  document.body.innerHTML='<div class="bye"><h1>已退出</h1><p>本地服务已停止，可以关闭此窗口。</p></div>';
}
</script>
</body>
</html>
"""


def html_doc(title: str, body: str, mid: str = "") -> bytes:
    s = (HTML_HEAD
         .replace("__TITLE__", esc(title))
         .replace("__CSS__", CSS)
         .replace("__MARK__", BRAND_MARK)
         .replace("__SUBTITLE__", APP_SUBTITLE)
         .replace("__MID__", mid)
         .replace("__BODY__", body)
         .replace("__VERSION__", APP_VERSION))
    return s.encode("utf-8")


# =============================================================== 启动页 =======

LOADING_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>正在启动 · 文书脱敏 / 还原</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
__CSS__
</style></head>
<body>
<div class="splash">
  <div class="box">
    <div class="mark">__MARK__</div>
    <h1>正在启动本地服务</h1>
    <p id="status">正在准备运行环境…</p>
    <div class="ring"></div>
    <div class="track"><div class="fill"></div></div>
    <p class="tip">首次安装依赖需要联网，安装完成后文书处理不联网 · 文件不离开本机</p>
  </div>
</div>
<script>
var msgs=['正在准备运行环境…','正在启动本地 Python 服务…','正在加载脱敏规则…','正在初始化会话目录…','即将进入主页面…'];
var el=document.getElementById('status'),i=0;
var t=setInterval(function(){ i=Math.min(i+1,msgs.length-1); el.textContent=msgs[i]; },700);
async function tick(){
  for(var n=0;n<240;n++){
    try{
      var r=await fetch('/health',{cache:'no-store'});
      if(r.ok){ clearInterval(t); el.textContent='服务已就绪，正在进入…'; setTimeout(function(){location.href='/';},350); return; }
    }catch(e){}
    await new Promise(function(z){setTimeout(z,400)});
  }
  el.textContent='⚠ 服务未就绪，请查看启动器日志';
}
tick();
</script>
</body></html>
"""


def loading_doc() -> bytes:
    return (LOADING_HTML
            .replace("__CSS__", CSS)
            .replace("__MARK__", BRAND_MARK)
            .encode("utf-8"))


# =============================================================== 上传首页 =====

UPLOAD_BODY = r"""
<section class="hero">
  <div class="hero-copy">
    <span class="eyebrow">安装完成后文书处理不联网</span>
    <h1>文书脱敏 / 还原</h1>
    <p>上传 Word、文字或扫描 PDF、文本文书，在本机识别并复核敏感内容。PDF 优先把可靠定位的文字替换为代号，其余可在原页框选遮盖。</p>
  </div>
  __HERO_ART__
</section>

<form id="upload-form">
  <div class="grid">
    <div class="col stack">

      <div class="card">
        <div class="ab-row">
          <button class="btn sm" type="button" onclick="openRulesFile()">打开规则文件</button>
          <span class="stat" id="rules-file-status">识别规则保存在本机，可自行修改；保存后重新上传文书生效。</span>
        </div>
        <input type="hidden" id="rules-open-token" value="__RULES_TOKEN__">
        <div id="rules-notice" style="margin-top:8px"></div>
        <details style="margin-top:8px;font-size:13px">
          <summary>想修改字段识别规则？可将规则内容交给您选择的AI Agent协助修改（本工具不会连接AI Agent；点击展开操作说明）</summary>
          <ol>
            <li>软件会自动保留最近一次通过校验的规则版本；重要修改前也可手动另存副本。规则内容变化时才生成新快照，同一内容反复打开不会重复备份。</li>
            <li>点击“打开规则文件”，把规则全部内容和你想修改的要求粘贴给你自己选择的 AI Agent。只发送规则和虚构示例，不要发送真实合同、客户信息或原文映射；完整规则文件会离开本机，是否到达云端取决于你选择的 AI 服务。</li>
            <li>让 AI 返回完整规则文件，可直接复制下面的提示词：</li>
          </ol>
          <textarea id="rules-prompt" readonly style="width:100%;min-height:130px;font-size:12px;white-space:pre-wrap;margin:6px 0">我想修改合同脱敏规则。请只按我的修改要求调整下面的规则清单，保留其他规则、表格结构和程序需要的字段，不修改程序代码。除非我明确要求删除某条自定义规则，否则请保留全部现有规则行和正则表达式；如果有内容无法保留，请先指出，不要自行删行。请返回可以直接复制回原文件的完整Markdown内容，不要省略未修改的部分，也不要用代码围栏包裹全文；最后另行说明改动和仅使用虚构信息的测试方法。我的修改要求是：【在这里填写】。以下是当前规则文件的全部内容：【在这里粘贴】。</textarea>
          <p style="margin:6px 0">把 AI 返回的完整内容保存回规则文件（UTF-8 编码，不要混入解释文字），然后重新上传虚构测试文件核对识别与导出；出问题可恢复上一份有效版本。AI Agent 只在修改规则时使用；安装和日常脱敏不需要 AI Agent。</p>
        </details>
      </div>

      <div class="card">
        <h2><span class="step-n">1</span>选择操作模式</h2>
        <div class="modes" id="mode-picker">
          <div class="mode active" data-mode="redact">
            <div class="ic">🔒</div>
            <div class="name">脱敏模式</div>
            <div class="desc">上传<strong>原始</strong>文书 → 复核识别结果 → 输出脱敏文件。还原映射文件须单独保管。</div>
          </div>
          <div class="mode" data-mode="restore">
            <div class="ic">🔓</div>
            <div class="name">还原模式</div>
            <div class="desc">上传<strong>已脱敏</strong>文书，并提供对应的映射文件，回填原文。</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h2><span class="step-n">2</span>上传文书</h2>
        <div class="alert info" id="uploadHelp" style="margin:0 0 12px">支持DOCX、PDF、TXT、MD；诉讼案号、法院名称及公文/机构编号均作为候选显示，是否脱敏由您在对照页选择。</div>
        <div class="alert info" id="local-service-note" style="margin:0 0 12px">启动工具后如果出现黑色窗口，请保持开启；关闭后将无法正常使用。</div>
        <label class="field" style="display:block;margin-bottom:12px">案件或项目名称（可选；后续文书请填写完全相同的名称）<br>
          <input type="text" name="case_name" id="case-name" maxlength="80" placeholder="例如：某某案或某项目" style="width:100%;padding:10px;margin-top:5px">
        </label>
        <p class="stat" style="margin:0 0 12px">例如起诉状和证据目录都填“某某案”：自选规则可供后续上传的文书沿用；已经导出的文件不会自动修改。留空则只处理当前文书。</p>
        <div class="drop" id="drop">
          <div class="ic">📄</div>
          <p class="t">把文件拖到这里，或点击选择</p>
          <p class="h" id="fileTypes">支持 .docx / .pdf / .txt / .md · 单个文件不超过 100 MB</p>
          <input type="file" name="file" id="file-input" accept=".docx,.pdf,.txt,.md">
        </div>
        <div id="policy-wrap" style="margin-top:12px">
          <label class="field" style="display:block">金额、日期处理策略<br>
            <select name="redaction_policy" id="redaction-policy" style="width:100%;padding:10px;margin-top:5px">
              <option value="" selected disabled>请选择金额、日期的处理方式</option>
              <option value="privacy_first">脱敏交易要素</option>
              <option value="preserve_transaction">保留交易要素</option>
            </select>
          </label>
          <div class="stat" id="policy-help" style="margin-top:6px"></div>
        </div>
        <div id="filename"></div>
        <input type="hidden" name="mode" id="mode-input" value="redact">
      </div>

      <div class="card">
        <div class="ab-row">
          <button class="btn primary lg" type="submit" id="submit-btn" disabled>下一步</button>
          <span class="stat" id="hint">请先选择文件</span>
        </div>
        <div id="result" class="result"></div>
        <button class="btn" type="button" id="recheck-service" hidden>检测本地服务状态</button>
      </div>

    </div>

    <div class="col stack">
      <div class="card">
        <h2>🔎 自动识别的基础范围</h2>
        <ul class="tlist">
          <li>身份证号、港澳台居民证件、护照号</li>
          <li>手机号、固定电话、邮箱</li>
          <li>自然人姓名、职务+姓名</li>
          <li>公司 / 机构名称、统一社会信用代码</li>
          <li>住址、通信地址、送达地址</li>
          <li>银行账号、开户行</li>
          <li>合同金额、价款、违约金（大写与小写）</li>
          <li>合同编号、日期、期限</li>
          <li>诉讼案号、法院名称及公文/机构编号作为候选显示；请在对照页核对并选择是否脱敏</li>
        </ul>
        <div class="alert info" style="margin:14px 0 0">
          <span class="i">ℹ️</span>
          <span>文字层候选默认勾选；扫描页低置信度候选需手工确认。医疗信息、商业秘密等通常还需补充具体原文。</span>
        </div>
      </div>

      <div class="card">
        <h2>🛡 安全边界</h2>
        <ul class="tlist">
          <li>首次安装依赖需要联网；安装完成后文书处理不联网，<b>不调用大模型与云端接口</b></li>
          <li>只监听 <code>127.0.0.1</code>，外部设备无法访问</li>
          <li>处理结果落盘在本地会话目录，可随时清理</li>
          <li>“文本框”包括通过Word“插入→文本框”添加的文字，也包括输入在矩形、箭头等绘图形状里的文字。普通文本框、符合简单结构的VML文字水印、简单内容控件和受支持隐藏文字纳入扫描；Word为兼容不同版本保留的备用文本框副本（如VML与DrawingML）、复杂水印及脚注/尾注、字段代码、图片文字和部分修订仍需按提示人工核对</li>
        </ul>
      </div>

      <div class="card">
        <h2>🧭 使用流程</h2>
        <ol class="olist">
          <li><span class="n">1</span><span>选择脱敏/还原并上传文书</span></li>
          <li><span class="n">2</span><span>核对候选；PDF 逐页补框并确认，文字文书在左右对比页核对</span></li>
          <li><span class="n">3</span><span>导出脱敏文件；PDF 只下载对外稿，原件留在本机项目</span></li>
          <li><span class="n">4</span><span>PDF 从本机项目取回原件；支持的文字文书用映射表还原</span></li>
        </ol>
      </div>
    </div>
  </div>
</form>

<script>
var drop=document.getElementById('drop'),
    fileInput=document.getElementById('file-input'),
    filenameEl=document.getElementById('filename'),
    submitBtn=document.getElementById('submit-btn'),
    modeInput=document.getElementById('mode-input'),
    hint=document.getElementById('hint'),
    resultEl=document.getElementById('result'),
    policySelect=document.getElementById('redaction-policy'),
    recheckButton=document.getElementById('recheck-service');

var LABEL={redact:'进入左右对比 →',restore:'上传并还原 →'};
var uploadInFlight=false,healthFailures=0,pendingRulesConfirmSha=null;

function serviceHelp(){
  return '本地脱敏服务连接失败。请重新运行工具；服务恢复后点击“检测本地服务状态”。服务重启后，未清理明文的PDF可从历史记录继续；已清理明文的PDF不能继续复核。DOCX、TXT、Markdown的未完成复核状态须重新上传。';
}
async function serviceAvailable(){
  try{
    var r=await fetch('/health',{cache:'no-store'});
    if(!r.ok)return false;
    var j=await r.json();
    return j.service==='contract_app_server';
  }catch(e){return false;}
}
function showUploadError(message,showRecheck){
  resultEl.innerHTML='<div class="alert err"><span class="i">✗</span><span>'+String(message).replace(/[<>&]/g,'')+'</span></div>';
  recheckButton.hidden=!showRecheck;
}
function rulesToken(){var el=document.getElementById('rules-open-token');return el?el.value:'';}
async function openRulesFile(){
  var st=document.getElementById('rules-file-status');
  try{
    var r=await fetch('/api/rules/open',{method:'POST',headers:{'X-Rules-Open-Token':rulesToken()}});
    var j=await r.json();
    st.textContent=(j.ok?'已请求系统编辑器打开：':(j.error||'无法打开规则文件')+' ')+(j.path||'');
  }catch(e){ st.textContent='打开失败，请确认本地服务仍在运行。'; }
}
async function loadRulesStatus(){
  try{
    var r=await fetch('/api/rules/status',{cache:'no-store'});
    if(!r.ok)return;
    var j=await r.json();
    var n=document.getElementById('rules-notice');
    if(!n)return;
    if(j.bundled_template_updated){
      n.innerHTML+='<div class="alert info"><span class="i">ℹ️</span><span>默认规则已更新，当前仍保留你的规则。如需采用新默认规则，可参考安装目录 references 下的新版模板手动合并。</span></div>';
    }
    if(j.init_error){
      n.innerHTML+='<div class="alert err"><span class="i">✗</span><span>'+String(j.init_error).replace(/[<>&]/g,'')+'</span></div>';
    }
  }catch(e){}
}
function rulesActionsHtml(path){
  return '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
    +'<button class="btn sm" type="button" onclick="openRulesFile()">打开规则文件</button>'
    +'<button class="btn sm ghost" type="button" onclick="restoreLastValid()">恢复上一份有效规则</button></div>'
    +(path?'<p class="stat" style="margin:6px 0 0">规则文件位置：<code>'+String(path).replace(/[<>&]/g,'')+'</code></p>':'');
}
function showRulesError(j){
  resultEl.innerHTML='<div class="alert err"><span class="i">✗</span><span>'+String(j.error||'规则文件未通过校验，已停止本次识别。').replace(/[<>&]/g,'')+'</span></div>'+rulesActionsHtml(j.rules_path);
}
function showRuleDeletionConfirm(j){
  var names=(j.missing||[]).map(function(m){return String(m.type_id||m).replace(/[<>&]/g,'');}).join('、');
  resultEl.innerHTML='<div class="alert err"><span class="i">✗</span><span>新规则文件将移除以下自定义规则：'+names+'。确认删除后本次识别才会继续。</span></div>'
    +'<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
    +'<button class="btn sm primary" type="button" id="confirm-rule-del">确认删除并继续</button>'
    +'<button class="btn sm" type="button" onclick="openRulesFile()">打开规则文件</button>'
    +'<button class="btn sm ghost" type="button" onclick="restoreLastValid()">恢复上一份有效规则</button></div>';
  var b=document.getElementById('confirm-rule-del');
  if(b) b.addEventListener('click',function(){ pendingRulesConfirmSha=j.rules_sha256||''; document.getElementById('upload-form').requestSubmit(); });
}
async function restoreLastValid(){
  try{
    var r=await fetch('/api/rules/restore-last-valid',{method:'POST'});
    var j=await r.json();
    resultEl.innerHTML='<div class="alert '+(j.ok?'ok':'err')+'"><span class="i">'+(j.ok?'✓':'✗')+'</span><span>'+(j.ok?'已恢复上一份通过校验的规则，请重新上传文书。':String(j.error||'恢复失败').replace(/[<>&]/g,''))+'</span></div>';
  }catch(e){ resultEl.innerHTML='<div class="alert err"><span class="i">✗</span><span>恢复失败，请确认本地服务仍在运行。</span></div>'; }
}
loadRulesStatus();
function updatePolicyHelp(){
  var policy=policySelect.value, file=fileInput.files&&fileInput.files[0], ext=file?file.name.toLowerCase().split('.').pop():'';
  var pdf=ext==='pdf';
  var text=policy==='privacy_first'?'金额、日期默认脱敏。':policy==='preserve_transaction'?'金额、日期默认保留并在导出检查中列出。':'请选择金额、日期的处理方式。';
  if(pdf)text='PDF导出为像素稿，不生成映射文件，不能用工具还原。'+text;
  document.getElementById('policy-help').textContent=text;
}

function syncFileMode(){
  var help=document.getElementById('uploadHelp');
  help.textContent=modeInput.value==='restore'
    ? '文字映射还原须提供对应的mapping.json。PDF原件请从本机历史项目取回。'
    : '支持DOCX、PDF、TXT、MD。PDF逐页核对；案号、法院名称及公文/机构编号候选在对照页由您选择是否脱敏。';
  document.getElementById('fileTypes').textContent=modeInput.value==='restore'
    ? '支持 .docx / .txt / .md · PDF原件从历史项目取回'
    : '支持 .docx / .pdf / .txt / .md · 单个文件不超过 100 MB';
  fileInput.accept=modeInput.value==='restore'?'.docx,.txt,.md':'.docx,.pdf,.txt,.md';
  document.getElementById('policy-wrap').hidden=modeInput.value==='restore';
  updatePolicyHelp();
  syncBtn();
}

function syncBtn(){
  var has=fileInput.files && fileInput.files.length;
  var policyReady=modeInput.value!=='redact'||!!policySelect.value;
  submitBtn.disabled=!has||!policyReady;
  submitBtn.textContent=LABEL[modeInput.value]||'下一步';
  hint.textContent = !has ? '请先选择文件' : !policyReady ? '请选择金额、日期的处理方式' : '已就绪，点击开始处理';
  updatePolicyHelp();
}

Array.prototype.forEach.call(document.querySelectorAll('#mode-picker .mode'),function(el){
  el.addEventListener('click',function(){
    Array.prototype.forEach.call(document.querySelectorAll('#mode-picker .mode'),function(x){x.classList.remove('active');});
    el.classList.add('active');
    modeInput.value=el.getAttribute('data-mode');
    syncFileMode();
  });
});

function showFile(f){
  if(!f) return;
  filenameEl.innerHTML='<div class="picked"><span>📎</span><span>'+f.name.replace(/[<>&]/g,'')+' · '+(f.size/1048576).toFixed(2)+' MB</span><span class="x" id="clearFile">✕</span></div>';
  var c=document.getElementById('clearFile');
  if(c) c.addEventListener('click',function(){ fileInput.value=''; filenameEl.innerHTML=''; syncBtn(); });
  syncBtn();
}
fileInput.addEventListener('change',function(){ showFile(fileInput.files[0]); });
policySelect.addEventListener('change',function(){updatePolicyHelp();syncBtn();});
drop.addEventListener('click',function(){ fileInput.click(); });
drop.addEventListener('dragover',function(e){ e.preventDefault(); drop.classList.add('drag'); });
drop.addEventListener('dragleave',function(){ drop.classList.remove('drag'); });
drop.addEventListener('drop',function(e){
  e.preventDefault(); drop.classList.remove('drag');
  if(e.dataTransfer.files && e.dataTransfer.files.length){ fileInput.files=e.dataTransfer.files; showFile(e.dataTransfer.files[0]); }
});

document.getElementById('upload-form').addEventListener('submit',async function(ev){
  ev.preventDefault();
  var f=fileInput.files[0];
  if(!f){ return; }
  if(modeInput.value==='redact'&&!policySelect.value){
    showUploadError('请先选择金额、日期的处理方式。',false);
    syncBtn();
    return;
  }
  submitBtn.disabled=true;
  uploadInFlight=true;
  recheckButton.hidden=true;
  submitBtn.textContent='上传与识别中…';
  resultEl.innerHTML='<div class="alert info"><span class="i">⏳</span><span>正在上传并识别敏感信息，文件较大时请稍候…</span></div>';
  try{
    if(!await serviceAvailable()){
      showUploadError(serviceHelp()+'当前没有发送合同内容。',true);
      return;
    }
    var fd=new FormData();
    fd.append('file',f);
    fd.append('mode',modeInput.value);
    fd.append('redaction_policy',policySelect.value);
    fd.append('case_name',document.getElementById('case-name').value.trim());
    if(pendingRulesConfirmSha){ fd.append('rules_confirm_sha256',pendingRulesConfirmSha); }
    pendingRulesConfirmSha=null;
    var r;
    try{r=await fetch('/api/upload',{method:'POST',body:fd});}
    catch(networkError){
      if(!await serviceAvailable()){
        var serviceError=new Error(serviceHelp()+'本次上传是否已生成项目无法确认，请先查看历史记录。');
        serviceError.localServiceUnavailable=true;
        throw serviceError;
      }
      throw new Error('上传连接中断，但本地服务仍可连接，无法确认是否已生成项目。请先查看历史记录，避免重复上传；确认没有新项目后再重新选择文件重试，并检查启动器窗口或server.log。');
    }
    var j;
    try{j=await r.json();}catch(parseError){throw new Error('服务响应无法读取。请先查看历史记录确认是否已生成项目，再决定是否重试。');}
    if(!r.ok||!j.ok){
      if(j&&j.code==='RULES_DELETION_CONFIRM'){ showRuleDeletionConfirm(j); return; }
      if(j&&typeof j.code==='string'&&j.code.indexOf('RULES')===0){ showRulesError(j); return; }
      throw new Error(j.error||'上传失败，请检查文件后重试');
    }
    resultEl.innerHTML='<div class="alert ok"><span class="i">✓</span><span>识别完成，正在进入下一步…</span></div>';
    var dest = j.redirect || ((j.mode==='restore') ? '/workspace?sid='+j.sid : '/review?sid='+j.sid);
    location.href=dest;
  }catch(e){
    showUploadError(e.message||e,!!e.localServiceUnavailable);
  }finally{
    uploadInFlight=false;
    submitBtn.disabled=false;
    syncBtn();
  }
});
recheckButton.addEventListener('click',async function(){
  recheckButton.disabled=true;
  if(await serviceAvailable()){
    resultEl.innerHTML='<div class="alert info"><span>服务已恢复。请确认历史记录后，再重新选择文件发起上传。</span></div>';
    recheckButton.hidden=true;
  }else{
    showUploadError(serviceHelp(),true);
  }
  recheckButton.disabled=false;
});
setInterval(async function(){
  if(document.hidden||uploadInFlight)return;
  if(await serviceAvailable()){healthFailures=0;return;}
  healthFailures++;
  if(healthFailures>=2&&fileInput.files.length&&!recheckButton.hidden)return;
  if(healthFailures>=2&&fileInput.files.length){showUploadError(serviceHelp()+'网页暂时无法连接服务。',true);}
},12000);
syncFileMode();
</script>
"""


def build_upload_page(rules_token: str = "") -> bytes:
    """首页；“打开规则文件”令牌仅用于该本机动作，随页面注入。"""
    return html_doc(APP_TITLE, UPLOAD_BODY
                    .replace("__HERO_ART__", HERO_ART)
                    .replace("__RULES_TOKEN__", esc(rules_token)))


# =============================================================== 工作台 =======

WORKSPACE_BODY = r"""
<div class="page-head">
  <div>
    <h1>__HEAD_TITLE__</h1>
    <div class="meta">
      <span class="pill">📄 __FILENAME__</span>
      <span class="pill ghost">__SUMMARY__</span>
      <span class="pill ghost">Session __SID__</span>
    </div>
  </div>
</div>

__HINT__

<div class="card">
  <h2>请选择要执行的操作</h2>
  <div class="modes" id="mode-picker">
    <div class="mode __REDACT_ACTIVE__" data-mode="redact">
      <div class="ic">🔒</div>
      <div class="name">脱敏</div>
      <div class="desc">识别敏感信息并生成可逆占位符；还原时需使用单独保存的JSON格式映射文件。</div>
    </div>
    <div class="mode __RESTORE_ACTIVE__" data-mode="restore">
      <div class="ic">🔓</div>
      <div class="name">还原</div>
      <div class="desc">通过单独保存的映射文件，把脱敏稿里的占位符回填为原文。</div>
    </div>
  </div>
</div>

<div class="card" id="redact-panel" style="display:none">
  <h2>脱敏设置</h2>
  <div class="meta" style="margin-bottom:14px">
    <span class="pill acc">共识别 __TYPE_COUNT__ 类 / __OCC_COUNT__ 处候选项</span>
  </div>
  <p class="stat" style="margin:0 0 16px">默认全部类型均已启用；在对比页可逐个类型、逐个位置取消，也可按严重度筛选。</p>
  <a class="btn primary lg" href="/review?sid=__SID__">进入左右对比页 →</a>
</div>

<div class="card" id="restore-panel" style="display:none">
  <h2>还原</h2>
  __RESTORE_STATUS__
  <div id="restore-msg" class="result"></div>
  __RESTORE_ACTION__
  <div id="restore-manual" style="margin-top:16px__MANUAL_HIDE__">
    <label class="field" style="display:block;font-size:12.8px;color:var(--sub);margin-bottom:6px">没有找到JSON格式的映射文件？手动选择对应的*.mapping.json：</label>
    <input type="file" id="manual-mapping" accept=".json">
    <div style="margin-top:12px">
      <button class="btn primary" type="button" onclick="uploadAndRestore()">上传映射并还原</button>
    </div>
  </div>
</div>

<script>
var SID='__SID__', FILE='__FILENAME__';
function goMode(m){
  Array.prototype.forEach.call(document.querySelectorAll('#mode-picker .mode'),function(el){
    el.classList.toggle('active', el.getAttribute('data-mode')===m);
  });
  document.getElementById('redact-panel').style.display = m==='redact' ? '' : 'none';
  document.getElementById('restore-panel').style.display = m==='restore' ? '' : 'none';
}
Array.prototype.forEach.call(document.querySelectorAll('#mode-picker .mode'),function(el){
  el.addEventListener('click',function(){ goMode(el.getAttribute('data-mode')); });
});

function okBox(url,name,extra){
  return '<div class="alert ok"><span class="i">✓</span><span>还原完成，共替换 '+extra+' 处。'+
         '<a class="btn ok sm" style="margin-left:10px" href="'+url+'">下载 '+name+'</a></span></div>';
}
async function runRestore(){
  var out=document.getElementById('restore-msg');
  out.innerHTML='<div class="alert info"><span class="i">⏳</span><span>正在还原正文文字…</span></div>';
  try{
    var r=await fetch('/api/restore/'+SID,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mapping:null})});
    var j=await r.json();
    if(j.ok){
      var warn=(j.missing&&j.missing.length)?('<div class="alert warn" style="margin-top:10px"><span class="i">⚠</span><span>有 '+j.missing.length+' 个占位符未在文档中找到，可能已被手工修改。</span></div>'):'';
      out.innerHTML=okBox(j.output_url,j.output_name,j.applied)+warn;
    }else{
      out.innerHTML='<div class="alert err"><span class="i">✗</span><span>还原失败：'+(j.error||'未知错误')+'</span></div>';
    }
  }catch(e){ out.innerHTML='<div class="alert err"><span class="i">✗</span><span>请求失败：'+String(e)+'</span></div>'; }
}
async function uploadAndRestore(){
  var file=document.getElementById('manual-mapping').files[0];
  if(!file){ alert('请先选择对应的JSON格式映射文件'); return; }
  var out=document.getElementById('restore-msg');
  out.innerHTML='<div class="alert info"><span class="i">⏳</span><span>正在上传映射并还原…</span></div>';
  try{
    var fd=new FormData(); fd.append('mapping',file);
    var r=await fetch('/api/restore/'+SID,{method:'POST',body:fd});
    var j=await r.json();
    if(j.ok){ out.innerHTML=okBox(j.output_url,j.output_name,j.applied); }
    else{ out.innerHTML='<div class="alert err"><span class="i">✗</span><span>还原失败：'+(j.error||'未知错误')+'</span></div>'; }
  }catch(e){ out.innerHTML='<div class="alert err"><span class="i">✗</span><span>请求失败：'+String(e)+'</span></div>'; }
}
goMode('__INIT_MODE__');
</script>
"""


def build_workspace_body(sid: str, info: Dict[str, Any]) -> bytes:
    name = esc(info.get("filename", ""))
    mode = info.get("mode")
    is_redacted = bool(info.get("looks_redacted") or info.get("is_redacted"))
    mapping = info.get("mapping_path")
    has_embedded = bool(info.get("has_embedded_mapping")) or mapping == "embedded"
    counts = info.get("counts") or {"types": 0, "occurrences": 0}

    if mode == "restore":
        summary = "还原模式 · 已跳过识别"
        head_title = "还原文书"
    else:
        summary = f"{counts.get('types', 0)} 类 / {counts.get('occurrences', 0)} 处候选项"
        head_title = "选择操作"

    hint = ""
    if is_redacted:
        hint = ('<div class="alert warn"><span class="i">⚠</span>'
                '<span>文档中检测到 <code>[类型#N]</code> 形态的占位符，看起来已经脱敏过。'
                '如果您要拿回原文，请选择 <b>还原</b>。</span></div>')

    if not mapping:
        restore_status = ('<div class="alert warn"><span class="i">⚠</span>'
                          '<span>没有在文档内部或同目录找到映射文件，需要您手动提供对应的JSON格式映射文件。</span></div>')
        restore_action = ""
        manual_hide = ""
    elif has_embedded:
        restore_status = ('<div class="alert ok"><span class="i">✓</span>'
                          '<span>已在文档内部（customXml）找到映射材料，可直接还原，无需外部文件。</span></div>')
        restore_action = ('<button class="btn primary lg" type="button" onclick="runRestore()">'
                          '✓ 直接执行还原</button>')
        manual_hide = ";display:none"
    else:
        restore_status = ('<div class="alert ok"><span class="i">✓</span>'
                          '<span>已找到映射文件：<code>' + esc(str(mapping).split("/")[-1]) + '</code></span></div>')
        restore_action = ('<button class="btn primary lg" type="button" onclick="runRestore()">'
                          '✓ 使用已找到的映射执行还原</button>')
        manual_hide = ";display:none"

    init_mode = "restore" if mode == "restore" else "redact"

    body = (WORKSPACE_BODY
            .replace("__HEAD_TITLE__", head_title)
            .replace("__FILENAME__", name)
            .replace("__SUMMARY__", summary)
            .replace("__SID__", esc(sid))
            .replace("__HINT__", hint)
            .replace("__TYPE_COUNT__", str(counts.get("types", 0)))
            .replace("__OCC_COUNT__", str(counts.get("occurrences", 0)))
            .replace("__RESTORE_STATUS__", restore_status)
            .replace("__RESTORE_ACTION__", restore_action)
            .replace("__MANUAL_HIDE__", manual_hide)
            .replace("__REDACT_ACTIVE__", "active" if init_mode == "redact" else "")
            .replace("__RESTORE_ACTIVE__", "active" if init_mode == "restore" else "")
            .replace("__INIT_MODE__", init_mode))
    return html_doc("选择操作 · " + info.get("filename", ""), body,
                    mid='<span class="pill">' + name + '</span>')


# =============================================================== 对比页 =======

REVIEW_BODY = r"""
<div class="page-head">
  <div>
    <h1>脱敏前 / 脱敏后 · 左右对比</h1>
    <div class="meta">
      <span class="pill">📄 __FILENAME__</span>
      <span class="pill ghost">__TYPES__ 类 · __OCC__ 处候选项</span>
      <span class="pill ghost">Session __SID__</span>
    </div>
  </div>
</div>

<div class="alert info" style="margin-bottom:14px"><span class="i">ℹ️</span><div><div style="margin-bottom:8px">__REVIEW_CONTEXT__</div><div style="font-size:13px;font-weight:400;line-height:1.65">使用说明：<br>①识别到的敏感信息默认勾选；低置信候选由您判断是否脱敏，勾选即可。<br>②可取消某类字段、点击原文高亮取消单处，也可手动补录漏识别字段。<br>③含批注或修订时按页面选项处理。</div><div class="notice-primary">__EXPORT_REVIEW_NOTICE__</div><small class="notice-secondary">__EXPORT_UNRECOGNIZED_NOTE__</small></div></div>
__LANGUAGE_NOTICE__
__REVISION_NOTICE__
__IMAGE_WARNING__
__OBJECT_WARNING__
__PARSE_WARNINGS__

<div class="card" id="commentRevisionSettings" style="display:none;margin:14px 0">
  <h2 style="margin:0 0 12px">批注与修订</h2>
  <div id="commentSettings" style="display:none">
    <div class="ab-row" style="align-items:center;gap:10px">
      <label for="commentMode"><b>批注处理</b></label>
      <select id="commentMode" style="padding:8px 10px">
        <option value="redact_preserve">保留并脱敏批注</option>
        <option value="preserve_unredacted">保留批注但不脱敏</option>
        <option value="remove_all">移除全部批注</option>
      </select>
      <span class="stat" id="commentSummary"></span>
    </div>
    <div class="stat" style="margin-top:8px"><b>工具只能按既有规则识别批注中的敏感信息，不能判断批注意见本身是否属于内部秘密，请人工核对。</b></div>
    <div class="stat" id="commentModeHint" style="margin-top:5px"></div>
  </div>
  <div id="revisionSettings" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft)">
    <div class="field" style="margin:0"><b>修订处理</b>：受支持的修订默认保留并脱敏，修订痕迹仍会显示。</div>
    <div class="stat" id="revisionModeHint" style="margin-top:5px"></div>
  </div>
  <div id="externalRelationshipSettings" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft)">
    <label for="externalRelationshipAction"><b>外部链接</b></label>
    <select id="externalRelationshipAction" style="padding:8px 10px;margin-left:8px">
      <option value="remove">外发时移除链接关系，保留可见文字</option>
      <option value="preserve">保留外部链接</option>
    </select>
    <div class="alert warn" id="externalRelationshipHint" style="margin:8px 0 0"></div>
  </div>
  <div id="externalTargetReview" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft)">
    <b>超链接目标中的敏感候选</b>
    <div id="externalTargetList" style="margin-top:7px"></div>
  </div>
</div>

<div class="toolbar">
  <div class="tb-row">
    <span class="tb-label">严重度</span>
    <div class="seg" id="sev">
      <button type="button" class="seg-btn on" data-sev="all">全部</button>
      <button type="button" class="seg-btn" data-sev="high">高</button>
      <button type="button" class="seg-btn" data-sev="medium">中</button>
      <button type="button" class="seg-btn" data-sev="low">低</button>
    </div>
    <span class="grow"></span>
    <button type="button" class="btn sm ghost" id="toggleTypes">识别类型：默认已选 ▾</button>
    <button type="button" class="btn sm ghost" id="selAll">全选</button>
    <button type="button" class="btn sm ghost" id="selNone">清空</button>
    <button type="button" class="btn sm ghost" id="resetMarks">恢复全部位置</button>
  </div>
</div>

<div class="card types-card open" id="typesWrap">
  <div id="types"></div>
  <div class="chips" style="margin-top:12px">
    <button type="button" class="chip addchip jsJumpTerms">＋ 添加自选字段（如公司简称）</button>
  </div>
</div>

<div class="card termcard" id="termsCard" style="margin:14px 0">
  <h2>添加漏识别的字段 <span class="pill acc" style="font-size:11px;vertical-align:middle">自动识别之外，在这里补</span></h2>
  <p class="stat">没在下方高亮里出现的敏感内容（例如<b>公司简称</b>、项目别名），在这里手工填写。自选原文和别名会用于当前文书；选择“本案后续上传也使用”后，填写相同案件名称的新文书也会沿用。已经导出的文件不会自动修改，需重新上传。诉讼材料还应检查当事人、证人、案号、住址、账号、医疗信息和商业秘密；后两类通常需要手工添加。</p>
  <div class="ab-row" style="flex-wrap:wrap;gap:8px">
    <input id="termLabel" maxlength="12" placeholder="字段名，如当事人" style="padding:9px;min-width:130px">
    <input id="termValue" maxlength="120" placeholder="原文，如北京某某公司" style="padding:9px;min-width:230px;flex:1">
    <input id="termAliases" placeholder="别名，使用逗号分隔（可选）" style="padding:9px;min-width:230px;flex:1">
    <select id="termScope" style="padding:9px"><option value="case">本案后续上传也使用</option><option value="document">只在当前文书使用</option></select>
    <button type="button" class="btn sm" id="addTerm">添加并重新识别</button>
  </div>
  <div id="termList" class="stat" style="margin-top:9px"></div>
  <div id="termError" class="result"></div>
</div>

<div class="card lowcard" id="lowCard" style="display:none;margin:14px 0">
  <h2>低置信候选 <span class="pill" style="font-size:11px;vertical-align:middle">机器没把握 · 已默认不勾</span></h2>
  <p class="stat">把握是机器对「像不像」的自我评分，<b>不是正确的概率</b>。<b>没把握 ≠ 不是敏感信息</b>——默认不勾只是提示您自己判断一下；要处理，勾上即可。</p>
  <div id="lowList"></div>
  <div id="lowMore" style="margin-top:10px"></div>
</div>

<div class="compare">
  <div class="pane">
    <div class="pane-head"><span class="sw"></span>原文 · 彩色高亮 = 会被替换；点击高亮可取消该处</div>
    <div class="pane-body doc" id="left"></div>
  </div>
  <div class="pane">
    <div class="pane-head"><span class="sw alt"></span>脱敏后 · 占位符预览（随勾选实时变化）</div>
    <div class="pane-body doc" id="right"></div>
  </div>
</div>

<div class="actionbar">
  <div class="ab-row">
    <button class="btn primary lg" type="button" id="go" __REVISION_BLOCKED_ATTR__>__REVISION_BUTTON_LABEL__</button>
    <button class="btn ghost" type="button" onclick="history.back()">返回</button>
    <span class="grow"></span>
    <span class="stat" id="stat"></span>
  </div>
  <div class="out-row">
    <label class="chk locked" title="脱敏文件为必选项"><input type="checkbox" id="optDoc" checked disabled><span>脱敏文件 <b>*.__OUTPUT_EXT__</b></span></label>
    <label class="chk" id="jsonWrap" title="默认未勾选；勾选后仅在脱敏成功时下载JSON格式的映射文件。请单独保管，勿随脱敏稿外发。"><input type="checkbox" id="optJson"><span>下载JSON格式的映射文件（*.mapping.json）</span></label>
    <button type="button" class="btn sm ghost" id="pickDir" title="选择一个文件夹后，将所勾选的文件保存进去">选择保存文件夹…</button>
    <span class="folder" id="dirState">未选文件夹 → 按浏览器下载设置保存</span>
  </div>
  <div class="stat" style="margin-top:7px">浏览器可能询问保存位置；否则按您在浏览器中设置的下载位置保存。输出文件名沿用源文件名，请检查文件名并在含敏感信息时自行修改。</div>
  <div class="stat" id="mappingSaveHelp" style="margin-top:7px">JSON格式的映射文件（*.mapping.json）仅在脱敏成功后生成，含原文且不嵌入脱敏DOCX。默认未勾选。勾选上方选项后，文件会在脱敏成功后下载；已授权保存文件夹时写入该文件夹，否则按浏览器下载设置保存。未勾选时，请在成功结果区点击“另存JSON格式的映射文件”；手动另存按浏览器下载设置保存。导出失败时不会显示本次文件的另存入口。映射文件请单独保管，不要随脱敏文档外发。</div>
  <div id="result" class="result"></div>
</div>

<script>
var DATA = __DATA__;
var EXPORT_REVIEW_NOTICE = __EXPORT_REVIEW_NOTICE_JSON__;
var EXPORT_UNRECOGNIZED_NOTE = __EXPORT_UNRECOGNIZED_NOTE_JSON__;
var enabled = new Set(DATA.types.filter(function(t){return t.default_enabled!==false;}).map(function(t){return t.id;}));
var commentMode = DATA.comment_mode_default || 'redact_preserve';
var revisionInventory = DATA.revision_inventory || {};
var unsupportedRevisions = revisionInventory.unsupported_manifest || [];
var passthroughUnverified = !!revisionInventory.passthrough_safe && !revisionInventory.complete;
var unsafeFieldCount = Number(revisionInventory.unsafe_field_instruction_count || 0);
var unredactedContentCount = Number(revisionInventory.unredacted_content_count || 0);
var unredactedContentManifest = revisionInventory.unredacted_content_manifest || [];
var embeddedImageCount = Number(revisionInventory.embedded_image_count || 0);
var headerFooterParts = revisionInventory.header_footer_content_parts || [];
var hasUnsupportedRevisions = unsupportedRevisions.length > 0 || passthroughUnverified ||
  !!(revisionInventory.unsupported_object_manifest||[]).length || unsafeFieldCount > 0 ||
  !!revisionInventory.unscanned_part_count;
var supportedRevisionCount = Number(revisionInventory.supported_text_revision_count || 0) +
  Number(revisionInventory.supported_property_revision_count || 0);
var hasSupportedRevisions = !!(revisionInventory.has_revisions && supportedRevisionCount > 0);
var hasAnyRevisions = !!revisionInventory.has_revisions;
var preserveRevisions = true;
var externalRelationshipCount = Number(revisionInventory.external_relationship_count || 0);
var externalTargets = DATA.external_targets || [];
var externalRelationshipAction = 'remove';
var settingsWrap = document.getElementById('commentRevisionSettings');
if(DATA.comments && DATA.comments.has_comments){
  settingsWrap.style.display='block';
  document.getElementById('commentSettings').style.display='block';
  document.getElementById('commentMode').value=commentMode;
  document.getElementById('commentSummary').textContent=DATA.comments.unsupported&&DATA.comments.unsupported.length?
    '存在批注相关部件，发现 '+DATA.comments.unsupported.length+' 项无法安全脱敏结构':
    '批注 '+DATA.comments.count+' 条，段落 '+DATA.comments.paragraph_count+' 段，插入人 '+DATA.comments.author_count+' 位，候选 '+DATA.comments.candidate_count+' 处';
  function updateCommentMode(){
    commentMode=document.getElementById('commentMode').value;
    var remove=commentMode==='remove_all';
    var preserveRaw=commentMode==='preserve_unredacted';
    var unsupported=DATA.comments.unsupported&&DATA.comments.unsupported.length>0;
    document.getElementById('commentModeHint').textContent=remove?
      '导出时将删除批注正文、批注人和锚点；mapping不能恢复已删除的批注结构。':
      (preserveRaw?(unsupported?
       '批注将原样保留，不会脱敏；当前无法识别并脱敏批注内容，请人工核对批注内的敏感信息。':
       '批注将原样保留，不会脱敏；请人工核对批注内的敏感信息。'):
       '批注正文和批注人中勾选的内容会写入mapping供还原；mapping含原文，请单独保管。');
  }
  document.getElementById('commentMode').addEventListener('change',function(){ updateCommentMode(); render(); });
  updateCommentMode();
}
if(hasSupportedRevisions || hasUnsupportedRevisions){
  settingsWrap.style.display='block';
  document.getElementById('revisionSettings').style.display='block';
  var revisionHint=document.getElementById('revisionModeHint');
  if(passthroughUnverified){
    revisionHint.textContent=DATA.revision_warning||
      '修订盘点未完整完成；可以继续导出，无法确认的内容将按原样保留。';
  }else if(hasSupportedRevisions){
    revisionHint.textContent='已选择保留并脱敏受支持的修订；如需不带修订痕迹的版本，请先在Word中处理后重新上传。';
  }else{
    revisionHint.textContent=DATA.revision_warning||'未支持的修订内容将按原样保留。';
  }
}
if(externalRelationshipCount){
  settingsWrap.style.display='block';
  var externalWrap=document.getElementById('externalRelationshipSettings');
  var externalSelect=document.getElementById('externalRelationshipAction');
  externalWrap.style.display='block';
  document.getElementById('externalRelationshipHint').textContent=
    '检测到外部超链接或资源关系。默认移除外部关系并保留超链接显示文字。如选择保留，工具会扫描超链接显示文字和目标网址中符合规则的敏感信息并提供候选；不会扫描链接打开后的远端网页内容、其他关系目标及Word字段指令中的网址。外链图片或对象可能不再显示。';
  function updateExternalRelationshipAction(){
    externalRelationshipAction=externalSelect.value;
    document.getElementById('externalTargetReview').style.display=
      externalRelationshipAction==='preserve'&&externalTargets.length?'block':'none';
  }
  externalRelationshipAction=externalSelect.value;
  externalSelect.addEventListener('change',function(){
    updateExternalRelationshipAction();
    render();
  });
  updateExternalRelationshipAction();
}
if(!DATA.case_name){
  document.getElementById('termScope').value='document';
  document.getElementById('termScope').disabled=true;
}
var sev = 'all';
var itemEnabled = new Map();
// v1.3.0「低置信可见可勾」：分数低于门槛的候选机器没把握 —— 复核页里可见，但默认不勾。
// 注意：这是「不确定性」，与「金额/日期被故意跳过」（策略性排除）语义不同，须分开呈现。
var LOWCUT = (typeof DATA.min_confidence==='number') ? DATA.min_confidence : 0.5;
var lowExpanded = false;                       // 低置信列表默认折叠
function isLow(m){ return typeof m.conf==='number' && m.conf < LOWCUT; }
DATA.blocks.forEach(function(b,bi){ (b.marks||[]).forEach(function(m,mi){ itemEnabled.set(bi+':'+mi, !isLow(m)&&!m.rewrite_blocked); }); });
externalTargets.forEach(function(m,i){ itemEnabled.set('external:'+i, !isLow(m)); });

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }

function renderTerms(){
  var box=document.getElementById('termList'); box.innerHTML='';
  if(!(DATA.terms||[]).length){box.textContent='尚无自选字段';return;}
  DATA.terms.forEach(function(t){
    var row=document.createElement('div'); row.className='ln';
    var label=document.createElement('span');
    label.textContent=t.label+'（'+(t.scope==='document'?'只在本文':'本案后续沿用')+'）：'+(t.values||[]).join('、')+' ';
    var del=document.createElement('button'); del.type='button'; del.className='btn sm ghost'; del.textContent='删除';
    del.addEventListener('click',function(){changeTerm({action:'delete',id:t.id});});
    row.appendChild(label);row.appendChild(del);box.appendChild(row);
  });
}
async function changeTerm(payload){
  var out=document.getElementById('termError');out.textContent='正在更新…';
  try{
    payload.revision_mode=preserveRevisions?'redact_preserve':'require_resolved';
    var r=await fetch('/api/term/__SID__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var j=await r.json(); if(!j.ok) throw new Error(j.error||'更新失败');
    location.reload();
  }catch(e){out.textContent=String(e.message||e);}
}
document.getElementById('addTerm').addEventListener('click',function(){
  var label=document.getElementById('termLabel').value.trim();
  var original=document.getElementById('termValue').value.trim();
  var aliases=document.getElementById('termAliases').value.split(/[，,]/).map(function(v){return v.trim();}).filter(Boolean);
  changeTerm({action:'add',scope:document.getElementById('termScope').value,
              term:{label:label,values:[original].concat(aliases)}});
});
renderTerms();

function renderTypes(){
  var box=document.getElementById('types'); box.innerHTML='';
  var cats={};
  DATA.types.forEach(function(t){ (cats[t.cat]=cats[t.cat]||[]).push(t); });
  var rank={high:0,medium:1,low:2};
  var visible=0;
  Object.keys(cats).forEach(function(cat){
    var list=cats[cat].filter(function(t){ return sev==='all'||t.sev===sev; });
    if(!list.length) return;
    var d=document.createElement('div'); d.className='cat'; d.textContent=cat; box.appendChild(d);
    var wrap=document.createElement('div'); wrap.className='chips';
    list.slice().sort(function(a,b){ return rank[a.sev]-rank[b.sev]; }).forEach(function(t){
      var on = enabled.has(t.id);
      var c=document.createElement('label');
      c.className='chip'+(on?' on':' off');
      c.setAttribute('data-sev',t.sev);
      c.innerHTML='<input type="checkbox" '+(on?'checked':'')+'>'+
        '<span class="dot" style="background:'+({high:'#e5484d',medium:'#dd9207',low:'#0d9bc0'}[t.sev]||'#94a3b8')+'"></span>'+
        esc(t.label)+' <span class="cnt">'+t.count+'</span>';
      c.querySelector('input').addEventListener('change',function(ev){
        if(ev.target.checked) enabled.add(t.id); else enabled.delete(t.id);
        render();
      });
      wrap.appendChild(c);
      visible++;
    });
    box.appendChild(wrap);
  });
  document.getElementById('toggleTypes').textContent='识别类型：已选 '+enabled.size+' 类 ▾';
}

function renderExternalTargets(){
  var box=document.getElementById('externalTargetList');
  if(!box) return;
  box.replaceChildren();
  var wrap=document.getElementById('externalTargetReview');
  if(externalRelationshipAction!=='preserve'||!externalTargets.length){wrap.style.display='none';return;}
  wrap.style.display='block';
  externalTargets.forEach(function(m,i){
    var row=document.createElement('label');row.className='field';
    var cb=document.createElement('input');cb.type='checkbox';
    cb.checked=enabled.has(m.type)&&!!itemEnabled.get('external:'+i);
    cb.disabled=!enabled.has(m.type);
    cb.addEventListener('change',function(){itemEnabled.set('external:'+i,cb.checked);render();});
    var text=document.createElement('span');
    text.textContent=m.label+'：'+m.display_text+' → '+m.placeholder+
      (isLow(m)?'（把握较低，默认未勾选）':'');
    row.dataset.externalIndex=String(i);
    row.append(cb,text);box.append(row);
  });
}

function render(){
  var goButton=document.getElementById('go');
  goButton.disabled=false;
  goButton.textContent='导出脱敏文件';
  Array.prototype.forEach.call(document.querySelectorAll('.seg-btn'),function(b){
    b.classList.toggle('on', b.getAttribute('data-sev')===sev);
  });
  renderTypes();
  renderExternalTargets();
  var left=[],right=[],n=0,lastLoc=null;
  DATA.blocks.forEach(function(b,bi){
    var ms0=b.marks||[];
    var rawComment=commentMode==='preserve_unredacted'&&String(b.kind||'').indexOf('comment_')===0;
    var plain=(b.text||'').replace(/\s+/g,' ').trim();
    if(!plain && !ms0.length) return;                 /* 跳过空段落，减少噪音 */
    var loc=b.loc||('段落 '+(bi+1));
    var head=(loc!==lastLoc)?('<span class="loc">'+esc(loc)+'</span>'):'';
    lastLoc=loc;
    var ms=ms0.slice().sort(function(a,c){return a.s-c.s;});
    var pos=0,L='',R='';
    ms.forEach(function(m,mi){
      m.bk=bi; m.mi=mi;
      var ls=m.s-b.start, le=m.e-b.start;
      if(ls<pos||ls<0) return;
      L+=esc(b.text.slice(pos,ls)); R+=esc(b.text.slice(pos,ls));
      var low=isLow(m);
      if(m.rewrite_blocked){
        var blockedReason=m.rewrite_block_reason||'该处文字无法安全定位，不能选择脱敏';
        L+='<span class="off" title="'+esc(blockedReason)+'">'+esc(b.text.slice(ls,le))+'</span>';
        R+=esc(b.text.slice(ls,le));
      }else if(enabled.has(m.t)&&itemEnabled.get(bi+':'+mi)&&!rawComment){
        n++;
        L+='<mark class="'+m.sev+(low?' lowconf':'')+'" data-bk="'+bi+'" data-mi="'+mi+'" title="点击取消该处'+(low?'（把握较低）':'')+'">'+esc(b.text.slice(ls,le))+'</mark>';
        R+='<mark class="ph sev-'+m.sev+'" title="'+esc(m.label)+'">'+esc(m.placeholder||m.masked)+'</mark>';
      }else{
        L+='<span class="off'+(low?' lowconf':'')+'" data-bk="'+bi+'" data-mi="'+mi+'" title="'+(rawComment?'批注将原样保留，不会脱敏':(low?'把握较低，默认未脱敏；点一下即可勾选处理':'点击恢复该处'))+'">'+esc(b.text.slice(ls,le))+'</span>';
        R+=esc(b.text.slice(ls,le));
      }
      pos=le;
    });
    L+=esc(b.text.slice(pos)); R+=esc(b.text.slice(pos));
    left.push('<div class="blk">'+head+(L||'&nbsp;')+'</div>');
    right.push('<div class="blk">'+head+(R||'&nbsp;')+'</div>');
  });
  var lEl=document.getElementById('left'), rEl=document.getElementById('right');
  if(!DATA.blocks.length){
    lEl.innerHTML='<div class="empty">未读取到可复核的正文</div>';
    rEl.innerHTML='<div class="empty">不能判断是否含有敏感信息。请检查文件格式，或先完成本地 OCR 再上传。</div>';
  }else{
    lEl.innerHTML=left.join('');
    rEl.innerHTML=right.join('');
  }
  Array.prototype.forEach.call(document.querySelectorAll('mark[data-bk]'),function(el){
    el.addEventListener('click',function(){
      itemEnabled.set(el.getAttribute('data-bk')+':'+el.getAttribute('data-mi'),false); render();
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('span.off[data-bk]'),function(el){
    el.addEventListener('click',function(){
      itemEnabled.set(el.getAttribute('data-bk')+':'+el.getAttribute('data-mi'),true); render();
    });
  });
  document.getElementById('stat').innerHTML='将替换 <b>'+n+'</b> 处 · 生效类型 <b>'+enabled.size+'</b> 类';
  renderLow();
}

/* 低置信候选面板：按实体（占位符）分组，展开后可逐条勾选、可点回复核页对应位置。 */
function lowGroups(){
  var groups={};
  DATA.blocks.forEach(function(b,bi){
    (b.marks||[]).forEach(function(m,mi){
      if(!enabled.has(m.t)||!isLow(m)||m.rewrite_blocked) return;
      var key=m.t+'|'+(m.placeholder||m.masked||'');
      var g=groups[key]||(groups[key]={t:m.t,sev:m.sev,label:m.label,
        ph:m.placeholder||m.masked||'',conf:m.conf,items:[],reviewReasons:[]});
      if(typeof m.conf==='number') g.conf=Math.max(g.conf,m.conf);
      (m.review_reasons||[]).forEach(function(reason){
        if(g.reviewReasons.indexOf(reason)<0) g.reviewReasons.push(reason);
      });
      g.items.push({bi:bi,mi:mi,key:bi+':'+mi,loc:b.loc||('段落 '+(bi+1)),
        text:b.text.slice(m.s-b.start,m.e-b.start)});
    });
  });
  externalTargets.forEach(function(m,i){
    if(!enabled.has(m.type)||!isLow(m)) return;
    var key=m.type+'|'+(m.placeholder||'');
    var g=groups[key]||(groups[key]={t:m.type,sev:'low',label:m.label,
      ph:m.placeholder||'',conf:m.confidence,items:[],reviewReasons:[]});
    g.items.push({key:'external:'+i,externalIndex:i,loc:'外部超链接目标',text:m.display_text});
  });
  return Object.keys(groups).map(function(k){return groups[k];});
}
function lowCheckedCount(){
  var n=0;
  DATA.blocks.forEach(function(b,bi){
    (b.marks||[]).forEach(function(m,mi){
      if(enabled.has(m.t)&&isLow(m)&&!m.rewrite_blocked&&itemEnabled.get(bi+':'+mi)) n++;
    });
  });
  externalTargets.forEach(function(m,i){
    if(enabled.has(m.type)&&isLow(m)&&itemEnabled.get('external:'+i)) n++;
  });
  return n;
}
function renderLow(){
  var card=document.getElementById('lowCard'), box=document.getElementById('lowList');
  var more=document.getElementById('lowMore');
  if(!card) return;
  var gs=lowGroups();
  if(!gs.length){ card.style.display='none'; return; }
  card.style.display='';
  var total=0; gs.forEach(function(g){ total+=g.items.length; });
  var head=document.querySelector('#lowCard h2 .pill');
  if(head) head.textContent='机器没把握 '+total+' 处 · 已默认不勾';

  box.innerHTML='';
  var shown = lowExpanded ? gs : gs.slice(0,3);
  shown.forEach(function(g,gi){
    var row=document.createElement('div'); row.className='lc-row';
    var idx=document.createElement('div'); idx.className='n'; idx.textContent=(gi+1);
    var bd=document.createElement('div'); bd.className='b';
    var anyOn=g.items.some(function(it){return itemEnabled.get(it.key||it.bi+':'+it.mi);});
    bd.innerHTML='<div class="t"><span class="lc-badge">把握较低</span>'+
      esc(g.label)+'：'+esc(g.items[0].text)+(g.items.length>1?('　<span style="color:var(--faint)">共 '+g.items.length+' 处</span>'):'')+'</div>'+
      '<div class="m">'+esc(g.items.slice(0,3).map(function(it){return it.loc;}).join('、'))+'</div>';
    if(g.reviewReasons.length){
      bd.innerHTML+='<div class="m">'+esc(g.reviewReasons.join('；'))+'</div>';
    }
    var lab=document.createElement('label'); lab.className='chk'; lab.style.flex='none';
    var cb=document.createElement('input'); cb.type='checkbox'; cb.checked=anyOn;
    cb.addEventListener('change',function(){
      g.items.forEach(function(it){ itemEnabled.set(it.key||it.bi+':'+it.mi, cb.checked); });
      render();
    });
    lab.appendChild(cb); lab.appendChild(document.createTextNode('脱敏'));
    bd.appendChild(document.createElement('div'));
    row.appendChild(idx); row.appendChild(bd); row.appendChild(lab);
    row.addEventListener('click',function(ev){
      if(ev.target===cb||ev.target===lab) return;
      var it=g.items[0]; var tgt=it.externalIndex!==undefined?
        document.querySelector('[data-external-index="'+it.externalIndex+'"]'):
        (document.querySelector('span.off[data-bk="'+it.bi+'"][data-mi="'+it.mi+'"]')||
        document.querySelector('mark[data-bk="'+it.bi+'"][data-mi="'+it.mi+'"]'));
      if(tgt){ tgt.scrollIntoView({behavior:'smooth',block:'center'});
        tgt.style.outline='2px solid var(--acc)';
        setTimeout(function(){ tgt.style.outline=''; },1600); }
    });
    box.appendChild(row);
  });

  var bits=[];
  if(gs.length>3 && !lowExpanded) bits.push('<button type="button" class="btn sm ghost" id="lowToggle">显示全部候选（含把握较低的，共 '+total+' 处）</button>');
  else if(gs.length>3 && lowExpanded) bits.push('<button type="button" class="btn sm ghost" id="lowToggle">收起</button>');
  more.innerHTML=bits.join(' ');
  var tg=document.getElementById('lowToggle');
  if(tg) tg.onclick=function(){ lowExpanded=!lowExpanded; renderLow(); };
}

/* 左右同步滚动 */
(function(){
  var l=document.getElementById('left'), r=document.getElementById('right'), lock=false;
  function link(a,b){
    a.addEventListener('scroll',function(){
      if(lock) return; lock=true; b.scrollTop=a.scrollTop; b.scrollLeft=a.scrollLeft;
      setTimeout(function(){lock=false;},30);
    });
  }
  link(l,r); link(r,l);
})();

Array.prototype.forEach.call(document.querySelectorAll('.seg-btn'),function(b){
  b.addEventListener('click',function(){ sev=b.getAttribute('data-sev'); render(); });
});
document.getElementById('toggleTypes').addEventListener('click',function(){
  document.getElementById('typesWrap').classList.toggle('open');
});
// 自选字段入口：滚到「添加漏识别的字段」卡片、高亮一次并聚焦「字段名」输入框
function jumpToTerms(){
  var card=document.getElementById('termsCard'); if(!card) return;
  card.scrollIntoView({behavior:'smooth',block:'center'});
  card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');
  var f=document.getElementById('termLabel'); if(f) f.focus({preventScroll:true});
}
Array.prototype.forEach.call(document.querySelectorAll('.jsJumpTerms'),function(b){
  b.addEventListener('click',jumpToTerms);
});
document.getElementById('selAll').addEventListener('click',function(){
  enabled=new Set(DATA.types.map(function(t){return t.id;})); render();
});
document.getElementById('selNone').addEventListener('click',function(){
  enabled=new Set(); render();
});
document.getElementById('resetMarks').addEventListener('click',function(){
  DATA.blocks.forEach(function(b,bi){ (b.marks||[]).forEach(function(m,mi){ itemEnabled.set(bi+':'+mi,!m.rewrite_blocked); }); });
  render();
});

// ============================================================ 输出保存 =======
// 脱敏文书始终下载；JSON格式的映射文件仅在脱敏成功后生成，默认不自动下载。
// 手段：① 用户若选过文件夹 → 用 File System Access API 直接写入该文件夹
//          （127.0.0.1 属安全上下文，Chrome/Edge 可用；不再弹下载框）
//       ② 否则 → 按浏览器设置触发所选文件的下载（兜底，任何浏览器都可）
var dirHandle = null;
var pickBtn = document.getElementById('pickDir');
var dirStateEl = document.getElementById('dirState');
var FSA_OK = !!(window && window.showDirectoryPicker);

if(!FSA_OK){
  pickBtn.disabled = true;
  pickBtn.textContent = '当前浏览器不支持选文件夹';
  pickBtn.title = '将按浏览器中设置的下载位置保存；浏览器也可能询问保存位置';
}

pickBtn.addEventListener('click', async function(){
  if(!FSA_OK) return;
  try{
    var h = await window.showDirectoryPicker({id:'contract-redactor-out', mode:'readwrite', startIn:'downloads'});
    if(await h.queryPermission({mode:'readwrite'}) !== 'granted'){
      if(await h.requestPermission({mode:'readwrite'}) !== 'granted') return;
    }
    dirHandle = h;
    dirStateEl.className = 'folder on';
    dirStateEl.innerHTML = '已选择文件夹：<code>' + esc(h.name) + '</code>（仅保存所勾选的文件）';
    pickBtn.textContent = '更换文件夹…';
  }catch(e){ /* 用户取消选择，保持原状 */ }
});

function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }

function browserDownload(url){
  var a = document.createElement('a');
  a.href = url; a.download = ''; a.rel = 'noopener'; a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(function(){ if(a.parentNode) a.parentNode.removeChild(a); }, 2000);
}

async function writeToFolder(url, name){
  var resp = await fetch(url);
  if(!resp.ok) throw new Error('HTTP ' + resp.status);
  var blob = await resp.blob();
  var fh = await dirHandle.getFileHandle(name, {create:true});
  var w = await fh.createWritable();
  await w.write(blob);
  await w.close();
  return blob.size;
}

// 自动保存脱敏文书；mapping仅在用户主动勾选后自动保存。
async function saveOutputs(j){
  var jobs = [{url:j.output_url, name:j.output_name}];
  if(document.getElementById('optJson').checked && j.mapping_url){
    jobs.push({url:j.mapping_url, name:j.mapping_name || 'mapping.json'});
  }

  if(dirHandle){
    var saved = [], failed = [];
    for(var i=0;i<jobs.length;i++){
      try{
        var sz = await writeToFolder(jobs[i].url, jobs[i].name);
        saved.push({name:jobs[i].name, size:sz, how:'folder'});
      }catch(e){
        failed.push(jobs[i]);
      }
    }
    for(var k=0;k<failed.length;k++){       // 写盘失败 → 退化成浏览器下载
      browserDownload(failed[k].url);
      saved.push({name:failed[k].name, size:0, how:'browser'});
      await sleep(400);
    }
    return {folder:dirHandle.name, files:saved, degraded:failed.length > 0};
  }

  // 未选文件夹：按浏览器设置保存，浏览器可能询问具体位置。
  var out = [];
  for(var n=0;n<jobs.length;n++){
    browserDownload(jobs[n].url);
    out.push({name:jobs[n].name, size:0, how:'browser'});
    if(n < jobs.length - 1) await sleep(500);
  }
  return {folder:'', files:out, degraded:false};
}

function renderSaved(sv, j){
  var lines = sv.files.map(function(f){
    var kb = f.size ? '（' + Math.max(1, Math.round(f.size/1024)) + ' KB）' : '';
    return '<div class="ln">• <b>' + esc(f.name) + '</b>' + kb + '</div>';
  }).join('');
  var head;
  if(sv.folder){
    head = '已自动保存到文件夹 <code style="background:var(--surface-2);border:1px solid var(--line-soft);border-radius:6px;padding:1px 6px">' + esc(sv.folder) + '</code>';
    if(sv.degraded) head += '（部分文件写入失败，已改为下载）';
  }else{
    head = '已触发浏览器下载；浏览器可能询问保存位置，否则按您在浏览器中设置的下载位置保存。';
  }
  return '<div class="saved">' +
    '<div class="ln saved-muted">' + head + '</div>' + lines +
    '<div class="ln saved-emphasis">请检查输出文件名；如果文件名包含敏感信息，请自行重命名。</div>' +
    (j.mapping_url?'<div class="ln saved-emphasis">提示：还原须使用对应的JSON格式映射文件，请单独保管，勿随脱敏稿外发。</div>':'<div class="ln saved-emphasis">本次没有可还原替换记录，不提供映射文件；清理操作可能不可还原。</div>') +
    '</div>';
}

function renderSavedWarnings(warnings){
  var text=String(warnings||'');
  var seen=[];
  text=text.split('；').map(function(part){return part.trim();}).filter(function(part){
    if(!part||seen.indexOf(part)>=0)return false;
    seen.push(part);return true;
  }).join('；');
  var unscanned='相关脚注、尾注或其他未纳入扫描的文档内容整体未经扫描和脱敏，其中普通文字也可能含敏感信息。';
  var emphasis='';
  if(text.indexOf(unscanned)>=0){
    text=text.split(unscanned).join('').trim();
    emphasis='<div class="saved-warning-emphasis">'+esc(unscanned)+'</div>';
  }
  var other=text?'<div class="saved-warning-muted">'+esc(text)+'</div>':'';
  return other+emphasis;
}

function renderSkippedDetails(rows){
  if(!rows||!rows.length)return '';
  var items=rows.map(function(row){
    var where=String(row.location||'文档位置待确认');
    var label=String(row.label||row.type||'已选内容');
    return '<div class="ln">'+esc(label)+' · '+esc(where)+' · '+esc(row.value||'')+'</div>';
  }).join('');
  return '<div class="saved-warning-emphasis">以下已选内容未自动改写，已按原文导出；请在Word中搜索并手动处理：'+items+'</div>';
}

function collectItems(){
  var items=[];
  DATA.blocks.forEach(function(b,bi){
    if(commentMode==='preserve_unredacted'&&String(b.kind||'').indexOf('comment_')===0) return;
    (b.marks||[]).forEach(function(m,mi){
      // 用绝对偏移（m.s/m.e）而不是实体内部序号：序号每个实体都从 0 重来，会串号
      if(enabled.has(m.t)&&itemEnabled.get(bi+':'+mi)){ items.push({type:m.t,offset:[m.s,m.e]}); }
    });
  });
  if(externalRelationshipAction==='preserve'){
    externalTargets.forEach(function(m,i){
      if(enabled.has(m.type)&&itemEnabled.get('external:'+i)){
        items.push({type:m.type,offset:m.offset});
      }
    });
  }
  return items;
}

/* 服务端按一次确认收集红档，其他提示在导出后显示。 */
function buildManifest(items){
  var red=[], soft=[];
  if(externalRelationshipCount){
    if(externalRelationshipAction==='remove') soft.push(
      '将移除 '+externalRelationshipCount+' 个外部链接或资源关系，保留超链接显示文字；外链图片或对象可能不再显示。');
  }
  if(hasSupportedRevisions&&preserveRevisions){
    soft.push('将保留并脱敏受支持的修订；修订痕迹仍会显示。');
  }
  if(DATA.comments&&DATA.comments.has_comments){
    var selectedComments=0, uncheckedLowComments=0;
    DATA.blocks.forEach(function(b,bi){
      if(String(b.kind||'').indexOf('comment_')!==0) return;
      (b.marks||[]).forEach(function(m,mi){
        var selected=enabled.has(m.t)&&itemEnabled.get(bi+':'+mi);
        if(selected) selectedComments++;
        else if(isLow(m)) uncheckedLowComments++;
      });
    });
    if(commentMode!=='preserve_unredacted'){
      soft.push('批注候选 '+DATA.comments.candidate_count+' 处，当前选中 '+selectedComments+
        ' 处，未选低置信 '+uncheckedLowComments+' 处。');
      if(commentMode==='redact_preserve') soft.push('批注会保留并脱敏；批注意见仍需核对。');
    }
  }
  var hasUncheckedLowConfidence=false;
  DATA.blocks.forEach(function(b,bi){
    (b.marks||[]).forEach(function(m,mi){
      if(!enabled.has(m.t)||!isLow(m)) return;
      if(!itemEnabled.get(bi+':'+mi)) hasUncheckedLowConfidence=true;
    });
  });
  if(hasUncheckedLowConfidence){
    soft.push(EXPORT_REVIEW_NOTICE);
  }
  var offTypes=DATA.types.filter(function(t){ return !enabled.has(t.id); });
  if(offTypes.length){
    soft.push('已按您的选择保留 '+offTypes.length+' 类字段原文。');
  }
  return {red:red, soft:soft};
}

async function renderManifest(man, items, snapshot){
  if(!snapshot){
    try{
      var localPayload=canonicalApplyPayload(makeApplySelection(items,false));
      var response=await fetch('/api/export-plan/__SID__',{method:'POST',headers:{'Content-Type':'application/json'},body:localPayload});
      snapshot=await response.json();if(!snapshot.ok)throw Error(snapshot.error||'无法建立导出快照');snapshot.localPayload=localPayload;
    }catch(e){document.getElementById('result').textContent=String(e.message||e);return;}
  }
  var out=document.getElementById('result');
  var red=man.red||[];
  var html='<div class="manifest"><div class="head">导出前请知悉</div>'+
      '<div class="seg"><div class="notice-primary">'+esc(EXPORT_REVIEW_NOTICE)+'</div>'+
      '<small class="notice-secondary">'+esc(EXPORT_UNRECOGNIZED_NOTE)+'</small></div>';
  if(red.length) html+='<div class="seg unscanned"><div class="unscanned-title">本文件另有以下内容可能按原文保留：</div>'+red.map(function(t){ return '<div class="li"><span class="d">•</span><span>'+esc(t)+'</span></div>'; }).join('')+'</div>';
  html+='<div class="seg"><div class="acts"><button type="button" class="btn primary" id="manGo">我已知悉，仍要导出</button> '+
      '<button type="button" class="btn ghost" id="manBack">返回查看</button></div></div>';
  html+='</div>';
  out.innerHTML=html;
  var back=document.getElementById('manBack');
  if(back) back.onclick=function(){ out.innerHTML=''; };
  var go=document.getElementById('manGo');
  if(go) go.addEventListener('click',function(){ runApply(items,true,snapshot,man.soft||[]); });
}

function canonicalApplyPayload(payload){
  var normalized=Object.assign({},payload);
  if(Array.isArray(normalized.items)){
    normalized.items=normalized.items.slice().sort(function(a,b){
      var left=JSON.stringify(a,Object.keys(a||{}).sort());
      var right=JSON.stringify(b,Object.keys(b||{}).sort());
      return left<right?-1:left>right?1:0;
    });
  }
  return JSON.stringify(normalized);
}

function makeApplySelection(items,confirmed){
  return {enabled_types:Array.from(enabled),
             min_confidence:(DATA.min_confidence||0.5),
             min_severity:(DATA.min_severity||'low'),
             items:items,
             allow_no_replacements:!!(confirmed&&!items.length),
             comment_mode:commentMode,
             revision_mode:passthroughUnverified?'passthrough_unverified':
               'redact_preserve',
             revision_acknowledged:!!(confirmed&&hasUnsupportedRevisions),
             revision_ack_digest:revisionInventory.unsupported_digest||'',
             review_digest:revisionInventory.review_digest||'',
             external_relationship_action:externalRelationshipAction,
             external_relationship_acknowledged:!!(confirmed&&externalRelationshipAction==='preserve'),
             external_relationship_digest:revisionInventory.external_relationship_digest||'',
             confirmed:!!confirmed};
}

async function runApply(items, confirmed, snapshot, softNotes){
  var btn=document.getElementById('go'), out=document.getElementById('result');
  btn.disabled=true;
  out.innerHTML='<div class="alert info"><span class="i">⏳</span><span>正在生成文件并核验所选处理结果…</span></div>';
  try{
    // 与上传时的识别参数保持一致：否则服务端重扫得到的偏移会与这里的位置白名单对不上
    var sel=makeApplySelection(items,confirmed);
    if(snapshot){
      var currentItems=collectItems();
      if(snapshot.localPayload!==canonicalApplyPayload(makeApplySelection(currentItems,false))){
        var refreshed=buildManifest(currentItems);
        refreshed.red.unshift('导出选择已变化，请核对当前选择并重新确认。');
        btn.disabled=false;
        await renderManifest(refreshed,currentItems,null);
        return;
      }
      sel.selection_digest=snapshot.selection_digest;
    }else{
      var planResponse=await fetch('/api/export-plan/__SID__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sel)});
      var plan=await planResponse.json();if(!plan.ok)throw Error(plan.error||'导出快照校验失败');sel.selection_digest=plan.selection_digest;
    }
    var r=await fetch('/api/apply/__SID__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sel)});
    var j=await r.json();
    if(j.code==='EXPORT_SELECTION_STALE'){
      var updatedItems=collectItems();
      var updatedManifest=buildManifest(updatedItems);
      updatedManifest.red.unshift('导出选择已变化，请核对当前选择并重新确认。');
      btn.disabled=false;
      await renderManifest(updatedManifest,updatedItems,null);
      return;
    }
    if(j.needs_confirm){
      btn.disabled=false;
      await renderManifest({red:j.red||[],soft:(j.notes||[]).concat(softNotes||[])},items,snapshot);
      return;
    }
    if(j.ok){
      out.innerHTML='<div class="alert info"><span class="i">⏳</span><span>脱敏完成，正在保存文件…</span></div>';
      var sv=await saveOutputs(j);
      var postNotes=[j.warnings].concat(j.notes||[],softNotes||[]).filter(Boolean);
      postNotes=postNotes.filter(function(note,index){return postNotes.indexOf(note)===index;});
      out.innerHTML='<div class="alert ok"><span class="i">✓</span><span>脱敏完成：替换 '+j.applied+' 处，跳过 '+j.skipped+' 处。'+
        renderSaved(sv,j)+
        '<div style="margin-top:9px">'+
          '<a class="btn ok sm" href="'+j.output_url+'" download>另存 '+esc(j.output_name)+'</a>'+
          (j.mapping_url?('<a class="btn sm" style="margin-left:8px" href="'+j.mapping_url+'" download>另存JSON格式的映射文件</a>'):'')+
        '</div>'+
        renderSavedWarnings(postNotes.join('；'))+
        renderSkippedDetails(j.skipped_details)+
        '</span></div>';
    }else{
      var errorText=String(j.error||'未知错误').replace(/^(脱敏失败：)+/, '');
      out.innerHTML='<div class="alert err"><span class="i">✗</span><span>脱敏失败：'+esc(errorText)+'</span></div>';
    }
  }catch(e){
    out.innerHTML='<div class="alert err"><span class="i">✗</span><span>请求失败：'+esc(String(e))+'</span></div>';
  }
  btn.disabled=false;
}

document.getElementById('go').addEventListener('click',function(){
  var out=document.getElementById('result');
  var items=collectItems();
  var man=buildManifest(items);
  if(man.red.length){ renderManifest(man, items); return; }
  runApply(items,false,null,man.soft);
});

render();
</script>
"""


def build_review_page(sid: str, info: Dict[str, Any]) -> bytes:
    payload = info.get("review_payload") or {}
    report = info.get("report") or {}
    types_n = len(payload.get("types") or [])
    occ_n = (report.get("summary") or {}).get("occurrences", 0)
    name = info.get("filename", "")
    scene = ("当前用途：合同、诉讼及其他文书统一脱敏；诉讼案号、法院名称与公文/机构编号均作为候选，"
             "由您在对照页逐项选择。DOCX批注默认保留并脱敏，受支持修订默认保留并脱敏。")
    preflight = ((payload.get("revision_inventory") or {}).get("compat_preview") or [])
    if preflight:
        compatible = sum(row.get("verdict") == "supported" for row in preflight)
        blocked = sum(row.get("verdict") == "blocked" for row in preflight)
        scene += " 上传预检：%d处普通文字/页码控件可兼容，%d处控件仍需处理；最终导出按当前选择重新验证。" % (compatible, blocked)
    policy_description = payload.get("policy_description") or ""
    policy_count = int(payload.get("policy_excluded_count") or 0)
    if policy_description:
        scene += " " + policy_description
        if policy_count:
            scene += "；本文件有%d条金额、日期候选按策略默认保留" % policy_count
        scene += "。本会话不支持中途切换策略；如需更改，请重新上传。"
    case_name = info.get("case_name") or ""
    if case_name:
        inherited = len((info.get("case_data") or {}).get("terms") or [])
        case_context = ("案件或项目名称：" + case_name + "；已加载 " + str(inherited) +
                        " 条本案自选规则。以后上传时填写完全相同的名称，才能继续沿用；已导出的文件不会自动修改。")
    else:
        case_context = "未填写案件名称；新增的自选规则只用于当前文书。"
    context = esc(scene + " " + case_context)
    warnings = payload.get("warnings") or []
    warning_html = ('<div class="alert err" style="margin-bottom:14px"><span>解析提示：<br>' +
                    '<br>'.join(esc(str(w)) for w in warnings) + '</span></div>') if warnings else ''
    language_notice_html = (
        '<div class="alert warn" style="margin-bottom:14px"><span class="i">⚠</span>'
        '<div><b>检测到以英文为主的文书。英文规则仍有覆盖边界，自动候选不能证明文书已完整脱敏。'
        '请逐项核对姓名、地址、电话、身份与税务编号、银行信息及金额；未出现的字段可在“添加漏识别的字段”中补录。</b></div></div>'
        if payload.get("english_dominant") else "")
    output_ext = name.rsplit(".", 1)[-1].lower() if "." in name else "txt"
    image_count = int(payload.get("embedded_image_count") or 0)
    image_warning_html = (
        '<div class="alert warn" style="margin-bottom:14px"><span class="i">⚠</span>'
        '<div><b>当前DOCX含%d处内置图片或绘图对象，工具无法扫描其中的文字，'
        '请自行核对图片内是否包含敏感信息。</b></div></div>' % image_count
        if output_ext == "docx" and image_count > 0 else "")
    # The review page keeps a separate slot for object-specific diagnostics;
    # current wording consolidates those risks into the common notice.
    object_warning_html = ""
    revisions = payload.get("revision_inventory") or {}
    revision_html = ""
    revision_blocked = False
    if output_ext == "docx" and (
            revisions.get("unsupported_manifest") or
            revisions.get("unsupported_object_manifest") or
            revisions.get("unsafe_field_instruction_count") or
            revisions.get("unscanned_part_count") or
            revisions.get("unredacted_content_count") or
            revisions.get("header_footer_content_parts") or
            revisions.get("embedded_image_count") or
            revisions.get("hidden_text_count")):
        revision_html = (
            '<div class="notice-secondary" style="margin:-8px 0 14px 0">'
            '本文件包含未支持或未扫描结构；未扫描到的内容将按原文保留，具体项目会在导出前列出。'
            '</div>')
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")

    body = (REVIEW_BODY
            .replace("__FILENAME__", esc(name))
            .replace("__TYPES__", str(types_n))
            .replace("__OCC__", str(occ_n))
            .replace("__REVIEW_CONTEXT__", context)
            .replace("__EXPORT_REVIEW_NOTICE__", esc(EXPORT_REVIEW_NOTICE))
            .replace("__EXPORT_UNRECOGNIZED_NOTE__", esc(EXPORT_UNRECOGNIZED_NOTE))
            .replace("__EXPORT_REVIEW_NOTICE_JSON__", json.dumps(EXPORT_REVIEW_NOTICE, ensure_ascii=False))
            .replace("__EXPORT_UNRECOGNIZED_NOTE_JSON__", json.dumps(EXPORT_UNRECOGNIZED_NOTE, ensure_ascii=False))
            .replace("__LANGUAGE_NOTICE__", language_notice_html)
            .replace("__REVISION_NOTICE__", revision_html)
            .replace("__IMAGE_WARNING__", image_warning_html)
            .replace("__OBJECT_WARNING__", object_warning_html)
            .replace("__REVISION_BLOCKED_ATTR__", 'disabled aria-disabled="true"' if revision_blocked else "")
            .replace("__REVISION_BUTTON_LABEL__", "修订处理后重新上传" if revision_blocked else "确认并执行脱敏")
            .replace("__PARSE_WARNINGS__", warning_html)
            .replace("__OUTPUT_EXT__", esc(output_ext))
            .replace("__SID__", esc(sid))
            .replace("__DATA__", data_json))
    return html_doc("左右对比 · " + name, body,
                    mid='<span class="pill">' + esc(name) + '</span>')


# =============================================================== 失效页 =======

# =========================================================== 历史记录页 =====
# 内容全部来自本机磁盘（sessions/），不联网、不加载任何外部资源。
# 页面本身只在「服务重启后旧会话链接失效」这一痛点上做文章：列表 / 下载 / 删除。

HISTORY_BODY = r"""
<div class="page-head">
  <div>
    <h1>历史记录</h1>
    <div class="meta">
      <span class="pill">本机留存 __COUNT__ 次</span>
      <span class="pill ghost">占用 __TOTAL__</span>
      <span class="pill ghost">全部解析均在本机完成，文件不外传</span>
    </div>
  </div>
</div>

<div class="alert info">
  <span class="i">🔒</span>
  <span><b>这些记录只存在本机</b>：原件、映射与输出都在本地会话目录里，没有上传到任何服务器。含明文的记录可单独删除，删除后不可恢复。</span>
</div>

<div class="card" style="margin-bottom:12px;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap">
  <span class="muted">“清理全部明文”会按当前规则尝试清理<strong>工具数据目录中</strong>各会话记录的上传文书副本、PDF私有缓存和项目状态，以及符合当前命名规则的映射、处理日志和临时PDF文件。已完成的脱敏稿会保留。<strong>电脑原位置上的源文件、浏览器下载或用户另存的文件、案件/项目自选规则及会话目录外的文件不受影响</strong>；名为<code>mapping.json</code>等不符合规则的映射也可能保留。有PDF正在处理时，清理请求会被拒绝。<strong>单条记录旁的“删除”会尝试删除该条记录对应的会话目录</strong>；若删除失败，页面会提示错误。</span>
  <button id="clear-private" class="btn sm warn" type="button" onclick="clearPlaintext()">清理全部明文</button>
</div>

<div class="card">
__ROWS__
</div>

<p class="stat" style="margin-top:16px">
  <a class="btn sm" href="/">← 回到首页</a>
</p>

<div id="clear-modal" class="modal-backdrop" hidden>
  <section class="modal" role="dialog" aria-modal="true" aria-busy="false" aria-labelledby="clear-modal-title" aria-describedby="clear-modal-copy">
    <h2 id="clear-modal-title">确认尝试清理留存内容</h2>
    <p id="clear-modal-copy">将尝试清理<strong>当前工具数据目录中</strong>按会话规则识别出的文件，包括上传文书副本、PDF私有缓存和项目状态，以及符合当前命名规则的映射、处理日志和临时PDF文件。已完成的脱敏稿会保留。<strong>电脑原位置上的源文件、浏览器下载文件、用户另存文件、案件/项目自选规则及会话目录外的文件不在本次范围。</strong>名为<code>mapping.json</code>等不符合映射命名规则的文件可能无法清理。若有PDF正在处理，清理请求会被拒绝。此操作无法在工具内撤销，也不等于安全擦除。确定继续尝试清理吗？</p>
    <p id="clear-modal-status" class="muted" role="status" aria-live="polite" tabindex="-1"></p>
    <p id="clear-modal-error" class="modal-error" role="alert" hidden></p>
    <div class="modal-actions">
      <button id="clear-cancel" class="btn" type="button">取消</button>
      <button id="clear-confirm" class="btn warn" type="button">确定继续</button>
    </div>
  </section>
</div>

<script>
var clearModal=document.getElementById('clear-modal');
var clearConfirm=document.getElementById('clear-confirm');
var clearCancel=document.getElementById('clear-cancel');
var clearStatus=document.getElementById('clear-modal-status');
var clearError=document.getElementById('clear-modal-error');
var clearPreviousFocus=null;
var clearBusy=false;
function closeClearModal(){
  if(!clearModal||clearBusy)return;
  clearModal.hidden=true;
  clearStatus.textContent='';
  clearError.hidden=true;
  clearError.textContent='';
  clearModal.setAttribute('aria-busy','false');
  clearBusy=false;
  if(clearPreviousFocus&&clearPreviousFocus.focus)clearPreviousFocus.focus();
}
function openClearModal(){
  if(!clearModal)return;
  clearPreviousFocus=document.activeElement;
  clearModal.hidden=false;
  clearStatus.textContent='';
  clearError.hidden=true;
  clearError.textContent='';
  clearModal.setAttribute('aria-busy','false');
  clearConfirm.disabled=false;
  clearCancel.disabled=false;
  clearConfirm.focus();
}
clearCancel.addEventListener('click',closeClearModal);
clearModal.addEventListener('click',function(ev){if(ev.target===clearModal)closeClearModal();});
document.addEventListener('keydown',function(ev){
  if(clearModal.hidden)return;
  if(ev.key==='Escape'){if(!clearBusy)closeClearModal();return;}
  if(ev.key==='Tab'){
    if(clearBusy){ev.preventDefault();return;}
    var focusable=Array.prototype.slice.call(clearModal.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),[tabindex]:not([tabindex="-1"])'));
    if(!focusable.length)return;
    var first=focusable[0],last=focusable[focusable.length-1];
    if(ev.shiftKey&&document.activeElement===first){ev.preventDefault();last.focus();}
    else if(!ev.shiftKey&&document.activeElement===last){ev.preventDefault();first.focus();}
  }
});
async function delHistory(sid){
  if(!confirm('将尝试删除该条记录对应的会话目录。若目录删除失败，页面会提示错误。确定继续吗？')) return;
  try{
    var r = await fetch('/api/history/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sid:sid})});
    var j = await r.json();
    if(j.ok){ location.reload(); }
    else{ alert('删除失败：'+(j.error||'未知错误')); }
  }catch(e){ alert('请求失败：'+String(e)); }
}
function clearPlaintext(){openClearModal();}
async function confirmClearPlaintext(){
  if(clearBusy)return;
  clearBusy=true;
  var btn=document.getElementById('clear-private');
  if(btn){ btn.disabled=true; btn.textContent='清理中…'; }
  clearConfirm.disabled=true;
  clearCancel.disabled=true;
  clearModal.setAttribute('aria-busy','true');
  clearStatus.textContent='清理中，请稍候…';
  clearStatus.focus();
  clearError.hidden=true;
  try{
    var r=await fetch('/api/history/clear-private',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})});
    var j=await r.json();
    if(j.ok){ clearBusy=false; clearModal.setAttribute('aria-busy','false'); closeClearModal(); alert('已按当前规则尝试清理 '+j.sessions+' 条记录；已完成的脱敏稿保留。'); location.reload(); return; }
    throw new Error(j.error||'未知错误');
  }catch(e){
    clearBusy=false;
    clearStatus.textContent='';
    clearError.textContent='清理失败：'+String(e.message||e);
    clearError.hidden=false;
    clearModal.setAttribute('aria-busy','false');
    clearConfirm.disabled=false;
    clearCancel.disabled=false;
    clearConfirm.focus();
  }
  if(btn){ btn.disabled=false; btn.textContent='清理全部明文'; }
}
clearConfirm.addEventListener('click',confirmClearPlaintext);
</script>
"""


def _fmt_size(n) -> str:
    try:
        v = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return ("%.0f %s" % (v, unit)) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024
    return "—"


def _hist_rows(rows) -> str:
    if not rows:
        return ('<div class="hist-empty">还没有处理记录。上传一份文书完成脱敏后，'
                '这里会列出本机留存的记录。</div>')
    out = []
    for r in rows:
        sid = esc(str(r.get("sid") or ""))
        name = esc(str(r.get("filename") or "(未命名)"))
        bits = []
        if r.get("when"):
            bits.append(esc(str(r["when"])))
        if r.get("count") is not None:
            bits.append("替换 %d 处" % r["count"])
        if r.get("redacted"):
            bits.append("已脱敏")
        if r.get("restored"):
            bits.append("已还原")
        if r.get("revision_blocked"):
            bits.append("修订未处理，未提供导出件")
        if r.get("live"):
            bits.append("本次运行内")
        bits.append("会话 " + sid)
        bits.append(_fmt_size(r.get("size") or 0))
        acts = []
        if r.get("revision_blocked"):
            acts.append('<span class="pill ghost">含未处理修订，导出已拦截</span>')
        elif r.get("pdf_purged"):
            acts.append('<span class="pill ghost">原件已清理</span>')
        elif r.get("pdf_project"):
            acts.append('<a class="btn sm" href="/pdf/%s">继续处理 / 取回原件</a>' % sid)
        for label, fn in (r.get("files") or []):
            url = "/history/download/%s/%s" % (sid, urllib.parse.quote(str(fn)))
            cls = "btn sm" if label in ("脱敏稿", "还原稿") else "btn sm ghost"
            acts.append('<a class="%s" href="%s">%s</a>' % (cls, esc(url), esc(label)))
        acts.append('<button class="btn sm warn" type="button" onclick="delHistory(\'%s\')">删除</button>' % sid)
        out.append(
            '<div class="histrow">'
            '<div class="hist-main">'
            '<div class="hist-name" title="%s">%s</div>'
            '<div class="hist-sub">%s</div>'
            '</div>'
            '<div class="hist-act">%s</div>'
            '</div>' % (name, name, " · ".join(bits), "".join(acts))
        )
    return "".join(out)


def build_history_page(data: Dict[str, Any]) -> bytes:
    data = data or {}
    rows = data.get("rows") or []
    body = (HISTORY_BODY
            .replace("__COUNT__", str(data.get("count", len(rows))))
            .replace("__TOTAL__", _fmt_size(data.get("total_bytes", 0)))
            .replace("__ROWS__", _hist_rows(rows)))
    return html_doc("历史记录", body)


def build_expired_page() -> bytes:
    body = """
<div class="empty" style="padding:90px 20px">
  <div class="big">🕓</div>
  <h1 style="margin-bottom:8px">会话已失效</h1>
  <p class="muted">可能是服务重启过，之前的会话记录已清空。请重新上传文件。</p>
  <p style="margin-top:20px"><a class="btn primary lg" href="/">回到首页重新上传</a></p>
</div>
"""
    return html_doc("会话已失效", body)
