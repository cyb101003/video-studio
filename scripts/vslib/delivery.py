"""平台交付与预览：把成片转成各平台规格、生成封面与候选对比图。

注意：本模块的能力一律「显式调用才执行」，默认不启用。
本项目遵循「没问清楚不许动手」的约束——除非调用方（vs.py 的命令）
明确要求导出或预览，否则不得自动触发任何转码、落盘或上传动作。
所有产出路径都由调用方通过 out / out_dir 参数决定，本模块不会往项目的
output/ 根目录写任何文件，也不会自行猜测路径。

底层全部基于本地 ffmpeg，复用 post.py 的 run / extract_frame /
duration_of / size_for / has_ffmpeg / _font 及 core.py 的 log / fail / pad，
不引入任何第三方依赖。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .core import display_width, fail, log, pad
from .post import _font, duration_of, extract_frame, has_ffmpeg, run, size_for

# ---------------------------------------------------------------- 预设

# max_duration 是「建议/常见上限（秒）」，仅作为规格提示供 list_presets 展示。
# export_variant 不会据此裁剪用户成片，避免「没问清楚就动手」。
PLATFORM_PRESETS: dict[str, dict] = {
    "douyin": {
        "name": "抖音", "w": 1080, "h": 1920, "fps": 30, "bitrate": "6M",
        "max_duration": 60, "note": "竖屏短视频，控制在 60 秒内更利于完播。",
    },
    "xiaohongshu": {
        "name": "小红书", "w": 1080, "h": 1440, "fps": 30, "bitrate": "6M",
        "max_duration": 300, "note": "竖屏图文社区，3:4 画幅的信息密度更友好。",
    },
    "bilibili": {
        "name": "哔哩哔哩", "w": 1920, "h": 1080, "fps": 30, "bitrate": "8M",
        "max_duration": 900, "note": "横屏中长视频，码率略高以保留画面细节。",
    },
    "youtube": {
        "name": "YouTube", "w": 1920, "h": 1080, "fps": 30, "bitrate": "10M",
        "max_duration": 900, "note": "横屏长视频，未验证账号单条默认上限约 15 分钟。",
    },
    "square": {
        "name": "方形通用", "w": 1080, "h": 1080, "fps": 30, "bitrate": "5M",
        "max_duration": 60, "note": "1:1 方形画幅，适配信息流与多平台通用投放。",
    },
}


def list_presets() -> str:
    """返回可直接打印的对齐表格（中文按终端显示宽度对齐，复用 core.pad）。"""
    headers = ["预设", "平台", "分辨率", "帧率", "码率", "建议上限", "说明"]
    rows: list[list[str]] = []
    for key, p in PLATFORM_PRESETS.items():
        rows.append([
            key, str(p["name"]), f"{p['w']}x{p['h']}", str(p["fps"]),
            str(p["bitrate"]), f"{p['max_duration']}s", str(p["note"]),
        ])
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))
    sep = "  "

    def fmt(cells: list[str]) -> str:
        return sep.join(pad(c, widths[i]) for i, c in enumerate(cells)).rstrip()

    lines = [
        "平台交付预设（显式调用才导出，默认不启用）：",
        fmt(headers),
        sep.join("-" * widths[i] for i in range(len(headers))),
    ]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- 内部工具


def _require_ffmpeg() -> None:
    """所有需要 ffmpeg 的公开能力统一在此检查，给出明确中文安装提示。"""
    if not has_ffmpeg():
        fail(
            "平台交付需要本地 ffmpeg，当前未安装。\n"
            "  macOS:   brew install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  安装后重新运行即可；本能力仅在显式调用时执行，默认不启用。"
        )


def _esc(s: str) -> str:
    """drawtext 文本转义，沿用 post.py title_card 的处理方式。"""
    return s.replace("\\", "").replace(":", r"\:").replace("'", "").replace("%", "")


def _font_arg() -> str:
    """仅在字体真实存在时拼 fontfile；缺失返回空串，避免生成非法参数。"""
    font = _font()
    return f":fontfile='{font}'" if font else ""


def _letter(i: int) -> str:
    """0->A, 1->B ... 25->Z, 26->AA，用于对比图上的候选项标签。"""
    n, out = i + 1, ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _safe_duration(src: Path) -> float:
    """duration_of 依赖 ffprobe；ffprobe 缺失时降级为 0，不牵连主流程。"""
    if shutil.which("ffprobe") is None:
        return 0.0
    try:
        return duration_of(src)
    except SystemExit:
        return 0.0


# ---------------------------------------------------------------- 单版本导出


def export_variant(src: Path, out: Path, preset: str, *, watermark: str = "") -> Path:
    """把成片转成目标平台规格：等比缩放+居中补黑边、重编码、可加右下角水印。"""
    _require_ffmpeg()
    if preset not in PLATFORM_PRESETS:
        fail(f"未知平台预设：{preset}（可用：{', '.join(PLATFORM_PRESETS)}）")
    src, out = Path(src), Path(out)
    if not src.is_file():
        fail(f"源视频不存在：{src}")
    p = PLATFORM_PRESETS[preset]
    out.parent.mkdir(parents=True, exist_ok=True)

    w, h, fps, br = p["w"], p["h"], p["fps"], p["bitrate"]
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )
    if watermark:
        vf += (
            f",drawtext=text='{_esc(watermark)}':fontcolor=white@0.65:fontsize=24"
            f":x=w-text_w-24:y=h-text_h-24"
            f":box=1:boxcolor=black@0.35:boxborderw=6{_font_arg()}"
        )

    run(
        ["-i", str(src), "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(br),
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(out)],
        desc=f"导出 {p['name']}({preset}) 版本",
    )
    return out


# ---------------------------------------------------------------- 候选对比图


def contact_sheet(clips: list[Path], out: Path, *, cols: int = 3,
                  label: bool = True) -> Path:
    """并排比较 A/B/C 候选：每片段抽一帧拼成 PNG 网格（默认带 A/B/C 标签）。"""
    _require_ffmpeg()
    if not clips:
        fail("contact_sheet 需要至少一个片段，但 clips 为空")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 临时抽帧放在 out 同级目录，结束后清理；不往项目 output/ 根目录写东西。
    tmpdir = Path(tempfile.mkdtemp(prefix=".vs_sheet_", dir=str(out.parent)))

    frames: list[Path] = []
    try:
        for i, clip in enumerate(clips):
            clip = Path(clip)
            dest = tmpdir / f"frame_{i:03d}.png"
            try:
                extract_frame(clip, dest, where="first")
            except (SystemExit, OSError):
                log(f"跳过无法抽帧的片段：{clip.name}", level="warn")
                dest.unlink(missing_ok=True)
                continue
            if dest.is_file() and dest.stat().st_size > 0:
                frames.append(dest)
            else:
                dest.unlink(missing_ok=True)
                log(f"跳过空帧片段：{clip.name}", level="warn")

        if not frames:
            fail("所有片段抽帧均失败，无法生成候选对比图")

        cols = max(1, min(int(cols), len(frames)))  # 片段不足时自动缩小列数
        cw, ch = size_for("16:9", 270)              # 复用 post.py → 480x270 单元
        font_arg = _font_arg()

        inputs: list[str] = []
        for f in frames:
            inputs += ["-i", str(f)]

        parts: list[str] = []
        for i in range(len(frames)):
            chain = (
                f"[{i}:v]format=yuv420p,"
                f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
            if label:
                chain += (
                    f",drawtext=text='{_letter(i)}':fontcolor=white:fontsize=28"
                    f":x=14:y=14:box=1:boxcolor=black@0.55:boxborderw=6{font_arg}"
                )
            parts.append(chain + f"[v{i}]")

        layout = "|".join(
            f"{(i % cols) * cw}_{(i // cols) * ch}" for i in range(len(frames))
        )
        parts.append(
            "".join(f"[v{i}]" for i in range(len(frames)))
            + f"xstack=inputs={len(frames)}:layout={layout}:fill=black[grid]"
        )
        run(
            [*inputs, "-filter_complex", ";".join(parts), "-map", "[grid]",
             "-frames:v", "1", str(out)],
            desc="拼接候选对比图",
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# ---------------------------------------------------------------- 封面帧


def cover_frame(src: Path, out: Path, *, at: float = 1.0) -> Path:
    """从成片指定秒数抽一帧作为封面（JPG/PNG 由 out 后缀决定）。"""
    _require_ffmpeg()
    src, out = Path(src), Path(out)
    if not src.is_file():
        fail(f"源视频不存在：{src}")
    out.parent.mkdir(parents=True, exist_ok=True)

    at = max(float(at), 0.0)
    total = _safe_duration(src)
    if total > 0 and at >= total:
        at = max(total - 0.1, 0.0)  # 超出时长时退到临近末尾，避免空输出

    def grab(lead: list[str]) -> None:
        run([*lead, "-frames:v", "1", "-update", "1", "-q:v", "2", str(out)],
            desc="生成封面帧")

    grab(["-ss", f"{at:.3f}", "-i", str(src)])
    if not out.is_file() or out.stat().st_size == 0:
        grab(["-sseof", "-0.15", "-i", str(src)])  # 兜底：取临近末帧
    return out


# ---------------------------------------------------------------- 批量导出


def export_all(src: Path, out_dir: Path, presets: list[str], *,
               watermark: str = "") -> list[Path]:
    """批量导出多个平台版本，返回产出路径列表；路径由 out_dir 决定。"""
    _require_ffmpeg()
    src, out_dir = Path(src), Path(out_dir)
    if not src.is_file():
        fail(f"源视频不存在：{src}")
    if not presets:
        fail("export_all 需要至少一个目标预设，但 presets 为空")
    unknown = [p for p in presets if p not in PLATFORM_PRESETS]
    if unknown:
        fail(f"未知平台预设：{', '.join(unknown)}")
    out_dir.mkdir(parents=True, exist_ok=True)

    outs: list[Path] = []
    for key in presets:
        out = out_dir / f"{src.stem}_{key}.mp4"
        outs.append(export_variant(src, out, key, watermark=watermark))
    return outs
