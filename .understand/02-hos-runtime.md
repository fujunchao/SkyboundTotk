# 02 · Horizon OS 运行时层（a64-kit/hos）：没有固件，怎么让游戏以为 Switch 还在

> 阅读范围：a64-kit/hos/ 下除 GPU 后端（gpu.c、nvn_*.c、nvdrv.c）以外的全部运行时源码，以及相邻的
> a64-kit/rt/a64.h、rt/a64_rt.c、a64-kit/hos/nx_image.py、a64-kit/build.py 的接口部分（只读）。
> 行号对应当前工作树。

## 0. 一句话结论

这一层**不是模拟器**，而是把“操作系统”整体换成一份 Windows 上的 C 运行时：没有 IPC、没有内核、没有固件。
HLE 发生在**链接期**——nn:: 边界的每个符号被替换成宿主函数 void imp_<mangled>(Ctx*)，svc 被换成一个 switch，
guest 内存就是一块固定的 4 GB 宿主虚拟地址区间。
统计：HLE(...) 定义 **396 个**（nn_os_sync 96、nn_audio 115、nn_applet 43、nn_hid 34、nn_fs 33、hos_thread 25、
nn_movie 16、hos_mem 8、nn_codec 8、nn_vi 6、nn_diag 4、nvdrv 7、nvn_trace 1），另有 12 个 ENTRY_HOOK_SYM
（nn_libc 6、nn_image_debug 6）与 6 个 ENTRY_HOOK、9 处 SPLICE_AFTER（见 §13）。

## 1. 层职责与三方契约

| 边界 | 契约 | 证据 |
| --- | --- | --- |
| 对 lift / rt | 一切 guest 调用走 A64Fn = void(*)(Ctx*)；HLE 函数读写 c->x[0..7]、返回 c->x[0]；Ctx 里有 vreg[][]、fpcr/fpsr、tpidr_el0/rro_el0、x[31] 当 SP | hos.h:18,30-34；a64.h:30 |
| 对 guest | guest 地址空间 < 4 GB，宿主基址固定 A64_HOST_BASE=0x100000000000；A64_PTR(a)=A64_MEM+(uint32_t)a | a64.h:60-65 |
| 对渲染器 | hos_gpu_present(addr,w,h,at_vsync)（gpu.c:891）+ rx_start/rx_present/rx_resize（gpu/render/rx.h:16-53）；hos_guest_base()/hos_guest_size() 交出整块 guest 窗口（hos_main.c:133-134） | §10 |

HOS_TRACE=1 会让每个用 HOS_TRACE_CALL 的 HLE 打印参数（hos.h:60-62）。

## 2. 启动时序：谁先初始化，nnMain 何时被调用

main()（hos_main.c:153-253）严格按 nn::init::Start（sdk+0xfa060）的顺序手工展开：

1. **参数**：[image.bin] [--romfs d] [--user d] [--trace]（:155-160）；Windows 下未给则用 exe 所在目录的
   image.bin/romfs/user（:51-61、:166-168），并检查 romfs 目录存在（:169-173）。CreateDirectoryA(user)（:182）。
2. **保留 guest 窗口**：VirtualAlloc(A64_HOST_BASE, 4 GB, MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH)（:111-125）。
   可选 HOS_LARGE_PAGES=1 走 2 MB 大页（:82-103，需 SeLockMemoryPrivilege）。
3. **读 image.bin 到 0x08000000**（:197-209），只读文件大小那么多，理由见 :202-203（读满 4 GB 会让 Windows
   提前提交约 2 GB 物理页）。尺寸与 hos_image_end 不符直接退出（:211-215）。
4. hos_mem_init(image_end)（:216）→ hos_mods_init(image)（:220，先把 exefs 数据补丁写进已加载镜像）。
5. hos_boot_main_thread(hos_main_stack_size)（:222）：**当前宿主线程**变成 guest 主线程；栈大小/优先级/核号来自
   main.npdm（build.py:255-280 生成 hos_image.c）。
6. hos_prof_start()（:225）、timeBeginPeriod(1) + 关闭电源节流（:227-229）。
7. hos_display_start()（:231，先建窗口并初始化渲染器；HOS_HEADLESS=1 只做离屏）。
8. 依序 call_guest：_init_libc0(:234) → 设 x0=主线程 handle、x1=0 → nnosInitialize(:237) →
   hos_thread_adopt_guest(:239) → _init_libc1(:240) → nninitStartup(:243) → _init_libc2(:244) →
   每模块 DT_INIT(:245-249) → **nnMain（:250）**。
   nninitInitializeSdkModule 被**故意不执行**（:241-242）：它只通过 IPC 打开 sm:/diag/oe/aoc/err/fs/pctl 客户端，
   而每个客户端都已被 HLE 顶掉。每步之前 hos_mem_checkpoint()（:144-146）在 HOS_MEM_TIMELINE=1 时打印常驻内存。

“谁先初始化”的答案：**宿主先把自己变成 guest 主线程与窗口进程，再让 guest 从 libc 到 nnMain 自己跑**；
nnMain 是最后一步，返回即进程结束（:250-252）。

## 3. Guest 内存模型

映射表（hos.h:8-12 + hos_mem.c）：

