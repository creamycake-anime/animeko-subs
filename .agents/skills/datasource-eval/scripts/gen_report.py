#!/usr/bin/env python3
"""生成三层图文报告: README 索引 → sources/<源>.md → channels/<源-线路>.md

数据来源:
  subjects/<id>-<名>/summary.json      跨番快指标 (分辨率/码率(ffprobe 优先)/起播/HLS 过滤器结论)
  subjects/<id>-<名>/sources/*.json     逐源 trace (每线路 resolve/probe 成功失败 + 错误原因)
  deep/<tier>-<源>/deep.json            深采: 长播多点截图 + 播放性能 + adDetect 结构探测 + ffprobe
  combined/frames_verdicts.json         视觉广告标注 (subagent 逐张看图)
"""
import json
import os
import pathlib
import re
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import pick_bitrate_resolution, playback_ok

BASE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
META = json.loads((BASE / "meta.json").read_text(encoding="utf-8")) if (BASE / "meta.json").exists() else {}
EVAL_DATE = META.get("evalDate", "?")
SUBJECTS_DIR = BASE / "subjects"
DEEP_DIR = BASE / "deep"
VERDICTS_FILE = BASE / "combined" / "frames_verdicts.json"
CH_DIR = BASE / "channels"
SRC_DIR = BASE / "sources"

AD_RANK = {"none": 0, "clean": 0, "low": 1, "suspected_low": 1, "medium": 2,
           "suspected_medium": 2, "high": 3, "suspected_high": 3, "unknown": -1, None: -1}
AD_EMOJI = {0: "✅ 无广告", 1: "🟡 轻度(横幅仅片头/片尾)", 2: "🟠 中度(片中也有横幅)",
            3: "🔴 重度(全程水印/滤不掉的插入广告)", -1: "❔ 未判定"}
AD_SHORT = {0: "无", 1: "轻", 2: "中", 3: "重", -1: "-"}


def res_s(r):
    return r or "-"


def ttp_s(t):
    return f"{t}ms" if t else "-"


def br_s(b):
    # 两位小数: 1.46M 不会被读成 1.5M, 避免与 tier 阈值对不上
    return f"{b/1_000_000:.2f}M" if b else "-"


def res_h(r):
    """分辨率高度 (排序用)."""
    try:
        return int((r or "0x0").split("x")[1])
    except Exception:
        return 0


def slug(s):
    return re.sub(r"[^\w一-鿿]+", "_", str(s if s is not None else "默认线路")).strip("_")


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


VERDICTS = json.loads(VERDICTS_FILE.read_text(encoding="utf-8")).get("_channel_level", {}) if VERDICTS_FILE.exists() else {}


def visual_ad(source, channel):
    v = VERDICTS.get(f"{source}/{channel}") or VERDICTS.get(source)
    return (v or {}).get("ad"), (v or {}).get("note")


def load_subjects():
    out = {}
    for d in sorted(SUBJECTS_DIR.glob("*")):
        sj = d / "summary.json"
        if sj.exists():
            sid = d.name.split("-", 1)[0]
            out[sid] = {"dir": d, "name": d.name.split("-", 1)[-1],
                        "rows": {r["source"]: r for r in json.loads(sj.read_text(encoding="utf-8"))}}
    return out


def load_deep():
    out = {}
    for dj in DEEP_DIR.glob("*/deep.json"):
        rec = json.loads(dj.read_text(encoding="utf-8"))
        out[rec["source"]] = rec
    return out


def rel(path_str, from_dir):
    return os.path.relpath(path_str, from_dir)


def disp_ch(cname):
    return cname if cname else "默认线路"


def per_subject_cells(st, sids):
    """每部番一格: ✅可播 / ❌失败 / — 未出现."""
    out = []
    for sid in sids:
        if sid in st["ok_sids"]:
            out.append("✅")
        elif sid in st["appear_sids"]:
            out.append("❌")
        else:
            out.append("—")
    return out


def summarize_fail(r):
    """把一条 extractResult 的失败归纳成人话原因."""
    errs = r.get("errors") or []
    txt = " ".join(str(e) for e in errs) or (r.get("summary") or "")
    low = txt.lower()
    if "no video url matched" in low:
        return "WebView 未匹配到视频 URL(matchVideo 正则或播放页结构失配)"
    if "403" in txt:
        return "视频 URL 返回 403(防盗链 / 地区限制)"
    if "404" in txt:
        return "视频 URL 返回 404"
    if "timeout" in low or "超时" in txt:
        return "解析 / 播放超时"
    if r.get("resolveStatus") == "failed":
        return "WebView 解析失败: " + (txt[:50] or "未知")
    if r.get("probeStatus") == "failed":
        return "探测失败: " + ((r.get("summary") or txt)[:50] or "未知")
    return (txt[:70] or "未知")


