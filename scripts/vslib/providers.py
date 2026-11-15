"""多 provider 适配层。

把 7 家视频模型的差异（路径、鉴权、字段名、状态机、结果位置）收敛成
统一的 GenRequest -> job_id -> JobStatus 三件事。

新增一家只需继承 Provider 并实现 submit()/poll()/pick_model()。
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import (
    Config,
    download,
    fail,
    image_dims,
    is_url,
    log,
    request,
    to_b64,
    to_data_url,
)

# ---------------------------------------------------------------- 统一数据结构


@dataclass
class GenRequest:
    prompt: str
    image: str | None = None          # 首帧（本地路径或 URL）
    last_image: str | None = None     # 尾帧
    ref_images: list[str] = field(default_factory=list)
    duration: int = 5
    ratio: str = "16:9"
    resolution: str = "1080p"
    audio: bool = False           # 模型原生音效，默认关闭；用户明确要才开
    seed: int | None = None
    model: str | None = None
    negative: str | None = None

    @property
    def mode(self) -> str:
        if self.image and self.last_image:
            return "first-last"
        if self.image:
            return "i2v"
        if self.ref_images:
            return "ref"
        return "t2v"

    def summary(self) -> str:
        bits = [f"mode={self.mode}", f"{self.duration}s", self.ratio, self.resolution]
        if self.audio:
            bits.append("audio")
        return " ".join(bits)


@dataclass
class JobStatus:
    state: str                      # queued | running | succeeded | failed
    url: str | None = None
    error: str | None = None
    progress: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state in ("succeeded", "failed")


def nearest(value: int, allowed: list[int]) -> int:
    if not allowed:
        return value
    return min(allowed, key=lambda x: (abs(x - value), x))


def pick_resolution(value: str, allowed: list[str]) -> str:
    """把 1080P / 1080p / 720P 等写法归一到 provider 认可的值。"""
    low = (value or "").lower()
    for cand in allowed:
        if cand.lower() == low:
            return cand
    # 4k > 2k > 1080p > 768p > 720p > 540p > 480p
    order = ["4k", "2k", "1080p", "768p", "720p", "540p", "480p"]
    try:
        idx = order.index(low)
    except ValueError:
        idx = order.index("1080p")
    target = order[idx]
    for cand in allowed:
        if cand.lower() == target:
            return cand
    # 取不高于目标的最大档
    for t in order[idx:]:
        for cand in allowed:
            if cand.lower() == t:
                return cand
    return allowed[-1]


# ---------------------------------------------------------------- 基类


class Provider:
    name: str = ""
    label: str = ""
    base_url: str = ""
    models: list[str] = []
    durations: list[int] = []
    resolutions: list[str] = []
    ratios: list[str] = []
    supports: set[str] = set()
    price_hint: str = ""
    doc_url: str = ""
    key_help: str = ""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # -- 能力 ----------------------------------------------------

    def key(self) -> str:
        return self.cfg.key(self.name)

    def available(self) -> bool:
        return bool(self.key())

    def require_key(self) -> str:
        k = self.key()
        if not k:
            fail(f"{self.label} 未配置密钥。设置环境变量 {Config.KEY_ENVS[self.name]}，"
                 f"或写入项目 .env。申请地址：{self.key_help or self.doc_url}")
        return k

    def can(self, feature: str) -> bool:
        return feature in self.supports

    def pick_model(self, req: GenRequest) -> str:
        if req.model:
            return req.model
        return self.models[0]

    # -- 归一化 --------------------------------------------------

    def norm(self, req: GenRequest) -> GenRequest:
        req.duration = nearest(req.duration, self.durations) if self.durations else req.duration
        if self.resolutions:
            req.resolution = pick_resolution(req.resolution, self.resolutions)
        if self.ratios and req.ratio not in self.ratios:
            req.ratio = "9:16" if req.ratio in ("9:16", "3:4") and "9:16" in self.ratios else self.ratios[0]
        return req

    def check_support(self, req: GenRequest) -> None:
        mode = req.mode
        need = {"t2v": "t2v", "i2v": "i2v", "first-last": "last_frame", "ref": "ref"}[mode]
        if need not in self.supports:
            # 有降级方案的场景，这里不硬失败，交给调用方决定
            log(f"{self.label} 不支持 {mode}，已忽略额外输入", level="warn")

    # -- 待实现 --------------------------------------------------

    def submit(self, req: GenRequest) -> str:
        raise NotImplementedError

    def poll(self, job_id: str) -> JobStatus:
        raise NotImplementedError


# ---------------------------------------------------------------- 可灵 Kling


class Kling(Provider):
    name = "kling"
    label = "可灵 Kling"
    base_url = "https://api-beijing.klingai.com"
    models = ["kling-3.0", "kling-4.0"]
    durations = [5, 10]
    resolutions = ["720p", "1080p", "4k"]
    ratios = ["16:9", "9:16", "1:1"]
    supports = {"t2v", "i2v", "last_frame", "audio", "seed", "negative"}
    price_hint = "约 0.1~0.6 元/秒（按分辨率和模型浮动）"
    doc_url = "https://klingai.com/document-api/"
    key_help = "https://app.klingai.com/cn/dev/api-key"

    def pick_model(self, req: GenRequest) -> str:
        return req.model or "kling-3.0"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        model = self.pick_model(req)
        base = self.cfg.get("KLING_BASE_URL", self.base_url)

        contents: list[dict] = [{"type": "prompt", "text": req.prompt}]
        if req.image:
            contents.append({"type": "first_frame", "url": to_data_url(req.image)})
        if req.last_image:
            contents.append({"type": "last_frame", "url": to_data_url(req.last_image)})

        settings: dict[str, Any] = {
            "resolution": req.resolution,
            "duration": int(req.duration),
            "audio": "native" if req.audio else "off",
            "multi_shot": False,
            "aspect_ratio": req.ratio,
        }
        payload: dict[str, Any] = {"contents": contents, "settings": settings,
                                   "options": {"watermark_info": {"enabled": False}}}
        if req.negative:
            payload["options"]["negative_prompt"] = req.negative

        path = "/image-to-video" if req.image else "/text-to-video"
        r = request("POST", f"{base}{path}/{model}",
                    headers={"Authorization": f"Bearer {key}"}, body=payload)
        if r.get("code", 0) != 0:
            fail(f"可灵提交失败：{r.get('message') or r}")
        return str((r.get("data") or {}).get("id") or "")

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        base = self.cfg.get("KLING_BASE_URL", self.base_url)
        r = request("GET", f"{base}/tasks?task_ids={urllib.parse.quote(job_id)}",
                    headers={"Authorization": f"Bearer {key}"})
        items = r.get("data") or []
        if isinstance(items, dict):
            items = items.get("result", [])
        if not items:
            return JobStatus("running", raw=r)
        t = items[0]
        st = str(t.get("status", "")).lower()
        if st == "succeeded":
            for o in t.get("outputs") or []:
                if o.get("type") == "video" and o.get("url"):
                    return JobStatus("succeeded", url=o["url"], raw=t)
            return JobStatus("failed", error="任务成功但没有视频输出", raw=t)
        if st == "failed":
            return JobStatus("failed", error=t.get("message") or "生成失败", raw=t)
        return JobStatus("running" if st == "processing" else "queued", raw=t)


# ---------------------------------------------------------------- 火山方舟 Seedance（即梦）


class Ark(Provider):
    name = "ark"
    label = "即梦 Seedance（火山方舟）"
    base_url = "https://ark.cn-beijing.volces.com/api/v3"
    models = [
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedance-1-5-pro-251215",
        "doubao-seedance-2-0-260128",
    ]
    durations = [5, 10]
    resolutions = ["480p", "720p", "1080p"]
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"]
    supports = {"t2v", "i2v", "last_frame", "audio", "seed", "negative"}
    price_hint = "约 0.05~0.5 元/秒，pro-fast 最便宜"
    doc_url = "https://www.volcengine.com/docs/82379/1520757"
    key_help = "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        base = self.cfg.get("ARK_BASE_URL", self.base_url).rstrip("/")

        content: list[dict] = [{"type": "text", "text": req.prompt}]
        if req.image:
            content.append({"type": "image_url", "image_url": {"url": to_data_url(req.image)}})
        if req.last_image:
            content.append({
                "type": "image_url",
                "image_url": {"url": to_data_url(req.last_image)},
                "role": "last_frame",
            })
        for ref in req.ref_images:
            content.append({"type": "image_url", "image_url": {"url": to_data_url(ref)},
                            "role": "reference_image"})

        payload: dict[str, Any] = {
            "model": self.pick_model(req),
            "content": content,
            "duration": int(req.duration),
            "ratio": req.ratio,
            "resolution": req.resolution,
            "generate_audio": bool(req.audio),
            "watermark": False,
        }
        if req.seed is not None:
            payload["seed"] = int(req.seed)
        if req.negative:
            payload["negative_prompt"] = req.negative

        r = request("POST", f"{base}/contents/generations/tasks",
                    headers={"Authorization": f"Bearer {key}"}, body=payload)
        tid = r.get("id") or (r.get("data") or {}).get("id")
        if not tid:
            fail(f"方舟提交失败：{json.dumps(r, ensure_ascii=False)[:500]}")
        return str(tid)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        base = self.cfg.get("ARK_BASE_URL", self.base_url).rstrip("/")
        r = request("GET", f"{base}/contents/generations/tasks/{job_id}",
                    headers={"Authorization": f"Bearer {key}"})
        st = str(r.get("status", "")).lower()
        if st == "succeeded":
            url = (r.get("content") or {}).get("video_url")
            if url:
                return JobStatus("succeeded", url=url, raw=r)
            return JobStatus("failed", error="任务成功但没有 video_url", raw=r)
        if st in ("failed", "expired", "cancelled"):
            return JobStatus("failed", error=(r.get("error") or {}).get("message") or st, raw=r)
        return JobStatus("running" if st == "running" else "queued", raw=r)


# ---------------------------------------------------------------- 海螺 MiniMax


class MiniMax(Provider):
    name = "minimax"
    label = "海螺 Hailuo（MiniMax）"
    base_url = "https://api.minimaxi.com"
    models = ["MiniMax-Hailuo-2.3", "MiniMax-Hailuo-2.3-Fast", "MiniMax-Hailuo-02"]
    durations = [6, 10]
    resolutions = ["768P", "1080P"]
    ratios = ["16:9", "9:16", "1:1"]
    supports = {"t2v", "i2v", "last_frame"}
    price_hint = "约 0.3~0.9 元/秒"
    doc_url = "https://platform.minimaxi.com/document/video_generation"
    key_help = "https://platform.minimaxi.com/user-center/basic-information/interface-key"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        base = self.cfg.get("MINIMAX_BASE_URL", self.base_url).rstrip("/")

        payload: dict[str, Any] = {
            "model": self.pick_model(req),
            "prompt": req.prompt,
            "duration": int(req.duration),
            "resolution": req.resolution,
        }
        if req.image:
            payload["first_frame_image"] = to_data_url(req.image)
        if req.last_image:
            payload["last_frame_image"] = to_data_url(req.last_image)
        if req.ratio and req.ratio in ("16:9", "9:16", "1:1") and not req.image:
            payload["ratio"] = req.ratio

        r = request("POST", f"{base}/v1/video_generation",
                    headers={"Authorization": f"Bearer {key}"}, body=payload)
        tid = r.get("task_id")
        if not tid:
            msg = (r.get("base_resp") or {}).get("status_msg") or json.dumps(r, ensure_ascii=False)
            fail(f"海螺提交失败：{msg[:400]}")
        return str(tid)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        base = self.cfg.get("MINIMAX_BASE_URL", self.base_url).rstrip("/")
        hdr = {"Authorization": f"Bearer {key}"}
        r = request("GET", f"{base}/v1/query/video_generation?task_id={job_id}", headers=hdr)
        st = str(r.get("status", "")).lower()
        if st in ("success", "succeeded"):
            file_id = r.get("file_id")
            if not file_id:
                return JobStatus("failed", error="成功但没有 file_id", raw=r)
            fr = request("GET", f"{base}/v1/files/retrieve?file_id={file_id}", headers=hdr)
            url = ((fr.get("file") or {}).get("download_url")) or r.get("video_url")
            if not url:
                return JobStatus("failed", error="拿不到下载地址", raw=fr)
            return JobStatus("succeeded", url=url, raw=r)
        if st in ("fail", "failed"):
            return JobStatus("failed", error=r.get("status_msg") or "生成失败", raw=r)
        return JobStatus("running" if st == "processing" else "queued", raw=r)


# ---------------------------------------------------------------- Vidu


class Vidu(Provider):
    name = "vidu"
    label = "Vidu"
    base_url = "https://api.vidu.com"
    models = ["viduq2", "viduq3-pro", "viduq3-turbo"]
    durations = [4, 8]
    resolutions = ["540p", "720p", "1080p"]
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4"]
    supports = {"t2v", "i2v", "last_frame", "ref", "audio"}
    price_hint = "约 0.1~0.5 元/秒"
    doc_url = "https://platform.vidu.cn/docs/api-reference"
    key_help = "https://platform.vidu.cn/"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        base = self.cfg.get("VIDU_BASE_URL", self.base_url).rstrip("/")
        hdr = {"Authorization": f"Token {key}"}

        if req.image and req.last_image:
            path = "/ent/v2/start-end2video"
            images = [to_data_url(req.image), to_data_url(req.last_image)]
        elif req.image:
            path = "/ent/v2/img2video"
            images = [to_data_url(req.image)]
        elif req.ref_images:
            path = "/ent/v2/reference2video"
            images = [to_data_url(x) for x in req.ref_images[:7]]
        else:
            path = "/ent/v2/text2video"
            images = []

        payload: dict[str, Any] = {
            "model": self.pick_model(req),
            "prompt": req.prompt,
            "duration": int(req.duration),
            "resolution": req.resolution,
            "aspect_ratio": req.ratio,
            "watermark": False,
        }
        if images:
            payload["images"] = images
        if req.seed is not None:
            payload["seed"] = int(req.seed)
        if req.audio and self.pick_model(req).startswith("viduq3"):
            payload["audio"] = True

        r = request("POST", f"{base}{path}", headers=hdr, body=payload)
        tid = r.get("task_id")
        if not tid:
            fail(f"Vidu 提交失败：{json.dumps(r, ensure_ascii=False)[:400]}")
        return str(tid)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        base = self.cfg.get("VIDU_BASE_URL", self.base_url).rstrip("/")
        r = request("GET", f"{base}/ent/v2/tasks/{job_id}/creations",
                    headers={"Authorization": f"Token {key}"})
        st = str(r.get("state", "")).lower()
        if st == "success":
            for c in r.get("creations") or []:
                if c.get("url"):
                    return JobStatus("succeeded", url=c["url"], raw=r)
            return JobStatus("failed", error="成功但无视频地址", raw=r)
        if st == "failed":
            return JobStatus("failed", error=r.get("err_code") or "生成失败", raw=r)
        return JobStatus("running" if st == "processing" else "queued", raw=r)


# ---------------------------------------------------------------- Runway


class Runway(Provider):
    name = "runway"
    label = "Runway"
    base_url = "https://api.dev.runwayml.com"
    models = ["gen4_turbo", "gen4.5", "gen4_aleph", "veo3.1_fast"]
    durations = [5, 10]
    resolutions = []
    ratios = ["16:9", "9:16", "1:1"]
    supports = {"t2v", "i2v", "seed", "negative"}
    price_hint = "gen4_turbo 约 0.05 美元/秒（5 积分/秒）"
    doc_url = "https://docs.dev.runwayml.com/"
    key_help = "https://dev.runwayml.com/"

    RATIO_PX = {
        "16:9": "1280:720",
        "9:16": "720:1280",
        "1:1": "960:960",
        "4:3": "1104:832",
        "3:4": "832:1104",
        "21:9": "1584:672",
    }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_key()}",
            "X-Runway-Version": "2024-11-06",
        }

    def _upload(self, src: str) -> str:
        """Runway 必须先上传素材换 runway:// URI。"""
        if src.startswith("runway://"):
            return src
        base = self.cfg.get("RUNWAY_BASE_URL", self.base_url).rstrip("/")
        path = Path(src).expanduser()
        if not path.is_file():
            fail(f"Runway 需要本地图片文件：{src}")
        r = request("POST", f"{base}/v1/uploads", headers=self._headers(),
                    body={"filename": path.name,
                          "type": mimetypes.guess_type(path.name)[0] or "image/png"})
        upload_url = r.get("uploadUrl") or r.get("upload_url")
        uri = r.get("runwayUri") or r.get("runway_uri") or r.get("uri")
        if not upload_url or not uri:
            fail(f"Runway 上传初始化失败：{json.dumps(r, ensure_ascii=False)[:300]}")
        request("PUT", upload_url, body=path.read_bytes(),
                headers={"Content-Type": mimetypes.guess_type(path.name)[0] or "image/png"},
                raw=True)
        return str(uri)

    def submit(self, req: GenRequest) -> str:
        req = self.norm(req)
        base = self.cfg.get("RUNWAY_BASE_URL", self.base_url).rstrip("/")
        model = self.pick_model(req)
        ratio_px = self.RATIO_PX.get(req.ratio, "1280:720")

        payload: dict[str, Any] = {
            "model": model,
            "promptText": req.prompt,
            "duration": int(req.duration),
            "ratio": ratio_px,
        }
        if req.seed is not None:
            payload["seed"] = int(req.seed)

        if req.image:
            payload["promptImage"] = self._upload(req.image)
            if req.last_image:
                payload["lastFrame"] = self._upload(req.last_image)
            path = "/v1/image_to_video"
        else:
            path = "/v1/text_to_video"

        r = request("POST", f"{base}{path}", headers=self._headers(), body=payload)
        tid = r.get("id")
        if not tid:
            fail(f"Runway 提交失败：{json.dumps(r, ensure_ascii=False)[:400]}")
        return str(tid)

    def poll(self, job_id: str) -> JobStatus:
        base = self.cfg.get("RUNWAY_BASE_URL", self.base_url).rstrip("/")
        r = request("GET", f"{base}/v1/tasks/{job_id}", headers=self._headers())
        st = str(r.get("status", "")).upper()
        if st == "SUCCEEDED":
            out = r.get("output") or []
            if isinstance(out, str):
                out = [out]
            if out:
                return JobStatus("succeeded", url=out[0], raw=r)
            return JobStatus("failed", error="成功但无输出", raw=r)
        if st in ("FAILED", "CANCELLED"):
            return JobStatus("failed", error=r.get("failure") or st, raw=r)
        prog = int((r.get("progress") or 0) * 100) if isinstance(r.get("progress"), float) else 0
        return JobStatus("running" if st == "RUNNING" else "queued", progress=prog, raw=r)


