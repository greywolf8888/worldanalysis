"""Prepare and verify the project-owned DeepSeek Harness instance."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
import re
import shutil
from stat import FILE_ATTRIBUTE_REPARSE_POINT
import subprocess
import sys
import tarfile
from threading import Thread
import time
from urllib.parse import urljoin, urlsplit


PROJECT = Path(__file__).resolve(strict=True).parents[1]
ROOT = PROJECT / ".runtime/dsh-worldanalysis"
RUNTIME = ROOT / "runtime"
HOME_DIR = ROOT / "home"
PROFILE = "worldanalysis-dedicated"
PROFILE_MANIFEST = HOME_DIR / "profiles" / PROFILE / "package.json"
PROFILE_BUNDLES = {"@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "dsh-mcp-bridge-server"}
MARKET_BUNDLE = "@deepseek-ai/dsh-experimental-plugin-market-bundle"
BUNDLE = PROJECT / ".runtime/v4-dsh-bridge-install/dsh-mcp-bridge-server-1.1.0.tgz"
BUNDLE_SHA256S = frozenset({
    "9c4633e05295a5393c3470985b99fe60280a071849b3d839b9611411805d7044",
    "1eb8ca7284f9bfca4d4e0c801f3e7e252e4a72d55592bfb29ceb03d4fa80e2b3",
})
PLUGIN = ROOT / "plugins/dsh-mcp-bridge"
BRIDGE = ROOT / "bridge"
DISCOVERY = BRIDGE / "mcp-bridge.json"
DESKTOP_MANIFEST = HOME_DIR / "profiles/desktop/package.json"
DESKTOP_DISCOVERY = ROOT / "desktop-bridge/mcp-bridge.json"
DESKTOP_READBACK_BINDING = ROOT / "desktop-session-readback.json"
BROWSER_ELECTRON_VERSION = "44.10.2"
BROWSER_ELECTRON_ROOT = ROOT / "browser-runtime" / f"electron-{BROWSER_ELECTRON_VERSION}"
BROWSER_ELECTRON_RECORD = BROWSER_ELECTRON_ROOT.with_name(f"{BROWSER_ELECTRON_ROOT.name}.json")
CHECKPOINT = ROOT / "readiness.json"
ISOLATION_PATCH = ROOT / "config/runtime-isolation.yml"
DESKTOP_EXE = "DeepSeek Harness.exe"
DESKTOP_PUBLISHER = "Hangzhou DeepSeek Artificial Intelligence Co., Ltd."
CLI_ASAR = "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
INTEGRITY_FILES = (DESKTOP_EXE, "resources/app.asar")
MAX_RESPONSE = 1_048_576
WEB_LAUNCH = re.compile(rb"dsh web: (http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+)")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
DEVICE_NAME = re.compile(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", re.IGNORECASE)
DSH_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def exact(path):
    info = path.lstat()
    if (path.resolve(strict=True) != path or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("项目运行路径包含重解析点")
    return path


def project_directory(path):
    if not path.is_relative_to(PROJECT):
        raise ValueError("运行数据必须位于 Worldanalysis 内")
    relative = path.relative_to(PROJECT)
    for parent in (*reversed(relative.parents), relative):
        current = PROJECT / parent
        current.mkdir(exist_ok=True)
        if not exact(current).is_dir():
            raise ValueError("运行目录不可用")
    return path


def read_record_bytes(path):
    with exact(path).open("rb") as stream:
        raw = stream.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("运行记录超出读取范围")
    return raw


def read_json(path):
    return json.loads(read_record_bytes(path))


def write_json(path, value):
    project_directory(path.parent)
    if path.exists():
        exact(path)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp.replace(path)


def digest(path):
    with exact(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def launcher_lock():
    import msvcrt

    path = project_directory(ROOT) / "launcher.lock"
    if path.exists():
        exact(path)
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("专用 DSH 另一项准备或验证正在执行") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def signed_distribution(source):
    executable = exact(source / DESKTOP_EXE)
    env = {**os.environ, "WORLDANALYSIS_DSH_DISTRIBUTION_EXE": str(executable)}
    command = (
        "$s=Get-AuthenticodeSignature -FilePath $env:WORLDANALYSIS_DSH_DISTRIBUTION_EXE\n"
        "[pscustomobject]@{valid=($s.Status -eq 'Valid');subject=$s.SignerCertificate.Subject;"
        "version=(Get-Item -LiteralPath $env:WORLDANALYSIS_DSH_DISTRIBUTION_EXE).VersionInfo.ProductVersion}"
        " | ConvertTo-Json -Compress"
    )
    powershell = shutil.which("pwsh") or "powershell.exe"
    output = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                            env=env, capture_output=True, text=True, timeout=25, check=True)
    signer = json.loads(output.stdout)
    if signer.get("valid") is not True or DESKTOP_PUBLISHER not in (signer.get("subject") or ""):
        raise ValueError("请提供已安装且签名有效的 DeepSeek Harness 官方发行目录")
    return signer["version"]


def distribution_files(source):
    for item in source.iterdir():
        if item.name in {"locales", "resources"}:
            exact(item)
            for child in item.rglob("*"):
                exact(child)
                if child.is_file() and child.name not in {"app-update.yml", "elevate.exe"}:
                    yield child
        elif item.is_file() and not item.name.startswith("Uninstall"):
            yield exact(item)


def copy_distribution(source):
    source = exact(source.resolve(strict=True))
    version = signed_distribution(source)
    destination = project_directory(RUNTIME)
    if (ROOT / "runtime.json").exists():
        existing = check_runtime()
        if existing["version"] == version and all(
                digest(source / name) == existing["files_sha256"][name] for name in INTEGRITY_FILES):
            return existing
        raise ValueError("专用运行目录已有不同版本，请先对照升级记录")
    files = size = 0
    for item in distribution_files(source):
        target = destination / item.relative_to(source)
        project_directory(target.parent)
        length = item.stat().st_size
        if target.exists():
            if not exact(target).is_file() or digest(item) != digest(target):
                raise ValueError("专用运行目录已有不同版本，请先对照升级记录")
        else:
            shutil.copy2(item, target)
        files += 1
        size += length
    record = {"version": version, "files": files, "bytes": size,
              "files_sha256": {name: digest(destination / name) for name in INTEGRITY_FILES}}
    write_json(ROOT / "runtime.json", record)
    return record


def stage_plugin():
    if digest(BUNDLE) not in BUNDLE_SHA256S:
        raise ValueError("项目已有 MCP 插件包的指纹不匹配")
    project_directory(PLUGIN)
    with tarfile.open(exact(BUNDLE)) as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "package" or any(part in {"..", "."} or "\\" in part or ":" in part for part in parts):
                raise ValueError("插件包的路径无效")
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2:
                raise ValueError("插件包包含不受支持的项目")
            # The upstream patch targets the general instance. Use the dedicated root instead.
            target = PLUGIN.joinpath(*parts[1:])
            if target.name == "cordis.patch.yml":
                continue
            project_directory(target.parent)
            if target.exists():
                exact(target)
            target.write_bytes(bundle.extractfile(member).read())
    project_directory(BRIDGE)
    write_json(PLUGIN / "cordis.patch.yml", [{"insert": [{"id": "dsh-mcp-bridge", "name": "dsh-mcp-bridge-server",
        "config": {"host": "127.0.0.1", "port": 0, "interactionMode": "ui", "exposeSubagents": False,
                   "exposeZcodeTools": False, "discoveryPath": DISCOVERY.as_posix(),
                   "statePath": (BRIDGE / "mcp-bridge-state.json").as_posix()}}]}])


def cli(*args):
    return [str(exact(RUNTIME / DESKTOP_EXE)), "--expose-internals", str(RUNTIME / CLI_ASAR), *args]


def browser_electron():
    if not BROWSER_ELECTRON_RECORD.exists():
        return None
    try:
        receipt = read_json(BROWSER_ELECTRON_RECORD)
        if (receipt["version"] != BROWSER_ELECTRON_VERSION
                or Path(receipt["destination"]) != BROWSER_ELECTRON_ROOT):
            return None
        program = BROWSER_ELECTRON_ROOT / "electron.exe"
        if receipt.get("verified_location") == "original_installed":
            original = (Path.home() / ".dsh/profiles/desktop/node_modules/electron/dist").resolve(strict=True)
            if original != Path(receipt["source"]):
                return None
            program = original / "electron.exe"
        elif receipt.get("verified_location") != "project_copy":
            return None
        return program if digest(program) == receipt["files"].get("electron.exe") else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def runtime_environment():
    system_keys = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "OS", "PROCESSOR_ARCHITECTURE",
                   "NUMBER_OF_PROCESSORS", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
    env = {key: value for key, value in os.environ.items() if key.upper() in system_keys}
    npm_config = project_directory(ROOT / "config") / "npmrc"
    if not npm_config.exists():
        with npm_config.open("x", encoding="utf-8") as stream:
            stream.write("update-notifier=false\n")
    temporary = str(project_directory(ROOT / "temp"))
    env.update(DSH_HOME=str(project_directory(HOME_DIR)), ELECTRON_RUN_AS_NODE="1", TEMP=temporary, TMP=temporary,
               npm_config_userconfig=str(exact(npm_config)),
               npm_config_cache=str(project_directory(ROOT / "npm-cache")))
    if browser := browser_electron():
        env["ELECTRON_PATH"] = str(browser)
    return env


def run_cli(name, *args):
    log = project_directory(ROOT / "logs") / f"{name}.log"
    if log.exists():
        exact(log)
    with log.open("wb") as stream:
        outcome = subprocess.run(cli(*args), env=runtime_environment(), cwd=PROJECT,
                                 stdout=stream, stderr=subprocess.STDOUT, timeout=90,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if outcome.returncode:
        raise ValueError(f"专用 DSH {name} 失败，日志在 {log.as_posix()}")


def check_runtime():
    record = read_json(ROOT / "runtime.json")
    if any(digest(RUNTIME / name) != record["files_sha256"][name] for name in INTEGRITY_FILES):
        raise ValueError("专用 DSH 运行发行件已变化，请核对版本后重试")
    return record


def matching_installed_desktop(fingerprints=None):
    import winreg

    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, uninstall)
        except FileNotFoundError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                        name = winreg.QueryValueEx(entry, "DisplayName")[0]
                        if name != "DeepSeek Harness":
                            continue
                        publisher = winreg.QueryValueEx(entry, "Publisher")[0]
                        location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                    if publisher != DESKTOP_PUBLISHER or not isinstance(location, str) or not Path(location).is_absolute():
                        continue
                    folder = Path(location).resolve(strict=True)
                    if fingerprints is None:
                        signed_distribution(folder)
                        exact(folder / "resources/app.asar")
                    elif any(digest(folder / item) != fingerprints[item] for item in INTEGRITY_FILES):
                        continue
                    return folder
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
    return None


def start_desktop():
    # The current Windows installation can start the registered build when the identical portable copy exits early.
    if (ROOT / "runtime.json").exists():
        runtime = check_runtime()
        folder = matching_installed_desktop(runtime["files_sha256"]) or RUNTIME
    else:
        folder = matching_installed_desktop()
        if folder is None:
            raise ValueError("请先安装并打开官方 DeepSeek Harness 桌面版，完成 Windows 安装登记。")
    executable = exact(folder / DESKTOP_EXE)
    desktop_data = project_directory(ROOT / "desktop-user-data")
    logs = project_directory(ROOT / "logs")
    log = logs / "desktop-launcher.log"
    if log.exists():
        exact(log)
    env = runtime_environment()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["DSH_DESKTOP_DIAGNOSTIC_FILE"] = str(logs / "desktop-startup.txt")
    env["DSH_BAIDU_NETDISK_CREDENTIAL_DIR"] = str(project_directory(ROOT / "desktop-netdisk-credentials"))
    env["DSH_INNER_BROWSER_STATE_DIR"] = str(project_directory(ROOT / "desktop-inner-browser"))
    env["BSK_HOME"] = str(project_directory(ROOT / "desktop-browser-skill"))
    with log.open("a", encoding="utf-8") as output:
        process = subprocess.Popen([str(executable), f"--user-data-dir={desktop_data}"], cwd=folder, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        output.write(f"{stamp} 已启动 {process.pid} {executable}\n")
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            output.flush()
            helper = exact(PROJECT / "_tools/dsh_desktop_welcome.py")
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(helper),
                                     str(process.pid), str(executable)],
                                    cwd=PROJECT, env=env, capture_output=True, text=True, timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            readback = result.stdout.strip() or "readback_unavailable"
            output.write(f"{stamp} 专用工作区 {process.pid}: {readback}\n")
            if result.returncode or readback not in {"workspace_after_skip", "workspace_ready", "process_exited"}:
                raise ValueError(f"专用 DSH 未能自动进入工作区。启动记录位于 {log}")
            if process.poll() not in (None, 0):
                raise ValueError(f"专用 DSH 启动后退出。启动记录位于 {log}")
            return 0
        if code:
            output.write(f"{stamp} 启动退出 0x{code:08X}\n")
            raise ValueError(f"DSH 未能打开。启动记录位于 {log}")
    return 0


def prepare():
    if not PROFILE_MANIFEST.exists():
        run_cli("profile-bootstrap", "--profile", PROFILE, "--from-default-profile", "web", "--dump-config")
    profile = read_json(PROFILE_MANIFEST)
    if "dsh-mcp-bridge-server" not in profile["dsh"]["profile"]["bundles"]:
        store = project_directory(ROOT / "pnpm-store")
        run_cli("plugin-install", "plugin", "--profile", PROFILE, "add", "file:" + PLUGIN.as_posix(),
                "--offline", "--store-dir", str(store))
        profile = read_json(PROFILE_MANIFEST)
    if "dsh-mcp-bridge-server" not in profile["dsh"]["profile"]["bundles"]:
        raise ValueError("专用 Profile 缺少 MCP 插件")
    spill = project_directory(ROOT / "spill")
    write_json(ISOLATION_PATCH, [{"id": "spill-local", "config": {"root": spill.as_posix(), "cleanupPeriodDays": 0}}])


def configure_market(enabled):
    check_runtime()
    manifest = read_json(PROFILE_MANIFEST)
    profile = manifest["dsh"]["profile"]
    before = profile["bundles"]
    if not PROFILE_BUNDLES.issubset(before):
        raise ValueError("请先准备专用 DSH Profile")
    after = [bundle for bundle in before if bundle != MARKET_BUNDLE]
    if enabled:
        after.append(MARKET_BUNDLE)
    if after != before:
        profile["bundles"] = after
        write_json(PROFILE_MANIFEST, manifest)
    try:
        composed = subprocess.run(cli("--profile", PROFILE, "--patch", str(exact(ISOLATION_PATCH)), "--dump-config"),
                                  env=runtime_environment(), cwd=PROJECT, capture_output=True, timeout=25, check=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ids = set(re.findall(rb"^\s*- id: ([a-z0-9-]+)\s*$", composed.stdout, re.MULTILINE))
        market_ids = {b"plugin-market", b"ui-plugin-market"}
        composed_market = market_ids.issubset(ids) if enabled else not (market_ids & ids)
        if not composed_market or b"preset-cordis" not in ids:
            raise ValueError("官方配置组合未读回预期的插件市场和创造预设")
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        if after != before:
            profile["bundles"] = before
            write_json(PROFILE_MANIFEST, manifest)
        raise
    return {"profile": PROFILE, "market_enabled": enabled, "creator_preset": "cordis", "composition_verified": True}


def endpoint(record):
    url = urlsplit(record["url"])
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path != "/mcp"
            or url.username or url.password or url.query or url.fragment or type(record.get("pid")) is not int):
        raise ValueError("专用 DSH 发现记录的地址无效")
    return url.port


def request(port, method, route, headers=None, body=None, *, timeout=3):
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, route, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("MCP 返回超出读取范围")
        return response.status, {key.casefold(): value for key, value in response.headers.items()}, raw
    finally:
        connection.close()


def bridge_identity(record, *, timeout=3):
    port = endpoint(record)
    instance = record.get("instanceId")
    if not isinstance(instance, str) or not instance or record.get("running") is not True or record["pid"] <= 0:
        raise ValueError("专用 DSH 当前未报告运行")
    status, _, raw = request(port, "GET", "/healthz", timeout=timeout)
    health = json.loads(raw)
    if status != 200 or (health.get("pid"), health.get("instanceId")) != (record["pid"], instance):
        raise ValueError("专用 DSH 的发现记录和健康状态不一致")
    return port


def bridge_probe(record, *, session_id=None, deadline=None):
    def remaining():
        if deadline is None:
            return 3
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError("专用桌面读回超时")
        return timeout

    port = bridge_identity(record, timeout=remaining())
    token = record.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{16,}", token):
        raise ValueError("专用 DSH 发现记录缺少可核对的本地授权")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(serial, method, params, *, unauthenticated=False):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if serial is not None:
            body["id"] = serial
        status, response_headers, raw = request(port, "POST", "/mcp", headers, json.dumps(body).encode("utf-8"),
                                                timeout=remaining())
        if unauthenticated:
            if status != 401:
                raise ValueError("隔离 MCP 未拒绝无凭据请求")
            return None
        if serial is None and status in {200, 202, 204}:
            return None
        reply = json.loads(raw)
        if status != 200 or reply.get("id") != serial or "error" in reply:
            raise ValueError("专用 MCP 握手未完成")
        transport = response_headers.get("mcp-session-id")
        if transport:
            headers["Mcp-Session-Id"] = transport
        return reply["result"]

    initialise = {"protocolVersion": "2025-11-25", "capabilities": {},
                  "clientInfo": {"name": "worldanalysis-dedicated-verification", "version": "1"}}
    post(1, "initialize", initialise, unauthenticated=True)
    headers["Authorization"] = "Bearer " + token
    details = post(1, "initialize", initialise)
    headers["MCP-Protocol-Version"] = details["protocolVersion"]
    post(None, "notifications/initialized", {})
    catalog = post(2, "tools/list", {})
    tools = {item["name"] for item in catalog["tools"]}
    if "dsh_list_sessions" not in tools or "dsh_list_workspaces" not in tools:
        raise ValueError("专用 MCP 没有实际 DSH 会话工具")
    observed = post(3, "tools/call", {"name": "dsh_list_sessions", "arguments": {}})
    if observed.get("isError") is not False:
        raise ValueError("专用 DSH Host 的会话读回未通过")
    sessions = observed.get("structuredContent")
    if isinstance(sessions, dict) and isinstance(sessions.get("sessions"), list):
        sessions = sessions["sessions"]
    if not isinstance(sessions, list):
        raise ValueError("专用 DSH 会话读回格式不匹配")
    result = {"loopback": True, "unauthenticated_status": 401, "protocol": details["protocolVersion"],
              "tools": len(tools), "session_readback": len(sessions)}
    if session_id is not None:
        observed = post(4, "tools/call", {"name": "dsh_get_session", "arguments": {"sessionId": session_id}})
        session = observed.get("structuredContent")
        if observed.get("isError") is not False or not isinstance(session, dict):
            raise ValueError("专用桌面未返回已配置会话")
        result["session"] = session
    return result


def await_bridge(process, log):
    for _ in range(90):
        if process.poll() is not None:
            raise ValueError(f"专用 DSH Host 提前退出，日志在 {log.as_posix()}")
        try:
            entry = read_json(DISCOVERY)
            if entry.get("running") is True and entry.get("pid") == process.pid:
                return bridge_probe(entry)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.25)
    raise ValueError(f"没有读回专用 DSH Host 的 MCP 服务，日志在 {log.as_posix()}")


def log_host_output(source, destination, launch):
    with source:
        for line in source:
            matched = WEB_LAUNCH.search(line)
            if matched:
                launch.put(matched.group(1).decode("ascii"))
                line = b"dsh web: local launch credential received\n"
            destination.write(line)
            destination.flush()


def web_readback(location, requested_port):
    url = urlsplit(location)
    if (url.hostname != "127.0.0.1" or url.scheme != "http" or url.path != "/"
            or not url.port or (requested_port and url.port != requested_port)):
        raise ValueError("专用 DSH 界面地址不匹配")
    status, headers, body = request(url.port, "GET", f"{url.path}?{url.query}")
    if status in {302, 303}:
        destination = headers.get("location", "")
        landing = urlsplit(urljoin(location, destination))
        cookie = headers.get("set-cookie", "").split(";", 1)[0]
        if (not destination or (landing.scheme, landing.netloc) != (url.scheme, url.netloc)
                or not landing.path.startswith("/") or not cookie):
            raise ValueError("专用 DSH 界面登录读回不匹配")
        route = f"{landing.path}?{landing.query}" if landing.query else landing.path
        status, headers, body = request(url.port, "GET", route, {"Cookie": cookie})
    if status != 200 or "text/html" not in headers.get("content-type", "") or b"<html" not in body.lower():
        raise ValueError("专用 DSH 界面未返回可读的页面")
    return status


@contextmanager
def running_host(web_port, *, open_browser=False):
    runtime = check_runtime()
    stage_plugin()
    prepare()
    log = project_directory(ROOT / "logs") / "isolated-host.log"
    if log.exists():
        exact(log)
    with log.open("wb") as stream:
        args = ("--profile", PROFILE, "--patch", str(exact(ISOLATION_PATCH)), "--host", "127.0.0.1", "--port", str(web_port))
        process = subprocess.Popen(cli(*args, *(() if open_browser else ("--no-open",))),
                                   env=runtime_environment(), cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        launch = Queue()
        reader = Thread(target=log_host_output, args=(process.stdout, stream, launch), daemon=True)
        reader.start()
        try:
            result = await_bridge(process, log)
            try:
                location = launch.get(timeout=5)
            except Empty as error:
                raise ValueError(f"专用 DSH 界面未完成监听，日志在 {log.as_posix()}") from error
            if not open_browser:
                result["web_status"] = web_readback(location, web_port)
            yield runtime, process, result
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=8)
            reader.join(timeout=3)


def verify():
    with running_host(0) as (runtime, _, result):
        pass
    checkpoint = {"profile": PROFILE, "distribution": runtime["version"], "plugin_sha256": digest(BUNDLE),
                  "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "production_execution": "disabled", "isolated_host": "stopped", "readback": result}
    write_json(CHECKPOINT, checkpoint)
    return checkpoint


def intake_binding(frozen=None):
    from workspace_paths import path_bindings

    version = frozen.get('version_id') if isinstance(frozen, dict) else None
    if frozen is not None and (not isinstance(version, str) or set(frozen) != {'version_id', 'root'}):
        raise ValueError('收件位置需要固定目录和版本')
    values = path_bindings(workspace=PROJECT, version_id=version)
    value = values['paths']['intake']
    root = Path(value) if value else None
    if root is None or not root.is_relative_to(PROJECT) or root == PROJECT or not exact(root).is_dir():
        raise ValueError('任务收件目录不可用')
    binding = {'version_id': values['version_id'], 'root': root.as_posix()}
    if frozen is not None and frozen != binding:
        raise ValueError('任务固定收件位置与登记不一致')
    return binding


def install_v4_import_path():
    branch = PROJECT / '_ingest/wangzjk-to-pan'
    sys.path[:0] = [str(branch), str(branch / 'v4-review')]
    return branch


def preflight_task(task_id):
    if not TASK_ID.fullmatch(task_id) or DEVICE_NAME.fullmatch(task_id):
        raise ValueError("任务编号无效")
    name = f"{task_id}.json"
    receipt_path = PROJECT / "_exchange/dsh/receipts" / name
    task_path = PROJECT / "_exchange/control/tasks" / name
    try:
        raw = read_record_bytes(task_path)
    except FileNotFoundError:
        return {"task_id": task_id, "state": "missing"}
    task = json.loads(raw)
    received_path = task.get("receipt_path")
    if (task.get("schema_version") != 1 or task.get("record_type") != "control_task"
            or task.get("producer") != "codex-control" or task.get("plan_version") != "V4"
            or task.get("task_id") != task_id or task.get("demo") is True
            or not isinstance(received_path, str) or Path(received_path) != receipt_path):
        raise ValueError("共享任务身份或回执路径不匹配")
    install_v4_import_path()
    planned = (task.get('inputs') or {}).get('intake_binding')
    intake = intake_binding(planned if planned is not None else
                            {'version_id': 'legacy', 'root': (PROJECT / '分析目标下载地址').as_posix()})
    receipt = "missing"
    if receipt_path.exists():
        row = read_json(receipt_path)
        receipt = "unmatched"
        if (row.get("task_id") == task_id and row.get("task_sha256") == hashlib.sha256(raw).hexdigest()
                and row.get("session_id") == task.get("target_session_id")):
            receipt = "task_identity_matched"
    return {"task_id": task_id, "state": "ready" if task.get("state") == "ready" else "not_ready",
            "authorization": "reference_present_unverified" if task.get("authorization_ref") else "missing",
            "receipt": receipt, "intake": intake, "intake_frozen": planned is not None,
            "dedicated_session_bound": (DESKTOP_READBACK_BINDING.is_file()
                                        and read_json(DESKTOP_READBACK_BINDING).get('session_id') == task.get('target_session_id'))}


def desktop_session_summary(session, binding):
    projections = session["projections"]
    if (session.get("sessionId") != binding["session_id"] or session.get("workspaceId") != binding["workspace_id"]
            or projections.get("title") != binding["title"]
            or any(Path(session.get(key) or "") != PROJECT for key in ("workspacePath", "cwd"))
            or Path(projections.get("workingDirectory") or "") != PROJECT):
        return {"state": "identity_mismatch", "identity_verified": False, "read_only_ready": False}
    state = session["state"]
    permissions = projections.get("permissions") or {}
    preset = permissions.get("currentValue", permissions.get("preset"))
    role = (session.get("live") is True and session.get("interactionMode") == "ui"
            and preset == permissions.get("sandbox", preset) == "danger-full-access"
            and permissions.get("approval", "never") == "never")
    pending = (state.get("pendingInput"), session.get("pendingInteractions"))
    busy = (state.get("queue"), session.get("activeWorkflowRuns", []))
    if "desktopPendingEvents" in session:
        pending += (session["desktopPendingEvents"],)
    cursor = state.get("cursor")
    if not role:
        status = "role_unverified"
    elif not all(isinstance(rows, list) for rows in (*pending, *busy)):
        status = "readback_incomplete"
    elif any(pending):
        status = "awaiting_user"
    elif (any(busy) or state.get("running") is not False or session.get("turnSettled") is not True
          or (projections.get("plan") or {}).get("active") is not False):
        status = "busy"
    elif type(cursor) is not int or cursor < 0:
        status = "readback_incomplete"
    else:
        status = "ready"
    return {"state": status, "identity_verified": True, "read_only_ready": status == "ready",
            "role_verified": role, "cursor": cursor if type(cursor) is int and cursor >= 0 else None}


def desktop_preflight():
    bridge = {"connected": False, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    session = {"state": "unconfigured", "identity_verified": False, "read_only_ready": False}
    result = {"profile": "desktop", "profile_ready": False, "bridge": bridge, "session": session}
    try:
        bundles = read_json(DESKTOP_MANIFEST)["dsh"]["profile"]["bundles"]
        result["profile_ready"] = PROFILE_BUNDLES.issubset(bundles)
        if not result["profile_ready"]:
            return result
        record = read_json(DESKTOP_DISCOVERY)
        bridge_identity(record, timeout=0.5)
        bridge["connected"] = True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
        session["state"] = "bridge_unavailable"
        return result
    try:
        binding = read_json(DESKTOP_READBACK_BINDING)
        if (binding.get("schema_version") != 1 or binding.get("purpose") != "read_only_preflight"
                or Path(binding["project_path"]) != PROJECT or not re.fullmatch(DSH_UUID, binding["workspace_id"])
                or not re.fullmatch("session-" + DSH_UUID, binding["session_id"])
                or not isinstance(binding["title"], str) or not 0 < len(binding["title"]) <= 128):
            raise ValueError("只读会话绑定无效")
    except FileNotFoundError:
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        session["state"] = "invalid_binding"
        return result
    try:
        observed = bridge_probe(record, session_id=binding["session_id"], deadline=time.monotonic() + 1.5)
        current = read_json(DESKTOP_DISCOVERY)
        identity = ("url", "pid", "instanceId", "running")
        if any(current.get(key) != record.get(key) for key in identity):
            bridge["connected"] = False
            raise ValueError("专用桌面桥接身份在读回期间变化")
        result["session"] = desktop_session_summary(observed["session"], binding)
        bridge["authenticated_readback"] = True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
        session["state"] = "readback_failed"
    return result


def preflight(task_id=None):
    bundles = set()
    if PROFILE_MANIFEST.exists():
        bundles = set(read_json(PROFILE_MANIFEST)["dsh"]["profile"]["bundles"])
    profile_ready = PROFILE_BUNDLES.issubset(bundles)
    checkpoint = read_json(CHECKPOINT) if CHECKPOINT.exists() else {}
    last_verified_at = checkpoint.get("verified_at") if checkpoint.get("profile") == PROFILE else None
    connected = False
    if profile_ready and DISCOVERY.exists():
        try:
            bridge_identity(read_json(DISCOVERY))
            connected = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
            pass
    result = {"schema_version": 1, "profile": PROFILE, "profile_ready": profile_ready,
              "bridge": {"connected": connected, "last_verified_at": last_verified_at},
              "desktop": desktop_preflight(),
              "profile_market_enabled": MARKET_BUNDLE in bundles,
              "session_binding": "unbound", "netdisk_account": "unverified",
              "production_execution": "disabled", "claim_enabled": False, "download_enabled": False}
    if task_id is not None:
        result["handoff"] = preflight_task(task_id)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("prepare", help="从本机签名发行件准备项目内运行目录")
    install.add_argument("distribution", type=Path)
    commands.add_parser("verify", help="启动隔离服务，核对 MCP 和本机界面后退出")
    serve = commands.add_parser("serve", help="在当前终端运行专用界面，使用 Ctrl+C 退出")
    serve.add_argument("--port", type=int, default=8781)
    serve.add_argument("--open", action="store_true", help="通过官方启动器打开已认证的本机界面")
    commands.add_parser("desktop", help="打开 Worldanalysis 专用 Windows 桌面程序")
    commands.add_parser("status", help="查看隔离验证记录和正式接管状态")
    market = commands.add_parser("market", help="启用或关闭专用 Profile 的内置插件市场")
    market.add_argument("state", choices=("enable", "disable"))
    check = commands.add_parser("preflight", help="只读检查专用桥接、正式派发门禁及共享任务")
    check.add_argument("--task-id")
    commands.add_parser('intake-binding', help='只读取得当前收件目录及版本，供新任务保存')
    args = parser.parse_args()
    try:
        if args.action == "status":
            record = read_json(CHECKPOINT) if CHECKPOINT.exists() else {"production_execution": "disabled", "readback": None}
        elif args.action == "preflight":
            record = preflight(args.task_id)
        elif args.action == 'intake-binding':
            install_v4_import_path()
            record = intake_binding()
        elif args.action == "desktop":
            return start_desktop()
        else:
            with launcher_lock():
                if args.action == "prepare":
                    record = copy_distribution(args.distribution)
                    stage_plugin()
                    prepare()
                elif args.action == "market":
                    record = configure_market(args.state == "enable")
                elif args.action == "serve":
                    if not 1024 <= args.port <= 65535:
                        raise ValueError("请指定 1024 至 65535 之间的端口")
                    with running_host(args.port, open_browser=args.open) as (_, process, _):
                        print(f"专用 DSH 已监听本机 {args.port} 端口。", flush=True)
                        return process.wait()
                else:
                    record = verify()
        print(json.dumps(record, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, subprocess.SubprocessError) as error:
        if args.action == "desktop" and sys.stderr is None:
            from ctypes import windll

            windll.user32.MessageBoxW(None, str(error), "Worldanalysis DSH", 0x10)
        else:
            print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
