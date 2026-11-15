# 网关聚合、改片与素材生成

> 核对日期 **2026-11-15**。这一层是本次补齐的能力，**端点与鉴权头已用无效密钥验证（401），但结果字段、轮询状态、真实下载都没跑过付费实单**。
> 所有命令都是显式调用才执行，默认关闭，不得替用户决定使用。

## 为什么要网关

直连七家要分别申请七个 key、各自审核计费。网关用一个 key 调用十余个模型，且把排队、文件托管、结果下载统一掉。代价是中间商加价、多一层故障点。

| provider | 环境变量 | 覆盖 | 申请 |
|---|---|---|---|
| `fal` | `FAL_KEY` | fal 市场上任意模型（Wan / Flux / Kling / Veo / Seedance / Sora 系等，取决于市场上架情况） | https://fal.ai/dashboard/keys |
| `heygen` | `HEYGEN_API_KEY` | Veo 3.1 / Kling / Sora 2 / Runway Gen-4 / Seedance / LTX | https://app.heygen.com/settings?tab=api |

**没有适配 MuAPI / Replicate / WaveSpeed**：没找到可核对的公开 REST 请求格式，不写猜测代码。想加时照 `vslib/gateway.py` 的 `Fal` 类实现 `submit()` / `poll()` 即可。

## fal.ai 队列接口（通用）

```
提交  POST https://queue.fal.run/{model-id}
      Authorization: Key $FAL_KEY        ← 注意是 Key，不是 Bearer
      {"prompt": "...", "aspect_ratio": "16:9", "resolution": "1080p", "duration": "5"}

状态  GET  https://queue.fal.run/{model-id}/requests/{request_id}/status
      IN_QUEUE / IN_PROGRESS / COMPLETED / FAILED

结果  GET  https://queue.fal.run/{model-id}/requests/{request_id}
      video 模型 → {"video": {"url": ...}}
      image 模型 → {"images": [{"url": ...}]}
      audio 模型 → {"audio_url": ...}
```

`_media_url()` 会依次尝试这三种 schema。结果 URL 有过期时间，`save_video()` 在轮询成功的当次就下载，不留到以后。

`Fal` 把 `model|request_id` 编进 job id，`poll()` 靠这一串就能重建状态与结果 URL，不需要额外表。

### 已核实的 model-id

脚本里只硬编码了**在官方文档里确认存在**的几个：

```
alibaba/wan-3.0/text-to-video
fal-ai/wan/v2.7/reference-to-video
fal-ai/flux/schnell
fal-ai/nano-banana-2
```

其余模型（Sora、Kling v3、Veo、Seedance 2.0、PixVerse、LTX、Pika、混元、万相）**故意不硬编码**：模型 ID 会变，写死就是明天坏掉。用 `--model` 传市场里查到的 ID：

```bash
vs.py gen -p "城市夜景" --provider fal --model fal-ai/某模型/id -d 5
vs.py run --provider fal --model fal-ai/某模型/id
vs.py media --kind image --prompt "角色设定图" --model fal-ai/flux/schnell
```

去 https://fal.ai/models 查当前 ID 与输入 schema。不同模型的输入字段不同，`Fal.submit()` 只填通用字段；需要专属字段时得改这个类，**别指望一个 prompt 通吃**。

## HeyGen 工作流

```
提交  POST https://api.heygen.com/v1/workflows/executions
      X-Api-Key: $HEYGEN_API_KEY
      {"workflow_type":"GenerateVideoNode",
       "input":{"prompt":"...", "provider":"veo_3_1", "aspect_ratio":"16:9"}}

轮询  GET  https://api.heygen.com/v1/workflows/executions/{execution_id}
完成  output.video_url
```

`provider` 取值见 `vs.py models` 里 heygen 一节。`reference_image_url` / `tail_image_url` **要求是公网可访问 URL**，本地图片得先上传，脚本没有实现托管上传。

## 对话式改片（Gemini Omni）

`vs.py edit` 走 Interactions API，同步返回：

```
POST https://generativelanguage.googleapis.com/v1beta/interactions?key=$GEMINI_API_KEY
{"model":"gemini-omni-1.2-flash", "input":[{"type":"video",...},{"type":"text","text":"..."}],
 "store":true, "response_format":{"type":"video","delivery":"inline"}}

结果 → steps[].content[] 里 type=video 的 .data（base64）或 .uri
```

多轮用 `previous_interaction_id`，本 skill 把它存在 `.vs/edits.json`，`--previous last` 自动取上一轮。

限制（来自官方文档）：

- 内联视频 **≤ 4MB**；更大的要先走 Files API 上传并用 `--video-uri`（脚本未实现上传）
- 输入/输出 3–15 秒；续写每轮 3–10 秒，累计最多 30 秒
- 只支持 `16:9` / `9:16`
- 语音编辑、Reference video 在编辑模式下不支持
- 改片提示词要短，「Keep everything else the same」能显著减少误改

这个 key 和 Veo 共用 `GEMINI_API_KEY`。

## 素材生成

`vs.py media --kind image|music|sfx|video` 走同一个 fal 通用队列，只是默认 model 不同。**music / sfx 的默认 model-id 是占位值，没有核实过**，用前必须用 `--model` 指定你在 fal 市场确认的模型；否则会报错，不会静默生成一个错东西。

静帧生成能力让「静帧先行」不再依赖 Agent 外部的生图工具，但仍需 `FAL_KEY`。

## 音频链路现状

| 环节 | 状态 |
|---|---|
| 中文配音 | `vs.py voice`，MiniMax T2A v2，已接进 CLI |
| 替换音轨 | `voice --replace-into`（显式） |
| 字幕对齐 | `vs.py align --srt`，解析标准 SRT / WebVTT |
| 语音识别 | **本地未内置**，需要外部 ASR 产出 SRT 再喂给 `align` |
| 背景音乐 | 仍需用户提供文件（`assemble --bgm`），或用 `media --kind music` 生成 |
| 音效 | `media --kind sfx`（同上，需指定 model） |

字幕时间轴原本按字数估时，`align` 可以用真实转录时间轴替代它。

## 平台交付

`vs.py presets` 看全部预设，`vs.py export --presets douyin,bilibili --cover` 一次导出多规格。全部基于本地 ffmpeg，scale + pad 不拉伸、右下角可选水印、可选封面帧。`vs.py sheet` 把候选片段抽帧拼成一张并排图，挑 A/B/C 时不用逐个点开视频。

这些都是**显式调用**，不会在 `assemble` 里自动发生。

## 未验证清单（拿到 key 后逐条核对）

- [ ] fal 各模型的真实输出 schema（`_media_url` 是否覆盖）
- [ ] HeyGen `execution_id` 与 `video_url` 的实际字段名
- [ ] 网关的图片上传方式（本地文件 / data URI / 公网 URL）
- [ ] Gemini Omni 的 `steps` 结构与 `previous_interaction_id` 多轮
- [ ] `media --kind music/sfx` 的真实 model-id 与输入字段
- [ ] 真实计费与 `RATES` 粗估的偏差

**在核对完之前，别把这些能力当成「已可用」向用户承诺。**