def summarize_probe_fail(probe):
    """把播放器探测失败归纳成人话；可播只认 probe.ok，不能把 URL 解析成功当成实播成功。"""
    txt = " ".join(str(x) for x in (probe.get("errors") or []))
    summary = str(probe.get("summary") or "")
    combined = f"{summary} {txt}".strip()
    low = combined.lower()
    if "403" in combined:
        return "视频 URL 返回 403(防盗链 / 地区限制)"
    if "404" in combined:
        return "视频 URL 返回 404"
    if "timeout" in low or "超时" in combined:
        return "播放器实播超时 / 卡顿"
    if "state=error" in low or "播放器无法播放" in combined:
        return "播放器无法播放"
    return "播放探测失败: " + (combined[:55] or "未知")


# ---- 逐源 trace: 每线路跨番 成功/失败/原因 ----

def load_source_trace(subject_dir, tier, source):
    f = subject_dir / "sources" / f"{tier}-{source}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def classify_step_fail(step):
    """(阶段, 人话原因). 阶段='搜索' 的归入搜索阶段失败一节."""
    name = step.get("name", "")
    txt = (step.get("summary", "") + " " + " ".join(str(e) for e in (step.get("errors") or [])))
    low = txt.lower()
    if name == "searchSubjects":
        if "验证" in txt or "captcha" in low or "拦截" in txt:
            return ("搜索", "站点需人机验证(图片验证码 / Cloudflare)")
        if "network" in low or "网络" in txt or "unknownhost" in low or "connect" in low:
            return ("搜索", "网络错误 / 站点无法连接(域名失效或已下线)")
        if "404" in txt:
            return ("搜索", "搜索页返回 404")
        if "403" in txt or "429" in txt:
            return ("搜索", "搜索页被封禁(403 / 429)")
        if "unknown" in low or "repositoryunknown" in low:
            return ("搜索", "搜索请求异常(站点返回异常响应 / 已下线)")
        return ("搜索", "搜索请求失败: " + (txt.strip()[:40] or "未知"))
    if name == "selectSubjects":
        return ("解析条目", "条目 selector 失配 / 搜索无结果(站点结构变更或配置失效)")
    if name == "searchEpisodes":
        return ("详情页", "条目详情页获取失败(404 / 网络)")
    if name == "selectEpisodes":
        return ("解析剧集", "剧集 selector 失配(站点结构变更或配置失效)")
    if name == "selectMedia":
        if "must start with" in low or "webvideo uri" in low:
            return ("链接", "播放链接非 http(js 伪链接 / 配置问题)")
        return ("匹配", "选集失败: " + (txt.strip()[:40] or "未知"))
    if name == "extractVideo":
        return ("视频解析", "WebView 未解析出视频 URL")
    return (name or "未知", txt.strip()[:50] or "未知")


def dead_reason(source, tier, subjects):
    """跨番聚合全失败源的失败阶段与原因, 取最常见."""
    from collections import Counter
    counter = Counter()
    for sd in subjects.values():
        trace = load_source_trace(sd["dir"], tier, source)
        if not trace:
            continue
        rv = trace.get("resolve", {})
        steps = rv.get("steps", [])
        failed = next((s for s in steps if s.get("status") == "failed"), None)
        if failed:
            counter[classify_step_fail(failed)] += 1
        else:
            summ = rv.get("summary", "")
            if "没有找到匹配" in summ or "未找到" in summ:
                counter[("匹配", "解析成功但未匹配到目标集数(选集为空)")] += 1
            elif "未通过播放探测" in summ:
                counter[("播放探测", "视频 URL 解析成功但播放探测失败(403 / 防盗链 / 地区限制)")] += 1
            elif "未能解析出视频" in summ:
                counter[("视频解析", "找到候选播放页, 但 WebView 未解析出视频 URL")] += 1
            else:
                counter[("未知", summ[:40] or "无明确失败步骤")] += 1
    if not counter:
        return ("未知", "无 trace 数据(可能未跑到该源)")
    return counter.most_common(1)[0][0]


# ---- 数据聚合 ----

