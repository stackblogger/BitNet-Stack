import codecs
import os
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, request, Response, stream_with_context, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="")

stop_event = threading.Event()
current_process = None

MODEL_PATH = os.getenv("MODEL_PATH", "/models/ggml-model-i2_s.gguf")
BITNET_DIR = os.getenv("BITNET_DIR", "/app/BitNet")
DEFAULT_SYSTEM = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant")

CNV_IDLE_SEC = float(os.getenv("CNV_IDLE_SEC", "0.45"))
CNV_MAX_SEC = float(os.getenv("CNV_MAX_SEC", "120"))
CNV_READ_CHUNK = int(os.getenv("CNV_READ_CHUNK", "256"))
CNV_STARTUP_DRAIN_MAX = float(os.getenv("CNV_STARTUP_DRAIN_MAX", "4"))
CNV_STARTUP_QUIET_COLD = float(os.getenv("CNV_STARTUP_QUIET", "0.85"))
CNV_STARTUP_QUIET_WARM = float(os.getenv("CNV_STARTUP_QUIET_WARM", "0.42"))
CNV_STARTUP_STRAGGLER_COLD = float(os.getenv("CNV_STARTUP_STRAGGLER_SEC", "0.55"))
CNV_STARTUP_STRAGGLER_WARM = float(os.getenv("CNV_STARTUP_STRAGGLER_WARM", "0.2"))
THREADS = os.getenv("THREADS", "4")
CTX_SIZE = os.getenv("CTX_SIZE", "2048")
TEMP = os.getenv("TEMP", "0.8")

cnv_sessions: dict = {}
cnv_global_lock = threading.Lock()
_startup_warm_lock = threading.Lock()
_cnv_process_startup_warmed = False


def sse_chunk(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def llama_cli_path() -> str:
    import platform

    base = Path(BITNET_DIR) / "build" / "bin"
    if platform.system() == "Windows":
        p = base / "Release" / "llama-cli.exe"
        if p.exists():
            return str(p)
        p = base / "llama-cli.exe"
        if p.exists():
            return str(p)
    return str(base / "llama-cli")


def llama_cmd(system_prompt: str) -> list:
    exe = llama_cli_path()
    core = [
        exe,
        "-m",
        MODEL_PATH,
        "-t",
        str(THREADS),
        "-p",
        system_prompt,
        "-ngl",
        "0",
        "-c",
        str(CTX_SIZE),
        "--temp",
        str(TEMP),
        "-b",
        "1",
        "-cnv",
    ]
    if shutil.which("stdbuf"):
        return ["stdbuf", "-o0", "-e0"] + core
    return core


def _stdout_reader(proc: subprocess.Popen, q: queue.Queue) -> None:
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        raw = proc.stdout
        while True:
            block = raw.read(CNV_READ_CHUNK)
            if not block:
                break
            text = dec.decode(block)
            if text:
                q.put(text)
        tail = dec.decode(b"", final=True)
        if tail:
            q.put(tail)
    finally:
        q.put(None)


def _startup_drain_stragglers(q: queue.Queue, budget_sec: float) -> None:
    deadline = time.monotonic() + budget_sec
    while time.monotonic() < deadline:
        drained = False
        try:
            while True:
                x = q.get_nowait()
                if x is None:
                    return
                drained = True
        except queue.Empty:
            pass
        if not drained:
            time.sleep(0.07)


def drain_cnv_startup_queue(q: queue.Queue) -> None:
    with _startup_warm_lock:
        warmed = _cnv_process_startup_warmed
    quiet_need = CNV_STARTUP_QUIET_WARM if warmed else CNV_STARTUP_QUIET_COLD
    straggler_budget = CNV_STARTUP_STRAGGLER_WARM if warmed else CNV_STARTUP_STRAGGLER_COLD
    try:
        end = time.monotonic() + CNV_STARTUP_DRAIN_MAX
        saw_any = False
        quiet_at = None
        while time.monotonic() < end:
            try:
                _ = q.get(timeout=0.12)
                if _ is None:
                    return
                saw_any = True
                quiet_at = None
            except queue.Empty:
                if not saw_any:
                    continue
                now = time.monotonic()
                if quiet_at is None:
                    quiet_at = now
                elif now - quiet_at >= quiet_need:
                    got_more = False
                    try:
                        while True:
                            x = q.get_nowait()
                            if x is None:
                                return
                            got_more = True
                    except queue.Empty:
                        pass
                    if got_more:
                        quiet_at = None
                        continue
                    return
    finally:
        _startup_drain_stragglers(q, straggler_budget)


def _terminate_session(data: dict) -> None:
    proc = data.get("proc")
    if not proc:
        return
    if proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass


def kill_cnv_session(session_id: str) -> None:
    with cnv_global_lock:
        data = cnv_sessions.pop(session_id, None)
    if data:
        _terminate_session(data)


def kill_all_cnv_sessions() -> None:
    with cnv_global_lock:
        items = list(cnv_sessions.items())
        cnv_sessions.clear()
    for _, data in items:
        _terminate_session(data)


def kill_current():
    global current_process
    p = current_process
    if not p:
        return
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/stop")
def stop():
    global current_process
    stop_event.set()
    body = request.get_json(silent=True) or {}
    if body.get("all"):
        kill_all_cnv_sessions()
        kill_current()
        current_process = None
    elif body.get("session_id"):
        kill_cnv_session(str(body["session_id"]))
    else:
        kill_all_cnv_sessions()
        kill_current()
        current_process = None
    stop_event.clear()
    return ("", 204)


@app.post("/cnv/init")
def cnv_init():
    global _cnv_process_startup_warmed
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id") or body.get("sessionId")
    system = (body.get("system") or body.get("system_prompt") or DEFAULT_SYSTEM).strip()
    if not session_id:
        return ({"error": "session_id required"}, 400)
    session_id = str(session_id)
    with cnv_global_lock:
        existing = cnv_sessions.get(session_id)
    if existing and existing["proc"].poll() is None:
        return {"ok": True, "reused": True}
    kill_cnv_session(session_id)
    cmd = llama_cmd(system)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BITNET_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
        )
    except Exception as e:
        return ({"error": str(e)}, 500)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_stdout_reader, args=(proc, q), daemon=True).start()
    with cnv_global_lock:
        cnv_sessions[session_id] = {
            "proc": proc,
            "q": q,
            "lock": threading.Lock(),
        }
    drain_cnv_startup_queue(q)
    if proc.poll() is None:
        with _startup_warm_lock:
            _cnv_process_startup_warmed = True
    return {"ok": True}