| 区间 | 用途 |
| --- | --- |
| 0x00100000-0x01000000 | 低洞（web 构建把地址区间塞这里）hos_mem.c:35 |
| 0x01000000-0x07000000 | 系统区：主栈、每线程 TLS(4 KB)+TLR(0x200)、ThreadType、窗口 buffer 描述符；hos_sys_alloc 只增不减 |
| 0x06000000-… | **host call 表**（每槽 16 B，最多 8192）hos_hostcall.c:12-13 |
| 0x07000000-… | import trampoline 槽（未定义符号）nx_image.py:30 |
| 0x08000000-image_end | main/sdk/subsdk* 镜像，各模块 1 MB 对齐（nx_image.py:29,41-46） |
| 0x10000000-… | heap：SetMemoryHeapSize 定界，AllocateMemoryBlock 2 MB 对齐向上分配 |
| image_end..heap、heap 之上 | 地址区间（AllocateAddressRegion），**自顶向下**从 HOS_SPACE_END=0xFFFF0000 分配 |

**为什么必须 < 4 GB**：A64_PTR 把地址截成 32 位再加常量基址（a64.h:65），这样基址能折进每条访存指令的寻址，
不必每次从全局重载（a64.h:53-56）。代价是 guest 指针放不进 64 位高位；A64_HOST_BASE 又特意选在 16 TB，
远离 Windows/glibc 的默认映射（a64.h:61）。

分配器要点：

- hos_sys_alloc：只 bump，超出 0x07000000 直接 hos_fatal（hos_mem.c:54-66）；分配后 memset 0 + HOS_WRITTEN。
- hos_region_alloc：首次适配、自顶向下，优先大区间，失败再落到两个低洞；维护 region_floor 供
  SetMemoryHeapSize 检测冲突（hos_mem.c:68-114、172-182）。区间记录按地址降序。
- 页表只用于“哪些页曾被用过”：pages_used 位图 1 M 位 = 128 KB（hos_mem.c:116-120）。AllocateMemoryPages 只清零
  “曾被用过”的页段（HOS_ZERO_ALL_PAGES=1 全清，:232-264）——在“新页读出来是 0”与“不要每次 memset 几百 MB”之间折中。
- 释放：FreeMemoryPages/FreeAddressRegion → discard_pages → DiscardVirtualMemory（或 MEM_RESET），大页模式跳过
  （:122-142）；HOS_KEEP_FREED_PAGES=1 关闭。
- 对 guest 报告的容量：HOS_MEMORY_MB（默认 3285，零售机数值）；QueryMemoryInfo 只填 TOTK 真正读的字段
  （+0 total、+8 used，hos_mem.c:273-285；注释给出 main+0x1131004 处 heap = total - used - 1080 MB）。

## 4. SVC 层：一个 switch，不是函数指针表

- 分发表就是 a64_svc() 里的 switch (imm)（hos_svc.c:246-357）；default 直接 hos_fatal 并打印 svc 号与 x0-x2
  （:354-356）。**没有表驱动**，也没有“未实现返回错误码”的宽容路径。
- lift 把 SVC #imm 翻成 a64_svc(c, imm)（lift/a64_lift.py:1242）；rt 的弱定义只在未被覆盖时 trap
  （rt/a64_rt.c:211-215）。因此本文件覆盖到的就是全部可用 SVC。
- 已实现 25 个：0x01 SetHeapSize / 0x02-0x03 权限与属性 / 0x06 QueryMemory / 0x07 ExitProcess / 0x0A ExitThread /
  0x0B SleepThread / 0x0C-0x0F 线程优先级与核掩码 / 0x10 GetCurrentProcessorNumber / 0x16 CloseHandle /
  0x1A-0x1D 用户态互斥量与进程级条件变量仲裁 / 0x1E GetSystemTick / 0x24 GetProcessId / 0x25 GetThreadId /
  0x26 Break / 0x27 OutputDebugString / 0x29 GetInfo / 0x2A-0x2B 数据缓存刷新。
- **trap 行为**：svcBreak → fatal（:331-335）；未知 svc → fatal（:354）；a64_on_trap（hos_thread.c:243-245）
  统一转 hos_fatal：打印线程名、lr/sp、hos_backtrace()、hos_hostcall_stats()，然后 ExitProcess(3)
  （hos_thread.c:224-240）。svcExitThread 直接 ExitThread(0)（hos_svc.c:260-262）——**绕过** TLS 退出 hook（见 §17）。
- GetInfo 的 25 个 id 全是合成常量（hos_svc.c:14-38）：核掩码 (1<<cores)-1、Alias/Heap 区地址与大小、
  TotalMemorySize、RandomEntropy（固定种子线性混洗）、ProgramId 硬编码 0x0100F2C0115B6000（:32 标注 TODO）、
  IsApplication=1。QueryMemory 按地址落在栈/代码/heap/系统区之一编造 state+perm，其余返回一页 Free（:225-240）。
- GetSystemTick = recomp_cntpct()（hos_thread.c:467），频率报 19.2 MHz（hos.h:54、hos_thread.c:662）。
- 自述“游戏本身没有 SVC、SDK 有约 60 个 wrapper”（hos_svc.c:1-4）——**源码断言**，未独立验证；
  与“svc 由 lifted 的 sdk 模块发起”一致【推测】。

## 5. 线程：1:1 映射 + 半套内核仲裁

