#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻舟冒烟测试：用纯标准库 mock 一个 OpenAI 兼容端点，端到端验证 agent 循环。

不依赖任何第三方库，不需要真实 LLM key。
用法：python tests_smoke.py
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
QZ = ROOT / "qingzhou.py"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


# ---------------------------------------------------------------------------
# Mock LLM 端点：按任务关键词走预设剧本，逐次弹出下一句回复
# ---------------------------------------------------------------------------

MOCK_PORT = 18321
STATE = {"stream_mode": True, "reject_stream": False}  # reject_stream: POST stream=True 时回 400


class MockHandler(BaseHTTPRequestHandler):
    SCRIPTS = {}   # 关键词 -> [回复1, 回复2, ...]
    COUNTERS = {}  # 关键词 -> 已消耗次数
    CALLS = []     # 审计：每次请求的 (stream, n_msg)
    EFFORTS = []   # 审计：每次请求的 reasoning_effort
    LAST_SYSTEM = ""  # 最近一次请求里的 system 消息全文（验证注入用）
    LAST_USER_JOIN = ""  # 最近一次请求里所有 user 消息拼接（验证钩子反馈注入用）

    def log_message(self, *a):
        pass

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        stream = bool(payload.get("stream"))
        MockHandler.CALLS.append({"stream": stream, "n_msg": len(payload.get("messages", []))})
        MockHandler.EFFORTS.append(payload.get("reasoning_effort", "(未发)"))
        for m in payload.get("messages", []):
            if m.get("role") == "system":
                MockHandler.LAST_SYSTEM = str(m.get("content", ""))
        MockHandler.LAST_USER_JOIN = "\n".join(
            str(m.get("content", "")) for m in payload.get("messages", []) if m.get("role") == "user")

        # 找最近一条用户消息命中哪个剧本
        user_text = ""
        for m in reversed(payload.get("messages", [])):
            if m.get("role") == "user":
                user_text += str(m.get("content", ""))[:1500] + "\n"
        script_key = None
        for key in MockHandler.SCRIPTS:
            if key in user_text:
                script_key = key
                break

        seq = MockHandler.SCRIPTS.get(script_key, [])
        if not seq:
            reply = '```qingzhou\n{"tool": "final_answer", "args": {"answer": "mock 默认答复"}}\n```'
        else:
            used = MockHandler.COUNTERS.get(script_key, 0)
            MockHandler.COUNTERS[script_key] = used + 1
            reply = seq[min(used, len(seq) - 1)]  # 超出剧本后重复最后一条

        if stream and STATE["reject_stream"]:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "stream not supported"}')
            return

        if stream and STATE["stream_mode"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            n = max(1, len(reply) // 3)
            for piece in (reply[:n], reply[n:2 * n], reply[2 * n:]):
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
                               "usage": {"total_tokens": 0}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)


def start_mock():
    server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reset_scripts():
    MockHandler.SCRIPTS.clear()
    MockHandler.COUNTERS.clear()
    MockHandler.CALLS.clear()


def script(task_key, steps):
    MockHandler.SCRIPTS[task_key] = list(steps)


def run_qz(args, cwd, timeout=120):
    """跑一次轻舟（配置全走环境变量），返回 (returncode, stdout, stderr)。"""
    env = dict(os.environ)
    env["QINGZHOU_BASE_URL"] = "http://127.0.0.1:%d/v1" % MOCK_PORT
    env["QINGZHOU_API_KEY"] = "mock-key"
    env["QINGZHOU_MODEL"] = "mock-model"
    done = subprocess.run(
        [sys.executable, str(QZ)] + args,
        cwd=str(cwd), env=env, capture_output=True, timeout=timeout)
    out = done.stdout.decode("utf-8", errors="replace")
    err = done.stderr.decode("utf-8", errors="replace")
    return done.returncode, out, err


def latest_session_content():
    """取最近修改的会话文件内容（按 mtime，避免按名字排序踩坑）。"""
    sessions = [p for p in (ROOT / "sessions").glob("*.jsonl") if p.is_file()]
    if not sessions:
        return ""
    return max(sessions, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def main():
    server = start_mock()
    tmp = ROOT / ".smoke-tmp"
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()

    print("== 1. CLI 基本可用 ==")
    rc, out, err = run_qz(["--version"], tmp)
    check("CLI --version", rc == 0 and "qingzhou" in out, out + err)

    print("== 2. 单发任务：写文件 → 跑命令 → final_answer（流式 + 全工具链） ==")
    reset_scripts()
    script("hello.txt", [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "hello.txt", "content": "你好轻舟"}}\n```',
        '```qingzhou\n{"tool": "run_cmd", "args": {"command": "type hello.txt"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "已写入 hello.txt 并验证内容"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "帮我写个小文件到 hello.txt", "--yolo"], tmp)
    hello = tmp / "hello.txt"
    check("任务成功退出", rc == 0, out[-2000:] + err[-800:])
    check("文件已写出且内容正确", hello.exists() and hello.read_text(encoding="utf-8") == "你好轻舟")
    check("工具结果回喂后命令真执行了（type 输出可见）", "你好轻舟" in out, out)
    check("final_answer 展示", "已写入 hello.txt" in out)
    check("请求走的流式", any(c["stream"] for c in MockHandler.CALLS))
    check("单任务恰好 3 次请求（写→命令→final）", len(MockHandler.CALLS) == 3, str(len(MockHandler.CALLS)))

    print("== 3. 会话落盘（会话跟配置目录走，不跟工作区走） ==")
    sessions = sorted((ROOT / "sessions").glob("*.jsonl"))
    check("会话文件已生成在配置目录", len(sessions) >= 1)
    check("工作区不落会话目录", not (tmp / "sessions").exists())
    raw = sessions[-1].read_text(encoding="utf-8")
    check("jsonl 含 meta/msg/action 事件", '"t": "meta"' in raw and '"t": "msg"' in raw and '"t": "action"' in raw)

    print("== 4. 权限闸：逐条档在非交互下自动拒绝 ==")
    reset_scripts()
    script("删掉一切", [
        '```qingzhou\n{"tool": "run_cmd", "args": {"command": "echo should-not-run"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "用户拒绝执行，我停止了"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "删掉一切", "--mode", "3"], tmp)
    check("非交互自动拒绝（命令没跑）", "[用户拒绝了该命令]" in out and "[执行] run_cmd" not in out, out)
    check("拒绝原因回喂模型", "用户拒绝执行" in out, out)

    print("== 5. 协议容错：不按协议 → 提示重整 → 3 次失败终止 ==")
    reset_scripts()
    script("乱说话", ["我不会任何工具调用。"] * 5)
    rc, out, err = run_qz(["--task", "乱说话", "--yolo", "--max-steps", "6"], tmp)
    check("连续无工具被纠正并终止", "没有按协议" in out or "协议" in out, out)

    print("== 6. 流式 400 → 自动退化非流式 ==")
    reset_scripts()
    STATE["reject_stream"] = True
    script("退个流", [
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "非流式也能跑通"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "退个流", "--yolo"], tmp)
    check("退化非流式成功", rc == 0 and "非流式也能跑通" in out, out[-2000:] + err[-800:])
    check("两种请求都发过（流式+非流式）", any(c["stream"] for c in MockHandler.CALLS) and any(not c["stream"] for c in MockHandler.CALLS))
    STATE["reject_stream"] = False

    print("== 7. 工作区边界：逐条档非交互下拒绝工作区外写 ==")
    reset_scripts()
    script("写外面", [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "../outside.txt", "content": "x"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "已停止"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "写外面", "--mode", "3"], tmp)
    check("工作区外写入被拦", not (tmp.parent / "outside.txt").exists(), out)

    print("== 8. 战役模式：P0 领活 → 干活 → 回写留档 → 盖章 ==")
    camp = tmp / "camp1"
    camp.mkdir()
    (camp / "TASKS.md").write_text(
        "# 任务账本 · camp1\n\n## 待办\n\n- [ ] P1 搞定甲：产出 jia.txt\n"
        "- [ ] P2 搞定乙：产出 yi.txt\n- [ ] P0 最优先：产出 ling.txt\n",
        encoding="utf-8")
    reset_scripts()
    script("最优先", [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "ling.txt", "content": "P0 done"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "P0 任务完成，产出 ling.txt <promise>DONE</promise>"}}\n```',
    ])
    rc, out, err = run_qz(["--campaign", "TASKS.md", "--rounds", "1", "--yolo"], camp)
    tasks = (camp / "TASKS.md").read_text(encoding="utf-8")
    check("战役退出码 0", rc == 0, out[-2000:] + err[-800:])
    check("按 P 级领了 P0（ling.txt 产出）", (camp / "ling.txt").exists() and "P0 done" in (camp / "ling.txt").read_text(encoding="utf-8"))
    check("账本回写打勾留档", "- [x] P0 最优先" in tasks and "轻舟完成于" in tasks)
    check("流水已追加", "轻舟：P0 任务完成" in tasks)
    check("进度盖章 Round 1", (camp / ".qz-progress.md").exists() and "Round 1" in (camp / ".qz-progress.md").read_text(encoding="utf-8"))
    check("锁文件已释放", not (camp / ".qz-lock").exists())

    print("== 9. 战役 STOP 收兵 ==")
    (camp / ".qz-stop").write_text("", encoding="utf-8")
    rc, out, err = run_qz(["--campaign", "TASKS.md", "--yolo"], camp)
    check("STOP 生效退出 0", rc == 0 and "收兵" in out, out)

    print("== 10. env 配置直跑（未生成配置文件） ==")
    check("未误写配置文件（走 env）", not (tmp / "qingzhou.json").exists())

    print("== 11. 计划模式闸门（plan_only 直测） ==")
    import importlib.util
    spec = importlib.util.spec_from_file_location("qz_mod", QZ)
    qz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qz)
    from pathlib import Path as _P
    gate = qz.PermissionGate(1, _P(tmp), interactive=False)
    gate.plan_only = True
    ok_w, note_w = gate.check("write_file", {"path": "x.txt", "content": "x"})
    ok_c, note_c = gate.check("run_cmd", {"command": "echo hi"})
    ok_r, _ = gate.check("read_file", {"path": "hello.txt"})
    ok_f, _ = gate.check("final_answer", {"answer": "计划：…"})
    check("计划模式拦 write_file", ok_w is False and "计划模式" in note_w)
    check("计划模式拦 run_cmd", ok_c is False and "计划模式" in note_c)
    check("计划模式放行 read_file", ok_r is True)
    check("计划模式放行 final_answer", ok_f is True)
    gate.plan_only = False
    ok_c2, _ = gate.check("run_cmd", {"command": "echo hi"})
    check("退出计划模式后 run_cmd 恢复（非交互仍拒）", ok_c2 is False and "计划模式" not in _)

    print("== 11b. 非法参数不裸崩（Bug ① 回归用例） ==")
    reset_scripts()
    script("坏参数", [
        '```qingzhou\n{"tool": "read_file", "args": {"path": "hello.txt", "start": "abc"}}\n```',
        '```qingzhou\n{"tool": "list_dir", "args": {"path": ".", "depth": "很深"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "收到错误但没崩"}}\n```',
    ])
    MockHandler.COUNTERS.clear()
    rc, out, err = run_qz(["--task", "坏参数", "--yolo"], tmp)
    check("非法参数被转成错误文本（会话不崩）", rc == 0 and "工具执行异常" in out and "收到错误但没崩" in out, out[-1200:])
    check("没有裸 traceback", "Traceback" not in err, err[-600:])

    print("== 12. test 钩子：失败才注入，通过不注入 ==")
    camp2 = tmp / "camp2"
    camp2.mkdir()
    # 钩子：哨兵文件存在才算通过 → 模型"修复"=写哨兵文件，可真实走通 失败→反馈→修复→通过 全链路
    (camp2 / "qingzhou.json").write_text(
        json.dumps({"hooks": {"test": 'python -c "import os,sys; sys.exit(0 if os.path.exists(\'DONE\') else 1)"'}}),
        encoding="utf-8")
    reset_scripts()
    script("勾子任务", [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "h.txt", "content": "1"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "第一次宣布完成"}}\n```',
        '```qingzhou\n{"tool": "write_file", "args": {"path": "DONE", "content": "修复：创建哨兵"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "第二次宣布完成"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "勾子任务", "--yolo"], camp2)
    check("钩子失败反馈给了模型（进入下一轮请求的 user 消息）",
          "测试钩子结果" in MockHandler.LAST_USER_JOIN and "请修复后再次" in MockHandler.LAST_USER_JOIN,
          MockHandler.LAST_USER_JOIN[-500:])
    check("终端可见钩子失败与修复提示", "测试失败（退出码 1）" in out and "反馈给模型修复" in out, out[-800:])
    check("模型收到反馈后修复重做（第二次final）", "第二次宣布完成" in out, out[-1500:])
    check("最终退出 0", rc == 0)
    check("哨兵文件已创建（模型真的修复了）", (camp2 / "DONE").exists())
    camp3 = tmp / "camp3"
    camp3.mkdir()
    (camp3 / "qingzhou.json").write_text(
        json.dumps({"hooks": {"test": 'python -c "print(42)"'}}), encoding="utf-8")
    reset_scripts()
    script("勾子过", [
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "直接完成"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "勾子过", "--yolo"], camp3)
    check("钩子通过时不注入输出", "直接完成" in out and "测试钩子结果" not in out, out)

    print("== 12b. test 钩子熔断：修不好时带病收尾不烧满步数 ==")
    camp4 = tmp / "camp4"
    camp4.mkdir()
    (camp4 / "qingzhou.json").write_text(
        json.dumps({"hooks": {"test": 'python -c "import sys; sys.exit(1)"'}}), encoding="utf-8")
    reset_scripts()
    script("修不好", [
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "我宣布完成"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "修不好", "--yolo", "--max-steps", "30"], camp4)
    check("熔断触发（带病收尾提示）", "带病收尾" in out and "测试钩子未通过" in out, out[-1200:])
    check("没烧满 30 步", "已达最大步数" not in out)
    check("最终退出 0（带病交付仍算完成）", rc == 0)

    print("== 13. /undo 快照：改写已存在文件可回滚（含重启后回滚） ==")
    undo_dir = tmp / "undo-case"
    undo_dir.mkdir()
    target = undo_dir / "code.txt"
    target.write_text("v1-原始", encoding="utf-8")
    reset_scripts()
    script("改写它", [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "code.txt", "content": "v2-轻舟改写"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "已改写"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "改写它", "--yolo"], undo_dir)
    check("文件已被轻舟改写", target.read_text(encoding="utf-8") == "v2-轻舟改写")
    backups = list((undo_dir / ".qingzhou-undo").glob("*.bak"))
    check("改写前快照已存", len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "v1-原始")
    check("快照清单 registry.json 已落盘", (undo_dir / ".qingzhou-undo" / "registry.json").exists())
    # 模拟"重启进程后 /undo"：新进程直接调 undo_last_write
    import importlib.util
    spec2 = importlib.util.spec_from_file_location("qz_undo", QZ)
    qz2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(qz2)
    undone = qz2.undo_last_write(undo_dir)
    check("重启进程后仍可回滚（registry 生效）", undone and target.read_text(encoding="utf-8") == "v1-原始",
          "undone=%r content=%r" % (undone, target.read_text(encoding="utf-8")))
    check("回滚后备份已清理", not list((undo_dir / ".qingzhou-undo").glob("*.bak")))

    print("== 14. 指令层级：上级目录 qingzhou.md 注入 + @import ==")
    lvl = tmp / "lvl"
    (lvl / "sub").mkdir(parents=True)
    (lvl / "qingzhou.md").write_text("规矩一：永远用中文回复。\n@style.md\n", encoding="utf-8")
    (lvl / "style.md").write_text("风格：简短。\n", encoding="utf-8")
    reset_scripts()
    script("层级", [
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "收到指令"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "层级", "--yolo"], lvl / "sub")
    raw = MockHandler.LAST_SYSTEM
    check("上级目录指令已注入", "规矩一：永远用中文回复" in raw, raw[-300:])
    check("@import 已展开（@style.md 的内容进了系统提示）", "风格：简短。" in raw)

    print("== 15. AGENTS.md 兼容 ==")
    ag = tmp / "ag"
    ag.mkdir()
    (ag / "AGENTS.md").write_text("AGENTS 规矩：先看后动。", encoding="utf-8")
    reset_scripts()
    script("agents兼容", [
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "ok"}}\n```',
    ])
    rc, out, err = run_qz(["--task", "agents兼容", "--yolo"], ag)
    raw = MockHandler.LAST_SYSTEM
    check("AGENTS.md 也被注入", "AGENTS 规矩：先看后动" in raw)

    print("== 16. 顶层平铺参数兼容（真机联调抓到的 GLM 写法） ==")
    reset_scripts()
    MockHandler.SCRIPTS["平铺写法"] = [
        '```qingzhou\n{"tool": "write_file", "path": "flat.txt", "content": "顶层参数"}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "平铺也能跑通"}}\n```',
    ]
    MockHandler.COUNTERS.clear()
    rc, out, err = run_qz(["--task", "平铺写法", "--yolo"], tmp)
    check("顶层平铺参数被正确解析执行", (tmp / "flat.txt").exists() and (tmp / "flat.txt").read_text(encoding="utf-8") == "顶层参数", out[-1200:])
    check("平铺写法任务成功", rc == 0 and "平铺也能跑通" in out)

    print("== 17. 运行日志：环境快照+调用过程留痕+key掩码 ==")
    log_file = ROOT / "logs" / "qingzhou.log"
    check("日志文件生成", log_file.exists())
    log = log_file.read_text(encoding="utf-8", errors="replace")
    check("环境快照含 Python 版本与平台", "session_start" in log and "python=" in log and "platform=" in log)
    check("调用过程留痕（llm_request/tool_exec）", "llm_request" in log and "tool_exec" in log)
    check("非法参数记为 tool_error", "tool_error" in log)
    # 掩码允许前6位出现（真机冒烟日志 key=bea90e*** 属正常掩码）；泄漏判定=完整明文key
    full_key_leak = "bea90e6ca77f4986afb11d867cc99e6e" in log or "mock-key" in log
    check("key 只记掩码不入明文", not full_key_leak and "key=mock-k***" in log)
    check("单文件轮转上限配置存在", "LOG_MAX_BYTES" in (ROOT / "qingzhou.py").read_text(encoding="utf-8"))

    print("== 18. 鉴权失败 401：人话提示，绝不闪退（真机闪退事故回归） ==")
    class AuthFailHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_POST(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "invalid api key"}')
    auth_server = ThreadingHTTPServer(("127.0.0.1", 18399), AuthFailHandler)
    threading.Thread(target=auth_server.serve_forever, daemon=True).start()
    env401 = tmp / "auth401"
    env401.mkdir()
    env = dict(os.environ)
    env["QINGZHOU_BASE_URL"] = "http://127.0.0.1:18399/v1"
    env["QINGZHOU_API_KEY"] = "bad-key"
    env["QINGZHOU_MODEL"] = "m"
    done = subprocess.run([sys.executable, str(QZ), "--task", "你好", "--yolo"],
                          cwd=str(env401), env=env, capture_output=True, timeout=120)
    out = done.stdout.decode("utf-8", errors="replace")
    err = done.stderr.decode("utf-8", errors="replace")
    check("401 给出人话提示", "鉴权失败" in out and "401" in out, out[-800:])
    check("没有裸 traceback（stderr 干净）", "Traceback" not in err, err[-600:])
    check("退出码 2（友好失败）", done.returncode == 2, str(done.returncode))
    auth_server.shutdown()


    print("== 19. reasoning_effort 思考档（对齐 Claude Code effortLevel） ==")
    MockHandler.EFFORTS = []
    env9 = tmp / "effort"
    env9.mkdir()
    cfg9 = env9 / "qingzhou.json"
    cfg9.write_text(json.dumps(
        {"base_url": "http://127.0.0.1:%d/v1" % MOCK_PORT, "api_key": "k", "model": "m", "reasoning_effort": "low"}),
        encoding="utf-8")
    reset_scripts()
    script("effort测", ['```qingzhou\n{"tool": "final_answer", "args": {"answer": "ok"}}\n```'])
    MockHandler.COUNTERS.clear()
    e2 = dict(os.environ); e2.pop("QINGZHOU_BASE_URL", None); e2.pop("QINGZHOU_API_KEY", None); e2.pop("QINGZHOU_MODEL", None)
    d2 = subprocess.run([sys.executable, str(QZ), "--config", str(cfg9), "--task", "effort测", "--yolo"], cwd=str(env9), env=e2, capture_output=True, timeout=120)
    check("配置 reasoning_effort=low 会进请求体", MockHandler.EFFORTS and MockHandler.EFFORTS[0] == "low", str(MockHandler.EFFORTS[:3]))


    print("== 20. deny 危险命令清单（任何档位都拦） ==")
    reset_scripts()
    MockHandler.SCRIPTS["危险命令"] = [
        '```qingzhou\n{"tool": "run_cmd", "args": {"command": "rm -rf /"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "被拦后收尾"}}\n```',
    ]
    MockHandler.COUNTERS.clear()
    rc, out, err = run_qz(["--task", "危险命令", "--yolo"], tmp)
    check("rm -rf / 被安全闸拦截", "危险命令规则" in out and "安全闸" in out, out[-800:])
    check("拒绝理由回喂模型", "被拦后收尾" in out)
    check("即便 --yolo 也拦", rc == 0)

    print("== 21. PreToolUse 用户钩子（CC 式：非零退出拦截） ==")
    hookdir = tmp / "hookcase"
    hookdir.mkdir()
    # 钩子脚本：读 stdin，若 tool==run_cmd 则退出 1（拦截）
    hook = hookdir / "hook.py"
    hook.write_text('import json,sys\nd=json.load(sys.stdin)\n'
                    'sys.exit(1) if d.get("tool")=="run_cmd" else sys.exit(0)\n', encoding="utf-8")
    (hookdir / "qingzhou.json").write_text(json.dumps(
        {"base_url": "http://127.0.0.1:%d/v1" % MOCK_PORT, "api_key": "k", "model": "m",
         "hooks": {"pre_tool": "python hook.py"}}), encoding="utf-8")
    reset_scripts()
    MockHandler.SCRIPTS["钩子拦截"] = [
        '```qingzhou\n{"tool": "run_cmd", "args": {"command": "echo hi"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "钩子拦了我"}}\n```',
    ]
    MockHandler.COUNTERS.clear()
    cfgH = hookdir / "qingzhou.json"
    env3 = dict(os.environ)
    for k in ("QINGZHOU_BASE_URL", "QINGZHOU_API_KEY", "QINGZHOU_MODEL"):
        env3.pop(k, None)
    d3 = subprocess.run([sys.executable, str(QZ), "--config", str(cfgH), "--task", "钩子拦截", "--yolo"],
                        cwd=str(hookdir), env=env3, capture_output=True, timeout=120)
    out3 = d3.stdout.decode("utf-8", errors="replace")
    check("PreToolUse 钩子拦截了 run_cmd", "PreToolUse 钩子拦截" in out3, out3[-800:])
    check("拦截理由回喂模型", "钩子拦了我" in out3)

    print("== 22. 战役 completion-promise（Ralph 式假完工防护） ==")
    camp5 = tmp / "camp5"
    camp5.mkdir()
    (camp5 / "TASKS.md").write_text("- [ ] P1 打个招呼：产出 hello.txt\n", encoding="utf-8")
    reset_scripts()
    MockHandler.SCRIPTS["招呼"] = [
        '```qingzhou\n{"tool": "write_file", "args": {"path": "hello.txt", "content": "hi"}}\n```',
        '```qingzhou\n{"tool": "final_answer", "args": {"answer": "做完了（没写承诺词）"}}\n```',
    ]
    MockHandler.COUNTERS.clear()
    rc, out, err = run_qz(["--campaign", "TASKS.md", "--rounds", "1", "--yolo"], camp5)
    tasks5 = (camp5 / "TASKS.md").read_text(encoding="utf-8")
    check("无承诺词 → 账本记 [~] 未完全成功", "- [~] P1 打个招呼" in tasks5, tasks5)
    check("文件实际产出了（活干了，但不算完成）", (camp5 / "hello.txt").exists())

    print("== 23. save_config 密钥红线（env 注入永不落盘） ==")
    import tempfile
    tdir = Path(tempfile.mkdtemp())
    cfa = tdir / 'a.json'
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location('qz_mod2', QZ)
    qzm = _ilu.module_from_spec(_spec); _spec.loader.exec_module(qzm)
    qzm.save_config(cfa, {'base_url': 'https://x/v1', 'api_key': 'ENV_SECRET', 'model': 'm', '_api_key_from_env': True})
    saved_a = json.loads(cfa.read_text(encoding='utf-8'))
    check('A: env 注入的 key 不落盘', 'ENV_SECRET' not in json.dumps(saved_a))
    cfb = tdir / 'b.json'
    cfb.write_text(json.dumps({'api_key': 'OLD'}), encoding='utf-8')
    qzm.save_config(cfb, {'api_key': 'ENV_SECRET', '_api_key_from_env': True})
    saved_b = json.loads(cfb.read_text(encoding='utf-8'))
    check('B: 文件旧 key 保留且新值不进', saved_b.get('api_key') == 'OLD')
    cfc = tdir / 'c.json'
    qzm.save_config(cfc, {'model': 'm', 'api_key': 'RUNTIME_KEY'})
    saved_c = json.loads(cfc.read_text(encoding='utf-8'))
    check('C: 运行时 key 不新增落盘', 'api_key' not in saved_c)

    print("== 24. deny 不误杀合法命令 ==")
    _pats = qzm.compile_deny_patterns({}, Path('.'))
    def _hit(cmd):
        return any(p.search(cmd) for p, _ in _pats)
    check('拦 shutdown /s', _hit('shutdown /s /t 0'))
    check('拦 diskpart', _hit('diskpart'))
    check('不拦 echo shutdown', not _hit('echo shutdown help'))
    check('不拦 cat app.log 检查 shutdown 字样', not _hit('grep shutdown app.log'))

    print("== 25. 日志脱敏：write_file 正文不入日志 ==")
    logf = ROOT / 'logs' / 'qingzhou.log'
    if logf.exists():
        logt = logf.read_text(encoding='utf-8', errors='replace')
        has_leak = ('content=' + chr(34) + '你好轻舟' in logt) or ('正文不入日志' in logt)
        check('write_file 日志只有长度标记', '正文不入日志' in logt or '你好轻舟' not in logt)
    else:
        check('write_file 日志只有长度标记（日志存在）', False, '无日志文件')

    print("== 26. DECSTBM 固定底栏（CC CLAUDE_CODE_DECSTBM 模式同款） ==")
    import importlib.util as _ilu3
    _spec3 = _ilu3.spec_from_file_location('qz_tui', QZ)
    qz3 = _ilu3.module_from_spec(_spec3); _spec3.loader.exec_module(qz3)
    ui = qz3.FixedBottomUI()
    ui.on = True
    ui.rows, ui.cols = 30, 100
    _cap = type('Cap', (), {'buf': [], 'write': staticmethod(lambda t: type('Cap',(),{'buf':[]}).buf.append(t) if False else None), 'flush': staticmethod(lambda: None)})()
    buf3 = []
    _cap.write = buf3.append
    _cap.flush = lambda: None
    _real = qz3.sys.stdout
    qz3.sys.stdout = _cap
    qz3.os.get_terminal_size = lambda: __import__('os').terminal_size((100, 30))
    ui.engage()
    ui.draw_fixed_rows('status-test', 'X ')
    ui.release()
    qz3.sys.stdout = _real
    joined = ''.join(buf3)
    check('DECSTBM 滚动区域[1;28r', chr(27) + '[1;28r' in joined)
    check('状态栏定位[29;1H', chr(27) + '[29;1H' in joined)
    check('输入行定位[30;1H', chr(27) + '[30;1H' in joined)
    check('退出恢复[r', chr(27) + '[r' in joined)
    check('非tty自动降级', qz3.FixedBottomUI.__new__(qz3.FixedBottomUI) is not None)

    print("== 27. auto-compact 阈值机制（CC autoCompactEnabled 同款） ==")
    _spec4 = importlib.util.spec_from_file_location('qz_ac', QZ)
    qz4 = importlib.util.module_from_spec(_spec4); _spec4.loader.exec_module(qz4)
    check("阈值常量存在", hasattr(qz4, 'AUTO_COMPACT_THRESHOLD'))
    check("阈值默认 0.85", qz4.AUTO_COMPACT_THRESHOLD == 0.85)
    # 非阻塞键盘采集器：管道下应返回 (buf, False) 不崩
    b, done = qz4._poll_keyboard("abc")
    check("采集器无键时安全返回", b == "abc" and done is False)
    src = (ROOT / "qingzhou.py").read_text(encoding="utf-8")
    check("自动压缩配置项 auto_compact 可关", '"auto_compact"' in src and "auto_compact\", True" in src)
    check("低水位预警文案存在", "上下文已用" in src)

    print("== 28. 小修小补回归（v0.3.1 修复项） ==")
    check("pretty_model v3 不再吞数字", qz4.pretty_model("glm-v3") == "GLM V3")
    check("pretty_model v12 完整保留", qz4.pretty_model("llama-v12") == "Llama V12")
    check("拦行首 Restart-Computer", _hit('Restart-Computer -Force'))
    check("拦 rd /s 别名", _hit('rd /s /q C:\\temp'))
    check("不拦 echo Restart-Computer", not _hit('echo Restart-Computer -Force'))
    cfd = tdir / 'd.json'
    qzm.save_config(cfd, {'base_url': 'https://x/v1', 'api_key': 'WIZARD_KEY', 'model': 'm'}, allow_api_key=True)
    check("向导显式同意时 key 可落盘", json.loads(cfd.read_text(encoding='utf-8')).get('api_key') == 'WIZARD_KEY')
    camp6 = Path(tempfile.mkdtemp())
    (camp6 / "TASKS.md").write_text("## 待办\n- [ ] P1 不该被执行\n", encoding="utf-8")
    rc6, out6, err6 = run_qz(["--campaign", "TASKS.md", "--rounds", "0"], camp6)
    tasks6 = (camp6 / "TASKS.md").read_text(encoding="utf-8")
    check("--rounds 0 立即收工退出 0", rc6 == 0, out6[-800:] + err6[-400:])
    check("--rounds 0 不动账本", "[~]" not in tasks6 and "[x]" not in tasks6, tasks6)

    server.shutdown()
    print()
    print("════════════════════════════════════")
    print("  冒烟测试：%d 通过 / %d 失败" % (PASS, FAIL))
    print("════════════════════════════════════")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())