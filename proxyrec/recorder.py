"""Запись трафика через mitmproxy."""
import asyncio
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from .storage import new_test, pack_body

STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
              ".woff", ".woff2", ".ttf", ".eot", ".map", ".mp4", ".mp3")


class RecorderAddon:
    def __init__(self, store, test, hosts, keep_static, echo=False):
        self.store = store
        self.test = test
        self.hosts = [h.lower() for h in hosts]
        self.keep_static = keep_static
        self.echo = echo

    def _wanted(self, req: http.Request) -> bool:
        host = req.pretty_host.lower()
        if self.hosts and not any(host == h or host.endswith("." + h) for h in self.hosts):
            return False
        if not self.keep_static and urlsplit(req.path).path.lower().endswith(STATIC_EXT):
            return False
        return True

    def response(self, flow: http.HTTPFlow):
        req, resp = flow.request, flow.response
        if not self._wanted(req):
            return
        steps = self.test["steps"]
        elapsed = None
        if resp.timestamp_end and req.timestamp_start:
            elapsed = round((resp.timestamp_end - req.timestamp_start) * 1000)
        steps.append({
            "id": len(steps) + 1,
            "elapsed_ms": elapsed,  # время ответа при записи, для сравнения с прогоном
            "request": {
                "method": req.method,
                "url": req.url,
                "headers": [[k, v] for k, v in req.headers.items(multi=True)],
                **pack_body(req.get_content(strict=False) or b""),
            },
            "response": {
                "status": resp.status_code,
                "headers": [[k, v] for k, v in resp.headers.items(multi=True)],
                **pack_body(resp.get_content(strict=False) or b"", try_json=True),
            },
        })
        # Сохраняем после каждого запроса, чтобы ничего не потерять при аварийной остановке.
        self.store.save(self.test)
        if self.echo:
            print(f"  [{len(steps)}] {req.method} {req.url} -> {resp.status_code}", flush=True)


class RecordingSession:
    """Прокси в фоновом потоке: start() / stop(). Шаги видны в .test во время записи."""

    def __init__(self, store, name, port=8888, listen_host="127.0.0.1", reverse=None, hosts=(),
                 keep_static=False, echo=False):
        self.store, self.name, self.port = store, name, port
        self.listen_host, self.reverse = listen_host, reverse
        self.test = new_test(name)
        self.addon = RecorderAddon(store, self.test, hosts, keep_static, echo)
        self._loop = self._master = None
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def main():
            opts = Options(listen_host=self.listen_host, listen_port=self.port)
            # Работаем только с HTTP: HTTPS-туннели пропускаем насквозь, не расшифровывая.
            opts.update(ignore_hosts=[r".*:443$"])
            if self.reverse:
                opts.update(mode=[f"reverse:{self.reverse}"])
            self._master = DumpMaster(opts, with_termlog=True, with_dumper=False)
            opts.update(termlog_verbosity="warn")  # опция появляется только после создания termlog
            self._master.addons.add(self.addon)
            self._loop = asyncio.get_running_loop()
            await self._master.run()

        try:
            asyncio.run(main())
        except BaseException as e:  # в т.ч. SystemExit от errorcheck mitmproxy
            self._error = e

    @property
    def alive(self):
        return self._thread.is_alive()

    def start(self, timeout=15):
        connect_host = "127.0.0.1" if self.listen_host in ("0.0.0.0", "") else self.listen_host
        try:
            socket.create_connection((connect_host, self.port), timeout=0.3).close()
            raise OSError(f"порт {self.port} уже занят")
        except OSError as e:
            if "занят" in str(e):
                raise
        self.store.save(self.test)
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive:
                raise OSError(f"прокси не запустился: {self._error!r}")
            try:
                socket.create_connection((connect_host, self.port), timeout=0.3).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise OSError("прокси не запустился за отведённое время")

    def stop(self):
        if self._loop and self._master and self.alive:
            self._loop.call_soon_threadsafe(self._master.shutdown)
        self._thread.join(timeout=10)
        return self.test

    def describe(self):
        if self.reverse:
            return (f"reverse-прокси http://{self.listen_host}:{self.port} -> {self.reverse}",
                    f"Направьте приложение на http://{self.listen_host}:{self.port} вместо {self.reverse}")
        return (f"HTTP-прокси {self.listen_host}:{self.port}",
                f"Укажите в приложении прокси {self.listen_host}:{self.port} "
                f"(или HTTP_PROXY=http://{self.listen_host}:{self.port})")


def record(store, name, port, listen_host, reverse, hosts, keep_static):
    """Запись из командной строки: до Enter или Ctrl+C."""
    session = RecordingSession(store, name, port, listen_host, reverse, hosts, keep_static, echo=True)
    session.start()
    title, hint = session.describe()
    print(f"Запись '{name}': {title}")
    print(hint)
    print("Нажмите Enter (или Ctrl+C), чтобы остановить запись.\n", flush=True)
    try:
        # При закрытом stdin (EOF) не останавливаемся — ждём Ctrl+C.
        if not sys.stdin.readline():
            while session.alive:
                session._thread.join(0.5)
    except KeyboardInterrupt:
        pass
    test = session.stop()
    print(f"\nЗаписано шагов: {len(test['steps'])} -> {store.test_path(name)}")
    return test
