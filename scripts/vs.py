#!/usr/bin/env python3
"""video-studio 统一命令行。

一条命令打通：查环境 → 建项目 → 生成片段 → 后期成片。

重要：后期处理（字幕、配乐、片头、转场、淡入淡出）默认全部关闭。
只有用户逐项明确同意后，才用对应参数显式开启。用户不需要剪辑时，
跑完 run 直接用 clips 交付片段，绝对不要调用 assemble。

常用：
  vs.py doctor                       自检环境与密钥
  vs.py init "我的项目"               建项目骨架
  vs.py plan                         打印开工确认单（必须先给用户核对）
  vs.py estimate --takes 3 --draft   按实际生成量（倍数）报账
  vs.py bible --set identity='...'   写制作圣经（角色块逐字复用到每镜）
  vs.py run --stage draft            便宜草稿 → vs.py animatic 看节奏
  vs.py run --takes 3 / takes / pick 每镜多候选，挑最好的一版
  vs.py run --chain                 逐镜尾帧链（推荐，边生成边衔接）
  vs.py chain --apply               补救用：改首帧图后须 --force 重跑后续镜头
  vs.py qc                           成片前质检
  vs.py gen -p "描述" -d 5           生成一个片段
  vs.py run                          按分镜批量生成定稿
  vs.py clips                        只交付片段
  vs.py assemble                     合成成片（默认纯拼接）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vslib import post, storyboard as sbmod
from vslib.pipeline import Bible, TakesStore, chain_plan, qc
from vslib.core import (
    Config,
    ENV_FILE,
    IMAGE_EXT,
    VIDEO_EXT,
    USER_CONFIG,
    download,
    fail,
    find_project_root,
    is_url,
    log,
    now_stamp,
    pad,
    set_quiet,
    slugify,
    state_dir,
)
from vslib.jobs import JobStore, resume_all, save_video, submit_and_track, wait
from vslib.providers import (
    AUTO_ORDER,
    PROVIDERS,
    GenRequest,
    get_provider,
    resolve_provider,
)

PROJECT_JSON = "project.json"

# 粗略单价（元/秒，1080p 档），仅用于量级预估
RATES = {
    ("ark", "doubao-seedance-1-0-pro-250528"): 0.26,
    ("ark", "doubao-seedance-1-5-pro-251215"): 0.30,
    ("ark", "doubao-seedance-2-0-260128"): 0.40,
    ("kling", "kling-3.0"): 0.35,
    ("kling", "kling-4.0"): 0.55,
    ("minimax", "MiniMax-Hailuo-2.3"): 0.50,
    ("minimax", "MiniMax-Hailuo-2.3-Fast"): 0.25,
    ("minimax", "MiniMax-Hailuo-02"): 0.35,
    ("vidu", "viduq2"): 0.15,
    ("vidu", "viduq3-pro"): 0.25,
    ("vidu", "viduq3-turbo"): 0.12,
    ("runway", "gen4_turbo"): 0.36,
    ("runway", "gen4.5"): 0.86,
    ("runway", "veo3.1_fast"): 1.10,
    ("veo", "veo-3.1-generate-preview"): 2.90,
    ("veo", "veo-3.1-fast-generate-preview"): 1.20,
    ("luma", "ray-2"): 0.60,
    ("luma", "ray-flash-2"): 0.30,
}


def est_cost(provider: str, model: str, seconds: int) -> float:
    rate = RATES.get((provider, model))
    if rate is None:
        rate = next((v for (p, _), v in RATES.items() if p == provider), 0.3)
    return rate * seconds


def cost_scale(args: argparse.Namespace) -> float:
    """成本报账倍数：候选 ×N，草稿按 720p 约 0.5 倍另计一轮。

    第 5 轮把成本贴给用户时，必须按实际要生成的量报，不能只报单倍定稿。
    """
    n = max(1, int(getattr(args, "takes", 1) or 1))
    return n + (0.5 if getattr(args, "draft", False) else 0)


def cost_note(args: argparse.Namespace) -> str:
    n = max(1, int(getattr(args, "takes", 1) or 1))
    bits = []
    if n > 1:
        bits.append(f"每镜 {n} 个候选（×{n}）")
    if getattr(args, "draft", False):
        bits.append("另加一轮 720p 草稿（×0.5）")
    if getattr(args, "chain", False):
        bits.append("逐镜尾帧链")
    return "；".join(bits)


# ---------------------------------------------------------------- 项目


def project_root(required: bool = False) -> Path:
    root = find_project_root()
    if root:
        return root
    if required:
        fail("当前目录不是 video-studio 项目。先运行：vs.py init \"项目名\"")
    return Path.cwd()


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.dir).resolve() if args.dir else Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    title = args.title or root.name
    meta = {
        "title": title,
        "created": now_stamp(),
        "provider": args.provider or "auto",
        "ratio": args.ratio,
    }
    (root / PROJECT_JSON).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    sb_path = root / "storyboard.json"
    if not sb_path.is_file():
        sb = sbmod.scaffold(title, ratio=args.ratio, n=args.shots)
        sb.path = sb_path
        sbmod.save(sb)
    for sub in ("videos", "audio", "output", "images", ".vs/work"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    env = root / ENV_FILE
    if not env.is_file():
        env.write_text(
            "# video-studio 密钥配置（不要提交到 git）\n"
            "# 至少填一个，不用的留空即可\n"
            "ARK_API_KEY=\n"
            "KLING_API_KEY=\n"
            "MINIMAX_API_KEY=\n"
            "VIDU_API_KEY=\n"
            "RUNWAY_API_KEY=\n"
            "GEMINI_API_KEY=\n"
            "LUMA_API_KEY=\n",
            encoding="utf-8",
        )
    gi = root / ".gitignore"
    if not gi.is_file():
        gi.write_text(".env\n.vs/\nvideos/\noutput/\n*.mp4\n*.part\n", encoding="utf-8")

    log(f"项目已创建：{root}", level="ok")
    log(f"  分镜：{sb_path.name}（{args.shots} 个空镜头）")
    log(f"  密钥：{env.name}")
    log("下一步：填好 .env 里的密钥，或在对话里告诉我你的密钥来源")


def cmd_doctor(args: argparse.Namespace) -> None:
    cfg = Config()
    print("── 环境 ─────────────────────────────")
    ok_py = sys.version_info >= (3, 9)
    print(f"  Python {sys.version.split()[0]}  {'✓' if ok_py else '✗ 需要 3.9+'}")
    has_ff = post.has_ffmpeg()
    print(f"  ffmpeg        {'✓ ' + (shutil.which('ffmpeg') or '') if has_ff else '✗ 未安装（后期合成不可用）'}")
    has_fp = shutil.which("ffprobe") is not None
    print(f"  ffprobe       {'✓' if has_fp else '✗ 未安装'}")

    print("\n── 密钥 ─────────────────────────────")
    any_key = False
    for name, cls in PROVIDERS.items():
        p = cls(cfg)
        env = Config.KEY_ENVS[name]
        if p.available():
            any_key = True
            print(f"  ✓ {pad(p.label, 28)} {env}")
        else:
            print(f"  · {pad(p.label, 28)} {env} 未设置")
    if not any_key:
        print(f"\n  还没配置任何密钥。写入 {ENV_FILE} 或 {USER_CONFIG}，")
        print("  或运行 vs.py setup 看申请指引。")

    root = find_project_root()
    if root:
        store = JobStore(root)
        jobs = store.all()
        done = sum(1 for j in jobs if j.state == "succeeded")
        pend = len(store.unfinished())
        print(f"\n── 项目 {root.name} ─────────────────")
        print(f"  任务 {len(jobs)} 个（成功 {done}，进行中 {pend}）")
        vids = [p for p in (root / "videos").glob("*.mp4")] if (root / "videos").is_dir() else []
        drafts = [p for p in vids if p.name.startswith("draft-")]
        print(f"  片段 {len(vids)} 个（草稿 {len(drafts)}）")
        b = Bible.load(root)
        print(f"  制作圣经  {'✓ ' + b.identity[:20] if b.identity else '· 未填身份块'}"
              f"{' · 已锁定' if b.locked else ''}")
        lock = state_dir(root) / "model-lock.json"
        locked = ""
        if lock.is_file():
            try:
                locked = json.loads(lock.read_text(encoding="utf-8")).get("provider", "")
            except (OSError, json.JSONDecodeError):
                pass
        print(f"  模型锁定  {locked or '· 尚未生成'}")

    if not has_ff:
        print("\n提示：装 ffmpeg 才能自动拼接成片 → brew install ffmpeg")


def cmd_models(args: argparse.Namespace) -> None:
    cfg = Config()
    for name in AUTO_ORDER:
        p = get_provider(name, cfg)
        flag = "✓" if p.available() else "·"
        print(f"\n{flag} {p.label}  ({name})")
        print(f"   模型      {', '.join(p.models)}")
        if p.durations:
            print(f"   时长      {', '.join(str(d) + 's' for d in p.durations)}")
        if p.resolutions:
            print(f"   分辨率    {', '.join(p.resolutions)}")
        print(f"   支持      {', '.join(sorted(p.supports))}")
        print(f"   价格      {p.price_hint}")
        if not p.available():
            print(f"   密钥      export {Config.KEY_ENVS[name]}=...   （{p.key_help}）")


def cmd_setup(args: argparse.Namespace) -> None:
    cfg = Config()
    if args.set:
        # 两种写法都收：ark=密钥（provider 名）/ ARK_API_KEY=密钥（环境变量名）
        env_alias = {v: k for k, v in Config.KEY_ENVS.items()}
        for pair in args.set:
            if "=" not in pair:
                fail(f"格式应为 provider=key 或 环境变量名=key，收到：{pair}")
            prov, val = pair.split("=", 1)
            prov = env_alias.get(prov.strip(), prov.strip())
            if prov not in Config.KEY_ENVS:
                fail(f"未知 provider：{prov}。可选：{', '.join(Config.KEY_ENVS)}"
                     f"（也可直接写环境变量名，如 {Config.KEY_ENVS['ark']}）")
            path = cfg.write_env_line(prov, val.strip())
            log(f"已写入 {path.name}：{Config.KEY_ENVS[prov]}", level="ok")
        return

    print("配置 API 密钥")
    print("=" * 60)
    for name in AUTO_ORDER:
        p = get_provider(name, cfg)
        print(f"\n{p.label}")
        print(f"  环境变量  {Config.KEY_ENVS[name]}")
        print(f"  申请地址  {p.key_help or p.doc_url}")
        print(f"  价格参考  {p.price_hint}")
    print("\n" + "=" * 60)
    print("最快的方式（三选一）：")
    print(f"  1. 写入当前项目的 {ENV_FILE}")
    print(f"  2. 写入全局配置 {USER_CONFIG}（所有项目共用）")
    print("  3. 命令行一条搞定：")
    print("       python3 scripts/vs.py setup --set ARK_API_KEY=你的密钥")
    print("\n新手建议从火山方舟（即梦 Seedance）开始：国内直连、按秒计费、有免费额度。")


# ---------------------------------------------------------------- 生成


def build_request(args: argparse.Namespace, base: GenRequest | None = None) -> GenRequest:
    req = base or GenRequest(prompt="")
    if getattr(args, "prompt", None):
        req.prompt = args.prompt
    for attr, target in (("image", "image"), ("last_image", "last_image")):
        val = getattr(args, attr, None)
        if val:
            setattr(req, target, val)
    if getattr(args, "ref", None):
        req.ref_images = list(args.ref)
    if getattr(args, "duration", None):
        req.duration = int(args.duration)
    if getattr(args, "ratio", None):
        req.ratio = args.ratio
    if getattr(args, "resolution", None):
        req.resolution = args.resolution
    if getattr(args, "model", None):
        req.model = args.model
    if getattr(args, "seed", None) is not None:
        req.seed = args.seed
    if getattr(args, "negative", None):
        req.negative = args.negative
    if getattr(args, "audio", False):       # 用户明确要原生音效才开
        req.audio = True
    if getattr(args, "no_audio", False):   # 兼容旧写法（默认本来就是关）
        req.audio = False
    return req


def cmd_gen(args: argparse.Namespace) -> None:
    cfg = Config()
    root = project_root()
    if not (root / PROJECT_JSON).exists():
        root.mkdir(parents=True, exist_ok=True)
        (root / PROJECT_JSON).write_text(json.dumps({"title": root.name}), encoding="utf-8")

    req = build_request(args)
    if not req.prompt:
        fail("缺少 --prompt，例如：vs.py gen -p \"海边日落，镜头缓慢推进\" -d 5")

    provider = resolve_provider(args.provider or "auto", cfg, req)
    store = JobStore(root)
    job = submit_and_track(cfg, store, provider, req, shot=args.shot or "",
                           auto_wait=not args.no_wait,
                           timeout=args.timeout, quiet=args.quiet)
    if job.state == "succeeded":
        print(json.dumps({"job": job.id, "file": job.file, "url": job.url,
                          "cost_est": round(est_cost(job.provider, job.model, job.duration), 2)},
                         ensure_ascii=False))
    elif job.state == "failed":
        fail(f"生成失败：{job.error}")
    else:
        print(json.dumps({"job": job.id, "state": job.state, "hint": "用 vs.py resume 继续等"},
                         ensure_ascii=False))


def pick_provider(args: argparse.Namespace, cfg: Config, bible: Bible,
                  req: GenRequest, root: Path) -> object:
    """整批只解析一次模型，并跨多次 run 锁定。

    逐镜调用 auto 是新手最容易踩的坑：某镜有首帧图、下一镜没有，
    就可能换一家模型，角色/风格立刻断。这里把结果落到 .vs/model-lock.json。
    """
    lock_path = state_dir(root) / "model-lock.json"
    forced = bible.provider if (bible.locked and bible.provider) else ""
    if not forced and lock_path.is_file() and not getattr(args, "unlock_model", False):
        try:
            forced = json.loads(lock_path.read_text(encoding="utf-8")).get("provider", "")
        except (OSError, json.JSONDecodeError):
            pass

    name = args.provider or "auto"
    if forced and not getattr(args, "unlock_model", False):
        if name != "auto" and name != forced:
            log(f"本项目已锁定 {forced}，忽略 --provider {name}（要换模型加 --unlock-model）",
                level="warn")
        name = forced

    p = resolve_provider(name, cfg, req)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"provider": p.name, "model": bible.model or ""},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def shot_image(root: Path, value: str | None) -> str | None:
    if not value:
        return None
    return str((root / value).resolve()) if not is_url(value) else value


def cmd_run(args: argparse.Namespace) -> None:
    cfg = Config()
    root = project_root(required=True)
    sb_path = root / (args.storyboard or "storyboard.json")
    sb = sbmod.load(sb_path)
    for p in sbmod.validate(sb):
        log(p, level="warn")
    if not sb.shots:
        fail("分镜为空")

    bible = Bible.load(root)
    takes = TakesStore(root)
    for issue in bible.check(root):
        log(issue, level="warn")

    stage = getattr(args, "stage", "final") or "final"
    n_takes = max(1, int(getattr(args, "takes", 1) or 1))
    is_candidate = n_takes > 1
    is_draft = stage == "draft"
    write_back = not (is_candidate or is_draft)   # 草稿/候选都不覆盖定稿分镜

    if is_draft and is_candidate:
        fail("--stage draft 和 --takes 不能同时用：先看草稿节奏，定稿阶段再做候选")

    chain_mode = bool(getattr(args, "chain", False))
    if chain_mode and (is_draft or is_candidate):
        fail("--chain 只用于定稿生成，不能和 --stage draft / --takes 同时用")
    if chain_mode and not post.has_ffmpeg():
        fail("逐镜尾帧链需要 ffmpeg 抽帧：brew install ffmpeg")
    if chain_mode:
        log("逐镜尾帧链已开启：每生成完一镜，末帧自动接成下一镜首帧（不额外重跑）",
            level="step")

    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    store = JobStore(root)
    todo = []
    for s in sb.shots:
        if only and s.id not in only:
            continue
        existing = root / s.video if s.video else None
        if write_back and existing and existing.is_file() and existing.stat().st_size > 1024 and not args.force:
            log(f"{s.id} 已存在，跳过（--force 重跑）")
            continue
        todo.append(s)

    if not todo:
        log("没有需要生成的镜头", level="ok")
        return

    refs: list[str] = []
    if getattr(args, "use_refs", False) and bible.references:
        refs = [str((root / r).resolve()) for r in bible.references if (root / r).is_file()]
        if not refs:
            log("--use-refs 已指定，但 bible.json 里的参考图文件都不存在", level="warn")

    # 用第一镜挑模型，之后整批复用；参考图需求也参与选型
    first = todo[0]
    probe = GenRequest(prompt=first.prompt, duration=first.duration,
                       ratio=sb.ratio, resolution=sb.resolution,
                       image=shot_image(root, first.image), ref_images=list(refs))
    provider = pick_provider(args, cfg, bible, probe, root)

    if bible.seed is not None and not provider.can("seed"):
        log(f"{provider.label} 不支持 seed，候选版本靠多次生成挑选取舍", level="warn")
    if refs and not provider.can("ref"):
        log(f"{provider.label} 不支持多图参考，参考图只用于生图阶段（已忽略）", level="warn")
        refs = []

    total_sec = sum(s.duration for s in todo) * n_takes
    log(f"待生成 {len(todo)} 镜 × {n_takes} 版 / 约 {total_sec}s 素材 / {stage} 阶段", level="step")
    if is_draft:
        log("草稿预演：720p 低成本，不写回分镜；用 vs.py animatic 看节奏", level="step")
    if is_candidate:
        log("候选模式：版本记入 .vs/takes.json，用 vs.py takes / pick 选版", level="step")

    ok, bad = 0, 0
    for idx, s in enumerate(todo):
        log(f"\n── {s.id} · {s.desc or s.prompt[:30]}", level="step")
        prompt = bible.compose_prompt(s.prompt, sb)
        for k in range(n_takes):
            label = takes.next_label(s.id) if is_candidate else ""
            shot_name = (f"{s.id}-{label}" if is_candidate else
                         f"draft-{s.id}" if is_draft else s.id)
            req = GenRequest(
                prompt=prompt,
                image=shot_image(root, s.image),
                last_image=shot_image(root, s.last_image),
                duration=s.duration, ratio=sb.ratio,
                resolution="720p" if is_draft else sb.resolution,
                audio=(sb.audio or getattr(args, "audio", False)) and not args.no_audio,
                negative=bible.neg(sb),
                seed=(bible.seed + k) if bible.seed is not None else None,
                model=bible.model or None,
            )
            if refs:
                req.ref_images = refs
            try:
                job = submit_and_track(cfg, store, provider, req, shot=shot_name,
                                       auto_wait=not args.no_wait,
                                       timeout=args.timeout, quiet=args.quiet)
            except SystemExit:
                raise
            except Exception as e:
                log(f"{shot_name} 出错：{e}", level="err")
                bad += 1
                continue

            # 可复现记录：prompt / 参考图 / seed / 模型 / 版本都留痕
            job.extra.update({"stage": stage, "take": label, "seed": req.seed,
                              "refs": refs, "identity": bible.identity})
            store.save()

            if job.state == "succeeded" and job.file:
                ok += 1
                if is_candidate:
                    takes.record(s.id, label, {
                        "file": job.file, "stage": stage, "seed": req.seed,
                        "provider": provider.name, "model": job.model,
                        "prompt": prompt, "job": job.id,
                    })
                elif write_back:
                    s.video = job.file
                    sbmod.save(sb)
                else:
                    log(f"草稿片段：{job.file}", level="ok")
            else:
                bad += 1
                log(f"{shot_name} 未完成：{job.error}", level="warn")
                if args.stop_on_error:
                    break

        # 尾帧链必须在「生成下一镜之前」做：改的是下一镜的 image 字段，
        # 若等定稿全部跑完再 chain，已生成的视频不会变，只能整批重跑。
        if chain_mode and write_back and s.video and idx + 1 < len(todo):
            nxt = todo[idx + 1]
            src = root / s.video
            if src.is_file() and (not nxt.image or "chain/" in nxt.image):
                dest = root / "images" / "chain" / f"{nxt.id}-first.png"
                try:
                    post.extract_frame(src, dest, where="last")
                    nxt.image = str(dest.relative_to(root))
                    sbmod.save(sb)
                    log(f"尾帧链：{s.id} 末帧 → {nxt.id} 首帧", level="ok")
                except (Exception, SystemExit) as e:
                    log(f"抽帧失败，{nxt.id} 保持原首帧（已生成片段不受影响）：{e}",
                        level="warn")
        if args.stop_on_error and bad:
            break

    log(f"\n完成 {ok} 个，失败/待续 {bad} 个", level="ok" if bad == 0 else "warn")
    if bad:
        log("未完成的可以稍后 `vs.py resume` 继续，不会重复扣费")
    print(sbmod.report(sbmod.load(sb_path)))


def cmd_list(args: argparse.Namespace) -> None:
    root = project_root(required=False)
    store = JobStore(root)
    jobs = store.all()
    if not jobs:
        print("还没有任务")
        return
    print(f"{'任务ID':<34} {'状态':<10} {'镜头':<6} {'文件'}")
    print("-" * 88)
    for j in jobs[: args.limit]:
        mark = {"succeeded": "✓", "failed": "✗", "running": "▶", "queued": "…"}.get(j.state, "?")
        file = Path(j.file).name if j.file else ("-" if not j.error else j.error[:24])
        print(f"{pad(j.id, 34)} {pad(mark + ' ' + j.state, 10)} {pad(j.shot or '-', 6)} {file}")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = Config()
    root = project_root()
    store = JobStore(root)
    job = store.get(args.job_id)
    provider = get_provider(job.provider, cfg)
    if job.state in ("succeeded", "failed"):
        print(json.dumps({"job": job.id, "state": job.state, "file": job.file,
                          "url": job.url, "error": job.error}, ensure_ascii=False))
        return
    st = provider.poll(job.remote_id)
    job.state = st.state
    if st.url:
        job.url = st.url
    store.save()
    print(json.dumps({"job": job.id, "state": st.state, "progress": st.progress}, ensure_ascii=False))
    if st.state == "succeeded" and st.url:
        save_video(job, st.url, store)


def cmd_fetch(args: argparse.Namespace) -> None:
    root = project_root(required=True)
    store = JobStore(root)
    job = store.get(args.job_id)
    if not job.url:
        provider = get_provider(job.provider, Config())
        st = provider.poll(job.remote_id)
        job.url = st.url or ""
        job.state = st.state
        store.save()
    if not job.url:
        fail(f"任务还没有可下载的地址（状态 {job.state}）")
    dest = save_video(job, job.url, store)
    print(dest)


def cmd_resume(args: argparse.Namespace) -> None:
    cfg = Config()
    root = project_root(required=True)
    store = JobStore(root)
    ok, bad = resume_all(cfg, store, timeout=args.timeout)
    log(f"跟进完成：成功 {ok}，失败 {bad}", level="ok" if bad == 0 else "warn")


# ---------------------------------------------------------------- 后期


def add_post_options(p: argparse.ArgumentParser, *, with_audio: bool = True) -> None:
    """assemble / plan 共用的后期参数。

    设计铁律：默认全部关闭。任何会改变画面或声音的操作，都必须由用户
    逐项明确同意后，由 Agent 显式传入对应参数。脚本绝不替用户做决定，
    也不会因为 storyboard.json 里"有这个字段"就自动执行。
    """
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.add_argument("--subtitles", action="store_true",
                   help="烧入字幕（必须先得到用户确认）")
    p.add_argument("--no-subtitles", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--bgm", help="背景音乐文件（必须先得到用户确认）")
    p.add_argument("--bgm-volume", type=float, default=0.18)
    p.add_argument("--title", help="加片头标题（必须先得到用户确认）")
    p.add_argument("--subtitle", help="片头副标题")
    p.add_argument("--title-dur", type=float, default=2.5)
    p.add_argument("--transition", default=None,
                   choices=["fade", "none", "dissolve", "slideleft", "circleopen", "wipeleft"],
                   help="转场方式，默认 none（硬切）；要转场必须显式指定")
    p.add_argument("--font-size", type=int, default=20)
    p.add_argument("--fade", action="store_true", help="首尾淡入淡出（必须先得到用户确认）")
    p.add_argument("--fps", type=int, default=30)
    if with_audio:
        # 原生音效属于「生成阶段」，只有 plan 用它做确认单标记；
        # assemble 不该暴露这个开关，避免被误以为后期能补音效。
        p.add_argument("--audio", action="store_true",
                       help="在确认单上标记「用户已确认要模型原生音效」（生成阶段选项）")
        p.add_argument("--takes", type=int, default=1,
                       help="确认单成本按每镜 N 个候选计算")
        p.add_argument("--draft", action="store_true",
                       help="确认单成本额外计入一轮 720p 草稿")
        p.add_argument("--chain", action="store_true",
                       help="在确认单上标出「逐镜尾帧链」（生成阶段选项）")
    p.add_argument("--use-storyboard", action="store_true",
                   help="沿用分镜里已逐项确认过的字幕/BGM/转场设置（仅在用户看过确认单后使用）")


def resolve_post_plan(args: argparse.Namespace, sb: sbmod.Storyboard) -> dict:
    """把命令行参数解析成「这次到底要做什么」的清单。默认什么都不做。"""
    use_sb = bool(getattr(args, "use_storyboard", False))
    has_text = any(s.text_for_subtitle.strip() for s in sb.shots)

    subtitles = bool(getattr(args, "subtitles", False)) or (use_sb and has_text)
    if getattr(args, "no_subtitles", False):  # 兼容旧写法
        subtitles = False
    if subtitles and not has_text:
        log("分镜里没有字幕文案，字幕项自动跳过", level="warn")
        subtitles = False

    bgm = getattr(args, "bgm", None) or (sb.bgm if use_sb else "") or ""
    transition = getattr(args, "transition", None)
    if transition is None:
        transition = (sb.transition if use_sb else "none") or "none"

    return {
        "subtitles": subtitles,
        "audio": bool(sb.audio or getattr(args, "audio", False)),
        "bgm": bgm,
        "bgm_volume": getattr(args, "bgm_volume", 0.18),
        "transition": transition,
        "transition_dur": sb.transition_dur,
        "title": getattr(args, "title", "") or "",
        "title_sub": getattr(args, "subtitle", "") or "",
        "title_dur": getattr(args, "title_dur", 2.5),
        "fade": bool(getattr(args, "fade", False)),
        "font_size": sb.font_size or getattr(args, "font_size", 20),
        "fps": getattr(args, "fps", 30),
        "use_storyboard": use_sb,
    }


def plan_text(sb: sbmod.Storyboard, plan: dict, *, do_assemble: bool,
              clip_count: int = 0, scale: float = 1.0, note: str = "") -> str:
    """生成一张可以直接贴给用户核对的开工确认单。"""
    on, off = "做  ", "不做"
    def row(label: str, active: bool, detail: str = "") -> str:
        tail = (" · " + detail) if active and detail else ""
        return f"  [{on if active else off}] {pad(label, 10)}{tail}"
    def line(label: str, value: str) -> str:
        return f"  {pad(label, 12)}{value}"

    seconds = sb.total_duration
    lines = [
        "════ 开工确认单 · 请逐项跟用户核对 ════",
        line("项目", sb.title),
        line("规格", f"{sb.ratio} / {sb.resolution} / {len(sb.shots)} 镜 / {seconds}s"),
        line("预估成本", f"约 ¥{est_cost('ark', 'doubao-seedance-1-0-pro-250528', seconds) * scale:.1f}"
             "（即梦档，实际以 vs.py estimate 为准）"),
        line("加速环节", note or "静帧 / 草稿 / 多候选 / 尾帧链 都不做"),
        "",
        "── 交付形态 ──",
        row("剪辑合并成片", do_assemble,
            f"把 {clip_count or len(sb.shots)} 段拼成 1 个 mp4" if do_assemble else ""),
        row("只交付片段", not do_assemble,
            "不做任何后期，逐个交付 mp4" if not do_assemble else ""),
        row("模型原生音效", plan["audio"], "片段自带 AI 生成声音"),
    ]
    if do_assemble:
        lines += [
            "",
            "── 后期逐项（默认全部关闭）──",
            row("字幕", plan["subtitles"], "烧录到画面上"),
            row("配音/旁白", False),
            row("背景音乐", bool(plan["bgm"]),
                f"{plan['bgm']} · 音量 {plan['bgm_volume']}"),
            row("片头", bool(plan["title"]), plan["title"]),
            row("转场", plan["transition"] != "none", plan["transition"]),
            row("淡入淡出", plan["fade"]),
            "  ※ 配音需单独确认后另行处理，本命令不会自动生成。",
        ]
    lines += [
        "",
        "── 检查 ──",
        "  以上每一项都必须由用户口头确认过，未确认的一律保持「不做」。",
        "  生成前还要单独确认一次成本（vs.py estimate 的输出）。",
        "════════════════════════════════════",
    ]
    return "\n".join(lines)


def collect_clips(root: Path, sb: sbmod.Storyboard) -> list[Path]:
    clips: list[Path] = []
    for s in sb.shots:
        if not s.video:
            continue
        p = root / s.video
        if p.is_file() and p.stat().st_size > 1024:
            clips.append(p)
        else:
            log(f"{s.id} 的视频文件缺失，跳过", level="warn")
    return clips


def cmd_plan(args: argparse.Namespace) -> None:
    """打印开工确认单。Agent 必须把它贴给用户、逐项核对后才能动手。"""
    root = project_root(required=True)
    sb = sbmod.load(root / (args.storyboard or "storyboard.json"))
    plan = resolve_post_plan(args, sb)
    do_assemble = bool(getattr(args, "assemble", False))
    print(plan_text(sb, plan, do_assemble=do_assemble,
                    clip_count=len(collect_clips(root, sb)),
                    scale=cost_scale(args), note=cost_note(args)))
    for p in sbmod.validate(sb):
        print(f"  ⚠ {p}")


def cmd_clips(args: argparse.Namespace) -> None:
    """列出可直接交付的片段（用户说「不用剪辑」时用这个）。"""
    root = project_root(required=True)
    sb = sbmod.load(root / (args.storyboard or "storyboard.json"))
    clips = collect_clips(root, sb)
    if not clips:
        fail("还没有生成任何片段。先运行 vs.py run。")
    if args.json:
        print(json.dumps([{
            "shot": s.id, "file": str(root / s.video),
            "duration": s.duration, "size_mb": round((root / s.video).stat().st_size / 1048576, 2),
        } for s in sb.shots if s.video and (root / s.video).is_file()], ensure_ascii=False))
        return
    print(f"共 {len(clips)} 个片段（未做任何剪辑/字幕/配乐）：")
    for i, c in enumerate(clips, 1):
        print(f"  {i}. {c}  {c.stat().st_size / 1048576:.1f}MB")
    print("\n把这些路径用 present_files 逐个交付给用户即可。")


def cmd_assemble(args: argparse.Namespace) -> None:
    root = project_root(required=True)
    sb_path = root / (args.storyboard or "storyboard.json")
    sb = sbmod.load(sb_path)

    clips = collect_clips(root, sb)
    if not clips:
        fail("没有可用片段。先运行 vs.py run 生成视频。")

    plan = resolve_post_plan(args, sb)
    log("本次后期动作：" + ", ".join(
        [k for k, v in {
            "字幕": plan["subtitles"], "配乐": bool(plan["bgm"]),
            "片头": bool(plan["title"]), "转场": plan["transition"] != "none",
            "淡入淡出": plan["fade"],
        }.items() if v] or ["纯拼接（无附加处理）"]), level="step")

    w, h = post.size_for(sb.ratio, {"1080p": 1080, "720p": 720, "4k": 2160}.get(sb.resolution, 1080))
    work = state_dir(root) / "work"
    work.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else root / "output" / f"{slugify(sb.title)}-{now_stamp()}.mp4"
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log(f"归一化 {len(clips)} 个片段 → {w}x{h}", level="step")
    norm: list[Path] = []
    for i, c in enumerate(clips, 1):
        dest = work / f"norm-{i:02d}.mp4"
        if dest.is_file() and dest.stat().st_size > 1024 and not args.rebuild:
            norm.append(dest)
            continue
        post.normalize(c, dest, w=w, h=h, fps=plan["fps"])
        norm.append(dest)

    # 片头
    lead = 0.0
    if plan["title"]:
        title_file = work / "title.mp4"
        if args.rebuild or not title_file.is_file():
            post.title_card(plan["title"], title_file, w=w, h=h, dur=plan["title_dur"],
                            subtitle=plan["title_sub"])
        post.normalize(title_file, work / "title-norm.mp4", w=w, h=h, fps=plan["fps"])
        norm.insert(0, work / "title-norm.mp4")
        lead = plan["title_dur"]

    joined = work / "joined.mp4"
    log("拼接中", level="step")
    using_xfade = plan["transition"] != "none"
    if using_xfade:
        post.concat_xfade(norm, joined, transition=plan["transition"],
                          dur=plan["transition_dur"], fps=plan["fps"])
    else:
        post.concat(norm, joined)

    current = joined

    # 字幕（默认关闭，必须 --subtitles 或 --use-storyboard 显式开启）
    if plan["subtitles"]:
        # 转场会吃掉重叠时长，字幕轴要同步压缩，否则越往后越提前
        overlap = plan["transition_dur"] if (using_xfade and len(norm) > 2) else 0.0
        cues = sbmod.timeline(sb, overlap=overlap)
        if lead:
            cues = [(a + lead, b + lead, t) for a, b, t in cues]
        if cues:
            srt = work / "subs.srt"
            post.write_srt(cues, srt)
            subbed = work / "subbed.mp4"
            post.burn_subtitles(current, srt, subbed, font_size=plan["font_size"])
            current = subbed
            log(f"已烧入 {len(cues)} 条字幕", level="ok")

    # 背景音乐（默认关闭，必须显式给 --bgm）
    if plan["bgm"]:
        bgm_path = Path(plan["bgm"])
        if not bgm_path.is_absolute():
            bgm_path = root / bgm_path
        if bgm_path.is_file():
            mixed = work / "mixed.mp4"
            post.add_bgm(current, bgm_path, mixed, bgm_volume=plan["bgm_volume"])
            current = mixed
            log("已混入背景音乐", level="ok")
        else:
            log(f"找不到 BGM：{bgm_path}", level="warn")

    if plan["fade"]:
        faded = work / "faded.mp4"
        post.fade_edges(current, faded)
        current = faded

    shutil.copy2(current, out_path)
    dur = post.duration_of(out_path)
    size_mb = out_path.stat().st_size / 1048576
    log(f"成片完成：{out_path}", level="ok")
    log(f"  {dur:.1f}s · {size_mb:.1f}MB · {w}x{h}")
    print(str(out_path))


# ---------------------------------------------------------------- 生产管线


def cmd_bible(args: argparse.Namespace) -> None:
    """制作圣经：写一次，每个镜头逐字复用。"""
    root = project_root(required=True)
    b = Bible.load(root)
    touched = False

    if args.set:
        pairs = [x for group in args.set for x in group]  # 兼容多次 --set 与一次多个
        for pair in pairs:
            if "=" not in pair:
                fail(f"格式应为 字段=值，收到：{pair}")
            key, val = pair.split("=", 1)
            key, val = key.strip(), val.strip()
            if key not in Bible.__dataclass_fields__ or key == "path":
                fail(f"未知字段：{key}。可选：identity, style, lighting, negative, "
                     "references, provider, model, seed, locked")
            if key == "references":
                b.references = [x.strip() for x in val.split(",") if x.strip()]
            elif key == "seed":
                b.seed = None if val.lower() in ("", "none", "null") else int(val)
            elif key == "locked":
                b.locked = val.lower() in ("1", "true", "yes", "on")
            else:
                setattr(b, key, val)
        touched = True
    if args.lock:
        b.locked = True
        touched = True
    if args.unlock:
        b.locked = False
        touched = True

    if not b.path.is_file():
        b.save(root)
        log("已生成 bible.json 模板。先用 --set 填好身份块再生成视频。", level="ok")
    elif touched:
        b.save(root)
        log("bible.json 已更新", level="ok")

    print("── 制作圣经 ────────────────────────────────")
    for label, key in (("角色身份块", "identity"), ("统一风格", "style"),
                        ("光线", "lighting"), ("排除项", "negative")):
        print(f"  {label:<10} {getattr(b, key) or '（未填）'}")
    print(f"  {'参考图':<10} {', '.join(b.references) or '（未填）'}")
    print(f"  {'模型':<10} {b.provider or '自动锁定'} / {b.model or '默认'}")
    print(f"  {'seed':<10} {b.seed if b.seed is not None else '（未设，结果不可复现）'}")
    print(f"  {'锁定':<10} {'是' if b.locked else '否'}")
    print("─────────────────────────────────────────────")
    for issue in b.check(root):
        print(f"  ⚠ {issue}")
    print("\n身份块会逐字拼到每个镜头 prompt 最前面；改一个字都可能造成漂移。")
    print("示例：vs.py bible --set identity='28岁女性，齐肩深棕发，右眼下痣，深绿高领针织衫' "
          "--set seed=12345 --lock")


def cmd_takes(args: argparse.Namespace) -> None:
    root = project_root(required=True)
    takes = TakesStore(root)
    if not takes.all_shots():
        print("还没有候选版本。用 vs.py run --takes 3 一次出三版再挑。")
        return
    for shot in takes.all_shots():
        print(f"\n{shot}")
        for label, info in sorted(takes.for_shot(shot).items()):
            f = root / info.get("file", "")
            mark = "✓" if f.is_file() else "✗"
            print(f"  {mark} {label:<4} seed={str(info.get('seed') or '-'):<8} "
                  f"{info.get('stage', ''):<6} {info.get('file', '')}")
    print("\n选版：vs.py pick s1 B（只改分镜引用，不重新生成，不额外花钱）")


def cmd_pick(args: argparse.Namespace) -> None:
    root = project_root(required=True)
    takes = TakesStore(root)
    label = args.label.upper()
    info = takes.get(args.shot, label)
    if not info:
        fail(f"{args.shot} 没有版本 {label}。用 vs.py takes 看有哪些。")
    f = root / info.get("file", "")
    if not f.is_file():
        fail(f"版本文件不存在：{f}")
    sb_path = root / (args.storyboard or "storyboard.json")
    sb = sbmod.load(sb_path)
    shot = next((s for s in sb.shots if s.id == args.shot), None)
    if not shot:
        fail(f"分镜里没有 {args.shot}")
    shot.video = info["file"]
    sbmod.save(sb)
    log(f"{args.shot} 已选定 {label} → {info['file']}", level="ok")
    log("只改了分镜引用，没有重新生成，不额外花钱", level="ok")


def cmd_chain(args: argparse.Namespace) -> None:
    """上一镜末帧 → 下一镜首帧。默认只预览，--apply 才真的改分镜。"""
    root = project_root(required=True)
    sb_path = root / (args.storyboard or "storyboard.json")
    sb = sbmod.load(sb_path)
    plan = chain_plan(root, sb)
    if not plan:
        print("没有可串接的片段。先生成至少两个镜头。")
        return

    print("尾帧链计划（把上一镜的最后一帧，作为下一镜的首帧）：")
    for p in plan:
        note = f" · 现有首帧 {p['current']}" if p["current"] else ""
        print(f"  {p['from']} 末帧  →  {p['to']} 首帧   images/chain/{p['dest'].name}{note}")

    if not args.apply:
        print("\n这是预览，没有改动任何文件。")
        print("确认后执行：vs.py chain --apply（会写入分镜的 image 字段）")
        return
    if not post.has_ffmpeg():
        fail("抽帧需要 ffmpeg：brew install ffmpeg")

    changed: list[str] = []
    for p in plan:
        if p["current"] and "chain/" not in p["current"] and not args.force:
            log(f"{p['to']} 已有自定义首帧，跳过（确要覆盖加 --force）", level="warn")
            continue
        post.extract_frame(p["src"], p["dest"], where="last")
        nxt = next((s for s in sb.shots if s.id == p["to"]), None)
        if nxt:
            nxt.image = str(p["dest"].relative_to(root))
            changed.append(nxt.id)
    sbmod.save(sb)
    log("已接好首尾帧，分镜的 image 字段已改写。", level="ok")
    if changed:
        print("\n⚠ 改的是首帧图，**已经生成好的视频不会自动变化**。")
        print(f"让衔接生效，需要重跑这些镜头（会再花一次生成费用）：")
        print(f"    vs.py run --only {','.join(changed)} --force")
        print("  下次从头做时更省的做法：直接 vs.py run --chain，边生成边衔接，不用重跑。")
        print("  预览计划用 vs.py chain（不带 --apply），不会改任何文件。")


def cmd_animatic(args: argparse.Namespace) -> None:
    """把草稿片段拼成粗剪，检查节奏。内部预演，不作为交付物。"""
    root = project_root(required=True)
    sb = sbmod.load(root / (args.storyboard or "storyboard.json"))
    files = list((root / "videos").glob("draft-*.mp4"))
    if not files:
        fail("没有草稿片段。先跑：vs.py run --stage draft")
    if not post.has_ffmpeg():
        fail("拼 animatic 需要 ffmpeg：brew install ffmpeg")

    ordered: list[Path] = []
    for s in sb.shots:
        hits = sorted(p for p in files
                      if p.name.startswith(f"draft-{s.id}-") or p.name == f"draft-{s.id}.mp4")
        if hits:
            ordered.append(hits[-1])
    if not ordered:
        fail("草稿文件名和分镜编号对不上，检查一下 videos/ 目录")

    w, h = post.size_for(sb.ratio, 720)
    work = state_dir(root) / "work" / "animatic"
    work.mkdir(parents=True, exist_ok=True)
    norm = []
    for i, c in enumerate(ordered, 1):
        dest = work / f"a-{i:02d}.mp4"
        post.normalize(c, dest, w=w, h=h, fps=args.fps)
        norm.append(dest)
    # 内部预演文件放 .vs/work/，绝不写进 output/：
    # 用户答「只要片段」时 output/ 必须是空的，不能被预演产物污染。
    out = state_dir(root) / "work" / "animatic.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    post.concat(norm, out)
    log(f"animatic 粗剪完成：{out}", level="ok")
    log(f"  {len(ordered)} 个草稿片段 · {post.duration_of(out):.1f}s · 720p", level="ok")
    print("这是内部预演，用来检查节奏和镜头顺序，不要直接交付给用户。")
    print("确认节奏 OK 后：vs.py run --stage final 出定稿（提示词不变，只换质量）。")


def cmd_qc(args: argparse.Namespace) -> None:
    root = project_root(required=True)
    sb = sbmod.load(root / (args.storyboard or "storyboard.json"))
    issues, manual, tips = qc(root, sb, Bible.load(root), TakesStore(root), JobStore(root).all())

    print("════ 自动检查 ════")
    if issues:
        for i, p in enumerate(issues, 1):
            print(f"  {i}. ⚠ {p}")
    else:
        print("  ✓ 没有发现结构性问题")
    if tips:
        print("\n════ 建议（不阻断，按项目类型判断）════")
        for t in tips:
            print(f"  · {t}")
    print("\n════ 人工过目（机器替不了的部分）════")
    for i, item in enumerate(manual, 1):
        print(f"  □ {i}. {item}")
    print("\n发现问题就在漂移传递下去之前用 vs.py run --only sX --force 重跑那一镜。")
    print("每镜的 prompt / seed / 模型 / 参考图都已记在 .vs/jobs.json，可复现。")


def cmd_estimate(args: argparse.Namespace) -> None:
    root = find_project_root()
    sb_path = Path(args.storyboard) if args.storyboard else (root / "storyboard.json" if root else None)
    if not sb_path or not sb_path.is_file():
        fail("找不到 storyboard.json")
    sb = sbmod.load(sb_path)
    seconds = sb.total_duration
    scale = cost_scale(args)
    print(f"项目：{sb.title} · {len(sb.shots)} 镜 · {seconds}s\n")
    if scale != 1.0:
        print(f"计费生成量：{seconds}s × {scale:g} = {seconds * scale:g}s"
              f"（{cost_note(args)}）\n")
    print(f"{'provider':<12} {'模型':<34} {'估算成本'}")
    print("-" * 66)
    for name in AUTO_ORDER:
        p = get_provider(name, Config())
        for m in p.models[:2]:
            print(f"{pad(name, 12)} {pad(m, 34)} ¥{est_cost(name, m, seconds) * scale:.1f}")
    print("\n注：按 1080p 档粗估，实际以各家控制台账单为准。")


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vs.py",
        description="AI 视频工作流：一个命令从想法到成片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出关键信息")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="自检环境与密钥")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("models", help="查看所有模型与能力")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("setup", help="配置密钥（看指引或直接写入）")
    p.add_argument("--set", nargs="*", metavar="PROVIDER=KEY",
                   help="例如 --set ARK_API_KEY=xxx（也可写 --set ark=xxx）")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("init", help="创建项目骨架")
    p.add_argument("title", nargs="?", default="", help="项目名")
    p.add_argument("--dir", help="目标目录（默认当前目录）")
    p.add_argument("--ratio", default="16:9", choices=list(post.RATIO_SIZE))
    p.add_argument("--shots", type=int, default=4, help="空镜头数（默认 4）")
    p.add_argument("--provider", default="auto")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("gen", help="生成单个片段")
    p.add_argument("-p", "--prompt", required=True)
    p.add_argument("-i", "--image", help="首帧图（本地路径或 URL）")
    p.add_argument("--last-image", help="尾帧图")
    p.add_argument("--ref", nargs="*", help="参考图（Vidu/Kling omni）")
    p.add_argument("-d", "--duration", type=int, default=5)
    p.add_argument("--ratio", default="16:9")
    p.add_argument("-r", "--resolution", default="1080p")
    p.add_argument("-m", "--model")
    p.add_argument("--provider", default="auto", choices=["auto", *PROVIDERS])
    p.add_argument("--seed", type=int)
    p.add_argument("--negative")
    p.add_argument("--audio", action="store_true",
                   help="让模型生成原生音效（必须先得到用户确认，默认关闭）")
    p.add_argument("--no-audio", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--shot", help="镜头名，用于文件命名")
    p.add_argument("--no-wait", action="store_true", help="提交后立即返回，不等待")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("run", help="按分镜批量生成")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.add_argument("--provider", default="auto", choices=["auto", *PROVIDERS])
    p.add_argument("--only", help="只跑指定镜头，如 s1,s3")
    p.add_argument("--force", action="store_true", help="重跑已存在的镜头")
    p.add_argument("--audio", action="store_true",
                   help="让模型生成原生音效（必须先得到用户确认，默认关闭）")
    p.add_argument("--no-audio", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--stage", choices=["draft", "final"], default="final",
                   help="draft=720p 便宜草稿看节奏（不写回分镜）；final=定稿")
    p.add_argument("--takes", type=int, default=1,
                   help="每镜出几个候选版本（A/B/C…），用 takes / pick 选版")
    p.add_argument("--use-refs", action="store_true",
                   help="把 bible.json 的参考图作为多图参考传给模型（需 provider 支持）")
    p.add_argument("--chain", action="store_true",
                   help="逐镜尾帧链：生成完一镜就抽末帧接成下一镜首帧（需 ffmpeg，且须先得到用户确认）")
    p.add_argument("--unlock-model", action="store_true",
                   help="解除本项目的模型锁定，允许换 provider（换模型会导致风格漂移）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list", help="查看任务列表")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="查单个任务")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("fetch", help="下载任务结果")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("resume", help="继续等待未完成任务")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("assemble", help="合成成片（默认纯拼接，附加效果需显式开启）")
    add_post_options(p, with_audio=False)
    p.add_argument("-o", "--out")
    p.add_argument("--rebuild", action="store_true", help="不复用中间文件")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("plan", help="打印开工确认单，贴给用户逐项核对")
    add_post_options(p)
    p.add_argument("--assemble", action="store_true",
                   help="本次计划包含剪辑合并；不带则计划只交付片段")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("clips", help="列出可直接交付的片段（不剪辑时用）")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.add_argument("--json", action="store_true", help="输出 JSON，便于程序化交付")
    p.set_defaults(func=cmd_clips)

    p = sub.add_parser("estimate", help="预估成本（按实际生成量报账）")
    p.add_argument("-s", "--storyboard")
    p.add_argument("--takes", type=int, default=1,
                   help="每镜生成几个候选版本，成本按倍数计")
    p.add_argument("--draft", action="store_true",
                   help="额外计入一轮 720p 草稿预演")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("bible", help="制作圣经：锁定角色块/风格/参考图/模型/seed")
    p.add_argument("--set", nargs="*", action="append", metavar="字段=值",
                   help="可重复；也可一次写多个：--set identity='...' seed=12345")
    p.add_argument("--lock", action="store_true", help="锁定模型，禁止中途换")
    p.add_argument("--unlock", action="store_true", help="解除锁定")
    p.set_defaults(func=cmd_bible)

    p = sub.add_parser("takes", help="列出每镜的候选版本")
    p.set_defaults(func=cmd_takes)

    p = sub.add_parser("pick", help="选定某个候选版本回填分镜")
    p.add_argument("shot", help="镜头号，如 s1")
    p.add_argument("label", help="版本字母，如 B")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("chain", help="上一镜末帧接下一镜首帧（默认只预览）")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.add_argument("--apply", action="store_true", help="真的抽帧并写入分镜")
    p.add_argument("--force", action="store_true", help="覆盖已有的自定义首帧")
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("animatic", help="把草稿片段拼成粗剪，检查节奏")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.add_argument("--fps", type=int, default=30)
    p.set_defaults(func=cmd_animatic)

    p = sub.add_parser("qc", help="成片前质检（自动检查 + 人工清单）")
    p.add_argument("-s", "--storyboard", default="storyboard.json")
    p.set_defaults(func=cmd_qc)

    return ap


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    set_quiet(getattr(args, "quiet", False))
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。已提交的任务不会丢，用 vs.py resume 继续。", file=sys.stderr)
        raise SystemExit(130)
    except BrokenPipeError:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
