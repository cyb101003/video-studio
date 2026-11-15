# 视频模型参数对照（七家直连 + 两个网关）

**参数与费率快照：2026-11-15。** 厂商改模型 ID、时长档位、价格都很频繁，用前请核对各家控制台；`vs.py models` 会打印这个日期。字段以各家官方文档为准，`scripts/vslib/providers.py` 与 `gateway.py` 里的实现已做归一化。

> 网关 provider（`fal` / `heygen`）的接口格式、model-id 查法与未验证清单见 `references/gateways.md`。
> 所有 provider 的 endpoint 与鉴权头只用无效密钥验证过（停在鉴权层），**结果字段与轮询状态尚未跑过真实实单**。

## 总览

| provider | 标识 | 密钥变量 | 默认模型 | 优势 |
|---|---|---|---|---|
| 即可 Seedance | `ark` | `ARK_API_KEY` | doubao-seedance-1-0-pro-250528 | 便宜、国内直连、中文准 |
| 可灵 Kling | `kling` | `KLING_API_KEY` | kling-3.0 | 运镜稳、支持 4K 和多镜头 |
| 海螺 MiniMax | `minimax` | `MINIMAX_API_KEY` | MiniMax-Hailuo-2.3 | 人物表演自然 |
| Vidu | `vidu` | `VIDU_API_KEY` | viduq2 | 便宜快、参考图模式强 |
| Runway | `runway` | `RUNWAY_API_KEY` | gen4_turbo | 生态成熟、有视频转视频 |
| Veo | `veo` | `GEMINI_API_KEY` | veo-3.1-generate-preview | 画质最好、原生音效 |
| Luma | `luma` | `LUMA_API_KEY` | ray-2 | 画面美学强 |
| fal.ai 网关 | `fal` | `FAL_KEY` | alibaba/wan-3.0/text-to-video | 一个 key 调市场里任意模型，`--model` 指定 |
| HeyGen 网关 | `heygen` | `HEYGEN_API_KEY` | veo_3_1 | 一个 key 覆盖 Veo/Kling/Sora/Runway/Seedance |

## 时长与分辨率

| provider | 支持时长 | 支持分辨率 | 支持比例 |
|---|---|---|---|
| ark | 5 / 10 | 480p / 720p / 1080p | 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive |
| kling | 5 / 10（3.0 可 3–15） | 720p / 1080p / 4k | 16:9 / 9:16 / 1:1 |
| minimax | 6 / 10 | 768P / 1080P | 16:9 / 9:16 / 1:1（由首帧或提示词决定） |
| vidu | 4 / 8 | 540p / 720p / 1080p | 16:9 / 9:16 / 1:1 / 4:3 / 3:4 |
| runway | 5 / 10 | 由比例决定（720p 级） | 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 |
| veo | 4 / 6 / 8 | 720p / 1080p / 4k | 16:9 / 9:16 |
| luma | 5 / 9 | 540p / 720p / 1080p / 4k | 16:9 / 9:16 / 1:1 / 4:3 / 3:4 |

脚本的 `norm()` 会把用户给的时长吸附到最近的合法值，分辨率向下取最近的合法档。比如给 veo 传 5 秒，会变成 4 或 6。

## 输入能力

| provider | 文生视频 | 图生视频 | 首尾帧 | 参考图 | 原生音频 | seed |
|---|---|---|---|---|---|---|
| ark | ✓ | ✓ | ✓ | — | ✓（1.5 pro） | ✓ |
| kling | ✓ | ✓ | ✓ | ✓（omni） | ✓ | ✓ |
| minimax | ✓ | ✓ | ✓ | — | — | — |
| vidu | ✓ | ✓ | ✓ | ✓（最多 7 张） | ✓（q3） | ✓ |
| runway | ✓ | ✓ | ✓ | — | — | ✓ |
| veo | ✓ | ✓ | ✓ | ✓（最多 3 张） | ✓ 始终开 | ✓ |
| luma | ✓ | ✓ | ✓ | — | — | — |

`auto` 选路时会检查这个表：如果要首尾帧，只会选支持 `last_frame` 的那几家。

## 接口速查（排错用）

