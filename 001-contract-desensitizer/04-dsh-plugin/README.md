# 文书脱敏工作台

**在 DSH 里，三步完成文书脱敏。**

合同要交给 AI 分析，诉状要发给同事讨论，证据材料要整理成案例——反复查找、替换姓名和证件号，既费时又容易漏。

文书脱敏工作台把**识别、复核、替换、还原**放进 DeepSeek Harness。上传文书，核对敏感内容，导出脱敏稿；同一案件的多份材料还能沿用同一套字段和占位符。

**Windows / macOS 均已实测 · 支持 Word、PDF、TXT、Markdown · 文书在本机处理**

[**下载 v1.3.1**](https://github.com/rainbowzhu22-eng/dsh-document-redactor/releases/tag/v1.3.1) · [工具官网](https://xingchendata.com/dsh-document-redactor.html) · [帮助与反馈](https://xingchendata.com/dsh-document-redactor.html#community)

![文书脱敏工作台界面](media/desktop-home-0.3.1.png)

## 核心功能

| 你要做的事 | 工作台怎么帮你 |
| --- | --- |
| 找出文书里的敏感信息 | 自动识别人名、证件号、联系方式、机构、地址等内容，在页面中集中核对与补充。 |
| 处理同一案件的多份文书 | 沿用同案字段与占位符，保持同一当事人在不同材料中的称呼一致。 |
| 遮盖 PDF 页面内容 | 在页面上直接框选、调整遮盖区域，处理文字、签章等需要隐藏的内容。 |
| 把脱敏后的 Word 还原 | 配合映射恢复原有字段，方便继续编辑与办理案件。 |
| 让工具适应自己的文书 | 用自然语言告诉 DSH Agent 识别需求，查看规则修改方案后应用。 |
| 接着处理以前的材料 | 从历史记录重开项目、取回原件，继续处理。 |

## 开始使用

1. 安装 [DeepSeek Harness](https://deepseek.com/harness/)，通过应用菜单“管理 dsh 命令…”注册 `dsh` 命令。
2. [下载插件安装包](https://github.com/rainbowzhu22-eng/dsh-document-redactor/releases/tag/v1.3.1)，选择 `dsh-document-redactor-1.3.1.tgz`。
3. 从菜单或托盘退出 DSH，在下载目录打开终端，执行：

```sh
dsh plugin --profile desktop add "./dsh-document-redactor-1.3.1.tgz"
```

重新打开 DSH，点击侧栏 **“文书脱敏” → “打开脱敏工具”**，即可进入工作台。Windows 与 macOS 使用同一份 TGZ 安装包。

## 从上传到导出，只要三步

1. **上传文书**：选择文件，按需要设置金额、日期等处理策略。
2. **核对与调整**：查看识别结果，补充字段；PDF 可直接框选遮盖。同案材料可复用字段设置。
3. **导出使用**：保存脱敏稿，用于 AI 分析、材料协作或案例整理。需要恢复 Word 内容时，使用映射还原。

识别规则想再贴合业务一些？在复核页点击 **“让 Agent 调整识别规则”**，描述需求、查看差异、应用方案，后续文书就能使用这套规则。

## 日常使用

- **同案材料**：在上传时选用同案设置，让多份文书延续一致的字段与占位符。
- **历史项目**：从插件顶栏进入历史记录，重开项目或取回原件。
- **调整规则**：从复核页或“识别规则”入口打开 Agent 规则助手。
- **交流反馈**：点击首页或右上角的“帮助与反馈”，查看交流群与联系入口。
- **返回 DSH**：点击“返回 DSH”，继续使用宿主中的其他功能。

## 反馈与贡献

欢迎通过 [GitHub Issues](https://github.com/rainbowzhu22-eng/dsh-document-redactor/issues) 提交使用反馈和功能建议，也可以从 [工具官网](https://xingchendata.com/dsh-document-redactor.html#community) 加入交流群。参与开发请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 来源与致谢

本项目基于 [Cece 的 cece_legal_skills](https://github.com/whatcccup/cece_legal_skills) 及 Phoeny-Xu 的协作返件开发。感谢上游作者的文书脱敏引擎与持续贡献。

**DSH 适配与维护：朱玲凤 / The Data Galaxy**。

上游与适配层 MIT 版权声明见 [LICENSE](LICENSE)；群码控制器的 Apache-2.0 来源声明见 [NOTICE](NOTICE)，许可全文见 [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt)。

## English

Redact documents without leaving DeepSeek Harness. Upload Word, PDF, TXT or Markdown files, review sensitive fields, and export a redacted copy. Keep placeholders consistent across case documents, mark PDF regions, restore Word fields with a mapping, and ask the DSH Agent to tailor detection rules. Document processing runs locally. Tested on Windows and macOS.
