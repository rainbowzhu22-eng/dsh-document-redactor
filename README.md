> **本 fork 新增：文书脱敏 DSH 插件 1.3.1（预发布）**
>
> [插件源码与使用说明](001-contract-desensitizer/04-dsh-plugin/README.md) · [DSH 插件安装包](https://github.com/rainbowzhu22-eng/cece_legal_skills/releases/tag/v1.3.1) · [工具官网](https://xingchendata.com/dsh-document-redactor.html)
>
> 这是安装在 **DeepSeek Harness 中的插件**，在插件内上传、复核、PDF 框选、导出、历史恢复，并可调用 Agent 改规则。**Windows 待实机验收**。文书在本机处理；首次依赖、群码与云端规则助手会联网，后者提交当前规则全文和手填需求，不自动附带文书或映射。
>
> 根 `package.json` 是指向 `001-contract-desensitizer/04-dsh-plugin` 的 DSH 安装入口。原离线工具及 SkillHub 文件保持；下方原有“完全离线”等描述仅适用于原工具，不涵盖新增插件的 Agent 与帮助联网功能。上游作者及许可保留，DSH 适配与维护：朱玲凤 / The Data Galaxy。
>
> 桌面版先通过官方 DSH 的“管理 dsh 命令…”注册其 CLI，并从菜单/托盘正常退出宿主，再执行：
>
> `dsh plugin --profile desktop add "github:rainbowzhu22-eng/cece_legal_skills#v1.3.1"`
>
> 优先使用Release的TGZ。1.3.1接入Windows OCR/Word修复，补回PDF规则快照与导出门禁，并修正插件内映射确认；运行时与Mac双端定向实装候选一致。Windows实机与合并版全量回归尚未覆盖。Git标签安装路径尚未单独实装；Web profile将desktop改为web。

---

# cece_legal_skills

法务 / 法律场景的**离线小工具**集合：不联网、不上传、可逆，装好就能用。

<p align="center">
  <img src="001-contract-desensitizer/01-source/docs/images/03-左右对比.png" alt="合同脱敏前后对比" width="760">
</p>

**共同原则**：完全离线（不调大模型、不产生外联流量）· 结果可复现（靠格式规则与国标校验位，同样输入永远同样输出）· 不折腾就能用（可一键安装的 AI Skill，或双击启动器）。

---

## 30 秒上手

### 方案 A（推荐）：装成 AI Skill，环境都不用管

把这句话发给你的 AI Agent（WorkBuddy / Claude Code / Cursor / Codex 都行）：

> 请用 SkillHub CLI 安装技能 `contract-desensitizer-offline`（`skillhub install contract-desensitizer-offline --namespace user_47430f88`，`--dir` 指向你自己的 skills 目录），安装成功后确认你会用它做合同脱敏：装依赖、启动本地服务、告诉我访问地址，全程不要修改技能里的源码。

手动也行：

```bash
curl -fsSL https://skillhub.cn/install/install.sh | bash -s -- --cli-only   # 装 CLI（仅一次）
export PATH="$HOME/.local/bin:$PATH"
skillhub install contract-desensitizer-offline --namespace user_47430f88 --dir ~/.workbuddy/skills
```

`--dir` 必须指向客户端自己的 skills 目录：WorkBuddy `~/.workbuddy/skills`、Claude Code `~/.claude/skills`、Cursor `~/.cursor/skills`、Codex `~/.codex/skills`。默认会装到当前目录的 `./skills/`，客户端识别不到。

### 方案 B：GitHub 完整版（带双击图标）

```bash
git clone https://github.com/whatcccup/cece_legal_skills.git
cd cece_legal_skills/001-contract-desensitizer/01-source
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

之后双击启动器即可（macOS `launcher/mac/Contract_Redactor.app`，Windows `launcher/windows/Contract_Redactor.vbs`），
浏览器打开 <http://127.0.0.1:18800/>。

> 方案 A 是轻量技能包（128 KB，不含图标与截图，跨平台启动器是 `start.py`）；
> 方案 B 是完整工程（含 `.app` / `.vbs` 图标、截图、回归测试）。**引擎是同一套**，选哪个都行。

完整教程：**[001-contract-desensitizer/01-source/README.md](001-contract-desensitizer/01-source/README.md)**

---

## 技能索引

| 编号 | 技能 | 一句话定位 | GitHub | SkillHub |
|---|---|---|---|---|
| [001](001-contract-desensitizer/) | 合同脱敏 / 还原 | 把合同敏感信息换成可逆占位符，需要时一键还原 | ✅ 本仓库 | [contract-desensitizer-offline](https://skillhub.cn/skills/user_47430f88/contract-desensitizer-offline) |

---

## 仓库里有什么

```
cece_legal_skills/
├── README.md                 ← 本文件（落地页）
├── LICENSE                   ← MIT
├── templates/                ← 新技能 README 模板
└── 001-contract-desensitizer/
    ├── README.md             ← 技能卡片（定位 / 快速开始 / 能力边界 / 目录地图）
    ├── 01-source/            ← 【唯一真源】4 个引擎 + 规则清单 + 启动器 + 截图 + 测试 + 完整教程
    ├── 02-github/            ← CHANGELOG / RELEASE-CHECKLIST
    ├── 03-skillhub/          ← 上传 SkillHub 的发布形态（SKILL.md + scripts + references）
    ├── 90-build/             ← build.sh / publish_to_skillhub.sh（生成的 zip 不入库）
    └── 99-temp/              ← 会话、报告、脱敏稿（含真实敏感信息，不入库）
```

**这个仓库的用处**：它是源码真源 + 完整版分发渠道 + 版本历史。
`01-source/` 不是"藏起来"，它就是这个工具本身（含图标、截图、测试、完整教程），
只是 SkillHub 那边另有一份**由它同步出来的轻量副本**（`03-skillhub/`，不能手改，由 `90-build/build.sh` 生成）。

<details>
<summary><b>维护者专区</b>（目录规范 / README 规范 / 发版流程 / 上架硬约束）—— 普通使用者不用展开</summary>

### 目录规范

一个技能 = 一个编号文件夹。编号从 `001` 起，**只增不复用、不重排**；已发布的技能即使改名也保留原号。

| 目录 | 性质 | 装什么 | 是否入库 |
|---|---|---|---|
| `01-source/` | 定稿·**唯一真源** | 完整可运行工程：源码、规则、启动器、截图、测试、完整教程 | ✅ |
| `02-github/` | 定稿·发布物料 | `CHANGELOG.md`、`RELEASE-CHECKLIST.md` 等只在 GitHub 侧用的物料 | ✅ |
| `03-skillhub/` | 定稿·发布形态 | `SKILL.md` + `scripts/` + `references/` + `tests/` | ✅ |
| `90-build/` | 构建产物 | 构建 / 发布脚本 + 生成的 zip | 脚本 ✅，产物 ❌ |
| `99-temp/` | 临时 | 运行会话、识别报告、脱敏稿、mapping、测试样本 | ❌（含敏感信息） |

**关联与边界**

- 源码只有一个真源 `01-source/`；`03-skillhub/scripts/` 由 `build.sh` 同步，**禁止手改**，否则下次构建被覆盖。
- 技能包里手写的只有两样：`SKILL.md`、`scripts/launcher/start.py`（`.bat`/`.vbs`/`.sh` 会被上架拦截，只能用 `.py`）。
- GitHub 发布内容 = `README.md` + `01-source/` + `02-github/`；SkillHub 发布内容 = `03-skillhub/`。
- `.gitignore` 里带斜杠的模式只匹配仓库根，多技能必须写 `**/90-build/*.zip`、`**/99-temp/*`。

### SkillHub 上架硬约束（踩过坑）

- 技能包里**不能出现** `.bat` / `.vbs` / `.sh` / `.exe`，否则报 `400 不允许的文件类型`。
- `SKILL.md` frontmatter 必须有：`slug`（kebab-case，全网唯一）、`version`（三段 SemVer）、`displayName`。
- 同一 skillId 可重复 publish 新版本号，`tags.latest` 会自动跟到新版，不需要先撤销旧版。
- 推送 GitHub 若报 `CONNECT tunnel failed 502`，是本机代理挂了，绕开代理即可：
  `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy git push origin HEAD`

### README 规范

每个技能必须有 `README.md`（技能卡片），六段，**顺序与小标题固定**，缺失写「待补」不要删：

| 段 | 标题 | 写什么 |
|---|---|---|
| 1 | 一句话定位 | 用户角色 + 场景 + 结果，两行以内 |
| 2 | 快速开始 | 推荐安装方式的一句话 Prompt（可直接复制）+ 一条手动命令 |
| 3 | 能力边界 | 支持 / 不支持 / 已知限制 / 合规红线，用表格 |
| 4 | 目录地图 | 本技能五个目录各装什么 |
| 5 | 维护说明 | 改哪里、怎么自检、怎么发两个版本、什么是禁止操作 |
| 6 | 许可与来源 | 许可、仓库地址、SkillHub 地址 |

模板：[templates/skill-README.md](templates/skill-README.md)

### 发版流程

```bash
# 1) 改代码：只动 01-source/
# 2) 一键同步 + 自检 + 打包 + 预检
bash 001-contract-desensitizer/90-build/build.sh

# 3) 发 GitHub 版（本仓库即 GitHub 仓库）
git add -A && git commit -m "..." && git push

# 4) 发 SkillHub 版：先改 03-skillhub/SKILL.md 的 version（SemVer），再发布
skillhub publish 001-contract-desensitizer/03-skillhub --changelog "..."
```

发版前按 `001-contract-desensitizer/02-github/RELEASE-CHECKLIST.md` 逐条打勾。

</details>

---

## 许可

[MIT](LICENSE) © 2026 whatcccup
