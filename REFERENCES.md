# 开源参考项目

按「能不能直接解决我们当前的问题」排序，不是按 star 数。

## 一、同题：头部追踪 + 离轴投影

### 1. RmaNMetaverse/Cam2Hologram-Window-into-3D-reality-

- https://github.com/RmaNMetaverse/Cam2Hologram-Window-into-3D-reality-
- JavaScript / 2026-08 / 2★
- 技术栈与本项目完全一致：MediaPipe FaceLandmarker + three.js 离轴投影。

**最值得抄的是它的加载策略**（`js/tracker.js`）：

```js
const LOCAL_BASE = new URL('../vendor/', import.meta.url).href;

const BUNDLE_SOURCES = [
  { label: 'local',    module: `${LOCAL_BASE}mediapipe/vision_bundle.js`, wasm: `${LOCAL_BASE}mediapipe/wasm` },
  { label: 'jsdelivr', module: 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/vision_bundle.mjs', ... },
  { label: 'unpkg',    ... },
];
```

它把 `vision_bundle` + wasm + 模型全部 vendored 进仓库（仓库 39MB 里大头就是这些），正常安装下**一次网络请求都不发**；CDN 条目只作为残缺 checkout 的兜底。而且路径用 `import.meta.url` 锚定，所以部署在 GitHub Pages 的 `/repo-name/` 子路径下也不会 404。

> 本项目的模型已经走「本地 task → base64 副本 → Google」三路，但 **wasm 运行时仍只走 CDN**。把它一起 vendor 掉，断网时才算真的能跑。

### 2. icurtis1/off-axis-sneaker

- https://github.com/icurtis1/off-axis-sneaker
- TypeScript / React / 435★
- Johnny Chung Lee 式 head-coupled perspective，把球鞋「放在屏幕后面」。

值得借鉴的产品化细节：

- `CalibrationWizard.tsx`：引导用户输入屏幕尺寸、眼睛高度、观看距离，算出物理单位，存本地
- `CdnStatusIndicator.tsx`：把 CDN 可达性做成可见状态，而不是让用户对着白屏猜
- `useFaceLandmarker` / `useFaceMesh` 拆成两个 hook：媒体流与推理分离，推理失败可以单独重试
- 独立渲染循环：摄像头帧率与渲染帧率解耦

### 3. Dushannavodya/depthShift

- https://github.com/Dushannavodya/depthShift
- Vue / 真离轴矩阵

离轴公式与本项目**完全一致**，可以互相验证：

```js
const left   = near * (screenLeft   - eye.x) / eye.z;
const right  = near * (screenRight  - eye.x) / eye.z;
const bottom = near * (screenBottom - eye.y) / eye.z;
const top    = near * (screenTop    - eye.y) / eye.z;
projectionMatrix.makePerspective(left, right, top, bottom, near, far);
```

差别是它没有传 `coordinateSystem`——因为它只跑 WebGL（NDC 深度 `-1..1`）。我们跑 WebGPU，必须传 `renderer.coordinateSystem`（深度 `0..1`），否则画面直接消失。它另外把 `near/far` 按房间深度动态放宽，避免远处被裁。

### 4. charliegerard/interactive-frame-tfjs

- https://github.com/charliegerard/interactive-frame-tfjs
- JavaScript / Three.js + TensorFlow.js / 107★

艺术装置式实现，代码量小、可读性高。不含 3DGS，纯 head-coupled perspective，适合当最小可运行骨架读。TensorFlow.js 版人脸关键点比 MediaPipe 慢，但依赖更少。

### 5. umshere/headtrack-3d-window

- https://github.com/umshere/headtrack-3d-window
- Apache-2.0 / 原生 ES Module，无构建步骤

它的取舍很明确：**放弃自动测屏幕，直接让用户填物理尺寸。**

```js
const DEFAULT_IPD = 0.063;     // 63mm
const SCREEN_WIDTH_M = 0.28;   // 用户自己量
```

README 里直接写「For better realism: measure your actual screen width」。我们走的是"对角线 + 视口宽高比推算"，开箱即用但误差会进 z 的估计；它那条路更准但要求用户动手。两条路的差别值得保留成 A/B 选项。

