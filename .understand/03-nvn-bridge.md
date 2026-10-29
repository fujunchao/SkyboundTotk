# 03 · NVN / 图形桥接层（nvn_native · nvn_layout · nvn_trace · gpu · nvdrv）

> 范围：`a64-kit/hos/nvn_native.c`(2239) · `nvn_layout.c`(169) · `nvn_trace.c`(144) · `gpu.c`(1422) · `nvdrv.c`(617) ·
> `a64-kit/gpu/render/nvn_api.h`(1759) · 与 `gpu/render/rx_nvn.cpp/.h` 的接口边界（rx_nvn 内部由 T4 深读）。
> 方法：只读源码 + 只读 git；所有结论给 `路径:行号`，无法确证的标【推测】。

## 0. 一句话结论

**默认路径不是"解析 NVN 命令流"，而是"按 NVN API 粒度接管"**：整个 NVN 动态库被 `hos/nvn_native.c` 用
368 个 host 函数替换，游戏每次 NVN 调用落到一个 host call 槽（`0x06000000 + 16*i`）；只有"状态改变/GPU 工作"
被写成自研记录（`nvn_op_hdr + NOP_*`），在 `nvnQueueSubmitCommands` 时交给 `rx_nvn.cpp` 异步重放成 WebGPU 调用。
"解析 Maxwell 命令流"那条路（`hos/gpu.c` 解码 pushbuffer、B197/B1C0/A140/B0B5、MME 宏）仍然完整存在，但只在
`HOS_NVN_NATIVE=0` 时被走到；**默认 `HOS_NVN_NATIVE=1`**（`nvn_native.c:2235-2239`）。

## 1. 两条 GPU 路径（Lead 追加确认项，含行号）

| | 路径 A：原生 NVN 层（**默认**） | 路径 B：模拟 Maxwell GPU |
|---|---|---|
| 开关 | `HOS_NVN_NATIVE=1`（默认，`nvn_native.c:2235-2239`） | `HOS_NVN_NATIVE=0` |
| NVN 实现者 | `nvn_native.c` 里的 368 个 `n_*` 函数 | 真实 lifted NVN 驱动（SDK 代码，经 nvnBootstrapLoaderInternal 解析，`nvn_trace.c:141`） |
| 到达 GPU 的方式 | 自研记录 + `rx_nvn.cpp` 重放 | `NvOsDrvIoctl*` → `nvdrv.c` → `gpu.c` 解码 pushbuffer → `rx_3d_method` |
| 成熟度 | 更新、更快：`nvn_native.c:2230-2234` 记录 castle 70.6 vs 60.4 fps，2026-10-20 起 PC 默认、2026-10-23 起 web 默认 | 更老、更通用：`gpu.c:1-20` 自述为 "Step 1 of milestone 5, the command processor"，按 suyu video_core 语义实现 |

### 1.1 路径 A 调用链（函数级）

```
游戏代码 (lifted C)  bl nvnBootstrapLoader
  └─ imp_nvnBootstrapLoader            nvn_trace.c:130  (HLE, 构建期把 SDK 定义点改成钩子)
       ├─ nvn_native_on() ?           nvn_trace.c:131 / nvn_native.c:2235
       │    └─ nvn_native_proc(name)  nvn_native.c:2194  按名字 bsearch 368 项表 (1815-2188)
       │         └─ hos_hostcall()    hos_hostcall.c:22  返回 guest 地址 0x06000000+16*i
       └─ 否则: hos_call_guest(nvnBootstrapLoaderInternal) → wrap() (nvn_trace.c:112-125)
游戏: blr x8 (proc)
  └─ a64_icall                      rt/a64_rt.c:83
       ├─ a64_lookup → a64_lookup_host   rt/a64_rt.c:34-63 / hos_hostcall.c:44-55  (快路径: 直接 fn(c))
       └─ a64_call_miss                 hos_hostcall.c:63-86  (慢路径: 计数/环形缓冲/追踪)
n_nvnCommandBuffer* / n_nvnQueueSubmitCommands     nvn_native.c
  └─ rec()/REC() 写记录 → EndRecording 扁平化成一整块 host 拷贝 (1051-1125)
  └─ nvnQueueSubmitCommands (1715-1727): resolve_deferred() (1698-1714) → rx_nvn_submit()
       └─ rx_nvn.cpp:1246 → push_job → "NVN replay" 线程 (1134) → replay() (rx_nvn.cpp:654)
            └─ 每个 NOP_* 变成 rx_* 调用（rx_texture/rx_draw/rx_compute/rx_texture_gpu/…）
```

### 1.2 路径 B 调用链（函数级）

```
imp_nvnBootstrapLoader (HLE) → nvnBootstrapLoaderInternal (SDK 0x490110, build.py:276)
  → 真 NVN 驱动（lifted）→ NvOsDrvOpen("/dev/nvhost-gpu")   nvdrv.c:579
  → NvOsDrvIoctl2(fd, SUBMIT_GPFIFO, …)                     nvdrv.c:600 → ioctl() 557
       └─ ioctl_gpu nr 0x08/0x1B (438-462) → hos_gpu_queue_submit() / hos_gpu_submit()
            └─ gpu.c:1026 submit() → run_pushbuffer() 701
                 ├─ method() 572  (puller 0x00-0x3F: bind object / semaphore / syncpt)
                 └─ m3d_method() 488  (3D B197；MME 宏 0xE00，0x45/0x46/0x47/0x48；CB_DATA 0x8E4-0x8F3)
                      └─ rx_3d_method(ch->rx, r, method, arg)   gpu.c:528
  → nn_vi nw_dequeue → hos_gpu_present()  gpu.c:891 → "GPU commands"/"GPU render" 线程 → rx_present() 839
```

