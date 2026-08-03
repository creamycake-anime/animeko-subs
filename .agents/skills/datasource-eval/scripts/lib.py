"""共享库: MCP HTTP 客户端 (Streamable HTTP) + animeko MCP 二进制发现 + 报告目录约定.

被 run_eval.py / deep_sample.py / gen_report.py 复用.
"""
import atexit
import json
import os
import pathlib
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# 报告目录布局: <report_dir>/{meta.json, subjects/, deep/, montage/, combined/, sources/, channels/, README.md}


def repo_root():
    """ani-subs 仓库根 (脚本在 .agents/skills/datasource-eval/scripts/ 下)."""
    return pathlib.Path(__file__).resolve().parents[4]


def find_mcp_bin():
    """定位 animeko 的 datasource-test-mcp 可执行文件.

    顺序: 环境变量 ANIMEKO_MCP_BIN > sibling ../ani > 常见位置. 找不到返回 None.
    """
    env = os.environ.get("ANIMEKO_MCP_BIN")
    if env and pathlib.Path(env).exists():
        return pathlib.Path(env)
    rel = "tools/datasource-test-mcp/build/install/datasource-test-mcp/bin/datasource-test-mcp"
    candidates = [
        repo_root().parent / "ani" / rel,          # sibling animeko/ani
        repo_root().parent / "animeko" / rel,
        pathlib.Path.home() / "Projects/animeko/ani" / rel,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def require_mcp_bin(meta=None):
    if meta and meta.get("mcpBin") and pathlib.Path(meta["mcpBin"]).exists():
        return pathlib.Path(meta["mcpBin"])
    b = find_mcp_bin()
    if not b:
        sys.exit(
            "找不到 animeko datasource-test-mcp 可执行文件.\n"
            "  期望在 sibling 目录 ../ani/tools/datasource-test-mcp/build/install/...\n"
            "  请先在 animeko/ani 仓库构建: ./gradlew :tools:datasource-test-mcp:installDist\n"
            "  或设置环境变量 ANIMEKO_MCP_BIN 指向该可执行文件, 或在 meta.json 里写 mcpBin."
        )
    return b


def load_meta(report_dir):
    return json.loads((pathlib.Path(report_dir) / "meta.json").read_text())


def resolve_sources_root(report_dir, meta):
    """解析本次评测使用的 selector 配置根目录。

    meta.sourcesRoot 的相对路径优先相对于报告目录解析，便于报告冻结自己的 configs/；
    若该位置不存在，再相对于仓库根解析。未配置时保持兼容，读取 subs/web。
    """
    value = meta.get("sourcesRoot")
    if not value:
        return repo_root() / "subs" / "web"
    root = pathlib.Path(value)
    if root.is_absolute():
        return root
    report_relative = pathlib.Path(report_dir) / root
    if report_relative.exists():
        return report_relative
    return repo_root() / root


def playback_ok(probe):
    """真实实播成功: 只认播放器 playback.ran && ok.

    probe_video 的顶层 ok 在播放器不可用 (mpv 原生库没加载) 时会退回 HTTP 探测结论,
    HTTP 可达 ≠ 可播, 不能作为可播依据 (run_eval/deep_sample/gen_report 统一用本函数).
    """
    pb = (((probe or {}).get("mediaAnalysis") or {}).get("playback")) or {}
    return bool(pb.get("ran") and pb.get("ok"))


def require_player_available(probe):
    """mediaAnalysis.available == false 说明 mpv 原生库没加载, 播放全被跳过——
    继续跑会把 'HTTP 可达' 当 '可播' 记进报告, 直接中止并给出修复指引."""
    ma = (probe or {}).get("mediaAnalysis") or {}
    if ma.get("available") is False:
        sys.exit(
            "mpv 原生库未加载 (mediaAnalysis.available=false), 实播全部被跳过, 中止评测.\n"
            "  继续跑只会把 HTTP 可达当可播, 产出虚假报告.\n"
            "  修复: 构建 mediamp worktree 的 mpv natives, 或设 ANI_MPV_NATIVE_DIR 指向\n"
            "  含 libmpv/libmediampv 的目录 (见 SKILL.md 排障).\n"
            f"  播放器错误: {'; '.join(ma.get('errors') or [])[:300]}"
        )


def safe_dir(value):
    """线路名 → 安全目录名 (run_eval 与 deep_sample 统一用, 保证路径一致)."""
    return re.sub(r'[<>:"/\\|?*]+', "_", str(value)).strip() or "default"


def find_mpv_native_dir():
    """mpv 原生库目录 (mediamp runtime 工件未发布, 默认 classpath 加载会失败,
    需把 -Dani.mpv.native.dir 传给 MCP 的 JVM; 见 Mcp.start).

    顺序: 环境变量 ANI_MPV_NATIVE_DIR > mediamp worktree dev 构建按平台自动探测.
    找不到返回 None (MCP 自行报错降级, probe_video 退化为纯 HTTP 探测, 不算致命).
    """
    env = os.environ.get("ANI_MPV_NATIVE_DIR")
    if env and pathlib.Path(env).is_dir():
        return pathlib.Path(env)
    sysname = platform.system().lower()
    mach = platform.machine().lower()
    if sysname == "darwin":
        flavor = "MacosArm64" if mach in ("arm64", "aarch64") else "MacosX64"
    elif sysname == "windows":
        flavor = "WindowsX64"
    else:
        flavor = "LinuxX64"
    rel = f"mediamp/mediamp-mpv/build/mpv-output/{flavor}/lib"
    candidates = [
        pathlib.Path.home() / "Projects" / rel,
        repo_root().parent.parent / rel,  # ani-subs 所在目录的上一级 (与 animeko/ 同级)
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


class Mcp:
    """极简 MCP 客户端 (Streamable HTTP: 每条 JSON-RPC 消息 POST /mcp, 无状态/无 SSE).

    默认自启动: server 进程由本类启动, 用随机空闲端口 (避免与其他实例抢默认 8264).
    单例串行调用, 崩溃/超时自动重启; 进程不会因 stdin 关闭而退出, 必须显式 kill
    (atexit 兜底, 不留孤儿进程).

    外部 server 模式: 设置环境变量 ANIMEKO_MCP_URL (如 http://127.0.0.1:8264/mcp) 时,
    不 spawn 子进程, 直接 POST 到该 URL; stop() 也不 kill 任何东西.
    """

    READY_TIMEOUT_S = 120  # JVM 冷启动慢

    def __init__(self, bin_path, log=None):
        self.bin = str(bin_path)
        self.url = os.environ.get("ANIMEKO_MCP_URL")
        self.proc = None
        self.port = None
        self.next_id = 0
        self.log = log or (lambda m: None)
        atexit.register(self.stop)

    def start(self):
        if self.url:  # 外部 server: 只做一次 initialize 验证连通
            self.next_id = 0
            self._post({"jsonrpc": "2.0", "id": self._id(), "method": "initialize"}, 60)
            return
        self.stop()
        with socket.socket() as s:  # 拿一个空闲端口
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = os.environ.copy()
        native = find_mpv_native_dir()
        if native:  # Gradle 启动脚本认 DATASOURCE_TEST_MCP_OPTS 作为 JVM opts; 追加不覆盖
            opts = env.get("DATASOURCE_TEST_MCP_OPTS", "").strip()
            flag = f"-Dani.mpv.native.dir={native}"
            env["DATASOURCE_TEST_MCP_OPTS"] = f"{opts} {flag}" if opts else flag
        else:
            self.log("  (未找到 mpv 原生库目录, probe_video 可能退化为纯 HTTP 探测; "
                     "可设 ANI_MPV_NATIVE_DIR)")
        self.proc = subprocess.Popen(
            [self.bin, "--host", "127.0.0.1", "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.next_id = 0
        deadline = time.time() + self.READY_TIMEOUT_S
        while True:  # 轮询 initialize 直到 HTTP 200
            if self.proc.poll() is not None:
                raise RuntimeError(f"MCP server 启动即退出 (exit={self.proc.returncode})")
            try:
                self._post({"jsonrpc": "2.0", "id": self._id(), "method": "initialize"}, 5)
                return
            except Exception:
                if time.time() > deadline:
                    self.stop()
                    raise TimeoutError(f"MCP server {self.READY_TIMEOUT_S}s 内未就绪")
                time.sleep(0.5)

    def stop(self):
        if self.url:  # 外部 server 不归本类管
            return
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def _id(self):
        self.next_id += 1
        return self.next_id

    def _endpoint(self):
        return self.url or f"http://127.0.0.1:{self.port}/mcp"

    def _post(self, obj, timeout_s):
        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(obj, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
        return json.loads(body) if body else None  # notification 返回 202 空 body

    def call(self, tool, args, timeout_s=300):
        if self.url is None and (self.proc is None or self.proc.poll() is not None):
            self.start()
        msg = {"jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
        try:
            resp = self._post(msg, timeout_s)
        except Exception as e:
            if self.url:  # 外部 server: 不重启, 只报错
                self.log(f"  !! {tool}: {e}")
                return {"_error": str(e)}
            self.log(f"  !! {tool}: {e}; 重启 MCP")
            self.start()
            return {"_error": str(e)}
        sc = ((resp or {}).get("result") or {}).get("structuredContent")
        if sc is not None:
            return sc
        return {"_error": json.dumps((resp or {}).get("error") or resp, ensure_ascii=False)[:300]}


# ---- ffprobe / ffmpeg: 码率/分辨率/编码等基础媒体指标 (mpv 实播只负责可播性/起播/卡顿) ----

_HLS_PICKY_CACHE = {}
DIRECT_MEDIA_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov"})


def is_explicit_direct_media(url):
    """URL 路径有明确的非 HLS 媒体后缀。查询参数不参与判断。"""
    path = urllib.parse.urlsplit(str(url)).path
    return pathlib.PurePosixPath(path).suffix.lower() in DIRECT_MEDIA_SUFFIXES


def _hls_flags(bin_name):
    """伪装扩展名分片 (.jpeg/.png) 兼容参数: -allowed_extensions ALL;
    ffmpeg 7.1+ 的 hls demuxer 还需 -extension_picky 0 (探测一次并缓存)."""
    if bin_name not in _HLS_PICKY_CACHE:
        picky = False
        try:
            p = subprocess.run([bin_name, "-hide_banner", "-h", "demuxer=hls"],
                               capture_output=True, timeout=10)
            picky = b"extension_picky" in p.stdout
        except Exception:
            pass
        _HLS_PICKY_CACHE[bin_name] = picky
    flags = ["-allowed_extensions", "ALL"]
    if _HLS_PICKY_CACHE[bin_name]:
        flags += ["-extension_picky", "0"]
    return flags


def _input_flags(bin_name, url):
    """只给 HLS/无扩展名等可疑输入附加 HLS demuxer 参数。

    ``extension_picky`` 是 HLS demuxer 私有选项；传给明确的 MP4 等直链时，
    ffmpeg 会以 ``Option extension_picky not found`` 直接拒绝输入。
    """
    return [] if is_explicit_direct_media(url) else _hls_flags(bin_name)


def _headers_arg(headers):
    """headers dict → ffmpeg/ffprobe 的 -headers 参数值 (每行 'K: V' 以 CRLF 结尾)."""
    if not headers:
        return None
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def _num(x, cast=float):
    try:
        return cast(x)
    except (TypeError, ValueError):
        return None


def ffprobe_streams(url, headers=None, timeout=30):
    """ffprobe 拉流信息: 分辨率/编码/帧率/时长/format 码率. 失败返回 None (stderr 警告).

    注意: HLS 直连的 format.bit_rate 常缺/不可靠, 平均码率实测用 ffmpeg_measure_bitrate.
    """
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", *_input_flags("ffprobe", url)]
    ha = _headers_arg(headers)
    if ha:
        cmd += ["-headers", ha]
    cmd.append(str(url))
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        print("警告: 未安装 ffprobe (brew install ffmpeg), 跳过媒体基础指标", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"警告: ffprobe 超时 ({timeout}s): {str(url)[:80]}", file=sys.stderr)
        return None
    if p.returncode != 0:
        print(f"警告: ffprobe 失败: {p.stderr.decode(errors='replace').strip()[:200]}",
              file=sys.stderr)
        return None
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    fps = None
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
    if "/" in fr:
        num, den = (_num(x) for x in fr.split("/", 1))
        if num and den:
            fps = round(num / den, 2)
    return {
        "width": v.get("width"), "height": v.get("height"),
        "vcodec": v.get("codec_name"), "acodec": a.get("codec_name"),
        "fps": fps,
        "durationSeconds": _num(fmt.get("duration")),
        "formatBitrate": _num(fmt.get("bit_rate"), int),
    }


def ffmpeg_measure_bitrate(url, headers=None, seconds=30, timeout=None):
    """实测平均码率: ffmpeg -c copy 拷 N 秒到本地 .ts, 再 ffprobe 本地文件
    (本地文件的 bit_rate ≈ size*8/duration, 直连 m3u8 的 format.bit_rate 常缺).

    返回 {sampleSeconds(实际拷到的时长), sampleBytes, avgBitrate(bps)} 或 None.
    """
    timeout = timeout or seconds * 4 + 60
    fd, tmp = tempfile.mkstemp(suffix=".ts", prefix="dseval_")
    os.close(fd)
    try:
        cmd = ["ffmpeg", "-y", "-v", "error"]
        ha = _headers_arg(headers)
        if ha:
            cmd += ["-headers", ha]
        cmd += [*_input_flags("ffmpeg", url), "-t", str(seconds), "-i", str(url),
                "-c", "copy", "-f", "mpegts", tmp]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            print("警告: 未安装 ffmpeg (brew install ffmpeg), 跳过码率实测", file=sys.stderr)
            return None
        except subprocess.TimeoutExpired:
            print(f"警告: ffmpeg 码率实测超时 ({timeout}s): {str(url)[:80]}", file=sys.stderr)
            return None
        if not os.path.getsize(tmp):  # 拷贝没产出任何数据 (中途失败仍可能有部分数据, 照量)
            if p.returncode != 0:
                print(f"警告: ffmpeg 码率实测失败: {p.stderr.decode(errors='replace').strip()[:200]}",
                      file=sys.stderr)
            return None
        try:
            p2 = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate",
                 "-print_format", "json", tmp],
                capture_output=True, timeout=30)
            fmt = json.loads(p2.stdout).get("format", {})
        except Exception:
            return None
        dur = _num(fmt.get("duration"))
        size = _num(fmt.get("size"), int)
        br = _num(fmt.get("bit_rate"), int)
        if not dur or not size:
            return None
        return {"sampleSeconds": round(dur, 2), "sampleBytes": size,
                "avgBitrate": br or int(size * 8 / dur)}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def ffprobe_all(url, headers=None, sample_seconds=30):
    """流信息 + 实测均值码率一把梭: {"streams": ..., "measured": ...}.
    streams 失败 (URL 不可达 / ffprobe 拒读) 则跳过实测."""
    streams = ffprobe_streams(url, headers)
    measured = ffmpeg_measure_bitrate(url, headers, seconds=sample_seconds) if streams else None
    return {"streams": streams, "measured": measured}


def pick_bitrate_resolution(ffprobe, media_analysis):
    """码率/分辨率取值优先级 (run_eval 与 gen_report 共用, 保证口径一致).

    码率: ffprobe 实测均值 > ffprobe format.bit_rate > 播放器统计 (overallBitrate/流 bitrate).
    分辨率: ffprobe 流信息 > 播放器. 返回 (bitrate, bitrateSource, resolution).
    """
    fp = ffprobe or {}
    fst = fp.get("streams") or {}
    fme = fp.get("measured") or {}
    ma = media_analysis or {}
    v = ma.get("video") or {}
    if fme.get("avgBitrate"):
        br, src = fme["avgBitrate"], "ffprobe_measured"
    elif fst.get("formatBitrate"):
        br, src = fst["formatBitrate"], "ffprobe_format"
    else:
        br = ma.get("overallBitrate") or v.get("bitrate")
        src = "player" if br else None
    if fst.get("width") and fst.get("height"):
        res = f"{fst['width']}x{fst['height']}"
    elif v.get("width"):
        res = f"{v['width']}x{v['height']}"
    else:
        res = None
    return br, src, res