# ---------------------------------------------------------------- Google Veo


class Veo(Provider):
    name = "veo"
    label = "Veo 3.1（Google）"
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    models = ["veo-3.1-generate-preview", "veo-3.1-fast-generate-preview"]
    durations = [4, 6, 8]
    resolutions = ["720p", "1080p", "4k"]
    ratios = ["16:9", "9:16"]
    supports = {"t2v", "i2v", "last_frame", "negative", "seed"}
    price_hint = "约 0.4 美元/秒起，最贵但带原生音效"
    doc_url = "https://ai.google.dev/gemini-api/docs/video"
    key_help = "https://aistudio.google.com/apikey"

    def _base(self) -> str:
        return self.cfg.get("VEO_BASE_URL", self.base_url).rstrip("/")

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        model = self.pick_model(req)

        inst: dict[str, Any] = {"prompt": req.prompt}
        if req.image:
            path = Path(req.image).expanduser()
            if is_url(req.image):
                inst["image"] = {"uri": req.image}
            else:
                inst["image"] = {"bytesBase64Encoded": to_b64(str(path)),
                                 "mimeType": "image/png" if path.suffix.lower() == ".png" else "image/jpeg"}
        if req.last_image and not is_url(req.last_image):
            p2 = Path(req.last_image).expanduser()
            inst["lastFrame"] = {"bytesBase64Encoded": to_b64(str(p2)),
                                 "mimeType": "image/png" if p2.suffix.lower() == ".png" else "image/jpeg"}

        params: dict[str, Any] = {
            "aspectRatio": req.ratio,
            "resolution": req.resolution,
            "durationSeconds": int(req.duration),
        }
        if req.negative:
            params["negativePrompt"] = req.negative
        if req.seed is not None:
            params["seed"] = int(req.seed)

        r = request("POST", f"{self._base()}/models/{model}:predictLongRunning",
                    headers={"x-goog-api-key": key},
                    body={"instances": [inst], "parameters": params})
        name = r.get("name")
        if not name:
            fail(f"Veo 提交失败：{json.dumps(r, ensure_ascii=False)[:400]}")
        return str(name)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        r = request("GET", f"{self._base()}/{job_id}", headers={"x-goog-api-key": key})
        if not r.get("done"):
            return JobStatus("running", raw=r)
        if r.get("error"):
            return JobStatus("failed", error=(r["error"] or {}).get("message") or "生成失败", raw=r)
        resp = r.get("response") or {}
        gvr = resp.get("generateVideoResponse") or resp
        samples = gvr.get("generatedSamples") or gvr.get("videos") or []
        if samples:
            uri = (samples[0].get("video") or {}).get("uri") or samples[0].get("gcsUri")
            if uri:
                sep = "&" if "?" in uri else "?"
                if uri.startswith("http"):
                    uri = f"{uri}{sep}key={key}"
                return JobStatus("succeeded", url=uri, raw=r)
        if resp.get("raiMediaFilteredCount"):
            return JobStatus("failed", error="被安全策略拦截，换个描述重试", raw=r)
        return JobStatus("failed", error="任务完成但没有视频", raw=r)