### 1.3 nvn_trace.c 的分发点（`nvn_trace.c:127-144`）

```c
uint64_t nvn_native_proc(const char *name);   // 127
int nvn_native_on(void);                      // 128
HLE(nvnBootstrapLoader) {                     // 130
    if (nvn_native_on()) {                    // 131  ← 唯一分支点
        RET(ARG(0) ? nvn_native_proc((const char *)GP(ARG(0))) : 0);   // 132
        return;                               // 133
    }
    ... tracing/log 包装 (135-139) ...
    uint64_t real = hos_call_guest(c, hos_entry_nvnBootstrapLoaderInternal, name, 0, 0, 0);  // 141
    RET(name ? wrap((const char *)GP(name), real) : real);   // 143
}
```

也就是说：**两条路径共享同一个入口钩子，用 `nvn_native_on()` 一句 if 决定交给谁**；路径 B 里
`nvnDeviceGetProcAddress` 返回的 proc 也被 `wrap()` 包一层（`nvn_trace.c:98-102, 124`），用于
`HOS_NVN_TRACE=1`（全部 proc 记录进 host-call 环）与 `HOS_NVN_LOG=nvnQueueSubmitCommands,...`（按名字记日志，
`nvn_trace.c:104-110`），另有 `HOS_NVN_INST_STATS`/`HOS_NVN_RT_CALLERS` 两个统计钩（120-123）。

### 1.4 覆盖度对比（Lead 第 3 问）

- `nvn_api.h` 里 `typedef … (*PFNNVN…)` 共 **482** 个（`grep -c '^typedef .*(\*PFNNVN' nvn_api.h`）——这是重建头里 NVN 的完整函数面。
- `nvn_native.c` 实现 **368** 个（`grep -c '^NV(' `），即 **~76%**；表项同样 368 条（`nvn_native.c:1816-2187`）。
- 路径 B 的"实现数"不是函数数，而是 Maxwell 方法覆盖面：3D 引擎**全量寄存器**（`reg[8][0x1000]`，`gpu.c:46`）+
  MME 宏解释器 + puller 语义 + CB_DATA/上传/DMA 拷贝引擎；未识别的 3D 方法也不会崩，只是不产生 GPU 效果
  （`gpu.c:543-560` 的 default 只处理 CB_DATA）。
- **未实现的 NVN 调用会立刻停游戏**：`nvn_native.c:2214-2226` 给未知名字挂 `unimplemented`，
  `unimplemented()` (2189-2192) → `die()` (41-48) → `hos_fatal`，日志里带函数名与 x0/x1/x2。
  设计注释写得很明确："A function TOTK has not been seen calling stops the game with its name when called
  (no silent wrong result)"（`nvn_native.c:11`）。这是**故意 fail-fast**，而不是静默返回 0。

### 1.5 两条路径是否共存（Lead 第 4 问）

**gpu.c 的命令处理器在默认模式下完全不走；但 nvdrv.c 的"映射表/写跟踪"仍然被默认路径使用。**

- gpu.c 只在 `hos_gpu_submit`/`hos_gpu_queue_submit` 里被调用，而这两个只有 `nvdrv.c:451/453` 调用——
  即只有真 NVN 驱动做 `SUBMIT_GPFIFO` 时（路径 B）。
- 默认路径下 GPU 地址没有独立地址空间：`hos_gpu_ptr()` 直接走 `nvn_gpu_guest()`（`nvdrv.c:198-208`），
  `hos_gpu_translate()` 同理（179-190）。这是明确的 `if (native)` 分支。
- 但默认路径**主动**把每个普通内存池登记进 nvdrv 的映射表：`nvn_native.c:239` `hos_gpu_track(o->gpu,o->size,1)`
  → `nvdrv.c:126-131` `map_add`；`Finalize` 时注销（`nvn_native.c:257`）。目的是让渲染器的写扫描拿到
  "GPU 能看到的客机内存范围"：`hos_gpu_guest_ranges()`（`nvdrv.c:140-172`）被 `rx_buffer.cpp:353,379` 消费。
- `rx_3d_methods()`/`rx_channel_new()`/`rx_compute_launch()` 只出现在 gpu.c（`gpu.c:527,667,1040,1373`），
  默认路径不产生这些调用；默认路径用的是另一套 `rx_*`（`rx_nvn.cpp`）。
- `nn_vi.c:535` 的 `hos_gpu_present(hos_nvmap_address(...))` 是路径 B 的 VI 队列；默认路径由
  `nvnWindowAcquireTexture/nvnQueuePresentTexture` 直接调 `hos_vi_native_*`（`nn_vi.c:559-604`），
  注释也写明了两者并存（`nn_vi.c:556-558`）。

**结论**：路径 A 是"API 级 HLE + 自研记录重放"，路径 B 是"真驱动 + Maxwell 命令流模拟"；两者共用
① nvnBootstrapLoader 入口钩子、② nvdrv 的 GPU VA↔guest 映射与写跟踪、③ 同一套 rx_* 渲染器核心。

