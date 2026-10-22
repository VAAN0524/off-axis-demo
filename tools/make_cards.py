#!/usr/bin/env python3
"""照片 → 卡片栈（2 张前后景片 + 生成式补边），替掉 LDI 网格。

    python3 tools/make_cards.py img/near.jpg img/near.bin cards/near

为什么换掉 LDI 网格
------------------
网格是把整张照片拆成 480×300 个顶点，每个顶点按**自己那一像素的深度**算
位置。这条路上有三个躲不掉的毛病：

1. 同一块面内深度有微差 → 每个顶点缩放不同 → 面被剪切成"扭曲"；
2. 轮廓两侧相邻顶点深度差一大截 → 它们之间的那个三角形被拉成橡皮布，
   颜色跨着轮廓互相渗透 → 剪影一圈"锯齿 + 亮毛"；
3. 网格只覆盖原图，眼睛一移，边缘就露底 → 需要凭空补一整圈。

卡片栈把"面片"当成**刚体**：每层只有一个深度值，整层做同一个位移。
一块正对镜头的平面在针孔相机下本来就只有平移（没有变形），
所以这个简化不是偷工，是**物理上正确的那一半**：

    屏幕位移(px) = eye · k · zRef / (zRef + z)

层内零形变 → 没有扭曲；轮廓是真 alpha 遮罩整体平移 → 边缘就是原照片的边缘；
背景板四周先做生成式外扩 → 怎么移都不会露白。

代价：层内没有次生视差（背景墙自己不会微微变形）。眼位 7.5cm 这个量级下，
相对位移只有几十像素，这点损失换掉整片扭曲，非常划算。

产出
----
    bg.jpg        背景板：主体已抹掉，四周外扩 pad（默认 10%）
    subj.png      主体卡片：原图真实像素 + 软 alpha（已做去边色）
    meta.json     每层的 z、偏移、裁剪框

用法补充
--------
    --pad 0.10        四周外扩比例
    --z-subj 0.25     主体卡片相对屏幕的深度（米），越小越"贴脸"
    --z-bg 1.60       背景板深度
    --mirror-hf 0.5   外扩区高频借用镜像原图的比例（0=纯生成，1=纯镜像质地）
    --no-lama         不跑模型，只做低频外推（秒出，用于调渲染）
    --plate <图>      外部生成好的背景板（主体已抹掉、四周外扩过），
                      有它就不跑本地补全 —— 这是"云模型生图"的接入口
    --layers 1|2      强制层数。默认 auto：有独立主体就 2 层，风景照 1 层
"""
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bake_ldi as B  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
Z_REF = B.Z_REF
HALF_W_M = B.HALF_W_M
EYE_MAX = B.EYE_MAX


def argval(flag, default, cast=float):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


# ── 去边色：把半透明像素里混进去的旧背景剥出来 ────────────────────────
def decontaminate(rgb, alpha, bg, lo=0.12, hi=0.995):
    """
    抠图边缘一圈像素，颜色其实是 F·a + B·(1-a)——里面掺了**旧背景**的颜色。
    这张卡片一旦滑到别的位置，那圈旧背景色就成了一道亮/暗边，也就是
    "毛边"在合成阶段的真正来源。已知新背景 B（就是背景板），就能反解：

        F = (I - (1-a)·B) / a

    a 太小的像素反解会炸，所以只处理 0.03 < a < 0.995 的这一圈，结果裁到 [0,1]。
    """
    a = alpha[..., None]
    f = (rgb - (1.0 - a) * bg) / np.maximum(a, 1e-2)
    m = ((alpha > lo) & (alpha < hi))[..., None]
    return np.where(m, np.clip(f, 0, 1), rgb).astype(np.float32)


