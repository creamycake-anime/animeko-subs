---
name: datasource-eval
description: 评测 ani-subs 的 CSS selector 数据源并生成图文报告。跨多部番剧对每个 web selector 源跑完整解析流程 (searchSubjects→…→extractVideo)，用 Animeko 桌面播放器 (mpv) 真实播放每条线路测可播放性/起播耗时，ffprobe/ffmpeg 实测分辨率/码率/编码，detect_hls_ads 结构探测 HLS 插入广告并用 Ani 真实客户端过滤器验证能否自动滤除，多点截图后由 subagent 逐张看图判断博彩广告，生成三层报告，并可把线路 T0–T6 同步到 Animeko v4.9+ 的 arguments.channelTiers。当用户要"评测数据源/测试所有源/评估 selector 源/生成数据源报告/看哪些源能用/查数据源广告/配置线路 tier/channel tier"时使用。
---

# ani-subs 数据源评测

对 `subs/web/t0…t4/` 下的 CSS selector 数据源做端到端评测并生成图文报告。报告落在 `reports/<评测日期>-css-eval/`。

## 依赖: animeko 仓库的 MCP

本 skill 靠 **animeko `ani` 仓库**里的 `datasource-test-mcp` 工具驱动真实解析与播放。它是一个**本地 HTTP MCP server**（Streamable HTTP：每条 JSON-RPC 消息 `POST http://127.0.0.1:<port>/mcp`，无状态、无 SSE；CLI 参数 `--host 127.0.0.1 --port <port>`，默认端口 8264——脚本的 `lib.Mcp` 会自动起进程并用**随机空闲端口**，避免多实例抢端口），提供 `search_subjects` / `get_trends` / `get_subject_episodes` / `validate_selector_config` / `selector_resolve_episode` / `probe_video` / `detect_hls_ads` 等工具。

**定位方式**（脚本 `scripts/lib.py` 自动做，此处说明供排障）:
1. 环境变量 `ANIMEKO_MCP_BIN`（若设置且存在）
2. **sibling 目录** `../ani/tools/datasource-test-mcp/build/install/datasource-test-mcp/bin/datasource-test-mcp`（ani-subs 与 ani 通常同在 `animeko/` 下并列）
3. `~/Projects/animeko/ani/...`

**找不到时**: 需要先构建 MCP，或询问用户 animeko 项目位置。
```bash
# 若已知 animeko/ani 位置, 在 ani 仓库根构建:
cd <animeko>/ani && ./gradlew :tools:datasource-test-mcp:installDist
```
构建产物在 `ani/tools/datasource-test-mcp/build/install/.../bin/datasource-test-mcp`。
`probe_video` 用 Animeko 自家桌面播放器内核 (mediamp-**mpv**) 真实播放，无需手装任何播放器。**mpv 原生库来源**: mediamp 的 runtime 工件尚未发布，默认 classpath 加载会失败——脚本的 `lib.Mcp` 启动 server 时会自动注入 JVM flag `-Dani.mpv.native.dir`（经 `DATASOURCE_TEST_MCP_OPTS` 环境变量，追加不覆盖），目录取环境变量 `ANI_MPV_NATIVE_DIR` 或按平台自动探测 mediamp worktree dev 构建（`~/Projects/mediamp/mediamp-mpv/build/mpv-output/<平台>/lib`，如 `MacosArm64`）；原生库加载失败时 probe_video 退化为纯 HTTP 探测（拿不到分辨率/码率/起播）。起播指标取 `mediaAnalysis.playback.timeToPlayingMillis`（**resume→播放位置首次前进**，即起播含缓冲；mpv 的 PLAYING 状态在 resume 后同步翻转、不含缓冲，故不用状态而用位置前进作判据），另有 openMillis / timeToFirstFrameMillis / playWallClockMillis / bufferingCount / bufferingTotalMillis。**可播口径**: 只认 `playback.ran && playback.ok`（脚本 `lib.playback_ok`）——`probe_video` 顶层 `ok` 在播放器不可用时会退回 HTTP 探测结论，HTTP 可达 ≠ 可播；脚本检测到 `mediaAnalysis.available == false`（mpv 没加载）会**直接中止评测**，防止产出虚假报告。

另需系统装 **ffmpeg/ffprobe**（macOS: `brew install ffmpeg`）——分辨率/码率/编码等基础媒体指标靠它实测，见下"媒体基础指标 (ffprobe)"。