**映射**：一个 guest 线程 = 一个宿主 CreateThread，宿主栈预留 64 MB（hos_thread.c:393）；每线程一份 Ctx、
4 KB TLS 块（TP 在块中点 +0x800）、0x200 TLR 块（:247-259）。tpidrro_el0 的 +0x1F8 放 guest ThreadType*，
ThreadType+0x1B0 放内核 handle（:34-36,45-46），使 SDK 自己的 InternalCriticalSection 读到的 tag 与 HLE 一致。
主线程的 ThreadType 由 nnosInitialize 建，随后 hos_thread_adopt_guest 认领（:41-47、hos_main.c:239）。

**调度**：没有时间片模拟——guest 线程跑在 Windows 调度器上。nn::os 的优先级（如 44）只用于：
① hos_thread_prio 让互斥量交授按“优先级值小者优先”选（hos_svc.c:62-75）；② 记录；
③ HOS_PRIO 名字匹配设置宿主优先级（hos_thread.c:448-464）。hos_thread_boost(2) 只给 vsync/音频线程提优先级
（nn_vi.c:96、nn_audio.c:775）。

**让出点**（“抢占”真正发生的地方）：

- nn::os::YieldThread → SwitchToThread()（:595）。
- svcSleepThread / nn::os::SleepThread → hos_sleep_ns：<1 ms 用 SwitchToThread 自旋，≥1 ms 用
  CreateWaitableTimerEx(HIGH_RESOLUTION)（:484-535；注释解释 20 µs 轮询被 timer 粒度放大 25 倍的问题）。
- 用户态互斥量/条件变量（svcArbitrateLock/Unlock、WaitProcessWideKeyAtomic、SignalProcessWideKey）：与 Horizon
  同构——锁字 = owner handle、bit30 = 有等待者；赢家直接交授所有权，输家在 WaitForSingleObject（auto-reset event）
  上睡（hos_svc.c:45-204；hos_thread.c:301-305）。带 20 µs 自适应自旋（:92-121，HOS_SPIN_US）。
- nn::os 事件/信号量/条件变量 → SRW + CONDITION_VARIABLE（nn_os_sync.c）。

**能力查询**：GetCurrentCoreNumber 返回 ideal_core % cores，无 ideal core 时取掩码最低位（hos_thread.c:91-98）；
注释说明引擎按核号区分 per-core 命令缓冲；默认核数 3（:82-90，HOS_CORES）。

## 6. “IPC”？——不存在的 IPC，与真正的 host call 表

任务书假设 hos_hostcall.c 复刻了 Switch IPC 头。**事实相反**：