# ── 外扩：边缘拉伸 + 递进模糊（延续但失焦） ────────────────────────────
def outpaint(lama, rgb, pad_x, pad_y, mirror_hf=0.0, sigma=9.0):
    """
    之前两版外扩都被打脸：

    · symmetric 镜像 + 借镜像高频：桌面的斜纹木板被翻折成万花筒蝴蝶纹
      （左右下角两块对称花纹），强结构边缘一眼假 —— 高频借得越足，镜像
      结构越清楚，这是方法自带的，调 mirror_hf 救不了；
    · 纯 LaMa 外扩：糊边，模型在 512 里看不到足够上下文。

    现在改「延续但失焦」：边缘像素向外拉伸（连续性最好，无翻折），
    再按离边界的距离递进模糊 —— 近处还是锐的、越远越糊。移动眼位时
    外扩区只在外围一瞥，**失焦的延续远比镜像结构不显眼**（摄影里
    边缘失焦本来就是自然的）。
    """
    H, W = rgb.shape[:2]
    canvas = np.pad(rgb, ((pad_y, pad_y), (pad_x, pad_x), (0, 0)), mode='edge')
    dist = np.ones(canvas.shape[:2], np.float32)
    dist[pad_y:pad_y + H, pad_x:pad_x + W] = 0.0
    dist = ndi.distance_transform_edt(dist)
    m = np.clip(dist / max(pad_x, pad_y, 1), 0, 1) ** 0.7      # 0=原像素 1=最外缘
    low = np.stack([ndi.gaussian_filter(canvas[..., c], 28.0) for c in range(3)], -1)
    ext = canvas * (1 - m[..., None]) + low * m[..., None]
    ext[pad_y:pad_y + H, pad_x:pad_x + W] = rgb
    return np.clip(ext, 0, 1).astype(np.float32)


# ── 露出带：分块送模型，别整幅缩到 512 ────────────────────────────────
def ring_fill(lama, img, ring, win=512, stride=256, feather=40):
    """
    整幅图（960×600）直接送进 512 的 LaMa，等于把画面缩到 0.53 倍再补 ——
    68px 宽的露出带在模型眼里只剩 36px，出来必然是一块平滑的米色，
    挪一下头就是一根"贴上去的柱子"（实测右移 7.5cm 时笔电右边立起一根）。

    改成：只在露出带附近开窗口，一块一块补。窗口 512 时模型是 1:1 甚至放大
    在看，砖缝、桌沿这些结构留得住；窗口重叠一半、按羽化权重融合，接缝不可见。
    """
    h, w = ring.shape
    acc = np.zeros_like(img)
    wsum = np.zeros((h, w), np.float32)
    ys, xs = np.nonzero(ring)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1

    def feath(shape):
        a, b = shape
        yy = np.minimum(np.arange(a), np.arange(a)[::-1]) + 1
        xx = np.minimum(np.arange(b), np.arange(b)[::-1]) + 1
        return np.clip(np.minimum(yy[:, None], xx[None, :]) / max(1, feather), 0, 1)

    def starts(a0, a1, limit):
        xs = list(range(a0, a1 + 1, stride))
        if xs[-1] + win < a1 or len(xs) == 1:
            xs.append(a1)
        return sorted({min(max(0, t), max(0, limit - win)) for t in xs})

    for ty in starts(y0, y1, h):
        for tx in starts(x0, x1, w):
            ry0, rx0 = max(0, ty), max(0, tx)
            ry1, rx1 = min(h, ty + win), min(w, tx + win)
            sub = ring[ry0:ry1, rx0:rx1]
            if not sub.any():
                continue
            out = lama(img[ry0:ry1, rx0:rx1], sub)
            wt = feath(sub.shape) * sub
            acc[ry0:ry1, rx0:rx1] += out * wt[..., None]
            wsum[ry0:ry1, rx0:rx1] += wt
    out = img.copy()
    m = wsum > 1e-6
    out[m] = acc[m] / wsum[m][..., None]
    return np.clip(out, 0, 1)


