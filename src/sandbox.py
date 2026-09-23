# -*- coding: utf-8 -*-
"""代码沙箱：AST 静态审查 + rlimit 资源受限子进程。
（AutoDL 容器无 Docker，沙箱降级实现；生产环境按实施方案替换为一次性 Docker 容器）"""
import ast
import json
import os
import resource
import shutil
import subprocess
import sys
from pathlib import Path

from src.config import abs_path, settings

BANNED_MODULES = {"os", "sys", "subprocess", "socket", "ssl", "shutil", "ctypes",
                  "multiprocessing", "threading", "requests", "urllib", "http",
                  "ftplib", "smtplib", "pickle", "importlib", "pathlib"}
BANNED_NAMES = {"__import__", "eval", "exec", "compile", "open", "globals",
                "locals", "breakpoint", "system", "popen", "kill", "remove",
                "unlink", "rmtree"}


def audit(code: str) -> list[str]:
    """返回违规列表；非空则拒绝执行"""
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            issues += [a.name for a in node.names if a.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BANNED_MODULES:
                issues.append(node.module)
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            issues.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            issues.append(node.attr)
    return issues


def _limits() -> None:  # 子进程内生效
    # 注意: 本容器内核上 RLIMIT_AS 会挂死/击杀 python(_malloc 循环), 故不设内存硬限制;
    # 内存越界靠墙钟超时兜底, 生产环境由 Docker 沙箱(--memory)保障 —— 见实施方案 2.2
    cpu = settings.sandbox_timeout_seconds
    # P1-2: 软限=墙钟超时(CPU 到点发 SIGXCPU), 硬限=+5s 兜底(SIGKILL);
    # 区分 timeout(墙钟)/resource_limit(SIGXCPU/SIGKILL) 语义
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setsid()  # 独立进程组，超时可整组杀掉


def run_code(task_id: str, code: str, source_file: Path) -> dict:
    """在工作目录 input/ 下执行代码，返回 {status, stdout, error, result, charts}"""
    work = abs_path(settings.task_dir) / task_id
    inp, out = work / "input", work / "output"
    inp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    # 只清上一次执行的结果文件；charts 累积保留（多步骤图表都会进最终报告）
    for name in ("result.json", "result_df.csv", "result_df.parquet"):
        (out / name).unlink(missing_ok=True)
    (inp / "code.py").write_text(code, encoding="utf-8")
    link = inp / "sales.csv"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(source_file.resolve())

    runner = Path(__file__).resolve().parent / "sandbox_runner.py"
    import time as _time
    t0 = _time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(runner)],
            cwd=str(work), capture_output=True, text=True,
            timeout=settings.sandbox_timeout_seconds + 5,
            preexec_fn=_limits,
            env={"PATH": "/usr/bin:/bin", "HOME": str(work),
                 "MPLCONFIGDIR": str(work / ".mpl"), "LANG": "C.UTF-8"})
        elapsed = round(_time.time() - t0, 2)
        res_path = out / "result.json"
        if not res_path.exists():
            # P1-2: 按终止信号区分状态语义（timeout / resource_limit / error）
            rc = proc.returncode
            sig, status = None, "error"
            if rc == -24:                       # SIGXCPU: CPU 软限制触发
                status, sig = "resource_limit", "SIGXCPU"
            elif rc == -9:                      # SIGKILL: CPU 硬限制/OOM 等内核击杀
                status, sig = "resource_limit", "SIGKILL"
            elif rc is not None and rc < 0:
                status, sig = "resource_limit", f"SIG{-rc}"
            return {"status": status, "exit_code": rc, "signal": sig,
                    "elapsed_time": elapsed,
                    "error": f"runner 被终止(rc={rc}, {sig or '-'}) "
                             f"stderr={(proc.stderr or '')[-1500:]}",
                    "stdout": (proc.stdout or "")[-1000:],
                    "result": None, "charts": []}
        res = json.loads(res_path.read_text(encoding="utf-8"))
        res["charts"] = sorted(p.name for p in (out / "charts").glob("*.png"))
        res["exit_code"] = proc.returncode
        res["signal"] = None
        res["elapsed_time"] = elapsed
        return res
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        return {"status": "timeout", "exit_code": None, "signal": "WALLCLOCK",
                "elapsed_time": settings.sandbox_timeout_seconds + 5,
                "error": f"执行超时(>{settings.sandbox_timeout_seconds}s，墙钟)",
                "stdout": "", "result": None, "charts": []}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "exit_code": None, "signal": None,
                "error": f"沙箱异常: {e}", "stdout": "",
                "result": None, "charts": []}
