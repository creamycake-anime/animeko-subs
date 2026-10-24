#!/usr/bin/env python3
"""批量评测: 对 subs/web 下全部 CSS selector 数据源, 跨 meta.json 指定的番剧,
跑全流程解析 (all_channels) + **每条 resolved 线路**全量 mpv 实播 (采集可播性/起播)
+ ffprobe 实测分辨率/码率 (bitrateSource 标注来源) + HLS 过滤器结论 (adFilter).

只解析 entries/picks.json 里选定的站内条目 (先跑 pick_entries.py, 由 agent 选定正确条目):
引擎自动匹配是「包含 / 80% 相似」, 会把续季 / 剧场版 / 总集篇当成候选, 评测不吃这个顺序.
选条目结论为 unlisted (该站未收录) 的 源×番 不发请求, summary 记 unlisted, 报告显示 ⊘.

用法: python3 run_eval.py <report_dir> [只跑指定源名...]
读 <report_dir>/meta.json: {evalDate, subjects:[{subjectId, episodeId, name}], mcpBin?}
产物: <report_dir>/subjects/<subjectId>-<name>/{sources/*.json, frames/, summary.json, driver.log}

断点续跑安全: 已有 summary 行的源会跳过.
"""
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import (Mcp, collect_sources, ffprobe_all, load_meta, pick_bitrate_resolution, pick_for,
                 playback_ok, repo_root, require_mcp_bin, require_picks, require_player_available,
                 resolve_sources_root, safe_dir, startup_millis)

MAX_CANDIDATES = 100  # 实际上不设限: 每条线路都要解析并实播


def stage_timings(resolve):
    agg = {}
    for s in resolve.get("steps", []):
        if s.get("durationMillis") is not None:
            agg[s["name"]] = agg.get(s["name"], 0) + s["durationMillis"]
    return agg


