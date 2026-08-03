#!/usr/bin/env python3
"""深度采样: 对可用源 (跨番至少成功 1 次), 按 **每部番 × 每条线路** 全覆盖重新解析,
先 detect_hls_ads 做 HLS 结构预筛 (真实客户端过滤器; 疑似插入广告段中点自动加为截图点),
再用 mpv 长播 (基础 28s, 有加采点时延长至最多 60s) + 多点截图 (0/3/8/15/25s + 加采点),
外加 ffprobe 实测基础指标, 供 subagent 逐张看图判广告 (每条线路都必须有图可判).

用法:
  python3 deep_sample.py <report_dir>
  python3 deep_sample.py <report_dir> --backfill-quick

第二种模式会找出快测曾真实播放成功、但深采二次解析未复现截图的番剧/线路，直接复用快测
保存的视频 URL 补跑 28s 完整多点截图；URL 已过期/截不到帧则跳过，该线路保持"未判定"
(快测只有开头 ~4s 两帧，判不出轻/中/重，不拿它充当视觉证据)。
读 <report_dir>/subjects/*/summary.json 找可用源, meta.json 拿番剧/episodeId.
产物: <report_dir>/deep/<tier>-<源>/<番>/<线路>/frames/*.png + deep/<tier>-<源>/deep.json
      (deep.json 每线路含 adDetect 结构探测结果与 ffprobe 实测指标)
"""
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import (Mcp, ffprobe_all, is_explicit_direct_media, load_meta, playback_ok,
                 require_mcp_bin, require_player_available, resolve_sources_root, safe_dir)

MAX_CANDIDATES = 100          # 实际上不设限: 每条线路都要深采
SAMPLE_SECONDS = [0, 3, 8, 15, 25]


def find_config(root, name):
    for f in root.glob(f"*/{name}.json"):
        return f
    return None


def usable_sources(report_dir, subject_ids):
    per = {}
    for d in sorted((report_dir / "subjects").glob("*")):
        sj = d / "summary.json"
        if not sj.exists():
            continue
        sid = d.name.split("-", 1)[0]
        for r in json.loads(sj.read_text()):
            if r.get("resolveOk"):
                per.setdefault(r["source"], {"tier": r["tier"], "sids": set()})["sids"].add(sid)
    out = []
    for name, v in per.items():
        sids = [s for s in subject_ids if s in v["sids"]]  # 按 meta 顺序
        out.append((name, v["tier"], sids))
    out.sort(key=lambda x: (x[1], x[0]))
    return out


def _channel_key(value):
    return "None" if value is None else str(value)


def ad_capture_plan(ad_detect):
    """由 detect_hls_ads 结果算截图计划: 疑似插入广告段的中点 (≤55s) 加为截图点,
    并相应延长播放时长 (基础 28s, 上限 60s). 返回 (groups, extras, capture_at, play_seconds)."""
    groups = (((ad_detect.get("analysis") or {}).get("hlsFilter") or {})
              .get("removedGroups")) or []
    extras = sorted({
        int((g["startOffsetSeconds"] + g["endOffsetSeconds"]) / 2)
        for g in groups
        if g.get("startOffsetSeconds") is not None
        and g.get("endOffsetSeconds") is not None
    } - set(SAMPLE_SECONDS))
    extras = [e for e in extras if 0 <= e <= 55]
    capture_at = sorted(set(SAMPLE_SECONDS) | set(extras))
    play_seconds = min(60, max(28, max(capture_at) + 3))
    return groups, extras, capture_at, play_seconds


def detect_hls_ads(server, url, headers):
    """只对可能是 HLS 的地址调用结构探测。

    MCP 的 ``detect_hls_ads`` 会把无扩展名地址也按 HLS 尝试，这对伪装/无后缀的
    m3u8 很重要；但对明确以 MP4 等直链后缀结尾的地址没有结构广告可分析，强制
    下载并按文本解析既无意义，也可能一直读取大文件直到超时。此处只短路这些
    明确的非 HLS 直链，其余地址仍交给 MCP 判定。
    """
    if not is_explicit_direct_media(url):
        return server.call("detect_hls_ads", {"url": url, "headers": headers}, 120)
    suffix = pathlib.PurePosixPath(url.split("?", 1)[0]).suffix.lower()
    reason = f"明确的非 HLS 直链 ({suffix}); 跳过 HLS 结构广告探测，广告以多点截图判定"
    return {
        "ok": False,
        "url": url,
        "summary": reason,
        "analysis": {"suspicion": "unknown", "reasons": [reason]},
        "errors": [],
        "skippedNonHls": True,
    }