## 2. 拦截机制到底是怎么"接管"的（三层）

1. **构建期符号绑定**（不是运行时 patch）：`hos/nx_image.py` 自述 "we are rtld"（1-19），把 ExeFS 各模块摆到固定
   guest 地址、解重定位、一次性绑好所有 import。`hle` 集合里的名字，若某模块**定义**了它，就在定义点挂钩
   （`nx_image.py:60-68`："the lifter emits a call of the hook there instead of the body"）；未定义符号绑到
   `0x07000000` 起的 16 字节 trampoline，lifter 把调用改写成 `imp_<mangled>`（12-13 行注释、`trampoline()` 75-82、
   `resolve()` 84-93）。钩子名为 `imp_` + 清洗后的符号名（`c_ident` 34-35）。
2. **哪些 NVN 符号真的被 HLE**：只有 `nvnBootstrapLoader`（`nvn_trace.c:130`）以及 NvOs 层的
   `NvOsDrvOpen/Close/Ioctl/Ioctl2/Ioctl3/MapSharedMem/QueryEvent`（`nvdrv.c:579-617`）。其余 368 个 NVN 函数
   **不是 import 钩子**，而是运行时被当作**函数指针**发出去的（见 3）。
3. **运行时 host call 槽**：`hos_hostcall(name, fn, user)` 按名字去重、返回 `0x06000000+16*i`（`hos_hostcall.c:22-33`），
   上限 8192 槽（12-13）。lifted 代码的 `blr` 落到该地址 → `a64_icall`（`a64_rt.c:83-93`）→
   `a64_lookup`：先查 `a64_fn_addr` 二分表，未命中再 `a64_lookup_host`（30-63）。**快路径**把无 user 数据的 host call
   直接当 `A64Fn` 返回并写进 per-site 缓存，省掉 `site_call→a64_lookup→a64_call_miss→handler` 三跳
   （`a64_rt.c:30-33` 注释：每秒数万次的 NVN 调用）。NVN proc 全部 `user=NULL`（`nvn_native.c:2212`），
   所以天然走快路径；`HOS_NVN_TRACE`/`hos_trace`/`HOS_HOSTCALL_STATS` 时强制走慢路径（`hos_hostcall.c:52-54`）。
   ⚠️ 快路径把 `HostFn(Ctx*,void*)` 强转成 `A64Fn(Ctx*)` 调用，第二个参数是寄存器残值——handler 不得读 `u_`；
   web 上因为 wasm 间接调用类型校验而禁用（`hos_hostcall.c:42-48`）。

**所以"命令流解析"在默认路径只是"解析我们自己刚写的记录"**（`rx_nvn.cpp:654` 的 `replay` 是个 46 路 opcode
switch），真正的硬件命令流解析只在路径 B（`gpu.c:701 run_pushbuffer`）。

## 3. 对象模型（`rx_nvn.h:44-137`，实现在 `nvn_native.c`）

所有对象**就地**放在游戏自己给的 NVN 对象内存里（`rx_nvn.h:44` "in place in the game's NVN object memory"），
布局按 NVN 真机尺寸裁到 `<= sizeof(NVNxxx)`（`NVN_FITS` 宏 45 行；本文件实际只在 `nvn_view` 上用了等长静态断言 83 行）。

| 对象 | 结构 | 关键点 |
|---|---|---|
| device | `nvn_dev{magic,flags,origin,depth_mode}` | `depth_mode` 决定 GL 式 z（`nvnDeviceSetDepthMode` 188-192 → `nvn_depth_minus_w()`） |
| memory pool | `nvn_pool{magic,flags,mem,size,gpu,vmap}` | 普通池 `gpu = guest 地址`（恒等映射）；**虚拟池**（sparse）分配 `0x1000000000` 起的 GPU 地址 + 64 KiB 页表（`nvn_native.c:54-128, 216-235, 277-301`） |
| buffer | `nvn_buffer{pool,off,size}` | 不拥有内存，只有 (pool,offset) |
| texture | `nvn_tex{magic,d(desc),has_layout,layout,L(layout 结果),gpu,pool,off,serial}` | `serial` 每次 Initialize 自增；游戏**按值拷贝** texture 对象，故用 serial 认身份（`rx_nvn.h:76`） |
| texture view | 16 字节位域 `nvn_view` | 与 TOTK 栈上 16 字节（main+0x823398）严格一致（静态断言 83） |
| sampler | `nvn_samp{magic,p(nvn_samp_par)}` | 参数是纯数据；descriptor pool 只登记参数（`nvnSamplerPoolRegisterSampler` 370） |
| program | `nvn_prog{magic,nstage,sd[6]{data,control},serial}` | 只存 6 个 stage 的 code/control 地址，真翻译在渲染器 |
| sync / event | `nvn_sync{magic,fenced,fence}` / `nvn_event{magic,gpu}` | fence=提交计数；event=一个 32 位客机地址 |
| descriptor pool | `nvn_dpool{pool,off,n}` | 句柄 = `texture_id | sampler_id<<20`（`nvnDeviceGetTextureHandle` 175-179，Maxwell 布局，shader 读句柄时要用） |
| command buffer | `nvn_cmd`（约 30 个字段） | **记录不写客机命令内存**，写 host 可增长段（`rx_nvn.h:107-122`） |
| window | `nvn_win{magic,ntex,interval,next,active,crop[4],tex[4],presents}` | 2-4 张窗口纹理，active 2-4（1749, 1761） |