# ---------------------------------------------------------------- Luma


class Luma(Provider):
    name = "luma"
    label = "Luma Ray"
    base_url = "https://api.lumalabs.ai/dream-machine/v1"
    models = ["ray-2", "ray-flash-2"]
    durations = [5, 9]
    resolutions = ["540p", "720p", "1080p", "4k"]
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4"]
    supports = {"t2v", "i2v", "last_frame"}
    price_hint = "按次计费，ray-2 约 0.5 美元/条"
    doc_url = "https://docs.lumalabs.ai/docs/api"
    key_help = "https://lumalabs.ai/dream-machine/api"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        req = self.norm(req)
        base = self.cfg.get("LUMA_BASE_URL", self.base_url).rstrip("/")
        payload: dict[str, Any] = {
            "model": self.pick_model(req),
            "prompt": req.prompt,
            "aspect_ratio": req.ratio,
            "duration": f"{int(req.duration)}s",
            "resolution": req.resolution,
        }
        kf: dict[str, Any] = {}
        if req.image:
            kf["frame0"] = {"type": "image", "url": to_data_url(req.image)}
        if req.last_image:
            kf["frame1"] = {"type": "image", "url": to_data_url(req.last_image)}
        if kf:
            payload["keyframes"] = kf
        if req.negative:
            payload["callback_url"] = None
        r = request("POST", f"{base}/generations",
                    headers={"Authorization": f"Bearer {key}"}, body=payload)
        tid = r.get("id")
        if not tid:
            fail(f"Luma 提交失败：{json.dumps(r, ensure_ascii=False)[:400]}")
        return str(tid)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        base = self.cfg.get("LUMA_BASE_URL", self.base_url).rstrip("/")
        r = request("GET", f"{base}/generations/{job_id}",
                    headers={"Authorization": f"Bearer {key}"})
        st = str(r.get("state", "")).lower()
        if st == "completed":
            url = (r.get("assets") or {}).get("video")
            if url:
                return JobStatus("succeeded", url=url, raw=r)
            return JobStatus("failed", error="完成但无视频", raw=r)
        if st == "failed":
            return JobStatus("failed", error=r.get("failure_reason") or "生成失败", raw=r)
        return JobStatus("running" if st == "dreaming" else "queued", raw=r)