# ── 色调：让补出来的内容采纳"洞外真实像素"的局部色温 ─────────────────
def tone_match(fill, rgb, hole, ring_px=22, sigma=11.0):
    """
    LaMa 补露出带，结构对、**色温却常跟周围差一截** —— 一面暖米色的砖墙，
    它补出一块偏冷偏灰的平滑色。眼一移，那条补出来的带子就是"贴上去的一块"，
    边界上还有一条看得见的线。

    正确参考只能来自**洞外的真实像素**（洞内是我们要造的东西，不能当参照）。
    做法是归一化卷积求"周围真实像素的局部平均色"，只把差值加上去：

        low_out = Gaussian(rgb · m) / Gaussian(m)      m = 洞外
        out     = fill + (low_out - Gaussian(fill))

    只补低频，模型给的纹理原封不动 —— 边界两侧色温一致，线自然消失。
    """
    m = (~hole).astype(np.float32)
    num = np.stack([ndi.gaussian_filter(rgb[..., c] * m, sigma) for c in range(3)], -1)
    den = np.maximum(ndi.gaussian_filter(m, sigma), 1e-4)[..., None]
    low_out = num / den
    low_fill = np.stack([ndi.gaussian_filter(fill[..., c], sigma) for c in range(3)], -1)
    return np.clip(fill + (low_out - low_fill), 0, 1)