def main():
    report_dir = pathlib.Path(sys.argv[1]).resolve()
    only = set(sys.argv[2:])
    meta = load_meta(report_dir)
    mcp_bin = require_mcp_bin(meta)
    subjects = meta["subjects"]
    sources_root = resolve_sources_root(report_dir, meta)
    all_sources = collect_sources(sources_root, only)
    picks = require_picks(report_dir, [f.stem for _, f in all_sources], [s["subjectId"] for s in subjects])

    for sub in subjects:
        sid, ep, name = sub["subjectId"], sub["episodeId"], sub["name"]
        rd = report_dir / "subjects" / f"{sid}-{name}"
        rd.joinpath("sources").mkdir(parents=True, exist_ok=True)
        log_f = open(rd / "driver.log", "a", buffering=1, encoding="utf-8")

        def log(m):
            line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}"
            print(line, flush=True)
            log_f.write(line + "\n")

        server = Mcp(mcp_bin, log)
        server.start()
        summary_path = rd / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else []
        done = {r["source"] for r in summary}

        sources = collect_sources(sources_root, only)
        log(f"=== [{name}] {len(sources)} 源, 已完成 {len(done)}")
        for tier, f in sources:
            src = f.stem
            if src in done:
                continue
            config = json.loads(f.read_text(encoding="utf-8"))
            pick = pick_for(picks, src, sid)
            entry = {"status": pick["status"], "name": pick.get("name"), "url": pick.get("url"),
                     "urls": pick.get("urls") or ([pick["url"]] if pick.get("url") else []),
                     "note": pick.get("note")}
            record = {"source": src, "tier": tier, "file": str(f.relative_to(repo_root())),
                      "testedAt": datetime.now(timezone.utc).isoformat(),
                      "subjectId": sid, "episodeId": ep, "entry": entry}
            if pick["status"] == "unlisted":
                # 该站未收录这部番: 不发请求, 不算失败也不算成功 (报告显示 ⊘)
                log(f"[{tier}] {src} ⊘ 未收录 ({pick.get('note') or ''})")
                (rd / "sources" / f"{tier}-{src}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                summary.append({"source": src, "tier": tier, "unlisted": True, "entry": entry,
                                "configValid": None, "resolveOk": None, "resolveSummary": "站点未收录",
                                "mediasFound": 0, "channelsResolved": [], "channelsFailed": [],
                                "perChannel": []})
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                done.add(src)
                continue
            log(f"[{tier}] {src} ... 条目: {pick.get('name') or '(自动匹配, 搜索曾失败)'}")
            record["validate"] = server.call("validate_selector_config", {"config": config}, 120)
            resolve_args = {
                "subjectId": sid, "episodeId": ep, "config": config,
                # selector 只负责保留全部最终 URL；内部 HTTP probe 冗余且不作为可播依据。
                # 每条 resolved URL 在下方统一交给 probe_video 做 Animeko mpv 实播。
                "extractVideo": True, "probeVideo": False, "extractMode": "all_channels",
                "maxCandidatesToExtract": MAX_CANDIDATES, "maxSubjectsPerName": 2,
                "probeTimeoutMillis": 12000,
            }
            if entry["urls"]:
                # 只展开 agent 选定的条目 (search_failed 的源没有条目, 照常跑以记录搜索失败原因)
                resolve_args["subjectUrls"] = entry["urls"]
            resolve = server.call("selector_resolve_episode", resolve_args, 20 * 60)
            record["resolve"] = resolve
            log(f"  resolve ok={resolve.get('ok')} | {str(resolve.get('summary') or resolve.get('_error'))[:90]}")

            media_by_id = {m["mediaId"]: m for m in resolve.get("medias", [])}
            probes = []
            resolved = [r for r in resolve.get("extractResults", []) if r.get("resolvedVideo")]
            for idx, r in enumerate(resolved):  # 全部线路都实播, 不截断
                media = media_by_id.get(r["candidate"]["mediaId"], {})
                ch = safe_dir(media.get("channel") or f"ch{idx}")
                fdir = report_dir / "subjects" / f"{sid}-{name}" / "frames" / f"{tier}-{src}" / ch
                rv = r["resolvedVideo"]
                probe = server.call("probe_video", {
                    "videoUrl": rv["url"], "headers": rv.get("headers") or {},
                    "showWindow": False, "playSeconds": 4, "playTimeoutMillis": 15000,
                    "probeTimeoutMillis": 12000,
                    "detectAds": True, "captureFramesDir": str(fdir),
                }, 4 * 60)
                require_player_available(probe)  # mpv 没加载直接中止, 防止 HTTP 可达充当可播
                # 真实播出来的线路再用 ffprobe 实测基础指标 (分辨率/编码/码率); 只测可播的, 控制耗时
                fprobe = ffprobe_all(rv["url"], rv.get("headers") or {}, sample_seconds=10) \
                    if playback_ok(probe) else None
                probes.append({"mediaId": r["candidate"]["mediaId"], "channel": media.get("channel"),
                               "videoUrl": rv["url"], "probe": probe, "ffprobe": fprobe})
            record["playerProbes"] = probes
            (rd / "sources" / f"{tier}-{src}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

            # 汇总行
            er = resolve.get("extractResults", [])
            ok_ch = [media_by_id.get(r["candidate"]["mediaId"], {}).get("channel") or r["candidate"]["mediaId"]
                     for r in er if r.get("resolvedVideo")]
            fail_ch = [media_by_id.get(r["candidate"]["mediaId"], {}).get("channel") or r["candidate"]["mediaId"]
                       for r in er if not r.get("resolvedVideo")]
            per_channel = []
            for p in probes:
                ma = p["probe"].get("mediaAnalysis") or {}
                v = ma.get("video") or {}
                pb = ma.get("playback") or {}
                ad = p["probe"].get("adAnalysis") or {}
                br, br_src, res = pick_bitrate_resolution(p.get("ffprobe"), ma)
                per_channel.append({
                    # 可播只认真实实播 (playback.ran && ok), 不是 probe.ok (后者含 HTTP 兜底)
                    "channel": p["channel"], "playerOk": playback_ok(p["probe"]),
                    "resolution": res,
                    "bitrate": br, "bitrateSource": br_src,
                    "codec": v.get("codec") or ((p.get("ffprobe") or {}).get("streams") or {}).get("vcodec"),
                    "adSuspicion": ad.get("suspicion"),
                    # 真实客户端 HLS 广告过滤器结论 (filterable/removedGroups), 供报告与判定用
                    "adFilter": ad.get("hlsFilter"),
                    # 起播 = 打开媒体到画面首次前进 (open + resume→首帧); timeToPlayingMillis 单独看恒为几毫秒
                    "startupMillis": startup_millis(pb),
                    "openMillis": pb.get("openMillis"),
                    "timeToPlayingMillis": pb.get("timeToPlayingMillis"),
                })
            # 行级快照 (首条可播线路), 仅供人肉翻 summary.json; 报告总表的"最佳线路"由 gen_report 按
            # 无广告>分辨率>码率>起播 从 perChannel 重新算, 不用这几个字段.
            best = next((p for p in probes if playback_ok(p["probe"])), probes[0] if probes else None)
            bma = ((best or {}).get("probe", {}) or {}).get("mediaAnalysis") or {}
            bpb = bma.get("playback") or {}
            bbr, bbr_src, bres = pick_bitrate_resolution((best or {}).get("ffprobe"), bma)
            summary.append({
                "source": src, "tier": tier, "entry": entry,
                "configValid": (record["validate"] or {}).get("ok"),
                "resolveOk": resolve.get("ok"),
                "resolveSummary": resolve.get("summary") or resolve.get("_error"),
                "mediasFound": len(resolve.get("medias", [])),
                "channelsResolved": ok_ch, "channelsFailed": fail_ch,
                "stageTimings": stage_timings(resolve),
                "totalResolveMillis": resolve.get("totalDurationMillis"),
                "resolution": bres,
                "bitrate": bbr, "bitrateSource": bbr_src,
                "startupMillis": startup_millis(bpb),
                "timeToPlayingMillis": bpb.get("timeToPlayingMillis"),
                "perChannel": per_channel,
            })
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            done.add(src)
        server.stop()
        log(f"=== [{name}] 完成: {len(summary)} 源")


if __name__ == "__main__":
    main()