def aggregate(subjects, deep):
    all_sources = {}
    for sd in subjects.values():
        for name, row in sd["rows"].items():
            all_sources.setdefault(name, row["tier"])
    agg = {}
    for source, tier in all_sources.items():
        attempted = ok = 0
        per_subject = {}
        # channel -> 聚合
        channels = {}

        def ch_entry(cname):
            return channels.setdefault(cname, {
                "res": [], "brs": [], "ttp": [], "codecs": [], "ok_subjects": set(),
                "appear": set(), "fails": [], "adsusp": [],
            })

        attempted_sids = set()
        for sid, sd in subjects.items():
            row = sd["rows"].get(source)
            if not row:
                continue
            attempted += 1
            attempted_sids.add(sid)
            trace = load_source_trace(sd["dir"], tier, source)
            # summary 快数据: 每线路实播的 分辨率/码率/起播/广告启发式
            for ch in (row.get("perChannel") or []):
                c = ch_entry(ch.get("channel"))
                c["appear"].add(sid)
                # 可播以 trace 的实播结果为准 (playback.ran && ok); 只有缺 trace 的旧数据才退 playerOk
                if trace is None and ch.get("playerOk"):
                    c["ok_subjects"].add(sid)
                if ch.get("resolution"):
                    c["res"].append(ch["resolution"])
                if ch.get("bitrate"):
                    c["brs"].append(ch["bitrate"])
                if ch.get("codec"):
                    c["codecs"].append(ch["codec"])
                if ch.get("timeToPlayingMillis"):
                    c["ttp"].append(ch["timeToPlayingMillis"])
                if ch.get("adSuspicion"):
                    c["adsusp"].append(ch["adSuspicion"])
            # trace: 每线路 成功/失败/错误原因
            if trace:
                resolve = trace.get("resolve", {})
                mid2ch = {m["mediaId"]: m.get("channel") for m in resolve.get("medias", [])}
                # 真实可播只认播放器实播 (playback.ran && ok, 见 lib.playback_ok)。
                # probe.ok 在 mpv 不可用时退回 HTTP 探测结论, extractResult.ok 仅代表拿到
                # 了视频 URL——都不能算实播成功。
                for p in trace.get("playerProbes", []):
                    cn = p.get("channel")
                    c = ch_entry(cn)
                    c["appear"].add(sid)
                    probe = p.get("probe") or {}
                    if playback_ok(probe):
                        c["ok_subjects"].add(sid)
                    else:
                        c["fails"].append((sd["name"], summarize_probe_fail(probe)))
                for r in resolve.get("extractResults", []):
                    cn = mid2ch.get((r.get("candidate") or {}).get("mediaId"))
                    c = ch_entry(cn)
                    c["appear"].add(sid)
                    if not r.get("ok"):
                        c["fails"].append((sd["name"], summarize_fail(r)))

        # deep 长播 (基础 28s, 检出疑似广告时延长) 的实测数据并入线路聚合 (比 4s 快测更可靠);
        # deep 实播成功也算可播证据 (失败不算 ❌: deep 是二次解析, 可能只是 URL 过期)
        for run in (deep.get(source) or {}).get("runs", []):
            rsid = str(run.get("subjectId"))
            for dc in run.get("channels", []):
                c = ch_entry(dc.get("channel"))
                if rsid in subjects and playback_ok(dc.get("probe")):
                    c["appear"].add(rsid)
                    c["ok_subjects"].add(rsid)
                ma = (dc.get("probe") or {}).get("mediaAnalysis") or {}
                v = ma.get("video") or {}
                pb = ma.get("playback") or {}
                # 码率/分辨率优先 ffprobe 实测 (deep 记录里的 ffprobe 字段; 旧数据没有则退播放器)
                br, _src, res = pick_bitrate_resolution(dc.get("ffprobe"), ma)
                if res:
                    c["res"].append(res)
                if br:
                    c["brs"].append(br)
                codec = v.get("codec") or ((dc.get("ffprobe") or {}).get("streams") or {}).get("vcodec")
                if codec:
                    c["codecs"].append(codec)
                if pb.get("timeToPlayingMillis"):
                    c["ttp"].append(pb["timeToPlayingMillis"])

        # 源级和番格成功率同样只认真实播放器成功（快测或 deep 任一成功即可）。
        # 番格三态: 只对真实尝试过的番记 ✅/❌; 未尝试 (断点续跑局部数据) 不进 perSubject, 渲染为 —。
        actual_ok_sids = set()
        for c in channels.values():
            actual_ok_sids.update(c["ok_subjects"])
        per_subject = {sid: sid in actual_ok_sids for sid in attempted_sids}
        ok = len(actual_ok_sids)

        # 源级广告: 只用视觉判读 (subagent 看图), 各线路取最干净可选 (min)
        ch_ads = []
        for cname in channels:
            v, _ = visual_ad(source, cname)
            if v is not None:
                ch_ads.append(AD_RANK.get(v, -1))
        ch_ads = [a for a in ch_ads if a >= 0]
        ad_clean = min(ch_ads) if ch_ads else -1
        ad_worst = max(ch_ads) if ch_ads else -1
        # 总表口径: 最佳线路 = 实播成功过的线路里, 按 无广告 > 分辨率 > 码率 > 起播 排第一
        best = None
        for cname, c in channels.items():
            if not c["ok_subjects"]:
                continue
            st = channel_stats(source, cname, c, attempted)
            if best is None or channel_rank_key(st) < channel_rank_key(best["st"]):
                best = {"channel": cname, "st": st}
        agg[source] = {
            "tier": tier, "okNum": ok, "attempted": attempted,
            "best": best,
            "perSubject": per_subject, "channels": channels,
            "adClean": ad_clean, "adWorst": ad_worst,
        }
    return agg


def channel_stats(source, cname, c, n_subjects):
    """归纳单线路: 广告等级, 成功番数, 分辨率(跨番众数), 码率/起播(中位数), 失败原因."""
    ad_v, ad_note = visual_ad(source, cname)
    ad_rank = AD_RANK.get(ad_v, -1) if ad_v is not None else -1
    ok_n = len(c["ok_subjects"])
    res = max(set(c["res"]), key=c["res"].count) if c["res"] else None
    br = med(c["brs"])
    ttp = med(c["ttp"])
    codec = max(set(c["codecs"]), key=c["codecs"].count) if c["codecs"] else None
    # 失败原因: 取最常见
    reasons = [r for _, r in c["fails"]]
    top_reason = max(set(reasons), key=reasons.count) if reasons else None
    return {"ad_rank": ad_rank, "ad_note": ad_note, "ok_n": ok_n, "res": res,
            "br": br, "ttp": ttp, "codec": codec, "fails": c["fails"],
            "top_reason": top_reason, "appear": len(c["appear"]),
            "ok_sids": set(c["ok_subjects"]), "appear_sids": set(c["appear"])}


