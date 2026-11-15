# 排错手册

按「症状 → 原因 → 处理」查。

## 密钥与鉴权

### `HTTP 401` / `AuthenticationError`
密钥没配、配错、或复制时带了空格。
- 跑 `vs.py doctor` 确认哪家显示 ✓
- 检查 `.env` 里有没有多余引号或行尾空格
- 可灵要的是**开发者 API Key**（控制台 API 密钥页），不是 AK/SK
- Runway 的 key 必须以 `key_` 开头

### `login fail: Please carry the API secret key`（MiniMax）
域名用错了。国内账号必须用 `api.minimaxi.com`，海外账号用 `api.minimax.io`。
脚本默认国内。海外账号在 `.env` 加 `MINIMAX_BASE_URL=https://api.minimax.io`。

### `HTTP 403` / `Not authenticated`（Vidu、Luma）
Vidu 的鉴权头是 `Authorization: Token xxx`，不是 `Bearer`（代码已处理，手动 curl 时注意）。
Luma 检查 key 是否已激活，新注册需要先充值。

### `API key not valid`（Veo）
Gemini API key 无效。注意区分 Google AI Studio 的 key（`AIza...`）和 Vertex AI 的服务账号，本 skill 用的是前者。

### 想用中转/代理
在 `.env` 里覆盖 `*_BASE_URL` 变量，见 `references/providers.md` 末尾。代码不用改。

## 提交阶段

### `HTTP 400` + 字段名
参数组合非法。最常见三种：
- `audio=true` 但模型不支持原生音频 → 加 `--no-audio`
- 1080p 配了非 8 秒时长（Veo）→ 改 `-d 8` 或 `-r 720p`
- 首尾帧同时给但模型只支持首帧 → 去掉 `--last-image`

### `内容风控` / `raiMediaFiltered`
提示词触发了安全策略。不是 key 的问题。
- 去掉真实人名、明星、品牌 logo、政治符号
- 去掉暴力、血腥、暴露类描述
- 人物改成「一个年轻人」这类泛化描述
- 实在过不了就换一家模型，各家策略不同

### `模型不存在` / `model not found`
模型 ID 写错或没开通。跑 `vs.py models` 看可用列表，或去控制台确认该模型已开通（火山方舟需要手动开通）。

### `配额不足` / `insufficient balance`
账户余额或额度用完。去控制台充值，或换一家有额度的 provider。

## 生成阶段

### 一直 `queued` 不动
高峰期排队，国内模型常见 3–10 分钟。
- 先确认不是网络断了：`vs.py status <id>` 手动查一次
- 超过 10 分钟仍然排队，可能是该模型容量紧张，换 provider 重跑
- `vs.py resume` 可以关掉终端之后再回来等

### 任务卡在 `running` 很久
部分模型长视频（10s / 4K）确实慢，20 分钟不算异常。
超过 30 分钟基本是卡住了，建议 `vs.py run --only sX --force` 重跑那一镜。

### `下载失败` / `链接可能过期`
各家结果链接有效期：可灵 30 天，火山方舟 24 小时，MiniMax 24 小时。
- 立刻用 `vs.py fetch <job_id>` 重试
- 链接彻底失效只能重跑（`--force`）

## 后期阶段（ffmpeg）

### `没有找到 ffmpeg`
```bash
# macOS
brew install ffmpeg
# Windows
winget install Gyan.FFmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
```
装完重启终端，`vs.py doctor` 应显示 ✓。
**注意**：普通片段生成不需要 ffmpeg；`assemble`、`animatic`、`chain --apply`、`run --chain` 需要（后两者要抽帧）。
可以先出片段，之后再装。**用户说「只要片段」且不做草稿/尾帧链时就不需要装它**。

### `concat` 报错 / 拼接后画面花屏
片段规格不一致。`assemble` 已经强制先归一化，如果还出错：
```bash
vs.py assemble --rebuild
```
清掉 `.vs/work` 的缓存中间文件重来。

### 拼接后音画不同步
某些模型输出的音轨时间戳有问题。若用户明确同意配乐并提供了文件，可以用 `add_bgm` 重建音轨：
```bash
vs.py assemble --bgm audio/bgm.mp3
```
**没得到配乐授权就别用这个办法**——先问用户，或干脆只交付片段让他自己后期处理。

### 字幕不显示 / 显示方块
- macOS 缺字体：确认 `/System/Library/Fonts/PingFang.ttc` 存在
- Linux 装 `fonts-noto-cjk`：`sudo apt install fonts-noto-cjk`
- Windows 用 `C:/Windows/Fonts/msyh.ttc`（微软雅黑）
- 字幕被裁到画面外：调小 `--font-size` 或在 storyboard 里改 `font_size`

