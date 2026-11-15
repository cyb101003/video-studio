"""网关聚合层：一个 key 打通多模型 + 对话式改片 + 素材生成。

包含三块：
  Fal / HeyGen  —— 作为 Provider 注册进主流程，run/gen 可直接使用
  omni_edit     —— Gemini Omni Interactions API，多轮对话式局部改片
  generate_media—— 走 fal 通用 queue 生成静帧 / 音乐 / 音效 / 视频素材

端点结构核对自 fal.ai Queue API 文档、HeyGen 官方 skills 仓库、
Google Gemini Omni / Interactions API 文档，核对日期 2026-11-15。
**尚未用真实密钥跑通**：假密钥只能验证端点与鉴权头，不能验证响应字段。
所有新增能力默认都关闭，必须用户明确要求后才调用。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .core import Config, HTTPError, download, fail, log, request, to_b64, to_data_url
from .providers import GenRequest, JobStatus, Provider

STRUCTURE_VERIFIED_AS_OF = "2026-11-15"

# fal 市场里已经核实存在的 model-id。其余模型不必硬编码：
# 通用 queue 适配器接受任意 model-id，用 --model 传入即可（见 references/gateways.md）。
FAL_VERIFIED_MODELS = [
    "alibaba/wan-3.0/text-to-video",
    "fal-ai/wan/v2.7/reference-to-video",
    "fal-ai/flux/schnell",
    "fal-ai/nano-banana-2",
]

# HeyGen GenerateVideoNode 公开的 provider 取值（来自其官方 skills 仓库 SKILL.md）
HEYGEN_PROVIDERS = [
    "veo_3_1", "veo_3_1_fast", "veo3", "veo3_fast", "veo2",
    "kling_pro", "kling_v2", "sora_v2", "sora_v2_pro",
    "runway_gen4", "seedance_lite", "seedance_pro", "ltx_distilled",
]


class Fal(Provider):
    """fal.ai 通用队列网关。

    submit: POST https://queue.fal.run/{model-id}
    status: GET  https://queue.fal.run/{model-id}/requests/{id}/status
    result: GET  https://queue.fal.run/{model-id}/requests/{id}
    鉴权头是 `Authorization: Key $FAL_KEY`（不是 Bearer）。
    """

    name = "fal"
    label = "fal.ai 网关"
    base_url = "https://queue.fal.run"
    models = FAL_VERIFIED_MODELS
    durations = [2, 3, 4, 5, 6, 7, 8, 9, 10]
    resolutions = ["720p", "1080p"]
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4"]
    supports = {"t2v", "i2v", "ref", "seed"}
    price_hint = "按输出计费，各模型不同；失败不扣费"
    doc_url = "https://docs.fal.ai"
    key_help = "https://fal.ai/dashboard/keys"

    def pick_model(self, req: GenRequest) -> str:
        if req.model:
            return req.model
        if req.ref_images:
            return FAL_VERIFIED_MODELS[1]
        return FAL_VERIFIED_MODELS[0]

    def _url(self, model: str) -> str:
        return f"{self.base_url}/{model.strip('/')}"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        model = self.pick_model(req)
        payload: dict[str, Any] = {
            "prompt": req.prompt,
            "aspect_ratio": req.ratio if req.ratio in self.ratios else "16:9",
            "resolution": req.resolution.lower(),
            "duration": str(nearest_duration(req.duration)),
        }
        if req.negative:
            payload["negative_prompt"] = req.negative
        if req.seed is not None:
            payload["seed"] = req.seed
        if req.image:
            payload["image_url"] = to_data_url(req.image) if not str(req.image).startswith(("http://", "https://")) else req.image
        if req.ref_images:
            payload["reference_image_urls"] = [
                to_data_url(x) if not str(x).startswith(("http://", "https://")) else x
                for x in req.ref_images[:6]
            ]
        r = request("POST", self._url(model),
                    headers={"Authorization": f"Key {key}"}, body=payload)
        rid = r.get("request_id") or (r.get("data") or {}).get("request_id")
        if not rid:
            fail(f"fal.ai 提交后没拿到 request_id：{str(r)[:200]}")
        # 把 model 编进 job_id，poll 只需这一串就能重建 URL
        return f"{model}|{rid}"

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        if "|" not in job_id:
            return JobStatus("failed", error=f"fal 任务 ID 格式异常：{job_id}")
        model, rid = job_id.split("|", 1)
        base = f"{self._url(model)}/requests/{urllib.parse.quote(rid, safe='')}"
        try:
            st = request("GET", f"{base}/status",
                         headers={"Authorization": f"Key {key}"})
        except HTTPError as e:
            return JobStatus("failed", error=f"查询状态失败：HTTP {e.status} {e.body[:120]}")
        state_raw = str(st.get("status") or "").upper()
        if state_raw in ("FAILED", "ERROR", "CANCELLED"):
            return JobStatus("failed", error=str(st.get("error") or st.get("detail") or state_raw))
        if state_raw not in ("COMPLETED", "SUCCEEDED"):
            return JobStatus("running" if state_raw == "IN_PROGRESS" else "queued",
                             progress=int(st.get("queue_position") or 0), raw=st)
        try:
            out = request("GET", base, headers={"Authorization": f"Key {key}"})
        except HTTPError as e:
            return JobStatus("failed", error=f"取结果失败：HTTP {e.status} {e.body[:120]}")
        url = _media_url(out)
        if not url:
            return JobStatus("failed", error=f"结果里找不到媒体 URL：{str(out)[:180]}")
        return JobStatus("succeeded", url=url, raw=out)


class HeyGen(Provider):
    """HeyGen GenerateVideoNode 工作流网关。一个 key 覆盖 Veo/Kling/Sora/Runway/Seedance。"""

    name = "heygen"
    label = "HeyGen 网关"
    base_url = "https://api.heygen.com"
    models = HEYGEN_PROVIDERS
    durations = [4, 5, 6, 8, 10]
    resolutions = ["720p", "1080p"]
    ratios = ["16:9", "9:16", "1:1"]
    supports = {"t2v", "i2v", "last_frame"}
    price_hint = "按工作流计费，以控制台为准"
    doc_url = "https://docs.heygen.com"
    key_help = "https://app.heygen.com/settings?tab=api"

    def pick_model(self, req: GenRequest) -> str:
        return req.model or "veo_3_1"

    def submit(self, req: GenRequest) -> str:
        key = self.require_key()
        inp: dict[str, Any] = {
            "prompt": req.prompt,
            "provider": self.pick_model(req),
            "aspect_ratio": req.ratio if req.ratio in self.ratios else "16:9",
        }
        if req.image:
            inp["reference_image_url"] = req.image
        if req.last_image:
            inp["tail_image_url"] = req.last_image
        r = request("POST", f"{self.base_url}/v1/workflows/executions",
                    headers={"X-Api-Key": key},
                    body={"workflow_type": "GenerateVideoNode", "input": inp})
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        eid = (r.get("execution_id") or data.get("execution_id")
               or r.get("id") or data.get("id"))
        if not eid:
            fail(f"HeyGen 提交后没拿到 execution_id：{str(r)[:200]}")
        return str(eid)

    def poll(self, job_id: str) -> JobStatus:
        key = self.require_key()
        try:
            r = request("GET", f"{self.base_url}/v1/workflows/executions/{job_id}",
                        headers={"X-Api-Key": key})
        except HTTPError as e:
            return JobStatus("failed", error=f"查询失败：HTTP {e.status} {e.body[:120]}")
        data = r.get("data") if isinstance(r.get("data"), dict) else r
        raw_state = str(data.get("status") or r.get("status") or "").lower()
        if raw_state in ("failed", "error", "cancelled"):
            return JobStatus("failed", error=str(data.get("error") or raw_state))
        if raw_state in ("completed", "succeeded", "success"):
            out = data.get("output") if isinstance(data.get("output"), dict) else {}
            url = out.get("video_url") or data.get("video_url") or _media_url(out)
            if not url:
                return JobStatus("failed", error=f"完成但没找到 video_url：{str(r)[:180]}")
            return JobStatus("succeeded", url=url, raw=r)
        return JobStatus("running" if raw_state in ("running", "processing", "in_progress") else "queued",
                         raw=r)


def nearest_duration(value: int) -> int:
    return max(2, min(10, int(value)))


def _media_url(obj: Any) -> str | None:
    """从 fal 各家模型的结果里找媒体 URL（video / images / audio 三种 schema）。"""
    if not isinstance(obj, dict):
        return None
    for path in (("video", "url"), ("audio", "url"), ("output", "video", "url")):
        cur: Any = obj
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur.startswith("http"):
            return cur
    for key in ("video_url", "audio_url", "url", "audio"):
        v = obj.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    imgs = obj.get("images")
    if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
        u = imgs[0].get("url")
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


# ---------------------------------------------------------------- 通用素材生成


def generate_media(cfg: Config, *, kind: str, prompt: str, model: str | None = None,
                   out: Path, timeout: int = 600, interval: int = 6,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """走 fal 通用 queue 生成静帧 / 音乐 / 音效 / 视频素材，并下载到本地。

    kind 只影响默认 model 与提示文案；实际能力取决于所选 fal 模型。
    返回 {"file", "model", "raw"}。需要 FAL_KEY，且**未做真实端到端验证**。
    """
    key = cfg.key("fal")
    if not key:
        fail("素材生成需要 fal.ai 密钥（FAL_KEY）。运行 vs.py setup 查看申请地址。")
    defaults = {
        "image": "fal-ai/flux/schnell",
        "music": "fal-ai/mmaudio-v2",
        "sfx": "fal-ai/mmaudio-v2",
        "video": FAL_VERIFIED_MODELS[0],
    }
    if kind not in defaults:
        fail(f"未知素材类型 {kind}，可选：{', '.join(defaults)}")
    mid = model or defaults[kind]
    payload: dict[str, Any] = {"prompt": prompt}
    payload.update(extra or {})
    url = f"https://queue.fal.run/{mid.strip('/')}"
    hdr = {"Authorization": f"Key {key}"}
    r = request("POST", url, headers=hdr, body=payload)
    rid = r.get("request_id")
    if not rid:
        fail(f"提交后没拿到 request_id：{str(r)[:200]}")
    log(f"[{mid}] 已提交 {rid}", level="step")
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = request("GET", f"{url}/requests/{urllib.parse.quote(rid, safe='')}/status", headers=hdr)
        state = str(st.get("status") or "").upper()
        if state in ("FAILED", "ERROR", "CANCELLED"):
            fail(f"生成失败：{st.get('error') or state}")
        if state in ("COMPLETED", "SUCCEEDED"):
            result = request("GET", f"{url}/requests/{urllib.parse.quote(rid, safe='')}", headers=hdr)
            media = _media_url(result)
            if not media:
                fail(f"结果里找不到媒体 URL：{str(result)[:200]}")
            out.parent.mkdir(parents=True, exist_ok=True)
            download(media, out)
            log(f"素材已保存：{out}", level="ok")
            return {"file": str(out), "model": mid, "raw": result}
        time.sleep(interval)
    fail(f"等待超时（{timeout}s），可稍后重试")


# ---------------------------------------------------------------- 对话式改片


def omni_edit(cfg: Config, *, prompt: str, video: Path | None = None,
              video_uri: str | None = None, previous_id: str | None = None,
              out: Path, aspect_ratio: str = "16:9", duration: int | None = None,
              timeout: int = 300) -> dict[str, Any]:
    """Gemini Omni 对话式局部改片 / 续写，返回 {"id","file"}。

    同步 Interactions API：POST /v1beta/interactions，结果在
    steps[].content[].data（base64）或 .uri。多轮用 previous_interaction_id。
    本地视频以 base64 inline 传入，超过 4MB 会拒绝并提示改用 Files API。
    """
    key = cfg.key("veo") or cfg.get("GEMINI_API_KEY")
    if not key:
        fail("对话式改片需要 GEMINI_API_KEY（与 Veo 共用一个 key）。")
    content: list[dict[str, Any]] = []
    if video is not None:
        data = video.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            fail("内联视频超过 4MB。请先上传到 Files API 后用 --video-uri 传入（本脚本未实现上传）。")
        content.append({"type": "video", "data": base64.b64encode(data).decode(),
                        "mime_type": "video/mp4"})
    elif video_uri:
        content.append({"type": "video", "uri": video_uri})
    elif not previous_id:
        fail("改片需要提供 --video / --video-uri，或用 --previous 接着上一轮改。")
    content.append({"type": "text", "text": prompt})

    body: dict[str, Any] = {
        "model": "gemini-omni-1.2-flash",
        "input": content,
        "store": True,
        "response_format": {"type": "video", "aspect_ratio": aspect_ratio, "delivery": "inline"},
    }
    if duration:
        body["response_format"]["duration"] = f"{max(3, min(15, duration))}s"
    if previous_id:
        body["previous_interaction_id"] = previous_id

    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    r = request("POST", f"{endpoint}?key={urllib.parse.quote(key)}", body=body, timeout=timeout)
    iid = r.get("id") or ""
    blob: str | None = None
    uri: str | None = None
    for step in r.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for c in step.get("content") or []:
            if c.get("type") == "video":
                blob = c.get("data")
                uri = c.get("uri")
    if not blob and not uri:
        fail(f"改片结果里找不到视频：{json.dumps(r, ensure_ascii=False)[:250]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if blob:
        out.write_bytes(base64.b64decode(blob))
    elif uri:
        download(f"{uri}&key={key}" if "?" in uri else f"{uri}?key={key}", out)
    log(f"改片完成：{out}（interaction {iid}）", level="ok")
    return {"id": iid, "file": str(out), "uri": uri or ""}
