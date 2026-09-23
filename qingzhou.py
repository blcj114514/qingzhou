#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻舟 qingzhou — 极简终端 Agent（单文件 · 零第三方依赖 · OpenAI 兼容通吃）

融合各家 harness 之长：
  - smolagents/gptme 的文本协议工具调用（不依赖端点 function calling，杂牌后端兼容）
  - Codex / Claude Code 的三档权限模式（审批 / 全自动 / 逐条）
  - Claude Code 的会话落盘与 /resume /compact
  - DanTide 的健壮性（密钥 env 优先不落盘、容错 JSON、退避重试）
  - 夜班工坊的任务账本（TASKS.md，P0-P4 + 三态 + 留档）与 HANDOFF 交接
  - agent-relay v0.2 的战役模式（班次锁 TTL 接管 / STOP 收兵 / 轮次盖章）

用法：
  python qingzhou.py                     交互对话（配置缺失时自动进入首次配置向导）
  python qingzhou.py --task "任务"       单发任务（无交互，跑完退出）
  python qingzhou.py --yolo              全自动档（危险，等同 Codex --yolo）
  python qingzhou.py --campaign TASKS.md --rounds 10
                                         战役模式：按账本逐项领活干活回写，无人值守
  python qingzhou.py --resume            恢复上一次会话
会话内命令：/help /mode /model /new /resume /compact /tasks /handoff /status /exit

仅用 Python 标准库，兼容 Python 3.9+（老爷机可用 3.9 embeddable 便携包运行）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

QZ_VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DEFAULT = SCRIPT_DIR / "qingzhou.json"
# 会话目录跟配置文件走（U 盘场景：配置和会话都在盘里）；启动时由 main() 按实际 config 位置改写
SESSIONS_DIR = SCRIPT_DIR / "sessions"

LOCK_FILE = ".qz-lock"
STOP_FILE = ".qz-stop"
PROGRESS_FILE = ".qz-progress.md"
LOCK_TTL_DEFAULT = 1500  # 秒；与 agent-relay 同源：保持 < 调度间隔 ×2

MAX_TOOL_RESULT_CHARS = 8000     # 单次工具结果回喂模型的最大字符数
HISTORY_SOFT_LIMIT_CHARS = 150000  # 消息总长超过此值提示 /compact
AUTO_COMPACT_THRESHOLD = 0.85      # 超过软上限的此比例时自动压缩（对齐 CC autoCompact）
MAX_LIST_ENTRIES = 200

ENV_BASE_URL = "QINGZHOU_BASE_URL"
ENV_API_KEY = "QINGZHOU_API_KEY"
ENV_MODEL = "QINGZHOU_MODEL"

LOG_MAX_BYTES = 1024 * 1024  # 日志单文件上限 1MB，超了轮转成 .old


# ---------------------------------------------------------------------------
# 调用过程日志（U 盘跨主机排障用）：logs/qingzhou.log，纯文本逐行留痕
# ---------------------------------------------------------------------------


class RunLog:
    """轻量运行日志：记录环境快照、每步 LLM 请求、工具执行与异常。

    文件位置：配置文件旁的 logs/qingzhou.log（U 盘场景=日志跟着盘走）。
    单文件超 1MB 轮转成 qingzhou.log.old。敏感信息（api_key）绝不入日志。
    """

    def __init__(self, base_dir: Path, enabled: bool = True):
        self.enabled = enabled
        self.path = base_dir / "logs" / "qingzhou.log"
        if not enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_MAX_BYTES:
                old = self.path.with_suffix(".log.old")
                try:
                    old.unlink()
                except OSError:
                    pass
                self.path.replace(old)
            self.handle = self.path.open("a", encoding="utf-8", errors="replace", newline="\n")
        except OSError:
            self.enabled = False
            self.handle = None

    def write(self, level: str, event: str, **fields):
        if not self.enabled:
            return
        parts = ["%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, event]
        for k, v in fields.items():
            sv = str(v).replace("\n", "\\n")[:500]
            parts.append("%s=%s" % (k, sv))
        try:
            self.handle.write(" | ".join(parts) + "\n")
            self.handle.flush()
        except OSError:
            pass

    def snapshot(self, cfg: dict, workspace: Path):
        """环境快照：跨主机排障最关键的一段——系统/Python/工作区/后端（无密钥）。"""
        plat = "%s %s" % (sys.platform, os.name)
        try:
            import platform
            plat += " | " + platform.system() + " " + platform.release()
        except Exception:
            pass
        masked = (str(cfg.get("api_key") or "")[:6] + "***") if cfg.get("api_key") else "(env/未设)"
        self.write("INFO", "session_start",
                   version=QZ_VERSION, python=sys.version.split()[0], platform=plat,
                   cwd=str(workspace), argv=" ".join(sys.argv[1:]),
                   base_url=cfg.get("base_url", ""), model=cfg.get("model", ""), key=masked)

    def close(self):
        if getattr(self, "handle", None):
            try:
                self.handle.close()
            except OSError:
                pass

# ---------------------------------------------------------------------------
# 终端输出：ANSI 主题（Claude Code 风格），不支持 ANSI 的环境自动降级纯文本
# ---------------------------------------------------------------------------


class Theme:
    """终端配色。检测规则：ANSI 开启需同时满足——stdout 是 tty、非重定向、
    Windows 10 1809+（os.environ 里有 WT_SESSION/ANSICON 或 win10+ 均按可用处理，
    老机器 cmd 检测失败则整体降级为纯文本，绝不乱码）。"""

    def __init__(self):
        self.on = self._detect()
        # 色号：1 亮红 2 亮绿 3 黄 4 暗蓝 5 品红 6 青 7 亮白 90 亮黑(灰)
        self.dim = "\x1b[90m"      # 次要信息（提示、边框、说明）
        self.cyan = "\x1b[96m"     # 工具动作 / 标题
        self.green = "\x1b[92m"    # 成功
        self.yellow = "\x1b[93m"   # 警告
        self.red = "\x1b[91m"      # 错误
        self.bold = "\x1b[1m"
        self.reset = "\x1b[0m"
        # 轻舟自有符号（帆船拟人，区别于 Claude Code 的星形机器人）
        self.dot = "⛵"            # 动作圆点：小帆船
        self.indent = "  ⎿ "       # 结果缩进
        # 单行船 spinner：帆船破浪前进（4 帧摇桨）
        self.spinner_chars = [" ⛵ ", " ⛵~", " ~⛵", " ⛵ "]
        if not self.on:
            self.dim = self.cyan = self.green = self.yellow = self.red = self.bold = self.reset = ""
            self.dot = "*"
            self.indent = "    - "
            self.spinner_chars = ["|", "/", "-", "\\"]

    @staticmethod
    def _detect() -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        if not sys.stdout.isatty():
            return False
        if os.name == "nt":
            # Windows Terminal / 新版 conhost 都能吃 ANSI；老 conhost 试试启用 VT
            if os.environ.get("WT_SESSION") or os.environ.get("ANSICON") or os.environ.get("TERM_PROGRAM"):
                return True
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except Exception:
                return False
        return True

    def c(self, color: str, text: str) -> str:
        if not self.on:
            return text
        return color + text + self.reset

    def paint(self, text: str) -> str:
        """行内标记上色：【标签】→ 黄、[轻舟] → 青、⚠ → 黄、✅ → 绿。"""
        if not self.on:
            return text
        text = text.replace("[轻舟]", self.c(self.cyan, "[轻舟]"))
        text = text.replace("⚠", self.c(self.yellow, "⚠"))
        text = text.replace("✅", self.c(self.green, "✓"))
        text = text.replace("[出错]", self.c(self.red, "[出错]"))
        return text


THEME = Theme()


def _reconfigure_stdio():
    """尽量把 stdout/stderr 调成 UTF-8，防止 GBK 控制台打印中文崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


class Spinner:
    """思考中动画：单行转圈 + 计时（Claude Code 式）。quiet/非 tty 时静默。

    线程安全设计（修 UI 三病根）：spinner 与流式文本绝不共写终端——
    1) 流式首字节到达时 spinner 立即熄灭（content_started 事件）并让出行；
    2) 擦除按显示宽度计算（CJK=2 格），不再依赖字符数猜测；
    3) 所有写终端都走同一个锁，杜绝 \r 与流式输出交叠截断。
    """

    def __init__(self, label: str = "思考中"):
        self.label = label
        self._stop = threading.Event()
        self._content_started = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._last_width = 0
        self.on = THEME.on and sys.stdout.isatty() and not os.environ.get("QINGZHOU_NO_SPINNER")

    def notify_content(self):
        """流式内容开始输出前调用：spinner 立即熄灭让行。

        擦除必须同步在锁内完成再放行——否则 spin 线程的收尾 _erase
        可能晚于流式首行写出，把已经输出的首行当残余帧擦掉（竞争窗口 G）。
        """
        if self.on:
            with self._lock:
                self._content_started.set()
                self._erase()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=0.5)

    def _erase(self):
        """按显示宽度擦除上一帧（CJK 宽字符占 2 格，退格数要对得上）。"""
        if self._last_width > 0:
            try:
                sys.stdout.write("\r" + " " * self._last_width + "\r")
                sys.stdout.flush()
            except OSError:
                pass
            self._last_width = 0

    def __enter__(self):
        if not self.on:
            return self
        self._start_t = time.time()

        def _spin():
            i = 0
            while not self._stop.is_set() and not self._content_started.is_set():
                ch = THEME.spinner_chars[i % len(THEME.spinner_chars)]
                elapsed = int(time.time() - self._start_t)
                line = "  %s %s… %ds" % (THEME.c(THEME.cyan, ch), self.label, elapsed)
                with self._lock:
                    if self._content_started.is_set():
                        break
                    try:
                        self._erase()
                        sys.stdout.write("\r" + line)
                        sys.stdout.flush()
                        self._last_width = _disp_width(line) + 2
                    except OSError:
                        break
                i += 1
                self._stop.wait(0.1)
            with self._lock:
                self._erase()

        self._thread = threading.Thread(target=_spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self.on:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=1)
            with self._lock:
                self._erase()
        return False


def _poll_keyboard(buf):
    """非阻塞收键（消息排队用）：Windows 走 msvcrt，Unix 走 termios。
    返回 (updated_buf, complete)；回车时 complete=True。
    每次调用排空所有已按下的键——只读一个键会让长输出时排队输入一字一顿。"""
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    # 功能键/方向键是双码序列：吞掉后缀码，不混入输入缓冲
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                if ch in ("\r", "\n"):
                    return buf, True
                if ch == "\b":
                    buf = buf[:-1]
                    continue
                buf += ch
        else:
            import select
            while select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    return buf, True
                if ch in ("\x7f", "\b"):
                    buf = buf[:-1]
                    continue
                buf += ch
    except Exception:
        pass
    return buf, False


def say(text: str = ""):
    print(THEME.paint(text), flush=True)


def pretty_model(model: str) -> str:
    """模型名美化：glm-5.3-flash → GLM 5.3 Flash（仅显示用，请求仍用原名）。"""
    BRAND = {"gpt": "GPT", "kimi": "Kimi", "glm": "GLM", "llama": "Llama", "qwen": "Qwen",
             "deepseek": "DeepSeek", "gemini": "Gemini", "claude": "Claude"}
    s = re.sub(r"(?<=[a-zA-Z0-9])-(?=[a-zA-Z0-9])", " ", str(model))

    def cap(tok: str) -> str:
        low = tok.lower()
        if low in BRAND:
            return BRAND[low]
        if re.fullmatch(r"v\d+", low):
            return "V" + tok[1:]
        if re.fullmatch(r"\d+(\.\d+)*", tok):
            return tok
        if re.fullmatch(r"\d+[bkm]", low):
            return tok.upper()
        return tok.capitalize() if len(tok) > 2 else tok.upper()

    return " ".join(cap(t) for t in s.split())


def _disp_width(text: str) -> int:
    """终端显示宽度：CJK/全角算 2 格，其余 1 格（对齐框线用）。"""
    import unicodedata
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def banner(cfg: dict = None):
    """CC 式紧凑横幅（逆向其布局后落地）：小帆船 + 双列信息，总高 5 行。

    CC 布局三原则的轻舟落地：
    - banner 只占顶部一小块，对话区是绝对主角（CC 的 9 行帆船 ASCII 换成 2 行）
    - 信息双列排布（形象 | 版本/模型/目录），一眼看清上下文
    - 不再打印圆角框（CC 无框，靠留白分区）——留白即分隔
    """
    t = THEME
    say("")
    info1 = t.c(t.bold, "轻舟 qingzhou") + t.c(t.dim, "  v%s · 极简终端 Agent" % QZ_VERSION)
    model_txt = pretty_model(str(cfg.get("model", ""))) if cfg and cfg.get("model") else "(未配置)"
    info2 = t.c(t.cyan, model_txt) + t.c(t.dim, " · " + os.getcwd())
    info3 = t.c(t.dim, "会读写文件、执行命令；/help 命令 · Ctrl+C 打断")
    say("  " + t.c(t.cyan, "⛵ ") + info1)
    say("  " + t.c(t.dim, "▛▀▜ ") + info2)
    say("  " + t.c(t.dim, "▂▃▂ ") + info3)
    say("")


class QingzhouError(Exception):
    """可预期的错误（配置缺失、请求失败等），直接展示给用户。"""


# ---------------------------------------------------------------------------
# 配置层：qingzhou.json + 环境变量（env 优先；env 的 key 永不回写落盘）
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    cfg = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise QingzhouError("配置文件损坏（%s）：%s\n请修复或删除后重跑向导。" % (config_path, exc))
    return cfg if isinstance(cfg, dict) else {}


def env_override(cfg: dict) -> dict:
    """环境变量覆盖配置文件；env 提供的值标记 _from_env，保存时剔除不落盘。"""
    merged = dict(cfg)
    for env_name, key in (
        (ENV_BASE_URL, "base_url"),
        (ENV_API_KEY, "api_key"),
        (ENV_MODEL, "model"),
    ):
        val = os.environ.get(env_name, "").strip()
        if val:
            merged[key] = val
            merged["_" + key + "_from_env"] = True
    return merged


def save_config(config_path: Path, cfg: dict, allow_api_key: bool = False):
    """落盘前剔除 env 注入的键，密钥来源为环境变量时绝不写入文件。

    双保险：即使 cfg 里没有 _from_env 标记（如运行时手工塞入的值），
    api_key 也只在"配置文件里本来就写着"时才原样保留，防止任何路径
    把环境变量/内存里的密钥意外落盘。唯一例外：首次配置向导中用户
    显式同意保存，此时以 allow_api_key=True 调用。
    """
    file_cfg = load_config(config_path) if config_path.exists() else {}
    clean = {}
    for k, v in cfg.items():
        if k.startswith("_") and k.endswith("_from_env"):
            continue
        if k in ("base_url", "api_key", "model"):
            if cfg.get("_" + k + "_from_env"):
                # env 提供的值不落盘，但文件里已有的旧值保留
                if k in file_cfg:
                    clean[k] = file_cfg[k]
                continue
            if k == "api_key" and k not in file_cfg and not allow_api_key:
                # 运行时产生的 key 值不新增落盘
                continue
        clean[k] = v
    config_path.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_base_url(raw: str) -> str:
    """校验并规范 base_url：补 /chat/completions 尾巴；只放行 http/https。

    注：不拦内网/回环地址——LM Studio / Ollama 本地后端（127.0.0.1）是明确需求，
    本工具是用户自配自用的单机程序，SSRF 威胁模型不适用。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        raise QingzhouError("base_url 未配置")
    low = base.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise QingzhouError("base_url 必须以 http:// 或 https:// 开头：%r" % raw)
    if not low.endswith("/chat/completions"):
        base += "/chat/completions"
    return base


