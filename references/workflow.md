# 工作流详解与进阶用法

> **开工前先读 `references/intake.md`。** 五轮询问没走完、成本没确认，不要提交任何生成任务。
> 做多镜头项目再读 `references/pipeline.md`（一致性、预演、候选、质检的方法论）。
> 本文档只讲「确认之后怎么跑」。

## 项目结构

`vs.py init` 之后目录长这样：

```
my-project/
├── brief.md            ★ 用户确认记录（授权凭证，每轮问完就写）
├── bible.json          制作圣经：身份块/风格/参考图/模型/seed（用户确认过才填）
├── project.json        项目元信息（provider 偏好、比例）
├── storyboard.json     分镜（唯一真相源，改这里）
├── .env                密钥（已 gitignore）
├── refs/               角色设定表、场景参考、道具参考（资产库）
├── images/             首帧图
│   └── chain/          尾帧链抽出的下一镜首帧
├── videos/             生成的片段（draft-*.mp4 是草稿，sN-X-* 是候选版本）
├── audio/              配音、BGM
├── output/             最终成片（用户说「只要片段」时这个目录必须是空的）
└── .vs/
    ├── jobs.json       所有任务记录（含 prompt/seed/参考图/版本，断点续跑靠它）
    ├── takes.json      每镜 A/B/C 候选版本清单
    ├── model-lock.json 本项目锁定的 provider（防止中途换模型）
    └── work/           中间文件（可删）
        ├── animatic.mp4  草稿粗剪，仅内部预演，不交付、不进 output/
        ├── norm-*.mp4    归一化副本
        └── subs.srt     字幕
```

`.vs/work/` 会占不少空间（每镜一份归一化副本）。成片出来后可以放心删，`assemble --rebuild` 会重建。

## 两种交付形态

| 用户第 3 轮的答案 | 跑什么 | 绝对不要做 |
|---|---|---|
| **只要片段** | `run` → `clips` → 逐个交付 `videos/*.mp4` | 不要调 `assemble`，不要生成 `output/*.mp4` |
| **帮我剪成一条** | `run` → `assemble` + 只带确认过的参数 | 不要顺手加任何没确认的效果 |

`assemble` **默认是纯拼接**：归一化规格 + 硬切，不加字幕、不铺音乐、不加片头、不做转场、不加淡入淡出。
`storyboard.json` 里的 `bgm` / `transition` / `subtitle` 默认**不生效**，只有显式参数或 `--use-storyboard`（用户已看过确认单）才会启用。

## 生产管线命令速查

完整方法论见 `references/pipeline.md`（一致性三板斧、阶段 0–7、故障速查）。
以下增强环节**都要先问用户**，没答就不做。

| 命令 | 用途 |
|---|---|
| `vs.py bible` | 查看 / 生成 `bible.json` |
| `vs.py bible --set identity='...' seed=12345 --lock` | 锁定身份块与 seed（`--set` 可重复，也可一次写多个） |
| `vs.py run --stage draft` | 720p 草稿，不写回分镜 |
| `vs.py animatic` | 草稿拼粗剪到 `.vs/work/`，检查节奏（内部预演，不交付） |
| `vs.py run --takes 3` | 每镜 A/B/C 候选，seed 依次 +1 |
| `vs.py takes` / `pick s1 B` | 列候选 / 选版回填（不重新生成、不花钱） |
| `vs.py run --chain` | **推荐**：逐镜尾帧链，边生成边接末帧，不用重跑 |
| `vs.py chain` / `chain --apply` | 补救用：尾帧链预览 / 改写分镜 `image`（之后必须 `--force` 重跑后续镜头） |
| `vs.py qc` | 自动质检 + 建议分级 + 人工过目清单 |
| `vs.py run --unlock-model` | 解除项目模型锁定（换模型会导致风格漂移） |

`run` 会整批只解析一次 provider 并写进 `.vs/model-lock.json`，后续镜头与 `run --only` 都沿用同一家；
身份块自动逐字拼到每个镜头 prompt 最前面；每镜的 prompt / seed / 模型 / 参考图 / 版本都记进 `.vs/jobs.json`。

