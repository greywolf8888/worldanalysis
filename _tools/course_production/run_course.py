"""Run course tools with per-process scratch and an optional full-result log."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


CHECKOUT = Path(__file__).resolve().parents[2]
OUTPUT_LIMIT = 8192


def course_runtime():
    # A linked checkout under the project's .runtime uses the shared runtime, not a new model copy.
    if CHECKOUT.parent.name == ".runtime":
        return CHECKOUT.parent
    configured = os.environ.get("WORLDANALYSIS_COURSE_RUNTIME")
    return Path(configured).resolve() if configured else CHECKOUT / ".runtime"


def child_environment(runtime, scratch):
    env = dict(os.environ)
    env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
               WORLDANALYSIS_COURSE_RUNTIME=str(runtime), PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    packages = str(runtime / "site-packages")
    if Path(packages).is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([packages] + ([existing] if existing else []))
    return env


def remove_run_scratch(scratch, parent):
    # Only the newly created run directory may be removed, and only after the child has exited.
    if not scratch.exists():
        return True
    if (not scratch.name.startswith("course-run-") or scratch.resolve().parent != parent.resolve()
            or scratch.is_symlink() or scratch.is_junction()):
        return False
    try:
        shutil.rmtree(scratch)
    except OSError:
        return False
    return True


def show_output(label, path):
    size = path.stat().st_size
    if not size:
        return
    # Long JSON, image payloads and diagnostic dumps remain complete in the log on the data disk.
    if size <= OUTPUT_LIMIT:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "data:image/" not in text and "data:audio/" not in text:
            print(f"{label}:\n{text.rstrip()}", flush=True)
            return
    print(f"{label}: {size} bytes; complete result: {path}", flush=True)


def execute(command, runtime, compact=False):
    runtime = runtime.resolve()
    temp_root = runtime / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    if temp_root.resolve().parent != runtime or temp_root.is_symlink() or temp_root.is_junction():
        raise ValueError(f"临时目录不在项目运行目录内：{temp_root}")
    scratch = Path(tempfile.mkdtemp(prefix="course-run-", dir=temp_root))
    env = child_environment(runtime, scratch)
    started = time.monotonic()
    code = 1
    finished = False
    log_dir = None
    try:
        if compact:
            log_root = runtime / "agent-runs"
            log_root.mkdir(parents=True, exist_ok=True)
            if log_root.resolve().parent != runtime or log_root.is_symlink() or log_root.is_junction():
                raise ValueError(f"日志目录不在项目运行目录内：{log_root}")
            log_dir = log_root / scratch.name
            log_dir.mkdir(exist_ok=False)
            print(f"完整运行输出：{log_dir}", flush=True)
            with (log_dir / "stdout.log").open("wb") as stdout, (log_dir / "stderr.log").open("wb") as stderr:
                code = subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=False).returncode
        else:
            code = subprocess.run(command, env=env, check=False).returncode
        finished = True
    finally:
        removed = finished and code == 0 and remove_run_scratch(scratch, temp_root)
        if log_dir:
            report = {
                "exit_code": code if finished else None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "runtime": str(runtime),
                "scratch": None if removed else str(scratch),
                "outputs": {name: (log_dir / name).stat().st_size for name in ("stdout.log", "stderr.log")
                            if (log_dir / name).is_file()},
            }
            (log_dir / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not removed:
            print(f"保留本次临时目录：{scratch}", file=sys.stderr, flush=True)
    if compact:
        show_output("stdout", log_dir / "stdout.log")
        show_output("stderr", log_dir / "stderr.log")
        print(f"退出码 {code}；运行回执：{log_dir / 'receipt.json'}", flush=True)
    return code


def main(argv):
    compact = bool(argv and argv[0] == "--agent-compact")
    if compact:
        argv = argv[1:]
    script = Path(__file__).resolve().with_name("course.py")
    return execute([sys.executable, "-X", "utf8", "-B", str(script), *argv], course_runtime(), compact)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