def channel_rank_key(st):
    """"最好线路"排序键 (小者优), 逐级比较: ①无广告(视觉判定, 未判定排最后)
    ②分辨率高 ③码率高 ④起播快."""
    return (st["ad_rank"] if st["ad_rank"] >= 0 else 9,
            -res_h(st["res"]), -(st["br"] or 0), st["ttp"] or 10**9)


# 能力分级: (Tier, [备选条件组...]), 每组 = (广告等级上限(None=不限), 最低分辨率高度,
# 最低码率bps, 最大起播ms). 从上往下取第一个有任一条件组满足的 tier.
CAP_TIERS = [
    ("T0", [(0, 1080, 1_800_000, 3000)]),
    ("T1", [(0, 1080, 1_500_000, 5000)]),
    ("T2", [(0, 1080, 1_200_000, 5000)]),
    ("T3", [(1, 1080, 1_500_000, 8000),   # ≤轻广告
            (0, 1080, 1_000_000, 8000)]),  # 或无广告低码率
    ("T4", [(1, 1080, 1_000_000, 8000)]),  # ≤轻广告低码率
    ("T5", [(None, 1080, 1_000_000, None)]),
    ("T6", [(None, 0, 0, None)]),
]
HEAVY_AD_MIN_TIER = "T5"  # 硬性约束: 重度广告 (全程水印/滤不掉的插入广告) 必须落在 T5 及之后


def capability_tier(st):
    """按硬指标给线路分级 T0-T6. 广告等级只认视觉判定, 未判定不满足任何广告上限要求;
    缺码率/起播数据视为不满足该项要求; 重度广告最高只能到 HEAVY_AD_MIN_TIER."""
    ad = st["ad_rank"]
    h = res_h(st["res"])
    br = st["br"] or 0
    ttp = st["ttp"]
    for name, alts in CAP_TIERS:
        if ad >= 3 and name < HEAVY_AD_MIN_TIER:
            continue
        for max_ad, min_h, min_br, max_ttp in alts:
            if max_ad is not None and (ad < 0 or ad > max_ad):
                continue
            if h < min_h or br < min_br:
                continue
            if max_ttp is not None and (not ttp or ttp > max_ttp):
                continue
            return name
    return "T6"


# ---- channel 报告 ----

def channel_hls_filter(source, cname, deep_runs, subjects):
    """取该线路的 HLS 结构探测结论: deep 的 detect_hls_ads (adDetect) 优先,
    退 summary 的 adFilter (probe adAnalysis.hlsFilter). 优先取检出过插入片段的那份."""
    found = None
    for _, dc in deep_runs:
        h = ((dc.get("adDetect") or {}).get("analysis") or {}).get("hlsFilter")
        if h:
            if h.get("removedGroups"):
                return h
            found = found or h
    for sd in subjects.values():
        row = sd["rows"].get(source) or {}
        for ch in row.get("perChannel") or []:
            if ch.get("channel") == cname and ch.get("adFilter"):
                h = ch["adFilter"]
                if h.get("removedGroups"):
                    return h
                found = found or h
    return found


def hls_structural_line(source, cname, deep_runs, subjects):
    """广告判定的辅助证据行. 等级仍以 frames_verdicts 的视觉判读为准 (烧录水印看不见)."""
    h = channel_hls_filter(source, cname, deep_runs, subjects)
    if not h:
        return None
    groups = h.get("removedGroups") or []
    if groups:
        rngs = ", ".join(
            f"{int(g.get('startOffsetSeconds') or 0)}-{int(g.get('endOffsetSeconds') or 0)}s"
            for g in groups)
        can = "**可**" if h.get("filterable") else "**不可**"
        return (f"HLS 结构探测: 检出 {len(groups)} 组插入片段 ({rngs}), "
                f"Ani 客户端广告过滤器{can}自动滤除。(辅助证据, 等级以看图为准)")
    # unchanged 的这几种 reason 是"检出候选但拒绝滤除", 不是干净——按滤不掉口径处理
    UNFILTERABLE_REASONS = {
        "all_segments_candidate": "全部分组都命中候选, 拒绝滤除以防误删正片",
        "encrypted_implicit_iv": "AES-128 无显式 IV, 删段会破坏解密链",
        "byterange_implicit_offset": "byterange 无显式偏移, 删段会破坏偏移链",
    }
    if h.get("status") == "unchanged" and h.get("reason") in UNFILTERABLE_REASONS:
        return (f"HLS 结构探测: 检出疑似插入片段, 但过滤器**不可**自动滤除"
                f" ({UNFILTERABLE_REASONS[h['reason']]})。(辅助证据, 等级以看图为准; 按滤不掉口径处理)")
    if h.get("status") == "unsupported":
        return "HLS 结构探测: 过滤器不支持该清单格式, 无法给出自动滤除结论 (按滤不掉口径处理)。"
    return "HLS 结构探测: 无插入片段迹象 (烧录水印/横幅需看图判定)。"


