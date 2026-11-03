# 文书脱敏工作台

**在 DSH 里，三步完成文书脱敏。**

合同要交给 AI 分析，诉状要发给同事讨论，证据材料要整理成案例——反复查找、替换姓名和证件号，既费时又容易漏。

文书脱敏工作台把**识别、复核、替换、还原**放进 DeepSeek Harness。上传文书，核对敏感内容，导出脱敏稿；同一案件的多份材料还能沿用同一套字段和占位符。

**Windows / macOS 均已实测 · 支持 Word、PDF、TXT、Markdown · 文书在本机处理**

[**立即下载 v1.3.1**](https://github.com/rainbowzhu22-eng/dsh-document-redactor/releases/tag/v1.3.1) · [工具官网](https://xingchendata.com/dsh-document-redactor.html) · [插件使用说明](001-contract-desensitizer/04-dsh-plugin/README.md)

![文书脱敏工作台界面](001-contract-desensitizer/04-dsh-plugin/media/desktop-home-0.3.1.png)

## 它能帮你省下哪些重复操作

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

## 交流与反馈

使用问题、功能建议，欢迎在 [GitHub Issues](https://github.com/rainbowzhu22-eng/dsh-document-redactor/issues) 留言，或通过 [工具官网的帮助与反馈](https://xingchendata.com/dsh-document-redactor.html#community) 加入交流群。

## 来源与致谢

本项目基于 [Cece 的 cece_legal_skills](https://github.com/whatcccup/cece_legal_skills) 及 Phoeny-Xu 的协作返件开发。感谢上游作者的文书脱敏引擎与持续贡献。

**DSH 适配与维护：朱玲凤 / The Data Galaxy**。

插件源码位于 [`001-contract-desensitizer/04-dsh-plugin`](001-contract-desensitizer/04-dsh-plugin)，原工具与 SkillHub 资料继续保留在仓库中。上游与适配层采用 [MIT 许可](LICENSE)；相关来源声明见插件目录的 [NOTICE](001-contract-desensitizer/04-dsh-plugin/NOTICE)。
