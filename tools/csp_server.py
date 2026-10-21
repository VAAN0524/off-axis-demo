#!/usr/bin/env python3
"""带 WorkBuddy 预览同级 CSP 的静态服务器，用来复刻"connect-src 不放行 blob:"的环境。

CSP 逐字抄自预览服务器响应头（2026-10-21 实测）。
"""
import functools
import http.server
import socketserver
import sys

CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: https:; "
    "connect-src 'self' http://127.0.0.1:* https://*.map.qq.com https://mapapi.qq.com "
    "https://*.gtimg.com https://*.qpic.cn; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: blob: https:; "
    "media-src 'self' data: blob: https: http://127.0.0.1:*; "
    "worker-src 'self' blob: https:"
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_message(self, *a):
        pass


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8124
    root = sys.argv[2] if len(sys.argv) > 2 else '.'
    handler = functools.partial(Handler, directory=root)
    # 必须多线程：浏览器一次开 6 条连接，单线程会让主文档那条一直排队
    with http.server.ThreadingHTTPServer(('127.0.0.1', port), handler) as httpd:
        print(f'http://127.0.0.1:{port}/  (CSP 已加)', flush=True)
        httpd.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
