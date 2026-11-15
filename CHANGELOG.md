# Changelog

版本号见 `VERSION`，`vs.py --version` 会读它。

## 0.3.0 — 2026-11-15

对照同类 skill 补齐能力，并修复复查发现的实质缺陷。

### 新增

- **网关聚合**：`fal` / `heygen` 两个 provider，一个 key 调多模型；`--model` 可传任意 fal 市场 model-id，不必等脚本更新。详见 `references/gateways.md`。
- **对话式局部改片**：`vs.py edit`，走 Gemini Omni Interactions API，支持 `--previous last` 多轮续改。
- **音频链路**：`vs.py voice`（MiniMax TTS，接进 CLI 并量真实时长）、`vs.py align`（导入外部 ASR 的 SRT/VTT 生成对齐时间轴）。此前 `post.tts()` 是死代码。
- **素材生成**：`vs.py media --kind image|music|sfx|video`，走 fal 通用队列。
- **平台交付**：`vs.py export` 多平台规格 + 水印 + 封面、`vs.py sheet` 候选并排预览、`vs.py cover`、`vs.py presets`。新增 `vslib/delivery.py`。
- **预算守护**：`--max-cost` / `project.json.max_cost` / `VS_MAX_COST`，提交前粗估拦截。
- **并发与降级**：`run --parallel` 批量提交 + 统一轮询；`run --fallback` 失败自动换备用模型。
- **画面级质检**：`vs.py qc` 生成逐镜缩略图与每镜首/末帧证据，不再只查元数据。
- **快速通道**：`plan --save-auth` / `--reuse`，只复用「上次确认过什么」的描述，不跳过任何确认。
- **工程化**：MIT LICENSE、随仓库 `tests/`、GitHub Actions CI、`--version`、费率快照日期 `RATES_AS_OF`。

### 修复

- `.vs/jobs.json` 加跨进程文件锁 + 写入时与磁盘合并，避免多终端互相覆盖导致重复扣费。
- `setup --set` 同时接受 `ARK_API_KEY=xxx` 与 `ark=xxx`（帮助文本此前写的是前者，代码只认后者）。
- `SKILL.md` 的 description 从 377 字符压到 224，元数据预算占用从 4.7% 降到 2.8%。

### 变更

- provider 从 7 家增至 9 个；`AUTO_ORDER` 网关排在最后，只配网关 key 时 `auto` 依然可用。
- 所有新增能力默认关闭，纳入 SKILL.md / intake.md 的逐项授权约束。

> 网关、Omni、素材生成的端点结构用无效密钥验证过（停在鉴权层），**未跑过真实付费生成**。

## 0.2.0 — 2026-11-15

- 新增 `README.md` 中文使用教程。
- 去掉仓库 About 描述里的 `WorkBuddy skill: ` 前缀。

## 0.1.0 — 2026-11-15

- 七家视频模型直连统一适配层。
- 完整生产管线：制作圣经、分镜、草稿预演、多候选、逐镜尾帧链、质检、后期成片。
- 多轮询问授权机制；`assemble` 默认纯拼接，字幕/配乐/片头/转场全关。
- 任务持久化与断点续跑，成本按候选/草稿倍数报账。