### 6. MindDock/off-axis-demo（本项目的前身）

- https://github.com/MindDock/off-axis-demo
- 34★ / MIT / Three.js + MediaPipe FaceMesh

它的实现比想象中朴素：

```js
const SCREEN_WIDTH_CM = 30.0;
const SCREEN_HEIGHT_CM = 20.0;
const BOX_DEPTH_CM = 80.0;
const SENSITIVITY = 2.5;   // 头部位置直接乘一个系数
```

取虹膜 468/473，位置**乘灵敏度**后直接喂给离轴矩阵，z 用固定距离 + `LERP` 平滑。也就是说它没有针孔模型、没有瞳距反解深度 —— 我们现在的 z 是由 `z = z_ref × ipd_ref / ipd` 反解出来的，前后移动是真的会动，它那套只是"左右晃得像"。这也是它看起来"像看一个盒子"、我们看起来"像一扇窗"的原因。

### 7. 其它小体量实现（想吃透数学时读）

| 仓库 | 说明 |
| --- | --- |
| https://github.com/SpaceT1934/virtual-window-tracker | MediaPipe + Three.js 虚拟窗，2026-09，结构清晰 |
| https://github.com/fenollp/uniVR | 纯 GLSL + 单摄像头，软件实现的 head-coupled perspective |
| https://github.com/jsimonson2013/aframe-head-tracker-controls-component | A-Frame 组件形态，接口设计可参考 |
| https://github.com/sangil1018/off-axis-projection | React Three Fiber + MediaPipe，含校准向导与麦克风电平驱动的抖动 |

## 二、3DGS 渲染器（当前用 three 内置，可替换）

three r186 的 `GaussianSplat` 有个硬限制：NodeMaterial 来自 `three/webgpu`，**只有 WebGPU 能渲染**。没有 WebGPU 的机器（老 Safari、部分集显）会直接看不到模型。下面这些能解决：

| 仓库 | 星 | 许可 | 关键点 |
| --- | --- | --- | --- |
| https://github.com/sparkjsdev/spark | 3784 | MIT | World Labs 出品。主打「98%+ WebGL2 覆盖」，支持 PLY/SPZ/SPLAT/KSPLAT/SOG，**多 splat 正确排序、多视角同时渲染** |
| https://github.com/mkkellogg/GaussianSplats3D | 2920 | MIT | three.js 老牌实现，社区案例最多 |
| https://github.com/WilliamLiu-1997/Gaussian-Splat-Lite | 215 | Apache-2.0 | WebGL2 + WebGPU 双后端，SPZ/SOG/RAD、流式 LOD，要求 three ≥ 0.186 |
| https://github.com/playcanvas/supersplat | 10434 | MIT | 编辑器而非运行时：裁剪、压缩、导出 SOG，用来预处理素材 |
| https://github.com/KeKsBoTer/web-splat | 307 | MIT | Rust + WGPU 实现，性能上限参考 |

**Spark 的「多视角同时渲染」和离轴投影是天然搭配**：离轴的本质就是让屏幕四角各自对应一条视线，一个场景同时被多个视锥采样。

## 三、追踪基础件

| 项目 | 说明 |
| --- | --- |
| https://github.com/google-ai-edge/mediapipe | FaceLandmarker 478 点，含虹膜 468/473（我们取眼睛位置就靠这两个点） |
| https://github.com/jeeliz/jeelizFaceFilter | 老牌 WebGL 人脸追踪，模型小、启动快，适合低端机 |
| https://github.com/brownhci/WebGazer | 纯眼动（不动头）方案。与本项目互补：头动给视差，眼动给交互焦点 |

## 四、单图深度视差（我们现在的深度网格方案，同行是怎么做的）

### 1. Tiefling

- MIT，纯浏览器，`DepthAnythingV2`（ONNX）+ three.js，复刻 Facebook 3D Photo
- 关键一句（README）：*"expands the depth map at edges to reduce stretching artifacts"*