**生命周期/延迟释放**（关键设计）：

- 记录里的 texture **不留游戏指针，留"不可变拷贝的指针"**：`tex_copy()`（`nvn_native.c:1523-1539`）用
  "对象内容（serial 置 0）的 FNV 哈希"做键去重，表只增不减（注释 1489-1493：一帧一次新 serial 会把表撑满，
  曾在 2026-10-20 报 "texture copies table full"）。
- 命令句柄 = **一整块 host 拷贝的指针**（`EndRecording` 返回 `(uint64_t)(uintptr_t)copy`，1124）。安全性来自
  "游戏只把它当不透明 64 位值"，且校验 magic（`check_handle` 1127-1130）。拷贝按"控制内存页"分桶登记，
  `AddControlMemory` 时批量作废（`release_copies` 1005-1036）；作废后进 `dead[]` 队列，等
  `rx_nvn_replayed() >= after` 才真正还给 free list（`free_dead_copies` 993-1004）——因为重放线程可能还没跑到。
- 纹理"活表"：`live[65536]` 按 GPU 地址索引当前初始化在那块内存的 texture（`nvn_native.c:453-503`），
  给渲染器解决"游戏每帧在同一地址重新 Initialize 临时 target 却不重新注册"的问题（`rx_nvn.h:197-199`）。
- 分配器：记录段与拷贝走 2 的幂 free list（`pool_get/pool_put` 884-912，单 class 上限 64 MiB，`NVN_NO_POOL=1` 关闭），
  注释给出动机：走进程堆时 `NtFreeVirtualMemory` 占录制线程 ~0.6%（863-866）。

## 4. 命令记录格式与"解析器"

```c
typedef struct { uint32_t op, size; } nvn_op_hdr;   // size 含头、8 字节对齐（rx_nvn.h:139-140）
enum { NOP_END, NOP_JUMP, NOP_BEGIN, NOP_CALL, NOP_SIGNAL_EVENT, … NOP_BIND_BUFS, NOP_COUNT_ };  // 46 项（rx_nvn.h:141-151）
```

- 记录：`rec(o,op,payload)`（1162-1168）先 `cmd_alloc`（952-962）再填头、清零负载；`REC/RECN` 宏（1169-1170）按
  定长/变长（`nvn_p_bind_vattr` 等带数组）取 payload。
- `NOP_JUMP`/`NOP_CALL` **从未被写出**（`grep` 只有 `rx_nvn.cpp:662` 跳过它们）：`CallCommands`/`CopyCommands`
  在录制时把被调记录**内联拷贝**进调用者（`inline_recording` 1133-1159），因为"被调记录随时可能被回收"。
- 提交时机是唯一"读客机内存"的窗口：`nvnQueueSubmitCommands`（1715-1727）先 `resolve_deferred`
  （1698-1714）把 `*Deferred` 绑定记录里保留的**游戏 handle 数组地址**（`nvn_p_bind_handles.src`, `rx_nvn.h:166-169`）
  读成确定值，然后 `rx_nvn_submit`。注释解释：TOTK 在 submit 前一刻才填材质表，submit 后立刻改写
  （`nvn_native.c:8-10, 1416-1418`）。
- 带宽/开销技巧：`hw_fix`（963-965）让 `GetCommandMemoryUsed` 报"真驱动会用的字节数"（如 vertex attrib 68 字节、
  indexed draw 48/44 字节），因为游戏**按这些数字给资源堆定大小**；`Begin/End` 把头尾各减 8（1059, 1099）。
- **抑制 NVN 55 的内联快路径**：`nvnCommandBufferInitialize` 把 `inline_cur = ~0`、`inline_end = 0`（918-919），
  游戏内联代码判断"`0x30` 字节放得下"永远为假，于是所有 bind/draw 都走 API 版（即 `*Deferred` 版），
  这也解释了为什么 `BindTexturesDeferred` 等才是热路径（注释 `rx_nvn.h:113-115`，静态断言锁住 +0x20/+0x28 偏移）。
- 重放侧：`replay(NvnState&, const uint8_t*)`（`rx_nvn.cpp:654`）一个 46 路 switch；CPU 可见副作用按记录顺序执行
  （事件 664-676、计数器 677-697、buffer 拷贝 698-714、uniform 更新 715-719）；渲染状态进 `NvnState`；
  `NOP_RENDER_COND`（850-865）在**重放时刻**读客机计数器决定 `render_enabled`（见风险 7.2）。

## 5. 帧循环：window → swapchain → flip

### 5.1 路径 A（默认）

