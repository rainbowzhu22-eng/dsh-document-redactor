"""DSH presentation adapter. Keep the frozen R99 engine and export gates intact."""
from pathlib import Path
import re


DATA_AND_AI = """<h2>数据处理与 AI 使用</h2>
<ul class="tlist">
  <li><b>文书处理：</b>识别、脱敏与还原由运行插件设备上的本机引擎完成，复核由您在页面中确认；不会自动把上传文书、候选原文或映射交给规则助手。这不代表整个 DSH 宿主处于断网或网络隔离状态。</li>
  <li><b>AI 改规则：</b>点击生成方案后，会将当前规则全文和您填写的需求提交给 DSH 配置的模型；使用云端模型时需要联网。您写进规则或需求中的内容也会提交给该模型，请使用虚构示例。</li>
  <li><b>帮助与反馈：</b>首页或帮助中的群码可见时，会从官网读取最新配置和图片，不附带文书、规则、映射或对话内容；离开对应页面或隐藏后停止该处自动读取。</li>
  <li><b>访问范围：</b>文书处理服务绑定本机回环地址 <code>127.0.0.1</code>。DSH 宿主的远程访问、代理与访问权限另由宿主配置决定，不能据此保证整个宿主仅本机可访问。</li>
  <li><b>本机记录：</b>停止工具不会删除已保存项目。历史记录中的清理范围以该页说明为准；PDF 处理中会拒绝相应清理，删除也不等于安全擦除。电脑原位置的源文件、已下载文件、规则与备份、DSH Agent 对话需分别管理；模型服务侧可能保留的记录不由本机项目清理删除。</li>
</ul>
<details class="dsh-support-scope">
  <summary>Word 特殊对象与人工复核范围</summary>
  <p>普通文本框、符合简单结构的 VML 文字水印、简单内容控件和受支持隐藏文字纳入扫描。“文本框”也包括矩形、箭头等绘图形状里的文字。兼容备用文本框副本（如 VML 与 DrawingML）、复杂水印、脚注和尾注、字段代码、Word 内图片文字及未受支持的修订仍需按提示人工核对。</p>
  <p>规则助手用于调整识别规则，不会扩展 Word 解析器的结构支持范围；扫描 PDF 的识别能力也不代表 Word 内图片文字已被覆盖。</p>
</details>"""


def reword_boundaries(body):
    """Correct plugin disclosures while leaving the frozen R99 vendor untouched."""
    if '<form id="upload-form">' in body:
        body = re.sub(r'<h2>🛡 安全边界</h2>\s*<ul class="tlist">.*?</ul>',
                      lambda _match: DATA_AND_AI, body, count=1, flags=re.S)
        body = re.sub(r'<details\b[^>]*>\s*<summary>想修改字段识别规则？.*?</details>',
                      '', body, count=1, flags=re.S)
        body = body.replace('<span class="eyebrow">安装完成后文书处理不联网</span>',
                            '<span class="eyebrow">文书解析与改写在本机进行</span>')
    body = body.replace(
        '首次安装依赖需要联网，安装完成后文书处理不联网；所有解析与改写均在本机完成，文件不外传',
        '首次准备依赖需要联网；文书解析与改写在本机进行，使用云端模型改规则时需要联网')
    body = body.replace(
        '首次安装依赖需要联网，安装完成后文书处理不联网 · 文件不离开本机',
        '首次准备依赖需要联网；文书解析与改写在本机进行，使用云端模型改规则时需要联网')
    if '<h1>历史记录</h1>' in body:
        body = body.replace('全部解析均在本机完成，文件不外传', '文书解析与改写在本机进行')
        body = body.replace(
            '<b>这些记录只存在本机</b>：原件、映射与输出都在本地会话目录里，没有上传到任何服务器。含明文的记录可单独删除，删除后不可恢复。',
            '<b>这些是本机项目记录</b>：文件保存在本工具的数据目录，常规文书处理不会自动将其附带给规则助手。规则与备份、DSH Agent 对话及模型服务侧可能保留的记录需分别管理，不会随本页项目清理一并删除。')
    return body


def install(server):
    base = Path(__file__).resolve().parent
    css = (base / 'workbench.css').read_text(encoding='utf-8')
    script = (base / 'workbench.js').read_text(encoding='utf-8')
    # Share the exact host controller with the local upload page; one maintained policy.
    client = (base / 'client.js').read_text(encoding='utf-8')
    community = client.split('// COMMUNITY-CORE-BEGIN', 1)[1].split('// COMMUNITY-CORE-END', 1)[0]
    script = script.replace("'use strict';", "'use strict';\n" + community, 1)
    original = server.AppHandler._send_html

    def send_html(self, body, code=200):
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        body = reword_boundaries(body)
        body = body.replace('</head>', '<style>' + css + '</style></head>')
        body = body.replace('</body>', '<script>' + script + '</script></body>')
        return original(self, body, code)

    server.AppHandler._send_html = send_html
