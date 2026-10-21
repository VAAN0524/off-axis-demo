# 头部视差 · 离轴投影 3DGS 查看器

用摄像头算出眼睛在屏幕前的位置，把屏幕四个角对应的视线塞进投影矩阵，网页里的内容就会随头部移动产生「绕进屏幕」的视差。渲染走 WebGPU，3D 内容可以是照片分层、也可以是 3D 高斯泼溅（3DGS）。

单文件 `index.html`，不需要构建，CDN importmap 直接跑。

```
┌─ 摄像头 ─→ MediaPipe FaceLandmarker（478 关键点）
│                ↓ 虹膜 468/473 → 瞳距 + 位置
│            眼睛坐标 (x, y, z) 单位米
│                ↓ One Euro 滤波
└─→ 离轴投影矩阵（l/r/t/b 四边不对称）→ 相机 → 渲染
```

## 运行

```bash
cd off-axis-demo
python3 -m http.server 8123
# 打开 http://127.0.0.1:8123/index.html
```

必须用 http（或 https）打开，**不要双击 `index.html`**：`file://` 下浏览器会拦掉所有同目录文件，照片加载不出来、人脸模型也会报 `NetworkError`。页面检测到 `file://` 时会自己弹出醒目提示。

线上版（GitHub Pages）同样是 https，摄像头和本地模型都正常。

## 两种投影模式

| 模式 | 做法 | 特征 |
| --- | --- | --- |
| 真离轴 | 屏幕轴对齐，光轴 = 屏幕法线，四边不对称全塞进投影矩阵 | 屏幕四角在任意眼位都精确落在 NDC (±1, ±1)，像透过真实的窗框看 |
| 相机平移 | 相机跟着眼睛平移后 `lookAt` 场景中心 | 便宜，但灭点被锁在画面中央，房间会跟着「转」，丢掉了离轴的那部分视差 |

判断依据看走廊的灭点：近似模式下灭点始终在屏幕中心。

核心代码（`applyOffAxisCamera`）：

```js
const d = eye.z;                 // 眼到屏幕平面的距离
const k = NEAR / d;
const l = (-screen.w / 2 - eye.x) * k;
const r = ( screen.w / 2 - eye.x) * k;
const b = (-screen.h / 2 - eye.y) * k;
const t = ( screen.h / 2 - eye.y) * k;

camera.projectionMatrix.makePerspective(l, r, t, b, NEAR, FAR, renderer.coordinateSystem);
camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
camera.position.set(eye.x, eye.y, d);
camera.quaternion.identity();    // 不 lookAt
```

`renderer.coordinateSystem` 不能省：WebGPU 的 NDC 深度是 `0..1`，WebGL 是 `-1..1`，传错整幅画面会消失。

## 眼睛位置怎么算

1. 优先用虹膜中心 468 / 473，取不到回退到眼角 33 / 133 / 362 / 263。
2. 水平位置由双眼中点相对画面中心的偏移换算（按假定水平视场 60°），正负号取决于前置摄像头镜像，面板可切。
3. 深度用针孔模型 `z = z_ref × ipd_ref / ipd`，比「魔法常数除以眼距」稳定得多——瞳距是唯一随距离单调变化的量。
4. 滤波用 One Euro（低速重平滑、快速转头低延迟），不是固定指数平滑。
5. 按 `C` 把当前头部位置记为中立位。

## 内容

- **照片分层**：三层真实照片，分别在 0.48 / 1.05 / 1.85 m，边缘做了 alpha 羽化，遮挡仍按真实深度。
- **3DGS**：点「载入 3DGS 样本」加载 `model.spz`；也可以把 `.spz / .ply / .splat / .ksplat` 直接拖进窗口。扫描件朝向不对时点「旋转 90°」。
- **没摄像头也能用**：鼠标 = 头部，滚轮 = 前后。

## 人脸模型的四个来源

按顺序尝试，任意一条通就能起：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `models/face_landmarker.task` | `fetch` 读取，有下载百分比 |
| 2 | `models/face_landmarker.b64.js` | 普通 `<script>` 加载 base64 副本，**不走 CORS**，`file://`、断网、iframe 里都能用 |
| 3 | Google 存储 | 境外网络可达时的兜底，实测 281 KB/s，3.5MB 要 13 秒，国内多半连不上 |
| 4 | 拖拽 / 「选择模型文件」 | 手动指定 `.task`，`file://` 下唯一能自洽救回来的办法 |

`models/face_landmarker.b64.js` 重新生成：

```bash
python3 - <<'PY'
import base64, pathlib
raw = pathlib.Path('models/face_landmarker.task').read_bytes()
b64 = base64.b64encode(raw).decode('ascii')
pathlib.Path('models/face_landmarker.b64.js').write_text(
    'window.__MP_FACE_MODEL_B64="' + b64 + '";\n', encoding='utf-8')
PY
```

追踪初始化分四段，每段独立超时，失败原因会写在面板里：摄像头 12s → 运行时 15/25s（jsDelivr → unpkg）→ 模型 45s → GPU 初始化 6s（不过就退 CPU）。

## 已知的坑

- **3DGS 只能走 WebGPU**：`GaussianSplat` 的 NodeMaterial 来自 `three/webgpu`，WebGL 后端渲染不出来。页面会自动降级到 WebGL 并说明原因。
- **WebGPU 后端不支持 `LineLoop`**：画矩形框要用 `LineSegments` 手工闭合。
- **`GaussianSplat` 的导入路径**是 `three/addons/objects/GaussianSplat.js`，不在 `loaders/` 下；PLY 要用 `GaussianSplatPLYLoader`，普通 `PLYLoader` 读出来没有协方差和球谐。
- **扫描件朝向不一致**：本例 `model.spz`（three.js 官方 `lion.v3.spz`）需要 `rotation.set(0, π, π)`。朝向参数只传在调用处，加载函数保持通用。
- **越远的内容位移越大**，看着反直觉但正确：屏幕是固定的窗框，内容都在窗后。眼位横移 16 cm 时，前景卡片移动约 8 cm、背景约 12 cm。

## 素材

- 照片：`picsum.photos`（id 43 / 50 / 48），本地取图失败时自动回退到远端同款。
- 3DGS 样本：three.js 官方示例 `examples/models/spz/lion.v3.spz`（195k splats，4.1MB）。
- 人脸模型：MediaPipe `face_landmarker` float16。

## 授权

`model.spz` 与 `img/` 仅用于演示，版权归原作者；生产环境请替换为自己的素材。