def backfill_quick_success(report_dir, deep_dir, server, log):
    """用快测成功记录补齐深采漏掉的 (番剧, 源, 线路) 截图。

    selector 每次搜索结果可能变化，正常深采重新解析时可能无法复现快测中的线路。快测 source
    trace 保存了当时已实际播放成功的视频 URL 和 headers；直接对该 URL 重跑 28s 完整多点
    截图 (省掉二次解析)。URL 失效/截不到帧则跳过——快测只有开头 ~4s 两帧, 判不出轻/中/重,
    帧标签也不是 frame_XXs (看图 agent 与报告画廊都消费不了), 不拿它充当视觉证据,
    该线路保持"未判定"。
    """
    attempted = added = long_ok = 0
    for subject_dir in sorted((report_dir / "subjects").glob("*")):
        if not subject_dir.is_dir() or "-" not in subject_dir.name:
            continue
        sid, subject_name = subject_dir.name.split("-", 1)
        summary_file = subject_dir / "summary.json"
        if not summary_file.exists():
            continue
        for row in json.loads(summary_file.read_text()):
            source, tier = row["source"], row["tier"]
            trace_file = subject_dir / "sources" / f"{tier}-{source}.json"
            if not trace_file.exists():
                continue
            trace = json.loads(trace_file.read_text())
            quick_probes = [p for p in (trace.get("playerProbes") or [])
                            if playback_ok(p.get("probe"))]
            if not quick_probes:
                continue

            src_dir = deep_dir / f"{tier}-{source}"
            deep_file = src_dir / "deep.json"
            if deep_file.exists():
                record = json.loads(deep_file.read_text())
            else:
                record = {"source": source, "tier": tier,
                          "testedAt": datetime.now(timezone.utc).isoformat(), "runs": []}
            run = next((r for r in record["runs"] if str(r.get("subjectId")) == sid), None)
            if run is None:
                run = {"subjectId": sid, "subjectName": subject_name, "channels": []}
                record["runs"].append(run)

            resolved_by_media = {
                (r.get("candidate") or {}).get("mediaId"): r.get("resolvedVideo") or {}
                for r in (trace.get("resolve") or {}).get("extractResults", [])
            }
            seen_quick_channels = set()
            for quick in quick_probes:
                channel = quick.get("channel")
                ckey = _channel_key(channel)
                if ckey in seen_quick_channels:
                    continue
                seen_quick_channels.add(ckey)
                # 少量首帧并不足以判断 0/3/8/15/25s 的广告变化；只有至少覆盖
                # 全部基础采样点，才算已有可供视觉复核的完整证据。
                if any(_channel_key(c.get("channel")) == ckey and
                       len((c.get("probe") or {}).get("capturedFrames") or []) >= len(SAMPLE_SECONDS)
                       for c in run.get("channels", [])):
                    continue

                attempted += 1
                media_id = quick.get("mediaId")
                rv = resolved_by_media.get(media_id) or {}
                url = quick.get("videoUrl") or rv.get("url")
                headers = rv.get("headers") or {}
                fdir = src_dir / subject_name / f"{safe_dir(ckey)}__quick-backfill" / "frames"
                log(f"[backfill] {source}/{ckey} · {subject_name}")
                # 与主深采同口径: 结构预筛 + 加采点 + ffprobe 实测
                ad_detect = detect_hls_ads(server, url, headers)
                _groups, extras, capture_at, play_seconds = ad_capture_plan(ad_detect)
                if extras:
                    log(f"  HLS 检出疑似插入片段, 加采 {extras}s, 长播 {play_seconds}s")
                fprobe = ffprobe_all(url, headers, sample_seconds=30)
                probe = server.call("probe_video", {
                    "videoUrl": url, "headers": headers,
                    "showWindow": False, "playSeconds": play_seconds,
                    "playTimeoutMillis": 90000 + 2000 * (play_seconds - 28),
                    "probeTimeoutMillis": 12000, "detectAds": True,
                    "captureFramesDir": str(fdir), "captureAtSeconds": capture_at,
                }, 6 * 60)
                require_player_available(probe)
                new_frames = probe.get("capturedFrames") or []
                if not new_frames:  # URL 过期/播不出: 跳过, 该线路保持"未判定", 不拿快测帧充数
                    log(f"  !! 补跑无帧 (URL 可能已过期), 跳过: {source}/{ckey} · {subject_name}")
                    continue
                long_ok += 1

                run.setdefault("channels", []).append({
                    "channel": channel, "mediaId": media_id, "videoUrl": url,
                    "adDetect": ad_detect, "ffprobe": fprobe,
                    "probe": probe, "backfilledFromQuick": True,
                })
                added += 1
                src_dir.mkdir(parents=True, exist_ok=True)
                deep_file.write_text(json.dumps(record, ensure_ascii=False, indent=2))
                log(f"  + 帧={len(new_frames)} ok={probe.get('ok')}")
    log(f"=== 快测补采完成: 尝试 {attempted}, 写入 {added}, 28s 补采有帧 {long_ok}")