@app.post("/cnv/send")
def cnv_send():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id") or body.get("sessionId")
    text = (body.get("text") or body.get("message") or "").strip()
    if not session_id or not text:
        return ({"error": "session_id and text required"}, 400)
    session_id = str(session_id)
    with cnv_global_lock:
        data = cnv_sessions.get(session_id)
    if not data or data["proc"].poll() is not None:
        return ({"error": "session not ready; call POST /cnv/init"}, 409)

    proc = data["proc"]
    q = data["q"]
    lock = data["lock"]

    def generate():
        try:
            with lock:
                if stop_event.is_set():
                    yield sse_chunk({"text": ""})
                else:
                    proc.stdin.write((text + "\n").encode("utf-8"))
                    proc.stdin.flush()
        except BrokenPipeError:
            yield sse_chunk({"text": "[session closed]\n"})
            yield sse_chunk({"done": True})
            return
        started = time.monotonic()
        last_chunk_at = None
        while True:
            if stop_event.is_set():
                break
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                if last_chunk_at is None:
                    if time.monotonic() - started > CNV_MAX_SEC:
                        break
                    continue
                if time.monotonic() - last_chunk_at >= CNV_IDLE_SEC:
                    break
                continue
            if chunk is None:
                break
            last_chunk_at = time.monotonic()
            yield sse_chunk({"text": chunk})
        yield sse_chunk({"done": True})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat")
def chat():
    global current_process

    body = request.get_json(silent=True) or {}
    args = (body.get("args") or "").strip()
    messages = body.get("messages")
    prompt = (body.get("prompt") or "").strip()
    query = (body.get("query") or "").strip()

    if messages is not None:
        text_for_model = build_prompt_from_messages(messages)
        if not text_for_model:
            return ({"error": "messages must end with a user turn"}, 400)
    elif prompt:
        text_for_model = prompt
    elif query:
        text_for_model = f"{DEFAULT_SYSTEM}\n\nUser: {query}\n\nAssistant:\n"
    else:
        return ({"error": "missing messages, prompt, or query"}, 400)

    stop_event.clear()
    kill_current()

    command = [
        "python",
        "run_inference.py",
        "-m",
        MODEL_PATH,
        "-p",
        text_for_model,
        "-cnv",
    ]

    if args:
        command.extend(args.split())

    def generate():
        global current_process

        proc = subprocess.Popen(
            command,
            cwd=BITNET_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        current_process = proc

        try:
            for line in proc.stdout:
                if stop_event.is_set():
                    break
                yield sse_chunk({"text": line})
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            try:
                proc.stdout.close()
            except Exception:
                pass

            if current_process is proc:
                current_process = None

        yield sse_chunk({"done": True})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def build_prompt_from_messages(messages):
    if not isinstance(messages, list) or not messages:
        return None
    system = DEFAULT_SYSTEM
    ordered = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system = content
            continue
        if role in ("user", "assistant"):
            ordered.append((role, content))
    if not ordered or ordered[-1][0] != "user":
        return None
    lines = [system, ""]
    i = 0
    while i < len(ordered):
        role, content = ordered[i]
        if role != "user":
            i += 1
            continue
        lines.append(f"User: {content}")
        if i + 1 < len(ordered) and ordered[i + 1][0] == "assistant":
            lines.append(f"Assistant: {ordered[i + 1][1]}")
            i += 2
        else:
            lines.append("Assistant:")
            i += 1
    return "\n".join(lines)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