1. **全层没有 IPC 序列化**。在 a64-kit/hos/*.c 里 grep CMIF|parcel|hipc|sf:: 无命中；“请求格式”问题不成立。
2. HLE 发生在**构建期**：nx_image.py 把 ExeFS 各模块布局到固定地址并完成重定位与符号绑定；hle 集合里的符号
   若某个已加载模块定义了它，就在**定义处**打 hook（imp_<sym>），lift 在那里生成对 hook 的调用而不是函数体
   （nx_image.py:60-68）。未定义又不在 hle 的符号绑到 0x07000000 起的 16 B trampoline（:75-82），lift 把它变成
   imp_<mangled> 调用（:12-13）。
3. hle 集合 = hos/ 里所有 imp_*（build.py:372-374 正则收集，check_hle 校验拼写，:217-225）+ main 中“OS 命名空间”
   的自由函数（build.py:189-191、379-381）。OS 命名空间：audio/hid/oe/prepo/nfp/time/account/vi/swkbd/pl/err/
   codec/diag/detail/settings/ns/applet/album/…；**nn::fs 不在其中**，它靠 hos/nn_fs.c 里的 imp_ 定义被 hook 在
   sdk 的定义处。未 HLE 的这些 import 得到生成桩：首次调用打日志、返回 0（build.py:174-183 → rt/a64_rt.c:220-226）。
4. 其余 SDK（libc、nn::util、nn::image、nn::fontll、nn::mem、std）**作为 lifted 代码原样运行**（build.py:186-188）。
   nn_libc.c 只把 memcpy/memmove/memset/memcmp/strlen/strcmp 换成宿主实现（ENTRY_HOOK_SYM，:18-81；
   HOS_GUEST_LIBC=1 可关），顺带 WAIT_WRITEBACKS 与 HOS_WRITTEN 通知渲染器。
5. hos_hostcall.c 真正做的事：**给 guest 一个可调用的宿主函数地址**。每槽占 0x06000000+16*i（背后无内存）；
   a64_lookup_host 在无 user 数据时把槽直接当 A64Fn 返回（:44-55），否则走 a64_call_miss（:63-86）。用途：
   NVN proc 表、ANativeWindow 的六个虚函数入口（nn_vi.c:613-626）、movie 的 vtable（nn_movie.c:140-166）。
   hos_call_guest 提供反方向（宿主里回调 guest，保留 LR，:90-97）。

**例：一次“系统调用”的实际路径**
nn::fs::OpenFile → [lift 生成的 imp__ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci(c)] → 宿主 CreateFileA
（nn_fs.c:194-219）。中间没有服务端、没有句柄表映射、没有超时。

## 7. 文件系统：挂载名 → 宿主目录

Mount 表最多 16 项：name + 宿主根 + 可写标志（nn_fs.c:21-37）。映射：

| guest 路径 | 宿主 |
| --- | --- |
| rom:/… | --romfs 目录（只读），MountRom 挂载（:107-111）；QueryMountRomCacheSize 返回 0x10000 |
| save:/… | <user>/save/<uid 高低 16 hex>（:129-141） |
| cache:/… | <user>/cache/<index>（:144-149），OpenCacheStorageList 枚举磁盘上存在的 index（:158-174） |
| temp:/… | <user>/temp（:142） |

- host_path() 拆 mount:、拼宿主路径并反斜杠化；**romfs 路径先查 mods 索引**（:44-63 调 hos_mods_romfs）。
- 句柄 = 表下标+1（4096 槽，SRWLOCK 保护，:78-103）；FileHandle/DirectoryHandle 就是这一个 64 位值。
- 读是**基于偏移**的（OVERLAPPED + ReadFile，:221-246），所以任意多 guest 线程可并发读同一文件；读完
  HOS_WRITTEN 通知渲染器（GPU 可能读这块内存）。HOS_FS_DELAY_MS 可人为放慢读（:224-234）。
- 目录项按 Switch 布局 0x310 字节写回：name[0x301]、type@0x302、size@0x308（:426-431）。
- 只读挂载上 OpenFile 请求写 → 直接 fs::ResultPathNotFound（:199-200）。
- 错误码是硬编码的 Switch 数值：0x202 path not found、0x402 exists、0x2EE202 out of range 等（:16-19,251,277）。
- 两个“越界”注入点：hos_ultrawide_redirect（HUD 布局档案换成本地重写副本，nn_fs.c:201-203）与
  hos_blank_cached_icon（把空图标当不存在并删掉，:204）。

## 8. 同步原语（nn_os_sync.c）：以 guest 地址为键的宿主对象表

- 每个 nn::os 对象在宿主侧是一个 Obj{SRWLOCK, CONDITION_VARIABLE, owner, count, …}（:17-44），用 guest 地址
  做键存在开放寻址哈希表（:48-60、205-244）；**guest 结构体本身不被改写**，未初始化也按需创建（:4-7），
  互斥量一律递归。
- 对象不真正释放：finalize 只把 kind 置 0 当墓碑，30 s 后才回收进 spare 空闲链（:70-126）；同地址再初始化原地复用
  （:220-227）。每线程还有 128 项直接映射缓存（:188-203，HOS_NO_OBJ_CACHE=1 关），注释给出 1.6%→0.6% 的收益。
- 覆盖：mutex（:343-416，CAS 抢 owner，只有有人睡时才动 SRW/CV）、condvar（:419-438）、Event/LightEvent/SystemEvent
  共用一套（:441-522）、TimerEvent（周期定时，错过周期按硬件行为跳过，:672-701）、信号量（普通/轻量，:703-731）、
  消息队列（普通/轻量 + jam/try/peek，:733-824）、读写锁（写者也可读、可重入，:827-875）、MultiWait
  （WaitAny/TimedWaitAny/TryWaitAny，holder 可挂事件/定时器/线程/信号量/队列，:877-980；事件用 poke_multiwait
  唤醒，其他 5 ms 复查）。
- 诊断钩子很多：HOS_WAIT_LOG、HOS_LONG_WAITS（>5 s 的无限等待打印调用者）、HOS_WAKE_LAT（信号→运行的延迟分布）、
  HOS_WAIT_WHO、HOS_PROFILE_GATE（与剖析器同步采样窗口）。

## 9. 音频链路

**两个独立部件**：

1. nn_codec.c：硬件 Opus 解码器 → libopus。InitializeHardwareOpusDecoder 建 OpusDecoder，句柄
   'OPUS'<<32|index（:42-68）；GetOpusPacketSize 读 8 字节大端头（payload size + final range，:93-102）；
   DecodeOpusInterleavedWithHardware 跳过 8 字节头解码成 S16 交织 PCM，回报 consumed/sampleCount（:106-122）。
2. nn_audio.c：**API 级**混音器。游戏用 Voice/SubMix/FinalMix/Splitter/DeviceSink/MemoryPool 建图，每个对象
   首 8 字节写 'AUDH'|kind|index 句柄（:103-113）。渲染线程 240 samples@48 kHz（5 ms）一帧（:27-28、773-792）：
   voices（PCM8/16/24/32/float/DSP-ADPCM，按 rate*pitch/48000 线性插值重采样，:361-434）→ 目标 mix 或 splitter
   （逐通道/逐 buffer 音量，帧内斜坡）→ 按“离 final mix 的深度”排序处理 sub mix（:453-471）→ final mix →
   device sink 的 6 通道映射（FL FR FC LFE RL RR，0.7071/0.5 系数降到立体声，:472-488）→ 轻量限幅器（:545-552）
   → waveOut（10 个 10 ms 块轮转，:566-599、621-646）。可选 HOS_AUDIO_DUMP=<wav>（自写 RIFF 头，:600-620）、
   HOS_AUDIO=0 静音。
3. **参数延迟提交**：Set*/Append*/SetVoicePlayState 只把变更排进 Cmd 队列（:664-765），RequestUpdateAudioRenderer
   一次性 apply（:810-816）；50 ms 没 update 则渲染线程自己提交（:785）。理由在 :648-660（否则会在“voice 已 start、
   音量还没设”之间渲染出爆音），且队列存**槽位下标**而非指针——游戏会立刻复用 VoiceType。HOS_AUDIO_IMMEDIATE=1
   回到即时 apply。
4. 已实现但被削的部分：I3DL2 混响有实现（:267-355，room/hf/damping/decay + effects_frame），但 aux send/return
   直通（:1220-1232）；voice 优先级、GetActualVibrationValue 等为桩。