# ---------------------------------------------------------------- 注册表

PROVIDERS: dict[str, type[Provider]] = {
    "kling": Kling,
    "ark": Ark,
    "minimax": MiniMax,
    "vidu": Vidu,
    "runway": Runway,
    "veo": Veo,
    "luma": Luma,
}

# 自动选路偏好：按"性价比 + 国内可用性"排序
AUTO_ORDER = ["ark", "kling", "minimax", "vidu", "runway", "veo", "luma"]


def get_provider(name: str, cfg: Config) -> Provider:
    cls = PROVIDERS.get(name)
    if not cls:
        fail(f"未知 provider：{name}。可选：{', '.join(PROVIDERS)}")
    return cls(cfg)


def resolve_provider(name: str, cfg: Config, req: GenRequest) -> Provider:
    """auto 模式下按可用密钥 + 能力匹配挑一家。"""
    if name and name != "auto":
        p = get_provider(name, cfg)
        if not p.available():
            p.require_key()
        return p

    need = "last_frame" if (req.image and req.last_image) else ("i2v" if req.image else "t2v")
    if req.ref_images:
        need = "ref"

    candidates: list[Provider] = []
    for key in AUTO_ORDER:
        p = get_provider(key, cfg)
        if p.available():
            candidates.append(p)

    if not candidates:
        fail(
            "没有检测到任何可用的 API 密钥。\n"
            "  最快的方式：运行  python3 scripts/vs.py setup  按引导配置\n"
            "  或手动写入项目 .env，例如  ARK_API_KEY=你的密钥"
        )

    fitted = [p for p in candidates if need in p.supports]
    if fitted:
        return fitted[0]
    log(f"没有 provider 原生支持 {need}，回退到 {candidates[0].label}", level="warn")
    return candidates[0]
