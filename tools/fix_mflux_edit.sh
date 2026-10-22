#!/bin/bash
# 将 mflux-test 中较新的 edit/step_cache/latent_creator 模块移植进服务器 venv
# （服务器 venv 的 mflux 是旧版：只有 txt2img 变体、缺 step_cache 与 latent_creator）
set -e
SRC=/Users/vaan/workspace/02-image-gen/mflux-test/lib/python3.12/site-packages/mflux
DST=/Users/vaan/workspace/02-image-gen/20260919-qwen-image/qwen-image-2.1-mlx-fast/.venv/lib/python3.13/site-packages/mflux

cp -R "$SRC/models/qwen21/variants/edit" "$DST/models/qwen21/variants/"
cp -R "$SRC/models/common/step_cache" "$DST/models/common/"
cp -R "$SRC/models/qwen21/latent_creator" "$DST/models/qwen21/"

echo "── 移植结果 ──"
ls "$DST/models/qwen21/variants/"
ls "$DST/models/common/step_cache/" | head -3
ls "$DST/models/qwen21/latent_creator/" | head -4

echo "── 导入测试 ──"
PY=/Users/vaan/workspace/02-image-gen/20260919-qwen-image/qwen-image-2.1-mlx-fast/.venv/bin/python
"$PY" -c "import mflux.models.qwen21.variants.edit as e; print('edit 导入 OK:', hasattr(e, 'QwenImage21Edit'))"