def first_run_wizard(config_path: Path) -> dict:
    """首次运行配置向导：选后端 → 填 key → 选/填模型。"""
    if not sys.stdin.isatty():
        raise QingzhouError(
            "未检测到配置，且当前不是交互终端，无法进入配置向导。\n"
            "请任选其一：\n"
            "  1. 直接双击运行（交互模式）完成首次配置；\n"
            "  2. 用环境变量提供配置：%s / %s / %s" % (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL))
    say("未检测到配置，进入首次配置向导（数据保存在 %s）" % config_path)
    say("选择模型后端：")
    say("  1. ollama.com 云端    （https://ollama.com/v1）")
    say("  2. LM Studio 本地     （http://127.0.0.1:1234/v1，需先在 LM Studio 起 Server）")
    say("  3. DeepSeek           （https://api.deepseek.com/v1）")
    say("  4. 其他 OpenAI 兼容端点（手填 base_url）")
    choice = input("输入 1-4 [4]: ").strip() or "4"
    presets = {
        "1": "https://ollama.com/v1",
        "2": "http://127.0.0.1:1234/v1",
        "3": "https://api.deepseek.com/v1",
    }
    base_url = presets.get(choice, "")
    if not base_url:
        base_url = input("base_url（如 https://api.example.com/v1）: ").strip()
    api_key = input("API Key（可留空，之后用环境变量 %s 提供）: " % ENV_API_KEY).strip()
    model = input("模型名（如 glm-5.3-flash / qwen3.5-9b / deepseek-chat）: ").strip()
    if not (base_url and model):
        raise QingzhouError("base_url 和模型名不能为空，向导未保存任何配置。")
    cfg = {"base_url": validate_base_url(base_url), "api_key": api_key, "model": model}
    if api_key:
        keep = input("把 API Key 保存进本地配置文件？单机自用可存 [Y/n]: ").strip().lower()
        if keep in ("n", "no"):
            cfg["api_key"] = ""
            say("已留空；之后请用环境变量 %s 提供密钥。" % ENV_API_KEY)
    save_config(config_path, cfg, allow_api_key=bool(cfg.get("api_key")))
    say("配置已保存 → %s\n" % config_path)
    return cfg


def ensure_config(config_path: Path) -> dict:
    cfg = env_override(load_config(config_path))
    if not cfg.get("base_url") or not cfg.get("model"):
        cfg = env_override(first_run_wizard(config_path))
    cfg["base_url"] = validate_base_url(str(cfg.get("base_url", "")))
    return cfg


# ---------------------------------------------------------------------------
# LLM 客户端：urllib 手写，SSE 流式优先，失败退化非流式；429/5xx 退避重试
# ---------------------------------------------------------------------------


VALID_EFFORTS = ("none", "low", "medium", "high", "max")

# 危险命令 deny 清单（照 Claude Code permissions.deny 精神）：正则，命中即拦，任何权限档都拦。
# 可用配置 "deny_patterns": ["格式串", ...] 追加自定义规则。
DEFAULT_DENY_PATTERNS = [
    r"rm\s+-rf\s+/",            # 递归强删根/关键路径
    r"(?:rd|rmdir)\s+/s\s+",     # Windows 递归删根（rd 是 rmdir 别名）
    r"del\s+/[sq]\s+C:\\",      # Windows del 强删 C 盘
    r"format\s+[a-zA-Z]:",       # 格式化磁盘
    r"mkfs",                      # Linux 格式化
    r":\(\)\s*\{.*\};:",      # fork bomb
    r"^\s*(shutdown|Restart-Computer)\b",  # 关机/重启（行首命令，避免误杀 echo/说明文字）
    r"^\s*diskpart\b",           # 磁盘分区操作（行首）
    r">\s*/dev/sd[a-z]",         # 直写块设备
]


def compile_deny_patterns(cfg: dict, workspace: Path):
    """内置 deny + 配置追加。返回 [(compiled, 原串)]。"""
    extra = cfg.get("deny_patterns") or []
    if not isinstance(extra, list):
        extra = []
    patterns = []
    for raw in list(DEFAULT_DENY_PATTERNS) + [str(x) for x in extra]:
        try:
            patterns.append((re.compile(raw, re.IGNORECASE), raw))
        except re.error:
            continue
    return patterns


