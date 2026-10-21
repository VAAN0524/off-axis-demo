#!/usr/bin/env python3
"""照片 + 深度 → LDI（分层深度图）资源，供前端离轴视差渲染。

    python3 bake_ldi.py img/near.jpg img/near.bin ldi/ --debug

输出（前端直接吃）：
    fg.jpg / fg_alpha.png / fg_depth.png    近景层：颜色（去色+外扩）、软 alpha、深度
    bg.jpg / bg_depth.png                   背景层：颜色（露出带已修复）、深度
    meta.json                               层数、切分深度、露出带宽度

三个关键决定，都是冲着"毛边"和"背景空白"去的
------------------------------------------------
1) 分层按 **深度片**（depth slice），不按语义物体。
   笔电放在桌上，底部和桌面深度是连续的 —— 那里本来就没有遮挡关系，
   硬切一刀只会在交界处造出假缝。真正要分开的是"近景片 / 远景片"。

2) 背景层只修 **露出带**（disocclusion band），不修整块前景。
   眼位横移 14cm 时，前景相对背景只错开几十像素，所以只有沿轮廓一圈
   会露出来。这一圈用 LaMa 按背景上下文补；轮廓里面的原像素留着
   —— 它永远被前景层盖住，补它纯属给模型出难题（大洞必糊）。

3) alpha 只做 1.5~2px 的软边，并用原图亮度把边缘吸附过去。
   上一版是 8px 的模糊边，配上 4px 一格的网格，看起来就是一圈毛。
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi

# ── 和前端 DEPTH_SCENE 对齐（决定"露出带"要留多宽）──────────────────
Z_REF, Z_NEAR, Z_FAR = 0.60, 0.40, 3.20
EYE_MAX = 0.075         # 眼位横移上限（米）
HALF_W_M = 0.35         # 屏幕半宽（米）


# ── 基础 ──────────────────────────────────────────────────────────────
def box(a, r):
    h, w = a.shape[:2]
    c = np.cumsum(np.cumsum(a, axis=0), axis=1)
    pad = ((1, 0), (1, 0), (0, 0)) if a.ndim == 3 else ((1, 0), (1, 0))
    c = np.pad(c, pad)
    y0 = np.clip(np.arange(h) - r, 0, h); y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w); x1 = np.clip(np.arange(w) + r + 1, 0, w)
    s = c[y1][:, x1] - c[y0][:, x1] - c[y1][:, x0] + c[y0][:, x0]
    n = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    if a.ndim == 3:
        n = n[..., None]
    return s / np.maximum(n, 1)


def guided(I, p, r=8, eps=2e-3):
    """导向滤波：p 的边缘吸附到 I 的边缘"""
    mI, mp = box(I, r), box(p, r)
    cov = box(I * p, r) - mI * mp
    var = box(I * I, r) - mI * mI
    a = cov / (var + eps)
    return box(a, r) * I + box(mp - a * mI, r)


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
    """多尺度填充（mask=1 已知）。只用于深度外推 / 颜色外扩，不做最终补洞。"""
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
        fill = c + (1.0 - np.clip(mm, 0, 1)) * _up(fill, h, w)
    return fill[..., 0] if img.shape[2] == 1 else fill


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(1e-6, e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def otsu(v, lo=0.02, hi=0.98):
    """在分位数裁剪后的直方图上找阈值（深度是双峰的：远景片 / 近景片）"""
    v = v[(v > lo) & (v < hi)]
    if v.size < 100:
        return 0.5
    hist, edges = np.histogram(v, bins=64, range=(0, 1))
    hist = hist.astype(np.float64)
    p = hist / hist.sum()
    om = np.cumsum(p)
    mu = np.cumsum(p * (edges[:-1] + edges[1:]) / 2)
    mu_t = mu[-1]
    denom = om * (1 - om)
    with np.errstate(divide='ignore', invalid='ignore'):
        sb = (mu_t * om - mu) ** 2 / denom
    sb = np.nan_to_num(sb)
    return float(edges[int(np.argmax(sb))])


# ── ① 深度锐化：把 784x490 的糊深度黏到原图边缘上 ──────────────────
def sharpen_depth(small, lum, out_shape):
    h, w = out_shape
    d = np.asarray(Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC),
                   dtype=np.float32) / 255.0
    # 粗 → 细三级引导：粗级把整体坡度拉直，细级把阶跃收到 1~2px
    for r, eps, it in ((20, 3e-2, 2), (10, 1.2e-2, 2), (5, 5e-3, 2)):
        for _ in range(it):
            d = np.clip(guided(lum, d, r=r, eps=eps), 0, 1)
    return ndi.median_filter(d, size=3).astype(np.float32)


# ── ② 深度片分割 ─────────────────────────────────────────────────────
def segment(d, lum):
    """
    沿"深度断层墙"切连通域，再按平均深度分成近景片 / 远景片。

    墙 = 深度阶跃（7x7 极差）+ 原图强边缘。只靠深度不够：笔电坐在桌面上，
    两者深度连续（物理上也确实连续），硬切只会造出假缝。
    """
    step = ndi.maximum_filter(d, 4) - ndi.minimum_filter(d, 4)
    gy, gx = np.gradient(ndi.gaussian_filter(lum, 1.2))
    gmag = np.hypot(gx, gy)
    t_step = max(0.012, np.percentile(step, 97))
    t_img = np.percentile(gmag, 92)
    edge = (step > t_step) | ((step > t_step * 0.45) & (gmag > t_img))
    wall = ndi.binary_dilation(edge, disk(1))

    lab, n = ndi.label(~wall)
    h, w = d.shape
    cut = otsu(d)
    # 逐像素先分一次（深度片），再用连通域剔掉碎块。
    # 用连通域平均深度来判定会被墙切碎 —— 上一版就这么错的：笔电被砖缝切成
    # 几十块，每块的平均深度都不够，最后只剩 1.5% 前景。
    raw_fg = d > cut
    lab2, n2 = ndi.label(raw_fg)
    fg = np.zeros((h, w), bool)
    comps = []
    for i in range(1, n2 + 1):
        m = lab2 == i
        a = m.mean()
        if a < 0.004:
            continue
        ys, xs = np.nonzero(m)
        md = float(d[m].mean())
        touch = ys.min() <= 1 or xs.min() <= 1 or ys.max() >= h - 2 or xs.max() >= w - 2
        comps.append({'i': i, 'area': round(float(a), 4), 'mean_d': round(md, 3),
                      'touch': bool(touch), 'bbox': [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]})
        fg |= m
    # 近景片内部的墙像素补回来（否则 alpha 上会有裂缝）
    fg = ndi.binary_fill_holes(ndi.binary_closing(fg, disk(2)))
    return fg, comps, cut, edge


def _runlen_1d(m):
    out = np.zeros(m.size, np.int32)
    d = np.diff(np.r_[0, m.astype(np.int8), 0])
    for s, e in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
        out[s:e] = e - s
    return out


def directional_fill(img, hole):
    """
    沿"跨洞更窄"的方向做插值填充。

    为什么不用生成式模型：这张图的背景本来就是虚焦的，LaMa 在 512 分辨率下
    既造不出比"平滑延续"更好的东西，还容易在大洞里把原物体又画回来。
    而窄带（露出带）里，方向插值能做到边界完全连续、无接缝 —— 虚焦背景看不出差别。

    方向按"行内洞宽 / 列内洞宽"逐像素挑：笔电上沿的横带用垂直插值（跨 66px），
    左右竖带用水平插值，避免横跨几百像素拉出条纹。
    """
    h, w = hole.shape
    nc = img.shape[2]
    xs = np.arange(w)
    ys = np.arange(h)
    hor = np.empty_like(img)
    for y in range(h):
        m = ~hole[y]
        k = xs[m]
        if k.size < 2:
            hor[y] = img[y].mean(0)
        else:
            for c in range(nc):
                hor[y, :, c] = np.interp(xs, k, img[y, m, c])
    ver = np.empty_like(img)
    for x in range(w):
        m = ~hole[:, x]
        k = ys[m]
        if k.size < 2:
            ver[:, x] = img[:, x].mean(0)
        else:
            for c in range(nc):
                ver[:, x, c] = np.interp(ys, k, img[:, x][m, c])

    hl = np.stack([_runlen_1d(hole[y]) for y in range(h)])
    vl = np.stack([_runlen_1d(hole[:, x]) for x in range(w)], axis=1)
    out = np.where((hl < vl)[..., None], hor, ver)
    # 洞外保持原样（插值在洞外本来就等于原值，这里只是防止数值误差）
    return np.where(hole[..., None], out, img)


# ── ③ 露出带宽度（像素）─────────────────────────────────────────────
def band_px(d_fg, d_bg, w_img):
    """
    眼移 EYE_MAX 时前景相对背景的屏幕位移。
    归一化屏幕坐标下位移 = e*zRef/(halfW*(zRef+z))，两层之差即会露出的宽度。
    """
    def z(dd):
        return Z_REF + (Z_FAR - Z_REF) * (1 - np.clip(dd, 0, 1))
    unit = EYE_MAX * Z_REF / HALF_W_M
    shift = unit * (1.0 / (Z_REF + z(d_fg)) - 1.0 / (Z_REF + z(d_bg)))
    return int(np.clip(abs(shift) * (w_img / 2.0) * 1.3 + 5, 6, w_img * 0.16))


# ── ④ 软 alpha ───────────────────────────────────────────────────────
def soft_alpha(fg, lum, width=1.6, guide=True, snap=10.0):
    """
    距离场 → 窄 S 曲线 → 用原图亮度吸附 → 大半径再吸附一次（snap）。

    第一级（r=3）只够把 1~2px 的毛刺收干净。真正的问题是**深度片边界本身
    就不在物体轮廓上**：单目深度在细杆、桌沿这种地方会糊十几像素，切出来的
    边界时而吃进物体、时而出界。眼睛不动看不出来，一移位就是一圈随轮廓抖动的
    亮边（实测白柱子左缘被吃进柱身 20px，露出背景的亮色）。
    所以再用一个大半径（r≈10）、小 eps 的引导滤波把 0.5 等值线整体拽到原图
    的强边缘上，然后把过渡重新压窄 —— 这时边界才真的贴在柱子上。
    """
    dist = ndi.distance_transform_edt(fg) - ndi.distance_transform_edt(~fg)
    a = np.clip(0.5 + dist / width, 0, 1)
    if not guide:
        return a.astype(np.float32)
    a = np.clip(guided(lum, a, r=3, eps=6e-3), 0, 1)
    a = np.clip((a - 0.5) * 1.6 + 0.5, 0, 1)
    if snap:
        r = int(round(snap))
        a = np.clip(guided(lum, a, r=r, eps=1.5e-3), 0, 1)
        a = np.clip((a - 0.5) * 2.2 + 0.5, 0, 1)
    return a.astype(np.float32)


# ── ⑤ LaMa ───────────────────────────────────────────────────────────
class Lama:
    """big-lama ONNX，固定 512×512 输入。

    注意：必须**等比缩放 + 居中补边**，不能直接 resize(512,512)。
    直接拉伸会把 960×600 压成正方形，砖块被压扁，模型按变形后的纹理生成，
    补出来的砖缝方向都是错的 —— 这一条踩过，结果就是"背景里有个笔电形状的浅色块"。
    """

    def __init__(self, path, size=512):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 6
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.s = ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])
        self.size = size

    def __call__(self, rgb01, hole):
        S = self.size
        h, w = rgb01.shape[:2]
        s = min(S / w, S / h)
        nw, nh = max(2, int(round(w * s))), max(2, int(round(h * s)))
        ox, oy = (S - nw) // 2, (S - nh) // 2

        im = Image.fromarray((np.clip(rgb01, 0, 1) * 255).astype(np.uint8)).resize((nw, nh), Image.LANCZOS)
        hk = Image.fromarray((hole * 255).astype(np.uint8)).resize((nw, nh), Image.NEAREST)
        # 补边用边缘像素填充，别留黑框（黑框会被当成强结构，模型会围着它生成）
        canvas = Image.new('RGB', (S, S))
        canvas.paste(im, (ox, oy), None)
        edge = im.resize((S, S), Image.NEAREST)
        canvas.paste(edge, (0, 0))
        canvas.paste(im, (ox, oy))
        cm = Image.new('L', (S, S), 0)
        cm.paste(hk, (ox, oy))

        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        m = (np.asarray(cm, dtype=np.float32)[None, None] / 255.0 > 0.5).astype(np.float32)
        out = self.s.run(None, {'image': x, 'mask': m})[0][0]
        out = np.transpose(np.clip(out, 0, 255) / 255.0, (1, 2, 0))
        out = out[oy:oy + nh, ox:ox + nw]
        return np.asarray(Image.fromarray((out * 255).astype(np.uint8)).resize(
            (w, h), Image.LANCZOS), dtype=np.float32) / 255.0


class Semantic:
    """
    通用显著物体分割（u2netp，4.3MB，320×320）。

    为什么必须有这一路：深度只能告诉你"谁近"，不能告诉你"谁是物体"。
    笔电坐在桌面上，两者深度是连续的 —— 光靠深度，笔电会被并进桌面，
    于是背景层里始终留着笔电，前景一移就露出"半台笔电"。

    也不要指望它单独干活：u2netp 只抠显著物体，桌面这种"支撑面"它不要。
    所以分工是 —— 语义给物体（笔电、手机），深度片给支撑面（桌面），
    两者并起来才是完整的前景层。
    """

    MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __init__(self, path, size=320):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 4
        self.s = ort.InferenceSession(path, so, providers=['CPUExecutionProvider'])
        self.iname = self.s.get_inputs()[0].name
        self.size = size

    def __call__(self, rgb01):
        h, w = rgb01.shape[:2]
        S = self.size
        im = Image.fromarray((np.clip(rgb01, 0, 1) * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
        x = np.asarray(im, dtype=np.float32) / 255.0
        x = (x - self.MEAN) / self.STD
        x = x.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.s.run(None, {self.iname: x})[0][0, 0]
        out = (out - out.min()) / max(1e-6, out.max() - out.min())
        return np.asarray(Image.fromarray((out * 255).astype(np.uint8)).resize(
            (w, h), Image.BILINEAR), dtype=np.float32) / 255.0


def box_mask(mask, grow=20, min_area=0.004):
    """
    把掩码换成「每个连通域的外接矩形」。

    LaMa 面对"笔电形状的洞"时，让洞内与边界一致的最省事办法就是再画一台笔电
    （实测：精确形状 mask 修完，笔电还在，只是躺平了）。矩形边界没有物体语义，
    模型只能老老实实延续墙面和桌面。
    """
    h, w = mask.shape
    out = np.zeros_like(mask)
    lab, n = ndi.label(mask)
    for i in range(1, n + 1):
        m = lab == i
        if m.mean() < min_area:
            continue
        ys, xs = np.nonzero(m)
        y0, y1 = max(0, ys.min() - grow), min(h, ys.max() + grow + 1)
        x0, x1 = max(0, xs.min() - grow), min(w, xs.max() + grow + 1)
        out[y0:y1, x0:x1] = True
    return out


def align_low(coarse, ref, hole, sigma=None):
    """
    低频对齐：把 coarse 在洞内的整体亮度/色温拉回 ref 的局部水平，只留它的高频纹理。

    LaMa 补出来的块常常整体偏亮或者偏冷 —— 墙有自上而下的明暗渐变，它在洞里
    没有这个概念，于是补完像"贴上去的一块亮片"。但直接把洞内低频换成两侧插值
    也不行：洞里的亮度会跟着左右两侧（一边是墙、一边是桌子）做线性渐变，跟
    头顶那片墙对不上，还是一块灰片。
    所以只取**边界处那一点差**，再用 push-pull 平滑地渗进洞里。
    """
    nc = coarse.shape[2]
    if sigma is None:
        sigma = max(16.0, min(hole.shape) * 0.055)
    low_c = np.stack([ndi.gaussian_filter(coarse[..., c], sigma) for c in range(nc)], -1)
    low_r = np.stack([ndi.gaussian_filter(ref[..., c], sigma) for c in range(nc)], -1)
    ring = ndi.binary_dilation(hole, disk(10)) & ~hole
    if not ring.any():
        return coarse
    off = push_pull(low_r - low_c, ring)
    return np.clip(coarse + off, 0, 1)


def local_inpaint(lama, rgb, hole, ctx=120):
    """
    只把包含洞的那块地方抠出来送进模型，而不是整幅图缩到 512。

    整图 960×600 缩到 512 是 0.53 倍，砖缝直接糊没，补出来就是一片颜色；
    局部裁 512 窗口基本是 1:1，纹理能留住。这是"背景空白"的最后一块拼图。
    """
    h, w = hole.shape
    ys, xs = np.nonzero(hole)
    if ys.size == 0:
        return rgb.copy()
    y0, y1 = max(0, ys.min() - ctx), min(h, ys.max() + ctx + 1)
    x0, x1 = max(0, xs.min() - ctx), min(w, xs.max() + ctx + 1)
    crop = rgb[y0:y1, x0:x1]
    cmask = hole[y0:y1, x0:x1]
    out = lama(crop, cmask)
    res = rgb.copy()
    res[y0:y1, x0:x1] = np.where(cmask[..., None], out, crop)
    return res


# ── 主流程 ───────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    photo, dbin, outdir = sys.argv[1:4]
    debug = '--debug' in sys.argv[4:] or os.environ.get('DEBUG') == '1'
    os.makedirs(outdir, exist_ok=True)
    dbg = os.path.join(outdir, '_debug')
    if debug:
        os.makedirs(dbg, exist_ok=True)

    color = Image.open(photo).convert('RGB')
    W, H = color.size
    rgb = np.asarray(color, dtype=np.float32) / 255.0
    lum = np.asarray(color.convert('L'), dtype=np.float32) / 255.0

    raw = np.fromfile(dbin, dtype='<u2').astype(np.float32)
    side = int(round(np.sqrt(raw.size * 600 / 960)))
    d0 = raw.reshape(side, raw.size // side) / 65535.0

    d = sharpen_depth(d0, lum, (H, W))
    fg, comps, cut, edge = segment(d, lum)

    # 近景片深度取 90 分位、远景片取 10 分位：露出带按"最坏情况"留，宁宽勿窄
    d_fg = float(np.percentile(d[fg], 90)) if fg.any() else 0.7
    d_bg = float(np.percentile(d[~fg], 10)) if (~fg).any() else 0.15
    band = band_px(d_fg, d_bg, W)

    alpha = soft_alpha(fg, lum)

    # 露出带 = 前景区域边缘向内 band 宽的一圈（前景整体平移时只有这一圈会露出来）。
    # 用距离场而不是形态学腐蚀：腐蚀会在笔电顶部那种窄处「穿透」，
    # 把整块都吞成露出带（实测腐蚀 66px 后 mask 覆盖了笔电大半）。
    # 距离场严格只取轮廓内 band 宽的一圈；画面外当作前景的延续。
    pad = band + 2
    fg_pad = np.pad(fg, pad, constant_values=True)
    dist_in = ndi.distance_transform_edt(fg_pad)[pad:-pad, pad:-pad]
    disocc = fg & (dist_in < band)

    # ── 语义分割：把"物体"从"支撑面"里分出来 ──
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    sem_path = os.path.join(root, 'model', 'u2netp.onnx')
    a_obj = Semantic(sem_path)(rgb) if os.path.exists(sem_path) else np.zeros((H, W), np.float32)
    obj = a_obj > 0.5
    if obj.mean() > 0.85 or obj.mean() < 0.002:      # 抠图失败（整片或全空）就别信它
        obj = np.zeros((H, W), bool)
        a_obj = np.zeros((H, W), np.float32)
    elif obj.any():
        # 语义模型常只抠到"主体最显著的那块"：笔电的屏幕抠出来了，底座却漏在
        # 背景层里 —— 一动就是"躺平的半台笔电"。用深度把它补齐：从 obj 出发，
        # 在"深度接近 obj 中位数"的近景里泛洪，底座的深度和屏幕是连续的，
        # 桌面的深度差得多，不会被并进来。
        md = float(np.median(d[obj]))
        # 只允许长到 obj 附近 30px 内：桌面的后沿深度和笔电接近，不限制距离的话
        # 泛洪会顺着桌面一路爬过去，把半张桌子也当成物体擦掉。
        near = ndi.binary_dilation(obj, disk(30))
        cand = fg & near & (np.abs(d - md) < 0.13)
        grow = ndi.binary_propagation(obj, mask=cand)
        obj = obj | grow
        a_obj = np.maximum(a_obj, grow.astype(np.float32))
    # 语义分割只在"确实有一个独立物体"时才敢用。风景照里 u2netp 会把桥、
    # 水面这种大面积横贯画面的东西当成主体，用矩形遮罩一擦，整幅图就废了。
    # 判据：面积适中、外接框不横跨整个画面、不是细长条。
    if obj.any():
        ys, xs = np.nonzero(obj)
        bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        bbox_ratio = (bw * bh) / float(W * H)
        bad = (obj.mean() > 0.30 or bbox_ratio > 0.55
               or bw > W * 0.85 or bh > H * 0.9)
        if bad:
            print('… 语义结果是"横贯画面的大结构"（bbox 占 %.0f%%），不擦物体，'
                  '退回纯深度片方案' % (100 * bbox_ratio))
            obj = np.zeros((H, W), bool)
            a_obj = np.zeros((H, W), np.float32)

    lama_path = os.path.join(root, 'model', 'lama_fp32.onnx')
    lama = None
    bg = rgb.copy()
    if obj.any() and os.path.exists(lama_path):
        # 物体的 mask 要比它本身大一圈（18px）。紧贴轮廓的洞会被模型当成"要延续
        # 的结构"，它会沿着笔电形状又画一个笔电出来（实测 mask=fg 时就是这样，
        # 反复迭代两次都残留）。放大后边界落在墙/桌上，模型才把它抹平。
        lama = Lama(lama_path)
        hole_obj = box_mask(obj, grow=22)
        # 整图 inpaint，不裁局部窗口。局部窗口试过：窗口里大半是笔电，
        # 上下文反而不够，LaMa 会把这台笔电"修"成躺着的一台还给你。
        coarse = lama(rgb, hole_obj)
        # 低频对齐：LaMa 补出来的块亮度和周围墙常常差一截（见 align_low 注释）
        fine = align_low(coarse, rgb, hole_obj)
        inpainted = np.where(hole_obj[..., None], fine, rgb)
        bg = np.where(ndi.binary_dilation(obj, disk(3))[..., None], inpainted, bg)
    elif obj.any():
        print('!! 缺少 model/lama_fp32.onnx，物体区只能靠插值（会有形状残留）')

    # 支撑面（桌面）**不做修复**，原样留着。
    #
    # 这是上一版"一动就露出空白块"的真正原因：背景层把桌面区域也填成墙，
    # 于是同一块桌面在前景层是 0.5m、在背景层是 2.9m —— 眼一动两层速度不同，
    # 桌面边缘立刻错开，露出背景层那块填出来的纯色。
    # 现在背景层在支撑面处保持原像素、原深度，两层完全同步，怎么动都不会撕。
    # 只有真正被物体挡住的地方（笔电背后）才需要造内容，而那块交给 LaMa。

    # ── 露出带：没有语义物体时，也得把"原件"从背景层里擦掉 ──
    #
    # 风景照里 u2netp 找不到"显著物体"，bg 就原样保留了 —— 等于背景层里放着
    # 一份和前景完全相同的内容。眼一动，两份按各自深度错开，轮廓边上立刻是
    # 双影 + 一圈抖边（实测右岸那根白柱子：左边 48px 全是背景里没擦掉的自己）。
    # 语义靠不住的时候，只有几何是可靠的：**眼位上限决定了最多只露 band 像素**，
    # 所以擦掉这一圈就够，不用碰整个前景 —— 这也顺手避开了"大洞必糊"。
    band_hole = disocc & ~ndi.binary_dilation(obj, disk(3))
    if band_hole.any():
        if os.path.exists(lama_path):
            # 露出带很窄（几十像素），但两侧上下文都是真实照片，生成式模型
            # 在这里是稳的 —— 让它把"背景"续过去，比插值干净得多。
            # 修完再做一次低频对齐，免得补出来的块整体偏亮/偏冷。
            lama = lama or Lama(lama_path)
            # 注意：这里**不能**用 box_mask。露出带是一圈细环，外接矩形动辄
            # 覆盖半个画面（笔电场景实测把背后那把椅子、杯子一起擦成浆糊）。
            # 细环本来就是 LaMa 最擅长的形状 —— 它两侧全是真实像素。
            hole = ndi.binary_dilation(band_hole, disk(2))
            fine = align_low(lama(rgb, hole), rgb, hole)
            bg = np.where(hole[..., None], fine, bg)
        else:
            # 没有模型时退回平滑外推：源只取前景以外，不会把物体边缘色拉进洞里
            # （两侧插值会，实测直接长出 48px 宽的“梳齿”）。
            bg = np.where(band_hole[..., None], push_pull(bg, ~fg), bg)

    # 前景 alpha：深度片的软边 + 语义软边取大。物体轮廓交给语义（它有真正的
    # 2~4px 过渡），桌面后沿交给深度距离场。
    alpha = np.maximum(alpha, a_obj)
    alpha = np.clip(guided(lum, alpha, r=2, eps=4e-3), 0, 1)

    # 背景深度：露出带的深度同样按"跨洞更窄"的方向插值 —— 和颜色用同一套逻辑，
    # 免得颜色是连续的、深度却鼓一个包。
    # 洞 = 物体 + 露出带；支撑面（不在洞里）的深度保持原值，两层才同步。
    hole_bg = ndi.binary_dilation(obj, disk(3)) | band_hole
    bg_d = push_pull(d, ~hole_bg)
    bg_d = np.where(hole_bg, bg_d, d)
    bg_d = np.clip(ndi.gaussian_filter(guided(lum, bg_d, r=5, eps=5e-3), 1.2), 0, 1)

    # 前景颜色：向 alpha 外扩散（双线性采样不混入背景色）+ 软边去色
    spread = push_pull(rgb, alpha > 0.55)
    fg_rgb = np.where((alpha > 0.02)[..., None], spread, rgb)
    ring = (alpha > 0.02) & (alpha < 0.98)
    if ring.any():
        B = np.stack([ndi.gaussian_filter(bg[..., c], 2.0) for c in range(3)], -1)
        a3 = alpha[..., None]
        F = (rgb - (1 - a3) * B) / np.maximum(a3, 0.2)
        fg_rgb = np.where(ring[..., None], np.clip(F, 0, 1), fg_rgb)

    # 前景深度：向外外推，剪影处没有落差
    fg_d = push_pull(d, alpha > 0.55)

    if debug:
        def sv(arr, name):
            Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8)).save(os.path.join(dbg, name))
        sv(d, 'depth_sharp.png')
        sv(edge.astype(np.float32), 'edges.png')
        sv(fg.astype(np.float32)[..., None] * 0.5 + rgb * 0.5, 'fg_overlay.png')
        sv(alpha, 'alpha.png')
        sv(disocc.astype(np.float32), 'disocc.png')
        sv(obj.astype(np.float32), 'semantic.png')

    def save_img(arr, name, **kw):
        Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8)).save(os.path.join(outdir, name), **kw)

    save_img(fg_rgb, 'fg.jpg', quality=93)
    save_img(np.repeat(alpha[..., None], 3, 2), 'fg_alpha.png')
    save_img(fg_d, 'fg_depth.png')
    save_img(bg, 'bg.jpg', quality=93)
    save_img(bg_d, 'bg_depth.png')

    meta = {'cut': round(cut, 3), 'band_px': band,
            'fg_ratio': round(float((alpha > 0.5).mean()), 4),
            'obj_ratio': round(float(obj.mean()), 4),
            'disocc_ratio': round(float(disocc.mean()), 4), 'size': [W, H]}
    with open(os.path.join(outdir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    print('切分深度 %.3f  前景 %.1f%%  露出带 %.1f%%  带宽 %dpx  域 %d'
          % (cut, 100 * (alpha > 0.5).mean(), 100 * disocc.mean(), band, len(comps)))
    for fn in ('fg.jpg', 'fg_alpha.png', 'fg_depth.png', 'bg.jpg', 'bg_depth.png'):
        print('  %-14s %6.0f KB' % (fn, os.path.getsize(os.path.join(outdir, fn)) / 1024))


if __name__ == '__main__':
    main()
    # onnxruntime 的 C++ 单例在解释器退出时会自己锁自己一下，抛
    # "recursive_mutex lock failed"，退出码变 1 —— 图早就写完了，
    # 但 build_ldi.sh 是 set -e，会被这一下打断。直接跳过析构。
    sys.stdout.flush()
    os._exit(0)
