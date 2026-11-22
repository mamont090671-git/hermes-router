#!/usr/bin/env python3
import os
import sys
import time
import signal
import logging
import threading
from urllib.parse import urlparse
import requests
from flask import Flask, request, Response

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("router")

# --- КОНФИГУРАЦИЯ (требуются env vars) ---
HOME_SERVER = os.environ["HOME_SERVER"]
LOCAL_LAPTOP = os.environ["LOCAL_LAPTOP"]

BASE_INTERVAL = float(os.environ.get("BASE_INTERVAL", "5.0"))
MAX_INTERVAL = float(os.environ.get("MAX_INTERVAL", "60.0"))
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "0.3"))
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "300"))
MAX_REQUEST_SIZE = int(os.environ.get("MAX_REQUEST_SIZE", "10485760")) # 10MB лимит (Пункт 7)

# --- ОБЩИЕ РЕСУРСЫ ---
_home_up = False
_local_up = False # Проверка доступности ноута (Пункт 10)
_status_lock = threading.Lock() # Один лок для защиты всех статусных переменных

_session = requests.Session()
_session.headers.update({"User-Agent": "hermes-router/3.0"})

_stop_event = threading.Event()
_health_thread = None

# --- ФОНОВЫЙ HEALTH-CHECK ---
def _health_check_loop():
    global _home_up, _local_up
    log.info("Health-check worker thread started.")
    
    current_interval = BASE_INTERVAL # Потокобезопасно, так как локально для воркера (Пункт 8)

    def _check_endpoint(url):
        try:
            resp = requests.get(f"{url}/v1/models", timeout=(CONNECT_TIMEOUT, 3.0), allow_redirects=True)
            return resp.status_code == 200
        except Exception:
            return False

    while not _stop_event.is_set():
        home_is_ok = _check_endpoint(HOME_SERVER)
        local_is_ok = _check_endpoint(LOCAL_LAPTOP)

        with _status_lock:
            # Логируем изменение состояния HOME
            if home_is_ok != _home_up:
                log.info("HOME (5090) state change: %s -> %s", "ONLINE" if _home_up else "OFFLINE", "ONLINE" if home_is_ok else "OFFLINE")
                _home_up = home_is_ok
            
            # Логируем изменение состояния LOCAL
            if local_is_ok != _local_up:
                log.info("LOCAL (Laptop) state change: %s -> %s", "ONLINE" if _local_up else "OFFLINE", "ONLINE" if local_is_ok else "OFFLINE")
                _local_up = local_is_ok

            # Управляем экспоненциальным откатом только по состоянию HOME
            if home_is_ok:
                current_interval = BASE_INTERVAL
            else:
                current_interval = min(current_interval * 2, MAX_INTERVAL)

        # Адаптивный неблокирующий сон (Пункт 3)
        start_sleep = time.time()
        while time.time() - start_sleep < current_interval:
            if _stop_event.is_set():
                break
            time.sleep(0.1)

def _start_health_worker():
    global _health_thread
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug: # (Пункт 4)
        if _health_thread is not None and _health_thread.is_alive():
            return
        _health_thread = threading.Thread(target=_health_check_loop, daemon=True)
        _health_thread.start()

# --- FLASK ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_SIZE # Аппаратная защита от OOM (Пункт 7)

@app.before_request
def _ensure_health_worker():
    _start_health_worker()

@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def route_api(path):
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    data = request.get_data() # Безопасно, так как Flask выбросит 413, если тело > MAX_CONTENT_LENGTH

    # Надежная сборка относительного пути через urlparse (Пункт 1)
    parsed_url = urlparse(request.url)
    url_path = parsed_url.path
    if parsed_url.query:
        url_path += f"?{parsed_url.query}"

    with _status_lock:
        home_up = _home_up
        local_up = _local_up

    if not home_up:
        if not local_up:
            log.critical("[ALL DOWN] Request failed. Both backends are dead: %s", url_path)
            return Response("Both backends unavailable. Retry later.", status=503, headers={"Retry-After": "5"})
        log.info("[OFFLINE] %s -> LOCAL_LAPTOP", url_path)
        return _forward(url_path, headers, data, LOCAL_LAPTOP, is_fallback_run=False)

    log.info("[ONLINE] %s -> HOME_SERVER (5090)", url_path)
    return _forward(url_path, headers, data, HOME_SERVER, is_fallback_run=False)

def _forward(url_path: str, headers: dict, data: bytes, target_base: str, is_fallback_run: bool) -> Response:
    target_url = f"{target_base}{url_path}"

    try:
        response = _session.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=data,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=True,
            allow_redirects=True, # Разрешаем редиректы (Пункт 3)
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        log.error("Forwarding failed to %s: %s", target_base, exc)
        return _handle_failure(url_path, headers, data, target_base, is_fallback_run)
    except Exception as exc:
        log.error("Unexpected error forwarding to %s: %s", target_base, exc)
        return Response(f"Forwarding error: {exc}", status=502)

    if response.status_code >= 500 or response.status_code == 429:
        log.warning("Backend error %d from %s", response.status_code, target_base)
        response.close()
        return _handle_failure(url_path, headers, data, target_base, is_fallback_run)

    return stream_response(response)

def _handle_failure(url_path: str, headers: dict, data: bytes, failed_base: str, is_fallback_run: bool) -> Response:
    """Плоская обработка ошибок вместо рекурсии (Пункт 5)"""
    global _home_up
    
    # Если упал домашний сервер, и это была первая попытка
    if failed_base == HOME_SERVER and not is_fallback_run:
        with _status_lock:
            _home_up = False
            local_up = _local_up
            
        if local_up:
            log.info("[FALLBACK] Switching to local due to in-flight error.")
            return _forward(url_path, headers, data, LOCAL_LAPTOP, is_fallback_run=True)

    log.critical("[ALL DOWN] Target failed and no functional fallback available for %s", url_path)
    return Response("Both backends unavailable. Retry later.", status=503, headers={"Retry-After": "5"}) # (Пункт 5)

def stream_response(response: requests.Response) -> Response:
    def generate():
        try:
            for chunk in response.iter_content(chunk_size=512):
                if chunk:
                    yield chunk
        except Exception as exc:
            log.error("Streaming error: %s", exc)
        finally:
            response.close()

    excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers = [
        (name, value) for name, value in response.headers.items()
        if name.lower() not in excluded_headers
    ]

    # Гарантируем корректный Content-Type для ИИ-клиентов (Пункт 6)
    content_type = response.headers.get("Content-Type", "application/json")

    return Response(
        generate(),
        status=response.status_code,
        headers=response_headers,
        content_type=content_type
    )

# --- МЯГКИЙ SHUTDOWN (Пункт 2) ---
def _graceful_shutdown(signum, frame):
    log.info("Received signal %s, performing graceful shutdown...", signal.Signals(signum).name)
    
    _stop_event.set()
    _session.close()
    
    # Гарантируем завершение хелс-чека перед выходом, чтобы избежать зависания процесса (Пункт 2)
    if _health_thread and _health_thread.is_alive():
        _health_thread.join(timeout=4.0)
    
    log.info("Router stopped cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

# --- MAIN ---
if __name__ == "__main__":
    log.info("Hermes Router v3.0 (Enterprise) starting on port 9999")
    app.run(host="0.0.0.0", port=9999, debug=False, use_reloader=False, threaded=True)