5. 与游戏侧的时钟：渲染线程每帧 hos_system_event_signal(renderer_event)（:790）；该事件就是游戏的
   nn::os::SystemEvent（同 §8 对象表，OpenAudioRenderer 处 init，:796-806）。

## 10. 显示 / present 链路 与 F11

    nvn / NvWsi → ANativeWindow(hostcall 表) → nw_dequeue / nw_queue (nn_vi.c:477-550)
                                             → hos_gpu_present(addr,w,h,at_vsync) (gpu.c:891)
                                             → rx_present() [渲染线程] → rx_present_fx: 保持宽高比 + FSR1
    window: display.c display_main → CreateWindowA → rx_start(hwnd) (:305-322)；F11 → set_fullscreen (:46-68,201)

- nn::vi 只回两个小 guest 对象：Display、Layer（各 0x100 字节，:176-186）。
- **Native window 是自己实现的一份 ANativeWindow**（:187-205 的注释列出 0xc0 字节布局：'_wnd' magic、
  +0x70 setSwapInterval、+0x90 query、+0x98 perform、+0xa8 dequeue、+0xb0 queue…），在 GetNativeWindow 里逐字段
  填 hostcall 地址（:608-631）。理由（:200-201）：不去跑 SDK 的 Surface + vi binder，驱动只碰这几项。
- 窗口 buffer 由 NVN 自己的窗口纹理承担；Nintendo 私有 perform 0x13 把 (slot, nvgr handle, w, h, format, usage,
  stride) 注册进来，handle==0 注销（:443-468）。nw_dequeue 选最早释放的槽（front 除外）；nw_queue 算“上屏 vsync”
  = max(now+1, last_display+interval)，并把**上一帧**的槽在 at-1 释放（:528；注释说明按主机语义在 at 释放会让
  21 ms 的帧等到 33 ms）。
- **vsync**：独立线程 60 Hz（HOS_VSYNC_HZ，:56-60），每 tick 递增计数、WakeAllConditionVariable、给所有
  GetDisplayVsyncEvent 拿到的 guest SystemEvent 发信号（:104-111）。Windows 上还读 DwmGetCompositionTimingInfo
  把 tick 锁到显示刷新的整数倍（:29-49、126-148）。HOS_VI_MODE 决定 swap interval 被谁等（0 dequeue 等、
  1 queue 等、2 GPU 线程等，:165-172）。
- **帧率与步长（TOTK 专属、风险最高的一块）**：
  - TOTK 自己会按帧耗时设 swap interval（注释 :226-236）。本运行时反客为主：nw_set_swap_interval 在调用瞬间从
    guest 栈 sp+0x20/sp+0x28 认出 TOTK 的 frame-rate controller，校验 ctl == fw+0x248 与 ctl+0x14 == requested
    才写回（:336-360），把 min/max/current 钉成 1 或 2。
  - 默认 realtime（HOS_REALTIME=0 可关）：用两次 present 的间隔（最近 5 帧、1/240..1/15 截断）在
    SPLICE_AFTER(0x087a83e4)、SPLICE_AFTER(0x088f689c) 两处直接写 TOTK 的帧步长变量（:281-318），
    使任意帧率下游戏速度正确。
  - 能力轮（按住 L）期间强制 1/30 步长，否则背景模糊截图会全黑（:263-280，HOS_WHEEL_STEP30=0 关）。
  - 自动 30/60 抖动控制器（frame_done，:366-399）含退避；HOS_TARGET_INTERVAL/HOS_DYNAMIC_INTERVAL 可接管。
- **display.c**：无渲染器（RX_OFF=1 或拿不到 WebGPU 设备）时走“CPU 回读”路径：每 33 ms grab() 从 guest 内存里的
  nvgr 句柄（magic 0xdaffcaff）解 block-linear 布局、RGBA→BGRA，再 StretchDIBits（:26-28、104-134、237-274）；
  全黑时叠一行提示文字（:261-271）。标题栏每秒显示 fps/帧数/draws/pipeline 与 present 设置（:138-175）。
  热键在 wndproc：F11 全屏、F6-F10 交给 rx_present_hotkey（:199-211），F6 还会 rx_resize 让宽高比重算。
  鼠标捕获（点击进、Esc 出、ClipCursor + WM_INPUT 原始增量，:84-99、186-195）供 nn_hid 当右摇杆。
  HOS_HEADLESS=1 不建窗口（:333-336）、HOS_WINDOW_SIZE、HOS_FULLSCREEN。

## 11. 输入链路

nn::hid::GetNpadStates 的**五种 Npad 状态类型共用**一个实现（nn_hid.c:396-400），只有一个 Npad No.1，风格位
FullKey（:402）。状态结构 0x28 字节：s64 sampling; u64 buttons; s32 lx,ly,rx,ry; u32 attributes; pad（:15-16、386-393），
每次调用 sampling 递增，count 个状态按“最新在前”递减填。

三路输入 OR 在一起（read_input，:329-350）：