```
每帧：录制若干 command buffer（多线程）→ nvnQueueSubmitCommands
   └─ rx_nvn_submit (rx_nvn.cpp:1246) → NvnJob{handles, scan} → "NVN replay" 线程 (1134) → replay()
nvnWindowAcquireTexture (nvn_native.c:1796-1804)
   ├─ 返回纹理序号 o->next++ % o->active        (1799)
   └─ hos_vi_native_acquire()  nn_vi.c:575-584   ← **真正的限帧点**（frame_limit / wait_vsync）
   └─ acquire sync 直接置 fenced=3,fence=0      (1802) → nvnSyncWait 立即 ALREADY_SIGNALED
nvnQueuePresentTexture (1805-1812)
   └─ rx_nvn_present(tex[i], crop) (rx_nvn.cpp:1301) → NvnJob{present} → 重放线程 run_present (1120)
        └─ rx_present(nvn_gpu_guest(t->gpu), w, h)  (rx_core.cpp:3942)
             └─ Image* im = image_at(guest)  (3998)  ← 直接拿"画在那块存储上的 render target"
             → present pass（FSR1 EASU/RCAS, rx_present_fx.cpp）→ wgpu surface present
   └─ hos_vi_native_presented()  nn_vi.c:585-604  ← 帧计数/统计/vsync 记账
```

- 队列深度：`RX_NVN_FRAMES_AHEAD` 默认 1（`rx_nvn.cpp:1030-1034`）——游戏最多领先重放 1 个 present。
- fence 语义：`rx_nvn_fence() = rx_nvn_pushed()`（1270，提交计数），`rx_nvn_wait(fence)` 等到
  `jobs_replayed >= fence`（1271-1296）。即"fence = 重放线程**解析**完成"，不是 GPU 真跑完；
  `RX_NVN_GPU_FENCES=1` 才等 GPU（1293-1294）。注释（1266-1269）解释了为什么够用：uniform 在 draw 时拷贝、
  上传按序入队、shader 写回自己回客机内存。
- `RX_NVN_SYNC=1`：回到"调用者线程同步重放"（1036-1039, 1249-1255）。
- 三线程协作：写扫描线程 `"NVN write scan"`（1052-1073，`GetWriteWatch` ~2 ms 从重放线程挪走，1041-1045）+
  重放线程 `"NVN replay"`（1134）+ 游戏录制线程。跨线程结构都有锁：`nvn_mu`（333）、`copy_lock`、`pin_lock`、
  `live_lock`、`vmap_lock`、`job_mu`。

### 5.2 路径 B

```
ANativeWindow (nn_vi.c:608-625, 由 vi.GetNativeWindow HLE 造出)
  → nw_dequeue → nw_queue (nn_vi.c ~500-556) → hos_gpu_present(addr,w,h,at_vsync)  gpu.c:891
       → Job{kind=1} → "GPU commands" 线程 (gpu.c:819-843) → hos_vsync_wait_until + rx_present()  (838-839)
  → SUBMIT_GPFIFO → hos_gpu_queue_submit → Job{kind=0} → submit() → run_pushbuffer → rx_3d_method
```
`HOS_GPU_SYNC=1`：提交/呈现都在调用线程（`gpu.c:810-813, 892-895`）；`HOS_GPU_SPLIT`（默认开）把解析/宏执行
拆到 `"GPU commands"` 与 `"GPU render"` 两个线程，用 2^21 条 `Rec{method|sub<<13, arg}` 环传递（`gpu.c:1138-1198, 1228-1340`）。

## 6. 纹理/内存布局：block linear 何时解压

**客机内存永远是 Tegra 的 block-linear/pitch 布局**（渲染器不解压客机内存），解压只发生在"某张纹理第一次被
绑定使用"时，解压结果进 WebGPU 纹理。

1. 布局算法在 `nvn_layout.c`（从 deko3d `dk_image.cpp` 移植，1-8 行）：一个 GOB = 64B×8 行 = 512B；
   block 高度 `tileH` 由 `height*1.5` 取整选 2 的幂（126-129：**NVN 是截断不是向上取整**，取整会让 40 行 BC
   从 H2 变 H3，"padding 变成黑色 texel"）；`pkg_block_h`（打包布局 byte0+1）优先于自动选择；
   `MINIMAL_LAYOUT`（flags&0x200）时若默认足迹 != 1-GOB 足迹则退回 tileH=tileD=0（85-94, 132-135）。
   层级偏移 `level_offset`（68-83）、层对齐 `layer_bytes`（95-106）、pitch 布局
   （`nvn_tex_is_linear` 40-42、`nvn_tex_linear_stride` 163-169；LINEAR_RENDER_TARGET 0x40 即使无 LINEAR 也是 pitch）。
   **这些数字必须与真驱动一致**，因为游戏按 `GetStorageSize/GetStorageAlignment` 自己分配并把预 tiled 数据
   拷进去（`nvn_native.c:421-431`，`nvn_layout.c:1-3`）。
2. 解压时机：`nvn_texture_resolve()`（`rx_texture.cpp:2079-2120`）——draw/present/拷贝时按
   (format, dim, sizes, levels, block, tile_width_spacing, linear, pitch, gpu VA) 组成 key，查 `decoded` 缓存；
   未命中才解码上传。缓存命中还要检查 `stale`（1 分钟内的写跟踪标记）。
3. 解压实现：小块/深格式在 CPU；`>=256 KiB` 且 2D、非压缩解码的走 **compute shader unswizzle**
   （`rx_texture.cpp:563-568`），ASTC 走 `astc_gpu_decode`（634-649，`rx_texture_gpu.cpp:1248, 1371-1428`）。
   动机：TOTK 的 3686×3686、7373×3686 BC 地图 CPU 解压 ~150 ms（`rx_texture_gpu.cpp:5-6`）。
