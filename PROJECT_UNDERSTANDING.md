# SkyboundTOTK · 项目完整理解

> 生成时间：2026-10-29 · 方法：全仓库静态通读（无游戏数据、未构建、未运行）+ git 历史核对。
> 分工：5 个并行深读报告见 [.understand/](.understand/)（01 lift/build、02 hos 运行时、03 NVN 桥接、04 渲染器、05 shader/Web）。
> 本文是它们的汇总与交叉验证；标【推测】处为从代码反推、未在实机验证的结论。

---

## 0. 一句话

**SkyboundTOTK 是把 Switch 版《塞尔达传说：王国之泪》1.0.0 的 AArch64 机器码在构建期整体翻译成 C、再本地编译成原生 Windows x64 程序，并用手写的 Horizon OS 服务层 + 手写 WebGPU 渲染器替代整台 Switch 的"静态重编译移植"项目** —— 不是模拟器，不含游戏数据。

| 事实卡 | 值 |
|---|---|
| 目标 | Windows x64，TOTK **1.0.0 基础版**（build ID 硬编码，其它版本不支持） |
| 图形 API | 游戏侧 NVN / Maxwell；主机侧 **WebGPU（WGSL）**，桌面经 wgpu-native 落到 Vulkan/D3D12 |
| 仓库规模 | 576 个跟踪文件；项目自有约 **60k 行**（hos 14.7k / gpu-render 24.3k / lift 1.6k / rt 526 / tools 944 / build.py 793）；内置 suyu 着色器重编译器 246 文件 **48.4k 行** |
| 运行时 HLE | **396 个** `HLE()/imp_*` 函数；**25 个** SVC；**368/482** 个 NVN API（76%） |
| 埋点/开关 | 代码里共 **267 个**不同环境变量（`RX_*`/`HOS_*`/`WGPU_*`/`A64_*`/`TOTK_*`） |
| 许可 | 自有代码 MIT；**suyu / yuzu 部分是 GPL-2.0/3.0**，因此 a64emit.dll、生成的运行时、提升出的 C、任何游戏构建**整体是 GPL**；游戏数据属于任天堂，不含在仓库 |
| 成熟度 | 可构建、可启动、可玩到开放世界（README 与代码注释里的实测帧率）；仍有画面缺陷；无 CI、无自动化测试 |

---

## 1. 这是什么 / 不是什么

**是**：一个**游戏专用的静态重编译移植（static recompilation port）**。三条腿：
1. **构建期翻译**：ExeFS 里的 `main`+`sdk` 全部 ARM64 代码 → C（约 28 万函数 / 1.1 GB C，文档值）。
2. **运行时 HLE**：把 `nn::*`（任天堂 SDK）边界整体换成 Windows 上的 C 实现，svc 换成一个 switch，客户机内存是一块固定 4 GB 宿主区间。
3. **手写渲染器**：接管 NVN，把 Maxwell 图形语义翻译成 WebGPU 调用与 WGSL。

**不是**：
- 不是通用 Switch 模拟器（无固件、无内核、无 IPC、无 JIT/解释器）；
- 不含任何游戏文件、密钥、存档；构建需要用户自备 1.0.0 ExeFS+RomFS，且**只支持这一个版本**；
- 不做 NSP/XCI 解包（`tools/nx.py` 有能力，但 `build_game.py` 只吃已解包目录）。

---

## 2. 两条主数据流（理解全局的关键）

### 2.1 构建期：`build.bat` → `dist/SkyboundTOTK/`

```
game/exefs/{rtld,main,subsdk0..9,sdk}
   │ tools/nso.py            NSO 解析：三段 LZ4 / MOD0 / dynamic / 符号 / 重定位 / init_array / .eh_frame_hdr
   ▼
hos/nx_image.py             「我们就是 rtld」：模块定址(0x08000000 起,1MB 对齐) + 重定位 + import 绑定 + entry 集合
   ▼
build/<name>/image/{image.bin,image.json}
   │ lift/a64_lift.py + a64emit.dll(suyu 单指令翻译器, GPL)
   ▼
build/<name>/gen/{code_NNN.c, code_hot.c, a64_table.c, a64_hooks.c, a64_fpx.h, a64_suyu.*, a64_stubs.c, hos_image.c}
   │ build.py native: clang -O3 + rt/ + hos/ + gpu/render + libopus (-O2)
   ▼
build/<name>/native/totk.exe   →  build_game.py package  →  dist/{SkyboundTOTK.exe, image.bin, image.json, romfs/, wgpu_native.dll, SDL2.dll}
```

关键点（每条都能用 `文件:行号` 复核）：
- **两级缓存语义不同**：`lift` 阶段**全有或全无**（`build_game.py:68-85` 的 SHA-256 覆盖 lift/hos/rt 源码 + build.py + nso.py + hot_funcs.txt + exefs 模块字节；不匹配就要人工挪走 `build/`），`native` 阶段**逐目标文件增量**（`build.py:463-474`）。
- **import 的接管是"改调用目标"**，不是二进制打桩：被 HLE 的符号在**定义处**挂钩（游戏自己的调用和 SDK 内部互调都落到同一个宿主函数），未定义符号绑到 `0x07000000` 起的 16 字节 trampoline，然后 lift 把它变成 `imp_<mangled>` 调用。
- **没实现的 API 不阻断链接**：`gen_stubs.c` 生成"日志一次、返回 0"的桩（`build.py:174-183`），于是第一天就能跑、日志告诉你下一步该实现什么。加了新 HLE 只需 `build.py stubs`，不必重 lift。
- **两处"注入"机制**让游戏补丁留在手写 C 里而不是手改生成物：`ENTRY_HOOK(0x...)`（函数入口拦截，6 处）、`ENTRY_HOOK_SYM`（12 处）、`SPLICE_AFTER(0x..., "anchor")`（函数中间插入，9 处）——`build.py:63-151` 在 native/web 阶段把它们拼进生成的 C。

### 2.2 运行期：从 `main()` 到 `nnMain` 到一帧

