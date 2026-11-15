"""分镜脚本：结构、校验、时间轴。

storyboard.json 是整个流水线的唯一真相源。Agent 负责写内容，
本模块负责保证结构合法、时长可算、字幕能对齐。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import fail, log
from .post import estimate_seconds


@dataclass
class Shot:
    id: str
    prompt: str
    duration: int = 5
    desc: str = ""
    image: str | None = None
    last_image: str | None = None
    voiceover: str = ""
    subtitle: str = ""
    video: str = ""          # 生成后的本地相对路径
    note: str = ""

    @property
    def text_for_subtitle(self) -> str:
        return self.subtitle or self.voiceover


@dataclass
class Storyboard:
    title: str = "未命名项目"
    ratio: str = "16:9"
    resolution: str = "1080p"
    audio: bool = False          # 模型原生音效，用户确认后才置 True
    voiceover: bool = False
    bgm: str = ""                # 用户确认并提供文件后才填
    style: str = ""
    negative: str = ""
    transition: str = "none"     # 默认硬切，用户确认后才改
    transition_dur: float = 0.5
    font_size: int = 20
    shots: list[Shot] = field(default_factory=list)
    path: Path | None = None

    @property
    def total_duration(self) -> int:
        return sum(s.duration for s in self.shots)


def load(path: Path) -> Storyboard:
    if not path.is_file():
        fail(f"找不到分镜文件：{path}\n  用 `vs.py init` 先建一个项目")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"storyboard.json 格式错误：{e}")

    sb = Storyboard(path=path)
    for key in ("title", "ratio", "resolution", "bgm", "style", "negative", "transition"):
        if raw.get(key):
            setattr(sb, key, raw[key])
    for key in ("audio", "voiceover"):
        if key in raw:
            setattr(sb, key, bool(raw[key]))
    if "transition_dur" in raw:
        sb.transition_dur = float(raw["transition_dur"])
    if "font_size" in raw:
        sb.font_size = int(raw["font_size"])

    for i, item in enumerate(raw.get("shots") or [], 1):
        if isinstance(item, str):
            sb.shots.append(Shot(id=f"s{i}", prompt=item, desc=item))
            continue
        sb.shots.append(Shot(
            id=str(item.get("id") or f"s{i}"),
            prompt=str(item.get("prompt") or item.get("desc") or ""),
            duration=int(item.get("duration") or 5),
            desc=str(item.get("desc") or ""),
            image=item.get("image"),
            last_image=item.get("last_image"),
            voiceover=str(item.get("voiceover") or ""),
            subtitle=str(item.get("subtitle") or ""),
            video=str(item.get("video") or ""),
            note=str(item.get("note") or ""),
        ))
    return sb


def save(sb: Storyboard, path: Path | None = None) -> Path:
    p = path or sb.path
    if not p:
        fail("没有指定保存路径")
    data: dict[str, Any] = {
        "title": sb.title,
        "ratio": sb.ratio,
        "resolution": sb.resolution,
        "audio": sb.audio,
        "voiceover": sb.voiceover,
        "bgm": sb.bgm,
        "style": sb.style,
        "negative": sb.negative,
        "transition": sb.transition,
        "transition_dur": sb.transition_dur,
        "font_size": sb.font_size,
        "total_duration": sb.total_duration,
        "shots": [
            {k: v for k, v in {
                "id": s.id, "desc": s.desc, "prompt": s.prompt, "duration": s.duration,
                "image": s.image, "last_image": s.last_image,
                "voiceover": s.voiceover, "subtitle": s.subtitle,
                "video": s.video, "note": s.note,
            }.items() if v not in (None, "", 0)}
            for s in sb.shots
        ],
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def validate(sb: Storyboard) -> list[str]:
    problems: list[str] = []
    if not sb.shots:
        problems.append("shots 为空，至少需要一个镜头")
    for s in sb.shots:
        if not s.prompt.strip():
            problems.append(f"{s.id} 缺少 prompt")
        if s.duration <= 0:
            problems.append(f"{s.id} duration 必须大于 0")
        text = s.text_for_subtitle.strip()
        if text:
            need = estimate_seconds(text)
            if need > s.duration * 1.25:
                problems.append(
                    f"{s.id} 文案偏长：{len(text)} 字约需 {need:.1f}s，"
                    f"但镜头只有 {s.duration}s。建议缩短文案或延长镜头（字幕会被切成多条）"
                )
    if sb.total_duration > 180:
        problems.append(f"总时长 {sb.total_duration}s 偏长，建议拆成多集")
    return problems


def timeline(sb: Storyboard, overlap: float = 0.0) -> list[tuple[float, float, str]]:
    """按镜头时长推出字幕时间轴。

    overlap 是转场重叠秒数。xfade 会让相邻片段重叠，时间轴必须同步压缩，
    否则字幕越往后越提前（每处转场漂移 overlap 秒）。
    """
    cues: list[tuple[float, float, str]] = []
    cursor = 0.0
    last = len(sb.shots) - 1
    for idx, s in enumerate(sb.shots):
        text = s.text_for_subtitle.strip()
        span = float(s.duration)
        step = span - (overlap if idx < last else 0.0)
        if text:
            est = estimate_seconds(text)
            if est > span * 0.95:
                # 一镜念不完 → 按句切分，铺满整个镜头
                start = cursor
                for txt, d in _split_caption(text, span):
                    cues.append((cursor, cursor + d, txt))
                    cursor += d
                cursor = start + step
                continue
            # 字幕至少停留镜头时长的 60%，避免闪一下就没
            hold = min(max(est * 1.3, span * 0.6), span - 0.15)
            cues.append((cursor, cursor + max(hold, 1.0), text))
        cursor += step
    return cues


def _split_caption(text: str, span: float) -> list[tuple[str, float]]:
    """把一镜塞不下的长文案按标点切成多条，时长按字数比例分配。"""
    import re

    chunks = [c for c in re.split(r"(?<=[。！？；!?;，,])", text) if c.strip()]
    if not chunks:
        return [(text, span)]

    # 贪心合并成不超过 16 字的字幕行
    lines: list[str] = []
    buf = ""
    for c in chunks:
        if buf and len(buf) + len(c) > 16:
            lines.append(buf)
            buf = c
        else:
            buf += c
    if buf:
        lines.append(buf)

    total = sum(len(t) + 6 for t in lines)  # +6 给短句留最低存在感
    return [(t, span * (len(t) + 6) / total) for t in lines]


def scaffold(title: str, ratio: str = "16:9", n: int = 4) -> Storyboard:
    sb = Storyboard(title=title, ratio=ratio)
    for i in range(1, n + 1):
        sb.shots.append(Shot(id=f"s{i}", desc=f"第 {i} 镜", prompt="", duration=5))
    return sb


def report(sb: Storyboard) -> str:
    lines = [
        f"项目：{sb.title}",
        f"规格：{sb.ratio} / {sb.resolution} / 共 {len(sb.shots)} 镜 / {sb.total_duration}s",
    ]
    if sb.style:
        lines.append(f"风格：{sb.style}")
    lines.append("")
    for s in sb.shots:
        mark = "✓" if s.video else "·"
        lines.append(f"  {mark} {s.id:<4} {s.duration}s  {s.desc or s.prompt[:40]}")
        if s.video:
            lines.append(f"        {s.video}")
    return "\n".join(lines)
