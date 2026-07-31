#!/usr/bin/env python3
"""将 datasource-eval 的线路能力 Tier 写入 web selector 的 arguments.channelTiers。"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


if not sys.flags.utf8_mode or os.environ.get("PYTHONHASHSEED") != "0":
    runtime_env = os.environ.copy()
    runtime_env["PYTHONUTF8"] = "1"
    runtime_env["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", *sys.argv],
        env=runtime_env,
        check=False,
    )
    raise SystemExit(completed.returncode)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
GEN_REPORT = SCRIPT_DIR / "gen_report.py"
DEFAULT_SOURCES_DIR = REPO_ROOT / "subs" / "web"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "按 datasource-eval 的 T0–T6 线路能力分级，更新 web selector "
            "配置中的 arguments.channelTiers。"
        )
    )
    parser.add_argument("report_dir", type=Path, help="评测报告目录")
    parser.add_argument(
        "--sources-dir",
        type=Path,
        default=DEFAULT_SOURCES_DIR,
        help=f"待同步的独立 web selector JSON 目录（默认: {DEFAULT_SOURCES_DIR}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 channelTiers 是否最新，不写文件",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def config_signature(config: dict, *, ignore_source_tier: bool) -> str:
    """匹配评测冻结配置；待生成的 channelTiers 不参与签名。"""
    normalized = copy.deepcopy(config)
    arguments = normalized.get("arguments")
    if isinstance(arguments, dict):
        arguments.pop("channelTiers", None)
        if ignore_source_tier:
            arguments.pop("tier", None)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_gen_report(report_dir: Path):
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(GEN_REPORT), str(report_dir)]
        spec = importlib.util.spec_from_file_location(
            "datasource_eval_gen_report_for_channel_tiers",
            GEN_REPORT,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 {GEN_REPORT}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.argv = old_argv


def build_manifest_indexes(
    report_dir: Path,
    manifest: dict,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    strict: dict[str, list[dict]] = {}
    without_source_tier: dict[str, list[dict]] = {}
    for row in manifest["sources"]:
        frozen_path = report_dir / row["configPath"]
        config = read_json(frozen_path)
        strict.setdefault(
            config_signature(config, ignore_source_tier=False),
            [],
        ).append(row)
        without_source_tier.setdefault(
            config_signature(config, ignore_source_tier=True),
            [],
        ).append(row)
    return strict, without_source_tier


def desired_channel_tiers(gen, source: str, aggregate: dict) -> tuple[dict[str, int], list[dict]]:
    tiers: dict[str, int] = {}
    empty_channels: list[dict] = []
    for channel, raw in aggregate["channels"].items():
        if not raw["appear"] and not raw["res"] and not raw["fails"]:
            continue
        stats = gen.channel_stats(source, channel, raw, aggregate["attempted"])
        tier = int(gen.capability_tier(stats)[1:])
        if channel is None or channel == "":
            empty_channels.append({"source": source, "tier": tier})
            continue
        tiers[str(channel)] = tier
    return (
        dict(sorted(tiers.items(), key=lambda item: (item[1], item[0]))),
        empty_channels,
    )


def main() -> None:
    args = parse_args()
    report_dir = args.report_dir.resolve()
    sources_dir = args.sources_dir.resolve()
    manifest_path = report_dir / "sources_manifest.json"

    if not manifest_path.is_file():
        raise SystemExit(f"缺少评测源清单: {manifest_path}")
    if not sources_dir.is_dir():
        raise SystemExit(f"数据源目录不存在: {sources_dir}")

    manifest = read_json(manifest_path)
    strict_index, without_source_tier_index = build_manifest_indexes(report_dir, manifest)
    gen = load_gen_report(report_dir)
    aggregates = gen.aggregate(gen.load_subjects(), gen.load_deep())

    changed: list[str] = []
    checked: list[str] = []
    empty_channels: list[dict] = []
    errors: list[str] = []
    channel_count = 0

    for path in sorted(sources_dir.rglob("*.json")):
        config = read_json(path)
        if config.get("factoryId") != "web-selector":
            errors.append(f"{path}: factoryId 不是 web-selector")
            continue

        matches = strict_index.get(
            config_signature(config, ignore_source_tier=False),
            [],
        )
        if len(matches) != 1:
            matches = without_source_tier_index.get(
                config_signature(config, ignore_source_tier=True),
                [],
            )
        if len(matches) != 1:
            errors.append(
                f"{path}: 冻结配置匹配数应为 1，实际 {len(matches)}；"
                "请确认配置来自本报告且除 tier/channelTiers 外未改动"
            )
            continue

        source = matches[0]["evalName"]
        aggregate = aggregates.get(source)
        if aggregate is None:
            errors.append(f"{path}: 报告中缺少聚合数据 {source}")
            continue

        desired, skipped = desired_channel_tiers(gen, source, aggregate)
        empty_channels.extend(skipped)
        channel_count += len(desired)
        checked.append(str(path.relative_to(REPO_ROOT)))

        arguments = config.get("arguments")
        if not isinstance(arguments, dict):
            errors.append(f"{path}: arguments 不是对象")
            continue
        if arguments.get("channelTiers") == desired:
            continue

        changed.append(str(path.relative_to(REPO_ROOT)))
        if not args.check:
            arguments["channelTiers"] = desired
            path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

    summary = {
        "mode": "check" if args.check else "write",
        "sourcesChecked": len(checked),
        "channelsWritten": channel_count,
        "changedSources": len(changed),
        "emptyChannelsSkipped": empty_channels,
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if errors or (args.check and changed):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