```
hos_main.c: main()
  ├ 保留 4 GB 客户机窗口 @ A64_HOST_BASE=0x100000000000
  ├ 读 image.bin → 0x08000000（尺寸必须等于 hos_image_end）
  ├ hos_mem_init / hos_mods_init / hos_boot_main_thread(主线程 = 当前宿主线程)
  ├ hos_prof_start / hos_display_start(建窗口 + rx_start)
  └ call_guest 依次：_init_libc0 → nnosInitialize → _init_libc1 → nninitStartup → _init_libc2 → 各模块 DT_INIT → **nnMain**
```

一帧的四个角色：

```
 [游戏线程/多线程]  lifted C  ── svc → hos_svc.c(switch,25 个)
                            └─ nn::* → imp_* (396 个 HLE)
                            └─ NVN 调用 → nvnBootstrapLoader 钩子(nvn_trace.c:130)
                                 ├─(默认) nvn_native.c 368 个函数 → 记录 NOP_* 流
                                 │        └ rx_nvn.cpp「NVN replay」线程 → rx_draw/rx_texture/... → WebGPU
                                 └─(HOS_NVN_NATIVE=0) 真实 lifted NVN 驱动 → NvOsDrvIoctl → nvdrv.c
                                          → gpu.c 解 Maxwell pushbuffer(B197/B1C0/A140/B0B5/MME)
                                          → rx_3d_method → 同一套 rx_* → WebGPU
 [nn_vi] 窗口缓冲队列 + 60 Hz vsync + 帧步长钉定 → rx_present(guest 地址)
 [渲染器] 批次化编码 → wgpuQueueSubmit → present pass(FSR1/插帧) → wgpuSurfacePresent
 [着色器] Maxwell 二进制 → suyu 前端/IR/16 个 pass → **WGSL 文本** → naga → SPIR-V → 驱动
```

---

## 3. 模块职责矩阵