# ── 露出带：调和插值（迭代扩散），边界条件 = 洞外真实像素 ─────────────
def harmonic_fill(rgb, src, band, iters=24):
    """
    multigrid 调和插值。洞里每一轮做一次高斯，再把洞外强制恢复成原图：

        out ← Gaussian(out)      只在洞内生效
        out ← rgb                洞外永远是真实像素

    收敛后洞内是"以洞外像素为边界条件的最小曲率曲面"，即调和插值。
    相比各种"平均色"的填法，它多出两个关键性质：

    · **只受最近那圈边界约束**，远处的过曝墙 / 暗桌掺不进来（归一化高斯
      sigma=15 时，笔电左上角那片很亮的墙会被算进洞里，补出来比真实墙
      亮一大截 —— 左移时就是一块白板）；
    · **边界上严格等于原值**，不会有接缝、也不会有虚化描边。

    **为什么必须 multigrid**：单尺度"σ·√N ≥ 洞半径就收敛"是错的 ——
    那只是让扰动衰减，而洞的初值是**原图的笔电像素**，要把"笔电"洗成
    "边界调和曲面"，信息必须从边界一路传到洞心。笔电洞宽 ~200px，
    σ=band/4≈11 的 14 轮扩散只能影响 ~40px —— 洞心永远留一层模糊过的
    笔电残影（Apple logo 隐约可见），背景板看着像盖了块磨砂玻璃。

    解法是经典 multigrid：一路降采样到最长边 ≤ 64px（此时洞只剩几个
    像素），在粗尺度上迭代 300 轮**真正解收敛**（开销可忽略），再逐级
    上采样作初值、每级 24 轮 polish。上采样解已非常接近真解，细尺度
    只需修局部 —— 每一级的信息都只需要走"本级洞的尺寸"，而不是整幅
    原图的洞尺寸。确定收敛，无条件留残影。

    **不要用"残差不变就早停"**：洞中心在头几轮本来就完全不动（扩散还没
    走到），max|Δ| 一直很小，会立刻误判收敛，然后洞里还是原图的笔电轮廓
    —— 背景板上那条虚线斜边就是这么来的。
    """
    H, W = rgb.shape[:2]
    n_lv = 0
    while max(H, W) // (2 ** (n_lv + 1)) > 64:
        n_lv += 1

    def down_color(a, hs, ws):
        z = ndi.zoom(a, (hs / a.shape[0], ws / a.shape[1], 1), order=1)
        return z[:hs, :ws]

    def down_mask(a, hs, ws):
        z = ndi.zoom(a.astype(np.float32), (hs / a.shape[0], ws / a.shape[1]), order=1)
        return z[:hs, :ws] > 0.5

    out = None
    for lv in range(n_lv, -1, -1):
        f = 2 ** lv
        hs, ws = max(2, H // f), max(2, W // f)
        rgb_s = down_color(rgb, hs, ws)
        src_s = down_mask(src, hs, ws)
        if out is None:
            out = rgb_s.copy()
            n_it = 300            # 最粗级：洞只剩几个像素，多轮必收敛
        else:
            out = down_color(out, hs, ws)
            n_it = iters          # 初值已是近似解，只做 polish
        sigma = max(2.0, band / f / 4.0)
        for _ in range(n_it):
            sm = np.stack([ndi.gaussian_filter(out[..., c], sigma) for c in range(3)], -1)
            out = np.where(src_s[..., None], rgb_s, sm)
    return np.clip(out, 0, 1)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    photo, dbin, outdir = sys.argv[1:4]
    pad = argval('--pad', 0.10)
    z_subj = argval('--z-subj', 0.35)
    z_bg = argval('--z-bg', 1.60)
    mirror_hf = argval('--mirror-hf', 0.5)
    max_side = argval('--max-side', 1600, int)
    use_lama = '--no-lama' not in sys.argv
    ring_mode = 'smooth'
    if '--ring' in sys.argv:
        ring_mode = sys.argv[sys.argv.index('--ring') + 1]
    plate_path = None
    if '--plate' in sys.argv:
        plate_path = sys.argv[sys.argv.index('--plate') + 1]

    os.makedirs(outdir, exist_ok=True)

    color = Image.open(photo).convert('RGB')
    if max(color.size) > max_side:
        color = color.resize((max_side, round(color.height * max_side / color.width)), Image.LANCZOS)
    W, H = color.size
    rgb = np.asarray(color, dtype=np.float32) / 255.0
    lum = np.asarray(color.convert('L'), dtype=np.float32) / 255.0
    print(f'照片 {W}×{H}')

    raw = np.fromfile(dbin, dtype='<u2').astype(np.float32)
    side = int(round(np.sqrt(raw.size * 600 / 960)))
    d0 = raw.reshape(side, raw.size // side) / 65535.0
    d = B.sharpen_depth(d0, lum, (H, W))

    # ── 主体遮罩：语义优先，深度片兜底 ──
    sem_path, lama_path = os.path.join(ROOT, 'model', 'u2netp.onnx'), os.path.join(ROOT, 'model', 'lama_fp32.onnx')
    fg, _, _, _ = B.segment(d, lum)
    guard_hit = False
    obj = np.zeros((H, W), bool)
    if os.path.exists(sem_path):
        a_obj = B.Semantic(sem_path)(rgb)
        obj = a_obj > 0.5
        if obj.mean() > 0.85 or obj.mean() < 0.002:
            obj[:] = False
        elif obj.any():
            md = float(np.median(d[obj]))
            cand = fg & ndi.binary_dilation(obj, B.disk(30)) & (np.abs(d - md) < 0.13)
            obj |= ndi.binary_propagation(obj, mask=cand)
            # 语义守卫：u2netp 的"显著"不等于"最近"。风景照里它会把桥、
            # 水面这种横贯画面的东西整块当主体，用它抠图等于把照片撕掉半张。
            ys, xs = np.nonzero(obj)
            bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            # 两条最硬的判据，都是"形状"层面的：
            # · **贴边**：独立物体（笔电、瓶子、人）四周一定留空隙；桥、
            #   城墙、海平线这类横贯画面的东西会一直顶到画面边上。
            # · **宽度占比**：实测大桥那张 bbox 是 81%×29% —— 面积只有 19%，
            #   又宽又扁，就是一条横带，不是物体。阈值取 0.60：真的独立物体
            #   很少占掉六成画面宽度，而误判成单层的代价（少一点视差）远小于
            #   误判成双层的代价（移动时露出补出来的糊块）。
            touch = (obj[0].mean() > 0.10) + (obj[-1].mean() > 0.10) \
                + (obj[:, 0].mean() > 0.10) + (obj[:, -1].mean() > 0.10)
            if (obj.mean() > 0.30 or (bw * bh) / float(W * H) > 0.55
                    or bw > W * 0.60 or bh > H * 0.85 or touch >= 2):
                print(f'语义守卫触发：没有可拆的独立主体（贴边 {touch} 条）')
                obj = np.zeros((H, W), bool)
                guard_hit = True

    # 单层 / 双层：这是整套方案里最重要的一个自适应判断。
    #
    # 双层（主体卡 + 背景卡）只有在"有一个**独立物体**"时才是对的 —— 物体的
    # 轮廓是真实的遮挡边界，卡片整体平移 = 物体在动，物理自洽。
    #
    # 而风景照里没有物体，深度片切出来的是一大块不规则的"近景板"（船头甲板、
    # 前景礁石）。把它当卡片平移 86px，等于把一大片真实像素整体拖走 —— 拖过去
    # 的内容既不属于那个深度，也不会带回它自己的次生视差，越看越怪。
    # 这种图正确的做法是**只做单层**：整张照片当一个卡片微微前移，没有相对位移，
    # 也就没有任何需要凭空生成的地方，零瑕疵。人在看远处风景时本来就几乎没有
    # 视差，这个选择在感知上也是对的。
    auto_single = guard_hit and '--layers' not in sys.argv
    forced_single = ('--layers' in sys.argv and sys.argv[sys.argv.index('--layers') + 1] == '1')
    if auto_single or forced_single:
        print('单层模式：整张照片当一块卡片，无相对位移 = 无露出带 = 无需生成')
        subject = np.zeros((H, W), bool)
    else:
        if obj.mean() > 0.004:
            # 语义掩码必须**完整**。之前试过用深度中值再切一刀（只取比
            # d[obj] 中值更近的部分，想滤掉"实体脚下的桌面"），结果深度图
            # 在反光黑屏上把屏幕估得比机身远 —— 完整的笔电掩码被切成了
            # C 形，屏幕和右半机身留在了背景板上：fill 把它们当"真实背景"
            # 原样保留，中心位就浮出一台半透明笔电（Apple logo 清晰可见）。
            # 深度图对反光/纯色/透明表面本来就不可靠，**不能让它决定掩码的
            # 完整性**。宁可多抹一点桌面前景 —— 调和插值（multigrid 版）
            # 对大洞也能收敛成边界材质的延展，虚焦桌面上看不出平滑。
            lab, n = ndi.label(obj)
            if n > 1:
                sizes = ndi.sum(obj, lab, range(1, n + 1))
                obj = lab == (1 + int(np.argmax(sizes)))
            subject = ndi.binary_fill_holes(obj)
        else:
            subject = fg
    if subject.mean() < 0.004:
        if not auto_single and not forced_single:
            print('!! 找不到主体，退化成单张卡片')
        subject = np.zeros((H, W), bool)
    elif subject.any():
        # 语义遮罩会在物体**内部**挖洞：u2netp 对一块深色反光的屏幕经常
        # 只给到 0.3~0.4 的置信度，于是笔电屏幕上破了一个大洞。洞本身不大，
        # 后果却很大 —— 卡片在那里 alpha < 1，背景板（已经抹掉笔电、补成墙色）
        # 就从屏幕里透上来，看起来是"笔电上糊了一团白雾"。
        # 填洞是安全操作：主体是连通的实体，内部不该有洞。
        subject = ndi.binary_fill_holes(subject)

    alpha = B.soft_alpha(subject, lum) if subject.any() else np.zeros((H, W), np.float32)
    if subject.any():
        # soft_alpha 末尾那道大半径"吸附"偶尔会在主体内部打洞（实测船景
        # 有 0.7% 的主体像素被吸到 0.5 以下）。洞虽小，但背景板在那里被抹掉了，
        # 中心位就会从洞里漏出补出来的内容 —— 一条脏点。所以离轮廓 2px 以上的
        # 地方一律强制 a=1，软边只允许活在最外 2px。
        inner = ndi.distance_transform_edt(subject) > 2.0
        alpha = np.where(inner, 1.0, alpha).astype(np.float32)

    # ── 位移预算：决定"要修多宽的一圈"和"外扩够不够" ──
    def shift_px(z):
        return EYE_MAX * (W / (2 * HALF_W_M)) * Z_REF / (Z_REF + z)

    s_sub, s_bg = shift_px(z_subj), shift_px(z_bg)
    reveal = s_sub - s_bg                     # 主体相对背景多走的像素
    band = int(max(24, reveal * 1.35 + 8))    # 多留 35%，免得补出来的接缝正好卡在露出边界
    # 真正要抹的只是一圈「轮廓内侧、宽=相对位移」的月牙：主体一移，露出来的
    # 就是这条。比 band 更窄 → 调和插值扩散半径够覆盖，能收敛成边界墙色梯度；
    # 不至于把整块主体框成大洞，填出来一片均匀均值（「雾」）。
    ring_w = int(argval('--ring-w', max(18, round(reveal * 1.25))))

    # ── 背景板：只修"轮廓内的一圈"，不修整块主体 ──
    #
    # 这是这张图之前最大的败因：把整台笔电（占画面 31%）框成一个大洞送进 LaMa。
    # 512 的模型面对 1/3 画面的洞、四周全是同色砖墙，只能靠"最省事"的方式填空，
    # 于是补出一台糊掉的笔电 —— 越修越像有东西。
    #
    # 但主体卡片一移，真正会露出来的只有**沿轮廓、宽度等于相对位移的那一圈**。
    # 圈里面永远被主体卡片盖着，补它毫无意义。所以只修这一圈：
    # 洞占总面积 5% 左右，外面全是真实像素，LaMa 在这里几乎不会失手。
    lama = None
    if use_lama and os.path.exists(lama_path):
        lama = B.Lama(lama_path)
    pad_x, pad_y = int(round(W * pad)), int(round(H * pad))
    bg = rgb.copy()
    # 洞/抹除范围——无论本地补全还是外部板子都要算，先算出来。
    erase = hole = None
    if subject.any():
        erase = ndi.binary_dilation(subject, B.disk(3))
        sp = band + 6
        erase_pad = np.pad(erase, sp, constant_values=True)
        dist_in = ndi.distance_transform_edt(erase_pad)[sp:-sp, sp:-sp]
        ring = erase & (dist_in < ring_w)
        hole = ndi.binary_dilation(ring, B.disk(5))
    if plate_path:
        # 外部生成好的背景板（通常是文生/编辑模型把主体抹掉后重绘的图）。
        # 关键架构：**板子只填洞内，洞外保持原像素**。
        #   · 生成图和原图不可能像素级对齐，整块替换会在边界穿帮；
        #   · 洞外本来就是真实像素，没必要动；
        #   · 只在会露出的 hole 区域用板子纹理，羽化融合。
        # 再做一次色彩迁移：以洞外原图为参考，把板子的均值/方差对齐过去，
        # 消除"生成图整体偏亮/偏暖"导致的接缝。
        plate = Image.open(plate_path).convert('RGB').resize((W, H), Image.LANCZOS)
        plate = np.asarray(plate, np.float32) / 255.0
        if subject.any():
            ref = ~erase
            for c in range(3):
                m0, s0 = rgb[..., c][ref].mean(), rgb[..., c][ref].std() + 1e-6
                m1, s1 = plate[..., c][ref].mean(), plate[..., c][ref].std() + 1e-6
                plate[..., c] = (plate[..., c] - m1) * (s0 / s1) + m0
            plate = np.clip(plate, 0, 1)
            # 写回整个主体区（不只是露出带）：生成板全图都有纹理，整块替换
            # 后 bg.jpg 资产本身就没有残影；边界靠色彩迁移 + 2px 羽化兜住。
            w = np.clip(ndi.gaussian_filter(erase.astype(np.float32), 2.0), 0, 1)
            bg = rgb * (1 - w[..., None]) + plate * w[..., None]
        else:
            bg = plate
        print(f'背景板：{plate_path}（洞内生成·洞外原图·色彩迁移）')
    else:
        if subject.any():
            # 只修"轮廓内侧 band 宽的一圈"。
            #
            # 这一版之前是把整块主体（469×339，占画面 15%）当洞送进 LaMa，
            # 结果背景板中间糊出一大片灰白的雾，还带一条斜接缝 —— 因为那个洞
            # 同时跨了砖墙和桌面两种材质，模型没有"这里是一张桌子"的概念，
            # 只能填最省事的均值。眼一移，那片雾就当作"白斑"露出来。
            #
            # 而按位移预算算过：主体相对背景最多错开 reveal 像素，所以**只有
            # 紧贴轮廓内侧、宽度 = reveal 的那一圈**会被露出来。更深处永远被
            # 卡片盖着，补它不但没用，还把洞撑大、把补全质量拉垮。
            # 抹除范围要比语义遮罩**外扩 3px**。
            #
            # u2netp 在阈值 0.5 处切出来的边界，通常比物体真实轮廓**内缩**
            # 2~3px —— 而物体最深处的那条暗边、反光边恰恰就在这 2~3px 里。
            # 只按遮罩抹，轮廓会被完整留在背景板上：眼一移，那道虚线状的
            # 斜边就从背景里浮出来（放大看是断续的点，因为遮罩边界是像素锯齿）。
            #
            # alpha 仍然用原遮罩，所以卡片剪影一点都不变。
            # （erase/ring/hole 已在上方统一计算。）
            if '--debug' in sys.argv:
                sy, sx = np.nonzero(subject)
                md = float(np.median(d[obj])) if obj.any() else -1
                print('DIAG subj%%=%.1f obj%%=%.1f fg%%=%.1f obj&fg%%=%.1f obj&near%%=%.1f bbox x[%d,%d] y[%d,%d] dist_max=%.0f ring%%=%.2f hole%%=%.2f'
                      % (subject.mean() * 100, obj.mean() * 100, fg.mean() * 100,
                         (obj & fg).mean() * 100, (obj & (d < md)).mean() * 100,
                         sx.min(), sx.max(), sy.min(), sy.max(),
                         dist_in.max(), ring.mean() * 100, hole.mean() * 100))
            if ring_mode == 'lama' and lama is not None:
                # 洞的输入图里，**整个主体 + 它的边界**都要抹平。
                #
                # 这里有两层坑，叠在一起：
                # 1. LaMa 是整图重建模型，在 mask 边界处倾向于**复制输入**
                #    （它也被训练成洞外要还原）。所以只要输入里还留着轮廓，
                #    补出来的背景板上轮廓就原封不动 —— 眼一移就是第二台笔电。
                # 2. 而 u2netp 的遮罩边界本身有 1~2px 锯齿，正好压在笔电最深的
                #    那条轮廓线上。只抹 subject 内部的话，这条断续的深色点线会
                #    留在 mask 外面，被模型当成"该延续的边缘"复制进去。
                #
                # 所以抹平范围要比 subject **外扩 3px**，让轮廓整条消失。
                grow = ndi.binary_dilation(subject, B.disk(3))
                fill = B.push_pull(rgb, ~grow)
                pre = np.where(grow[..., None], fill, rgb)
                if '--debug' in sys.argv:
                    Image.fromarray((np.clip(pre, 0, 1) * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_pre.png'))
                    Image.fromarray((ring * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_ring.png'))
                bg = np.where(hole[..., None], tone_match(ring_fill(lama, pre, hole), rgb, hole), rgb)
            else:
                # 露出带的填充：最近的**真实背景**像素 + 局部模糊。
                #
                # 三个都试过，只有这条能用：
                # · LaMa 整块：洞跨砖墙/桌面两种材质，补出一片雾，还沿着轮廓
                #   糊出一条污浊的斜带；
                # · LaMa 只修一圈：它在 mask 边界复制输入，笔电轮廓原样留下，
                #   眼一移就是第二台笔电；
                # · push_pull：多尺度金字塔一路降到 6px，洞中心拿到的其实是
                #   **整幅图的平均色**——一块灰白的雾，正是"白斑"。
                #
                # 距离场取最近背景像素，颜色一定来自**最近的**那片材质；
                # 再模糊 9px 抹掉 Voronoi 分界。57px 宽的带里，这就是
                # "没有这台笔电时，那里应该是墙/桌面"最接近的估计。
                # src 一定要是**整个主体之外**，不能只排掉轮廓外 5px：
                # 洞（dilate(ring,5)）的内侧边界落在主体深处，而那里原本
                # 还在 src 里 —— 调和插值就变成"墙 ↔ 笔电机身"的过渡，
                # 补出一根比周围亮的竖条（左移时最显眼的那块）。
                # 主体内部一律不可信，只留真背景当边界条件。
                src = ~erase
                fill = harmonic_fill(rgb, src, band)
                # 替换范围必须是**整个主体 + 3px**（erase），不能只是露出带
                # （hole=ring 外扩 5px 只有一圈）。中心眼位时 subj.png 会盖住
                # 主体，但它的 alpha 软边处是半透明的 —— bg 上若还留着笔电
                # 原像素，就会透过软边浮出一层"半透明笔电 + Apple logo 残影"
                # （正是磨砂玻璃现象的另一半根因：fill 算了整块，写回却只写了
                # 一圈）。羽化 1.6px 保证替换边界无硬缝。
                w = np.clip(ndi.gaussian_filter(erase.astype(np.float32), 1.6), 0, 1)
                bg = rgb * (1 - w[..., None]) + fill * w[..., None]
                if '--debug' in sys.argv:
                    Image.fromarray((np.clip(fill, 0, 1) * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_fill.png'))
                    Image.fromarray((erase * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_erase.png'))
                    Image.fromarray((obj * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_obj.png'))
                    Image.fromarray((fg * 255).astype(np.uint8)).save(
                        os.path.join(outdir, '_dbg_fg.png'))
        bg = np.clip(bg, 0, 1)
        bg = outpaint(lama, bg, pad_x, pad_y, mirror_hf)

    subj_rgb = decontaminate(rgb, alpha, bg[pad_y:pad_y + H, pad_x:pad_x + W])

    # ── 主体卡片：裁到外接框，省掉大片全透明 ──
    if subject.any():
        ys, xs = np.nonzero(alpha > 0.01)
        x0, x1 = int(max(0, xs.min() - 6)), int(min(W, xs.max() + 7))
        y0, y1 = int(max(0, ys.min() - 6)), int(min(H, ys.max() + 7))
    else:
        x0, y0, x1, y1 = 0, 0, W, H

    Image.fromarray((bg * 255).astype(np.uint8)).save(
        os.path.join(outdir, 'bg.jpg'), quality=93, subsampling=1)
    if subject.any():
        card = np.dstack([subj_rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1]])
        Image.fromarray((np.clip(card, 0, 1) * 255).astype(np.uint8), 'RGBA').save(
            os.path.join(outdir, 'subj.png'), optimize=True)

    layers = [{
        'id': 'bg', 'file': 'bg.jpg', 'z': z_bg, 'order': 0,
        'rect': [-pad_x, -pad_y, W + 2 * pad_x, H + 2 * pad_y],
    }]
    if subject.any():
        layers.append({
            'id': 'subj', 'file': 'subj.png', 'z': z_subj, 'order': 1,
            'rect': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        })

    # 单层时没有相对位移，外扩只需要盖住自己那点位移（s_bg）；
    # 双层时最大位移来自主体卡，它决定了剪影旁要做多宽的一条带。
    need = reveal if subject.any() else s_bg
    have = min(pad_x, pad_y)
    meta = {
        'version': 3, 'mode': 'cards',
        'image': {'w': W, 'h': H},
        'scene': {'zRef': Z_REF, 'halfWM': HALF_W_M, 'eyeMax': EYE_MAX},
        'pad': {'x': pad_x, 'y': pad_y},
        'layers': layers,
        'qa': {
            'reveal_pct': round(need / W * 100, 2),
            'shift_subject_px': round(s_sub, 1),
            'shift_background_px': round(s_bg, 1),
            'reveal_px': round(need, 1),
            'pad_px': have,
            'pad_ok': bool(have >= need * 1.15),
        },
    }
    with open(os.path.join(outdir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f'主体占比 {subject.mean() * 100:.1f}%   z 主体 {z_subj}m / 背景 {z_bg}m')
    print(f'位移预算：主体 {s_sub:.0f}px  背景 {s_bg:.0f}px  露出 {need:.0f}px  '
          f'外扩 {have}px  {"OK" if meta["qa"]["pad_ok"] else "不足！"}')
    for p in ('bg.jpg', 'subj.png'):
        f = os.path.join(outdir, p)
        if os.path.exists(f):
            print(f'  {p:<10} {os.path.getsize(f) / 1024:6.0f} KB')
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