def channel_report(source, tier, cname, c, subjects, deep):
    CH_DIR.mkdir(exist_ok=True)
    d = disp_ch(cname)
    fname = f"{tier}-{slug(source)}-{slug(d)}.md"
    fp = CH_DIR / fname
    st = channel_stats(source, cname, c, len(subjects))
    ad_rank = st["ad_rank"]
    res = st["res"] or "-"
    ttp = st["ttp"]
    ok_n = st["ok_n"]

    deep_runs = []
    if source in deep:
        for run in deep[source]["runs"]:
            for dc in run["channels"]:
                if dc["channel"] == cname:
                    deep_runs.append((run["subjectName"], dc))

    md = [f"# {source} · {d}\n"]
    md.append(f"> {tier} · {res} · {br_s(st['br'])} · {AD_EMOJI[ad_rank]} · 起播 {ttp or '?'}ms · 跨番实播成功 {ok_n}/{len(subjects)}\n")
    md.append(f"[← 返回 {source} 源页](../sources/{tier}-{slug(source)}.md) · [← 总索引](../README.md)\n")

    md.append(f"\n## 广告判定: {AD_EMOJI[ad_rank]}\n")
    if st["ad_note"]:
        md.append(f"**视觉判断**: {st['ad_note']}\n")
    elif ad_rank == -1:
        md.append("*(该线路未纳入视觉判读)*\n")
    hls_line = hls_structural_line(source, cname, deep_runs, subjects)
    if hls_line:
        md.append(hls_line + "\n")

    if deep_runs:
        md.append("\n## 播放采样\n")
        md.append("每部番长播实测 (基础 28s, 检出疑似插入广告段时延长), 在 0/3/8/15/25s "
                  "及疑似广告段中点截图. 首帧仍是广告说明前贴片较长.\n")
        for sub_name, dc in deep_runs:
            frames = [f for f in (dc["probe"].get("capturedFrames") or [])
                      if f["label"].startswith("frame_")]
            frames = sorted(frames, key=lambda f: f.get("positionMillis", 0))
            if not frames:
                continue
            md.append(f"\n### {sub_name}\n")
            row, sep = [], []
            for f in frames:
                r = rel(f["path"], CH_DIR)
                lbl = f["label"].replace("frame_", "").rstrip("s") + "s"
                row.append(f'<img src="{r}" width="300"><br>{lbl}')
                sep.append("---")
            md.append("| " + " | ".join(row) + " |")
            md.append("| " + " | ".join(sep) + " |")
    else:
        md.append("\n*(该线路未纳入深度采样, 无多点截图)*\n")

    sample = next((dc for _, dc in deep_runs if playback_ok(dc["probe"])), None)
    if not sample and deep_runs:
        sample = deep_runs[0][1]
    if sample:
        ma = sample["probe"].get("mediaAnalysis") or {}
        v = ma.get("video") or {}
        a = ma.get("audio") or {}
        pb = ma.get("playback") or {}
        fst = (sample.get("ffprobe") or {}).get("streams") or {}
        md.append("\n## 媒体信息\n")
        md.append("| 分辨率 | 视频编码 | 帧率 | 音频 | 时长 | 码率 | 码率来源 |")
        md.append("|---|---|---|---|---|---|---|")
        dur = ma.get("durationSeconds") or fst.get("durationSeconds")
        durs = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
        # 码率/分辨率优先 ffprobe 实测 (旧数据没有 ffprobe 字段则退播放器统计)
        br, br_src, res = pick_bitrate_resolution(sample.get("ffprobe"), ma)
        brs = f"{br/1_000_000:.1f} Mbps" if br else "?"
        src_label = {"ffprobe_measured": "ffprobe 实测均值", "ffprobe_format": "ffprobe format",
                     "player": "播放器统计"}.get(br_src, "-")
        codec = v.get("codec") or fst.get("vcodec") or "?"
        fps = v.get("frameRate") or fst.get("fps") or "?"
        md.append(f"| {res or '?'} | {codec} | {fps}fps | "
                  f"{a.get('codec')} {a.get('sampleRate')}Hz {a.get('channels')}ch | {durs} | {brs} | {src_label} |")
        md.append("\n## 播放性能\n")
        md.append("| 打开 | 起播 | 首帧 | 卡顿 |")
        md.append("|---|---|---|---|")
        md.append(f"| {pb.get('openMillis')}ms | {pb.get('timeToPlayingMillis')}ms | "
                  f"{pb.get('timeToFirstFrameMillis')}ms | {pb.get('bufferingCount')} 次 |")

    # 跨番实播 + 失败原因 (与聚合同口径: 快测或 deep/backfill 任一实播成功即 ✅)
    fail_by_subj = {sub: reason for sub, reason in st["fails"]}
    md.append("\n## 跨番实播\n")
    md.append("| 番剧 | 结果 | 失败原因 |")
    md.append("|---|---|---|")
    for sid, sd in subjects.items():
        if sid in st["ok_sids"]:
            mark, reason = "✅ 可播", ""
        elif sd["name"] in fail_by_subj:
            mark, reason = "❌ 失败", fail_by_subj[sd["name"]]
        elif sid in st["appear_sids"]:
            mark, reason = "❌ 失败", "实播未成功(无具体原因记录)"
        else:
            mark, reason = "— 未出现", "该番未解析到此线路"
        md.append(f"| {sd['name']} | {mark} | {reason} |")

    fp.write_text("\n".join(md), encoding="utf-8")
    return fname, st