### 字幕时间不对
时间轴是按「镜头时长」和「语速估算」推的，没有真实对齐配音。
要精确对齐：先 `post.tts()` 生成配音，量出音频时长，再把每镜 `duration` 设成对应时长。

### 转场后总时长变短
`xfade` 转场会重叠两段，每处转场缩短 `transition_dur` 秒。
6 镜 × 0.5s = 少 2.5 秒。要精确总时长就用 `--transition none`。

### 中文出现乱码
字幕文件编码问题。脚本用 UTF-8 写出，如果仍乱码，确认 ffmpeg 版本 ≥ 4.x（老版本 libass 有 bug）。

## 效果问题（非报错）

### 画面里的人变来变去
文生视频的通病。**必须**改成图生视频：先生成统一的首帧图，再 `-i` 传进去。见 SKILL.md 的「先出首帧」章节。

### 画面里出现奇怪的文字
模型试图"写字"但写不对。不要在 prompt 里要求文字，`negative` 字段加上 `文字, 字幕, 水印`，需要文字用后期字幕。

### 动作太快 / 太夸张
- prompt 里加「缓慢」「轻微」「自然地」
- 降低运动幅度：Vidu 有 `movement_amplitude` 可设 small（代码未暴露，需改 providers.py）
- 换模型：可灵的动作幅度控制相对克制，Vidu 更容易夸张

### 风格不统一，拼起来像不同片子
- 统一写进 `style` 字段，不要每镜各写一套
- 首帧图用同一段风格描述生成
- 调色后处理：`post` 里可以加 `eq` 滤镜统一色调

### 生成的是"慢动作幻灯片"
主体太小或动作描述太弱。改成：
- 加大主体在画面中的占比（「特写」「充满画面」）
- 明确写动作动词（「走过」「旋转」「落下」）
- 加运镜（「镜头推进」比静止画面动感强得多）

## 性能与稳定性

### 一次跑 20 镜太慢
拆批跑：
```bash
vs.py run --only s1,s2,s3,s4
vs.py run --only s5,s6,s7,s8
```
每批之间可以干别的。任务都在 `.vs/jobs.json` 里，不会丢。

### 想并行提交
```bash
for s in s1 s2 s3; do vs.py gen -p "..." --shot $s --no-wait; done
vs.py resume
```
`--no-wait` 提交后立即返回，最后统一 `resume` 等待。注意各家有并发限制，一次别超过 3–5 个。

### 磁盘占满
`.vs/work/` 是主要的空间占用方（每镜一份归一化副本）。成片出来后清理：
```bash
rm -rf .vs/work
```
下次 `assemble` 会自动重建。

## 流程 / 授权问题

### 分镜里明明写了 `bgm` / `subtitle` / `transition`，合成后却没生效
**这是刻意设计，不是 bug。** 这些字段默认不被 `assemble` 读取，防止模板里的示例值被误当成用户授权。
只有用户逐项确认过、并且你显式传了参数（`--bgm` / `--subtitles` / `--transition`），
或用户已看过确认单后加 `--use-storyboard`，才会生效。

### 用户说「不用剪辑」，但我已经合成了成片
不要交付那个成片。删掉 `output/` 下的本次产物（片段在 `videos/` 里，不受影响，不用重新生成、不会重复扣费），
然后 `vs.py clips` 列清单，逐个交付片段。跟用户说明一句即可。

### 不确定用户到底有没有同意加字幕 / 配乐
查项目根目录的 `brief.md`。**没有明确的「要」字就不算同意**，别翻聊天记录靠印象猜，直接再问一次：
「字幕和背景音乐这两项你要吗？不要的话我就保持原样。」

### 用户回「随便」「你看着办」
不算授权。按最保守方案（只给片段、零后期）复述一遍再确认一次；还是含糊就停下来，
把已确认的信息整理给他请他明示。详见 `references/intake.md`。

### 想复用上次确认过的设置
`vs.py assemble --use-storyboard`。前提：用户这次也看过确认单。跨项目、隔天继续、换了需求，都要重新问。

## 生产管线问题

### 多镜头之间人物换脸 / 衣服变色
先看 `vs.py qc` 有没有报「混用了多个模型」。再检查三件事：
`bible.json` 的 `identity` 是否填了且**每个镜头一字不差**（脚本会自动拼，别手动改写某一镜的 prompt）、
`.vs/model-lock.json` 里的 provider 是否全程一致、每镜是否都用了同一张参考图/首帧。
补做 `vs.py chain --apply` 把末帧接到下一镜，再 `run --only sX --force` 重跑漂移的那几镜。
完整排查表见 `references/pipeline.md`。