**和我们踩的坑完全一样，解法也一样**：深度图边缘不往外补，物体挪开就会露出拉伸的边。我们是 push-pull 外扩 + 底版补洞，它是把深度图本身往外扩。区别是它把深度推理放在浏览器里跑（快机器也要 ~20s），我们是离线烘焙（0.5s/张，产物 90KB）。**结论：这条路上"离线烘焙 + 运行时只读图"是对的。**

### 2. murtilli/parallax-maker

- Python + Dash，做 2.5D 动画卡片，可导出 glTF
- 核心做法：**用 SAM（Segment Anything）做实例分割来切卡片，而不是用深度阈值**

这是比我们更稳的一步。我们用「深度阈值 + 导向滤波」切笔电和桌面，边界依赖深度图精度；它用语义分割，边界直接贴在物体轮廓上。代价是要跑 SAM（重），收益是**"桌上笔电 vs 后面白墙"这种语义边界不会切错**。它还带 inpainting（把遮罩区域补成背景），和我们的 `near_plate.jpg` 同一件事。

### 3. metrovoc/spatial-scene

- FastAPI + PyTorch + React Three Fiber
- 流程：深度估计 → inpainting 补洞 → 分层渲染（自称 LDI，Layered Depth Image）

**和我们的「满幅背景底版 + 剪影近景」是同构的**，只是它把 inpainting 放在服务端。可以作为"这套结构是业界标准解"的旁证。

### 4. SLIDE: Single Image 3D Photography（ICCV 2021）

- 论文 + 社区复现（`macvincent/Single-Image-3D-Photography`）
- 提出 **soft layering**：层与层之间是软的 alpha 过渡，而不是硬切

正好命中我们的短板。现在前景是硬 alpha，遇到头发、树叶、纱窗这种"半透明薄结构"会切出一圈硬边。soft layering 是这类问题的正统解法，但需要额外的 matting 模型，暂时不做。

### 5. activeguild/render-image-depth

- Next.js + transformers.js，浏览器内跑 Depth Anything，按深度切 **2–20 层**透明平面

值得注意：它证明**层数多到一定程度，"每层整块平移"也能看起来顺滑**。反过来说，我们旧的「三层贴片」之所以像贴纸，不是层数少，而是**每层内部深度恒定、整块刚性平移**。这个对照能解释为什么深度网格（逐顶点 z）是质变而不是量变。

## 五、追踪的两种非主流路线（值得知道）

| 项目 | 做法 | 什么时候该用 |
| --- | --- | --- |
| https://github.com/quasarbright/head-tracking-window | 手机贴眼镜当追踪器，看屏幕上的 ArUco 标记，`solvePnP` 解位姿，WebRTC 传回 PC | 精度要求高、或摄像头不能正对脸。比单目人脸关键点准一个量级 |
| https://github.com/selop/pokebox | 桌面用 MediaPipe，**移动端改用陀螺仪**，键盘/鼠标兜底 | 手机上摄像头追踪又费电又不稳，陀螺仪给"倾斜视差"更划算 |

两者都印证了同一件事：**摄像头追踪一定要有降级路径**，不能只留"失败"这一种状态。

## 六、MediaPipe 运行时的加载，社区结论

- `google-ai-edge/mediapipe#4827`：官方维护者明确说 *"You can also use a custom location for the WASM files by passing it to `FilesetResolver.forVisionTasks('http:/...')`"*
- `#5495`：**同时加载 legacy `@mediapipe/pose` 和 `@mediapipe/tasks-vision` 会撞出两个 emscripten runtime**，症状就是各种 `Aborted` —— 一个页面只能有一份
- 通行做法（Cam2Hologram、parallax 类项目）：把 `vision_bundle.mjs` + `wasm/` 一起 vendored 进仓库，CDN 只作兜底
- `#5380` / `#5512` 一类 issue 里反复出现 `NetworkError when attempting to fetch resource.`，回复基本都是「路径不对 / 服务器没给 `application/wasm`」。但我们实测到第三种成因：**环境把 fetch 通道整体禁掉**（`file://`、sandbox iframe），此时报错和路径、MIME 全都无关 —— 判别方法是看同源相对路径是否也挂
- 打包器侧的同类思路：Vite 的 `assetsInlineLimit` / `?inline`、webpack 的 `asset/inline` 都是把二进制变 base64 塞进 JS，绕开独立请求。`emscripten` 自己也支持 `-sSINGLE_FILE=1` 生成「wasm 内嵌」的单文件 —— 官方给的正是这条路

