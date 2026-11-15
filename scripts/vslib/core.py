"""video-studio 公共底座：配置读取、HTTP、媒体处理、日志。

只用 Python 标准库，避免要求用户 pip install 任何东西。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 常量

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_FILE = "project.json"          # 项目根目录标记文件
STATE_DIR = ".vs"                      # 项目内状态目录（任务、日志）
ENV_FILE = ".env"
USER_CONFIG = Path.home() / ".workbuddy" / "video-studio.env"

VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


# ---------------------------------------------------------------- 日志

_QUIET = False


def set_quiet(v: bool) -> None:
    global _QUIET
    _QUIET = v


def log(msg: str, *, level: str = "info") -> None:
    if _QUIET and level == "info":
        return
    prefix = {"info": "·", "ok": "✓", "warn": "!", "err": "✗", "step": "→"}.get(level, "·")
    print(f"{prefix} {msg}", file=sys.stderr if level in ("warn", "err") else sys.stdout, flush=True)


def fail(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    log(msg, level="err")
    raise SystemExit(code)


# ---------------------------------------------------------------- 配置


class Config:
    """密钥与全局默认值。优先级：环境变量 > 项目 .env > ~/.workbuddy/video-studio.env"""

    # 每个 provider 的 key 环境变量名
    KEY_ENVS = {
        "kling": "KLING_API_KEY",
        "ark": "ARK_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        "vidu": "VIDU_API_KEY",
        "runway": "RUNWAY_API_KEY",
        "veo": "GEMINI_API_KEY",
        "luma": "LUMA_API_KEY",
        "fal": "FAL_KEY",
        "heygen": "HEYGEN_API_KEY",
    }

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd or Path.cwd()
        self._values: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        for path in (USER_CONFIG, self.cwd / ENV_FILE):
            if path.is_file():
                self._parse(path)
        for key in list(self.KEY_ENVS.values()) + [
            "VS_DEFAULT_PROVIDER", "ARK_BASE_URL", "VS_MAX_COST", "FAL_KEY",
        ]:
            if os.environ.get(key):
                self._values[key] = os.environ[key]

    def _parse(self, path: Path) -> None:
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    self._values.setdefault(k, v)
        except OSError:
            pass

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def key(self, provider: str) -> str:
        env = self.KEY_ENVS.get(provider, "")
        return self._values.get(env, "") if env else ""

    def has(self, provider: str) -> bool:
        return bool(self.key(provider))

    def available_providers(self) -> list[str]:
        return [p for p in self.KEY_ENVS if self.has(p)]

    def default_provider(self) -> str:
        preferred = self.get("VS_DEFAULT_PROVIDER")
        if preferred and self.has(preferred):
            return preferred
        avail = self.available_providers()
        return avail[0] if avail else ""

    def write_env_line(self, provider: str, value: str) -> Path:
        """把 key 写入项目 .env（用户主动要求时才调用）。"""
        env = self.KEY_ENVS[provider]
        path = self.cwd / ENV_FILE
        lines: list[str] = []
        if path.is_file():
            lines = [
                ln for ln in path.read_text(encoding="utf-8").splitlines()
                if not ln.strip().startswith(f"{env}=")
            ]
        lines.append(f"{env}={value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


# ---------------------------------------------------------------- HTTP

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} @ {url}: {body[:400]}")


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 120,
    retries: int = 3,
    raw: bool = False,
) -> Any:
    """发请求。body 为 dict 时自动 JSON 编码；raw=True 时返回 bytes。"""
    hdrs = {"User-Agent": "video-studio/1.0", "Accept": "application/json"}
    hdrs.update(headers or {})
    data: bytes | None = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload
                if not payload:
                    return {}
                try:
                    return json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"_raw": payload.decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in RETRY_STATUS and attempt < retries:
                last_err = HTTPError(e.code, text, url)
                time.sleep(min(2 ** attempt * 2 + random.random(), 30))
                continue
            raise HTTPError(e.code, text, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(min(2 ** attempt * 2, 20))
                continue
            raise

    raise last_err if last_err else RuntimeError("request failed")


def download(url: str, dest: Path, *, headers: dict[str, str] | None = None,
             timeout: int = 300, retries: int = 3) -> Path:
    """流式下载。dest 已存在且非空则跳过（断点续传友好）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={**(headers or {}),
                                                       "User-Agent": "video-studio/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            tmp.replace(dest)
            return dest
        except Exception:
            tmp.unlink(missing_ok=True)
            if attempt >= retries:
                raise
            time.sleep(2 ** attempt)
    return dest


# ---------------------------------------------------------------- 媒体工具


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def mime_of(path: Path) -> str:
    guess = mimetypes.guess_type(path.name)[0]
    if guess:
        return guess
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


def to_data_url(src: str) -> str:
    """本地文件 → data URL；URL 原样返回。"""
    if is_url(src):
        return src
    path = Path(src).expanduser()
    if not path.is_file():
        fail(f"图片不存在：{src}")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_of(path)};base64,{b64}"


def to_b64(src: str) -> str:
    """本地文件 → 纯 base64（不含前缀）；URL 则下载后再编码。"""
    if is_url(src):
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = Path(tf.name)
        try:
            download(src, tmp, retries=2)
            return base64.b64encode(tmp.read_bytes()).decode("ascii")
        finally:
            tmp.unlink(missing_ok=True)
    path = Path(src).expanduser()
    if not path.is_file():
        fail(f"图片不存在：{src}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def image_dims(src: str) -> tuple[int, int] | None:
    """读 PNG/JPEG 头部拿宽高，不依赖 Pillow。"""
    try:
        path = Path(src).expanduser()
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            head = fh.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if head[:2] == b"\xff\xd8":
                fh.seek(2)
                while True:
                    b = fh.read(1)
                    while b and b != b"\xff":
                        b = fh.read(1)
                    marker = fh.read(1)
                    if not marker:
                        return None
                    if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3):
                        fh.read(3)
                        h = int.from_bytes(fh.read(2), "big")
                        w = int.from_bytes(fh.read(2), "big")
                        return w, h
                    seg = fh.read(2)
                    if len(seg) < 2:
                        return None
                    fh.seek(int.from_bytes(seg, "big") - 2, 1)
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------- 目录与状态


def find_project_root(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / PROJECT_FILE).is_file():
            return p
    return None


def state_dir(root: Path) -> Path:
    d = root / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(text: str, limit: int = 40) -> str:
    keep = []
    for ch in text:
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_":
            keep.append("-")
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return (s[:limit] or "shot")


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- 终端排版


def display_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文占 2 格）。"""
    return text + " " * max(0, width - display_width(text))