## storyboard.json 字段全表

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string | 项目名，也用于成片文件名 |
| `ratio` | string | `16:9` `9:16` `1:1` `4:3` `3:4` `21:9` |
| `resolution` | string | `720p` `1080p` `4k`，影响成片尺寸 |
| `audio` | bool | **默认 false**：模型原生音效。用户确认要才置 true，或生成时加 `--audio` |
| `voiceover` | bool | 是否要配音轨（目前仅标记用途） |
| `style` | string | 统一风格，自动拼到每镜 prompt 后面 |
| `negative` | string | 全局排除项 |
| `transition` | string | **默认 `none`**。用户确认后才改 `fade` / `dissolve` / `slideleft` 等 |
| `transition_dur` | float | 转场时长，默认 0.5 秒 |
| `bgm` | string | **默认空**。用户确认并提供文件后才填 |
| `font_size` | int | 字幕字号 |
| `shots[].id` | string | 镜头编号，`--only` 用它 |
| `shots[].desc` | string | 给人看的镜头说明 |
| `shots[].prompt` | string | 给模型的描述 |
| `shots[].duration` | int | 秒，自动吸附到模型支持值 |
| `shots[].image` | string | 首帧图路径（要先问用户要不要做首帧图） |
| `shots[].last_image` | string | 尾帧图路径 |
| `shots[].voiceover` | string | 旁白文本（**用户确认要配音/字幕后**才填） |
| `shots[].subtitle` | string | 字幕文本（同上；留空则用 voiceover） |
| `shots[].video` | string | 生成后自动回填，不要手改 |
| `shots[].note` | string | 备注，不影响执行 |

## 断点续跑

视频生成很慢，中断是常态。三层保护：

1. **提交即落盘** — 拿到远程 ID 立刻写进 `.vs/jobs.json`
2. **`vs.py resume`** — 只跟进未完成的任务，不重复提交
3. **`vs.py run` 幂等** — 已有 `video` 字段的镜头自动跳过

正确的重启姿势：先 `resume`，再 `run`。之后**按 `brief.md` 里记录的交付形态**决定要不要 `assemble`，不要看到片段齐了就顺手合成。

```bash
vs.py resume        # 把上次没跑完的等完
vs.py run           # 补齐还缺的镜头
cat brief.md        # 确认用户到底要不要剪辑
# 剪辑=否 → vs.py clips 直接交付
# 剪辑=是 → vs.py assemble [只带确认过的参数]
```

## 只重跑某几镜

```bash
vs.py run --only s2,s5 --force
```

改完分镜里 s2 的 prompt，加 `--force` 覆盖已有视频。其他镜头不动。

## 批量变体（A/B 测试构图）

**项目内一律用生产管线的候选机制**，版本会记进 `.vs/takes.json`，可以并列比较、一键回填：

```bash
vs.py run --takes 3 --only s1     # 生成 A/B/C，seed 依次 +1，永不覆盖
vs.py takes                       # 列出候选及 seed / 模型 / prompt
vs.py pick s1 B                   # 只改引用，不重新生成、不额外花钱
```

只有**临时试一个片段、没建项目分镜**时才用 `gen` 手搓：

```bash
for i in 1 2 3; do
  vs.py gen -p "同一个 prompt" --seed $RANDOM --shot "s1-v$i" --provider ark
done
vs.py list
```

这种写法不会登记到 `takes.json`，也就不能用 `pick`，挑中后得手动把文件名写回 `video` 字段。
**多做几个版本会多花钱，先跟用户说一声并确认，报账用 `estimate --takes 3`。**

## 后期能力（每一项都要用户确认过）

### 只拼接，不做别的（默认）

```bash
vs.py assemble
```

不加任何参数就是这样：归一化 + 硬切，什么附加处理都没有。

### 字幕

```bash
vs.py assemble --subtitles
```

前提：用户明确说要字幕，且确认过文案。分镜里没有 `subtitle`/`voiceover` 文案时，脚本会提示并跳过。

### 片头

```bash
vs.py assemble --title "品牌名" --subtitle "2026 春季新品" --title-dur 3
```

片头会自动排在最前，字幕时间轴整体后移。前提：用户确认要片头、给了文字。

### 背景音乐

```bash
vs.py assemble --bgm audio/chill.mp3 --bgm-volume 0.25
```

**文件必须是用户提供的，不要自己找。**
默认 `0.18` 音量并开启 ducking（人声出现时音乐自动降低）。`--bgm-volume` 调不了避让强度，需要改 `post.add_bgm()` 的 `duck` 参数。

### 转场 / 淡入淡出

```bash
vs.py assemble --transition fade --fade
```

默认 `--transition none`（硬切），快节奏内容硬切反而更利落。要用转场必须显式指定。

### 沿用已确认过的分镜设置

```bash
vs.py assemble --use-storyboard
```

**仅在用户已看过确认单、逐项点头之后使用。** 它会读取分镜里的字幕文案、`bgm`、`transition`。
不加这个参数时，这些字段一律忽略——这是防止模板里的示例值被误当成用户授权的保险。

### 原生音效（生成阶段，不是后期）

```bash
vs.py gen -p "..." --audio        # 或把 storyboard.json 的 audio 置 true
```

