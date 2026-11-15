# video-studio

把「想做一条视频」变成「跑几条命令」。七家视频生成模型的接口被抹平成一套参数，再配上从分镜、预演、多候选到成片的完整生产管线。

**最省事的用法其实不用敲命令**：装好技能后，直接对 Agent 说「帮我做一个介绍 XX 的 30 秒竖屏视频」。它会分几轮问清需求、要不要剪辑、要不要字幕配乐，报出预估花费，等你点头才生成。

---

## 一、它解决什么

| 做 AI 视频常见的坑 | 这里的处理 |
|---|---|
| 每家 API 参数、鉴权、轮询都不一样 | 统一成 `--provider / --duration / --ratio / --resolution`，换模型不改分镜 |
| 多镜头里人物换脸、风格断裂 | 制作圣经逐字复用身份块、全项目锁定同一个模型、seed 与参考图全部落盘 |
| 各家片段分辨率/帧率/音轨不同，拼不起来 | `assemble` 先归一化再拼接 |
| 字幕和画面对不上 | 字幕轴按实际片段时长计算，并补偿转场吃掉的重叠时长 |
| 生成到一半断了要重来还重复扣费 | `.vs/jobs.json` 记录所有任务，`resume` 接着等 |
| 没问就自动加字幕、配乐、剪成一条 | **默认全关**，每项都要用户单独确认 |

---

## 二、支持的模型

| `--provider` | 对应服务 | 环境变量 | 参考单价（元/秒） |
|---|---|---|---|
| `ark` | 即梦 Seedance / 火山方舟 | `ARK_API_KEY` | 0.26 – 0.40 |
| `kling` | 可灵 Kling | `KLING_API_KEY` | 0.35 – 0.55 |
| `minimax` | 海螺 MiniMax | `MINIMAX_API_KEY` | 0.25 – 0.50 |
| `vidu` | Vidu | `VIDU_API_KEY` | 0.12 – 0.25 |
| `runway` | Runway Gen-4 / Gen-4.5 | `RUNWAY_API_KEY` | 0.36 – 0.86 |
| `veo` | Google Veo 3.1 | `GEMINI_API_KEY` | 1.20 – 2.90 |
| `luma` | Luma Ray | `LUMA_API_KEY` | 0.30 – 0.60 |
| `fal` | fal.ai 网关（一个 key 调市场里十余模型） | `FAL_KEY` | 各模型不同 |
| `heygen` | HeyGen 网关（Veo / Kling / Sora / Runway / Seedance） | `HEYGEN_API_KEY` | 按工作流计 |

不写 `--provider` 就是 `auto`：自动挑有密钥、且能力匹配（文生视频 / 图生视频 / 首尾帧）的那家。价格是粗估，实际以各家控制台账单为准，跑 `vs.py models` 可以看模型清单与快照日期。

**网关型**用 `--model` 传 fal 市场里的任意 model-id，Sora、Wan、PixVerse、Seedance 2.0 等不必等脚本更新。详见 `references/gateways.md`。注意：网关端点只验过结构，**没跑过真实付费生成**。

**新手建议从 `ark`（火山方舟）开始**：国内直连、按秒计费、有免费额度、中文提示词友好。

---

## 三、安装

技能要放在用户级技能目录下，所有工作区都能用：

```bash
git clone https://github.com/cyb101003/video-studio.git ~/.workbuddy/skills/video-studio
```

Windows 是 `%USERPROFILE%\.workbuddy\skills\video-studio`。已经装过的话直接 `git pull` 即可。

> 装完重启一下 WorkBuddy，技能才会出现在技能列表里。

教程里为了简短，用 `vs` 代表完整路径：

```bash
alias vs='python3 ~/.workbuddy/skills/video-studio/scripts/vs.py'
```

不想设 alias，就把下面所有 `vs` 换成 `python3 ~/.workbuddy/skills/video-studio/scripts/vs.py`。

---

## 四、环境准备

| 依赖 | 是否必需 | 说明 |
|---|---|---|
| Python 3.10+ | 必需 | 只用标准库，不需要 `pip install` |
| ffmpeg / ffprobe | **后期才需要** | 只生成片段的话不用装；拼接、字幕、配乐、抽帧、粗剪都要它 |
| 至少一家 API Key | 必需 | 见下一节 |

