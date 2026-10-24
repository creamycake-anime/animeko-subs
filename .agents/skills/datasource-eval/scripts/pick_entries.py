#!/usr/bin/env python3
"""选条目: 评测只解析「正确的站内条目」, 不吃引擎模糊匹配 (包含 / 80% 相似) 的顺序.

用法:
  python3 pick_entries.py <report_dir> [只跑指定源名...]     列出每个 源×番 的搜索结果, 能精确对上名字的自动选定, 其余待审核
  python3 pick_entries.py <report_dir> --status                 打印选条目状态表 (待审核项附候选列表)
  python3 pick_entries.py <report_dir> --set <源> <subjectId> <条目 URL 或 partialUrl>[,<URL2>...] [--note 说明]
                                                                 同一部番在站内有多个正确条目 (如「X」与「X 第一季」) 时用逗号列出
  python3 pick_entries.py <report_dir> --unlisted <源> <subjectId> [--note 说明]   该站未收录这部番
  python3 pick_entries.py <report_dir> --check                  有待审核项则退出码 1 (run_eval 开跑前校验)

产物: <report_dir>/entries/raw/<tier>-<源>.json (每番 listSubjectsOnly 的完整返回),
      <report_dir>/entries/candidates.json, <report_dir>/entries/picks.json.

picks.json 每条记录的 status:
  auto          条目名归一化后与 Ani 条目名之一精确相等 (全部这样的条目都选上)
  agent         人/agent 用 --set 选定
  unlisted      人/agent 用 --unlisted 判定该站未收录 (run_eval 不解析, 报告记 ⊘)
  search_failed 搜索阶段就失败 (验证码/域名失效), run_eval 照常跑以记录失败原因
  needs_review  待审核: 没有精确相等的条目
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import (CANDIDATES_REL, PICKS_REL, Mcp, collect_sources, load_meta, normalize_title, pick_ready,
                 require_mcp_bin, resolve_sources_root)


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def auto_pick(subject_names, candidates):
    """有精确相等 (归一化后) 的条目 → auto (全部选上); 否则 needs_review."""
    targets = {normalize_title(n) for n in subject_names if n}
    exact = []
    seen = set()
    for c in candidates:
        if normalize_title(c["name"]) in targets and c["fullUrl"] not in seen:
            seen.add(c["fullUrl"])
            exact.append(c)
    if exact:
        return {"status": "auto", "name": " / ".join(c["name"] for c in exact),
                "url": exact[0]["fullUrl"], "urls": [c["fullUrl"] for c in exact],
                "note": "条目名与 Ani 名称精确相等 (" + ", ".join(
                    f"搜索词 \"{c['keyword']}\" 第 {c['order'] + 1} 位" for c in exact) + ")"}
    if not candidates:
        return {"status": "needs_review", "name": None, "url": None, "urls": [], "note": "搜索结果为空"}
    return {"status": "needs_review", "name": None, "url": None, "urls": [], "note": "没有精确相等的条目"}


def discover(report_dir, only, refresh):
    meta = load_meta(report_dir)
    sources_root = resolve_sources_root(report_dir, meta)
    mcp_bin = require_mcp_bin(meta)
    cands = load_json(report_dir / CANDIDATES_REL, {"sources": {}})
    picks = load_json(report_dir / PICKS_REL, {
        "_note": "评测使用的站内条目. status: auto/agent/unlisted/search_failed/needs_review; 见 pick_entries.py 文档.",
        "sources": {}})
    raw_dir = report_dir / "entries" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources = collect_sources(sources_root, set(only))
    server = Mcp(mcp_bin, lambda m: print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True))
    server.start()
    for tier, f in sources:
        src = f.stem
        config = json.loads(f.read_text(encoding="utf-8"))
        raw_path = raw_dir / f"{tier}-{src}.json"
        raw = load_json(raw_path, {"source": src, "tier": tier, "subjects": {}})
        for sub in meta["subjects"]:
            sid = str(sub["subjectId"])
            if not refresh and sid in cands["sources"].get(src, {}):
                continue
            print(f"[{tier}] {src} × {sub['name']} ...", flush=True)
            r = server.call("selector_resolve_episode", {
                "subjectId": sub["subjectId"], "episodeId": sub["episodeId"], "config": config,
                "listSubjectsOnly": True, "extractVideo": False, "probeVideo": False,
            }, 10 * 60)
            raw["subjects"][sid] = r
            raw["testedAt"] = datetime.now(timezone.utc).isoformat()
            save_json(raw_path, raw)
            steps = r.get("steps") or []
            names = next((s.get("details", {}).get("subjectNames") for s in steps if s.get("name") == "aniMetadata"), None) or [sub["name"]]
            search_failed = any(s.get("name") == "searchSubjects" and s.get("status") == "failed" for s in steps) \
                and not any(s.get("name") == "searchSubjects" and s.get("status") == "success" for s in steps)
            candidates = r.get("subjects") or []
            entry = {"subjectName": sub["name"], "subjectNames": names, "candidates": candidates,
                     "summary": r.get("summary") or r.get("_error"), "searchFailed": search_failed}
            cands["sources"].setdefault(src, {})[sid] = entry
            existing = picks["sources"].setdefault(src, {}).get(sid)
            if existing and existing.get("status") in ("agent", "unlisted"):
                pass  # 人工结论不被自动覆盖
            elif search_failed:
                picks["sources"][src][sid] = {"status": "search_failed", "name": None, "url": None, "urls": [],
                                              "note": (r.get("summary") or "")[:80]}
            else:
                picks["sources"][src][sid] = auto_pick(names, candidates)
            print(f"   {len(candidates)} 个条目 → {picks['sources'][src][sid]['status']}: "
                  f"{picks['sources'][src][sid].get('name') or picks['sources'][src][sid].get('note')}", flush=True)
            save_json(report_dir / CANDIDATES_REL, cands)
            save_json(report_dir / PICKS_REL, picks)
    server.stop()
    print_status(report_dir)


def print_status(report_dir):
    cands = load_json(report_dir / CANDIDATES_REL, {"sources": {}})
    picks = load_json(report_dir / PICKS_REL, {"sources": {}})
    meta = load_meta(report_dir)
    name_of = {str(s["subjectId"]): s["name"] for s in meta["subjects"]}
    pending = 0
    sources = {f.stem for _, f in collect_sources(resolve_sources_root(report_dir, meta))}
    for src in sorted(sources | picks["sources"].keys()):
        print(f"\n== {src}")
        for sid in [str(s["subjectId"]) for s in meta["subjects"]]:
            rec = picks["sources"].get(src, {}).get(sid)
            if rec is None:
                pending += 1
                print(f"  {sid} {name_of.get(sid, ''):14} (未跑)")
                continue
            tag = rec["status"]
            print(f"  {sid} {name_of.get(sid, ''):14} {tag:13} {rec.get('name') or ''} {rec.get('url') or ''}  {rec.get('note') or ''}")
            if not pick_ready(rec):
                pending += 1
                c = cands["sources"].get(src, {}).get(sid, {})
                print(f"      Ani 名称: {c.get('subjectNames')}")
                for cand in c.get("candidates", []):
                    print(f"      - [{cand['keyword']}#{cand['order'] + 1}] {cand['name']}  {cand['fullUrl']}")
    print(f"\n待审核: {pending} 项" + ("  → 用 --set / --unlisted 处理后再跑 run_eval" if pending else ""))
    return pending


def set_pick(report_dir, source, sid, url, note, unlisted):
    picks = load_json(report_dir / PICKS_REL, None)
    if picks is None:
        sys.exit("还没有 picks.json, 先运行发现模式: pick_entries.py <report_dir>")
    cands = load_json(report_dir / CANDIDATES_REL, {"sources": {}})
    entry = cands["sources"].get(source, {}).get(str(sid))
    if entry is None:
        sys.exit(f"candidates.json 里没有 {source} × {sid}, 先跑发现模式")
    if unlisted:
        rec = {"status": "unlisted", "name": None, "url": None, "urls": [], "note": note or "该站未收录"}
    else:
        matches = []
        for u in [x.strip() for x in url.split(",") if x.strip()]:
            match = next((c for c in entry["candidates"] if u in (c["fullUrl"], c["partialUrl"])), None)
            if match is None:
                sys.exit(f"{u} 不在 {source} × {sid} 的搜索结果里. 候选:\n  " +
                         "\n  ".join(f"{c['name']}  {c['fullUrl']}" for c in entry["candidates"]))
            if match["fullUrl"] not in [m["fullUrl"] for m in matches]:
                matches.append(match)
        if not matches:
            sys.exit("至少指定一个候选条目的 URL")
        rec = {"status": "agent", "name": " / ".join(m["name"] for m in matches), "url": matches[0]["fullUrl"],
               "urls": [m["fullUrl"] for m in matches], "note": note or ""}
    picks["sources"].setdefault(source, {})[str(sid)] = rec
    save_json(report_dir / PICKS_REL, picks)
    print(f"{source} × {sid} → {rec['status']}: {rec.get('name') or rec['note']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report_dir", type=pathlib.Path)
    ap.add_argument("only", nargs="*", help="只跑指定源名 (发现模式)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="重新搜索并覆盖自动结论 (保留 agent/unlisted 结论)")
    ap.add_argument("--set", nargs=3, metavar=("SOURCE", "SUBJECT_ID", "URL"))
    ap.add_argument("--unlisted", nargs=2, metavar=("SOURCE", "SUBJECT_ID"))
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    rd = a.report_dir.resolve()
    if a.set:
        return set_pick(rd, a.set[0], a.set[1], a.set[2], a.note, unlisted=False)
    if a.unlisted:
        return set_pick(rd, a.unlisted[0], a.unlisted[1], None, a.note, unlisted=True)
    if a.status:
        return print_status(rd)
    if a.check:
        sys.exit(1 if print_status(rd) else 0)
    discover(rd, a.only, a.refresh)


if __name__ == "__main__":
    main()
