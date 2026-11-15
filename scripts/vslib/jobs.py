"""任务生命周期：持久化、轮询、下载。

视频生成通常 1~5 分钟，断网或关掉终端很常见。所有任务落盘到 .vs/jobs.json，
随时可以用 resume 继续等，不会重复烧钱。
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import string
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .core import Config, download, fail, log, now_stamp, slugify, state_dir
from .providers import JobStatus, Provider, get_provider

TERMINAL = ("succeeded", "failed")


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """跨进程互斥。POSIX 用 flock，Windows 用 msvcrt，都不可用时退化为无锁 + 原子写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    locked = False
    try:
        if os.name == "nt":
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except (ImportError, OSError):
                pass
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                try:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except (ImportError, OSError):
                    pass
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def new_job_id(provider: str) -> str:
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"{provider}-{now_stamp()}-{tail}"


@dataclass
class Job:
    id: str
    provider: str
    remote_id: str
    model: str
    prompt: str
    mode: str
    duration: int
    ratio: str
    resolution: str
    audio: bool
    created: float
    state: str = "queued"
    url: str = ""
    file: str = ""
    error: str = ""
    shot: str = ""
    cost_note: str = ""
    extra: dict = field(default_factory=dict)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = state_dir(root) / "jobs.json"
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("jobs", []):
            try:
                j = Job(**{k: v for k, v in item.items() if k in Job.__dataclass_fields__})
                self._jobs[j.id] = j
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        """持锁写入，并与磁盘上其它进程刚写入的记录合并，避免互相覆盖导致重复扣费。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.path.with_suffix(".lock")):
            disk: dict[str, dict] = {}
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    for item in raw.get("jobs", []):
                        if isinstance(item, dict) and item.get("id"):
                            disk[item["id"]] = item
                except (OSError, json.JSONDecodeError):
                    pass
            for jid, job in self._jobs.items():
                disk[jid] = asdict(job)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"jobs": list(disk.values())},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def add(self, **kw: Any) -> Job:
        jid = kw.pop("id", None) or new_job_id(kw.get("provider", "vs"))
        job = Job(id=jid, created=time.time(), **kw)
        self._jobs[job.id] = job
        self.save()
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if not job:
            near = [j for j in self._jobs if j.startswith(job_id)]
            if len(near) == 1:
                return self._jobs[near[0]]
            fail(f"找不到任务 {job_id}，用 `vs.py list` 查看")
        return job

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def unfinished(self) -> list[Job]:
        return [j for j in self.all() if j.state not in TERMINAL]


# ---------------------------------------------------------------- 提交与等待


def submit_and_track(cfg: Config, store: JobStore, provider: Provider, req: Any,
                     shot: str = "", auto_wait: bool = True,
                     timeout: int = 900, quiet: bool = False) -> Job:
    model = provider.pick_model(req)
    log(f"[{provider.label}] 提交中 · {model} · {req.summary()}", level="step")
    remote_id = provider.submit(req)
    if not remote_id:
        fail("提交后没有拿到任务 ID，请检查密钥和参数")

    job = store.add(
        provider=provider.name, remote_id=str(remote_id), model=model,
        prompt=req.prompt, mode=req.mode, duration=int(req.duration),
        ratio=req.ratio, resolution=req.resolution, audio=bool(req.audio),
        state="queued", shot=shot, cost_note=provider.price_hint,
    )
    log(f"任务已建：{job.id}（远程 {remote_id}）", level="ok")

    if auto_wait:
        wait(cfg, store, job, timeout=timeout, quiet=quiet)
    return job


def submit_many(cfg: Config, store: JobStore, provider: Provider,
                items: list[tuple[str, Any]], quiet: bool = False) -> list[Job]:
    """一次性把所有镜头提交出去（不等结果）。失败的单镜单独记 failed，不影响其它镜头。"""
    jobs: list[Job] = []
    for shot, req in items:
        model = provider.pick_model(req)
        if not quiet:
            log(f"[{provider.label}] 提交 {shot or '-'} · {model} · {req.summary()}", level="step")
        try:
            remote_id = provider.submit(req)
            if not remote_id:
                raise RuntimeError("提交后没有拿到任务 ID")
            job = store.add(
                provider=provider.name, remote_id=str(remote_id), model=model,
                prompt=req.prompt, mode=req.mode, duration=int(req.duration),
                ratio=req.ratio, resolution=req.resolution, audio=bool(req.audio),
                state="queued", shot=shot, cost_note=provider.price_hint,
            )
        except Exception as e:
            log(f"{shot or '-'} 提交失败：{e}", level="err")
            job = store.add(
                provider=provider.name, remote_id="", model=model,
                prompt=req.prompt, mode=req.mode, duration=int(req.duration),
                ratio=req.ratio, resolution=req.resolution, audio=bool(req.audio),
                state="failed", shot=shot, error=str(e)[:300],
                cost_note=provider.price_hint,
            )
        jobs.append(job)
    return jobs


def wait_many(cfg: Config, store: JobStore, jobs: list[Job], *,
              timeout: int = 900, interval: int = 8, quiet: bool = False) -> list[Job]:
    """统一轮询一批任务，而不是「提交一镜等一镜」。"""
    pending = [j for j in jobs if j.state not in TERMINAL]
    deadline = time.time() + timeout
    started = time.time()
    while pending and time.time() < deadline:
        for job in list(pending):
            try:
                st: JobStatus = get_provider(job.provider, cfg).poll(job.remote_id)
            except Exception as e:
                log(f"{job.shot or job.id} 查询异常：{e}", level="warn")
                continue
            if st.state != job.state:
                if not quiet:
                    log(f"  {job.shot or job.id} → {st.state} ({int(time.time() - started)}s)")
                job.state = st.state
                store.save()
            if st.state == "succeeded" and st.url:
                job.url = st.url
                job.error = ""
                store.save()
                save_video(job, st.url, store)
                pending.remove(job)
            elif st.state == "failed":
                job.error = st.error or "未知错误"
                store.save()
                pending.remove(job)
        if pending:
            time.sleep(interval)
    for job in pending:
        job.error = f"等待超时（{timeout}s），可稍后 resume 继续"
        store.save()
    return jobs


def wait(cfg: Config, store: JobStore, job: Job, *, timeout: int = 900,
         interval: int = 8, quiet: bool = False) -> Job:
    provider = get_provider(job.provider, cfg)
    deadline = time.time() + timeout
    last_state = ""
    started = time.time()

    while True:
        try:
            st: JobStatus = provider.poll(job.remote_id)
        except Exception as e:  # 网络抖动不该终止等待
            if time.time() > deadline:
                job.error = f"等待超时：{e}"
                store.save()
                return job
            log(f"查询异常，稍后重试：{e}", level="warn")
            time.sleep(interval)
            continue

        if st.state != last_state:
            label = {"queued": "排队中", "running": "生成中",
                     "succeeded": "完成", "failed": "失败"}.get(st.state, st.state)
            elapsed = int(time.time() - started)
            if not quiet:
                log(f"  {job.shot or job.id} → {label} ({elapsed}s)")
            last_state = st.state
            job.state = st.state
            store.save()

        if st.state == "succeeded" and st.url:
            job.state = "succeeded"
            job.url = st.url
            job.error = ""
            store.save()
            save_video(job, st.url, store)
            return job

        if st.state == "failed":
            job.state = "failed"
            job.error = st.error or "未知错误"
            store.save()
            return job

        if time.time() > deadline:
            job.error = f"等待超时（{timeout}s），任务可能仍在跑，稍后用 resume 继续"
            store.save()
            return job

        time.sleep(interval)


def save_video(job: Job, url: str, store: JobStore, subdir: str = "videos") -> Path:
    name = f"{slugify(job.shot or job.model)}-{job.id.split('-')[-1]}.mp4"
    dest = store.root / subdir / name
    try:
        download(url, dest)
        job.file = str(dest.relative_to(store.root))
        store.save()
        log(f"  已保存 {job.file}", level="ok")
    except Exception as e:
        job.error = f"下载失败（链接可能过期）：{e}"
        store.save()
        log(f"  下载失败：{e}。可稍后 `vs.py fetch {job.id}` 重试", level="warn")
    return dest


def resume_all(cfg: Config, store: JobStore, timeout: int = 900) -> tuple[int, int]:
    """继续等待所有未完成任务。返回 (成功数, 失败数)。"""
    pending = [j for j in store.unfinished() if j.state != "failed"]
    if not pending:
        log("没有进行中的任务")
        return 0, 0
    log(f"有 {len(pending)} 个任务待跟进", level="step")
    ok = bad = 0
    for job in pending:
        log(f"[{job.provider}] {job.shot or job.id}", level="step")
        if job.state == "succeeded" and job.url and not job.file:
            save_video(job, job.url, store)
            ok += 1
            continue
        r = wait(cfg, store, job, timeout=timeout)
        if r.state == "succeeded":
            ok += 1
        elif r.state == "failed":
            bad += 1
    return ok, bad