**若在 ani-subs 里找不到 sibling `ani` 目录，先问用户 animeko/ani 仓库在哪，不要瞎猜路径。**

脚本还需要 Python 的 Pillow（截图拼图，非必需）；MCP 与 ffmpeg/ffprobe 是硬依赖。

## 流程

所有脚本在 `.agents/skills/datasource-eval/scripts/` 下，第一个参数都是报告目录。

### 1. 选番剧 + 建报告目录

选 3–5 部**近年完结的热门番**（各源收录率高，能区分"源失效"与"没收录这部番"）。用 MCP 拿 subjectId 和 ep1 的 episodeId：
- `get_trends` 看当前热门，或 `search_subjects` 按名字搜 → 拿 `subjectId`
- `get_subject_episodes` → 拿该番 sort=1 那集的 `episodeId`

建报告目录并写 `meta.json`（**评测日期决定目录名**，用今天的日期）:
```bash
DATE=$(date +%Y%m%d)                      # 目录用 YYYYMMDD
R=reports/${DATE}-css-eval
mkdir -p "$R"
# 写 meta.json (评测日期 + 番剧列表), 例:
cat > "$R/meta.json" <<'JSON'
{
  "evalDate": "2026-07-05",
  "subjects": [
    {"subjectId": 329906, "episodeId": 1088220, "name": "间谍过家家"},
    {"subjectId": 400602, "episodeId": 1227087, "name": "葬送的芙莉莲"}
  ]
}
JSON
```
`evalDate` 用 `YYYY-MM-DD`，会写进报告头。名字里别带 `/`（会当路径分隔）。

### 2. 批量解析 + 实播 (慢, 后台跑)

对全部 selector 源 × 每部番，跑全流程解析 + **每条 resolved 线路**全量 mpv 实播（不设条数上限，采集可播性/起播）+ ffprobe 实测分辨率/码率（summary 每线路记 `bitrateSource`: `ffprobe_measured`/`ffprobe_format`/`player`）+ HLS 过滤器结论（`adFilter`，即 probe `adAnalysis.hlsFilter`，见第 4 步）+ 单帧广告启发式（仅参考，报告不采信）。断点续跑安全。

`run_eval.py` 与 `deep_sample.py` 调用 `selector_resolve_episode` 时必须传
`probeVideo=false`：selector 阶段只负责完整保留 WebView 解析出的最终 URL，不运行其内部的
HTTP 可达性 probe。随后每条 resolved URL 统一调用 `probe_video`，以 Animeko mpv 的
`playback.ran && playback.ok` 作为唯一可播结论。不得把内部 HTTP probe 当作实播，也不得让
其超时阻断后续 mpv 测试。

```bash
python3 .agents/skills/datasource-eval/scripts/run_eval.py "$R"
```
产物: `$R/subjects/<id>-<名>/{sources/*.json 逐源 trace, summary.json 汇总, driver.log}`。
耗时随线路数线性增长（每条线路实播 ~4s + ffprobe 采样 + 解析开销），每部番可达十几分钟。建议 `run_in_background` 并用日志盯进度。

Ani API 的搜索接口冷启动慢且会限流；MCP 工具内部已重试，脚本对超时也会重启 server。

#### 媒体基础指标 (ffprobe)

分辨率/码率/编码等**基础媒体指标以 ffprobe/ffmpeg 实测为准**（mpv 实播只负责可播性/起播/卡顿）。`lib.py` 的 `ffprobe_all()` 已封装，run_eval/deep_sample 自动跑；手动排查用:

```bash
# 流信息: 分辨率/编码/帧率/时长 (m3u8 直连)
ffprobe -v error -print_format json -show_format -show_streams \
  -headers $'Referer: https://example.com/\r\nUser-Agent: Mozilla/5.0\r\n' \
  -allowed_extensions ALL "$URL"

# 平均码率实测 (m3u8 直连的 format.bit_rate 常缺/不可靠): 拷 30s 到本地量大小
ffmpeg -y -v error -headers $'Referer: https://example.com/\r\n' \
  -allowed_extensions ALL -t 30 -i "$URL" -c copy -f mpegts /tmp/sample.ts
ffprobe -v error -show_entries format=duration,size,bit_rate -print_format json /tmp/sample.ts
# bit_rate ≈ size*8/duration; 脚本 lib.py 的 ffprobe_all() 就是这套
```

