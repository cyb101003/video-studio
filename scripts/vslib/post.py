"""后期成片：ffmpeg 归一化、拼接、字幕、配音、配乐。

设计原则：用户的片段来自不同模型，分辨率/帧率/音轨各不相同，
直接 concat 一定失败。所以一律「先归一化 → 再拼接」。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .core import Config, download, fail, log, request

# ---------------------------------------------------------------- 基础


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        fail(
            "没有找到 ffmpeg，后期合成需要它。\n"
            "  macOS:   brew install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  装好后重新运行即可（生成片段的部分不受影响）。"
        )
    return exe


def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        fail("没有找到 ffprobe，请安装完整版 ffmpeg")
    return exe


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def run(args: list[str], *, desc: str = "", quiet: bool = True) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        fail(f"{desc or 'ffmpeg'} 失败：\n  " + "\n  ".join(tail) + f"\n  命令：{' '.join(cmd[:12])} ...")


def probe(path: Path) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def duration_of(path: Path) -> float:
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def extract_frame(src: Path, dest: Path, *, where: str = "last") -> Path:
    """抽一帧存成 PNG。尾帧链用 where="last"，把上一镜末帧接到下一镜首帧。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    lead = ["-sseof", "-0.15", "-i", str(src)] if where == "last" else ["-i", str(src)]
    run([*lead, "-frames:v", "1", "-update", "1", "-q:v", "2", str(dest)],
        desc=f"抽{'末' if where == 'last' else '首'}帧")
    return dest


RATIO_WH = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "21:9": (21, 9),
}

# 短边 1080 时的成片尺寸，供快速查阅
RATIO_SIZE = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
    "21:9": (2520, 1080),
}


def size_for(ratio: str, short: int = 1080) -> tuple[int, int]:
    """按「短边 = short」推出画布尺寸。竖屏 9:16 + 1080 → 1080x1920。"""
    a, b = RATIO_WH.get(ratio, (16, 9))
    if a >= b:
        w, h = round(short * a / b), short
    else:
        w, h = short, round(short * b / a)
    return w - (w % 2), h - (h % 2)


# ---------------------------------------------------------------- 归一化


def normalize(src: Path, dest: Path, *, w: int = 1920, h: int = 1080,
              fps: int = 30, keep_audio: bool = True) -> Path:
    """缩放+补边到统一规格，音轨统一为 48k 立体声。没有音轨就补静音。"""
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}")

    info = probe(src)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))

    args: list[str] = ["-i", str(src)]
    if keep_audio and has_audio:
        args += ["-map", "0:v:0", "-map", "0:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2"]

    args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest)]
    run(args, desc=f"归一化 {src.name}")
    return dest


# ---------------------------------------------------------------- 拼接


def concat(clips: list[Path], out: Path) -> Path:
    """硬切拼接。输入必须已归一化到相同规格。"""
    if len(clips) == 1:
        shutil.copy2(clips[0], out)
        return out
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        for c in clips:
            fh.write(f"file '{c.resolve().as_posix()}'\n")
        listfile = Path(fh.name)
    try:
        run(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy",
             "-movflags", "+faststart", str(out)], desc="拼接")
    finally:
        listfile.unlink(missing_ok=True)
    return out


def concat_xfade(clips: list[Path], out: Path, *, transition: str = "fade",
                 dur: float = 0.5, fps: int = 30) -> Path:
    """带转场的拼接（xfade）。片段多时自动降级为硬切以免命令过长。"""
    if len(clips) < 2 or len(clips) > 12:
        return concat(clips, out)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]

    filter_parts: list[str] = []
    # 先统一每个输入的时间基
    for i, c in enumerate(clips):
        filter_parts.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}]")
        filter_parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")

    cur_v, cur_a = "v0", "a0"
    offset = 0.0
    for i in range(1, len(clips)):
        offset += max(duration_of(clips[i - 1]) - dur, 0.1)
        nv, na = f"vx{i}", f"ax{i}"
        filter_parts.append(
            f"[{cur_v}][v{i}]xfade=transition={transition}:duration={dur}:offset={offset:.3f}[{nv}]"
        )
        filter_parts.append(f"[{cur_a}][a{i}]acrossfade=d={dur}[{na}]")
        cur_v, cur_a = nv, na

    filter_complex = ";".join(filter_parts)
    run([*inputs, "-filter_complex", filter_complex, "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", "-r", str(fps), "-movflags", "+faststart", str(out)],
        desc="转场拼接")
    return out


# ---------------------------------------------------------------- 字幕


def srt_time(t: float) -> str:
    if t < 0:
        t = 0
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((t - int(t)) * 1000):03d}"