4. 拷贝/清屏在客机内存里按同一布局做（不解压）：`rx_nvn.cpp:382-392` 的 `bl_offset`、471-486 `surface_row`
   按行推进；`Read/WriteTexels` 也走同一寻址（`rx_nvn.cpp:1330-1382`，入口 `nvn_native.c:668-699`）。
5. CPU 写怎么让渲染器知道：软写跟踪 `A64_WRITE_TRACK`（`rt/a64.h:72-83`，默认开，`build.py:506-509`）——
   客机每次 store 标脏 4 KiB 页（`rt/a64.h:67-74`，host 侧写用 `HOS_WRITTEN`/`hos_mark_written` `hos_mem.c:163-173`）；
   `rx_collect_written` 在每次提交前收集，喂给 `rx_begin_submission_written`（`rx_nvn.cpp:1089-1111`）。

## 7. gpu.c 与 nvdrv.c 的分工（含伪造值清单）

`gpu.c`（1422 行）= **Maxwell GM20B 命令处理器**：通道/子通道与引擎寄存器（`Channel` 42-63）、pushbuffer
头解码（modes 1/3/4/5，698-766）、puller（bind object/semaphore/syncpt，593-653）、3D 方法分派（488-562）、
shadow RAM（0x49，518-524）、MME 宏解释器（359-438）、CB_DATA 直写客机内存（543-560, 705-733）、
inline-to-memory 上传（249-271）、DMA 拷贝引擎（271-346）、report semaphore（465-482）、
"shader 写的命令要等"（1094-1102）。GPU 内存就是客机内存（16 行）。

`nvdrv.c`（617 行）= **内核驱动面**：fd 表与 5 类设备（34-42）、nvmap handle（62-68）、GPU VA→guest 有序映射
（70-172）、192 个 syncpoint + 事件（44-59, 238-271）、各设备 ioctl（274-555）、`NvOsDrv*` HLE（579-617）。

**伪造/降级清单（可疑项）**：

| 位置 | 行为 | 风险 |
|---|---|---|
| `nvn_native.c:169-172` | `RegisterFastClearColor*` 一律返回 0（=未注册） | 安全（游戏退回普通 clear） |
| `nvn_native.c:432,548` | ZCull 存储大小恒 0 | 安全（无 zcull） |
| `nvn_native.c:561` | `GetRawStorageClass` 恒 1 | 安全（注释：无原型，调用方只和 1 比） |
| `nvn_native.c:854` | `QueueGetError` 永远 NO_ERROR | 会掩盖 GPU 错误 |
| `nvn_native.c:855` | `QueueFlush` 空 | 默认路径无真实队列 |
| `nvn_native.c:1178-1179` | `WaitEvent/WaitSync` 空（依赖单队列顺序重放） | 若重放乱序会错 |
| `nvn_native.c:1582-1606` | LineWidth(非 1 只日志)、PointSize、DepthBounds、TiledCache、Discard*、ZCull save/restore、**AlphaRef**、SampleMask、ConservativeRaster、PatchSize、全部 Debug* | **AlphaRef/SampleMask 可能影响 alpha-test 类效果** |
| `nvn_native.c:1622-1625` | 高级 blend（`AdvancedMode` 非 0 直接 die），overlap/premult/normalized 空 | 若游戏用高级 blend 会停 |
| `nvn_native.c:1655` | `SetSampleLocations` 空 | MSAA 采样点固定 |
| `nvn_native.c:698-699` | `FlushTexels/InvalidateTexels` 空 | 与写跟踪重复，安全 |
| `nvn_native.c:270,341` | `FlushMappedRange` 空（写 watch 覆盖） | 依赖软写跟踪正确 |
| `nvn_native.c:928-933` | 命令内存回调存下但**从不调用**（记录在 host 内存） | 游戏若依赖回调分配会失配 |
| `nvn_native.c:397-399, 583-585` | 模板平面（stencil-plane）采样直接 die | 若用到会停 |
| `nvn_native.c:1487` | `SetShaderScratchMemory` 空 | 安全（真驱动才需要） |
| `nvn_native.c:1767-1768` | `WindowFinalize` 空 | 句柄/资源不回收 |
| `nvdrv.c:363-376` | `MAP_BUFFER_EX` 的 REMAP（改 kind，NVN 对压缩深度纹理用）**只检查不改变** | 注释记录过症状：不加这个 workaround 时 `nvnTextureInitialize` 失败、雨的遮蔽深度被画到 GPU 地址 0 |
| `nvdrv.c:484` | `GET_CONFIG` → `NV_CONFIG_NOT_FOUND` | 频率/配置查询失败 |
| `nvdrv.c:523-552` | ZCull 尺寸/信息、TPC mask=0x3、`PMU_GET_GPU_LOAD`=0 全硬编码；`GET_CHARACTERISTICS` 硬编码 GM20B（530-543） | 安全但假 |
| `nvdrv.c:604` | `NvOsDrvMapSharedMem` → 0 | 无共享内存 |
| `nvdrv.c:337,356,419-420,516` | 一大批 ioctl 直接 NV_OK | 假成功 |
| `nvdrv.c:514` | 同步 GPU 模式下 fence 未到 → `NV_TIMEOUT` | 设计如此 |

## 8. 设计取舍与理由（作者在注释里给出的）