### ark（火山方舟）
```
POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
GET  .../api/v3/contents/generations/tasks/{id}
鉴权 Bearer ARK_API_KEY
结果 location: content.video_url
状态: queued / running / succeeded / failed / expired
```

### kling（可灵）
```
POST https://api-beijing.klingai.com/image-to-video/{model}   有图
POST https://api-beijing.klingai.com/text-to-video/{model}    纯文
GET  https://api-beijing.klingai.com/tasks?task_ids={id}
鉴权 Bearer KLING_API_KEY
结果 location: data[0].outputs[] 里 type=video 的 url
状态: submitted / processing / succeeded / failed
注意: 结果链接 30 天后清理，务必当天下载
```

### minimax（海螺）
```
POST https://api.minimaxi.com/v1/video_generation        （国内）
POST https://api.minimax.io/v1/video_generation          （国际）
GET  /v1/query/video_generation?task_id={id}
GET  /v1/files/retrieve?file_id={file_id}
鉴权 Bearer MINIMAX_API_KEY
结果: 先拿 file_id，再换 file.download_url
状态: Queueing / Preparing / Processing / Success / Fail
国内用 minimaxi.com，海外用 minimax.io，域名错会报 login fail
```

### vidu
```
POST https://api.vidu.com/ent/v2/text2video
POST https://api.vidu.com/ent/v2/img2video
POST https://api.vidu.com/ent/v2/start-end2video
POST https://api.vidu.com/ent/v2/reference2video
GET  https://api.vidu.com/ent/v2/tasks/{id}/creations
鉴权 Authorization: Token VIDU_API_KEY   ← 注意是 Token 不是 Bearer
结果 location: creations[].url
状态: created / queueing / processing / success / failed
```

### runway
```
POST https://api.dev.runwayml.com/v1/text_to_video
POST https://api.dev.runwayml.com/v1/image_to_video
POST https://api.dev.runwayml.com/v1/uploads        ← 本地图片必须先上传
GET  https://api.dev.runwayml.com/v1/tasks/{id}
鉴权 Authorization: Bearer RUNWAY_API_KEY
     X-Runway-Version: 2024-11-06      ← 少了这个头会 400
比例用像素写法: 1280:720 / 720:1280 / 960:960
结果 location: output[0]
状态: PENDING / RUNNING / SUCCEEDED / FAILED
```

### veo（Gemini API）
```
POST https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning
GET  https://generativelanguage.googleapis.com/v1beta/{operation_name}
鉴权 x-goog-api-key: GEMINI_API_KEY
结果 location: response.generateVideoResponse.generatedSamples[0].video.uri
下载时要在 URL 后加 &key=API_KEY
状态: 看 done 字段
限制: duration 只能 4/6/8；1080p 必须 8 秒
```

### luma
```
POST https://api.lumalabs.ai/dream-machine/v1/generations
GET  https://api.lumalabs.ai/dream-machine/v1/generations/{id}
鉴权 Bearer LUMA_API_KEY
结果 location: assets.video
状态: queued / dreaming / completed / failed
```

## 自定义域名（代理/中转）

如果用户用中转服务，在 `.env` 里覆盖这些变量即可，代码不用改：

```
ARK_BASE_URL=https://your-proxy.com/api/v3
KLING_BASE_URL=https://your-proxy.com
MINIMAX_BASE_URL=https://your-proxy.com
VIDU_BASE_URL=https://your-proxy.com
RUNWAY_BASE_URL=https://your-proxy.com
VEO_BASE_URL=https://your-proxy.com/v1beta
LUMA_BASE_URL=https://your-proxy.com/dream-machine/v1
```

## 新增一家模型

改 `scripts/vslib/providers.py`，三步：

1. 继承 `Provider`，填 `name / label / models / durations / resolutions / supports`
2. 实现 `submit(req) -> str`（返回远程任务 ID）和 `poll(job_id) -> JobStatus`
3. 登记到 `PROVIDERS` 字典，并在 `AUTO_ORDER` 里排个位置。同时在 `core.py` 的 `KEY_ENVS` 加密钥变量名

`submit` 里统一用 `self.require_key()` 取密钥、`to_data_url()` 处理本地图片、`request()` 发请求（自带重试）。`poll` 只负责把各家状态映射成 `queued / running / succeeded / failed` 四态。