class LLMClient:
    def __init__(self, cfg: dict, no_stream: bool = False, runlog=None):
        self.base_url = cfg["base_url"]
        self.api_key = str(cfg.get("api_key", "") or "")
        self.model = str(cfg["model"])
        self.max_tokens = cfg.get("max_tokens")
        # 思考档位（对齐 Claude Code effortLevel 设计）：配置键 reasoning_effort，
        # 环境变量 QINGZHOU_EFFORT 可覆盖；none=关思考，max=最深，缺省=服务端默认
        effort = str(os.environ.get("QINGZHOU_EFFORT", "") or cfg.get("reasoning_effort") or "").strip().lower()
        self.effort = effort if effort in VALID_EFFORTS else ""
        self.no_stream = no_stream
        self.runlog = runlog

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _body(self, messages, stream):
        body = {"model": self.model, "messages": messages, "stream": stream}
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        if self.effort:
            body["reasoning_effort"] = self.effort  # ollama.com/部分端点认档；不认会被忽略
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _post(self, messages, stream, timeout=180):
        req = urllib.request.Request(
            self.base_url, data=self._body(messages, stream), headers=self._headers(), method="POST"
        )
        return urllib.request.urlopen(req, timeout=timeout)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """429/5xx/网络错误值得退避重试。"""
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code == 429 or 500 <= exc.code < 600
        return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))

    def chat(self, messages, on_delta=None):
        """对话一轮，返回完整回复文本。流式边收边打印；流式不可用自动退化。

        对外只抛 QingzhouError（含人类可读的 401/403 提示）：HTTPError/URLError
        等原始异常全部在内部消化，任何一层都不会裸 traceback 闪退。
        """
        attempts = 3
        last_exc = None
        if self.runlog:
            self.runlog.write("INFO", "llm_request", n_msg=len(messages),
                              total_chars=sum(len(str(m.get("content", ""))) for m in messages),
                              stream=not self.no_stream)
        try:
            for i in range(attempts):
                if self.no_stream:
                    reply = self._chat_nonstream(messages)
                else:
                    try:
                        reply = self._chat_stream(messages, on_delta)
                    except urllib.error.HTTPError as exc:
                        last_exc = exc
                        if self.runlog:
                            self.runlog.write("WARN", "llm_stream_http_error", code=exc.code, attempt=i + 1)
                        if self._is_retryable(exc) and i < attempts - 1:
                            wait = 2 ** i
                            say("  [轻舟] 请求被限流/服务暂不可用（HTTP %s），%d 秒后重试…" % (exc.code, wait))
                            time.sleep(wait)
                            continue
                        # 400/404 等多半是端点不支持流式 → 退化非流式再试一次；
                        # 退化调用同样可能抛异常，由外层 except 统一接住
                        say("  [轻舟] 流式请求失败（HTTP %s），改用非流式…" % exc.code)
                        reply = self._chat_nonstream(messages)
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        last_exc = exc
                        if self.runlog:
                            self.runlog.write("WARN", "llm_network_error", error=str(exc), attempt=i + 1)
                        if i < attempts - 1:
                            wait = 2 ** i
                            say("  [轻舟] 网络波动（%s），%d 秒后重试…" % (exc, wait))
                            time.sleep(wait)
                            continue
                        break
                if self.runlog:
                    self.runlog.write("INFO", "llm_reply", chars=len(reply),
                                      has_tool_block="```qingzhou" in reply)
                return reply
        except urllib.error.HTTPError as exc:
            # 鉴权类错误给人话提示（退化非流式后的 401/403 会走到这里）
            if self.runlog:
                self.runlog.write("ERROR", "llm_http_fatal", code=exc.code)
            if exc.code in (401, 403):
                raise QingzhouError(
                    "鉴权失败（HTTP %d）：API Key 无效、过期或没有该模型权限。\n"
                    "请检查配置文件里的 api_key，或环境变量 %s；ollama.com 的密钥过期后需要重新生成。"
                    % (exc.code, ENV_API_KEY))
            raise QingzhouError("LLM 请求失败（HTTP %d）：%s" % (exc.code, exc))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.runlog:
                self.runlog.write("ERROR", "llm_network_fatal", error=str(exc)[:200])
            raise QingzhouError("网络请求失败：%s（检查网络/代理，或该 base_url 是否可达）" % exc)
        except QingzhouError as exc:
            if self.runlog:
                self.runlog.write("ERROR", "llm_failed", error=str(exc)[:300])
            raise
        if self.runlog:
            self.runlog.write("ERROR", "llm_retries_exhausted", last=str(last_exc)[:200])
        raise QingzhouError("LLM 请求重试 %d 次仍失败：%s" % (attempts, last_exc))

    def _chat_stream(self, messages, on_delta=None) -> str:
        resp = self._post(messages, stream=True)
        chunks = []
        saw_sse_data = False
        with resp:
            content_type = ""
            try:
                content_type = (resp.headers.get("Content-Type") or "").lower()
            except Exception:
                pass
            if "text/event-stream" not in content_type and "json" in content_type:
                # 端点无视 stream 参数直接回了 JSON —— 按非流式解析
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                choices = data.get("choices") or []
                if choices:
                    content = choices[0].get("message", {}).get("content")
                    if isinstance(content, str):
                        if on_delta:
                            on_delta(content)
                        return content
                raise QingzhouError("LLM 流式响应解析失败")
            for raw_line in resp:
                try:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not line or line.startswith(":"):
                    continue  # 注释/心跳
                if not line.startswith("data:"):
                    continue
                saw_sse_data = True
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or choices[0].get("message") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
                    if on_delta:
                        on_delta(piece)
        if not saw_sse_data and not chunks:
            raise QingzhouError("LLM 流式响应为空（端点可能不支持 stream），将退化非流式。")
        return "".join(chunks)

    def _chat_nonstream(self, messages) -> str:
        resp = self._post(messages, stream=False)
        with resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        choices = data.get("choices") or []
        if not choices:
            raise QingzhouError("LLM 响应异常（无 choices）：" + json.dumps(data, ensure_ascii=False)[:200])
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise QingzhouError("LLM 响应异常（无 content）：" + json.dumps(data, ensure_ascii=False)[:200])
        return content


# ---------------------------------------------------------------------------
# 工具集：read_file / write_file / list_dir / run_cmd / final_answer
# ---------------------------------------------------------------------------


def _normcase(p: Path) -> str:
    return os.path.normcase(str(p))


def in_workspace(path: str, workspace: Path) -> bool:
    """判断路径是否落在工作区（启动目录）内。Windows 不区分大小写。"""
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False
    probe = target if target.exists() or str(target) == str(workspace) else target.parent
    try:
        return os.path.commonpath([_normcase(workspace), _normcase(probe)]) == _normcase(workspace)
    except ValueError:
        return False