# ---- source 页 ----

def source_report(source, tier, a, subjects, ch_reports):
    SRC_DIR.mkdir(exist_ok=True)
    fp = SRC_DIR / f"{tier}-{slug(source)}.md"
    md = [f"# {source}\n"]
    ad_clean, ad_worst = a["adClean"], a["adWorst"]
    ad_txt = AD_EMOJI[ad_clean] + (f" · 另有线路{AD_SHORT[ad_worst]}" if ad_worst > ad_clean else "")
    best = a["best"]
    best_res = (best["st"]["res"] if best else None) or "?"
    md.append(f"> {tier} · 跨番成功率 {a['okNum']}/{a['attempted']} · {best_res} · {ad_txt}\n")
    md.append("[← 总索引](../README.md)\n")

    # 推荐线路 (无广告优先, 排序同总表最佳线路口径, 再看成功番数)
    recs = [(cn, info) for cn, info in ch_reports.items()
            if info["st"]["ad_rank"] == 0 and info["st"]["ok_n"] >= 1]
    recs.sort(key=lambda x: channel_rank_key(x[1]["st"]) + (-x[1]["st"]["ok_n"],))
    if recs:
        top = recs[0]
        md.append(f"\n**推荐线路: {disp_ch(top[0])}** — 无广告, {top[1]['st']['res'] or '?'}, "
                  f"{br_s(top[1]['st']['br'])}, 跨番 {top[1]['st']['ok_n']}/{len(subjects)} 可播, "
                  f"起播 {top[1]['st']['ttp'] or '?'}ms\n")

    sids = list(subjects.keys())
    subj_short = [sd["name"][:2] for sd in subjects.values()]
    md.append("\n## 线路拆解\n")
    md.append("每条线路 × 每部番实播: ✅可播 / ❌失败 / — 该番未解析到此线路. ⭐ = 总表所用最佳线路.\n")
    md.append("| 线路 | 广告 | 分辨率 | 码率 | 起播 | " + " | ".join(subj_short) + " | 主要失败原因 | 报告 |")
    md.append("|---|---|---|---|---|" + "---|" * len(subj_short) + "---|---|")

    def ch_sort(item):
        cn, info = item
        s = info["st"]
        return (-s["ok_n"], s["ad_rank"] if s["ad_rank"] >= 0 else 9, str(cn))
    for cn, info in sorted(ch_reports.items(), key=ch_sort):
        s = info["st"]
        star = "⭐ " if best and cn == best["channel"] else ""
        adcell = AD_SHORT[s["ad_rank"]]
        reason = s["top_reason"] if (s["ok_n"] < len(subjects) and s["top_reason"]) else ""
        cells = " | ".join(per_subject_cells(s, sids))
        md.append(f"| {star}{disp_ch(cn)} | {adcell} | {res_s(s['res'])} | {br_s(s['br'])} | {ttp_s(s['ttp'])} | "
                  f"{cells} | {reason} | [详情](../channels/{info['fname']}) |")

    md.append("\n## 跨番解析\n")
    md.append("| 番剧 | 整体解析 |")
    md.append("|---|---|")
    for sid, sd in subjects.items():
        mark = "✅" if a["perSubject"].get(sid) else ("❌" if sid in a["perSubject"] else "— 未测")
        md.append(f"| {sd['name']} | {mark} |")
    fp.write_text("\n".join(md), encoding="utf-8")
    return f"{tier}-{slug(source)}.md"


# ---- 主 ----

