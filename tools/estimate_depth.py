#!/usr/bin/env python3
"""用 Depth Anything V2 (small, ONNX) 估计单目深度，输出 16bit .bin + 预览 PNG。

    pip install onnxruntime pillow numpy
    # 模型：huggingface 上 onnx-community/depth-anything-v2-small 的 onnx/model_quantized.onnx
    #       国内可用 hf-mirror.com 镜像，约 27MB
    python3 estimate_depth.py ../model/depth-anything-v2-small.onnx ../img/near.jpg

输出：<图片同名>.bin（uint16 小端，行优先，0 = 最远 1 = 最近）+ <图片同名>_depth.png
"""
import json
import os
import sys
import time

import numpy as np
import onnxruntime as ort
from PIL import Image

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def smooth(d, iters=2):
    """3x3 高斯，压一压单像素噪点（深度图的边缘本来就是糊的，不怕）"""
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32) / 16.0
    for _ in range(iters):
        p = np.pad(d, 1, mode='edge')
        acc = np.zeros_like(d)
        for y in range(3):
            for x in range(3):
                acc += k[y, x] * p[y:y + d.shape[0], x:x + d.shape[1]]
        d = acc
    return d


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    model_path = sys.argv[1]
    imgs = sys.argv[2:]
    out_dir = os.path.dirname(os.path.abspath(imgs[0]))

    so = ort.SessionOptions()
    so.intra_op_num_threads = 6
    sess = ort.InferenceSession(model_path, so, providers=['CPUExecutionProvider'])
    iname = sess.get_inputs()[0].name

    for src in imgs:
        im = Image.open(src).convert('RGB')
        w0, h0 = im.size
        tw = 784                       # 14 的倍数，约 0.5s / 张（M 系列 CPU）
        th = int(round(tw * h0 / w0 / 14)) * 14
        img = im.resize((tw, th), Image.BICUBIC)
        x = np.asarray(img, dtype=np.float32) / 255.0
        x = ((x - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)

        t0 = time.time()
        d = np.squeeze(sess.run(None, {iname: x})[0])
        dt = time.time() - t0

        # 输出是"视差"（越大越近），按百分位归一化，切掉极端值
        lo, hi = np.percentile(d, 1.0), np.percentile(d, 99.5)
        d = np.clip((d - lo) / max(1e-6, hi - lo), 0, 1)
        d = smooth(d, 2)

        name = os.path.splitext(os.path.basename(src))[0]
        bin_path = os.path.join(out_dir, name + '.bin')
        np.round(d * 65535).astype(np.uint16).tofile(bin_path)
        Image.fromarray((d * 255).astype(np.uint8)).save(os.path.join(out_dir, name + '_depth.png'))
        print(json.dumps({'name': name, 'in': '%dx%d' % (w0, h0),
                          'depth': '%dx%d' % (d.shape[1], d.shape[0]),
                          'sec': round(dt, 2),
                          'kb': round(os.path.getsize(bin_path) / 1024)}))


if __name__ == '__main__':
    main()