许多盗版源把 HLS 分片伪装成 `.jpeg`/`.png` 扩展名，需 `-allowed_extensions ALL`（ffmpeg 7.1+ 再加 `-extension_picky 0`，脚本会自动探测追加）；这些 HLS demuxer 参数只用于 m3u8、无扩展名等可疑输入，明确的 `.mp4`/`.mkv`/`.webm`/`.flv`/`.avi`/`.mov` 直链不得附加，否则 ffmpeg 会报 `Option extension_picky not found`。ffprobe 仍拒绝的流，该线路以 mpv 实播数据为准。

### 3. 深度采样 (为看图判广告)

对**可用源**（跨番至少成功 1 次），按**每部番 × 每条线路全覆盖**重新解析。每条线路先用 MCP `detect_hls_ads` 做 **HLS 结构预筛**（真实客户端过滤器跑一遍 m3u8；疑似插入广告段的**中点（≤55s）自动加为截图点**，结果存 `adDetect` 供第 4 步判定用）；URL 路径明确以 `.mp4`/`.mkv`/`.webm`/`.flv`/`.avi`/`.mov` 结尾时直接记录 `skippedNonHls: true`，避免把大体积直链当 m3u8 下载至超时，广告改由多点截图判定；无扩展名或其他可疑地址仍交给 MCP。随后 mpv 长播（基础 28s，有加采点时相应延长，上限 60s）、在 0/3/8/15/25s + 加采点各截一帧，并跑 `ffprobe_all` 实测基础指标——每条线路都必须有图可供视觉判定，不许抽样。

```bash
python3 .agents/skills/datasource-eval/scripts/deep_sample.py "$R"
```
产物: `$R/deep/<tier>-<源>/<番>/<线路>/frames/frame_XXs.png` + 每源 `deep.json`（每线路含 `adDetect` 结构探测与 `ffprobe` 实测指标）。

**补采模式**（可选，深采跑完后再跑）:

```bash
python3 .agents/skills/datasource-eval/scripts/deep_sample.py "$R" --backfill-quick
```

找出快测曾真实播放成功、但深采二次解析未复现截图的 (番剧, 线路)，**复用快测保存的视频 URL** 直接补跑与主深采同口径的采样（detect_hls_ads 结构预筛 + 加采点 + 长播多点截图 + ffprobe，省掉二次解析，记录带 `backfilledFromQuick: true` 标记）。补跑失败（URL 过期/截不到帧）则跳过并记日志，该线路保持"未判定"——**不会拿快测的开头两帧充当视觉证据**（那只覆盖 ~4s，判不出轻/中/重）。

### 4. subagent 逐张看图判广告 (关键)

这些盗版源常把**博彩广告**（"XXXX.com 赞助发布 棋牌/真人娱乐/捕鱼/投注"横幅，或整屏前贴片）烧录进视频。OCR 对亮背景水印漏报严重，**必须靠视觉判读**。

**不要自己在主循环里读大量截图**（容易在批量时编造结果、且图会因 stale 被清理）。改为**每条线路派一个 Explore subagent** 读它的几帧、返回结构化判定：

