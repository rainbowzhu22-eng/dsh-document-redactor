# 文书脱敏工作台

**在 DSH 里，三步完成文书脱敏。**

合同要交给 AI 分析，诉状要发给同事讨论，证据材料要整理成案例——反复查找、替换姓名和证件号，既费时又容易漏。

文书脱敏工作台把**识别、复核、替换、还原**放进 DeepSeek Harness。上传文书，核对敏感内容，导出脱敏稿；同一案件的多份材料还能沿用同一套字段和占位符。

**支持 Word、PDF、TXT、Markdown · 文书在本机处理**

1.4.0 已完成 macOS Web／DSH 桌面验证。旧版 Windows 实测结果继续保留；本版新增安装／OCR路径仍待 Windows 现场复验。

[**立即下载 v1.4.0**](https://github.com/rainbowzhu22-eng/dsh-document-redactor/releases/tag/v1.4.0) · [工具官网](https://xingchendata.com/dsh-document-redactor.html) · [插件使用说明](001-contract-desensitizer/04-dsh-plugin/README.md)

![文书脱敏工作台界面](001-contract-desensitizer/04-dsh-plugin/media/desktop-home-0.3.1.png)

## 1.4.0 更新

- 首次准备显示阶段，可选择下载源；失败后重试保留已安装依赖与缓存。
- 依赖清单明确 UTF-8 编码，Windows 能力检查覆盖 OCR 组件和模型。
- 可渲染的结构异常 PDF 不再因原件链接统计失败而阻断导入。
- 规则方案可以放弃，也可预览并确认回退到某次应用前的完整规则。
- PDF 可展开全部缓存 OCR 行，点击定位，并单独记录人工核对。

[发布说明](001-contract-desensitizer/04-dsh-plugin/docs/RELEASE-NOTES-1.4.0.md) · [安装与 Windows 核验步骤](001-contract-desensitizer/04-dsh-plugin/docs/INSTALLATION-1.4.0.md)。

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

1. 安装并打开 [DeepSeek Harness](https://deepseek.com/harness/)。
2. [下载插件安装包](https://github.com/rainbowzhu22-eng/dsh-document-redactor/releases/tag/v1.4.0)，选择 `dsh-document-redactor-1.4.0.tgz`。
3. 在 DSH 侧栏选择 **“插件” → “添加插件”**，填入下载文件的**完整路径**，点击安装，再按提示启用或重启。

**如何取得完整路径：**

- **Windows**：在资源管理器找到下载的 TGZ，按住 Shift 右键点击文件，选择“复制文件地址”（部分版本显示“复制为路径”）。粘贴到 DSH 输入框后，**删掉最前和最后的双引号**，保留中间的完整路径及空格。例如：`C:\Users\你的用户名\Downloads\dsh-document-redactor-1.4.0.tgz`。
- **macOS**：在 Finder 选中文件，按 `Option + Command + C` 复制路径，再粘贴到 DSH。例如：`/Users/你的用户名/Downloads/dsh-document-redactor-1.4.0.tgz`。

示例中的路径仅作格式参考，请使用自己电脑复制出的实际路径。TGZ 无需解压。**只填 `dsh-document-redactor-1.4.0.tgz` 会被当作在线包名查询**，可能出现 `ERR_PNPM_INVALID_PACKAGE_NAME`；改为完整路径即可，切换镜像源不能修正路径。

如需通过终端安装，先通过应用菜单“管理 dsh 命令…”注册 `dsh` 命令，再从菜单或托盘退出 DSH，在下载目录打开终端，执行：

```sh
dsh plugin --profile desktop add "./dsh-document-redactor-1.4.0.tgz"
```

安装并启用后，点击侧栏 **“文书脱敏” → “打开脱敏工具”**，即可进入工作台。Windows 与 macOS 使用同一份 TGZ 安装包。终端命令中的双引号用于保护路径；桌面安装输入框中请勿保留外层双引号。

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