def main():
    subjects = load_subjects()
    deep = load_deep()
    agg = aggregate(subjects, deep)
    sids = list(subjects.keys())
    n = len(sids)
    subj_short = [sd["name"][:2] for sd in subjects.values()]

    src_pages = {}
    all_channel_stats = []  # (source, tier, cname, st) 供推荐榜
    for source, a in agg.items():
        if a["okNum"] == 0:
            continue
        ch_reports = {}
        for cname, c in a["channels"].items():
            # 跳过从没出现也没数据的空线路
            if not c["appear"] and not c["res"] and not c["fails"]:
                continue
            fname, st = channel_report(source, a["tier"], cname, c, subjects, deep)
            ch_reports[cname] = {"fname": fname, "st": st}
            all_channel_stats.append((source, a["tier"], cname, st, fname))
        src_pages[source] = source_report(source, a["tier"], a, subjects, ch_reports)

    usable = [(s, a) for s, a in agg.items() if a["okNum"] > 0]
    dead = sorted(f"{s}({a['tier']})" for s, a in agg.items() if a["okNum"] == 0)

    md = ["# ani-subs CSS Selector 数据源评估报告\n"]
    md.append(f"**评测日期: {EVAL_DATE}**\n")
    md.append(f"跨 **{n}** 部番剧 × **{len(agg)}** 个源. "
              "每源全流程解析 + 每条线路用 Animeko 桌面播放器 (mpv) 实播 + ffprobe 实测码率 "
              "+ HLS 结构广告探测 (真实客户端过滤器) + 多点截图 + subagent 逐张看图判广告.\n")
    md.append("\n测试番剧: " + ", ".join(sd["name"] for sd in subjects.values()) + "\n")
    md.append(f"\n**{len([1 for _,a in usable if a['okNum']==a['attempted']])}** 个稳定可用 · "
              f"**{len([1 for _,a in usable if 0<a['okNum']<a['attempted']])}** 个部分可用 · "
              f"**{len(dead)}** 个全部失败\n")

    # 推荐榜: 每个源挑一条最佳线路 (无广告 + 有实测数据 + 成功番多 + 画质好 + 码率高 + 起播快)
    best_per_source = {}
    for source, tier, cname, st, _fn in all_channel_stats:
        # 跨番成功 ≥ n-1 (与 SKILL/规范一致; 旧的 max(4, n-1) 在 3 部番时永远无解)
        if st["ad_rank"] != 0 or st["res"] is None or st["ok_n"] < max(1, n - 1):
            continue
        key = (-res_h(st["res"]), -st["ok_n"], -(st["br"] or 0), st["ttp"] or 9999)
        cur = best_per_source.get(source)
        if cur is None or key < cur[0]:
            best_per_source[source] = (key, tier, cname, st)
    recs = [(s, v[1], v[2], v[3]) for s, v in best_per_source.items()]
    recs.sort(key=lambda x: (-res_h(x[3]["res"]), -x[3]["ok_n"], -(x[3]["br"] or 0), x[3]["ttp"] or 9999))
    md.append("\n## 🏆 推荐线路 (无广告 + 跨番稳定, 直接播这条)\n")
    if recs:
        md.append("| 源 | 线路 | Tier | 分辨率 | 码率 | 起播 | 可播 |")
        md.append("|---|---|---|---|---|---|---|")
        for source, tier, cname, st in recs:
            md.append(f"| [{source}](sources/{src_pages[source]}) | **{disp_ch(cname)}** | {tier} | "
                      f"{st['res'] or '?'} | {br_s(st['br'])} | {st['ttp'] or '?'}ms | {st['ok_n']}/{n} |")
    else:
        md.append("*(无同时满足 无广告 + 跨番稳定 的线路)*\n")

    # 能力分级表: 每条实测线路一行, 按 T0-T5 硬指标分级
    md.append("\n## 📶 线路能力分级 (Tier)\n")
    md.append("每条线路按实测硬指标分级 (与 subs/web 的 t0–t4 **目录**分层无关). "
              "无广告只认视觉判定, 未判定不算无广告; 缺数据视为不满足:\n")
    md.append("> **T0** 无广告·1080P·码率≥1.8M·起播≤3s (该源查询成功即可直接选择, 无需等待其他源) · "
              "**T1** 无广告·1080P·≥1.5M·≤5s · **T2** 无广告·1080P·≥1.2M·≤5s · "
              "**T3** 1080P·(≤轻广告·≥1.5M·≤8s 或 无广告·≥1.0M·≤8s) · "
              "**T4** ≤轻广告·1080P·≥1.0M·≤8s · **T5** 1080P·≥1.0M · **T6** 无要求. "
              "重度广告 (全程水印/滤不掉的插入广告) 必须落在 T5 及之后.\n")
    md.append("| 源(线路) | Tier | 分辨率 | 码率 | 编码 | 起播 | 广告 | " + " | ".join(subj_short) + " |")
    md.append("|---|---|---|---|---|---|---|" + "---|" * n)
    tiered = sorted(all_channel_stats,
                    key=lambda x: (capability_tier(x[3]), -x[3]["ok_n"]) + channel_rank_key(x[3]) + (x[0],))
    for source, tier, cname, st, fname in tiered:
        cells = " | ".join(per_subject_cells(st, sids))
        md.append(f"| [{source}({disp_ch(cname)})](channels/{fname}) | **{capability_tier(st)}** | "
                  f"{res_s(st['res'])} | {br_s(st['br'])} | {st['codec'] or '-'} | {ttp_s(st['ttp'])} | "
                  f"{AD_SHORT[st['ad_rank']]} | {cells} |")

    md.append("\n## 全部可用源 (按广告轻重 → 成功率)\n")
    md.append("| 源 | Tier | 成功率 | 最佳线路 | 分辨率 | 码率 | 起播 | 广告 | " +
              " | ".join(sd["name"][:2] for sd in subjects.values()) + " |")
    md.append("|---|---|---|---|---|---|---|---|" + "---|" * n)
    for s, a in sorted(usable, key=lambda x: (x[1]["adClean"] if x[1]["adClean"] >= 0 else 9,
                                              -x[1]["okNum"], x[1]["tier"])):
        b = a["best"]
        bst = b["st"] if b else None
        bname = disp_ch(b["channel"]) if b else "-"
        adtxt = AD_SHORT[a["adClean"]] + ("*" if a["adWorst"] > a["adClean"] else "")
        cells = " | ".join(
            "✅" if a["perSubject"].get(sid) else ("❌" if sid in a["perSubject"] else "—")
            for sid in sids)
        md.append(f"| [{s}](sources/{src_pages[s]}) | {a['tier']} | {a['okNum']}/{a['attempted']} | "
                  f"{bname} | {res_s(bst['res']) if bst else '-'} | {br_s(bst['br']) if bst else '-'} | "
                  f"{ttp_s(bst['ttp']) if bst else '-'} | {adtxt} | {cells} |")
    md.append("\n> 分辨率/码率/起播 = 该源**最佳线路**的实测值 (最佳线路排序: ①无广告(视觉判定, 未判定排最后) "
              "②分辨率 ③码率 ④起播; 只在实播成功过的线路中选; 分辨率取跨番众数, 码率/起播取中位数). "
              "广告列: 最干净可选线路等级; `*` 表示该源另有更脏线路.\n")

    # 各源线路拆解 (直接嵌入 README, 无需点进源页)
    md.append("\n## 各源线路拆解\n")
    md.append("每个源逐条线路 × 每部番实播: ✅可播 / ❌失败 / — 该番未解析到此线路. "
              "广告 / 分辨率 / 码率 / 起播 / 失败原因. ⭐ = 总表所用最佳线路.\n")
    by_source = {}
    for source, tier, cname, st, fname in all_channel_stats:
        by_source.setdefault(source, {"tier": tier, "chs": []})["chs"].append((cname, st, fname))
    for s, a in sorted(usable, key=lambda x: (x[1]["adClean"] if x[1]["adClean"] >= 0 else 9,
                                              -x[1]["okNum"], x[1]["tier"])):
        info = by_source.get(s)
        if not info:
            continue
        adtxt = AD_SHORT[a["adClean"]] + ("*" if a["adWorst"] > a["adClean"] else "")
        md.append(f"\n### [{s}](sources/{src_pages[s]}) · {a['tier']} · 成功率 {a['okNum']}/{a['attempted']} · 广告 {adtxt}\n")
        md.append("| 线路 | 广告 | 分辨率 | 码率 | 起播 | " + " | ".join(subj_short) + " | 失败原因 |")
        md.append("|---|---|---|---|---|" + "---|" * len(subj_short) + "---|")
        for cname, st, fname in sorted(info["chs"], key=lambda x: (-x[1]["ok_n"],
                                       x[1]["ad_rank"] if x[1]["ad_rank"] >= 0 else 9, str(x[0]))):
            star = "⭐ " if a["best"] and cname == a["best"]["channel"] else ""
            reason = st["top_reason"] if (st["ok_n"] < n and st["top_reason"]) else ""
            cells = " | ".join(per_subject_cells(st, sids))
            md.append(f"| {star}[{disp_ch(cname)}](channels/{fname}) | {AD_SHORT[st['ad_rank']]} | "
                      f"{res_s(st['res'])} | {br_s(st['br'])} | {ttp_s(st['ttp'])} | {cells} | {reason} |")

    # 全失败源: 逐个分析失败阶段与原因
    dead_sources = sorted(((s, a["tier"]) for s, a in agg.items() if a["okNum"] == 0),
                          key=lambda x: (x[1], x[0]))
    analyzed = [(s, tier, *dead_reason(s, tier, subjects)) for s, tier in dead_sources]
    search_fail = [x for x in analyzed if x[2] == "搜索"]
    other_fail = [x for x in analyzed if x[2] != "搜索"]

    md.append(f"\n## 🔒 搜索阶段失败 ({len(search_fail)}) — 站点无法访问 / 验证码\n")
    md.append("搜索请求(searchSubjects)就失败, 后续步骤无从谈起. 多为站点下线 / 换域名 / 上了人机验证:\n")
    md.append("| 源 | Tier | 原因 |")
    md.append("|---|---|---|")
    for s, tier, stage, reason in sorted(search_fail, key=lambda x: (x[3], x[1], x[0])):
        md.append(f"| {s} | {tier} | {reason} |")

    md.append(f"\n## ❌ 解析 / 匹配阶段失败 ({len(other_fail)})\n")
    md.append("能搜到页面, 但后续某步失败. 多为站点结构变更导致配置 selector 失配:\n")
    md.append("| 源 | Tier | 失败阶段 | 原因 |")
    md.append("|---|---|---|---|")
    for s, tier, stage, reason in sorted(other_fail, key=lambda x: (x[2], x[1], x[0])):
        md.append(f"| {s} | {tier} | {stage} | {reason} |")

    (BASE / "README.md").write_text("\n".join(md), encoding="utf-8")
    print(f"生成: {len(src_pages)} 源页, {len(list(CH_DIR.glob('*.md')))} 线路报告, "
          f"{len(recs)} 条推荐线路, README.md")


if __name__ == "__main__":
    main()
