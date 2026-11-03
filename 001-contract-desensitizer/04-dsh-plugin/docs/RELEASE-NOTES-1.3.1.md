# 文书脱敏 · DSH 插件 1.3.1

开发预发布。社区独立项目，不代表DeepSeek官方。适用于DeepSeek Harness；不是独立EXE或WorkBuddy技能。

本版接入协作者的Windows OCR与退出恢复、Word图片替代文字/标题等修复，并保留DSH中的沉浸页面、Agent规则助手和动态帮助入口。

修复的关键问题：

- PDF上传时保存识别规则快照，初次扫描、补词和同案同步均使用该快照；自定义规则可以作用于PDF。
- 上传后规则变化或旧项目缺少快照时，拒绝旧PDF导出，`confirm=true`不能越过；请重新上传识别。
- 编号候选默认未勾选不等于明确保留；必须先处理或明确选择保留。撤销选择或遮盖后会重新阻断。
- 保留Mac原生OCR组件的755执行权限；安装后补充OCR实际运行通过。
- 插件iframe允许显示映射下载确认框；取消不下载映射，确认后可下载并用于还原。

金额/日期的明确选择入口、原规则模板、上游许可与来源摘要保留。Windows OCR改动尚未经本维护者的Windows实机复验，不能将Mac测试结果视为Windows通过。

安装：先正常退出待安装的DSH桌面宿主，使用官方DSH提供的CLI，在TGZ所在目录执行：

```sh
dsh plugin --profile desktop add "./dsh-document-redactor-1.3.1.tgz"
```

重新启动DSH，从侧栏“文书脱敏”进入。Web profile将`desktop`改为`web`。首次准备依赖需要联网。升级不主动删除独立数据目录中的规则和项目；重要原件、映射和规则备份请自行保管。

附件仅含插件TGZ及`SHA256SUMS.txt`。Mac和Windows均使用同一个TGZ安装；Windows测试辅助ZIP留在本地，不随本次Release上传。Windows实机验收仍待完成。

验证范围：10项源码定向检查；Mac Web及真实Electron桌面均完成官方CLI隔离安装、PDF规则变化与未处置门禁、处理/明确保留后导出、撤销后重新阻断、OCR、停止重开及退出清理；桌面实际下载成功。Web另完成一份虚构DOCX的9处改写及映射还原，正文与2项可读属性恢复一致。6份单页导出PDF逐页检查完成。

发布包相对上述实装候选仅更新README和溯源状态，运行时代码不变。未做合并版全量回归、Windows实机或Word原生版式验收，本轮没有新增模型调用。Word历史列表对可读属性的替换计数尚待对齐，文件改写和还原结果已核对。

文书解析与改写在本机完成；规则助手可能向DSH配置的模型提交当前规则全文和主动填写的需求，帮助入口也需联网。映射包含原文，默认不下载，勿随脱敏稿外发。PDF导出为像素稿，无映射还原；原件从本机项目取回。自动识别可能漏项，外发前人工核对。

维护：朱玲凤 / The Data Galaxy · [源码与使用说明](https://github.com/rainbowzhu22-eng/cece_legal_skills/tree/main/001-contract-desensitizer/04-dsh-plugin) · [帮助与反馈](https://xingchendata.com/dsh-document-redactor.html#community)