```bash
# macOS
brew install ffmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
```

先自检：

```bash
vs doctor
```

会列出 Python、ffmpeg、字体、已配置的密钥，以及制作圣经和模型锁定状态。**这个命令只是自检，不代表可以开工。**

---

## 五、配置密钥

先看各家申请地址和价格参考：

```bash
vs setup
```

写入密钥（在项目目录下执行，会写到当前项目的 `.env`，该文件已被 `.gitignore` 排除）：

```bash
# 两种写法都可以，环境变量名或 provider 名
vs setup --set ARK_API_KEY=你的密钥
vs setup --set ark=你的密钥
```

想让所有项目共用一份，写到全局配置 `~/.workbuddy/video-studio.env`，或直接导出环境变量：

```bash
export ARK_API_KEY=你的密钥
```

九家（含两个网关）的环境变量名见上面第二节的表格。**密钥只存在本地，不要提交到仓库。**

预算可以设一道硬刹车，三选一：`--max-cost 20`、`project.json` 里写 `"max_cost": 20`、或环境变量 `VS_MAX_COST=20`。提交前按公开单价粗估，超过就拦截。**它是量级刹车，不是账单。**

---

## 六、五分钟走一遍（手动模式）

以下都在**你自己的项目目录**里跑，不要在技能目录里跑。

### 1. 建项目

```bash
mkdir my-video && cd my-video
vs init "咖啡店宣传片" --shots 4 --ratio 9:16
```

生成 `storyboard.json`（分镜，唯一真相源）、`project.json`、`.env`、`.gitignore`。

### 2. 写分镜

打开 `storyboard.json`，给每镜填 `prompt`（画面描述）和 `duration`（秒）。一镜建议 3–8 秒：

```json
{
  "title": "咖啡店宣传片",
  "ratio": "9:16",
  "resolution": "1080p",
  "style": "温暖治愈，自然光，浅景深",
  "shots": [
    { "id": "s1", "desc": "推门", "duration": 5, "prompt": "清晨，她推开咖啡店玻璃门，风铃轻响，镜头缓慢前推" },
    { "id": "s2", "desc": "倒咖啡", "duration": 5, "prompt": "吧台特写，牛奶倒入浓缩咖啡形成拉花，俯拍微距" }
  ]
}
```

可选字段：`image`（首帧图）、`last_image`（尾帧图）、`subtitle` / `voiceover`（字幕或旁白文案）、`negative`（反向提示词）。

### 3. 先看成本

```bash
vs estimate              # 基础版
vs estimate --takes 3 --draft   # 如果还要 3 个候选 + 一轮草稿，按 ×3.5 报账
```

生成会花真钱且不可撤销，**这一步别跳**。

### 4. 打印开工确认单

```bash
vs plan                  # 只交付片段
vs plan --assemble      # 要剪成一条片
```

确认单会逐项列出「做 / 不做」，拿去跟需求方核一遍。`plan --assemble` 可以带上已确认的后期参数（见第七节）。

### 5. 生成片段

```bash
vs run
```

默认逐镜提交并等待，约 1–3 分钟/镜。整批锁定同一个模型，身份块自动拼进每个 prompt，任务记录全部落盘。

中断了不用重来：

```bash
vs resume                # 接着等已提交的任务，不会重复扣费
vs list                  # 看所有任务状态
vs run --only s2 --force # 只重跑第 2 镜
```

### 6. 交付

```bash
vs clips                 # 只要片段：列出 videos/*.mp4，逐个交付
vs assemble             # 要成片：拼成 1 个 mp4（默认纯拼接）
```

`assemble` 不带任何参数 = 只做规格归一化 + 硬切，成片在 `output/`。

### 7. 质检

```bash
vs qc
```

自动查片段缺失、模型/seed 混用、字幕超长、参考图齐全，另外给一份需要人眼过目的清单。

---

## 七、默认约定：不会替你做决定

这是这个技能最重要的一条，用命令行时同样生效：

