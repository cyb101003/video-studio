"""生产管线：制作圣经、候选版本、质检、尾帧链。

多镜头 AI 视频翻车，九成不是 API 调错，而是这四件事没做：
  - 每镜独立生成，角色/服装/光线逐镜漂移
  - 中途换模型，风格直接断
  - 没做便宜的预演，花钱之后才发现节奏不对
  - 没存 seed / prompt / 参考图，想重拍只能从零开始

本模块把这些约束做成可落盘、可复现、可检查的数据。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .core import log, state_dir
from .post import estimate_seconds
from . import storyboard as sbmod

BIBLE_FILE = "bible.json"


# ---------------------------------------------------------------- 制作圣经


@dataclass
class Bible:
    """一份写一次、每个镜头逐字复用的制作约束。

    identity 是「角色主文档」：脸部特征、发型、服装、配饰、体态，
    用词必须一字不改地出现在每个镜头 prompt 的最前面。
    """

    identity: str = ""
    style: str = ""
    lighting: str = ""
    negative: str = ""
    references: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    seed: int | None = None
    locked: bool = False
    path: Path | None = None

    @classmethod
    def load(cls, root: Path) -> "Bible":
        p = root / BIBLE_FILE
        b = cls(path=p)
        if not p.is_file():
            return b
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log("bible.json 读取失败，按空圣经处理", level="warn")
            return b
        for key in ("identity", "style", "lighting", "negative", "provider", "model"):
            if isinstance(raw.get(key), str):
                setattr(b, key, raw[key])
        if isinstance(raw.get("references"), list):
            b.references = [str(x) for x in raw["references"]]
        if raw.get("seed") is not None:
            try:
                b.seed = int(raw["seed"])
            except (TypeError, ValueError):
                pass
        b.locked = bool(raw.get("locked", False))
        return b

    def save(self, root: Path | None = None) -> Path:
        p = (root / BIBLE_FILE) if root else self.path
        if not p:
            raise ValueError("没有指定 bible.json 路径")
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("path", None)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path = p
        return p

    def compose_prompt(self, shot_prompt: str, sb: Any) -> str:
        """把固定块拼到镜头描述前面。身份块逐字不变，只有动作/运镜在变。"""
        fixed: list[str] = []
        if self.identity.strip():
            fixed.append(self.identity.strip().rstrip("。"))
        style = (self.style or getattr(sb, "style", "") or "").strip()
        if style:
            fixed.append(f"统一风格：{style.rstrip('。')}")
        if self.lighting.strip():
            fixed.append(f"光线：{self.lighting.strip().rstrip('。')}")
        body = shot_prompt.strip().rstrip("。")
        if not fixed:
            return shot_prompt
        return "。".join(fixed + [body]) + "。"

    def neg(self, sb: Any) -> str | None:
        return (self.negative or getattr(sb, "negative", "") or "") or None

    def check(self, root: Path) -> list[str]:
        """只报「确定的错误」。seed / identity 属于建议，交给 qc 按项目规模提示，
        否则纯风景、单镜项目也会被无意义地判成有问题。"""
        issues: list[str] = []
        for ref in self.references:
            if not (root / ref).is_file():
                issues.append(f"参考图缺失：{ref}")
        # 没指定 provider 不算问题：run 会把首次选型写进 .vs/model-lock.json 自动锁定
        return issues


# ---------------------------------------------------------------- 候选版本


class TakesStore:
    """每镜 A/B/C 候选版本。永不覆盖，选定后回填 storyboard。"""

    LABELS = "ABCDEFGH"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = state_dir(root) / "takes.json"
        self.data: dict[str, dict[str, dict]] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def next_label(self, shot: str) -> str:
        used = set(self.data.get(shot, {}))
        for c in self.LABELS:
            if c not in used:
                return c
        return str(len(used) + 1)

    def record(self, shot: str, label: str, info: dict) -> None:
        self.data.setdefault(shot, {})[label] = info
        self.save()

    def for_shot(self, shot: str) -> dict[str, dict]:
        return self.data.get(shot, {})

    def get(self, shot: str, label: str) -> dict | None:
        return self.for_shot(shot).get(label.upper())

    def all_shots(self) -> list[str]:
        return sorted(self.data)


# ---------------------------------------------------------------- 尾帧链


def chain_plan(root: Path, sb: sbmod.Storyboard) -> list[dict]:
    """列出「上一镜末帧 → 下一镜首帧」的抽取计划，不写文件。"""
    out: list[dict] = []
    shots = sb.shots
    for i in range(1, len(shots)):
        prev, nxt = shots[i - 1], shots[i]
        if not prev.video:
            continue
        src = root / prev.video
        if not src.is_file():
            continue
        dest = root / "images" / "chain" / f"{nxt.id}-first.png"
        out.append({"from": prev.id, "to": nxt.id, "src": src,
                    "dest": dest, "current": nxt.image or ""})
    return out


# ---------------------------------------------------------------- 质检


MANUAL_QC = [
    "把每镜的首帧和末帧并排看：脸型、发型、服装颜色、道具是否一致",
    "相邻两镜：光源方向是否一致、主体是否在同一侧、运动方向是否连续",
    "全景镜头里人脸是否小于画面 20%（小于就容易换脸）→ 改中近景",
    "头尾各 0.5 秒有没有形变、闪烁、糊化（剪辑时留 handles 裁掉）",
    "动作是否在剪辑点处连贯（转身、开门、物体掠过时切最自然）",
    "所有镜头统一调色 / 一套 LUT，别让不同模型的色温暴露出来",
    "通片看一遍节奏：有没有重复信息的镜头、有没有早到的结尾",
]


def qc(root: Path, sb: sbmod.Storyboard, bible: Bible,
       takes: TakesStore, jobs: list[Any]) -> tuple[list[str], list[str], list[str]]:
    """返回 (确定的问题, 人工过目清单, 按项目规模给的建议)。"""
    issues: list[str] = []
    tips: list[str] = []
    if not sb.shots:
        issues.append("分镜为空")

    missing = [s.id for s in sb.shots if not s.video or
               not (root / s.video).is_file() or
               (root / s.video).stat().st_size <= 1024]
    if missing:
        issues.append(f"{len(missing)} 镜还没有可用片段：{', '.join(missing)}")

    for s in sb.shots:
        text = s.text_for_subtitle.strip()
        if text and estimate_seconds(text) > s.duration * 1.25:
            issues.append(f"{s.id} 字幕文案偏长，镜头装不下")

    # 全项目必须同一 provider / model
    used: dict[str, set[str]] = {"provider": set(), "model": set()}
    for j in jobs:
        if getattr(j, "shot", ""):
            used["provider"].add(getattr(j, "provider", ""))
            used["model"].add(getattr(j, "model", ""))
    if len(used["provider"]) > 1:
        issues.append(f"混用了多个模型商 {sorted(used['provider'])}，风格必然漂移；"
                      "用 bible --set provider=... 锁定后重跑有问题的镜头")
    if len(used["model"]) > 1:
        issues.append(f"混用了多个模型版本 {sorted(used['model'])}，建议统一")

    issues.extend(bible.check(root))
    # 纯风景 / 单镜项目按 pipeline.md 可以跳过制作圣经，这里只给建议不判错
    if bible.seed is None:
        tips.append("未设置 seed，结果不可复现（bible --set seed=数字）；有人物/产品的项目建议设")
    if not bible.identity.strip():
        tips.append("未填 identity 身份块；片中有同一个人物或产品时必填，否则多镜容易换脸")
    if len(sb.shots) < 3:
        tips.append("镜头数少，纯风景/抽象内容可跳过静帧、尾帧链与多候选")
    for s in sb.shots:
        if s.image and "chain/" in s.image and not (root / s.image).is_file():
            issues.append(f"{s.id} 的首帧图（尾帧链）缺失：{s.image}")

    total = sb.total_duration
    if total > 60 and len(sb.shots) < total / 6:
        issues.append(f"{total}s 只有 {len(sb.shots)} 镜，单镜偏长容易动作失控；建议 3–8s 一镜")

    return issues, list(MANUAL_QC), tips
