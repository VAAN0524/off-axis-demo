#!/usr/bin/env python3
"""把"照片 + 深度图"烘焙成前端直接可用的四张图。

    near_mask_near.png     近景剪影（白 = 实心），导向滤波吸附到真实轮廓
    near_plate.jpg         背景底版：近景挡住的区域用周围像素补成虚焦背景
    near_plate_depth.png   底版的深度（8bit，已压到远景区间内）
    near_layer_depth.png   近景层深度（8bit，近景值向外补平，剪影处没有落差）

为什么要拆成这些：单张连续网格在物体轮廓处必然把深度断层拉成"橡皮布"，
头一偏就液化。拆成"满幅背景板 + 剪影近景"两层之后，跨轮廓的拉伸没了，
近景挪开的地方也有底版兜着。

用法：
    python3 bake_layers.py ../img/near.jpg depth.bin ../depth near
"""
import os
import sys
import numpy as np
from PIL import Image, ImageFilter

CUT = 0.55       # 深度切分点（1 = 最近）
W, H = 784, 490  # 深度图尺寸


def box(a, r):
    """(2r+1)^2 均值滤波，积分图实现"""
    h, w = a.shape[:2]
    c = np.cumsum(np.cumsum(a, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0), (0, 0)) if a.ndim == 3 else ((1, 0), (1, 0)))
    y0 = np.clip(np.arange(h) - r, 0, h); y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w); x1 = np.clip(np.arange(w) + r + 1, 0, w)
    s = c[y1][:, x1] - c[y0][:, x1] - c[y1][:, x0] + c[y0][:, x0]
    n = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    if a.ndim == 3:
        n = n[..., None]
    return s / np.maximum(n, 1)


def guided(I, p, r=8, eps=2e-3):
    """导向滤波：让软边吸附到原图亮度边缘上"""
    mI, mp = box(I, r), box(p, r)
    cov = box(I * p, r) - mI * mp
    var = box(I * I, r) - mI * mI
    a = cov / (var + eps)
    return box(a, r) * I + box(mp - a * mI, r)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(1e-6, e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def _down(a):
    h, w = a.shape[:2]
    a = a[:h // 2 * 2, :w // 2 * 2]
    return a.reshape(h // 2, 2, w // 2, 2, -1).mean(axis=(1, 3))


def _up(a, h, w):
    out = np.zeros((h, w, a.shape[2]), dtype=np.float32)
    for c in range(a.shape[2]):
        out[..., c] = np.asarray(
            Image.fromarray(a[..., c].astype(np.float32), mode='F').resize((w, h), Image.BICUBIC),
            dtype=np.float32)
    return out


def push_pull(img, mask):
    """多尺度填充：mask=1 表示已知。逐级下采样（预乘+权重），再逐级上采样补洞。"""
    img = img.astype(np.float32)
    if img.ndim == 2:
        img = img[..., None]
    m = mask.astype(np.float32)[..., None]
    pyr = []
    cur, cm = img * m, m
    while min(cur.shape[0], cur.shape[1]) > 6:
        pyr.append((cur, cm))
        cur, cm = _down(cur), _down(cm)
    fill = cur.sum(axis=(0, 1)) / max(cm.sum(), 1e-6)
    fill = np.broadcast_to(fill, cur.shape).copy()
    for (c, mm) in reversed(pyr):
        h, w = c.shape[:2]
        fill = c + (1.0 - np.clip(mm, 0, 1)) * _up(fill, h, w)  # c 已预乘
    return fill[..., 0] if img.shape[2] == 1 else fill


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    photo_path, dpath, outdir = sys.argv[1:4]
    key = sys.argv[4] if len(sys.argv) > 4 else 'near'
    os.makedirs(outdir, exist_ok=True)

    d = np.fromfile(dpath, dtype='<u2').astype(np.float32).reshape(H, W) / 65535.0
    color = Image.open(photo_path).convert('RGB')
    cw, ch = color.size
    small = color.resize((W, H), Image.BILINEAR)
    rgb = np.asarray(small, dtype=np.float32) / 255.0
    lum = np.asarray(small.convert('L'), dtype=np.float32) / 255.0

    # ① 近景遮罩：深度阈值 + 导向滤波吸附边缘
    f = 0.02
    m = smoothstep(CUT - f, CUT + f, d)
    m = np.clip((guided(lum, m, r=8, eps=2e-3) - 0.42) / 0.16, 0, 1)
    hard = m > 0.5

    mask = Image.fromarray((m * 255).astype(np.uint8)).resize((cw, ch), Image.BILINEAR)
    mask = mask.filter(ImageFilter.GaussianBlur(1.2))
    mask.save(os.path.join(outdir, '%s_mask_near.png' % key))

    # ② 背景底版：洞往外多挖一圈，避免留下近景轮廓的"鬼影"
    hole = box(hard.astype(np.float32), 12) > 0.001
    rgbf = push_pull(rgb, ~hole)
    rgbf = rgbf * (1 - hole[..., None]) + box(rgbf, 3) * hole[..., None]
    plate = Image.fromarray((np.clip(rgbf, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.BICUBIC)
    plate = plate.filter(ImageFilter.GaussianBlur(2.2))
    plate.save(os.path.join(outdir, '%s_plate.jpg' % key), quality=88)

    # ③ 底版深度：压在远景区间内，不然底版自身又是一道断层
    df = np.clip(box(push_pull(d, ~hole), 2), 0, CUT)
    Image.fromarray((df * 255).astype(np.uint8)).save(os.path.join(outdir, '%s_plate_depth.png' % key))

    # ④ 近景层深度：先腐蚀遮罩再填，让边缘取到真正靠里的深度值，
    #    这样剪影处没有落差，alpha 才能干净地切开
    dn = np.clip(d, CUT, 1.0)
    core = (box(hard.astype(np.float32), 5) > 0.999).astype(np.float32)
    dn = np.clip(box(push_pull(dn, core), 3), CUT, 1.0)
    Image.fromarray((dn * 255).astype(np.uint8)).save(os.path.join(outdir, '%s_layer_depth.png' % key))

    for fn in ('%s_mask_near.png' % key, '%s_plate.jpg' % key,
               '%s_plate_depth.png' % key, '%s_layer_depth.png' % key):
        p = os.path.join(outdir, fn)
        print('%-24s %6.0f KB' % (fn, os.path.getsize(p) / 1024))
    print('近景占比 %.1f%%' % (100 * hard.mean()))


if __name__ == '__main__':
    main()