### 想换模型却被拦下：「本项目已锁定 xxx」
这是**刻意保护**：换模型会让前后镜头风格断裂。真要换用 `vs.py run --unlock-model`，
并先跟用户说明「换模型后建议整批重跑，否则新旧片段混在一起」。
如果 `bible.json` 里 `--lock` 过，还要先 `vs.py bible --set provider=新模型 --unlock`。

### `bible` 提示参考图缺失
`references` 只是资产库清单，脚本会检查文件是否存在。把角色设定表放进 `refs/` 目录，
路径写相对项目根目录，例如 `refs/hero-front.png`。参考图不会自动传给模型
（要 provider 支持多图参考，且显式加 `--use-refs`）。

### `chain --apply` 没效果 / 报错
- **最常见的「没效果」其实是正常现象**：它只改分镜的 `image` 字段，**不会改已经生成好的 mp4**。
  命令输出里会列出需要重跑的镜头，照着执行 `vs.py run --only s2,s3 --force` 才生效（会再花一次生成费）。
  下次从头做直接用 `vs.py run --chain`，边生成边衔接，一次跑完不用重跑。
- 没装 ffmpeg → 抽帧做不了，`brew install ffmpeg`
- 下一镜已有自定义首帧 → 默认跳过并提示，确要覆盖加 `--force`
- 抽出来的图在 `images/chain/`，预览用 `vs.py chain`（不带 `--apply`），不改任何文件

### 只跑了一次 `--set`，其他字段没存进去
`bible --set` 支持重复写多次，也可以一次跟多个 `字段=值`。早期版本多次 `--set` 会互相覆盖，现已修复。

### 草稿片段和定稿混在一起
草稿文件名是 `draft-sX-*`，候选是 `sN-A-*` / `sN-B-*`，定稿回填在 `storyboard.json` 的 `video` 字段。
`animatic` 只挑 `draft-` 前缀、粗剪写到 `.vs/work/animatic.mp4`；`assemble` 只认 `video` 字段、成片写到 `output/`。
两者不会互相污染，用户答「只要片段」时 `output/` 仍然保持为空。

### 确认单上的成本比实际账单少很多
多半是选了 `--takes 3` / `--stage draft`，但第 5 轮 `estimate` 只报了 1 倍。
用 `vs.py estimate --takes 3 --draft` 和 `vs.py plan --takes 3 --draft --chain` 重新报账，再请用户确认一次。

## 新增能力常见问题

### `voice` 报「配音需要 MiniMax 密钥」
`voice` 走 MiniMax T2A v2，需要 `MINIMAX_API_KEY`（和海螺视频共用一个 key）。没有就先配，或者用外部配音文件 + `voice --replace-into`。

### `align` 解析出 0 条
只认标准 SRT / WebVTT 时间码（`00:00:00,000 --> 00:00:02,500`）。ASR 工具导出的纯文本、JSON 要先转成 SRT。本地**不做语音识别**，这一步只负责对齐。

### `media --kind music/sfx` 报错
这两个类型的默认 model-id 是占位值，没在 fal 市场核实过。用 `--model` 指定你确认过的 ID，并核对它的输入字段（`Fal.submit()` 只填通用字段）。

### `edit` 说视频超过 4MB
Gemini Omni 内联 base64 上限 4MB。更大的视频要先走 Files API 上传，再用 `--video-uri` 传 URI；本脚本没实现上传。另外单轮 3–15 秒、只支持 16:9 / 9:16。

### `run --parallel` 和 `--chain` / `--takes` 冲突
会显式拦下。尾帧链必须在生成下一镜之前完成，只能串行；并发模式也暂不支持候选/草稿的版本命名。普通多镜定稿用 `--parallel` 最快。

### 预算被 `--max-cost` 拦下
这是按公开单价的粗估刹车，不是账单。可以调小 `--takes`、去掉草稿、缩短时长、换便宜 provider，或明确提高上限后重跑。**提高上限等于用户重新确认成本，要再问一次。**

### 网关返回「找不到媒体 URL」
`_media_url()` 覆盖了 video/images/audio 三种常见 schema，但各模型输出不统一。把真实响应贴出来，按该模型补一个字段路径即可。这正是「只验过端点结构」的风险点。

### 多终端同时跑会不会覆盖任务记录
不会：`.vs/jobs.json` 写入带跨进程文件锁，且每次写入会与磁盘上其它进程刚写的记录合并。macOS / Linux 用 flock，Windows 用 msvcrt，都不可用时退化为原子替换。

## 快速诊断顺序

遇到问题按这个顺序过一遍，80% 情况能定位：

1. `vs.py doctor` — 环境和密钥对不对
2. `vs.py list` — 任务真实状态是什么
3. `vs.py status <id>` — 单个任务的最新状态和错误
4. 看 `.vs/jobs.json` — 原始记录
5. 手动 curl 一次（照 `references/providers.md` 的接口速查）— 排除网络/代理问题