def _clip(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（结果过长已截断，原长 %d 字符）" % len(text)


def _read_text_smart(path: Path) -> str:
    """读文件：先按 UTF-8（自动去 BOM），失败退 GBK，再失败用替换符。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def tool_read_file(args: dict, workspace: Path) -> str:
    path = str(args.get("path", "")).strip()
    if not path:
        return "[错误] read_file 缺少 path"
    target = Path(path)
    if not target.is_absolute():
        target = workspace / target
    if not target.is_file():
        return "[错误] 文件不存在：%s" % target
    try:
        text = _read_text_smart(target)
    except OSError as exc:
        return "[错误] 读取失败：%s" % exc
    lines = text.splitlines()
    start = int(args.get("start") or 1)
    end = int(args.get("end") or len(lines))
    start = max(1, start)
    picked = lines[start - 1 : end]
    numbered = "\n".join("%4d | %s" % (start + i, ln) for i, ln in enumerate(picked))
    return _clip("文件 %s（共 %d 行，显示第 %d-%d 行）\n%s" % (target, len(lines), start, end, numbered))


def tool_write_file(args: dict, workspace: Path) -> str:
    path = str(args.get("path", "")).strip()
    content = args.get("content", "")
    if not path:
        return "[错误] write_file 缺少 path"
    if not isinstance(content, str):
        return "[错误] write_file 的 content 必须是字符串"
    target = Path(path)
    if not target.is_absolute():
        target = workspace / target
    try:
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        # OpenCode 式可回滚：改写已存在文件前，把原件存入 .qingzhou-undo/
        # 记录清单（registry.json）与备份一起落盘，重启进程后 /undo 仍可用
        if target.exists():
            undo_dir = workspace / ".qingzhou-undo"
            undo_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(target.name, 20)
            backup = undo_dir / (stamp + ".bak")
            backup.write_bytes(target.read_bytes())
            _write_undo_registry(undo_dir, str(target), str(backup))
        target.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        return "[错误] 写入失败：%s" % exc
    return "已写入 %s（%d 字符）" % (target, len(content))


_last_write_backup = {"path": "", "backup": ""}


def _undo_registry_path(undo_dir: Path) -> Path:
    return undo_dir / "registry.json"


def _write_undo_registry(undo_dir: Path, target: str, backup: str):
    """快照清单落盘：[{path, backup}]，只记最近一次（/undo 单步回滚语义）。"""
    try:
        _undo_registry_path(undo_dir).write_text(
            json.dumps([{"path": target, "backup": backup}], ensure_ascii=False), encoding="utf-8")
        _last_write_backup["path"] = target
        _last_write_backup["backup"] = backup
        # 单步回滚只认最新一份快照：旧 .bak 已成孤儿，顺手清掉（便携盘空间有限）
        for old in undo_dir.glob("*.bak"):
            if str(old) != backup:
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def undo_last_write(workspace: Path) -> str:
    """/undo：把轻舟最近一次对已存在文件的改写回滚（快照恢复）。

    快照清单同时落盘（.qingzhou-undo/registry.json），重启进程后仍可回滚。
    新建文件不在回滚范围。
    """
    entry = dict(_last_write_backup)
    if not (entry.get("path") and entry.get("backup")):
        registry = _undo_registry_path(workspace / ".qingzhou-undo")
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            if isinstance(data, list) and data and isinstance(data[-1], dict):
                entry = {"path": data[-1].get("path", ""), "backup": data[-1].get("backup", "")}
        except (OSError, ValueError):
            pass
    if not (entry.get("path") and entry.get("backup")):
        return ""
    target, backup = Path(entry["path"]), Path(entry["backup"])
    try:
        if not backup.is_file():
            return ""
        target.write_bytes(backup.read_bytes())
        backup.unlink(missing_ok=True)
        # 清空清单，避免重复回滚到同一个快照
        registry = _undo_registry_path(workspace / ".qingzhou-undo")
        try:
            registry.unlink()
        except OSError:
            pass
        _last_write_backup.update(path="", backup="")
        return str(target)
    except OSError:
        return ""


def tool_list_dir(args: dict, workspace: Path) -> str:
    path = str(args.get("path") or ".")
    depth = max(1, min(3, int(args.get("depth") or 1)))
    root = Path(path)
    if not root.is_absolute():
        root = workspace / root
    if not root.is_dir():
        return "[错误] 目录不存在：%s" % root
    entries = []
    root_depth = len(root.parts)
    try:
        for cur, dirs, files in os.walk(root):
            cur_depth = len(Path(cur).parts) - root_depth
            if cur_depth >= depth:
                dirs[:] = []
            dirs.sort(key=str.lower)
            for name in dirs:
                entries.append("d %s%s%s" % (os.path.relpath(Path(cur) / name, root), os.sep, ""))
            for name in sorted(files, key=str.lower):
                fp = Path(cur) / name
                try:
                    size = fp.stat().st_size
                except OSError:
                    size = -1
                entries.append("f %s (%d B)" % (os.path.relpath(fp, root), size))
            if len(entries) > MAX_LIST_ENTRIES:
                break
    except OSError as exc:
        return "[错误] 遍历失败：%s" % exc
    if not entries:
        return "目录为空：%s" % root
    head = entries[:MAX_LIST_ENTRIES]
    tail = "\n…（共 %d 项，已截断）" % len(entries) if len(entries) > MAX_LIST_ENTRIES else ""
    return _clip("目录 %s：\n%s%s" % (root, "\n".join(head), tail))


def _decode_proc_output(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (ValueError, OSError):
        return {}


def _project_hooks(workspace: Path, cfg: dict) -> dict:
    """轻量钩子（Aider 式 lint/test 钩子 + Codex 式计划审批的配置）。

    优先级：qingzhou.json 的 hooks 键 > 工作区 qingzhou.json。值为命令行字符串：
      hooks: {"test": "python tests/smoke.py", "lint": "..."}
    test 命令非零退出时输出会作为任务收尾的检查反馈（失败才注入，对齐 Aider）。
    """
    merged = dict(_read_text_config(workspace / "qingzhou.json").get("hooks") or {})
    merged.update(cfg.get("hooks") or {})
    return {k: str(v).strip() for k, v in merged.items() if str(v).strip()}


def run_hook(command: str, workspace: Path, timeout: int = 300, stdin_data: str = None) -> tuple:
    """跑一个钩子命令，返回 (退出码, 输出)。stdin_data 非空时作为钩子的标准输入。"""
    try:
        done = subprocess.run(command, shell=True, cwd=str(workspace),
                              capture_output=True, timeout=timeout,
                              input=stdin_data.encode("utf-8") if stdin_data else None)
    except subprocess.TimeoutExpired:
        return 124, "(钩子超时被终止)"
    except OSError as exc:
        return 127, "(钩子启动失败：%s)" % exc
    text = _decode_proc_output(done.stdout)
    err = _decode_proc_output(done.stderr)
    if err.strip():
        text += ("\n[stderr]\n" if text else "") + err
    return done.returncode, text.strip()[:MAX_TOOL_RESULT_CHARS]


def tool_run_cmd(args: dict, workspace: Path, deny_patterns=None) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        return "[错误] run_cmd 缺少 command"
    # 安全闸（CC permissions.deny 精神）：危险命令一律拒绝，任何档位都不放行
    for pat, raw in (deny_patterns or []):
        if pat.search(command):
            return ("[拒绝] 命中危险命令规则（%s），已被安全闸拦截。"
                    "不要绕过或重试类似命令；如确有需要请用户手动执行。" % raw)
    try:
        timeout = int(args.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(1, min(600, timeout))
    try:
        done = subprocess.run(
            command, shell=True, cwd=str(workspace), capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        partial = _decode_proc_output(exc.output or b"") + _decode_proc_output(exc.stderr or b"")
        return _clip("[超时] 命令 %d 秒未完成被终止。已有输出：\n%s" % (timeout, partial))
    except OSError as exc:
        return "[错误] 启动命令失败：%s" % exc
    out = _decode_proc_output(done.stdout)
    err = _decode_proc_output(done.stderr)
    text = out
    if err.strip():
        text += ("\n[stderr]\n" if text else "") + err
    if done.returncode != 0:
        text = "[退出码 %d]\n%s" % (done.returncode, text)
    return _clip(text.strip() or "(命令执行成功，无输出)")


TOOLS = ("read_file", "write_file", "list_dir", "run_cmd", "final_answer")


# ---------------------------------------------------------------------------
# 权限闸：三档模式（1 审批 / 2 全自动 / 3 逐条），会话内 /mode 可切
# ---------------------------------------------------------------------------


class PermissionGate:
    MODE_NAMES = {1: "审批档（文件在工作区内免确认，命令逐条确认）",
                  2: "全自动档（不再询问，风险自担）",
                  3: "逐条档（每个动作都要确认）"}

    def __init__(self, mode: int, workspace: Path, interactive: bool):
        self.mode = mode
        self.workspace = workspace
        self.interactive = interactive
        self.allow_all_cmds = False     # 审批档里用户按过 A：本会话放行所有命令
        self.yolo_outside_ok = False    # 全自动档里用户放行过一次工作区外写
        self.plan_only = False          # /plan 计划模式：只许读，不许写/执行

    def describe(self) -> str:
        return self.MODE_NAMES[self.mode]

    def _ask(self, question: str, options: str = "Yn") -> str:
        """交互确认；非交互环境自动拒绝（无人值守请用全自动档 --yolo）。"""
        if not self.interactive or not sys.stdin.isatty():
            say("  [非交互环境，自动拒绝] %s" % question)
            return "n"
        while True:
            say(THEME.c(THEME.yellow, "  ? ") + question + THEME.c(THEME.dim, "  [%s]（回车=大写默认）" % "/".join(options)))
            ans = input(THEME.c(THEME.yellow, "  > ")).strip().lower()
            if not ans:
                return options[0].lower()
            if ans in options.lower():
                return ans
            say("  请输入 %s 之一" % "/".join(options))

    def check(self, tool: str, args: dict) -> tuple:
        """返回 (allowed, reason)。reason 仅在拒绝时给模型看。"""
        if tool == "final_answer":
            return True, ""

        path_arg = str(args.get("path", ""))
        outside = bool(path_arg) and not in_workspace(path_arg, self.workspace)

        # 计划模式：只许读，写/执行一律拦（对齐 Claude Code plan 档语义）
        if self.plan_only and tool in ("write_file", "run_cmd"):
            return False, ("[计划模式开启中] 当前为只读规划：请先用 read_file/list_dir 调研，"
                           "然后用 final_answer 给出分步实施计划（不要执行改动）。用户确认后可用 /plan off 退出。")

        if tool in ("read_file", "list_dir"):
            # 只读操作风险低：审批/全自动档放行（含工作区外读），逐条档确认
            if self.mode == 3:
                ok = self._ask("读取 %s（工作区外）？" % path_arg if outside else "读取 %s？" % path_arg)
                return ok == "y", "[用户拒绝了该读取操作]"
            return True, ""

        if tool == "write_file":
            if self.mode == 2:
                if outside and not self.yolo_outside_ok:
                    ok = self._ask("全自动档：本次会话放行向工作区外写入？（%s）" % path_arg, "yn")
                    if ok != "y":
                        return False, "[用户拒绝向工作区外写入]"
                    self.yolo_outside_ok = True
                return True, ""
            label = "写入 %s" % ("工作区外 " + path_arg if outside else path_arg)
            ok = self._ask(label + "？")
            return ok == "y", "[用户拒绝了该写入操作] 换别的方式，或用 final_answer 说明。"

        if tool == "run_cmd":
            if self.mode == 2:
                return True, ""
            if self.mode == 1 and self.allow_all_cmds:
                return True, ""
            ans = self._ask("执行命令：%s" % str(args.get("command", ""))[:120], "Yna")
            if ans == "y":
                return True, ""
            if ans == "a":
                self.allow_all_cmds = True
                say("  本会话放行后续所有命令。")
                return True, ""
            return False, "[用户拒绝了该命令] 不要擅自重试同一命令；换方式或用 final_answer 说明。"

        return False, "[错误] 未知工具"


# ---------------------------------------------------------------------------
# 工具调用协议：```qingzhou 代码块包 JSON（文本协议，兼容任何端点）
# ---------------------------------------------------------------------------

TOOL_BLOCK_RE = re.compile(r"```qingzhou\s*\n(.*?)```", re.DOTALL)
TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

SYSTEM_PROMPT = """你是轻舟（qingzhou），一个运行在用户终端里的极简编码助手。你通过工具调用来读写文件与执行命令。

## 工具调用协议（必须严格遵守）
每次回复必须包含恰好一个工具调用，写在 ```qingzhou 围栏代码块内，内容为单个 JSON 对象：

```qingzhou
{"tool": "工具名", "args": {"参数名": "值"}}
```

可用工具：
1. read_file    {"path": "路径", "start": 1, "end": 100}     读文本文件（start/end 可省略，行号从 1 开始）
2. write_file   {"path": "路径", "content": "完整文件内容"}    写文件（整文件覆盖，UTF-8）
3. list_dir     {"path": ".", "depth": 1}                    列目录（depth 1-3）
4. run_cmd      {"command": "命令", "timeout": 120}           执行 shell 命令（timeout 秒，默认 120，最大 600）
5. final_answer {"answer": "给用户的最终答复"}                 任务完成或无法继续时调用，结束本轮

规则：
- 每轮只调用一个工具；工具结果会以 [工具结果] 消息在下一轮给出。
- 可以在代码块之外写一两句简短说明，但工具调用不能省略。
- 路径尽量用相对当前目录的路径；当前工作目录就是用户的工作区。
- 操作失败时同一思路最多重试 2 次，然后换思路；确实做不成就用 final_answer 如实说明。
- 不要执行与任务无关或明显危险的操作（删库、改系统配置等）。用户会审阅你的每个动作。
- 计划模式下（若收到相关系统提示）只调研不改动，用 final_answer 交分步计划。
"""


def parse_tool_call(reply: str):
    """从模型回复中解析最后一个 ```qingzhou 块；返回 (tool, args) 或 None。

    兼容两种写法（真机联调实测 GLM 会用第二种）：
      {"tool": "write_file", "args": {"path": ...}}     标准嵌套
      {"tool": "write_file", "path": ..., "content": ...} 顶层平铺
    """
    blocks = TOOL_BLOCK_RE.findall(reply or "")
    if not blocks:
        return None
    raw = blocks[-1].strip()
    raw = TRAILING_COMMA_RE.sub(r"\1", raw)
    try:
        call = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(call, dict):
        return None
    tool = call.get("tool")
    args = call.get("args")
    if not isinstance(args, dict):
        args = {}
    if not args:
        # 顶层平铺兜底：除 tool/args 外的键全部收进 args
        args = {k: v for k, v in call.items() if k not in ("tool", "args")}
    if tool not in TOOLS:
        return None
    return tool, args


def execute_tool(tool: str, args: dict, workspace: Path, deny_patterns=None) -> str:
    """执行工具；任何未预期异常都转成错误文本回喂模型，绝不裸崩会话。"""
    try:
        if tool == "read_file":
            return tool_read_file(args, workspace)
        if tool == "write_file":
            return tool_write_file(args, workspace)
        if tool == "list_dir":
            return tool_list_dir(args, workspace)
        if tool == "run_cmd":
            return tool_run_cmd(args, workspace, deny_patterns=deny_patterns)
        return "[错误] 未知工具 %s" % tool
    except QingzhouError:
        raise
    except Exception as exc:  # 参数类型错误、编码意外等——喂回模型让它自行修正
        return "[错误] 工具执行异常（%s: %s），请检查参数类型后重试" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 会话持久化：sessions/*.jsonl，逐事件落盘，崩溃不丢
# ---------------------------------------------------------------------------


def slugify(text: str, limit: int = 24) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip())[:limit].strip("-")
    return slug or "session"


class Session:
    """一条会话 = 一个 jsonl 文件。msg 事件重建消息历史，action 事件只作审计。"""

    def __init__(self, path: Path):
        self.path = path
        self.handle = path.open("a", encoding="utf-8", newline="\n")

    @classmethod
    def new(cls, first_user_msg: str) -> "Session":
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "%s-%s.jsonl" % (stamp, slugify(first_user_msg))
        if (SESSIONS_DIR / name).exists():
            # 同一秒同名（双开/立即重试）：加 pid 后缀，避免两路会话写进同一文件
            name = "%s-%s-%d.jsonl" % (stamp, slugify(first_user_msg), os.getpid())
        session = cls(SESSIONS_DIR / name)
        session._write({"t": "meta", "version": QZ_VERSION, "cwd": os.getcwd(),
                        "created": datetime.now().isoformat(timespec="seconds")})
        return session

    @classmethod
    def branched(cls, summary_seed_msgs, from_name: str) -> "Session":
        """/compact 时新开文件，旧文件保留全量历史。"""
        session = cls.new("[compact] " + (summary_seed_msgs[-1].get("content", "") if summary_seed_msgs else ""))
        session._write({"t": "branched_from", "from": from_name})
        for m in summary_seed_msgs:
            session.append_msg(m)
        return session

    def _write(self, event: dict):
        self.handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.handle.flush()

    def append_msg(self, message: dict):
        self._write({"t": "msg", "role": message.get("role"), "content": message.get("content", "")})

    def append_action(self, tool: str, args: dict, result: str, allowed: bool):
        self._write({"t": "action", "tool": tool, "args": args, "allowed": allowed,
                     "result": result[:500], "at": datetime.now().strftime("%m-%d %H:%M:%S")})

    def close(self):
        try:
            self.handle.close()
        except OSError:
            pass


def list_sessions(limit: int = 10):
    if not SESSIONS_DIR.is_dir():
        return []
    files = [p for p in SESSIONS_DIR.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def load_session_messages(path: Path):
    """从 jsonl 重建消息列表（只取 msg 事件，保持顺序）。"""
    messages = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("t") == "msg" and event.get("role") in ("user", "assistant", "system"):
                messages.append({"role": event["role"], "content": str(event.get("content", ""))})
    except OSError as exc:
        raise QingzhouError("读取会话失败：%s" % exc)
    return messages


def pick_session_interactive(prefer_latest: bool = False):
    files = list_sessions()
    if not files:
        say("还没有任何会话记录（sessions/ 为空）。")
        return None
    if prefer_latest:
        return files[0]
    say("最近的会话：")
    for i, p in enumerate(files, 1):
        first_user = ""
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                event = json.loads(line)
                if event.get("t") == "msg" and event.get("role") == "user":
                    first_user = str(event.get("content", ""))[:40]
                    break
        except (ValueError, OSError):
            pass
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        say("  %d. [%s] %s ｜ %s" % (i, mtime, p.stem, first_user))
    raw = input("选择序号 [1]: ").strip() or "1"
    try:
        idx = int(raw)
    except ValueError:
        return None
    if 1 <= idx <= len(files):
        return files[idx - 1]
    return None


# ---------------------------------------------------------------------------
# Agent 循环
# ---------------------------------------------------------------------------


class Agent:
    def __init__(self, cfg: dict, gate: PermissionGate, session: Session, max_steps: int = 30,
                 no_stream: bool = False, quiet_stream: bool = False, hooks: dict = None, runlog=None,
                 deny_patterns=None, message_queue=None):
        self.llm = LLMClient(cfg, no_stream=no_stream, runlog=runlog)
        self.gate = gate
        self.session = session
        self.max_steps = max_steps
        self.quiet_stream = quiet_stream
        self.hooks = hooks or {}
        self.dial = str(cfg.get("dial") or "medium").lower()  # low/medium/high/ultra（Amp 式）
        self.runlog = runlog
        self.deny_patterns = deny_patterns or []
        self.message_queue = message_queue  # 交互模式的排队队列；None=单发/战役不启用

    def _on_delta(self, piece: str):
        """流式增量输出（CC nonBlockingStdout 思想的轻量版）。

        spinner 在首个增量前已由 notify_content 熄灭，此处不会再有回擦与之交叠。
        聚合到整行才刷出（每行一次 write），终端重绘次数降一个量级，
        且输出永不与 spinner 的擦除交叠（截断/漂移/花屏三病根同除）。
        """
        if self.quiet_stream:
            return
        self._stream_buf = getattr(self, "_stream_buf", "") + piece
        if "\n" in self._stream_buf:
            lines = self._stream_buf.split("\n")
            complete, self._stream_buf = lines[:-1], lines[-1]
            try:
                sys.stdout.write("\n".join(complete) + "\n")
                sys.stdout.flush()
            except OSError:
                pass

    def _flush_stream_tail(self):
        """一轮回复结束时刷出残留在缓冲的最后一行。"""
        buf = getattr(self, "_stream_buf", "")
        if buf:
            try:
                sys.stdout.write(buf)
                sys.stdout.flush()
            except OSError:
                pass
            self._stream_buf = ""

    def run(self, messages, user_prompt: str) -> str:
        """跑一轮 agent 循环，返回 final_answer 文本（未完成返回空串）。messages 会被就地更新。"""
        if user_prompt is not None:
            messages.append({"role": "user", "content": user_prompt})
            self.session.append_msg(messages[-1])

        no_tool_streak = 0
        hook_feedback_count = 0  # test 钩子失败→反馈修复的轮数，防"修不好"死循环
        for step in range(1, self.max_steps + 1):
            say("")
            say(THEME.c(THEME.dim, "~^~ 第 %d/%d 步 " % (step, self.max_steps) + "~^~" * 10))
            # CC 式消息排队采集：干完本轮前用户打的字入队（队满提示）
            if getattr(self, "message_queue", None) is not None:
                qbuf, done = getattr(self, "_qbuf", ""), False
                qbuf, done = _poll_keyboard(qbuf)
                if done and qbuf.strip():
                    if len(self.message_queue) < 10:
                        self.message_queue.append(qbuf.strip())
                        say(THEME.c(THEME.dim, "  ▸ 已排队（%d）：" % len(self.message_queue)) + qbuf.strip()[:50])
                    else:
                        say(THEME.c(THEME.yellow, "  ▸ 排队已满（10），本条丢弃：") + qbuf.strip()[:50])
                    qbuf = ""
                self._qbuf = qbuf
            spinner = Spinner(pretty_model(self.llm.model) + (("(%s) " % self.llm.effort if self.llm.effort else "") + "思考中"))
            with spinner:
                reply = self.llm.chat(
                    messages,
                    on_delta=None if self.quiet_stream
                    else (lambda piece: (spinner.notify_content(), self._on_delta(piece))[1]))
            if not self.quiet_stream:
                self._flush_stream_tail()
                say("")  # 流式打印后换行
            messages.append({"role": "assistant", "content": reply})
            self.session.append_msg(messages[-1])

            call = parse_tool_call(reply)
            if call is None:
                no_tool_streak += 1
                if no_tool_streak >= 3:
                    say("  [轻舟] 连续 %d 轮没有按协议给出工具调用，本轮终止。" % no_tool_streak)
                    return ""
                messages.append({"role": "user", "content":
                    "[系统提示] 你的上一条回复没有包含 ```qingzhou 工具调用代码块。"
                    "请严格按协议：每条回复恰好一个 ```qingzhou 代码块（JSON），"
                    "任务未完成就调用下一个工具，已完成就调用 final_answer。"})
                self.session.append_msg(messages[-1])
                continue

            no_tool_streak = 0
            tool, args = call
            if tool == "final_answer":
                answer = str(args.get("answer", "")).strip() or reply.strip()
                # Aider 式 test 钩子：收尾前跑一次测试命令，失败才把输出注入回模型
                if self.hooks.get("test"):
                    if self.runlog:
                        self.runlog.write("INFO", "hook_test_start", cmd=self.hooks["test"])
                    say("  " + THEME.c(THEME.yellow, THEME.dot + " 钩子") + THEME.c(THEME.dim, " 测试命令：" + self.hooks["test"]))
                    code, output = run_hook(self.hooks["test"], self.gate.workspace)
                    if self.runlog:
                        self.runlog.write("INFO" if code == 0 else "WARN", "hook_test_done",
                                          exit_code=code, output=(output or "")[:200])
                    if code == 0:
                        say("  [钩子] 测试通过（输出不注入，节省上下文）。")
                    else:
                        say("  [钩子] 测试失败（退出码 %d）" % code)
                        hook_feedback_count += 1
                        if hook_feedback_count >= 3:
                            say("  [钩子] 已反馈修复 %d 轮仍失败，带病收尾（钩子输出见会话记录）。" % hook_feedback_count)
                            say("")
                            say(THEME.c(THEME.green, THEME.dot + " ") + THEME.c(THEME.bold, pretty_model(self.llm.model)) + THEME.c(THEME.yellow, "  ⚠ 测试钩子未通过，请人工复核"))
                            for ln in answer.splitlines():
                                say(THEME.c(THEME.dim, "  ") + ln)
                            return answer
                        say("  [钩子] 反馈给模型修复（第 %d 次）。" % hook_feedback_count)
                        messages.append({"role": "user", "content":
                            "[测试钩子结果] 你宣布任务完成前的验证命令 `%s` 失败（退出码 %d）：\n%s\n"
                            "请修复后再次 final_answer。" % (self.hooks["test"], code, output or "(无输出)")})
                        self.session.append_msg(messages[-1])
                        continue
                say("")
                say(THEME.c(THEME.green, THEME.dot + " ") + THEME.c(THEME.bold, pretty_model(self.llm.model)))
                for ln in answer.splitlines():
                    say(THEME.c(THEME.dim, "  ") + ln)
                return answer

            allowed, deny_note = self.gate.check(tool, args)
            # CC 式 PreToolUse 用户钩子：非零退出=拦截，stdout=理由
            if allowed and self.hooks.get("pre_tool"):
                hook_in = json.dumps({"tool": tool, "args": args,
                                      "cwd": str(self.gate.workspace)}, ensure_ascii=False)
                hcode, hout = run_hook(self.hooks["pre_tool"], self.gate.workspace,
                                       timeout=60, stdin_data=hook_in)
                if self.runlog:
                    self.runlog.write("INFO" if hcode == 0 else "WARN", "pre_tool_hook",
                                      tool=tool, exit_code=hcode)
                if hcode != 0:
                    allowed, deny_note = False, "[PreToolUse 钩子拦截] %s" % (hout or "(无理由)")
            if allowed:
                if self.runlog:
                    # 日志脱敏：write_file 只记路径与内容长度，不记正文（防密钥/敏感内容入日志）
                    if tool == "write_file" and isinstance(args.get("content"), str):
                        log_args = dict(args)
                        log_args["content"] = "(%d 字符，正文不入日志)" % len(args["content"])
                    else:
                        log_args = args
                    self.runlog.write("INFO", "tool_exec", tool=tool,
                                      args=json.dumps(log_args, ensure_ascii=False)[:300])
                say("")
                say("  " + THEME.c(THEME.cyan, THEME.dot + " ") + THEME.c(THEME.cyan, tool) + THEME.c(THEME.dim, " " + json.dumps(args, ensure_ascii=False)[:120]))
                result = execute_tool(tool, args, self.gate.workspace, deny_patterns=self.deny_patterns)
                if self.runlog and (result.startswith("[错误]") or result.startswith("[超时]")):
                    self.runlog.write("WARN", "tool_error", tool=tool, result=result[:300])
            else:
                result = deny_note
                if self.runlog:
                    self.runlog.write("WARN", "tool_denied", tool=tool, reason=deny_note[:200])
            self.session.append_action(tool, args, result, allowed)
            first_line = (result.splitlines() or [""])[0][:120]
            if result.startswith("[错误]"):
                colored = THEME.c(THEME.red, first_line)
            elif result.startswith("[超时]"):
                colored = THEME.c(THEME.yellow, first_line)
            elif result.startswith("["):
                colored = THEME.c(THEME.yellow, first_line)
            else:
                colored = THEME.c(THEME.dim, first_line)
            say(THEME.c(THEME.dim, THEME.indent) + colored)

            messages.append({"role": "user", "content": "[工具结果] %s" % result})
            self.session.append_msg(messages[-1])

        say("  [轻舟] 已达最大步数（%d）仍未完成。可以继续追问，或用 /compact 释放上下文。" % self.max_steps)
        return ""


def build_system_prompt(workspace: Path) -> str:
    system = SYSTEM_PROMPT + "\n\n当前工作目录（你的工作区）：" + str(workspace)
    custom = collect_project_instructions(workspace)
    if custom:
        system += "\n\n## 项目指令（来自工作区的 qingzhou.md / AGENTS.md，优先级高于你的默认习惯）\n" + custom
    return system


def _resolve_import(text: str, base_dir: Path, depth: int = 0, seen=None) -> str:
    """@path 引用展开（Claude Code 式 @import）：相对所在文件解析，最多 3 跳。"""
    if seen is None:
        seen = set()
    if depth > 3:
        return text
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^@(.+)$", stripped)
        if m and not stripped.startswith("`@"):
            target_raw = m.group(1).strip()
            target = Path(target_raw)
            if not target.is_absolute():
                target = base_dir / target
            key = _normcase(target)
            if key in seen:
                out_lines.append("(循环引用已跳过：%s)" % target_raw)
                continue
            try:
                if target.is_file() and target.stat().st_size <= 1024 * 512:
                    seen.add(key)
                    imported = _read_text_smart(target)
                    out_lines.append(_resolve_import(imported, target.parent, depth + 1, seen))
                    continue
            except OSError:
                pass
            out_lines.append("(引用未找到：%s)" % target_raw)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _read_instruction_file(path: Path) -> str:
    """读一个指令文件；超过 4MiB 视为异常跳过（对齐 Claude Code 上限）。"""
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return ""
        return _read_text_smart(path)
    except OSError:
        return ""


def collect_project_instructions(workspace: Path) -> str:
    """层级收集项目指令（对齐 Claude Code/AGENTS.md 的目录向上遍历）：

    1. 用户级 ~/.qingzhou/qingzhou.md（个人跨项目偏好，最先注入）
    2. 从工作区向上遍历到文件系统根：每级 qingzhou.md / AGENTS.md / AGENTS.local.md
       （拼接顺序：根 → 工作区，越靠近启动目录的越晚、优先级越高）
    3. 支持 @路径 引用展开（最多 3 跳）
    """
    parts = []
    user_level = Path.home() / ".qingzhou" / "qingzhou.md"
    if user_level.is_file():
        text = _read_instruction_file(user_level)
        if text.strip():
            parts.append(("用户级指令（~/.qingzhou/qingzhou.md）", _resolve_import(text, user_level.parent)))

    ancestors = [p for p in workspace.resolve().parents]
    chain = list(reversed(ancestors)) + [workspace]
    for level_dir in chain:
        for name in ("qingzhou.md", "AGENTS.md", "AGENTS.local.md"):
            path = level_dir / name
            if path.is_file():
                text = _read_instruction_file(path)
                if text.strip():
                    parts.append(("项目指令（%s）" % path, _resolve_import(text, level_dir)))
    if not parts:
        return ""
    blocks = []
    for label, text in parts:
        blocks.append("### %s\n%s" % (label, text.strip()))
    return "\n\n".join(blocks)[:20000]


# ---------------------------------------------------------------------------
# 交互式主循环（/命令）
# ---------------------------------------------------------------------------

HELP_TEXT = """会话内命令：
  /help              显示本帮助
  /mode [1|2|3]      查看或切换权限档（1 审批 / 2 全自动 / 3 逐条）
  /plan [on|off]     计划模式开关（只读规划不动文件，OpenCode/Claude 式，Tab 精神）
  /effort [档位]     查看或切换思考档（none/low/medium/high/max；带 save 存入配置）
  /model <名称>      临时切换模型（仅本会话）
  /new               开新会话
  /resume            切换到历史会话
  /compact           把旧历史压缩成摘要（上下文太长时用）
  /tokens            估算当前上下文占用（字符数/token 估算，Aider 式）
  /copy-context      把当前上下文导出成 markdown 文件（调试复现用）
  /undo              撤销轻舟最近一次 write_file（本地快照回滚，OpenCode 式）
  /tasks             查看/创建工作区的 TASKS.md 任务账本
  /handoff           查看/创建工作区的 HANDOFF.md 交接文档
  /status            查看当前配置与状态
  /exit 或 Ctrl+C    退出（会话已实时落盘，随时 /resume 回来）"""

TASKS_TEMPLATE = """# 任务账本 · {name}

> 按优先级领活：P0=阻断级最先干，P4=远期想法。
> 三态：[ ] 待执行 ／ [~] 执行中 ／ [x] 已完成。
> 回写规则：完成打 [x] 后保留原位（留档）；经汇报呈现且用户无异议，移入底部归档区。

## 待办

- [ ] P1 示例任务：一句话说清做什么、做到什么程度算完成

## 归档区（只进不改，历史备查）

## 流水（每次收工追加一行）

- {stamp} ｜ 账本创建
"""

HANDOFF_TEMPLATE = """# HANDOFF — {name}

- **更新**: {stamp} ｜ **读者**: 接手的人或下一个 Agent
- **前置阅读**: TASKS.md

## 0. 一句话
（一段话讲清这套东西是什么、怎么运转。）

## 1. 组件与路径总表
| 组件 | 路径 | 说明 |
| --- | --- | --- |

## 2. 快速上手（标准流程，按顺序）
1.

## 3. 动/不动清单（最重要的节）
### 允许动（项 | 位置 | 方式）
### 禁止动（项 | 位置 | 原因）

## 4. 故障手册（按症状查）
| 症状 | 原因 | 处理 |
| --- | --- | --- |

## 5. 当前进度与下一步
（接任务账本的流水：现在干到哪、下一个该干什么。）

## 6. 存档与回滚
"""


def ensure_tasks_file(workspace: Path) -> Path:
    path = workspace / "TASKS.md"
    if not path.exists():
        path.write_text(TASKS_TEMPLATE.format(name=workspace.name, stamp=datetime.now().strftime("%Y-%m-%d %H:%M")),
                        encoding="utf-8", newline="\n")
        say("  已创建任务账本模板 → %s" % path)
    return path


def ensure_handoff_file(workspace: Path) -> Path:
    path = workspace / "HANDOFF.md"
    if not path.exists():
        path.write_text(HANDOFF_TEMPLATE.format(name=workspace.name, stamp=datetime.now().strftime("%Y-%m-%d %H:%M")),
                        encoding="utf-8", newline="\n")
        say("  已创建交接文档模板 → %s" % path)
    return path


def compact_messages(llm: LLMClient, messages) -> list:
    """/compact：保留 system + 最近 4 条，其余历史压成摘要，返回新消息列表。"""
    if len(messages) <= 6:
        return messages
    system, rest = messages[0], messages[1:]
    if len(rest) <= 4:
        return messages
    old, recent = rest[:-4], rest[-4:]
    prompt = ("请把下面这段对话历史压缩成一份简洁摘要（保留：任务目标、已完成的动作与产出文件、"
              "关键结论、未完成事项），直接输出摘要正文，不要客套：\n\n" +
              "\n".join("[%s] %s" % (m.get("role"), str(m.get("content", ""))[:1500]) for m in old))
    try:
        summary = llm.chat([{"role": "user", "content": prompt}], on_delta=None)
    except QingzhouError as exc:
        say("  压缩失败：%s" % exc)
        return messages
    except Exception as exc:  # 压缩属增值操作：任何意外都不打断交互循环
        say("  压缩异常（%s），已保留原历史。" % exc)
        return messages
    new_msgs = [system,
                {"role": "user", "content": "[此前对话的摘要，细节已省略]\n" + summary.strip()}]
    new_msgs.extend(recent)
    return new_msgs


class FixedBottomUI:
    """DECSTBM 固定底部 UI（逆向 Claude Code 的 CLAUDE_CODE_DECSTBM 模式落地）。

    原理：终端的"滚动区域"（DECSTBM）把屏幕划成两段——
      · 行 1 .. N-2：对话区，输出自动滚动
      · 最后 2 行：状态栏 + 输入框，**完全固定不滚动**
    这正是 CC"输入框永远钉在下面"的机制（比 alt-screen 帧渲染轻一个量级，
    纯标准转义序列，Windows Terminal/Win10+ conhost 均支持）。

    支持检测：需要 ANSI + tty + 终端行数 ≥ 10；任一不满足则降级为普通滚动式。
    """

    def __init__(self):
        self.on = THEME.on and sys.stdout.isatty() and not os.environ.get("QINGZHOU_NO_TUI")
        self.rows = self.cols = 0
        if self.on:
            self.rows, self.cols = self._term_size()
            if self.rows < 10:
                self.on = False

    @staticmethod
    def _term_size():
        try:
            size = os.get_terminal_size()
            return size.lines, size.columns
        except OSError:
            return 0, 0

    def _write(self, seq: str):
        try:
            sys.stdout.write(seq)
            sys.stdout.flush()
        except OSError:
            self.on = False

    def engage(self):
        """进入固定布局：滚动区域设为 1..rows-2，底部两行留给状态栏+输入。"""
        if not self.on:
            return
        self.rows, self.cols = self._term_size()
        if self.rows < 10:
            self.on = False
            return
        scroll_end = self.rows - 2
        # DECSTBM：\x1b[<top>;<bottom>r；之后光标定位与输出都限制在区域内
        self._write("\x1b[1;%dr" % scroll_end)
        # 光标跳到区域内首行，开始正常输出
        self._write("\x1b[%d;1H" % scroll_end)

    def release(self):
        """退出前恢复全屏滚动区域（\x1b[r），防止终端残留限制。"""
        if self.on:
            self._write("\x1b[r")

    def draw_fixed_rows(self, status: str, prompt_char: str = "› "):
        """在底部两行画状态栏与输入提示（区域外不受滚动影响）。"""
        if not self.on:
            return
        self.rows, self.cols = self._term_size()
        if self.rows < 10:
            return
        status1 = _clip_display(status, max(1, self.cols - 2))
        # 状态栏（倒数第2行）+ 输入提示（倒数第1行）
        self._write("\x1b[%d;1H\x1b[2K%s" % (self.rows - 1, status1))
        self._write("\x1b[%d;1H\x1b[2K%s" % (self.rows, prompt_char))
        # 光标放回输入行提示符之后
        self._write("\x1b[%d;%dH" % (self.rows, _disp_width(prompt_char) + 1))


def _clip_display(text: str, max_width: int) -> str:
    """按显示宽度截断：ANSI 转义序列不占宽，且保证不会被截在序列中间。"""
    out = []
    w = 0
    for seg in re.split(r"(\x1b\[[0-9;]*[A-Za-z])", text):
        if seg.startswith("\x1b["):
            out.append(seg)
            continue
        for ch in seg:
            cw = _disp_width(ch)
            if w + cw > max_width:
                return "".join(out) + THEME.reset
            out.append(ch)
            w += cw
    return "".join(out)


def interactive_mode(cfg: dict, gate: PermissionGate, max_steps: int, no_stream: bool, resume_latest: bool, runlog=None, config_path=None):
    workspace = Path(os.getcwd()).resolve()
    system_prompt = build_system_prompt(workspace)
    messages = [{"role": "system", "content": system_prompt}]
    session = None
    llm = LLMClient(cfg, no_stream=no_stream, runlog=runlog)

    if resume_latest:
        path = pick_session_interactive(prefer_latest=True)
        if path:
            old_msgs = load_session_messages(path)
            # 旧会话的 system 换成新的（工作区可能变了），其余保留
            old_msgs = [m for m in old_msgs if m.get("role") != "system"]
            if old_msgs:
                messages.extend(old_msgs)
                session = Session.branched(old_msgs, path.name)
                say("已恢复会话：%s（%d 条消息）" % (path.name, len(old_msgs)))

    banner(cfg)
    say(HELP_TEXT)
    say("")

    def status_line():
        """CC 式底部状态栏（/effort · /mode 快捷提示单行化）。"""
        eff = llm.effort or "auto"
        gate_txt = {1: "manual", 2: "yolo", 3: "strict"}[gate.mode]
        plan = " · ⏸ plan" if gate.plan_only else ""
        return THEME.c(THEME.dim, "  ⛵ %s · /effort · %s mode%s · /help for commands" % (eff, gate_txt, plan))

    # DECSTBM 固定底栏：输入框钉在终端最后两行，对话输出只在上方区域滚动
    ui = FixedBottomUI()
    ui.engage()
    # CC 式消息队列：AI 干活时用户输入先排队，本轮结束后自动续发（queued messages）
    message_queue = []
    while True:
        if message_queue:
            # 队列有货：直接取队头发送，不再等输入
            user_in = message_queue.pop(0)
            say(THEME.c(THEME.dim, "  ▸ 续发排队消息") + THEME.c(THEME.cyan, " › " + user_in[:60]))
        else:
            try:
                if ui.on:
                    ui.draw_fixed_rows(status_line(), THEME.c(THEME.cyan, "› "))
                else:
                    say(status_line())
                user_in = input(THEME.c(THEME.cyan, "› ")).strip()
            except (EOFError, KeyboardInterrupt):
                say("")
                break
        if not user_in:
            continue
        if user_in.startswith("/"):
            cmd, _, rest = user_in.partition(" ")
            cmd = cmd.lower()
            if cmd in ("/exit", "/quit", "/q"):
                break
            if cmd == "/help":
                say(HELP_TEXT)
            elif cmd == "/mode":
                if rest.strip() in ("1", "2", "3"):
                    gate.mode = int(rest.strip())
                    say("  权限档已切换：%s" % gate.describe())
                    if gate.mode == 2:
                        say("  ⚠ 全自动档：轻舟将不再询问直接执行命令与写文件。")
                else:
                    say("  当前：%s%s" % (gate.describe(), " ｜计划模式开启中" if gate.plan_only else ""))
            elif cmd == "/plan":
                arg = rest.strip().lower()
                if arg in ("on", "开", ""):
                    gate.plan_only = True
                    say("  ⏸ 计划模式开启：只读调研、产出计划，不动文件不跑命令。完成后 /plan off 回到执行。")
                elif arg in ("off", "关"):
                    gate.plan_only = False
                    say("  ▶ 已退出计划模式，恢复执行。")
                else:
                    say("  用法：/plan on 或 /plan off（当前：%s）" % ("on" if gate.plan_only else "off"))
            elif cmd == "/effort":
                arg = rest.strip().lower()
                if arg in ("", "查看"):
                    cur = llm.effort or "auto（服务端默认）"
                    say("  当前思考档：%s ｜ 可选 %s" % (cur, "/".join(VALID_EFFORTS)))
                elif arg.split()[0] in VALID_EFFORTS:
                    parts = arg.split()
                    llm.effort = parts[0]
                    cfg["reasoning_effort"] = parts[0]  # 写回 cfg：下一条消息新建的 Agent 才能继承（仅本会话生效的关键）
                    if "save" in parts[1:]:
                        if config_path:
                            save_config(config_path, cfg)
                        say("  思考档已切换并保存为默认：%s" % llm.effort)
                    else:
                        say("  思考档已切换（仅本会话）：%s ｜ 加 save 存为默认" % llm.effort)
                else:
                    say("  用法：/effort [档位] [save] ｜ 可选 %s" % "/".join(VALID_EFFORTS))
            elif cmd == "/tokens":
                total = sum(len(str(m.get("content", ""))) for m in messages)
                say("  上下文估算：%d 条消息，约 %d 字符 ≈ %d tokens（按 4 字符/token 粗估）"
                    % (len(messages), total, total // 4))
                if total > HISTORY_SOFT_LIMIT_CHARS:
                    say("  ⚠ 已超过软上限（%d 字符），建议 /compact。" % HISTORY_SOFT_LIMIT_CHARS)
            elif cmd == "/copy-context":
                if session is None:
                    say("  当前会话还没有内容。")
                else:
                    out = workspace / ("context-%s.md" % datetime.now().strftime("%m%d-%H%M%S"))
                    lines = ["# 轻舟上下文导出（%s）\n" % datetime.now().strftime("%Y-%m-%d %H:%M")]
                    for m in messages:
                        lines.append("## [%s]\n%s\n" % (m.get("role"), str(m.get("content", ""))[:6000]))
                    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
                    say("  已导出 → %s（可贴到外部工具复现问题）" % out)
            elif cmd == "/undo":
                undone = undo_last_write(workspace)
                if undone:
                    say("  已回滚：%s\n  （恢复到轻舟改写前的内容）" % undone)
                else:
                    say("  没有可回滚的轻舟写入记录。")
            elif cmd == "/model":
                if rest.strip():
                    llm.model = rest.strip()
                    cfg["model"] = llm.model
                    say("  本会话模型已切换为 %s" % llm.model)
                else:
                    say("  当前模型：%s" % llm.model)
            elif cmd == "/new":
                if session:
                    session.close()
                messages = [{"role": "system", "content": system_prompt}]
                session = None
                say("  已开新会话（发送第一条消息时落盘）。")
            elif cmd == "/resume":
                path = pick_session_interactive()
                if path:
                    if session:
                        session.close()
                    old_msgs = [m for m in load_session_messages(path) if m.get("role") != "system"]
                    messages = [{"role": "system", "content": system_prompt}] + old_msgs
                    session = Session.branched(old_msgs, path.name)
                    say("  已恢复：%s（%d 条消息）。继续输入即可接着聊。" % (path.name, len(old_msgs)))
            elif cmd == "/compact":
                if session is None:
                    say("  当前会话还没有内容。")
                    continue
                new_msgs = compact_messages(llm, messages)
                if new_msgs is not messages:
                    old_count = len(messages)
                    session.close()
                    session = Session.branched(new_msgs[1:], session.path.name)
                    messages = new_msgs
                    say("  已压缩：%d 条消息 → %d 条（旧文件保留全量历史）。" % (old_count, len(new_msgs)))
            elif cmd == "/tasks":
                say("  %s" % ensure_tasks_file(workspace).read_text(encoding="utf-8", errors="replace"))
            elif cmd == "/handoff":
                say("  交接文档：%s" % ensure_handoff_file(workspace))
            elif cmd == "/status":
                say("  模型: %s @ %s ｜ 思考档: %s" % (llm.model, llm.base_url, llm.effort or "(服务端默认)"))
                say("  权限: %s" % gate.describe())
                say("  工作区: %s" % workspace)
                say("  会话: %s" % (session.path.name if session else "(尚未落盘)"))
            else:
                say("  未知命令 %s（/help 查看帮助）" % cmd)
            continue

        # 正常对话：第一条消息时创建会话文件
        if session is None:
            session = Session.new(user_in)
            session._write({"t": "meta", "mode": gate.mode})

        # CC 式输入区：上分隔线 → 青色 › 回显用户输入 → 下分隔线（两条 888888 灰线
        # 把输入夹在中间，对话内容长在下方，视觉上"钉"住输入区不再下飘）
        say(THEME.c(THEME.dim, "─" * 64))
        say(THEME.c(THEME.cyan, "› ") + user_in)
        say(THEME.c(THEME.dim, "─" * 64))

        try:
            agent = Agent(cfg, gate, session, max_steps=max_steps, no_stream=no_stream,
                          hooks=_project_hooks(workspace, cfg), runlog=runlog,
                          deny_patterns=compile_deny_patterns(cfg, workspace),
                          message_queue=message_queue)
            if runlog:
                runlog.write("INFO", "user_turn", prompt_len=len(user_in))
            agent.run(messages, user_in)
        except QingzhouError as exc:
            say("  [出错] %s" % exc)
        except KeyboardInterrupt:
            say("\n  （本轮被打断。部分输出与已执行的工具动作已保留在会话里，下轮可直接引用）")

        # ── CC auto-compact（逆向 autoCompactEnabled 落地）：回合结束检查上下文水位 ──
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        ratio = total_chars / HISTORY_SOFT_LIMIT_CHARS
        auto_compact_on = cfg.get("auto_compact", True)
        if auto_compact_on and ratio >= AUTO_COMPACT_THRESHOLD and len(messages) > 6:
            say(THEME.c(THEME.yellow, "  ⚠ 上下文已用 %d%%，自动压缩历史…" % int(ratio * 100)))
            new_msgs = compact_messages(llm, messages)
            if new_msgs is not messages:
                session.close()
                session = Session.branched(new_msgs[1:], session.path.name)
                messages = new_msgs
                say(THEME.c(THEME.dim, "  已自动压缩 → %d 条消息（原文在旧会话文件里）。" % len(messages)))
        elif ratio >= 0.6:
            say(THEME.c(THEME.dim, "  ⚠ 上下文已用 %d%%（剩 %d%%），/compact 可提前压缩；配置 auto_compact=false 可关自动压缩。"
                % (int(ratio * 100), int((1 - ratio) * 100))))
        say("")

    # 主循环外：恢复终端滚动区域，再打印告别语
    ui.release()

    if session:
        session.close()
    say("再见。会话已保存，下次 /resume 可续。")


# ---------------------------------------------------------------------------
# 战役模式（源自 agent-relay v0.2：STOP 收兵 / 班次锁 TTL 接管 / 轮次盖章）
# ---------------------------------------------------------------------------


def read_lock(lock_path: Path):
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def acquire_lock(lock_path: Path, ttl_seconds: int):
    lock = read_lock(lock_path)
    now = time.time()
    if lock and isinstance(lock.get("epoch"), (int, float)):
        age = now - float(lock["epoch"])
        if age < ttl_seconds:
            return False, "班次锁被 pid %s 持有（%d 秒前 < TTL %d 秒），本轮让位" % (
                lock.get("pid", "?"), age, ttl_seconds)
    lock_path.write_text(json.dumps({"epoch": now, "pid": os.getpid()}), encoding="utf-8")
    return True, ("过期锁已接管" if lock else "锁已获取")


def release_lock(lock_path: Path):
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print("qingzhou: 无法删除锁文件：%s" % exc, file=sys.stderr)


def next_round_number(progress_path: Path) -> int:
    if not progress_path.exists():
        return 1
    text = progress_path.read_text(encoding="utf-8", errors="replace")
    rounds = [int(n) for n in re.findall(r"\bRound\s+(\d+)\b", text)]
    return max(rounds) + 1 if rounds else 1


def append_stamp(progress_path: Path, stamp: str):
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not progress_path.exists() or progress_path.stat().st_size == 0
    with progress_path.open("a", encoding="utf-8", newline="\n") as handle:
        if fresh:
            handle.write("# 轻舟战役进度\n\n" + stamp)
        else:
            handle.write("\n" + stamp)


def pick_task(campaign_path: Path):
    """按 P0>P1>…>P4 领第一个未完成任务；返回 (行号, 原文, 优先级, 标题) 或 None。"""
    lines = campaign_path.read_text(encoding="utf-8", errors="replace").splitlines()
    best = None
    item_re = re.compile(r"^- \[( |x|~)\]\s*(?:P(\d))?\s*(.+?)\s*(?:[：:]|$)")
    for idx, line in enumerate(lines):
        m = item_re.match(line.strip())
        if not m:
            continue
        state, prio, title = m.group(1), m.group(2), m.group(3)
        if state != " ":
            continue  # 已完成/执行中的跳过
        p = int(prio) if prio is not None else 2
        key = (p, idx)
        if best is None or key < best[0]:
            best = (key, idx, line, p, title)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def write_back_task(campaign_path: Path, line_no: int, original_line: str, note: str, ok: bool):
    """回写账本：完成任务打 [x] 留档 + 流水一行（留档版规则）。"""
    try:
        lines = campaign_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        say("  回写失败：%s" % exc)
        return
    if 0 <= line_no < len(lines) and lines[line_no].strip() == original_line.strip():
        done_line = original_line.replace("- [ ]", "- [x]", 1)
        if not ok:
            done_line = original_line.replace("- [ ]", "- [~]", 1) + " ⚠未完全成功，见流水"
        else:
            done_line += "（轻舟完成于 %s）" % datetime.now().strftime("%m-%d %H:%M")
        lines[line_no] = done_line
    stamp = datetime.now().strftime("%m-%d %H:%M")
    lines.append("- %s ｜ 轻舟：%s" % (stamp, note))
    campaign_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def campaign_mode(cfg: dict, args, runlog=None):
    """战役模式：读账本 → 领最高优先级任务 → 全自动执行 → 回写留档 → 盖章，循环 N 轮。"""
    workspace = Path(os.getcwd()).resolve()
    campaign_path = (workspace / args.campaign)
    progress_path = workspace / PROGRESS_FILE
    lock_path = workspace / LOCK_FILE
    stop_path = workspace / STOP_FILE
    ttl = args.lock_ttl

    max_rounds = args.rounds if args.rounds is not None else 10**9
    say("轻舟战役模式 ｜ 账本: %s ｜ 最多 %s 轮 ｜ 锁 TTL %d 秒" % (
        campaign_path, max_rounds if args.rounds is not None else "不限", ttl))
    say("⚠ 战役模式每轮以全自动档运行（无人值守，不再逐条确认）。")
    gate = PermissionGate(2, workspace, interactive=False)
    say("收兵方式：在工作区创建文件 %s 即可在下一轮优雅停止。" % STOP_FILE)

    for _round in range(max_rounds):
        # 0. STOP 标记：战役结束，不删任何文件
        if stop_path.exists():
            say("发现收兵标记（%s），战役结束。删除该文件可恢复。" % stop_path)
            return 0

        round_no = next_round_number(progress_path)
        if args.rounds and round_no > args.rounds:
            append_stamp(progress_path, "### Round %d -- %s -- 战役完成（max-rounds）\n- (无执行)\n" % (
                round_no, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            say("已达 --rounds %d，战役完成。" % args.rounds)
            return 0

        acquired, reason = acquire_lock(lock_path, ttl)
        if not acquired:
            append_stamp(progress_path, "### Round %d -- %s -- SKIPPED（%s）\n" % (
                round_no, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason))
            say("第 %d 轮跳过：%s" % (round_no, reason))
            return 3

        try:
            if not campaign_path.exists():
                say("账本不存在：%s（先用 /tasks 或手工创建）" % campaign_path)
                return 2
            picked = pick_task(campaign_path)
            if picked is None:
                append_stamp(progress_path, "### Round %d -- %s -- 无待办任务，战役完成\n" % (
                    round_no, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                say("账本里没有待执行任务了，战役完成。")
                return 0
            line_no, original_line, prio, title = picked
            say("")
            say("═══ 第 %d 轮 ｜ 领活 P%d：%s" % (round_no, prio, title[:80]))

            session = Session.new("战役R%d-%s" % (round_no, slugify(title, 12)))
            messages = [{"role": "system", "content": build_system_prompt(workspace)}]
            prompt = ("当前任务账本（TASKS.md）中领到的任务（P%d）：%s\n"
                      "请直接开始完成这个任务。要求：\n"
                      "1. 先用 list_dir/read_file 了解现场，再动手；\n"
                      "2. 完成后调用 final_answer，answer 里写清：做了什么、产出在哪、验收方式；\n"
                      "3. 做不完也要 final_answer 说明卡在哪里，不要空转。" % (
                          prio, re.sub(r"^-\s*\[\s*\]\s*", "", original_line.strip())))
            agent = Agent(cfg, gate, session, max_steps=args.max_steps, no_stream=args.no_stream,
                          quiet_stream=False, hooks=_project_hooks(workspace, cfg), runlog=runlog,
                          deny_patterns=compile_deny_patterns(cfg, workspace))
            answer = ""
            if runlog:
                runlog.write("INFO", "campaign_round", round=round_no, task=title[:80])
            try:
                answer = agent.run(messages, prompt)
            except QingzhouError as exc:
                say("  [轮次出错] %s" % exc)
            finally:
                session.close()

            promises = [p.strip().upper() for p in re.findall(r"<promise>(.*?)</promise>", answer, re.DOTALL)]
            ok = bool(answer.strip()) and "DONE" in promises
            note = (answer.splitlines()[0][:100] if ok else "本轮未获得明确结果（详见会话文件）")
            write_back_task(campaign_path, line_no, original_line, note, ok)
            append_stamp(progress_path, "### Round %d -- %s\n- task: %s\n- result: %s\n" % (
                round_no, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                title[:80], note[:120]))
            say("第 %d 轮收工，账本已回写。" % round_no)
        finally:
            release_lock(lock_path)

        time.sleep(1)
    return 0


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qingzhou.py",
        description="轻舟 — 极简单文件终端 Agent（纯标准库，OpenAI 兼容端点通吃）",
        epilog="会话内命令：/help /mode /model /new /resume /compact /tasks /handoff /status /exit",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", "-t", metavar="TEXT", help="单发任务：跑完即退出（无交互）")
    parser.add_argument("--yolo", action="store_true", help="全自动档（等同 --mode 2，危险，不再询问）")
    parser.add_argument("--mode", type=int, choices=(1, 2, 3), default=None, help="权限档：1 审批（默认）/ 2 全自动 / 3 逐条")
    parser.add_argument("--max-steps", type=int, default=30, help="单任务最大工具轮数（默认 30）")
    parser.add_argument("--no-stream", action="store_true", help="禁用流式输出（某些端点不支持时用）")
    parser.add_argument("--config", default=str(CONFIG_DEFAULT), help="配置文件路径（默认：脚本同目录 qingzhou.json）")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次会话")
    parser.add_argument("--campaign", metavar="TASKS.md", help="战役模式：按账本无人值守逐项干活")
    parser.add_argument("--rounds", type=int, default=None, help="战役模式最大轮数（默认不限，直到无任务/STOP）")
    parser.add_argument("--lock-ttl", type=int, default=LOCK_TTL_DEFAULT, help="班次锁 TTL 秒数（默认 %d）" % LOCK_TTL_DEFAULT)
    parser.add_argument("--no-log", action="store_true", help="禁用运行日志（默认启用：配置文件旁 logs/qingzhou.log）")
    parser.add_argument("--effort", choices=VALID_EFFORTS, default=None, help="思考档：none/low/medium/high/max（覆盖配置，对齐 Claude Code --effort）")
    parser.add_argument("--version", action="version", version="qingzhou " + QZ_VERSION)
    return parser


def main(argv=None):
    _reconfigure_stdio()
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    global SESSIONS_DIR
    SESSIONS_DIR = config_path.parent / "sessions"
    # 运行日志：跟配置文件走（U 盘场景=日志在盘上，跨主机留痕）
    runlog = RunLog(config_path.parent, enabled=not args.no_log)
    try:
        try:
            cfg = ensure_config(config_path)
        except QingzhouError as exc:
            say("[轻舟] %s" % exc)
            return 2

        if args.effort:
            cfg["reasoning_effort"] = args.effort  # 启动标志覆盖配置（env 更高，在 LLMClient 里处理）
        mode = 2 if args.yolo else (args.mode or 1)
        workspace = Path(os.getcwd()).resolve()
        gate = PermissionGate(mode, workspace, interactive=not args.task and not args.campaign)
        if mode == 2:
            say("⚠ 全自动档启动：轻舟将不再询问直接执行命令与写文件（工作区外写入首次放行前会确认一次）。")
        runlog.snapshot(cfg, workspace)

        try:
            if args.campaign:
                return campaign_mode(cfg, args, runlog=runlog)
            if args.task:
                return run_one_shot(cfg, gate, args, runlog=runlog)
            interactive_mode(cfg, gate, args.max_steps, args.no_stream, args.resume, runlog=runlog, config_path=config_path)
            return 0
        except KeyboardInterrupt:
            runlog.write("INFO", "interrupted")
            say("\n[轻舟] 已中断。")
            return 130
        except Exception as exc:  # 最后的保险丝：任何裸异常都进日志再退出
            runlog.write("ERROR", "fatal_unhandled", error=str(exc)[:300], where=sys.exc_info()[:2] and "%s:%s" % (type(exc).__name__, exc))
            raise
    finally:
        runlog.close()


def run_one_shot(cfg: dict, gate: PermissionGate, args, runlog=None) -> int:
    """--task 单发任务：跑完退出，结果打印到终端。"""
    workspace = Path(os.getcwd()).resolve()
    session = Session.new(args.task)
    messages = [{"role": "system", "content": build_system_prompt(workspace)}]
    agent = Agent(cfg, gate, session, max_steps=args.max_steps, no_stream=args.no_stream, quiet_stream=True,
                  hooks=_project_hooks(workspace, cfg), runlog=runlog,
                  deny_patterns=compile_deny_patterns(cfg, workspace))
    try:
        try:
            answer = agent.run(messages, args.task)
            if runlog:
                runlog.write("INFO", "task_done", ok=bool(answer.strip()), answer=answer[:150])
            return 0 if answer.strip() else 1
        except QingzhouError as exc:
            say("[出错] %s" % exc)
            return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())