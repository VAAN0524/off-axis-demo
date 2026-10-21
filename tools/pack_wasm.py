#!/usr/bin/env python3
"""
把 MediaPipe 的 wasm 运行时打包成「普通脚本」，给受限环境兜底。

为什么需要：实测两种受限环境（file:// 与 sandbox iframe）里，fetch / XHR /
同源 import 全被拒（NetworkError），但 classic script、fetch(blob:)、
DecompressionStream 三条通道都通。所以把 9.4MB 的 wasm gzip 后 base64
塞进一个 .js，页面用 <script src> 读进来，解码后转 blob: URL 交给 MediaPipe
—— 整条路径不碰网络型 fetch。

用法：
    python tools/pack_wasm.py
产物：
    wasm/vision_wasm_pack.js   约 4MB（gzip 传输后约 3MB）
"""

import base64
import gzip
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WASM = ROOT / "wasm" / "vision_wasm_internal.wasm"
LOADER = ROOT / "wasm" / "vision_wasm_internal.js"
OUT = ROOT / "wasm" / "vision_wasm_pack.js"
VERSION = "0.10.14"


def main() -> int:
    for p in (WASM, LOADER):
        if not p.exists():
            print(f"缺少 {p}", file=sys.stderr)
            return 1

    raw = WASM.read_bytes()
    if raw[:4] != b"\x00asm":
        print("不是 wasm 模块，先检查文件完整性", file=sys.stderr)
        return 1

    loader = LOADER.read_bytes()
    wz = gzip.compress(raw, 9)
    lz = gzip.compress(loader, 9)
    b64w = base64.b64encode(wz).decode("ascii")
    b64l = base64.b64encode(lz).decode("ascii")

    js = (
        "/* 自动生成，勿手改：tools/pack_wasm.py\n"
        f"   MediaPipe tasks-vision {VERSION} 的 vision_wasm_internal"
        f"（wasm {len(raw)/1048576:.1f}MB → gzip {len(wz)/1048576:.1f}MB → base64 {len(b64w)/1048576:.1f}MB）\n"
        "   给 fetch 被拦的环境（file:// / sandbox iframe）兜底用 */\n"
        "window.__MP_WASM_PACK={"
        f"ver:'{VERSION}',simd:true,"
        f"js:'{b64l}',"
        f"wasm:'{b64w}'"
        "};\n"
    )
    OUT.write_text(js)
    print(
        f"{OUT.relative_to(ROOT)}  {len(js)/1048576:.2f}MB "
        f"(wasm {len(raw)/1048576:.2f}MB → gzip {len(wz)/1048576:.2f}MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