- **不会默认剪辑**：`assemble` 必须你主动调用；不调用就只有片段。
- **不会默认加任何东西**：字幕、配乐、配音、模型原生音效、片头、转场、淡入淡出，全部默认关闭。分镜里写了 `bgm` / `transition` 也不会自动生效。
- **「没回答」等于「不做」**：`--subtitles`、`--bgm`、`--title`、`--transition`、`--fade`、`--audio` 都要显式给。

只在用户逐项确认后才这样开：

```bash
vs assemble --subtitles --bgm audio/bgm.mp3 --title "咖啡店" --transition fade --fade
```

音乐文件需要你自己准备，不会自动去找。确实逐项核对过确认单、想沿用分镜里的设置时，才用 `--use-storyboard`。

---

## 八、命令速查

| 命令 | 作用 |
|---|---|
| `doctor` | 环境、密钥、模型锁定状态自检 |
| `models` | 列出各家可用模型与参考价 |
| `setup` / `setup --set ARK_API_KEY=xxx` | 密钥指引 / 写入密钥 |
| `init "项目名" --shots 4 --ratio 9:16` | 建项目骨架 |
| `gen -p "描述" -d 5` | 只生成单个片段 |
| `gen -p "描述" -i images/first.png` | 图生视频（首帧图） |
| `run` | 按分镜批量生成 |
| `run --only s2,s4` / `--force` | 只跑指定镜头 / 强制重跑 |
| `run --provider kling` | 指定模型（项目已锁定时会被拦下） |
| `list` / `status` / `fetch` / `resume` | 任务管理、断点续跑 |
| `estimate --takes 3 --draft` | 按实际生成量报账 |
| `plan` / `plan --assemble` | 打印开工确认单 |
| `clips` / `clips --json` | 只交付片段时列清单 |
| `assemble` | 合成成片（默认纯拼接） |
| `bible --set identity='...' seed=12345 --lock` | 制作圣经：锁角色、风格、参考图、seed |
| `run --stage draft` / `animatic` | 720p 便宜草稿 + 粗剪看节奏 |
| `run --takes 3` / `takes` / `pick s1 B` | 每镜 A/B/C 候选、挑版（不重新生成、不花钱） |
| `run --chain` | **推荐**：边生成边把上一镜末帧接成下一镜首帧 |
| `chain` / `chain --apply` | 尾帧链补救（只预览 / 真写入，写入后须 `--force` 重跑后续镜头） |
| `qc` | 自动质检 + **逐镜缩略图与首末帧证据** + 人工清单 |
| `run --parallel N` | 批量提交 + 统一轮询，多镜提速（不与 `--chain` / `--takes` 同用） |
| `run --fallback` | 失败自动换备用模型（会产生新任务，须先确认） |
| `run --max-cost 20` | 整批成本硬上限 |
| `voice --text "..."` | 中文配音 + 量真实时长（MiniMax TTS） |
| `align --srt x.srt` | 导入外部 ASR 转录，生成对齐时间轴 |
| `media --kind image --prompt "..."` | 生成静帧 / 音乐 / 音效 / 视频素材 |
| `edit --video x.mp4 --prompt "..."` | 对话式局部改片，`--previous last` 多轮续改 |
| `export --presets douyin,bilibili --cover` | 多平台规格导出 + 封面 |
| `sheet` / `cover` / `presets` | 候选并排预览 / 抽封面 / 列平台预设 |

---

## 九、进阶管线（可选）

下面这些能显著提高多镜头一致性，**但都会多花钱或改分镜，用之前先跟用户确认**：

1. **制作圣经** — `vs bible --set identity='28岁女性，齐肩深棕发，右眼下方一颗痣，深绿色高领针织衫' --set lighting='侧逆光' --set seed=12345 --set references='refs/hero-front.png' --lock`
   身份块会**逐字**拼到每一镜的 prompt 最前面；整批只解析一次模型并写进 `.vs/model-lock.json`，防止中途换模型导致风格漂移。