def main():
    report_dir = pathlib.Path(sys.argv[1]).resolve()
    meta = load_meta(report_dir)
    mcp_bin = require_mcp_bin(meta)
    ep_of = {str(s["subjectId"]): (s["episodeId"], s["name"]) for s in meta["subjects"]}
    subject_ids = [str(s["subjectId"]) for s in meta["subjects"]]
    sources_root = resolve_sources_root(report_dir, meta)

    deep_dir = report_dir / "deep"
    deep_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(deep_dir / "deep.log", "a", buffering=1)

    def log(m):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}"
        print(line, flush=True)
        log_f.write(line + "\n")

    server = Mcp(mcp_bin, log)
    server.start()
    if len(sys.argv) > 2 and sys.argv[2] == "--backfill-quick":
        backfill_quick_success(report_dir, deep_dir, server, log)
        server.stop()
        return
    sources = usable_sources(report_dir, subject_ids)
    log(f"=== 深采 {len(sources)} 个可用源")

    for name, tier, sids in sources:
        src_dir = deep_dir / f"{tier}-{name}"
        if (src_dir / "deep.json").exists():
            log(f"[skip] {tier}-{name}")
            continue
        cfg = find_config(sources_root, name)
        if not cfg:
            continue
        config = json.loads(cfg.read_text())
        record = {"source": name, "tier": tier, "testedAt": datetime.now(timezone.utc).isoformat(), "runs": []}
        log(f"[{tier}] {name}: {[ep_of[s][1] for s in sids]}")
        for sid in sids:  # 该源解析成功过的每部番都深采
            ep, sub_name = ep_of[sid]
            resolve = server.call("selector_resolve_episode", {
                "subjectId": int(sid), "episodeId": int(ep), "config": config,
                "extractVideo": True, "probeVideo": False, "extractMode": "all_channels",
                "maxCandidatesToExtract": MAX_CANDIDATES, "maxSubjectsPerName": 2,
                "probeTimeoutMillis": 12000,
            }, 20 * 60)
            media_by_id = {m["mediaId"]: m for m in resolve.get("medias", [])}
            resolved = [r for r in resolve.get("extractResults", []) if r.get("resolvedVideo")]
            log(f"  {sub_name}: {len(resolved)} 线路")
            run = {"subjectId": sid, "subjectName": sub_name, "channels": []}
            for r in resolved:  # 每条线路都长播采样, 不截断
                media = media_by_id.get(r["candidate"]["mediaId"], {})
                ch = safe_dir(media.get("channel") or r["candidate"]["mediaId"])
                rv = r["resolvedVideo"]
                headers = rv.get("headers") or {}
                fdir = src_dir / sub_name / ch / "frames"
                # HLS 结构预筛: 拉 m3u8 跑结构启发式 + Ani 真实客户端广告过滤器 (HlsManifestFilter);
                # 疑似插入广告段的中点 (≤55s) 自动加为截图点, 结果 (adDetect) 供看图判定用
                ad_detect = detect_hls_ads(server, rv["url"], headers)
                groups, extras, capture_at, play_seconds = ad_capture_plan(ad_detect)
                if extras:
                    log(f"    {ch}: HLS 检出疑似插入片段 {len(groups)} 组, 加采 {extras}s, "
                        f"长播 {play_seconds}s")
                # 基础媒体指标: ffprobe 流信息 + ffmpeg 拷 30s 实测均值码率
                fprobe = ffprobe_all(rv["url"], headers, sample_seconds=30)
                probe = server.call("probe_video", {
                    "videoUrl": rv["url"], "headers": headers,
                    "showWindow": False, "playSeconds": play_seconds,
                    "playTimeoutMillis": 90000 + 2000 * (play_seconds - 28),
                    "probeTimeoutMillis": 12000, "detectAds": True,
                    "captureFramesDir": str(fdir), "captureAtSeconds": capture_at,
                }, 6 * 60)
                require_player_available(probe)  # mpv 没加载直接中止, 防止产出无帧假深采
                pb = (probe.get("mediaAnalysis") or {}).get("playback") or {}
                log(f"    {ch}: ok={probe.get('ok')} 帧={len(probe.get('capturedFrames') or [])} "
                    f"起播={pb.get('timeToPlayingMillis')}ms")
                run["channels"].append({"channel": media.get("channel"),
                                        "mediaId": r["candidate"]["mediaId"],
                                        "videoUrl": rv["url"],
                                        "adDetect": ad_detect, "ffprobe": fprobe,
                                        "probe": probe})
            record["runs"].append(run)
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "deep.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    server.stop()
    log("=== 深采完成")


if __name__ == "__main__":
    main()
