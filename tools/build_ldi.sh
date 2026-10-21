#!/usr/bin/env bash
# 任意一张照片 → 前端可用的 LDI 资源（一条命令跑完）
#
#   tools/build_ldi.sh img/near.jpg ldi
#   tools/build_ldi.sh ~/Pictures/portrait.jpg ldi --debug
#
# 依赖（都在 model/ 下，.gitignore 已忽略）：
#   model/depth-anything-v2-small.onnx   单目深度（约 27MB）
#   model/u2netp.onnx                    通用显著物体分割（4.3MB）
#   model/lama_fp32.onnx                 遮挡区修复（198MB，可缺省，缺省会退回插值）
#
# 环境变量 PY 指定解释器（默认用 WorkBuddy 的隔离环境，那里装了 onnxruntime）。
set -euo pipefail
cd "$(dirname "$0")/.."

PHOTO="${1:?用法: tools/build_ldi.sh <照片> [输出目录] [--debug]}"
OUT="${2:-ldi}"
# 把照片和输出目录都吃掉，剩下的（--debug 之类）原样透传给 bake_ldi.py
if [ $# -ge 2 ]; then shift 2; else shift 1; fi

PY="${PY:-/Users/vaan/.workbuddy/binaries/python/envs/default/bin/python}"
[ -x "$PY" ] || PY=python3

DEPTH_MODEL="model/depth-anything-v2-small.onnx"
[ -f "$DEPTH_MODEL" ] || DEPTH_MODEL="model/model_q.onnx"
[ -f "$DEPTH_MODEL" ] || { echo "缺少深度模型：$DEPTH_MODEL"; exit 1; }

echo "① 估计深度：$PHOTO"
"$PY" tools/estimate_depth.py "$DEPTH_MODEL" "$PHOTO"

BIN="${PHOTO%.*}.bin"
echo "② 烘焙 LDI → $OUT"
"$PY" tools/bake_ldi.py "$PHOTO" "$BIN" "$OUT" "$@"

echo
echo "③ 前端接入：把 index.html 里的 DEPTH_LAYERS 指向 $OUT/"
echo "   tex: '$OUT/bg.jpg'        depthSrc: '$OUT/bg_depth.png'"
echo "   tex: '$OUT/fg.jpg'        depthSrc: '$OUT/fg_depth.png'   mask: '$OUT/fg_alpha.png'"