2. **静帧先行** — 先用生图工具定好每镜构图、服装、光线，把图填进 `image` 字段走图生视频。改静帧几秒，改视频几分钟加真钱。
3. **草稿预演** — `vs run --stage draft` 出 720p 便宜草稿，`vs animatic` 拼成粗剪（在 `.vs/work/`，不污染交付目录）。草稿不写回分镜、不交付。
4. **多候选** — `vs run --takes 3` 每镜出 A/B/C，`vs takes` 看清单，`vs pick s1 B` 挑版。版本永不覆盖，选版不重新生成。
5. **尾帧链** — 用 `vs run --chain` 在生成过程中完成衔接；`chain --apply` 只是定稿后的补救手段，改完分镜必须重跑后续镜头才生效。需要 ffmpeg。
6. **复现** — 每个任务的 prompt / seed / 模型 / 参考图 / 版本都在 `.vs/jobs.json`，想重拍哪一镜都能对上。

`--stage draft` 不能和 `--takes` 同时用；`--chain` 也不能和 `--stage draft` / `--takes` 同时用（代码会显式拦下，避免草稿前缀丢失）。

---

## 十、更多能力（全部显式调用，默认关闭）

### 网关聚合：一个 key 调多模型

```bash
vs.py setup --set FAL_KEY=你的密钥
vs.py models                                   # 看快照日期和已核实的 model-id
vs.py gen -p "城市夜景" --provider fal --model alibaba/wan-3.0/text-to-video -d 5
```

`fal` / `heygen` 是网关型：一个 key 覆盖 Veo、Kling、Sora、Runway、Seedance、Wan 等（取决于市场上架情况）。`--model` 可传任意市场 model-id，脚本不硬编码猜测值。去 https://fal.ai/models 查当前 ID。详见 `references/gateways.md`。

### 对话式局部改片

```bash
vs.py edit --video videos/s1.mp4 --prompt "把车换成红色，其他保持不变"
vs.py edit --previous last --prompt "再让光线更暖一些"     # 多轮续改
```

走 Gemini Omni Interactions API，和 Veo 共用 `GEMINI_API_KEY`。内联视频 ≤ 4MB、单轮 3–15 秒，更大的视频要先走 Files API。改片提示词要短，加「其他保持不变」能减少误改。

### 音频链路

```bash
vs.py voice --text "清晨的光落进店里" --out audio/vo.mp3
vs.py voice --shot s1 --replace-into videos/s1.mp4        # 配音并替换音轨
vs.py align --srt 转录结果.srt                            # 用真实转录时间轴替代按字数估时
```

`voice` 调 MiniMax TTS 并量出音频真实时长。`align` 解析标准 SRT / WebVTT —— **本地不内置语音识别**，任何 ASR 工具产出的字幕都能直接喂进来对齐。

### 素材生成

```bash
vs.py media --kind image --prompt "咖啡店店员，齐肩深棕发，深绿高领针织衫" --out images/hero.png
vs.py media --kind music --prompt "温暖的爵士钢琴" --model <你在 fal 市场确认的 model-id>
```

静帧/音乐/音效走 fal 通用队列。`music` / `sfx` 的默认 model-id 是占位值，**必须自己指定核实过的 ID**，否则会明确报错。

### 平台交付

```bash
vs.py presets                                        # 抖音 / 小红书 / B站 / YouTube / 方形
vs.py export --presets douyin,bilibili --cover --watermark "@我的账号"
vs.py sheet --cols 3                                 # 候选 A/B/C 并排预览图
vs.py cover --at 2.5                                 # 抽封面
```

`export` 用 ffmpeg 做等比 scale + pad（不拉伸）、平台码率、右下角水印、封面帧。全部显式调用，`assemble` 不会自动做。

> **授权约定不变**：上面所有能力都默认关闭，必须用户明确要求后才调用。网关 / Omni / 素材生成的端点结构核对自官方文档（2026-11-15），但**尚未跑过真实付费生成**，第一次实单要当试跑并核对返回字段。

---

## 十一、常见问题

**没有 ffmpeg 会怎样？**
只生成片段完全不受影响。`assemble`、`animatic`、`chain --apply`、`run --chain` 会提示缺依赖。装完重跑即可。