## 七、分层与补洞：这一轮真正动手用上的方案

前面第四节是「同类项目怎么切层」，这一节是**毛边 / 背景空白**这两个具体问题的出处。

### 1. vt-vl-lab/3d-photo-inpainting（CVPR 2020）—— LDI 的原始定义

- https://github.com/vt-vl-lab/3d-photo-inpainting

它定义了 LDI 这个表示本身：近景 LDI + 满幅背景 LDI，被挡住的像素靠 context-aware inpainting 造出来。我们这套结构（剪影近景 + 满幅背景）与它同构，三处直接借了它的思路：

- **深度边缘修复**：单目深度边缘是糊的，它靠反复的 bilateral median / cumulative minimum 把阶跃收到几像素。我们换成三级引导滤波（r=20/10/5，eps 递减，各 2 次迭代 + `median_filter(3)`），目的相同，但对 960×600 只要几十毫秒。
- **先外扩再修**：它修遮挡区之前会先把边缘像素摊进去，否则补出来的内容和边界对不上。我们换成「颜色向 alpha 外扩散（push-pull）+ 软边去色」——注意这一步必须在补洞**之前**做，否则 LaMa 拿到的是被污染的边缘。
- **露出带 = 视差位移量**：`shift ≈ e·zRef/(halfW) · (1/z_fg − 1/z_bg)`。把眼位上限代入，就知道最多只会露几十像素，不必修整块前景。

它没解决的是细杆和头发 —— 硬深度分层表达不了薄结构，这正是下一篇的动机。

### 2. SLIDE（ICCV 2021）—— 软分层才是薄结构的正解

- 论文：*SLIDE: Single Image 3D Photography with Soft Layering and Depth-aware Inpainting*
- 社区复现：`macvincent/Single-Image-3D-Photography`

论文对 3D-Photo 那类模块化系统的批评非常直接：**硬深度分层无法表达头发、树叶、栏杆这类薄结构**。它的解法是给每个像素一个 alpha，层与层之间软混合，并且 inpainting 训练时就带深度先验（depth-aware），让补出来的背景知道自己该在哪个深度上。

我们现在的对应物只是它的廉价近似：

```python
alpha = np.maximum(soft_alpha(fg, lum), a_obj)     # 距离场软边 1.6px + 语义软边取大
alpha = guided(lum, alpha, r=2, eps=4e-3)          # 吸附到原图轮廓
```

够用（笔电、桌面、柱子这类实心物体），但**不等于软分层**：真做法要给每个像素一整套 (color, depth, alpha) 并逐像素合成，渲染侧的工作量完全是另一个量级。这也是我们认为「薄结构」还留在待办里的原因 —— 不是调参能解决的。

### 3. 抠图模型：u2netp 只是起点

| 模型 | 体积 | 定位 | 我们怎么用 |
| --- | --- | --- | --- |
| `u2netp` | 4.3 MB | 通用显著物体，320×320 | 现在用的。只抠"看得见的主体"，桌面这类支撑面它不管 —— 所以还要深度片补 |
| BiRefNet / RMBG-2.0 | ONNX int8 约 350 MB | 发丝级、能出半透明边 | 离线烘焙可以换，前端别想 |
| MODNet | ~25 MB | 人像实时 / 视频 | 人像场景专用，比 u2netp 干净 |
| SAM 2 | 数百 MB | 实例分割，要 prompt | parallax-maker 那条路（按实例切卡片） |

结论没变，但更清楚了：**深度片（支撑面）+ 深度断层（边界）+ 语义分割（谁是物体）三路互补**，换更强的抠图只提升其中一路。风景照里这一路本来就应该关掉 —— 见 README 里的语义守卫。

### 4. 浏览器里算深度：已经不需要服务器

| 项目 | 做法 |
| --- | --- |
| https://github.com/Xenova/webgpu-realtime-depth-estimation | Transformers.js + WebGPU，摄像头/图片流式出深度图 |
| https://github.com/sabicalija/relief | Vue3 + TresJS，Depth Anything V2 **Small（约 25 MB）直接在浏览器里跑**，GitHub Pages 部署，已实测可用 |
| https://github.com/activeguild/render-image-depth | transformers.js + 按深度切 2–20 层平面 |