1. **为什么要"API 级 HLE"而不是"模拟 GPU"**：路径 A 的注释直接给数字对比（`nvn_native.c:2230-2234`：
   castle 70.6 vs 60.4 fps）；`rx_nvn.cpp:1006-1012` 说重放从游戏帧线程挪到独立线程后，单帧从 ~14 ms 的等待消失。
2. **为什么记录而不是直通**：状态机变更（bind/draw）必须在 submit 时按序生效，且 deferred 绑定要延后读
   （`nvn_native.c:8-10`）；顺带把 NVN 55 的内联快路径"关掉"（`inline_cur=~0`）以保证语义单一。
3. **为什么手写 free list / 线程化**：注释都带量化理由（录制拷贝 ~13k/帧、`NtFreeVirtualMemory` 0.6%、
   写扫描 2 ms/提交、`GetWriteWatch` 整区 20-50 ms）。
4. **为什么 fail-fast**：未知 NVN 函数/未支持参数直接 `die`，"no silent wrong result"（11 行）。
5. **为什么保留路径 B**：它是"真驱动 + 真 GPU 语义"的参照实现（MME/宏、shadow RAM、semaphore、CB_DATA 直写），
   也是移植新游戏/对照调试的基线；两者共用 rx_* 后端，切换只需一个环境变量。

## 9. 风险与脆弱点（含 README 症状归因，标注推测）

**R1 异步重放 1 帧 + 顶点/索引数据不快照【推测，优先级最高】**
`rx_nvn_submit` 只排队（`rx_nvn.cpp:1246-1260`），默认 `FRAMES_AHEAD=1`；uniform 有专门的记录期拷贝
（`nvn_native.c:1190-1200`），但 `NOP_DRAW_IDX` 只存**索引缓冲地址**（`nvn_p_draw_idx.ibuf`），顶点/索引内存是
重放时才读的。写扫描只覆盖"提交之前"的 CPU 写（`rx_nvn.cpp:1089-1111`），提交之后到重放之前的写既不改
已录记录、也不进下一次扫描。若 TOTK 在 submit 后复用同一块动态顶点/索引内存，就可能出现
**随机几何错位/拉伸**——与 README "Stretched terrain **Reported**" 吻合（README:91）。**可验证**：
`RX_NVN_SYNC=1` 对比是否消失。

**R2 条件渲染（RENDER_COND）读计数器的时刻【推测】**
`NOP_RENDER_COND` 在重放时读客机计数器（`rx_nvn.cpp:850-865`；记录侧注释 `nvn_native.c:1301-1303` 说明 TOTK
用"CPU 写 1 → TIMESTAMP_TOP report 复位"这一套来门控 clear）。路径 B 的注释记录过几乎同构的 bug：
release 太早会让"同一 GPFIFO entry 内复用的 target（模糊的 ping-pong、复用在 1/8 target 上的云影）跳过 clear"，
"Room of Awakening 变成橄榄色而不是青色"（`gpu.c:613-621`）。默认路径同样存在"重放读到的计数器值与
真正的 clear 时机错位"的窗口，代码里甚至留了调试开关 `RX_NVN_COND_RECHECK`（`rx_nvn.cpp:627-651`）在
present 时复查并打印差异——**说明作者已知这类失配存在**。与 README "武器/盾牌轮盘背景应为模糊却变黑"
（README:93）、"Depths gloom"（README:96）可能同源。

**R3 纹理布局判定的单点错误【推测】**
`nvn_layout.c:126-135` 的注释明确说：tileH 选择错会"不改变总分配大小却让 padding 变成黑 texel"。
若 `pkg_block_h`（打包布局 byte0）或 MINIMAL_LAYOUT 分支判断错，症状正是**局部地形贴图错**
（README:92 "Incorrect terrain textures"）而不是崩溃。另有 `TOTK_MINIMAL_LAYOUT=0` 这个"负对照"开关（89-94），
说明该分支是经验性的。**可验证**：对同一场景分别开/关 `TOTK_MINIMAL_LAYOUT` 比较。

**R4 纹理解码缓存与写跟踪的假阴性【推测】**
`rx_texture.cpp:2111-2130` 用静态 key 查 `decoded`，靠 `stale` 标记（来自软写跟踪）失效。任何绕过写跟踪的写入
（例如 host 侧直接写而漏 `HOS_WRITTEN`、虚拟池、GPU 写回后未同步）都会让**旧解码继续被采样**：
表现为"某区域贴图错/图标黑"。README 的 "Fused-weapon inventory icons … Black icons … cache-retry workaround
… underlying timing issue remains unexplained"（README:97）与此类缓存/时序问题高度吻合 `【推测】`。

**R5 阴影/深度相关的桥接缝隙**
- 已修的一例有 git 证据：`552fa10` "WGSL backend: clamp the depth-compare reference to [0,1]"——
  WebGPU 只对 UNORM 深度钳制比较参考值，float 阴影级联远处会"略大于 1"被判成阴影，夜里出现随相机移动的暗带
  （commit message）。这属于 **WGSL 后端（T4/T5 范围）**，不是桥接层。
- 桥接层仍存在的缝隙：`SetDepthBounds` 只日志（`nvn_native.c:1584-1586`）、`SetDepthClamp` 有记录（1299）、
  模板平面采样直接 die（397-399）、压缩深度 REMAP 在路径 B 只检查（`nvdrv.c:363-376`）、
  `GetTimestampInNanoseconds` 用 19.2 MHz 换算（`nvn_native.c:195-199`）。