默认关闭。只有支持原生音频的模型（Veo / Seedance 1.5 Pro 等）才有意义。

## 在 Python 里直接调用后期能力

不用命令行也能用——**但同样要用户明确要求了才做**：

```python
import sys
sys.path.insert(0, "/Users/<你>/.workbuddy/skills/video-studio/scripts")
from pathlib import Path
from vslib import post

# 把一堆规格不一的片段拼成一个
clips = [Path("a.mp4"), Path("b.mp4")]
norm = [post.normalize(c, Path(f".vs/work/n{i}.mp4"), w=1080, h=1920) for i, c in enumerate(clips)]
post.concat(norm, Path("out.mp4"))

# 单独加字幕
post.write_srt([(0.0, 2.5, "第一句"), (2.5, 5.0, "第二句")], Path("s.srt"))
post.burn_subtitles(Path("out.mp4"), Path("s.srt"), Path("out-sub.mp4"))

# 生成中文配音
from vslib.core import Config
post.tts(Config(), "这是一段测试旁白", Path("audio/vo.mp3"))

# 用配音替换原音轨
post.replace_audio(Path("out-sub.mp4"), Path("audio/vo.mp3"), Path("final.mp4"))
```

## 成本控制

按这个顺序省：

1. **先 `estimate`，把账给用户看，等确认** — 这是硬性流程，不是建议。
   选了加速环节就带倍数：`estimate --takes 3 --draft`（3 候选 + 草稿 = ×3.5），确认单用 `plan --takes 3 --draft --chain`
2. **草稿用便宜模型** — 构图阶段用 `vidu`（¥0.15/s）或 `runway gen4_turbo`，定稿再用 `kling` / `veo`（换模型前先说一声）
3. **时长压到最短** — 5 秒能讲完就别写 10 秒，成本直接减半
4. **分辨率按需** — 只在手机看就用 720p，能省 40% 左右
5. **只重跑坏的那镜** — 用 `--only`，别全量 `--force`
6. **失败不重复扣费** — 任务失败通常不收费，但重试前先看 `list` 里的错误原因

一条 30 秒、6 镜的片子，用 ark 大约 ¥8–15，用 veo 大约 ¥90。

## 交付前检查清单

- [ ] **`brief.md` 里五轮询问都有明确结论**，没有把空白当默认
- [ ] 加速环节（静帧 / 草稿 / 多候选 / 尾帧链）每一项都有明确授权，没答的没做
- [ ] `vs.py qc` 跑过，自动检查无「混用多个模型」告警，人工清单逐条过目；
      **打开 `.vs/work/qc-sheet.png` 与 `qc-joints/` 逐镜看画面连贯性**（有 ffmpeg 时才有）
- [ ] 新增能力（`voice` / `media` / `edit` / `export` / `sheet` / `cover` / `--fallback` / `--parallel`）
      每一项都有明确授权，没答的没做；预算上限（若设了）没有被绕过
- [ ] 有人物/产品的项目填了 `bible.json` 身份块，且全项目同一个 provider + 同一个 seed
- [ ] 用户答「只要片段」→ `output/` 是空的，交付的是 `videos/*.mp4`
      （草稿粗剪在 `.vs/work/animatic.mp4`，不算成片，不会污染 `output/`）
- [ ] 用户没确认字幕 → 成片里没有字幕
- [ ] 用户没确认配乐 → 没有混入音乐，且用的是用户提供的文件
- [ ] 用户确认过成本（`estimate` 输出已贴给他看过）
- [ ] 每镜时长加起来符合预期
- [ ] 字幕没有超出镜头时长（太长会被截断，拆句或缩短文案）
- [ ] 相邻镜头色调不能跳太狠（靠统一 `style` 控制）
- [ ] 人物/产品在各镜之间一致（靠首帧图控制）
- [ ] BGM 没有盖住人声（听一遍，必要时降 `--bgm-volume`）
- [ ] 文件大小合理（30 秒 1080p 大约 15–30MB）

## 性能参考

| 环节 | 耗时 |
|---|---|
| 单镜生成（5s, 720p） | 40 秒 – 3 分钟 |
| 单镜生成（5s, 1080p，高峰期） | 3 – 10 分钟 |
| 单镜生成（veo 8s） | 2 – 6 分钟 |
| 归一化（每镜） | 5 – 15 秒 |
| 拼接（6 镜） | 10 – 30 秒 |
| 烧字幕 | 20 – 60 秒 |
| TTS（50 字） | 2 – 5 秒 |

6 镜的片子从零到成片，顺利的话 15–30 分钟；只交付片段则省掉后期那几分钟。