也就是说「用户拖一张图进来就出效果」在技术上已经通了：深度 1–3s，抠图几百毫秒。**卡的不是深度，是补洞** —— LaMa 198 MB，前端跑不动；轻量替代方案（512 输入的快速修复模型）质量会掉一档。

### 5. 所以「任意一张照片」的三条路

| 路径 | 首屏成本 | 单张耗时 | 质量 | 判断 |
| --- | --- | --- | --- | --- |
| 离线烘焙（现在） | 0 | 13–30 s（M 系列，含两次 LaMa） | 最好 | 适合固定素材 / 发布 |
| 本地服务（下一步） | 0 | 同上，拖图即出 | 最好 | 照片丢给 `build_ldi.sh`，前端 `?ldi=` 直接看；一条 HTTP 接口就够 |
| 纯前端 | 约 30 MB 模型（深度 25 + 抠图 4.3） | 2–5 s + 补洞 | 中（没有 LaMa，只能方向插值 / 平滑外推） | 可行，但要接受细杆处发糊 |

短期最划算的是第二条：前端一行 `?ldi=` 已经能指向任意目录，缺的只是一个把「上传 → 烘焙 → 回 JSON」串起来的服务端点。

## 八、这一轮踩的坑（补洞部分，都是实测）

1. **两侧插值填露出带 → 长出「梳齿」**。`directional_fill` 按行/列就近插值，但露出带紧贴物体，插值时会把物体边缘的颜色一圈圈拉进洞里。白柱子左边直接长出 48px 宽的梳齿，比原来的双影还显眼。
2. **露出带用外接矩形送 LaMa → 把背景擦成浆糊**。环状 mask 的 bbox 常常覆盖半个画面（笔电场景把背后那把椅子、杯子一起抹了）。细环必须原样送进去，LaMa 本来就擅长窄洞。
3. **LaMa 直接 `resize(512,512)`** → 960×600 被压成正方形，砖块被压扁，模型按变形纹理生成，砖缝方向全错。必须等比缩放 + 居中补边。
4. **紧贴轮廓的 mask** → 模型让洞内与边界一致的最省事办法就是再画一个物体（笔电「躺平」在原地）。外接矩形才治得住。
5. **LaMa 补完整体偏亮/偏冷** → 墙有自上而下的明暗渐变，它在洞里没有这个概念。低频对齐：只校正边界那点差，再 push-pull 渗进洞里。
6. **背景层把支撑面也填了** → 同一块桌面在两层里深度不同，眼一动就错开、露出背景填的纯色。支撑面必须保留原像素 + 原深度。
7. **风景照把桥当主体** → u2netp 的显著性是"画面里最大最集中的东西"，不是"离镜头最近的物体"。语义守卫：面积 > 30% / bbox > 55% / 宽 > 85% / 高 > 90% 直接弃用语义。
8. **onnxruntime 退出时自杀**：`recursive_mutex lock failed: Invalid argument`，图早写完了但退出码变 1，`set -e` 的脚本会被打断。末尾 `os._exit(0)` 跳过析构即可。

## 九、已经落地 / 还没做

**已落地**

0. **fetch 被禁时的第三层兜底**（2026-10-21 追加）：实测 `file://` 与 sandbox iframe 两种受限环境下，`fetch` / `XHR` / 同源 `import` 全部失败，但 classic `<script>`、`fetch(blob:)`、`DecompressionStream` 三条通道可用。于是 `tools/pack_wasm.py` 把 wasm 压成 gzip+base64 的普通脚本（9.0MB → 2.7MB → 3.7MB），页面在 fetch 三源全败后注入它，解码 → gunzip → `blob:` 交给 MediaPipe。模拟 fetch 全失败实测 **2.6s** 到「等待人脸」。这与 emscripten 官方 `-sSINGLE_FILE=1` 是同一个思路，只是我们在运行时做，不重新编译。