- 枚举 `deep/*/deep.json` 里每个 `(源, 线路)` 的 `frame_00s/08s/15s` 路径（含加采点帧）。**必须覆盖全部 (源, 线路) 组合**——凡实播成功过的线路都要有视觉判定；判定只认 agent 真实读图，`adSuspicion` 等**算法启发式绝不能单独作为判定依据**（gen_report 也不读它）。注意区分两种结构数据: `suspicion` 是启发式**猜测**（不作数）；`adDetect.analysis.hlsFilter` 是**真实客户端过滤器的执行结果**，仅对"滤不滤得掉"有权威性（见下），广告有没有/什么等级仍以看图为准。
- 对每条线路 `Agent(subagent_type="Explore")`，prompt 让它 Read 这几帧，并**附上该线路 `adDetect` 的疑似广告时间段摘要**（若有 `removedGroups`，告诉 agent 重点看落在这些区间的帧），按固定格式回 `AD_LEVEL`（none/low/medium/high）、`POSITION`（top/bottom/fullscreen/none）、`SEEN_AT`（在哪些采样点看到广告，如 `0s,3s`）、`EVIDENCE`（看到的域名或赌博词原文 / "clean anime + subtitles" / 正规台标如 bilibili独播、Muse木棉花）、`PERSISTS`（是否所有采样点都在）。可 6–8 个并行发。
- 汇总口径（按 `SEEN_AT`/`PERSISTS` + `POSITION` 归一）:
  - **轻（low）**: 横幅**仅在片头/片尾**出现，正片中没有。
  - **中（medium）**: **片中也会出现**横幅（时有时无，非全程）。
  - **重（high）**: **水印/横幅一直在**（全程）；或存在**插入的广告视频片段**（整屏广告画面，非正片——前贴片/中插，无论出现在哪里）且 HLS 自动广告过滤滤不掉。评测直连播放（mpv）、未经过 Ani 的 HLS 广告过滤；**滤不滤得掉以 `adDetect.analysis.hlsFilter` 为准**（真实客户端过滤器 HlsManifestFilter 的执行结果）: `filterable==true` 且看图确认的插入广告落在 `removedGroups` 时间范围内 → 客户端会在 App 内自动滤除，该片段**不单独构成重**（按滤除后剩余可见的广告归级）；`filterable==false` 或没有 hlsFilter 数据（非 HLS / 拉取失败）→ **默认按滤不掉处理（重）**。看图判定"有没有广告/什么等级"仍是必须且权威的——烧录进画面的水印/横幅 playlist 分析看不见。
  - **无（none）**: 干净。
  - 采样近似（长播只覆盖开头 28–60s）: 广告只在 0–3s 出现、8s 起消失 ≈ 片头（轻）；8s 及以后仍间歇出现 ≈ 片中（中）；所有采样点都在 ≈ 全程（重）。拿不准时往重的方向判。

把判定写入 `$R/combined/frames_verdicts.json`（**程序化写，不靠记忆**）:
```json
{
  "_note": "subagent 逐张看图判定. ad: none/low/medium/high. 键为 '源' 或 '源/线路'.",
  "_channel_level": {
    "叽哔动漫/叽哔1线": {"ad": "low", "note": "底部博彩横幅 07403.com 赞助发布 棋牌/真人娱乐/捕鱼, 仅片头, 8s 起消失"},
    "去看吧": {"ad": "none", "note": "干净正片, bilibili 独播台标"}
  }
}
```
键可用**源级**（整源统一，如 `"去看吧"`）或**线路级**（`"源/线路"`，同源不同线路差异大时用，如 omofun 高清线路干净但超快线路是博彩前贴片）。生成报告时线路级优先，未判读的线路显示"未判定"，不会拿不可靠的 OCR 启发式充数。

### 5. 生成报告

```bash
python3 .agents/skills/datasource-eval/scripts/gen_report.py "$R"
```
产物（全部在 `$R/`）:
- `README.md` — 总索引: 评测日期 → **推荐线路**（每源最佳的无广告稳定线路）→ 源级汇总表 → **各源线路拆解**（每线路 × 每部番实播 ✅/❌/— + 失败原因）→ **搜索阶段失败** 与 **解析/匹配阶段失败** 两节带原因。
- `sources/<tier>-<源>.md` — 每源线路拆解页。
- `channels/<tier>-<源>-<线路>.md` — 每线路图文页: 广告判定 + 播放采样画廊 + 媒体信息 + 播放性能 + 跨番实播/失败原因。

改了 `frames_verdicts.json` 后重跑 gen_report 即可刷新，无需重测。

### 6. 同步线路 Tier 到订阅配置（Animeko v4.9+）