def write_srt(cues: list[tuple[float, float, str]], path: Path) -> Path:
    lines: list[str] = []
    for i, (start, end, text) in enumerate(cues, 1):
        lines += [str(i), f"{srt_time(start)} --> {srt_time(end)}", text.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def burn_subtitles(src: Path, srt: Path, out: Path, *, font_size: int = 20,
                   color: str = "&H00FFFFFF", outline: str = "&H00000000",
                   margin_v: int = 60) -> Path:
    """把 srt 烧进画面。用 ass 样式控制字号/描边/位置。"""
    srt_arg = str(srt.resolve()).replace("\\", "/").replace(":", r"\:")
    style = (f"FontName=PingFang SC,Fontsize={font_size},PrimaryColour={color},"
             f"OutlineColour={outline},BorderStyle=1,Outline=2,Shadow=1,"
             f"Alignment=2,MarginV={margin_v}")
    vf = f"subtitles='{srt_arg}':force_style='{style}'"
    run(["-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "copy", "-movflags", "+faststart", str(out)], desc="烧字幕")
    return out


# ---------------------------------------------------------------- 配乐 / 混音


def add_bgm(src: Path, bgm: Path, out: Path, *, bgm_volume: float = 0.18,
            fade_out: float = 2.0, duck: bool = True) -> Path:
    """铺背景音乐。duck=True 时对人声做侧链压缩，音乐自动避让人声。"""
    total = duration_of(src)
    if total <= 0:
        fail("无法读取视频时长")

    args = ["-i", str(src), "-stream_loop", "-1", "-i", str(bgm)]
    if duck:
        fc = (
            f"[1:a]volume={bgm_volume},afade=t=out:st={max(total - fade_out, 0):.2f}:d={fade_out}[bg];"
            f"[bg][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[ducked];"
            f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        fc = (
            f"[1:a]volume={bgm_volume},afade=t=out:st={max(total - fade_out, 0):.2f}:d={fade_out}[bg];"
            f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    args += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-t", f"{total:.2f}",
             "-movflags", "+faststart", str(out)]
    run(args, desc="配背景音乐")
    return out


def replace_audio(video: Path, audio: Path, out: Path) -> Path:
    """用外部配音替换原音轨（保持视频流不变）。"""
    run(["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
         "-movflags", "+faststart", str(out)], desc="替换音轨")
    return out


def fade_edges(src: Path, out: Path, *, fin: float = 0.4, fout: float = 0.5) -> Path:
    total = duration_of(src)
    vf = f"fade=t=in:st=0:d={fin},fade=t=out:st={max(total - fout, 0):.2f}:d={fout}"
    af = f"afade=t=in:st=0:d={fin},afade=t=out:st={max(total - fout, 0):.2f}:d={fout}"
    run(["-i", str(src), "-vf", vf, "-af", af, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(out)], desc="淡入淡出")
    return out


def title_card(text: str, out: Path, *, w: int = 1920, h: int = 1080,
               dur: float = 2.0, subtitle: str = "", bg: str = "0x0F1117") -> Path:
    """生成黑底标题片头。字体缺失时退化为 ffmpeg 内置字体。"""
    def esc(s: str) -> str:
        return s.replace("\\", "").replace(":", r"\:").replace("'", "").replace("%", "")

    font = _font()
    font_arg = f":fontfile='{font}'" if font else ""

    main = (f"drawtext=text='{esc(text)}':fontcolor=white:fontsize=64"
            f":x=(w-text_w)/2{font_arg}")
    if subtitle:
        draw = (f",{main}:y=h/2-70"
                f",drawtext=text='{esc(subtitle)}':fontcolor=0xAAAAAA:fontsize=30"
                f":x=(w-text_w)/2:y=h/2+30{font_arg}")
    else:
        draw = f",{main}:y=(h-text_h)/2"

    run(["-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}:d={dur}:r=30",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-vf", f"format=yuv420p{draw}", "-t", f"{dur}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out)],
        desc="生成片头")
    return out


def _font() -> str:
    for cand in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]:
        if Path(cand).is_file():
            return cand
    return ""


# ---------------------------------------------------------------- 配音 TTS


def tts(cfg: Config, text: str, out: Path, *, voice: str = "male-qn-qingse",
        speed: float = 1.0, model: str = "speech-2.6-turbo") -> Path:
    """用 MiniMax 生成中文旁白。返回音频文件；失败时抛异常由调用方处理。"""
    key = cfg.key("minimax")
    if not key:
        fail("配音需要 MiniMax 密钥（MINIMAX_API_KEY）")
    base = cfg.get("MINIMAX_BASE_URL", "https://api.minimaxi.com").rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": speed, "vol": 1, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
    }
    r = request("POST", f"{base}/v1/t2a_v2",
                headers={"Authorization": f"Bearer {key}"}, body=payload)
    hex_audio = (r.get("data") or {}).get("audio")
    if not hex_audio:
        msg = (r.get("base_resp") or {}).get("status_msg") or "未知错误"
        fail(f"配音失败：{msg}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes.fromhex(hex_audio))
    return out


# 中文旁白时长粗估（字/秒），用于没有配音时对齐字幕
CHARS_PER_SEC = 4.6


def estimate_seconds(text: str) -> float:
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z]+", text))
    return max(cn / CHARS_PER_SEC + en / 2.6, 1.2)