1. **vendor MediaPipe 运行时**：`wasm/` 放仓库里，`FilesetResolver.forVisionTasks('wasm')` 优先，CDN 两源兜底。
   实测数据（别再被 curl 的 `size_download` 骗了，那是**压缩后**大小）：

   | 版本 | curl 报的大小 | 实际 `vision_wasm_internal.wasm` |
   | --- | --- | --- |
   | 1.1.1 | 3.7MB | **≈12MB** |
   | 0.10.14 | 3.3MB | **9.4MB** |

   两个版本都是 9MB 量级，**换版本救不了**，只能本地化。现在用 0.10.14（9.4MB，比 1.1.1 小 2.6MB），只 vendor SIMD 版，`nosimd` 那 9.3MB 删掉让非 SIMD 浏览器走 CDN。
   9.4MB 的文件在国内从 jsDelivr 拉，几乎必然断在中途 —— 断点处就是那句 `Aborted(both async and sync fetching of the wasm failed)`。这是这句话的真正成因，和模型文件、和 CORS 都无关。
2. **下载权收回自己手里**：fileset 实测就是 `{ wasmLoaderPath, wasmBinaryPath }` 两个字符串，换成 `blob:` URL 照样认。于是 `fetchBytes` 自己带 `AbortController` 超时、reader 百分比、`\0asm` 魔数校验 ——
   - 超时中断只 abort 我们自己的 fetch，不会把 emscripten 内部状态搞坏（直接交给它下载时，被打断之后就只会抛 `Aborted`）
   - 半截文件在开跑前就能识别，不用等它炸一句看不懂的英文
   - 顺便发现并修掉一处浪费：页面加载 350ms 自动起一次追踪 + 用户点「重启追踪」= 9.4MB 下载两遍（`performance.transferSize` 两条都是 9424286），加 blob 缓存后只剩一次
3. **错误文案翻译**：emscripten 的原话对用户没有意义，改成「wasm 运行时没下载下来」并区分模型 / wasm / 摄像头三类原因。

实测（`--use-fake-device-for-media-stream`）：

| 场景 | 点击到「等待人脸」 |
| --- | --- |
| 本地 http | 493 ms |
| GitHub Pages | 6.0 s（wasm 走 Pages 同源，gzip 到 2.9MB） |
| 改之前（直连 CDN） | 28.4 s（本地档 20s 超时被打断，被迫换源） |

4. **LDI 烘焙管线**（`tools/build_ldi.py` + `build_ldi.sh`，一条命令出六个文件）。毛边从「8px 模糊边 + 4px 网格台阶」收到「1.6px 距离场 + 引导吸附 + 2px 网格」；背景空白从「35% 的洞靠均值填」改成「只修露出带，物体用 LaMa、支撑面不动」。离线残影指标：笔电 10.6%、船景 2.7%（`tools/verify_ldi.py`）。
5. **语义守卫 + 自动降级**：u2netp 只抠显著物体，风景照里会把桥当主体。面积 / bbox / 长宽比超限就弃用语义，退回纯深度片；LaMa 缺失时退回插值与平滑外推。
6. **`?ldi=` / `?photo=` 查询参数**：换一张照片不必改代码，烘焙到任意目录指过去即可。

**还没做**

7. **3DGS 换 Spark**：`GaussianSplat` 在无 WebGPU 的机器上完全不可用；Spark 覆盖 WebGL2，且原生支持多视角，与离轴更搭。
8. **物理校准向导**（抄 off-axis-sneaker / pokebox）：让用户填屏幕对角线、观看距离、眼高并存 `localStorage`，现在靠滑杆推算是"够用但不准"。
9. **SAM 切层**（抄 parallax-maker）：把深度阈值换成语义分割，专治"笔电 vs 白墙"这类边界。
10. **软分层（抄 SLIDE）**：现在 alpha 只做 1.6px 软边，细杆/头发/树叶还是跟着深度片轮廓走。真做法要逐像素 (color, depth, alpha) 三元组 + 软合成，属于渲染侧重构。
11. **上传即出**（本地服务或纯前端）：`?ldi=` 已经把前端接口留好了，缺的是「上传 → 烘焙 → 回目录」的服务端点。纯前端路线要解决 LaMa 的 198MB。