| 来源 | 映射 |
| --- | --- |
| 键盘（GetAsyncKeyState，仅游戏窗口在前台时计） | Space=X、E=A、Shift/Q=B、F=Y、R=R、Z=L、C=ZL、V=ZR、Ctrl=LS(蹲)、Enter=Plus、Tab=Minus、方向键=D-pad、WASD=左摇杆、IJKL=右摇杆（:339-349） |
| XInput（xinput1_4.dll，每秒重试加载） | A→B、B→A、X→Y、Y→X（**按物理位置**）、肩键 L/R、Start=Plus、Back=Minus、D-pad、扳机>64→ZL/ZR、摇杆死区 7849（:292-322） |
| SDL2（运行时 LoadLibrary，线程每 4 ms 轮询、1 s 扫热插拔、最多 8 个） | 同一套位置映射（pad_sdl.c:149-152）；扳机>8000→ZL/ZR；同样死区；合并规则=按键 OR、摇杆取最大偏转（:181-199） |
| 鼠标（窗口内点击捕获后） | 原始增量按“最近 ≥8 ms 的速度”→ 右摇杆偏转；灵敏度 -/= 调（×0.8/×1.25，存 <user>/mouse_sens.txt），HOS_MOUSE_SENS/HOS_MOUSE_INVERT_Y（nn_hid.c:63-106） |

- 输入被“节流”：game_in_front() 判断前台窗口 pid（对 overlay 窗口有特例），不在前台返回空输入（:108-132,337）；
  SDL/XInput 是全局的，不判断会串味（注释 :333-336）。
- 测试钩子：HOS_INPUT_SCRIPT（"50:A,80:LUP/4000"）与 HOS_INPUT_LIVE=<file>（运行中追加行即可操控，还能发
  !dump / !synclog / !shots <ms> / !rdoc 指令，:160-195、197-240）。脚本激活后 5 s 内忽略玩家设备。
- 六轴：GetSixAxisSensorStates 恒定返回“静止朝下”(0,0,-1) 姿态矩阵与 5 ms 采样周期（:427-442）。
- 震动：VibrationValue{amp_low,freq_low,amp_high,freq_high} 取左右两侧较大值 → hos_sdl_rumble →
  SDL GameControllerRumble（:455-464；pad_sdl.c:202-208）。
- 桩：ShowControllerSupport 直接回“1 号玩家已确认”（:414-419）；键盘/鼠标 Initialize 全空函数。

## 12. 其余服务

| 文件 | 服务 | 真实度 |
| --- | --- | --- |
| nn_fs.c | fs | **真实现**（见 §7），33 个 HLE |
| nn_audio.c | audio | **真实现**混音+输出；aux/部分效果直通 |
| nn_vi.c | vi + nv:: | **真实现**窗口/vsync/节流；nv::InitializeGraphics 只打日志（:643-645） |
| nn_hid.c | hid | **真实现**（键盘/鼠标/XInput/SDL）；六轴为常量 |
| nn_os_sync.c | os 同步 | **真实现**，96 个 HLE |
| hos_thread.c / hos_mem.c | os 线程/内存 | **真实现** |
| nn_codec.c | codec(Opus) | **真实现**（libopus） |
| nn_movie.c + movie_mf.c | movie::（Media Foundation） | **真实现**；subsdk0 未被 lift（build_game.py:126-127 的 --skip subsdk0），故用 hos_hostcall 造 vtable 顶上（nn_movie.c:127-166）；依赖系统 VP9 解码器（movie_mf.c:71-75） |
| nn_applet.c | oe/account/time/pl/err/prepo | **常量回答**：英文、dock、Boost、InFocus、单用户 "Player"、UID 固定、字体空但“已加载”、PlayReport 丢弃（:13-154） |
| nn_diag.c | 游戏日志/abort | 转发到 hos_log；AbortImpl/UnexpectedDefaultImpl → fatal（:6-22） |
| nn_image_debug.c | PNG 编解码观测 | 纯日志/转储（HOS_PNG_LOG/HOS_PNG_DUMP/HOS_PNG_WATCH），不改语义（:38-78） |

## 13. TOTK 专属 hook（totk_*.c）

