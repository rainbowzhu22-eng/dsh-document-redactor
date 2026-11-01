# 文书脱敏 · DSH Document Redactor

运行在 **DeepSeek Harness（DSH）中的文书脱敏插件**。上传 Word、PDF、TXT 或 Markdown，在插件内复核敏感内容，确认后导出脱敏稿；需要扩展通用识别规则时，直接调用 DSH Agent 生成修改方案。

> 社区独立项目，不代表 DeepSeek 官方。当前 **1.3.0 为开发预发布，Windows 待实机验收**。识别可能漏项，外发前请人工检查最终文件。

**DSH 适配与维护：朱玲凤 / The Data Galaxy** · [工具官网](https://xingchendata.com/dsh-document-redactor.html) · [帮助与反馈](https://xingchendata.com/dsh-document-redactor.html#community) · [预发布下载](https://github.com/rainbowzhu22-eng/cece_legal_skills/releases/tag/v1.3.0)

![0.3.1-dev 在真实 DSH 桌面中的首页](media/desktop-home-0.3.1.png)

截图拍摄于 2026-11-01 的 macOS 桌面 0.3.1-dev，1.3.0 的界面代码未变。截图中的群码仅展示当时状态；最新有效二维码请查看插件或官网帮助区。

## 可以做什么

- **在 DSH 内完成处理**：进入“文书脱敏”后收起宿主侧栏，沿“上传→复核→导出”操作，历史记录从插件顶栏打开。
- **复核再导出**：增补漏识别字段；PDF 支持逐页框选遮盖，检出的待处置项须处理或明确保留。规则变更后重新上传识别。
- **按需改规则**：在复核页点击“让 Agent 调整识别规则”，描述需求、查看差异、确认应用；保留原规则备份。
- **保留本地项目**：历史可重开、取回原件。文字文书可按工具支持范围结合映射还原；PDF 导出不提供映射还原。
- **动态帮助群码**：首页与“帮助与反馈”读取线上配置；过期、暂停、满员或加载失败时隐藏旧码，换码无需重装。

## 安装到 DSH

先安装[官方 DSH](https://deepseek.com/harness/)。本插件不是独立 EXE，也不是 WorkBuddy 技能；不要把旧版独立工具安装说明用于本插件。

**桌面版**：至少打开一次官方 DSH，通过应用菜单“管理 dsh 命令…”注册它提供的 CLI，再从菜单或系统托盘正常退出 DSH，打开新终端安装。全局 npm/npx CLI 可能拒绝管理桌面配置，请使用官方桌面 CLI。参见[官方桌面指引](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.zh.md)。

从本项目 Release 下载 TGZ 后，在其所在目录执行：

```sh
dsh plugin --profile desktop add "./dsh-document-redactor-1.3.0.tgz"
```

预定标签安装方式（发布标签后可用，当前尚未做该 Git 传输路径的实装验收）：

```sh
dsh plugin --profile desktop add "github:rainbowzhu22-eng/cece_legal_skills#v1.3.0"
```

**Web profile** 同样是 DSH 插件宿主，可将上面的 `desktop` 改为 `web`，在对应宿主启动后使用。本插件不要求另开浏览器完成桌面文书流程。

重新启动 DSH，进入侧栏“文书脱敏”，点击“打开脱敏工具”。首次安装/准备依赖需要联网；由插件建立独立 Python 环境，代码最低要求 Python 3.9。较新 Python、Windows 与 Intel 的依赖组合仍需实测。准备失败时记录页面错误和本插件的 `environment.log`，不要删除整个 DSH 数据目录。

Windows 测试请使用 Release 中的测试 ZIP：完整解压后先读 `01-README.html`，内含安装辅助、5 份虚构样本及反馈模板。**测试包不等于 Windows 已验收通过。**

## 正常使用与联网边界

1. 上传需要处理的文书，选择金额/日期等处理策略。
2. 核对候选、补充遗漏；PDF 请逐页检查签章、图片文字及遮盖范围。
3. 处理或明确保留所有已检出的待处置项后导出，再检查最终文件。

文书解析与改写在本机执行，文书服务监听 `127.0.0.1`；这不代表整个 DSH 宿主都被禁止联网。首次依赖安装、群码和官网访问需要联网。

Agent 规则助手会提交**当前规则全文与主动填写的需求**给 DSH 配置的模型；云端模型需联网，可能产生模型费用。上传文书、候选原文和映射不会自动附带；用户写入规则或需求的内容仍会提交。请使用虚构示例。助手不开放文件、终端或其他工具能力，应用前校验方案并检查并发规则修改。

默认数据位于 `DSH_HOME/document-redactor/`，未设置 `DSH_HOME` 时为用户主目录下 `.dsh/document-redactor/`；可由 `DSH_REDACTOR_DATA_DIR` 改为指定位置。升级插件不主动删除项目。清理历史不涵盖原文件、下载文件、规则备份、DSH 会话或模型服务侧记录；删除不等于安全擦除。

## 验证状态与格式限制

- 已有 macOS Apple Silicon、DSH 0.2.1-rc.2 的 Web/真实桌面既定功能及本轮帮助入口定向证据。
- 当前 TGZ：408466 字节，38 文件，SHA-256 `af508a71b0b5e8f9d0bf150da5e6fc2b69c3c1d2556907b27d6f7ff75473b204`。Windows 转发包使用此 TGZ；1.3.0 仅版本与说明更新，运行时代码与已验 0.3.1-dev 一致。
- **Windows、Intel、新机器首次联网依赖组合及真实文书总体准确率仍未由这些证据证明。** Windows OCR 的 RapidOCR/系统 OCR 路径待实机验证。
- 普通文本框、符合简单结构的 VML 文字水印、简单内容控件和受支持隐藏文字纳入扫描；备用文本框副本、复杂水印、脚注/尾注、字段代码、图片文字与部分修订仍需人工核对。
- PDF 输出将页面栅格化，不保留文本层，不能使用映射还原；批注等剩余内容仍须按页面提示核对。

本目录是独立 DSH bundle；仓库根另设指向本目录的安装清单，保留原离线工具及 SkillHub 结构。运行时代码与预发布 TGZ 相同；本 README 为公开说明，包内原说明另存 [docs/PACKAGED-README-1.3.0.md](docs/PACKAGED-README-1.3.0.md)。制品记录见 [docs/ARTIFACT.json](docs/ARTIFACT.json)。

公开检查来自原项目的适配、规则和群码测试，仅调整到本仓库根目录的相对路径：

```sh
node --test test/adapter.test.mjs test/interaction.test.mjs test/community.test.mjs
```

其中真实本机服务检查需要已安装 Python 依赖；缺依赖应记录未覆盖，不能将跳过视为通过。不需要模型凭据，Agent 契约检查使用替身。网站部署、私有日志和本地验收目录不随源码公开。

## 反馈、贡献与许可

请通过 [GitHub Issues](https://github.com/rainbowzhu22-eng/cece_legal_skills/issues) 或[官网页末帮助](https://xingchendata.com/dsh-document-redactor.html#community)反馈，附系统、DSH/Python 版本、最短步骤、错误与脱敏截图。只提交虚构样本，不提交真实客户文书、密钥或映射文件。贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

R99 脱敏引擎来源于 `whatcccup/cece_legal_skills` 及 Phoeny-Xu 的协作返件；上游与适配层现有 MIT 版权声明保留于 [LICENSE](LICENSE)。群码控制器的现有 Apache-2.0 来源声明见 [NOTICE](NOTICE)，许可全文见 [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt)。DSH 宿主单独安装，不包含在本包中。

## English

A **DeepSeek Harness plugin**, not a standalone desktop application. Review and redact DOCX, PDF, TXT and Markdown inside DSH. Use the configured DSH Agent to propose rule edits, inspect the diff and apply it. Document processing runs locally; rule generation can send the current rules and typed request to the configured model. Dependencies and dynamic help QR codes need network access.

Version **1.3.0 is a development prerelease**. Existing evidence covers specified macOS Apple Silicon Web/Desktop flows; **Windows acceptance is pending**. Use fictional samples, review outputs manually, and report compatibility problems through Issues.