- README 说启动日志里有 "three shader-decoder assertions"（README:90）——那是 suyu 前端解码器（T4/T5），
  本层只负责把 `nvn_prog` 的 code/control 地址交出去（`set_shaders` 762-777，`ShadersExt` 的 reserved 非 0 会 die）。

**R6 host call 槽上限与快路径 ABI**
8192 槽（`hos_hostcall.c:13`）是全局共享的（NVN + vi + audio + fs + …），超限直接 `hos_fatal("out of host calls")`
（26 行）。快路径把两参数函数当一参数调用（`hos_hostcall.c:40-55`），任何新 handler 若读 `u_` 会在快路径下
拿到垃圾——这是**隐性约束**，没有编译期检查。

**R7 记录/拷贝的内存增长**
"不可变拷贝表"只增不减（`nvn_native.c:1489-1493`）；`dead[]` 队列依赖重放进度回收（993-1036）；
注释承认"控制内存被别用/解映射的拷贝未回收（参考实现有 mark，这里还没做）：长时间会话会留更多拷贝"（975-976）。

**R8 参数与上限的硬崩点（会让游戏停）**
`SetRenderTargets > 8`（1235）、bind handles > 32（1379）、deferred bufs > 16（1369）、
submit handles > 64（1719）、window textures 不在 2..4（1761）、texture handle id 越界（177）、
MipView levels/layers 越界（566-573）、`SetScissors` 只有 index 0 被重放（1277-1286，其余仅日志）。

**R9 其它**
`GetCommandMemoryUsed` 报的是估计值（`hw_fix` 963-965）——游戏按它切分资源堆，估计偏差会改变游戏自己的分配行为。
`nvnDeviceGetInteger` 未列出的属性会 `die`（164-168），属性表是硬编码 Tegra X1（149-163）。

## 10. 覆盖度小结（实现 / 桩）

| 类别 | 状态 |
|---|---|
| device / builder / 属性 | 实现（属性表硬编码），快清/调试/中途 shader cache 桩 |
| memory pool | 实现，含 sparse（virtual + MapVirtual 页表 + REMAP 只检查） |
| buffer / descriptor pool | 实现（不拥有内存） |
| texture / view / sampler | 实现；ZCull=0、stencil-plane die、raw storage class=1 |
| program | 只存 6 stage 指针；翻译在渲染器 |
| command buffer 记录 | 完整（46 opcode，覆盖 NVN 55 的 Deferred 变体） |
| 其中被忽略的状态 | LineWidth(非1)、PointSize、DepthBounds、TiledCache、Discard、SampleMask、AlphaRef、SampleLocations、高级 blend、PatchSize、Debug 组 |
| sync / event / counter | 实现（fence=重放计数；计数器在重放时读写客机内存） |
| queue | 提交/呈现/Finish 实现；Flush/GetError/WaitSync 桩 |
| 路径 B 的 GPU | Maxwell 3D/compute/i2m/DMA/puller/MME 全覆盖；2D blit 只计数 |
| nvdrv ioctl | 5 类设备的主路径实现；见 §7 伪造清单 |

## 11. 可验证的证据（只读命令）
```bash
wc -l a64-kit/hos/{nvn_native.c,nvn_layout.c,nvn_trace.c,gpu.c,nvdrv.c} a64-kit/gpu/render/{nvn_api.h,rx_nvn.cpp,rx_nvn.h}
grep -c '^NV(' a64-kit/hos/nvn_native.c                              # 368 个实现
grep -c '^typedef .*(\*PFNNVN' a64-kit/gpu/render/nvn_api.h           # 482 个 API 原型
grep -n 'HOS_NVN_NATIVE' -r a64-kit --include=*.c --include=*.cpp     # 开关与默认值
sed -n '2230,2239p' a64-kit/hos/nvn_native.c                          # 默认值 + fps 证据
sed -n '127,144p' a64-kit/hos/nvn_trace.c                             # 两条路径的分发点
grep -n 'hos_gpu_track' a64-kit/hos/*.c                               # 默认路径仍登记 nvdrv 映射
grep -rn 'rx_3d_methods()\|rx_channel_new()' a64-kit/hos a64-kit/gpu # 只在路径 B
grep -n 'HOS_HOSTCALL_BASE\|HOS_HOSTCALL_MAX' a64-kit/hos/hos_hostcall.c
git log --oneline -6 && git show 552fa10 --stat                        # 阴影 dref 修复的证据
```

## 12. 留给 T4/T5 的接口关注点

1. `replay()`（`rx_nvn.cpp:654`）里 `NOP_DRAW_IDX`/`NOP_BIND_VBUF` 的**客机内存读取时刻**是否有快照/双缓冲保护（R1）。
2. `NOP_RENDER_COND` 的判定时刻与 `RX_NVN_COND_RECHECK` 的实测结论（R2）。
3. `nvn_texture_resolve` 的 cache key 是否漏项（storage 版本、view 的 swizzle/format）、`nvn_texture_follows_storage`
   （`rx_nvn.cpp:181-185`）的触发条件（R4）。
4. `nvn_driver_bank`（`rx_nvn.cpp:250-332`）：驱动常量 bank c0/c2 的偏移是这套方案能跑 shader 的关键假设，
   建议与 shader 报告对齐核验。