Animeko [PR #3205](https://github.com/open-ani/animeko/pull/3205) 起支持
`SelectorMediaSourceArguments.channelTiers`。键必须与解析出的 channel 名
（`Media.properties.alliance`）逐字一致，值为数字 tier；channel 配置优先，未列出的
channel 回退到源级 `arguments.tier`。有效 tier 参与跨源排序，tier 0 还参与快速选择。
旧客户端会忽略未知字段，不需要提升 selector codec `version`。

报告和视觉判定定稿后，把本报告的线路能力 T0–T6（JSON 中写为 `0`–`6`）同步到独立源配置：

```bash
python3 .agents/skills/datasource-eval/scripts/sync_channel_tiers.py "$R" \
  --sources-dir subs/web
python3 .agents/skills/datasource-eval/scripts/sync_channel_tiers.py "$R" \
  --sources-dir subs/web --check
```

脚本用 `sources_manifest.json` 的冻结配置精确匹配当前源：先仅忽略待生成的
`channelTiers`；只有给原本未分级的导入源补过源级 tier 时，才在第二阶段忽略源级
`tier`，除此之外配置必须一致。写入所有在报告中出现过的非空 channel：
实测优质线路写 T0–T4，重广告/低质写 T5，从未播成或证据不足写 T6，防止它们回退到
较高的源级优先级。`channel == null`/空字符串无法被 Animeko 的 channel 覆盖查询命中，
必须告警并回退源 tier；不要伪造 `"默认"` 键。

示例：

```json
{
  "tier": 3,
  "channelTiers": {
    "高清在线": 1,
    "线路3": 5,
    "备用": 6
  }
}
```

这里的线路能力 T0–T6 与 `subs/web/t0`–`t4` 目录及源级静态优先级仍是两套概念。

### 可选但强烈推荐: chrome-devtools MCP(浏览器真相源)

如果当前**还**能用一个 **chrome-devtools MCP**(工具形如 `mcp__chrome-devtools__*`,如 `navigate_page` / `take_snapshot` / `list_network_requests` / `get_network_request` / `click` / `evaluate_script` / `list_console_messages`; 具体名称以所连接的 server 为准), 把它与 `datasource-eval` **结合使用**。它不是必需的。

**为什么有用**: `datasource-eval` 引擎是**无 JS 的静态 HTTP 抓取 + 解析**, chrome-devtools 驱动的是**真实浏览器**(执行 JS、能交互)。当某一步在 MCP 工具里跑不通、站点在浏览器里却正常时,浏览器看到的才是"真相",**两者的差异本身就是诊断信号**。

**怎么用**(按需, 不是每步都跑):

- 先根据上面 run_eval.py 的结果，找出有异常的数据源。
- 某解析步骤跑不通 → `navigate_page` 打开对应页面(搜索页代入真实关键词 / 条目详情页 / playUrl)→`take_snapshot` 看**渲染后** DOM,与 MCP 工具返回的静态 HTML 对比`list_network_requests` 看真实请求(搜索是否 POST/带 token、是否有返回数据的 JSON 接口、有没有 m3u8/mp4 媒体请求)。
- 判定「配置能修好」还是「引擎能力缺口」: 浏览器 DOM 与工具 HTML 结构一致 → 只是 selector 没写对; 浏览器有数据而工具 HTML 没有 → 页面靠 JS 渲染(找到底层 JSON 接口可改用 `json-path-indexed`, 否则是缺口)。

**关键纪律**: chrome-devtools 只用来**取真相、定位问题**。selector/正则的修正最终必须回到 `datasource-test` 的离线步骤、用**工具实际抓到的静态 HTML**(而非浏览器渲染后的 DOM)验证通过—— 因为 App 里跑的就是那个无 JS 引擎。**别把只在浏览器渲染后才成立的 selector 当作修好了**。

## 报告规范 (gen_report.py 实现此规范)   

报告由 `gen_report.py` **确定性生成**，不手写。以下是它必须产出的结构与约定——**这是标准**：改脚本或换实现时以此为准，保证每次评测产出一致的文档。面向开发者的完整评测规范另见仓库 [`docs/数据源评测规范.md`](../../../docs/数据源评测规范.md)，**修改任一处必须同步另一处**。

**`README.md`（总索引，顺序固定）**:
1. 标题 + `**评测日期: YYYY-MM-DD**`（来自 meta.json）+ 一句话方法说明 + 测试番剧列表。
2. 三个计数：稳定可用 / 部分可用 / 全部失败。
3. `## 🏆 推荐线路` — 每个源挑**一条**最佳线路（无广告 + 有实测数据 + 跨番成功 ≥ n-1 + 画质高 + 起播快），列 源·**线路**·Tier·分辨率·码率·起播·可播 N/n。直接告诉用户播哪条。
4. `## 📶 线路能力分级 (Tier)` — **每条实测线路一行**的最终 tier 表（与 `subs/web` 的 t0–t4 目录分层无关）。列: `源(线路)`（如 `omofun111(独家蓝光X)`，同源多线路拆成多行）· Tier · 分辨率 · 码率 · 编码 · 起播 · 广告 · 每部番 ✅/❌/—。分级标准（从上往下取第一个满足的；无广告只认视觉判定，未判定不算无广告；缺码率/起播数据视为不满足该项）:
   - **T0**（最高优先，该源查询成功客户端可直接选择、无需等待其他源）: 无广告 + 1080P + 码率 ≥ 1.8M + 起播 ≤ 3s
   - **T1**（高优先）: 无广告 + 1080P + 码率 ≥ 1.5M + 起播 ≤ 5s
   - **T2**（普通）: 无广告 + 1080P + 码率 ≥ 1.2M + 起播 ≤ 5s
   - **T3**: 1080P + 满足任一: ① 轻度及以下广告 + 码率 ≥ 1.5M + 起播 ≤ 8s；② 无广告 + 码率 ≥ 1.0M + 起播 ≤ 8s
   - **T4**: 轻度及以下广告 + 1080P + 码率 ≥ 1.0M + 起播 ≤ 8s
   - **T5**: 1080P + 码率 ≥ 1.0M
   - **T6**: 无要求
   **硬性约束**: 有**重度广告**（全程水印，或 HLS 自动广告过滤滤不掉的插入广告片段，见第 4 步归一口径）的线路**必须落在 T5 及之后**——T0–T4 的广告上限（无/≤轻）已保证这一点，日后调整分级标准时不得破坏。
   排序: Tier → 可播番数 → 最佳线路排序键。
5. `## 全部可用源` — 一行一源，按广告轻→成功率排序，含每部番 ✅/❌ 列 + 广告等级（`*` 表示另有更脏线路）。**分辨率/码率/起播列 = 该源"最佳线路"的实测值**，并列出最佳线路名。最佳线路定义（逐级比较，高优先在前）: ①无广告（视觉判定；未判定排最后，不吃算法启发式）②分辨率高 ③码率高 ④起播快；只在真实播出来过的线路中选。分辨率取该线路跨番实测**众数**，码率/起播取**中位数**。
6. `## 各源线路拆解` — 每个可用源一个小节，表格**每条线路 × 每部番**（列 = 番剧简称），单元格 ✅/❌/—，外加广告/分辨率/码率/起播/失败原因列，`⭐` 标记总表所用最佳线路。**这一节让主报告自包含，不必点进源页**。
7. `## 🔒 搜索阶段失败` — searchSubjects 就失败的源，表格 源·Tier·原因（验证码 / 域名失效 / 403 等）。
8. `## ❌ 解析/匹配阶段失败` — 搜到但后续步骤失败的源，表格 源·Tier·失败阶段·原因（selector 失配 / js 伪链接 / 视频解析失败 / 播放探测失败等）。

**`sources/<tier>-<源>.md`（源页）**: 顶部推荐线路一句话 + `## 线路拆解`（每线路 × 每部番，同 README 第 5 节）+ `## 跨番解析`。

**`channels/<tier>-<源>-<线路>.md`（线路页）**: 结论行 + `## 广告判定`（视觉判读结论 + HLS 结构探测辅助行: 检出几组插入片段、时间范围、客户端过滤器可否自动滤除）+ `## 播放采样`（0/3/8/15/25s 及加采点截图画廊，图文并茂）+ `## 媒体信息`（分辨率/编码/帧率/音频/时长/码率/**码率来源**: ffprobe 实测均值 / ffprobe format / 播放器统计）+ `## 播放性能`（打开/起播/首帧/卡顿）+ `## 跨番实播`（每番 结果 + 失败原因）。

**符号约定（全报告统一）**:
- 广告：`无`/`轻`/`中`/`重`；源级 `*` = 该源另有更脏线路。
- 数值口径：分辨率 = 该线路跨番实测众数；码率/起播 = 跨番实测中位数（含 deep 长播数据）。码率/分辨率优先 **ffprobe 实测**（实测均值 > format.bit_rate > 播放器统计，`bitrateSource` 标注来源）；起播为 **mpv 实播**的 `timeToPlayingMillis`。
- 列级 `-` = 该线路无此项数据（从未成功播放，故无分辨率/码率/起播/广告）。
- 番格 `✅` = 可播（真实实播 `playback.ran && ok`，快测或 deep/backfill 任一成功）；`❌` = 解析到但播放失败；`—` = 该番没解析到这条线路（线路名轮换），源级表里也可能是断点续跑时该番未测。
- 失败原因归纳成人话（"WebView 未匹配到视频 URL""视频 URL 返回 403(防盗链)""站点需人机验证"…），不暴露裸异常类名。

**广告判定来源**: 等级只用 `frames_verdicts.json` 的视觉判读（subagent 看图），线路级优先于源级；未判读的线路显示"未判定"而非拿 OCR 启发式充数。线路页另附 **HLS 结构探测**行（deep 的 `adDetect` / summary 的 `adFilter`）作辅助证据——它不决定等级本身，但它的 `filterable` 是"插入片段滤不滤得掉"的权威依据（进入第 4 步的重度判定口径）。

## 报告怎么读

- **推荐线路**: 直接告诉用户播哪个源的哪条线路（无广告 + 跨番稳定 + 画质好）。
- **线路能力分级 (T0–T6)**: 给客户端排优先级用的硬指标分级（见上节标准），T0 = 查询成功即可直接播、无需等待其他源。注意与 `subs/web` 的 t0–t4 **目录**分层是两回事。
- **`arguments.channelTiers`**: 将线路能力分级写入 Animeko v4.9+ 的有效 tier；线路名必须与报告/`alliance` 完全一致，空线路名不能覆盖。
- **可播证据**: ✅ 含 run_eval 快测与 deep 长播两路实播成功；deep 失败不记 ❌（二次解析可能只是 URL 过期）。
- **广告等级**: 无（干净）/ 轻（横幅仅片头/片尾）/ 中（片中也有横幅）/ 重（全程水印，或滤不掉的插入广告片段）；`*` 表示该源另有更脏线路。博彩水印是合规风险重点。
- **失败原因**: "搜索阶段失败"（验证码/域名失效 → 基本没救）vs "解析阶段失败"（selector 失配/js 伪链接 → 配置可修）——给维护者明确修复方向。
- 单元格 `-` = 该线路无此项数据（从未成功播放）; 番格 `—` = 该番没解析到这条线路（线路名轮换）, `❌` = 解析到但播放失败, `✅` = 可播。

## 排障

- **找不到 MCP**: 见上"依赖"节，构建或问用户 animeko/ani 位置。
- **MCP 起不来 / 端口异常**: server 起动后不响应 stdin，只走 HTTP `POST /mcp`；若端口被占或有残留实例，`pkill -f datasource-test-mcp` 清掉再跑（脚本本身用随机空闲端口，通常不冲突；JVM 冷启动可达 1–2 分钟属正常）。
- **全部源搜索失败**: 可能 Ani API 限流/元数据源问题，或番剧 subjectId/episodeId 填错——先用 MCP `get_subject_episodes` 核对。
- **评测跑一半报"mpv 原生库未加载, 中止评测"（或 mediaAnalysis.available 为 false）**: mpv 原生库没加载上，run_eval/deep_sample 会**主动中止**（继续跑只会把 HTTP 可达当可播）——`lib.Mcp` 会自动探测 mediamp worktree（`~/Projects/mediamp/mediamp-mpv/build/mpv-output/<平台>/lib`）并经 `DATASOURCE_TEST_MCP_OPTS` 注入 `-Dani.mpv.native.dir`；探测不到时设 `ANI_MPV_NATIVE_DIR` 指向含 libmpv/libmediampv/ffmpeg dylib 的目录。个别线路播不出来也可能是视频 URL 已过期（重跑 deep_sample 会重新解析）。分辨率/码率本就以 ffprobe 为准（见"媒体基础指标"）。
- **ffprobe/ffmpeg 拒读分片**: 伪装扩展名分片需 `-allowed_extensions ALL`（ffmpeg 7.1+ 再加 `-extension_picky 0`，脚本会自动探测追加）；仍拒绝时该线路码率/分辨率退用 mpv 实播数据。
- **detect_hls_ads 返回 unknown / 没有 hlsFilter**: 非 m3u8 URL 或清单拉取失败属预期（结果 JSON 会省略 null 字段，hlsFilter 可能整个缺失）；此时插入广告片段默认按滤不掉处理。
- **广告判定全"未判定"**: `frames_verdicts.json` 没写或键名对不上（源名/线路名要与 deep.json 完全一致）。