| 文件 | 手法 | 作用与结论 |
| --- | --- | --- |
| totk_cull.c | ENTRY_HOOK(0x08704b7c) 用宿主 C 重写 Frustum::isVisible | 城堡场景 ~5% 采样、最大单函数；严格复刻 FCMGT/FCSEL 顺序，FPCR 非 RNE 或 FPSR 位不齐、或出现 subnormal/微小结果时**退回 lifted 代码**（:41-56、84-122）。HOS_NO_NATIVE_CULL、HOS_CULL_CHECK（双跑比对） |
| totk_cores.c | ENTRY_HOOK(0x0a9bcccc) 重写核心数设置 + SPLICE_AFTER(0x09247560) 改 BaseProc 池 | 实验性（HOS_GAME_CORES / HOS_BASEPROC_EXECUTORS，默认关）。**注释里的结论：4 核时图形层 per-core 命令缓冲崩、6 核 boot 死锁；BaseProc 加 executor 反而 67→64.8 fps**（:1-14、48-57） |
| totk_icon_cache.c | nn_fs 钩子：savc:/icon/*.png 且 <256 字节就删掉当不存在 | 融合武器图标因“主视图第三帧才有模型、捕获取第一帧”而全透明并被永久缓存；删掉逼游戏重做（:1-36）。**根因未明**（注释列出已排除的假设） |
| totk_ultrawide.c | 2 处相机 aspect + 4 个 splice 覆盖注释列的 5 处 HUD 缩放 + 常量替换 | 仅在 hos_view_aspect>0 时生效；替换 main+0x377ac54 的 16:9 常量前先校验原值 0x3FE38E3B（:28-34） |
| totk_ultrawide_ui.c | OpenFile 重定向 content:/UI/LayoutArchive/Common.Product.100...blarc.zs | 用 zstd 解 SARC，把每个 .bflyt 的 RootPane 水平缩放设为 16:9/aspect（跳过 BG/Fade 等全屏层），再以 raw-block zstd frame 写回 <user>/ultrawide_ui/<aspect>.blarc.zs（:87-160） |
| totk_title.c | 4 个 ENTRY_HOOK + 输入注入 | 标题菜单加两条 credit 项（复用 Pa_Button_04/05）；点 “Ported By” 时**把光标移到 New Game 并按 A** 借出系统窗口，再劫持窗口文本 setter；“wasm.rip” 直接开链接；A 键从输入里摘掉（:195-234、262-359） |

## 14. 剖析（hos_prof.c）与 mods（mods.c）

- **采样剖析器**：HOS_PROFILE=<s> 起线程，用 SuspendThread + GetThreadContext 每毫秒抓每个 guest 线程的 RIP，
  配合 PDB（dbghelp）聚合到函数、按线程分组，可 HOS_PROFILE_PCS 导出指令级偏移（:363 起）。关键设计：只在挂起期间
  拷寄存器与栈副本，栈回溯在**恢复线程之后**做，否则与 dbghelp 的锁互锁（:404-406）。
  HOS_PROFILE_THREAD/CALLERS/LINES/EXCLUDE/DELAY/GATE 细分。
- 其它诊断线程：hos_backtrace（:582）、hos_dump_threads（:615）、HOS_EXIT_AFTER 退出时 dump 内核等待表
  （hos_svc.c:208-221）、HOS_STALL_MS（:971）、HOS_MEMDUMP、HOS_POKE（:916）、HOS_AB 帧时间基准（:777）、
  HOS_THREAD_STATS（:695）、HOS_TRACE_DRIVER_ALLOC（hook VirtualAlloc 抓驱动分配，:1112-1215）、
  HOS_MEM_TIMELINE / MEM_MAP（:43-224）。崩溃过滤器把内存里排队的日志刷出去（hos_thread.c:159-175）。
- **mods.c**：兼容模拟器布局（<mod>/romfs、<mod>/exefs/*.pchtxt|*.ips；<mods>/contents/<tid>/… 也能识别；
  名字以 -/_ 开头为关闭；同名文件取路径排序靠后者）（:1-14、299-330）。RomFS 部分启动时一次性索引成哈希表，
  OpenFile 时**优先于**真实 romfs 命中（mods.c:373-385），目录列举把 mod 独有条目追加在游戏条目之后（:389-414）。
  exefs 部分：.pchtxt/IPS/IPS32 按 build id 匹配模块，**data 段补丁**直接写进已加载镜像（:161-179），
  **code 段补丁不能运行时生效**——必须在 build.py lift --mods 时编译进去（:167-176、365-369）。

## 15. 设计取舍与理由（代码里写明的）

1. **HLE 在 nn:: 而非 IPC**：省掉服务端、句柄表、超时与序列化；代价是任何“必须经过服务的语义”（权限、错误码、
   异步完成）都要手工伪造，且桩静默返回 0 会掩盖问题。
2. **固定 4 GB 宿主窗口 @ 16 TB**：让 A64_PTR 退化成一次 32 位加法（a64.h:53-65），换来 lifted 代码里没有基址寄存器。
3. **svc 用 switch**：25 个分支、无表；可内联、可读，未知 svc 立刻 fatal（宁可炸也不要静默错）。
4. **线程 1:1 + 宿主 OS 调度**：不写调度器；Horizon 语义只补在“用户态互斥量仲裁”与“优先级交授”上
   （hos_svc.c:40-44 的注释说明这套算法与 libnx 的对应关系）。
5. **对象表 + 墓碑复用**：游戏每帧初始化/finalize 约 25 个短命对象，新对象会漏 13 MB/分（nn_os_sync.c:70-74）。
6. **音频延迟提交**：让主机 DSP 只看到完整 update（nn_audio.c:648-660）。
7. **present 用 mailbox 式（就绪即出）而非栅栏**：注释解释“渲染器自己拷了每帧，队列后没人再读那块 buffer”
   （nn_vi.c:523-528）——这是把主机语义**放宽**的关键假设。
8. **不跑 SDK 的 Surface/binder**，自己实现 ANativeWindow（nn_vi.c:200-201）。

## 16. 覆盖度小结

- **真实现**：guest 内存/堆/地址区间、线程与 TLS、用户态互斥量/条件变量仲裁、os 同步全套、fs 挂载与文件/目录、
  音频图与 waveOut、Opus 解码、vi 窗口/vsync/节流、hid 三路输入+震动、time/oe/account 常量、movie（MF）、
  窗口与热键、mods、剖析与诊断。
- **近似**：QueryMemory/GetInfo/QueryMemoryInfo 的合成值、六轴静止姿态、核号合成（掩码低位 % cores）、
  program_id 硬编码、SystemEvent 的假 handle（0xFFFF0001/2）。
- **桩/空实现**：audio 的 aux send/return、SetVoicePriority、IsVoiceDroppedFlagOn；hid 的 InitializeKeyboard/
  InitializeMouse/SetSupportedNpadIdType/StartSixAxisSensor；oe 的通知与 PopNotificationMessage（永久 sleep）；
  nn::pl 共享字体（0 大小）；nn::prepo 报告丢弃；nn::diag::GetBacktrace 返回 0；fiber 直接 fatal
  （hos_thread.c:674-675）；CloseHandle 是 no-op。
- **未覆盖**：任何真正的 IPC 服务（sm:/、bsd/、swkbd 交互）、hid 六轴数据、多手柄玩家区分。

## 17. 风险与脆弱点

1. **硬编码 TOTK 1.0.0 地址**：0x08704b7c、0x0a9bcccc、0x087a83e4、0x088f689c、0x09ada6c0 等（totk_*.c、
   nn_vi.c:307,315）。SPLICE_AFTER 的 anchor 文本对不上会让构建直接失败（build.py:148-150），但 ENTRY_HOOK 是
   地址直接生效——**换版本可能静默 hook 到别的函数**。totk_ultrawide 做了原值校验（:32），pin_totk_interval 也校验
   ctl == fw+0x248（nn_vi.c:341）。
2. **帧步长直接写游戏变量**（nn_vi.c:307-318、320-327）：偏移一错，游戏速度/物理步长静默改变而不是崩溃。
3. **present 提前释放槽位**依赖“渲染器有独立副本”（nn_vi.c:523-528）；若将来改成直接扫描输出就会读到正在写的 buffer。
4. **内核仲裁的竞态**：wait_key/signal_key 的注释记录了两类真实事故——“key 先置位再放锁”的时序、以及“普通 store
   让锁字指向已解锁线程”（hos_svc.c:148-154、184-199）。这类代码任何改动都要重新论证。
5. **对象表生命周期**：墓碑 30 s、spare 复用、multi-wait 链接；注释直言“释放仍被链接的对象会让大多数 boot 挂死”
   （nn_os_sync.c:70-74）。同地址换类型时 kind 竞争被 CAS 兜住（:219），但仍是共享可变状态。
6. **固定上限**：1024 线程（hos_thread.c:54）、1024 等待者（hos_svc.c:132,156）、4096 fs 句柄、512 voice、96 mix、
   8192 hostcall 槽——超限多为 hos_fatal。
7. **系统区与 hostcall 表同段**：hos_sys_alloc 只检查 HOS_SYSTEM_END=0x07000000（hos_mem.c:57），而 host call 槽从
   0x06000000 起（hos_hostcall.c:12）——系统区若用到 96 MB 边上就会压到函数指针表。当前用量远小于此，属**潜在**冲突【推测】。
8. **4 GB 一次性 VirtualAlloc(MEM_COMMIT)** 依赖“未触碰的已提交页不耗物理内存”（hos_main.c:112）；32 位宿主或地址空间
   碎片环境下直接失败。大页模式还要求账户有 SeLockMemoryPrivilege（:88-96）。
9. **svcExitThread 直呼 ExitThread(0)**（hos_svc.c:260-262）：跳过 nnmusl 的 TLS 退出 hook（对比 hos_thread.c:363-367
   的正常路径），线程局部析构不会跑。
10. **优先级/亲和性只做记账**：hos_thread_priority 不改宿主优先级（hos_thread.c:101-105），真实调度靠 Windows；
    主机上的实时性保证在这里不存在。HOS_PIN/HOS_PRIO 的实验结论也写着“自定义绑定比 Windows 自己的调度更慢”（:424-429）。
11. **音频队列与容量**：Cmd 队列增长失败就退化为立即 apply（nn_audio.c:751-761），语义会悄悄变；voice 满 512 时
    AcquireVoiceSlot 返回 0（:1041），游戏侧可能当成功。
12. **nn_fs 的伪错误码**：只用少数几个硬编码 Result（:16-19），调用方若区分更细的错误会误判。
13. **totk_title.c 的输入注入**：依赖菜单项数量（3 或 4）、是否有存档、窗口按钮顺序，任一处变化就点错
    （:176-193、300-332）；它还会“吞掉”玩家的 A 键。
14. **hos_blank_cached_icon 会删用户目录里的文件**（totk_icon_cache.c:33），且根因未明。
15. **依赖外部组件**：VP9 Video Extensions（movie_mf.c:71-75）、SDL2.dll（pad_sdl.c:69-96，缺则只剩 XInput）、
    wgpu-native / WebGPU 设备（缺则退化到 CPU 回读）。

## 18. 可验证的证据（只读命令）

    grep -c "^HLE(" a64-kit/hos/nn_os_sync.c                        # 96
    grep -c "case 0x" a64-kit/hos/hos_svc.c                       # 25 个 svc 分支
    grep -rn "CMIF\|parcel\|hipc" a64-kit/hos/*.c                 # 无命中：没有 IPC 序列化
    grep -n "OS_NAMESPACES\|os_facing\|stubbed" a64-kit/build.py   # 哪些 import 被 HLE、哪些被桩
    sed -n '60,68p' a64-kit/hos/nx_image.py                       # 在“sdk 定义处”打 hook
    sed -n '107,135p' a64-kit/hos/hos_main.c                      # 4 GB 窗口与写监控
    sed -n '234,250p' a64-kit/hos/hos_main.c                      # 到 nnMain 的启动顺序
    sed -n '443,470p' a64-kit/hos/nn_vi.c                         # perform 0x13 注册窗口 buffer
    sed -n '516,530p' a64-kit/hos/nn_vi.c                         # 提前释放上一帧槽位
    sed -n '339,350p' a64-kit/hos/nn_hid.c                        # 键盘映射表

（end）