**报「未设置密钥」？**
`vs doctor` 看哪家没配上；`vs setup` 看申请地址。密钥写在当前项目 `.env`、全局 `~/.workbuddy/video-studio.env`，或环境变量里都行。

**生成失败 / 卡住？**
`vs list` 看状态，`vs resume` 接着等（不会重复扣费），`vs run --only s2 --force` 单独重跑失败那镜。别一上来全量重跑。

**只想要几个片段？**
跑完 `vs run` 直接 `vs clips`，**不要调用 `assemble`**。此时 `output/` 应该是空的。

**片段拼起来画面跳？**
多半是各镜混用了模型或没锁 seed，`vs qc` 会指出来。有 recurring 人物时补做制作圣经、静帧先行和 `run --chain`。

**字幕和画面越往后越对不上？**
开了转场就会吃掉重叠时长，脚本会自动补偿。片头也会自动加到字幕轴上。

---

## 十二、项目结构

```
~/.workbuddy/skills/video-studio/
├── SKILL.md                 技能主说明（Agent 实际读的操作手册）
├── README.md                本教程
├── LICENSE                  MIT
├── VERSION / CHANGELOG.md   版本与变更记录
├── scripts/
│   ├── vs.py                命令行入口
│   └── vslib/
│       ├── providers.py      七家直连适配
│       ├── gateway.py        网关聚合、对话式改片、素材生成
│       ├── delivery.py       平台导出、并排预览、封面
│       ├── jobs.py           任务持久化（带跨进程锁）与并发轮询
│       ├── post.py           ffmpeg 后期、TTS、抽帧
│       └── pipeline.py / storyboard.py / core.py
├── references/              问询手册、生产管线、网关手册、参数对照、提示词、排错
├── tests/                   标准库单测，CI 每次提交自动跑
└── assets/project-template/ 项目模板（brief.md / storyboard.json）
```

你自己建的项目目录则是：

```
my-video/
├── storyboard.json     分镜（唯一真相源）
├── bible.json         制作圣经（有 recurring 主体时）
├── .env               密钥（已 gitignore）
├── images/  videos/  audio/  output/
└── .vs/
    ├── jobs.json        任务记录，含 prompt / seed / 模型 / 版本
    ├── takes.json       每镜候选清单
    ├── edits.json       对话式改片的多轮 interaction id
    ├── model-lock.json 本项目锁定的 provider
    └── work/            中间文件、逐镜缩略图与首末帧证据（可删）
```

---

## 十三、文档索引

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 技能入口：多轮询问关卡、命令总表、执行约束 |
| `references/intake.md` | 逐轮问询话术、决策树、反例清单 |
| `references/pipeline.md` | 生产管线方法论：一致性三板斧、阶段顺序、故障速查 |
| `references/workflow.md` | 项目结构与字段全表、交付前检查清单 |
| `references/providers.md` | 七家参数对照与限制（首尾帧、参考图、时长等） |
| `references/gateways.md` | 网关接口格式、model-id 怎么查、改片限制、未验证清单 |
| `references/prompt-guide.md` | 提示词写法 |
| `references/troubleshooting.md` | 排错速查 |

---

## 十四、当前状态

**验证到什么程度**：七家直连 + 两个网关的 endpoint 与鉴权头已用无效密钥逐一探测（都停在 401/403 鉴权层），后期链路用 ffmpeg 桩跑通了完整编排，单测与 CI 覆盖核心逻辑。**但还没有跑过任何真实付费生成** —— 各家的轮询状态、结果字段解析、图片上传方式、真实编码效果都要拿到密钥后做端到端实单才能确认。新增的网关 / Omni / 素材生成适配器尤其要当试跑。成本数字是按公开价格的粗估，不是账单。

**模型与价格会过期**：`RATES` 与 `references/providers.md` 标了快照日期 `2026-11-15`，用前请核对各家控制台。`vs.py models` 会打印这个日期。

**已知边界**：本地不内置 ASR（只对齐外部 SRT）；`music` / `sfx` 素材生成的默认 model-id 是占位值，必须自己指定；MuAPI 等无可核对公开文档的网关没有适配；Windows 只做了路径与字体兜底，未实测。

MIT 许可，欢迎提 issue 或 PR。