| 路径 | 行数 | 职责 | 关键事实 |
|---|---|---|---|
| `build_game.py` / `build.bat` | 138 / 14 | 用户入口：校验游戏文件与工具链 → lift → native → 打包 | 拒绝覆盖非空 `dist/`；先编 `a64emit.dll` 免得白等 lift |
| `a64-kit/build.py` | 793 | 构建编排：lift/native/web/stubs/run 五个子命令 | 大量环境变量实验开关，每个都附实测数字 |
| `a64-kit/tools/{nso,nx,survey,toolchain}.py` | 944 | NSO/NSP 解析、指令普查、工具链定位 | lld 需复制成 `lld-link.exe` 才会选 COFF 驱动 |
| `a64-kit/hos/nx_image.py` | 204 | **构建期 rtld**：定址/重定位/符号绑定/entry 集合 | 故意不收 ADR 目标（3,735 个会把函数切两半） |
| `a64-kit/lift/a64_lift.py` | 1359 | 函数发现、控制流恢复、跳转表符号执行、C 发射、六道文本后处理 | 本仓库最精巧的算法代码 |
| `a64-kit/lift/a64emit.*`, `build_emit.py` | ~200 | suyu `arm64_to_c.h` 作为 DLL，经 ctypes 批量翻译单条指令 | 只翻"不结束基本块"的指令 |
| `a64-kit/rt/a64.h`, `a64_rt.c` | 526 | 客户机模型：`GuestContext`、访存原语、间接调用缓存、独占监视、svc/trap 弱符号 | 承重契约，见 §5 |
| `a64-kit/hos/*.c` (32 个) | 14,691 | Horizon 服务 HLE：内存/线程/svc/fs/audio/vi/hid/applet/codec/movie/os_sync/mods/剖析 | **没有 IPC**；服务在链接期被替换 |
| `a64-kit/hos/{nvn_native,nvn_layout,nvn_trace,gpu,nvdrv}.c` | 4,589 | NVN 两条路径 + Tegra GM20B 命令处理器 + 内核驱动面 + 纹理布局 | 默认路径 = nvn_native |
| `a64-kit/gpu/render/rx_*` (24 文件) | 24,259 | WebGPU 渲染器：设备/批次/管线/纹理/缓冲/present/FSR/插帧/管线缓存/debug | 两个状态前端 + 一套下层 |
| `a64-kit/gpu/third_party/suyu/` | 246 文件 48.4k | Maxwell 着色器重编译器（前端/IR/pass/**WGSL 后端**）+ video_core 子集 | GPL；GLSL 后端只给测试用 |
| `a64-kit/gpu/compat/` | 少量 | boost/fmt/ankerl 桩头文件 | 让 suyu 代码不依赖真 boost |
| `a64-kit/gpu/tools/` | 4 个 | shader_test / wgsl_check / bcn_test / feedback_test | 手工编译，未接入任何脚本 |
| `a64-kit/hos/web/` | ~2.5k | 浏览器移植：Win32 on pthreads、HTML 壳、OffscreenCanvas、WebCodecs 替 MediaFoundation | `build.py web` 目标；未验证 |
| `ports/zelda-totk/` | 2 文件 | `hot_funcs.txt`（400 个热函数，进 lift 输入）+ `link_order.txt`（4,694 个函数，链接期排序） | 都是 profile 产物，生成脚本**不在仓库** |
| `third_party/` | wgpu-native / SDL2 / opus / zstd | 运行时依赖 + 源码 | 另见 `third_party/wgpu-native/totk-patches/`（补丁 wgpu-core/hal） |

---

## 4. 逐层解剖

### 4.1 Lift：ARM64 → C

- **函数模型**：每个 guest 函数是 `void f_XXXXXXXX(Ctx *restrict c)`；"函数" = 一个 entry 加不跨越其它 entry 可达的代码；跨 entry 的边变 `TAILCALL`（`musttail`）。任意地址都可作 entry——正确性 by construction，多收只花体积。
- **entry 来源**：模块代码起点、`DT_INIT/FINI`、全部 `.eh_frame_hdr` 起点、导出函数、`init_array`、`RELATIVE/ABS64` 指向代码的、全部 `BL` 目标。**不收** `ADR/ADRP+ADD` 目标。
- **跳转表**：在 `br` 前 32 条指令窗口里做符号执行（ADRP/ADD/LDR/AND 传播 + `CMP/CCMP` 推上界），三层兜底（hoisted constant → 全函数唯一常数 → 沿 CFG 反向 reaching-constants，含 `STR/LDR [sp,#off]` 槽位）。失败就退化成 `A64_IJUMP`（仍正确，只是慢）。
- **PLT/GOT**：识别 `adrp/ldr/add/br` 四指令桩，读**已重定位**的 GOT 槽 → 构建期消解。
- **性能后处理**（都是"每指令生效"的优化，且都带实测数字）：FPX 守卫外提成 helper 调用、2/4-lane SSE 向量化、整 Q 寄存器 store 合并、无用 NZCV store 删除、**SP/帧指针基址的 store 改用不标脏页的变体**（占 TOTK store 1/4）、可选 pack_flags（实测更慢，默认关）。
- **规模**（文档值，无法在本仓库核对）：281,751 函数、12.6M 指令、44+ 文件、1.1 GB C、约 60 s 提升；868,649 个不同指令字里 114 个不可译（数据或 A57 不走的 LSE 路径）；27,126 个 `br` 仍间接。

### 4.2 客户机模型（`rt/a64.h`）——整套方案的承重墙

```c
typedef struct GuestContext {
    uint64_t x[32];          // x[31] == SP（suyu 的写法）
    uint64_t pc; uint8_t n,z,c,v; int halted;
    uint64_t vreg[32][2];    // Q0..Q31
    uint64_t tpidr_el0, tpidrro_el0, fpcr, fpsr;
    uint64_t excl_addr, excl_lo, excl_hi; uint32_t excl_size; int excl_valid;
    void *thread;
} GuestContext;  typedef GuestContext Ctx;
```

- 访存：`A64_PTR(a) = A64_MEM + (uint32_t)a`，`A64_MEM` 原生固定 `0x100000000000`、wasm 下为 0。**没有页表、没有软 TLB**——guest 访存就是宿主访存。
- 调用：`TAILCALL(f) = musttail return f(c)`；`BLR/BR` 走**每调用点 2 槽内联缓存**（低 32 位存"函数指针相对 `&a64_lookup` 的偏移"，高 32 位存目标地址），miss 才二分查找。
- 独占访问：LDXR 记 (地址,大小,值)，STXR compare-and-swap —— 对真实宿主线程也安全。
- 脏页跟踪：`-DA64_WRITE_TRACK` 让 guest store 自己标记 4 KB 页（`a64_dirty[1<<20]`），替代 Windows `GetWriteWatch`（注释记录：整区扫描 20-50 ms → 0.08 ms，消除大天空岛每几秒 55 ms 卡顿）。

### 4.3 Horizon 层：HLE 在**链接期**，不是 IPC

最容易被误解的一点：**这一层没有 IPC 序列化**（`grep CMIF|parcel|hipc hos/*.c` 零命中）。HLE 是构建期把符号绑定换掉；`hos_hostcall.c` 也不是 IPC，而是"**给 guest 一个可调用的宿主函数地址**"的表（`0x06000000 + 16*i`，8192 槽），给 NVN proc、ANativeWindow 六个虚函数、movie vtable 用。

| 子系统 | 真实度 |
|---|---|
| 内存（`hos_mem.c`）| 真：系统区 bump 分配 + 地址区间自顶向下 + 页"曾用过"位图决定是否清零；对 guest 报 3285 MB |
| 线程（`hos_thread.c`）| 真但 1:1：一个 guest 线程 = 一个宿主线程（64 MB 栈），**无时间片模拟**；Horizon 语义只补在用户态互斥量仲裁与优先级交授 |
| svc（`hos_svc.c`）| switch，25 个；未知 svc/`svcBreak` → `hos_fatal` → `ExitProcess(3)`；`GetInfo` 的 25 个 id 全是合成常量（含硬编码 ProgramId） |
| fs（`nn_fs.c`）| 真：`rom:→romfs/`（只读，可被 mods 覆盖）、`save:/cache:/temp:→<user>/`；偏移式并发读；Switch 目录项 0x310 布局 |
| os 同步（`nn_os_sync.c`）| 真：96 个 HLE，guest 地址为键的宿主对象表；互斥量/条件变量/事件/定时器/信号量/消息队列/读写锁/MultiWait；finalize 只立墓碑，30 s 后回收 |
| audio（`nn_audio.c`+`nn_codec.c`）| 真：API 级混音图（voice→submix→final→sink）、240 samples/5 ms、线性重采样、轻量限幅、waveOut；参数**延迟到 RequestUpdateAudioRenderer 一次 apply**（避免爆音）；Opus 走 libopus |
| vi/display | 真：自实现 ANativeWindow、60 Hz vsync 线程、DWM 锁相、窗口 buffer 轮转、F11 全屏、CPU 回读降级路径 |
| hid | 真：键盘 + XInput + SDL2（运行时加载）+ 鼠标原始增量，三路 OR；映射按**物理位置**；六轴是常量姿态 |
| applet/time/oe/account | 常量回答（英文、dock、固定 UID、"Player"） |
| movie | Media Foundation 解码（因 `--skip subsdk0`，用 host vtable 顶替） |
| mods（`mods.c`）| 真：模拟器布局的 romfs 覆盖 + `.pchtxt/.ips`；**code 补丁必须编进镜像**（`ic ivau` 被忽略，无自修改代码） |

TOTK 专属 hook（`totk_*.c`）：原生重写视锥剔除（带 FP 前提检查，不满足就退回 lifted 代码）、核心数实验（结论：4 核图形崩、6 核死锁）、图标缓存"删掉逼重做"、**超宽屏 3 处 + HUD 5 处 splice**、标题菜单 credit/输入注入。

### 4.4 NVN：两条路径，**默认是"API 级接管"**

| | 路径 A（默认 `HOS_NVN_NATIVE=1`） | 路径 B（`=0`） |
|---|---|---|
| 谁实现 NVN | `nvn_native.c` 里 **368 个** `n_*` 函数 | 真实 lifted NVN 驱动（SDK 代码） |
| 如何到达 GPU | 自研记录（`nvn_op_hdr{op,size}` + 44~46 个 `NOP_*`）→ 提交时 `resolve_deferred` → `rx_nvn_submit` → 「NVN replay」线程 → `rx_*` | `NvOsDrvIoctl` → `nvdrv.c` → `gpu.c` 解 Maxwell pushbuffer（B197/B1C0/A140/B0B5 + MME 宏 + CB_DATA 直写 + DMA 拷贝）→ `rx_3d_method` |
| 切换 | 只有一个环境变量；两者共用 `rx_*` 下层与 nvdrv 的 GPU 地址映射 | |
| 未实现调用 | `die()` → 停游戏并报函数名（**故意 fail-fast**，"no silent wrong result"） | 未识别的 3D 方法只跳过，不崩 |
| 收益 | 注释记录 castle **70.6 vs 60.4 fps**，2026-10-20 起为 PC 默认 | 参照实现，移植新游戏/对照调试用 |

要点：
- 真正被 HLE 的 NVN 符号只有 `nvnBootstrapLoader` 一个；其余 368 个是本项目**运行时按名字发出的函数指针**（host call 槽），游戏 `blr` 过去。
- 所有 NVN 对象**就地**放在游戏给的 NVN 对象内存里；命令句柄是"host 内存里一整块拷贝的指针"，按控制内存页批量作废、并延迟到"重放已越过"才归还。
- **block linear 解码是惰性、按绑定触发**的：客机内存永远保持 Tegra 布局；≥256 KiB 且非压缩的走 compute shader unswizzle（TOTK 3686×3686 的 BC 地图 CPU 解压要 ~150 ms）。
- 布局算法 `nvn_layout.c` 从 deko3d 移植，**tileH 是截断不是向上取整**（取整会让 40 行 BC 从 H2 变 H3，padding 变成黑色 texel）。

### 4.5 渲染器：两个状态前端 + 一套下层

- **没有 BeginFrame/EndFrame**：帧边界由 submit 与 present 两个外部事件定义，`frame_number` 只在 present 里自增。
- 前端 A 读 suyu 的 `Maxwell3D::Regs`（寄存器镜像）；前端 B 读 `NvnState`（NVN 单队列状态）。**两者不共享状态结构**，各有 `compare_op/vertex_format/stage_resources/texture_for_handle/draw` 一套（画函数逐行同构）。共享：Stream/buffer cache/bind group 缓存/pipeline 缓存/render pass/延迟录制/回写/present。
- 资源：guest 内存切成 16 MB 段、8 MB 步长，保证 ≤8 MB 区间零拷贝直接绑定；页级脏跟踪 + 影子副本支持"CPU 写与 GPU 写按字合并"。
- 管线：键 = 程序文本哈希（会话间稳定，可落盘）→ 目标格式/blend → 顶点输入 → 拓扑 → 两阶段布局哈希。编译 2 个后台线程（6 个曾在空岛数秒内全部死锁）；未编好的 draw **跳过**而不是卡住——这就是"首次遇到新场景掉帧"的来源。
- 管线缓存：`<exe>/rxcache/pipelines.bin`（`RXPN` v1 + 结构体 sizeof 签名），记录 `'T'`(WGSL 文本)/`'P'`(管线)/`'C'`(compute)/`'U'`(会话使用记录)；只预热"最近 4 个会话用过"的（每条约 390 KB，3783 条曾达 1.2 GB）。
- 呈现：`present_rect`（保持比例的黑边）→ FSR1（EASU 进中间纹理 + RCAS 到窗口）/双线性 → present mode 优先 Mailbox/Immediate（游戏自己在 vi 队列配速，呈现不该等 vblank）。
- 插帧（F7，默认关）：图像法光流（1/4 亮度 → 16×16 块匹配 ±32 px → 3×3 中值），显示线程按最近帧间隔均匀摊开；>100 ms 的帧（读盘）不插值；显示滞后约一帧。
- 热键：捕获在 `hos/display.c:199-211`（F11 全屏；F6–F10 转 `rx_present_hotkey`），语义在 `rx_present_fx.cpp:413-438`，持久化到 `<user>/present.txt`。

### 4.6 着色器：Maxwell → IR → WGSL

```
rx_shader.cpp: 取 NVN shader 包/寄存器头 → Environment（三套：经典 Regs / QMD compute / NVN 包）
  → Maxwell::TranslateProgram → Decode(maxwell.inc 280 opcode / 292 处理器)
  → CFG 重建 → ASL 结构化 → IR → 16 个 pass → EmitWGSL（返回整段文本）
  → 按文本 hash intern → wgpuDeviceCreateShaderModule
```
- pass 顺序：LowerFp64/Fp16/Int64 → ConditionalBarrier → SsaRewrite → ConstantPropagation（用真实 cbuf 值折叠）→ PositionPass → GlobalMemoryToStorageBuffer → TexturePass → Rescaling → DCE → Verify(debug) → CollectShaderInfo → Layer → VendorWorkaround → 插值/NVN ssbo 补充。
- WGSL 契约：每 stage 一个 bind group（vs=0/fs=1/cs=0）；binding 0 是动态 offset 的 uniform 块（`[rescale 4×vec4][每纹理 32B swizzle/border 槽][装得下的 cbuf]`），其余 cbuf 走 storage、纹理各占 tex+samp 两个 binding、末尾是顶点拉取。
- **driver constant bank 假设**（能否跑 shader 的关键）：c0 = UBO 表、c1 = 程序自带数据段（越界读 0）、c2 = 纹理/图像/采样器描述符字。
- 覆盖度真相：**68 条指令是桩**（60 条抛异常，SAM/RAM 是静默空操作且日志宏默认被编译掉）；warp/subgroup 全是退化实现；几何/细分在 WebGPU 直接判死。断言被改成"打印前 3 次后继续"——这正解释了 README 里"启动时三条 shader-decoder 断言"：**它们是静默的错误 shader，不是崩溃**。

### 4.7 Web 移植

Emscripten + `--use-port=emdawnwebgpu` + pthreads + JSPI：guest 地址 = wasm 地址（`A64_MEM_ZERO`），宿主数据链在 guest 窗口之上（`-sGLOBAL_BASE=3456MB`），`-sINITIAL_MEMORY=4032MB` 不增长、dlmalloc。`hos/web/` 用 pthreads 复刻 Win32 API、用 HTTP 取 RomFS（brotli）、用 WebCodecs 替 Media Foundation、用 OffscreenCanvas 交接呈现。`RX_WEB_MT=1` 把所有 WebGPU 调用集中到一个线程，其宏表生成脚本 `scripts/web_mt_macros.py` **不在仓库**。状态：可构建，未验证（README 明说 root 构建不打包浏览器版）。

---

## 5. 承重不变量（改这里必须同时改多处）

1. **所有 guest 地址 < 4 GB**，且 `A64_MEM` 是编译期常量。
   支撑：一次寻址 `A64_MEM+(uint32_t)a`；`a64_lookup` 直接拒 `addr>>32`；每调用点缓存把地址打包进 64 位的高 32 位；wasm32 上"guest 地址 == wasm 地址"；渲染器把 guest 内存当一段连续窗口。地址由 loader/HLE 自己发（`MAIN_BASE 0x08000000` / `TRAMP 0x07000000` / 系统区 `0x01000000–0x07000000` / heap `0x10000000` / 地址区自 `0xFFFF0000` 向下）。
2. **`Ctx` 布局被三方共享**：lifted C、`rt/`、`hos/`、以及 suyu 生成的 helper 都按字段偏移访问。
3. **host call 槽契约**：`0x06000000+16*i`，8192 槽全局共享（NVN+vi+audio+fs+…）。快路径把 `HostFn(Ctx*,void*)` 强转成 `A64Fn(Ctx*)` 调用——**handler 不得读第二参数**（web 上因类型校验而禁用快路径）。
4. **写跟踪是"GPU 能看到什么"的唯一事实来源**：native 默认 `A64_WRITE_TRACK` 软标记；host 侧写必须走 `HOS_WRITTEN`；**SP/帧指针基址的 store 不标脏页**——前提是"GPU 永不读 guest 栈"，若把栈上临时缓冲直接交 NVN 上传就会读到陈旧数据。
   （渲染侧核对：suyu 的 CRS/临时寄存器在 WGSL 里是**私有数组**，不落 guest 内存（`emit_wgsl.cpp:535`），所以暴露面只限于"把栈上缓冲当纹理/缓冲直接交给 GPU"这种写法。）
5. **present 是 mailbox 语义**：`nw_queue` 在上一帧的 vsync 前就释放槽位，依赖"渲染器自己有独立副本"。
6. **fence = 重放线程已解析到的位置**，不是 GPU 跑完（`RX_NVN_GPU_FENCES=1` 才等 GPU）。
7. **入口 hook 是具体 guest 地址、具体锚点文本**：SPLICE 锚点找不到会 build 失败（好），ENTRY_HOOK 换版本会**静默 hook 到别的函数**（坏）。

---

## 6. 关键设计决策（含作者在注释里给出的理由与实测）

| 决策 | 替代方案 | 理由 |
|---|---|---|
| 构建期全量翻译成 C | 运行期 JIT / 解释器 | 目标机器上无翻译开销；可被普通编译器优化；静态重编译是这类"单游戏移植"的主流路线 |
| 扁平 4 GB guest 窗口 | 页表 + 软 TLB | 访存退化成一次 32 位加法，lifted 代码里省掉基址寄存器；代价是硬上限 4 GB |
| HLE 抬到 `nn::` 边界 | 复刻 IPC 服务端 | 省掉服务端/句柄表/超时/序列化；代价是权限、错误码、异步完成语义都要手工伪造 |
| 未实现即日志桩 | 链接失败 | 第一天能跑，日志驱动补实现 |
| 未知 NVN 调用直接 `die` | 静默返回 0 | "no silent wrong result"——宁可停也不要错画面 |
| 默认 API 级 NVN + 记录重放 | 只做 Maxwell 命令模拟 | 注释实测 70.6 vs 60.4 fps；且能绕开真驱动里大量无用状态机 |
| 两条状态前端各自实现 | 统一成一套中间表示 | 两套 API 的状态字段差异太大；共享下层就够（代价：两份逐行同构代码要同步维护） |
| 未编好的 pipeline 跳过 draw | 就地同步编译 | 否则首帧卡死；代价是"新场景掉帧"而不是"新场景等一会儿" |
| 管线缓存只预热"最近 4 会话用过"的 | 全量预热 | 3783 条 = 1.2 GB 工作集；每条 390 KB |
| `-O3` + 关闭 LoopRotate + capture-tracking=5000 + `A64_NOESC` | 默认 `-O1`/默认优化 | 依次换来：雨天 58.8→60、城堡 66.7→69.7 fps；编译 306→197 s；context 载入 535→280 |
| 软写监视替代 `GetWriteWatch` | 系统写监视 | 20-50 ms 扫描 → 0.08 ms，消除每几秒 55 ms 的卡顿 |
| 链接期 `/ORDER:@link_order.txt` | 编译器顺序 | 热函数共页/共 cache line，游戏线程 −1.4% CPU/帧 |

---

## 7. 构建与运行

**要求**（README 与脚本双重要求）：Windows 10/11 x64 + 支持 wgpu-native 的 GPU；额外 ≥45 GB 空间；Python ≥3.10 + `lz4`；VS2022 Build Tools + Windows SDK；clang/clang++/lld（emsdk 或 PATH，`EMSDK` 默认 `C:\emsdk`）。

```
game/exefs/{main,main.npdm,rtld,sdk,...}   game/romfs/{ActorSystem,Event,...}   # 路径不能多一层 exefs/
build.bat            → 校验 → lift(可复用) → native → dist/SkyboundTOTK/
dist/SkyboundTOTK/SkyboundTOTK.exe           # 与 image.bin/image.json/romfs/wgpu_native.dll/SDL2.dll 同目录，可整体搬走
```

- 复用规则：`build/totk/lift-input.sha256` 与当前输入一致才复用；否则**必须人工挪走 `build/`**（没有增量重 lift）。
- 重新打包：`dist/SkyboundTOTK/` 非空会拒绝；要保留存档先把 `user/` 拷出来。
- 运行时目录：`user/`（存档、`present.txt`、`mouse_sens.txt`、`ultrawide_ui/`）、`rxcache/`（管线缓存），都在 exe 旁边。
- 常用开关（共 267 个，以下是最有诊断价值的）：
  - 图形：`RX_BACKEND=vulkan|dx12|gl`、`RX_VALIDATION=1`、`RX_OFF=1`（不渲染）、`RX_FSR=0`、`RX_ASPECT=stretch`、`RX_FRAMEGEN=1`、`RX_ULTRAWIDE=1`、`RX_STATS=1`（每 5 s 打印 fps/draw/各阶段耗时）、`RX_DUMP_FRAME=`/`RX_SCREENSHOT=`。
  - NVN：`HOS_NVN_NATIVE=0/1`（切回 Maxwell 模拟路径）、`RX_NVN_SYNC=1`（同步重放，最强的正确性对照）、`RX_NVN_GPU_FENCES=1`、`HOS_NVN_LOG_OBJECTS`。
  - 系统：`HOS_TRACE=1`（每个 HLE 调用打参数）、`HOS_HEADLESS=1`、`HOS_MEMORY_MB`、`HOS_CORES`、`HOS_LARGE_PAGES=1`、`HOS_PROFILE=<s>`（采样剖析）、`HOS_AB`（A/B 帧时间）。
  - 输入脚本：`HOS_INPUT_SCRIPT` / `HOS_INPUT_LIVE`（可发 `!dump`/`!shots`/`!rdoc`）。

### 7.1 CI（本仓库新增：`.github/workflows/`）

| workflow | runner | 需要游戏数据 | 作用 |
|---|---|---|---|
| [ci.yml](.github/workflows/ci.yml) | `windows-2022`（托管，免费） | 否 | Python byte-compile、`a64emit.dll` 构建 + 指令翻译冒烟（[emit_smoke.py](a64-kit/tests/emit_smoke.py)）、渲染器+suyu 约 74k 行 C++ 的 `clang++ -fsyntax-only`（源码清单直接取自 `build.py:gpu_sources()`） |
| [build.yml](.github/workflows/build.yml) | self-hosted（或付费大规格 runner） | **是**（放 runner 上） | 指纹判定的 lift 复用 → `build.py native` → artifact（exe/dll/image.bin/image.json + 跳转表覆盖度诊断） |

- 为什么不在托管 runner 上做完整构建：官方规格只有 **14 GB 盘**，而构建要 45 GB + 游戏本体；游戏数据不能入库（secret 上限 48 KB）；单 job 6 小时且产物含游戏派生数据不能公开。详见 [.github/ACTIONS.md](.github/ACTIONS.md)。
- 与本地 `build_game.py` 的差异：CI **不打包 `dist/`**（省 16 GB 与数分钟），只出 exe + image.bin；lift 复用的清理是**自动**的（按 `lift-input.sha256` 指纹），不必手动挪 `build/`。
- 仍缺：任何**游戏内**回归测试与指令一致性测试（`tests/insn_test.py` 不在仓库），CI 只能保证"编得过"。

---

## 8. 当前状态与已知缺陷的归因

**能跑**：构建 → 启动 → 加载 RomFS → 标题 → 开放世界（注释里遍布 2026-10-15…10-28 的实测帧率与 A/B 结论，最近两个 commit 是真实缺陷修复）。

README 的已知问题，按代码证据的归因（均为【推测】，但都给了验证开关）：

| 症状 | 最可能的机制 | 验证 |
|---|---|---|
| 阴影/着色器问题；启动时三条 shader-decoder 断言 | 断言被改成"打印 3 次后继续"，断言来自 suyu 前端 = **静默的错误 shader**；阴影侧另有一处已修（`552fa10` depth-compare clamp） | `RX_DEBUG_SHADOWS_LIT`（把所有比较采样包成恒 1）、`RX_DUMP_WGSL` |
| 地形随机拉伸 | 异步重放落后 1 帧，**顶点/索引在重放时刻才从活内存读**（`rx_draw.cpp:1930` 的 `guest_buffer`、`:1945` 的 `gpu_read`），只有命令记录被拷贝；fence 在"重放走过"而非 GPU 完成时放行（`rx_nvn.cpp:1275-1295`）→ 存在"读到的不是提交时刻内容"的窗口 | `RX_NVN_SYNC=1` 对照是否消失 |
| 地形贴图错 | `nvn_layout.c` 的 tileH 截断/打包高度判定单点错 → padding 变黑 texel；或"由 render target 拼装纹理"用到旧子资源 | `TOTK_MINIMAL_LAYOUT=0` 负对照；`RX_DUMP_TEX` |
| 武器/盾牌轮盘背景黑（应为模糊） | 需要 bloom/mip 金字塔被正确拼装；条件渲染 `RENDER_COND` 在**重放时刻**读计数器，作者已留 `RX_NVN_COND_RECHECK` 复查钩 | `RX_NVN_COND_RECHECK`；`HOS_WHEEL_STEP30=0` |
| F6 超宽屏"被拉伸而不是加宽" | 设计是"相机 aspect 取窗口比例（2 处 splice）+ 更新 16:9 常量（原值校验 `0x3FE38E3B` 不符则直接 return）+ present 非等比拉满（`rx_present_fx.cpp:342`）+ HUD 5 处补偿"——这套组合**只有全部生效才几何正确**，任一环未命中（常量校验失败、非 gameplay 相机未被覆盖、HUD 补偿比例不符）观感就是"被拉伸"。注意 `totk_ultrawide.c` 文件头仍写着 "the HUD is stretched with it for now"，与下方 `ui_scale` 的"保持比例"注释**互相矛盾**，说明这块仍在演进【推测】 | 启动日志里 `rx: present: …` 一行（`rx_present_fx.cpp:369`）看矩形与是否 FSR；`RX_ASPECT=stretch` 做对照 |
| 融合武器图标黑 | 纹理解码缓存 + `texture_for_handle` memo 的时序；作者另有"删掉空图标缓存逼游戏重做"的 workaround | `trim_decoded_textures` 预算、`RX_TIC_LOG` |
| Zonai 便携锅粒子、Depths gloom | 与条件渲染/clear 时机、附件反馈快照同族（`gpu.c:613-621` 记录过同构 bug："同一 GPFIFO entry 内复用的 target 跳过 clear"） | 同上 |
| 首次进入/新场景卡顿 | by design：pipeline/翻译未就绪就跳过 draw，后台编译完再画；管线缓存只预热最近 4 会话用过的 | `RX_NO_PIPELINE_CACHE=1` A/B |

---

## 9. 风险与脆弱点（按"改动时的杀伤力"排序）

1. **版本耦合面极大**：`EXPECTED_IDS`（build ID）、ENTRY_HOOK/SPLICE 的**具体地址与锚点文本**、`hot_funcs.txt`/`link_order.txt` 的**具体 `f_` 名**、甚至 `thread_hooks` 依赖的三条具体指令字（`mrs x1,tpidrro_el0; add #0x1f8; str`）。换版本 = 大面积硬失败（好）或静默 hook 错函数（坏）。
2. **WGSL 绑定布局双写**：`wgsl_emit_context.cpp:158` 与 `rx_draw.cpp:660` 各写一遍 lane 布局，**无断言**——改动一处就会全场景错色/错纹理，且很难定位。
3. **直接写游戏内存里的语义变量**：`nn_vi.c` 的 `pin_totk_interval` 用 `SPLICE_AFTER` 直接写 TOTK 的帧步长变量（`0x087a83e4`/`0x088f689c`），偏移错 → 静默改变游戏速度/物理步长。
4. **SP 基址 store 不标脏页**（占 1/4 store）：前提"GPU 永不读 guest 栈"一旦被打破 → 极难定位的陈旧数据。
5. **图形阶段的 shared memory 没有承载物**：`GuestEnvironment::SharedMemorySize()` 恒返回 0、`WorkgroupSize()` 恒 `{1,1,1}`（`rx_shader.cpp:96-104`）——图形阶段的 shared memory 与 barrier 没有对应实现（compute 走另一套 Environment）。凡是依赖 shared memory 的图形着色器都会静默错，且不会报错。
6. **present mailbox**：提前释放槽位依赖渲染器独立拷贝；改成直接扫描输出就会读到正在写的缓冲。
7. **内核仲裁的竞态史**：`hos_svc.c` 的 lock 字语义（owner handle + bit30 有等待者）注释里记录了两类真实事故；任何改动都要重新论证。
8. **对象表生命周期**：`nn_os_sync` 的墓碑/spare 复用、`rx_nvn` 的"不可变拷贝表只增不减 + dead 队列待重放进度"；长时间会话会留下更多拷贝（作者自认）。
9. **静默空实现清单**：NVN 的 AlphaRef/SampleMask/LineWidth/DepthBounds/高级 blend（后者会 `die`）、纹理模板平面（`die`）、`QueueGetError` 恒 NO_ERROR、nvdrv 一批 ioctl 假成功（`GET_CONFIG` 返回 NOT_FOUND、PMU_GET_GPU_LOAD=0）；suyu 的 68 条指令桩。
10. **构建资源与可重现性**：1.1 GB C、单文件 100-200 s、ThinLTO 链接约 3 GB 内存；`a64emit.dll` 本地编译、编译器版本不锁；git 历史被压缩成 6 个提交（`4315342 Upload all files`），无法审计演进。
11. **wgpu 是打过补丁的**：`third_party/wgpu-native/totk-patches/` 含 1011 行对 wgpu-core/wgpu-hal（Vulkan）的性能补丁（bind group 合并、一次 command buffer、跳过空 transition、稀疏 usage scope、共享 framebuffer、memory-init 跳过、间接校验跳过，以及 `WGPU_*` 开关与 A/B 相位）。**补丁所依赖的 `../wgpu-patched` 源码树不在仓库**，随附 DLL 的构建过程无法从本仓库复现。

---

## 10. 仓库与文档缺口（都能用 `git ls-files` 验证）

| 缺失 | 被谁引用 | 影响 |
|---|---|---|
| `ports/zelda-totk/PORT.md` | `hos.h:8`、`a64-kit/README.md:3`、`hos_main.c:3` | **主要设计文档缺位**（"How the game starts"、内存地图等都在里面） |
| `a64-kit/tests/insn_test.py` | `a64-kit/README.md:33,52` | "574 形态 / 38,494 状态与 Unicorn 零差异"这一核心正确性断言**无法核对**（本仓库只补了轻量的 [`tests/emit_smoke.py`](a64-kit/tests/emit_smoke.py)） |
| `a64-kit/scripts/{devirt.py,sample_profile.py,bench_castle.sh,web_mt_macros.py}` | `a64_rt.c`、`a64_lift.py`、`build.py`、`rx_web_mt.h` | `hot_funcs.txt`/`link_order.txt`/`A64_DEVIRT` 档案/`RX_WEB_MT` 的生成器缺失 |
| 上游的 CI / Makefile / 测试脚本 | — | 原为「零自动化验证」；**本仓库已补** [ci.yml](.github/workflows/ci.yml)（无游戏数据的编译检查）与 [build.yml](.github/workflows/build.yml)（自托管完整构建），但 `gpu/tools` 四个测试工具仍靠手工 clang++，且没有游戏内回归 |
| `docs/` | — | 上游无架构文档（设计知识几乎全在源码头注释里）；**本仓库已补根目录 [PROJECT_UNDERSTANDING.md](PROJECT_UNDERSTANDING.md) 与 `.understand/` 五份深读报告**，但它们不属于上游 |

**文档漂移**（读文档会被误导的地方）：
- `a64-kit/README.md:5` 仍写 "Status (2026-10-11): milestone 2 … nothing boots yet"，而 `hos/gpu.c:3` 已到 "milestone 5"，游戏可玩。
- `a64-kit/README.md:27` 写 clang `-O1`，实际 `build.py:777-780` 默认 `-O3`。
- `build.py:602` 注释说 `link_order.txt` 有 2,966 个函数，文件实际 4,694 行。
- 根 README 的 "Ctrl 蹲 / F 攻击"等键位与 `nn_hid.c:339-349` 一致，但 "Ultrawide 未解决" 的结论与代码里的设计意图（有意做非等比拉伸 + HUD 补偿）措辞不同。

---

## 11. 上手阅读路线（建议顺序，每步都给出"为什么先读它"）

1. `README.md` + `NOTICE.md`（15 min）：定位、合法性、许可边界。
2. `a64-kit/build.py`（1 h，通读）：**唯一能一次看清全部阶段与全部实验开关的文件**，注释里全是实测数字。
3. `a64-kit/rt/a64.h`（20 min）：承重契约，之后所有代码都好懂。
4. `a64-kit/hos/hos_main.c` → `hos.h`（30 min）：启动时序 + HLE 宏（`HLE/ENTRY_HOOK/SPLICE_AFTER`）。
5. `hos/nx_image.py` + `lift/a64_lift.py`（2-3 h）：为什么地址、重定位、entry、跳转表是这样的。
6. `hos/nvn_trace.c`（10 min）→ `hos/nvn_native.c`（半天）→ `gpu/render/rx_nvn.h`：默认图形路径。
7. `gpu/render/rx.h` → `rx_internal.h` → `rx_core.cpp` 的 `rx_present`/`flush_batch`：渲染器主干。
8. `gpu/render/rx_draw.cpp` 的 `nvn_draw`（B 路径）或 `draw`（A 路径）选一个读透：状态→pipeline→bind→draw。
9. `third_party/suyu/shader_recompiler/backend/wgsl/emit_wgsl_context.cpp` + `rx_draw.cpp:641-1089`：**着色器与渲染器的契约**（也是最大脆弱点）。
10. 想看画面问题归因时再去 `rx_texture.cpp`（解码缓存）与 `rx_nvn.cpp`（重放时刻）。

**动手验证的最小实验集**（不需要改代码）：
- `HOS_NVN_NATIVE=0` 与默认对比同场景 → 检验两套状态前端是否等价。
- `RX_NVN_SYNC=1` → 检验"异步重放 + 不快照"类问题。
- `RX_STATS=1` + `HOS_FRAME_HIST` → 拿到每阶段耗时与慢帧归因。
- `RX_NO_PIPELINE_CACHE=1` → 分离"管线缓存"与"渲染逻辑"。
- `HOS_PROFILE=10` + `HOS_PROFILE_PCS` → 重新生成 `hot_funcs`/`link_order`（顺便验证缺失脚本的产出格式）。

---

## 12. 术语表

| 术语 | 含义 |
|---|---|
| 静态重编译（static recompilation） | 构建期把目标机机器码翻成宿主 C，运行时直接跑翻译结果 |
| HLE（high-level emulation） | 用宿主实现替换系统库/服务，而不是模拟硬件 |
| NSO / ExeFS / RomFS | Switch 的可执行模块格式 / 可执行分区 / 只读资源分区 |
| npdm / META | 模块元数据（主线程栈、优先级、核号从这里读） |
| NVN | 任天堂 Switch 的图形 API（TOTK 用它提交 GPU 工作） |
| Maxwell / GM20B | Switch 的 GPU 架构（Tegra X1）；B197=3D 引擎类，B1C0=compute，B0B5=copy |
| MME | Maxwell 宏引擎：上传的小程序，可再进入 3D 方法 |
| block linear / GOB | Tegra 纹理的内存排布（64B×8 行 = 512B 一块）；渲染器要解压才能采样 |
| WGSL / naga / wgpu / wgpu-native | WebGPU 着色语言 / 其编译器 / Rust 实现 / 其 C ABI 封装 |
| FSR1 (EASU+RCAS) | AMD 的空间放大 + 锐化，本项目移植成 WGSL 两趟全屏 pass |
| 光流插帧 | 本项目自研的图像法补帧（非 AMD FG），按块匹配估计运动 |
| guest / host | 被重编译的游戏侧 / 实际运行的 Windows 侧 |
| entry / lifted function | 函数边界判定的种子 / 生成出来的 C 函数 `f_XXXXXXXX` |
| spline / splice | 本项目在生成代码里插桩的机制（`SPLICE_AFTER`） |
| mailbox present | "就绪即出、不等栅栏"的呈现语义（本项目 vi 队列的实现假设） |

---

## 附：五份深读报告（未跟踪，可删）

| 文件 | 内容 |
|---|---|
| `.understand/01-lift-build.md` | 构建五阶段、image 格式、lifting 算法、guest 模型契约、编译链接参数与全部实测依据 |
| `.understand/02-hos-runtime.md` | 启动时序、内存/线程/svc、396 个 HLE 的真实度分级、音频/显示/输入链路、15 条风险 |
| `.understand/03-nvn-bridge.md` | 两条 GPU 路径的完整调用链、NVN 对象模型与记录格式、帧循环、伪造值清单、缺陷归因 |
| `.understand/04-renderer.md` | 两个状态前端 vs 共享下层、数据结构总纲、每帧流程、格式映射大表、管线缓存格式、并发与背压 |
| `.understand/05-shader-web.md` | Maxwell→IR→WGSL 全链路、16 个 pass、binding 契约、许可证边界、Web 移植与工具链、CI 现状 |
