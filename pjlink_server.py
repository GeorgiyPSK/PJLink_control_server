#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJLink ↔ HTTP мост с подробной отладкой.
Запуск: python pjlink_server.py
Порт:   9000
"""

import socket
import json
import logging
import time
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# --- Настройки ---
HTTP_PORT = 9001
PROJECTOR_PORT = 4352
CONNECT_TIMEOUT = 3
READ_TIMEOUT = 3

PROJECTORS = {
    "pro21": "192.168.12.21",
    "pro51": "192.168.12.51",
    "pro61": "192.168.12.61",
    "pro62": "192.168.12.62",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pjlink")

# Счётчик активных запросов
_active_requests = 0
_active_lock = threading.Lock()


def _inc_active():
    global _active_requests
    with _active_lock:
        _active_requests += 1
        return _active_requests


def _dec_active():
    global _active_requests
    with _active_lock:
        _active_requests -= 1
        return _active_requests


def _hexdump(data: bytes, prefix: str = "") -> str:
    if not data:
        return f"{prefix}(пусто)"
    hexs = " ".join(f"{b:02X}" for b in data)
    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{prefix}{hexs}  |{txt}|  ({len(data)} байт)"


# ---------- PJLink-клиент с полной отладкой ----------
def pjlink_command(ip: str, command: str) -> dict:
    """
    Отправляет команду PJLink проектору.
    Возвращает {"ok": bool, "response": str|None, "error": str|None}.
    """
    sock = None
    t0 = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((ip, PROJECTOR_PORT))
        except socket.timeout:
            log.warning(f"[{ip}] connect TIMEOUT ({time.time()-t0:.1f}s)")
            return {"ok": False, "response": None, "error": "connect timeout"}
        except ConnectionRefusedError:
            log.warning(f"[{ip}] connect REFUSED ({time.time()-t0:.1f}s)")
            return {"ok": False, "response": None, "error": "connection refused"}
        except OSError as e:
            log.warning(f"[{ip}] connect ERROR ({time.time()-t0:.1f}s): {e}")
            return {"ok": False, "response": None, "error": f"connect error: {e}"}

        # --- приветствие ---
        sock.settimeout(READ_TIMEOUT)
        greeting = b""
        try:
            while not greeting.endswith(b"\r"):
                chunk = sock.recv(64)
                if not chunk:
                    log.warning(f"[{ip}] закрытие до приветствия")
                    return {"ok": False, "response": None,
                            "error": "projector closed connection before greeting"}
                greeting += chunk
                if len(greeting) > 256:
                    log.warning(f"[{ip}] приветствие слишком длинное")
                    return {"ok": False, "response": None,
                            "error": "greeting too long"}
        except socket.timeout:
            log.warning(f"[{ip}] greeting TIMEOUT ({time.time()-t0:.1f}s)")
            return {"ok": False, "response": None, "error": "greeting timeout"}

        greeting_str = greeting.decode("ascii", errors="replace").strip()

        if greeting_str.startswith("PJLINK 0"):
            pass
        elif greeting_str.startswith("PJLINK 1"):
            log.warning(f"[{ip}] требуется пароль (PJLINK 1)")
            return {"ok": False, "response": None,
                    "error": "projector requires PJLink authentication (PJLINK 1)"}
        elif greeting_str.startswith("PJLINK ERRA"):
            log.warning(f"[{ip}] ошибка аутентификации (ERRA)")
            return {"ok": False, "response": None,
                    "error": "authentication failed (ERRA)"}
        else:
            log.warning(f"[{ip}] неизвестное приветствие: {greeting_str!r}")
            return {"ok": False, "response": None,
                    "error": f"unknown greeting {greeting_str!r}"}

        # --- команда и ответ ---
        sock.sendall((command + "\r").encode("ascii"))
        response = b""
        try:
            while not response.endswith(b"\r"):
                chunk = sock.recv(64)
                if not chunk:
                    break
                response += chunk
                if len(response) > 1024:
                    break
        except socket.timeout:
            log.warning(f"[{ip}] response TIMEOUT ({time.time()-t0:.1f}s, "
                        f"получено {len(response)} б)")
            return {"ok": False, "response": None, "error": "response timeout"}

        response_str = response.decode("ascii", errors="replace").strip()
        dt = time.time() - t0
        log.info(f"[{ip}] {command!r} → {response_str!r} ({dt:.2f}s)")
        return {"ok": True, "response": response_str, "error": None}

    except Exception as e:
        log.error(f"[{ip}] неожиданная ошибка: {type(e).__name__}: {e}")
        return {"ok": False, "response": None,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def parse_power_state(response: str) -> str:
    if not response:
        return "unknown"
    if "=" in response:
        value = response.split("=")[-1].strip()
        return {
            "0": "off", "1": "on", "2": "cooling", "3": "warmup",
        }.get(value, f"unknown({value})")
    return "unknown"


# ---------- HTTP ----------
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PJLinkHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as e:
            log.warning(f"[HTTP] write failed: {e}")

    def do_GET(self):
        t0 = time.time()
        client = self.client_address
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path in ("", "/status"):
                self._send_json(200, {
                    "ok": True,
                    "service": "PJLink bridge",
                    "projectors": list(PROJECTORS.keys()),
                })
                return

            if path == "/list":
                self._send_json(200, {"ok": True, "projectors": PROJECTORS})
                return

            parts = [p for p in path.split("/") if p]
            if len(parts) != 2:
                log.info(f"[HTTP] {client[0]} {self.path} → 404 (bad path)")
                self._send_json(404, {
                    "ok": False,
                    "error": "unknown path. Use /<projector>/<action>",
                    "example": "/pro21/state",
                })
                return

            name, action = parts
            if name not in PROJECTORS:
                log.warning(f"[HTTP] {client[0]} {self.path} → 404 "
                            f"(неизвестный проектор '{name}')")
                self._send_json(404, {
                    "ok": False,
                    "error": f"unknown projector '{name}'",
                    "available": list(PROJECTORS.keys()),
                })
                return

            ip = PROJECTORS[name]

            if action == "state":
                cmd = "%1POWR ?"
            elif action == "on":
                cmd = "%1POWR 1"
            elif action == "off":
                cmd = "%1POWR 0"
            else:
                log.info(f"[HTTP] {client[0]} {self.path} → 404 "
                         f"(неизвестное действие '{action}')")
                self._send_json(404, {
                    "ok": False,
                    "error": f"unknown action '{action}'",
                    "available": ["state", "on", "off"],
                })
                return

            result = pjlink_command(ip, cmd)

            payload = {
                "ok": result["ok"],
                "projector": name,
                "ip": ip,
                "action": action,
                "raw_response": result["response"],
                "error": result["error"],
            }
            if action == "state" and result["ok"]:
                payload["power"] = parse_power_state(result["response"])

            # логируем только ошибки; успешные state — уже в pjlink_command
            if not result["ok"]:
                log.warning(f"[HTTP] {name}/{action} → {result['error']} "
                            f"({time.time()-t0:.2f}s)")

            self._send_json(200 if result["ok"] else 502, payload)

        except Exception as e:
            log.error(f"[HTTP] ошибка обработки {self.path}: "
                      f"{type(e).__name__}: {e}")
            try:
                self._send_json(500, {"ok": False, "error": str(e)})
            except Exception:
                pass

    def log_message(self, fmt, *args):
        return


def run_http_server():
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), PJLinkHandler)
    log.info(f"HTTP-сервер запущен на 0.0.0.0:{HTTP_PORT} (многопоточный)")
    log.info("Проекторы:")
    for name, ip in PROJECTORS.items():
        log.info(f"    {name} -> {ip}:{PROJECTOR_PORT}")
    log.info("Маршруты:")
    log.info("    GET /list                 — список проекторов")
    log.info("    GET /<name>/state         — состояние")
    log.info("    GET /<name>/on            — включить")
    log.info("    GET /<name>/off           — выключить")
    log.info("Уровень логирования: DEBUG (всё видно)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановка…")
        server.server_close()


if __name__ == "__main__":
    run_http_server()