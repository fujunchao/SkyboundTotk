# 05 · Shader 重编译器与 Web 移植

范围：`a64-kit/gpu/third_party/suyu/`（Maxwell 前端 / IR / 优化 pass / WGSL 后端 / video_core 子集）、
`a64-kit/gpu/compat/`、`a64-kit/gpu/tools/*`、`a64-kit/hos/web/*`、`a64-kit/tools/serve_web.py`、
渲染器侧调用者 `a64-kit/gpu/render/rx_shader.cpp` 与 `build.py` 的两个目标。
本报告全部结论基于静态阅读（无构建、无游戏数据），路径均为仓库相对路径。

---

## 1. 规模与目录（先给量化）

| 目录 | 文件 | 行数 | 说明 |
|---|---|---|---|
| `.../suyu/` 合计 | 246 | ~47.8k | 只有 3 个子树：`common/`、`shader_recompiler/`、`video_core/` |
| `suyu/common/` | 12 | 1,405 | 类型/位域/字节序，外加上游 common 的替身（见 §9） |
| `suyu/shader_recompiler/`（顶层） | 12 | 1,543 | `profile.h`、`runtime_info.h`、`shader_info.h`、`environment.h`、`program_header.h` 等契约头 |
| `frontend/ir/` | 29 | 6,524 | IR 定义：`ir_emitter`、`opcodes.inc`(586)、`value`、`basic_block`、`attribute` |
| `frontend/maxwell/` | 15 | 3,045 | `maxwell.inc`(283 行表)、`decode`、`control_flow`(645)、`structured_control_flow`(998)、`translate_program`(465) |
| `frontend/maxwell/translate/impl/` | 92 | 10,066 | 85 个 `*.cpp`，292 个 `TranslatorVisitor::` 处理器定义 |
| `ir_opt/` | 18 | 5,428 | 16 个 pass + `passes.h` |
| `backend/wgsl/` | 25 | 6,211 | **本项目渲染器唯一的后端** |
| `backend/glsl/` | 25 | 6,404 | **不参与程序构建**，只给 `tools/shader_test` 做参照（`build.py:443`） |
| `video_core/` | 5+2+6 | 7,378 | `maxwell_3d_regs.h`(3,391)、`surface.*`、`textures/{decoders,astc,texture.h}`、`texture_cache/format_lookup_table` |

许可证面（SPDX 实际统计）：`GPL-2.0-or-later` 232 文件（yuzu 版权），`GPL-3.0-or-later` 61 文件（suyu 2025/2026 版权），
另有 1 个 `CC0-1.0`、1 个 `BSD-3-Clause AND GPL-2.0-or-later`、1 个 `Apache-2.0`。见 §8。

---

## 2. 端到端流水线：Maxwell 机器码 → WGSL 文本

```
 guest 显存中的 NVN shader package（或 program_region + offset）
        │  rx_shader.cpp:1007 read_nvn_code()：control 0x880 字节里取 c1 区偏移(+0x700)、代码长度(+0x704)、
        │  stage(+0x714)、compute 的 block/shared/local(+0x748/0x754/0x760)；c1 = 常量 bank1 数据
        ▼
 Shader::Environment 实现（三套！）
   GuestEnvironment      rx_shader.cpp:57   Regs+Channel（经典 NVN 寄存器路径，ctx 区）
   ComputeGuestEnvironment rx_shader.cpp:128 QMD/launch 描述符（compute）
   NvnEnvironment        rx_shader.cpp:1082 NVN 命令流路径：cbuf0/2 走 driver bank，cbuf3..18 走 UBO 绑定表
        │  接口：environment.h:75-100 ReadInstruction/ReadCbufValue/ReadTextureType/ReadPixelFormat/
        │  ReadViewportTransformState/LocalMemorySize/SharedMemorySize/WorkgroupSize + SPH()/StartAddress()
        ▼  Flow::CFG env, start_address = start + sizeof(ProgramHeader)=0x50     （rx_shader.cpp:216/1163）
 CFG 重建  frontend/maxwell/control_flow.cpp
   · 线性扫描 8 字节指令，遇 BRA/BRX/JMP/EXIT/RET/KIL 切块（control_flow.h:28 EndClass）
   · SSY/PBK/PEXIT/PRET/PCNT/PLONGJMP 压栈（control_flow.h:37-62），条件分支拆成 cond/else 双块（:362）
   · BRX 跳表：indirect_branch_table_track.cpp（107 行）
        ▼
 ASL 结构化  structured_control_flow.cpp:986 BuildASL()
   GotoPass（goto/label 归约成 if/loop/break，NeedsLift :310）→ TranslatePass
   TranslatePass 逐语句调 Translate(env, block, begin.Offset(), end.Offset())  （:691）
        ▼
 指令翻译  translate/translate.cpp:18 Translate()
   Decode(insn)（decode.cpp:43，用 maxwell.inc 的 20 bit mask/value 逐条匹配，280 条 opcode）
   → visitor.<Opcode>(insn)（292 个处理器，85 个文件；未实现的 68 个在 not_implemented.cpp）
   → IR::IREmitter 产出 SSA-ish IR（此时仍有 GetRegister/SetRegister）
        ▼
 TranslateProgram（translate_program.cpp:241-329）—— pass 顺序见 §3
        ▼
 IR::Program（stage/blocks/syntax_list/workgroup_size/shared_memory_size/info）
        ▼
 EmitWGSL（emit_wgsl.cpp:507）
   Precolor(phi 预着色) → EmitCode（按 syntax_list 展开 if/loop/break）→
   header（bindings、ub、helper 函数）+ `fn shader_main(){ var ...; <body> }` + EntryPoint()
        ▼  返回一个完整 WGSL 模块字符串（顶点/片元/计算各一个）
 消费：rx_shader.cpp:690/702 把文本存进 ShaderModule::wgsl → intern 成共享字符串（:746-769）
   → ensure_module(:396) 用 WGPUShaderSourceWGSL 建 WGPUShaderModule（rx_core.cpp:926 program_module）
   → pipeline 用 text_hash/cache_hash 做 key（:426-427），native 走 rxcache/，web 走 IndexedDB+rxcache
```

**调用者只有一个入口点函数**：`Shader::Maxwell::TranslateProgram(inst_pool, block_pool, env, cfg, host_info)`
（`translate_program.h:24`），`rx_shader.cpp:230 / 529 / 1169`，测试工具 `shader_test.cpp:130`、`wgsl_check.cpp:129`。

**产物消费方**：`EmitWGSL(profile, runtime_info, program, bindings, options)`（`emit_wgsl.h:59`）返回
`std::string`；渲染器不解析它，只把它当不透明文本（hash 去重 → `wgpuDeviceCreateShaderModule`）。
调试通道很完整：`RX_DUMP_WGSL`（dump 文本）、`RX_WGSL_OVERRIDE`（用改过的文本顶替，`rx_shader.cpp:716-729`）、
`RX_DUMP_IR`（`ir_dump_path`，常数折叠前的 IR，`translate_program.cpp:300-306`）、`RX_DUMP_SLOW_WGSL`。

---

## 3. IR 优化 pass 逐个说明（`translate_program.cpp:286-327` 的实际顺序）

| # | pass | 作用 | 入口行 |
|---|---|---|---|
| 1 | `LowerFp64ToFp32` | 无 f64 设备：f64 拆成双 u32 软件实现（`lower_fp64_to_fp32.cpp` 的 PackedF64ToF32 等） | :287 |
| 2 | `LowerFp16ToFp32` | 无 fp16：`FPAdd16→FPAdd32` 等 opcode 映射 | :290 |
| 3 | `LowerInt64ToInt32` | u64 拆成 `PackUint2x32`/`UnpackUint2x32` | :293 |
| 4 | `ConditionalBarrierPass` | 把条件控制流里的 `Barrier` 降级为 Identity，并在"第一个提前 return 之前的最后一个无条件块"补一个 Barrier（注释明确写该修复来自 Rexcomp，TOTK 光照 tile 分类依赖它） | :296 |
| 5 | `SsaRewritePass` | Braun 2013 的 SSA 构造：`SetRegister/SetPred/SetGotoVariable/*Flag` → phi/值（`ssa_rewrite_pass.cpp:308-369`）。**这一步让 WGSL 后端可以完全不实现寄存器** | :299 |
| 6 | `ConstantPropagationPass` | 1,116 行，用 `Environment` 里读到的真实 cbuf 值做常量折叠（所以同一 shader 在不同 cbuf 内容下会翻出不同结果——这也是为什么要 `ir_dump_path` 在它之前 dump） | :308 |
| 7 | `PositionPass` | 仅 VertexB 且 `ReadViewportTransformState()==0` 时：把 `SetAttribute(PositionX/Y)` 改成 `fma(v, 2/renderArea, -1)`（`position_pass.cpp:22-76`） | :310 |
| 8 | `GlobalMemoryToStorageBufferPass` | 把 `LoadGlobal*/WriteGlobal*/GlobalAtomic*` 换成 `LoadStorage*`/`StorageAtomic*`，并反推 storage buffer 描述符（`global_memory_to_storage_buffer_pass.cpp`，582 行） | :312 |
| 9 | `TexturePass` | 877 行，核心：把 `TEX/TLD/TLD4/TXD/TXQ...` 的 TIC/TSC 句柄从 cbuf 追踪出来，生成 `TextureDescriptor` 表、决定 sampler 共享、打 SNORM/整数格式补丁（`texture_pass.cpp:667`） | :313 |
| 10 | `RescalingPass` | 仅在 `Settings::values.resolution_info.active` 时（本项目 settings.h 里恒 false）→ 死代码 | :315 |
| 11 | `DeadCodeEliminationPass` | 反向扫描删无用指令 | :318 |
| 12 | `VerificationPass` | 仅 `renderer_debug` 时，类型/支配性校验 | :319 |
| 13 | `CollectShaderInfoPass` | 986 行，产出 `Info`（uses_*、descriptors、loads/stores 位图、interpolation），后端全部依赖它 | :322 |
| 14 | `LayerPass` | 设备不支持 `gl_Layer` 时把 Layer 映射到一个空闲 generic | :323 |
| 15 | `VendorWorkaroundPass` | NVIDIA 字节交换 bug（`IAdd32`→`BitwiseOr32` 模式匹配） | :324 |
| — | `CollectInterpolationInfo` | 从 SPH 的 `ps.imap_generic_vector` 推出每 generic 的插值方式，逐分量不同则抛 | :326 |
| — | `AddNVNStorageBuffers` | 把 NVN 的 16 个 driver storage buffer（cbuf0 的 0x110/0x210/0x510… 偏移）补成描述符 | :327 |

双顶点着色器：`MergeDualVertexPrograms`（:331）在图形路径上与 `VertexATransformPass/VertexBTransformPass` 配合，
但 `rx_shader.cpp:643/646` 只用 `Stage::VertexB`，A 路（NVN 的 vs A/B 分段）在本项目未启用。

---

## 4. WGSL 后端的 binding / descriptor 契约（与渲染器的硬契约）

### 4.1 bind group 与 binding 编号
`wgsl_emit_context.cpp:7-20,117-134`：**每个 stage 一个 bind group**——顶点 `@group(0)`、片元 `@group(1)`、计算 `@group(0)`。
binding 号由 `Bindings::unified`（`backend/bindings.h:11`）按**声明顺序**递增分配，顺序固定为：

| binding | 内容 | 证据 |
|---|---|---|
| 0 | `var<uniform> <stage>_ub: array<vec4<u32>, N>`，**动态 offset，每 draw 一份** | `wgsl_emit_context.cpp:177`；渲染器 `rx_draw.cpp:677` |
| 1..k | 未进 ub 的常量缓冲：`var<storage, read> <stage>_cbuf<i>: array<vec4<u32>>` | :237 |
| … | `var<storage, read\|read_write> <stage>_ssbo<i>: array<u32>`（有原子则 `array<atomic<u32>>`） | :278 |
| … | 每个纹理两个 binding：`tex<b>: texture_2d<f32>` + `samp<b+1>: sampler`（深度用 `texture_depth_2d`/`sampler_comparison`） | :308-311 |
| … | 存储纹理：`img<b>: texture_storage_2d<rgba32uint, read_write>` | :360 |
| 末尾 | 顶点 pull：`var<uniform> vs_pull: vec4<u32>` + `vs_vb<k>: array<u32>`（最多 3 路） | `emit_wgsl.cpp:516-527` |

渲染器侧的同构实现在 `rx_draw.cpp:473-475,641-700`（"the layout follows the WGSL backend"），两边必须逐条对齐。

### 4.2 uniform block（binding 0）的 lane 布局
`wgsl_emit_context.cpp:11-15,158-190`：
```
[0] scaling()      = 分辨率缩放
[1] render_area()  = 渲染区（.w = 颜色目标 UNORM/SNORM 钳制位图；.z != 0 → 16 位深度目标）
[2] (ax,bx,ay,by)  = 视口变换 X/Y
[3] (az,bz,vertexID offset, instanceID offset)
[4+2t]             = 纹理 t 的 TIC 分量 swizzle（x 位3-5=border 轴，y 位3-5=深度比较函数，z 位8-31=LOD bias×256）
[5+2t]             = 纹理 t 的 border color
之后                = 放得下的常量缓冲（原始字，浮点靠 bitcast），上限 0x1000 个 vec4 = 64 KB
```
常量缓冲是否进 ub 的判定 `ub_size + size > 0x1000`（:169）在渲染器 `rx_draw.cpp:668` 被逐字复制——**这是双份真值来源**。
`RX_CBUF_CLAMP=1` 可退回 WebGPU 的边界钳制行为（:35-40,241-245）。

### 4.3 WebGPU 缺口的三处运行时模拟
1. **纹理分量 swizzle / sampler border / LOD bias**：WebGPU 的 view 无 swizzle、sampler 无 border/bias，
   后端为每个纹理生成 `swizzle<b>()/border<b>()/bordercmp<b>()/lodbias<b>()` 四个 helper，参数放 ub（:316-344）。
2. **16 sampler 上限（浏览器）**：`#ifdef __EMSCRIPTEN__` 下从第 17 个纹理起复用同 kind 上一个纹理的 sampler
   （`sampler_plus1`，:294-311）。原生构建不启用，靠 wgpu 的大上限。
3. **顶点取数**：WebGPU 的 vertex fetch 无法描述 A2B10G10R10/B10G11R11/非 4 倍数 stride/超过 8 个 buffer（TOTK 用 9 个）。
   → `Options::VertexUnpack`（`emit_wgsl.h:20-28`）把属性读成一个 u32 再在着色器里拆位；
   `Options::VertexPull`（:42-49）则完全改成 storage buffer 取数，属性格式字（offset/size/type/bgra）在
   `emit_wgsl.cpp:236-316` 用主机的规则重算（含 10/11 位浮点 `uf10_/uf11_`）。渲染器选择见 `rx_shader.cpp:452-502`。

### 4.4 特殊寄存器与 varying 映射
`wgsl_emit_context.cpp:200-229` 把 GLSL 的内建**全部声明成 `var<private>` 模块级变量**
（`gl_Position/gl_FragCoord/gl_FragDepth/gl_ClipDistance[8]/gl_SubGroupInvocationARB/…`），
指令发射器像写 GLSL 一样读写它们；真正映射到 WGSL 内建只发生在 `EntryPoint()`：

| 阶段 | 输入 | 输出 |
|---|---|---|
| 顶点 (`emit_wgsl.cpp:320-419`) | `VsIn{@builtin(vertex_index), @builtin(instance_index), @location(i) a<i>}` | `VsOut{@builtin(position) position, @location(i) [@interpolate(flat\|linear)] o<i>}` |
| 片元 (:420-491) | `FsIn{@builtin(position), @builtin(front_facing), [@builtin(sample_index)], @location(i) a<i>}` | `FsOut{@location(i) c<i>: vec4<f32/i32/u32>, [@builtin(frag_depth)]}` |
| 计算 (:492-499) | `@compute @workgroup_size(x,y,z)` + `local_invocation_id`/`workgroup_id` | — |

其它约定：
- 视口变换在**顶点着色器里**做（WebGPU 无负视口/越界视口）：`output.position = (t.x*p.x+t.y*p.w, t.z*p.y+t.w*p.w, z.x*p.z+z.y*p.w, p.w)`（:395-398）。
- 16 位深度目标：`d16q_()` 把深度量化到最近步进+1/4 步（TOTK 把材质号编码进 D16），顶点与片元都做（:184,401-404,485）。
- `opq_(x)` 用 `ub[0].w|bitcast` 阻止宿主编译器把极小 epsilon 重结合（:187-190）。
- 片元 early-Z：`@early_depth_test(force)`（wgpu 扩展属性，可用 `RX_EARLY_Z_ATTR` 覆盖；:452-456），
  仅在 `runtime_info.force_early_z` 且不写深度时；本项目只有 NVN 路径会置 `force_early_z`（`rx_shader.cpp:1491`）。
- 循环安全计数：`loop<N>` 从 0x2000 递减（`Settings::values.disable_shader_loop_safety_checks` 恒 false，:167-178,201-203）。
- 所有局部变量集中在 `shader_main()` 顶部声明（`emit_wgsl.cpp:185-204`），靠 `VarAlloc` 复用槽位（`var_alloc.h`）；
  这规避了 WGSL 的"必须先声明"约束，代价是 WGSL 体积大（实测每模块 ~15 KB，`rx_internal.h:406-408` 注释）。

### 4.5 driver 常量 bank 的关键假设（c0 / c1 / c2）

NVN 的着色器把常量分成三类 bank，重编译器必须**按语义**回答 `Environment::ReadCbufValue(bank, offset)`：

| bank | 内容 | 代码里的假设 |
|---|---|---|
| **c0** | driver bank：每个 stage 一份 UBO 绑定表（16 字节一项：地址 lo/hi + size），另有 compute 的 grid 等 | `rx_shader.cpp:1105-1113`（cbuf0/2 → `nvn_driver_bank_word`；cbuf3..18 → `s.ubo[stage][i-3]` 的地址+size 表）；表基址按 stage 取 `0x110/0x310/0x510`（`rx_nvn.cpp:311`） |
| **c1** | **程序自带**的数据段：NVN package 的 control 区 `+0x700` 给出 c1 在代码区内的偏移，其后到结尾全是 c1 字节 | `rx_shader.cpp:1012,1041`（`c.c1.assign(area.begin()+c1o, area.end())`）、`:1100-1104`（直接 memcpy 出 4 字节）。**这条最容易错**：c1 未按 UBO 对齐、长度即剩余区，越界必须返回 0 |
| **c2** | 纹理/采样器/图像描述符字：tex 在 `0x20+8*32` 与 `0x168+8*128`，img 在 `0x120+8*9`，samp 在 `0x568+8*128`（8 字节槽，只取低字，偏移 8 对齐） | `rx_nvn.cpp:302-308` |

c2 的特殊性在于**翻译期就要知道纹理类型/格式**（`ReadTextureType/ReadTexturePixelFormat` 决定 WGSL 里声明成
`texture_2d<f32>` 还是 `texture_depth_2d`、以及是否整数），所以 `NvnEnvironment` 顺手记录"读过哪些 c2 字"
（`c2_reads`，`rx_shader.cpp:1106,1147`），模块按"答案集合"缓存成多个 variant
（`rx_shader.cpp:1178-1185 NvnVariant::answers`、`:1298/1455-1457`）——即**同一个程序在不同纹理类型下要重新发射 WGSL**。
这是 NVN 路径与经典寄存器路径（`GuestEnvironment` 从 TIC 读）最大的结构性差异。

⚠️ 潜在不一致（本报告发现的跨范围问题）：`translate_program.cpp:92-135 AddNVNStorageBuffers` 用的 driver 表基址约定是
`VertexA/B=0x110, TessControl=0x210, TessEval=0x310, Geometry=0x410, Fragment=0x510, Compute=0x310`（yuzu 的 HLE-macro 布局），
而 `rx_nvn.cpp:311` 给 UBO 表的基址是 `vs=0x110, fs=0x310, 其它=0x510`。两者只有在 `uses_global_memory` 为真时才相遇
（TOTK 基本不触发，且 WGSL 后端遇到 global memory 直接 throw，`wgsl_emit_context.cpp:404`），所以现在**没有活跃故障**，
但一旦有人打开 64 位全局内存支持，就会踩到这张对不上的表。

---

## 5. 覆盖度：实现了什么、桩了什么

**指令级**（`frontend/maxwell/translate/impl/`）：280 条 opcode，292 个处理器；`not_implemented.cpp` 里 68 个定义，
其中 60 个直接 `throw NotImplementedException`，8 个是静默空操作（`CAL/NOP/SSY/PBK/PCNT` 注释为 no-op，
`KIL` 也是 no-op——片元 kill 由 CFG 的 demote 路径处理，`SAM/RAM` 只 `LOG_WARNING("(STUBBED)")` 什么也不发）。
值得点名的缺口：`IMAD_*/``IMUL_*``/`PRMT_*``/`LD`/`ST`/`B2R`/`R2B`/`CCTL*`/`VADD/VSET/VSHL/VSHR/VABSDIFF*`。
TOTK 的 shader 大量使用 `XMAD`（`integer_short_multiply_add.cpp` 实现了）而不是 `IMAD`，所以能跑；一旦游戏换编译版本就可能立刻撞墙。

**后端级**（`emit_wgsl_not_implemented.cpp`）：`EmitGetRegister/EmitSetRegister/EmitGetPred/EmitSetPred/`
`*GotoVariable/*IndirectBranchVariable/*Flag/Get*FromOp` 全部 `NotImplemented()`——它们靠 §3 第 5 步的 SSA 重写消失。
注意 `NotImplemented()` 宏的文案仍写 `"GLSL instruction {}`（`emit_wgsl_instructions.h:24`），是从 GLSL 后端搬过来时漏改的。

**明确的"不支持"清单**（都是 throw，不是猜测）：
`WGSL texture buffers`（`wgsl_emit_context.cpp:289`）、`image buffers`（:352）、`texture arrays`/（:296-298）、
`image arrays`（:356-358）、`WGSL global memory (64-bit addresses)`（:404-406）、
`WebGPU has no {} shaders`（:133 —— 几何/细分在 WebGPU 里根本没有）。

**warp/subgroup 是退化实现**（`emit_wgsl_warp.cpp`，203 行）：WebGPU core 没有 subgroup，
所以 `VoteAll/VoteAny` = 谓词本身（:65-71）、`VoteEqual` = true（:73-75）、`SubgroupBallot` = 1 位（:77-79）、
`EqMask/LeMask/GeMask` = 1、`LtMask/GtMask` = 0（:81-99），四个 shuffle 都退化成"取自己的值 + 越界谓词"（:101-178）。
且 `gl_SubGroupInvocationARB` 只声明、从不赋值（`wgsl_emit_context.cpp:221`，`EntryPoint` 也不写它）。
⇒ **任何依赖 warp 协作的 compute kernel 结果都是错的**；GLSL 后端有真正的 `subgroupShuffle` 路径可对照。

**其它值得记录的边界**：`EmitSR_WScaleFactorXY/Z` 是空桩（`emit_wgsl_context_get_set.cpp:450-456`），
`EmitSetAttributeIndexed` 是 `NotImplemented()`（:361），`EmitYDirection` 返回 1.0 并让渲染器翻转 Y（:458-461）。

---

## 6. 纹理/格式支撑：video_core 的 4 个文件在本项目里怎么被用

| suyu 文件 | 内容 | 本项目调用点 |
|---|---|---|
| `video_core/surface.h/.cpp` | 137 个 `PixelFormat` 枚举（`PIXEL_FORMAT_LIST`，:20-135）、`DefaultBlockWidth/Height`、`BytesPerBlock`、`SurfaceTarget`、格式谓词 | **最热**：`rx_core.cpp:517/2739/3091/3471`、`rx_draw.cpp:2223/3358`、`rx_shader.cpp:88/155/315-318/1122`（着色器侧输出类型、整数判定） |
| `video_core/textures/decoders.cpp/.h` | Block-linear（GOB 64×8，`decoders.h:16-27`）反/正 swizzle、`CalculateSize`、`GetGOBOffset` | 纹理上传 `rx_texture.cpp:723/772`；渲染目标回写 `rx_core.cpp:3419-3425`（3D 切片先反再正） |
| `video_core/textures/astc.cpp`（2,054 行） | 纯软件 ASTC 解码（trit/quint 权重、块信息解码） | `rx_texture.cpp:20,546-604`：默认 CPU 解码到 RGBA8；`RX_ASTC_BC7`/GPU 路径另有实现 |
| `video_core/texture_cache/format_lookup_table.cpp` | `PixelFormatFromTextureInfo(format,r,g,b,a,srgb)` 哈希表（:44） | **着色器侧**也要：`rx_shader.cpp:84,151` 让 `Environment::ReadTexturePixelFormat` 能回答，TexturePass 才能判定整数/SNORM |
| `video_core/maxwell_3d_regs.h`（3,391 行） | Maxwell3D 寄存器文件与静态布局断言 | `rx_internal.h:32` 的 `Regs` 就是它（`RX_REG` 用 `offsetof` 生成寄存器索引，:54） |

注意 `video_core/texture_cache/` 只剩 `format_lookup_table`，真正的 texture cache（surface 分层、mip 计算、
重叠检测）**没有移植**——本项目用自己的 `rx_texture.cpp` + `Image`/buffer cache 替代（`rx_core.cpp` 里的 writeback 逻辑）。

---

## 7. Rexcomp 相对上游 suyu 改了哪些东西

树里显式写 "Rexcomp" 的只有 11 处（`grep -rn Rexcomp`），但改造面远大于此。可靠的判据有三类：
注释自称、与 GLSL 后端一一对照、以及与上游 GLSL 语义不符的地方。

| 改造点 | 证据 |
|---|---|
| **整个 WGSL 后端**（25 文件 / 6,211 行）是 Rexcomp 新增；上游只有 glsl/spirv | `emit_wgsl.h:4`、`wgsl_emit_context.cpp:4`、`emit_wgsl_image.cpp:4` 头注释均标 "WGSL backend (Rexcomp)"；`build.py:443` 把 glsl 目录排除出构建 |
| `common/` 三个核心头被替换为替身（assert/logging/settings） | `common/assert.h:1`、`common/logging.h:1`、`common/settings.h:1` 首行 "Rexcomp replacement for…" |
| `video_core/` 只留格式/解码，`gpu.h`/`render_formats.h`/`maxwell_3d_regs.h`/`workers.h` 是裁剪替身 | 同名 4 处 "Rexcomp:" 注释 |
| `Options`（vertex_unpack / vertex_pull / output_interpolation）——上游没有 | `emit_wgsl.h:31-50` |
| storage buffer 别名（浏览器禁止同一 range 两个可写 binding） | `emit_wgsl.h:52-57`、`wgsl_emit_context.cpp:97-98,182-192,267-271`、`rx_shader.cpp:533-534` |
| uniform block 化常量缓冲 + `_at()` 越界读 0 语义 + `RX_CBUF_CLAMP` | `wgsl_emit_context.cpp:158-178,231-249`；`runtime_info.h:92-94 cbuf_ub_limit` 注释直接写 "(WGSL backend)" |
| 视口变换上移进顶点着色器（`rescale_`）与 Y 方向由渲染器处理 | `wgsl_emit_context.cpp:179-190`、`emit_wgsl_context_get_set.cpp:458-461` |
| `d16q_`（16 位深度步进量化，TOTK 材质号编码）、`opq_`（阻重结合）、`rt_`（阻常量折叠） | `wgsl_emit_context.cpp:180-190,226-228` |
| 纹理 swizzle/border/LOD bias 的 helper 模拟、16 sampler 共享 | `wgsl_emit_context.cpp:292-344` |
| `ConditionalBarrierPass` 的 hoist 修复（TOTK 光照 tile） | `conditional_barrier_pass.cpp:9-14` 注释显式 "Rexcomp:" |
| `ir_dump_path`（IR dump 调试钩子） | `translate_program.h:21-22`、`translate_program.cpp:300-306` |
| `IR::IsLegacyAttribute` 被搬到 `attribute.h` 以便 `load_store_attribute.cpp` 使用 | `translate_program.cpp:137` |
| 上游语义偏差：`EmitYDirection`、`EmitSR_*` 空桩、warp 退化、`NotImplemented()` 文案仍写 GLSL | 见 §5 |
| 仓库内还在活跃改动：WGSL 后端深比较参考值钳制（夜间阴影条带） | git `552fa10`，"WGSL backend: clamp the depth-compare reference to [0, 1]"，只改 `emit_wgsl_image.cpp`，含 Co-Authored-By: Claude |

---

## 8. 许可证边界：`wgsl_emit_context.cpp` 是 GPL 遗产，不是自研

**结论：WGSL 后端是 yuzu/suyu GLSL 后端的派生作品，SPDX 为 `GPL-2.0-or-later`，因此整条着色器流水线（含生成的 WGSL、
含被链接进 `a64emit.dll`/`SkyboundTOTK.exe`/`totk.wasm` 的目标码）都落在 GPL 之下。** 依据：

1. `wgsl_emit_context.cpp:1-5` 第一行就是 `SPDX-FileCopyrightText: Copyright 2021 yuzu Emulator Project` +
   `SPDX-License-Identifier: GPL-2.0-or-later`，第 4 行注释自认 "derived from suyu's GLSL backend context"。
   整个 `backend/wgsl/` 25 个文件里，23 个是 `GPL-2.0-or-later / yuzu`，只有 `emit_wgsl_integer.cpp`、
   `emit_wgsl_instructions.h`、`var_alloc.cpp` 三个叠加了 `Copyright 2025 suyu / GPL-3.0-or-later`（suyu 时代的改写）。
2. 结构上就是搬的：`emit_glsl_*` ↔ `emit_wgsl_*` 文件一一对应（各 25 个，6,404 / 6,211 行），
   `EmitContext::Add/AddU32/AddF32…`、`VarAlloc`、`header/code` 两段式、`Invoke<&Emit##name>` 的
   `opcodes.inc` 派发（`emit_wgsl.cpp:95-104`）与 GLSL 后端逐行同构。
3. 遗留痕迹：`#define NotImplemented() throw NotImplementedException("GLSL instruction {}", __func__)`
   （`emit_wgsl_instructions.h:24`）；`profile.h` 里那一堆 `has_gl_*` 设备缺陷位在 WGSL 后端仍被读取
   （`emit_wgsl_context_get_set.cpp:55,177`）——它们对 WGSL 没有意义。
4. `NOTICE.md:8` 自己也没声称这层是自研："suyu / yuzu shader recompiler, common and video_core parts,
   **with Rexcomp's WGSL backend and changes** | GPL-2.0-or-later and GPL-3.0-or-later per file"；
   `NOTICE.md:20-21` 进一步说 `a64emit.dll`、生成的运行时、lifted C 和每个游戏构建产物都是 GPL。
5. 影响面：`build.py:437-450 gpu_sources()` 把 `shader_recompiler/`（除 glsl）整棵编进渲染器，渲染器又被
   native exe 和 wasm 各编一次 ⇒ MIT（`LICENSE`）只覆盖 NotRexed 自有代码，**不能**把这套 shader 后端换成别的许可证。
   若要做"干净"的 MIT 版本，必须重写整个 `backend/wgsl`（以及 `frontend/maxwell`、`frontend/ir`、`ir_opt` 的全部
   GPL 部分），成本等于重做一个着色器重编译器。

---

## 9. Web 目标（Emscripten / wasm32）

### 9.1 目标与产物
`build.py:625-722 cmd_web()`：`emcc/em++` 把 lifted C + `rt/` + `hos/`（含 `hos/web/` 的 Windows 替身）+ `gpu/render/`
+ suyu 部分编成**一个 wasm + JS 胶水**：
`build/<name>/web/totk.js` + `totk.wasm` + `index.html`（`RX_WEB_MT=1` 时另出 `totk_mt.js` + `index_mt.html`，:716-721）。
关键链接参数（:702-711）：`-pthread`、`-msimd128`、`-msse2`、`--use-port=emdawnwebgpu`（浏览器 WebGPU 的 C 绑定）、
`-sJSPI=1`（同步等待 GPU 回调）、`-sPROXY_TO_PTHREAD=1`、`-sPTHREAD_POOL_SIZE=64`、`-sOFFSCREENCANVAS_SUPPORT=1`、
`-sGLOBAL_BASE=<guest_mb<<20>` + `-sINITIAL_MEMORY=4032MB` + `-sALLOW_MEMORY_GROWTH=0`（guest 窗口占 0..N GB，
宿主数据链在高处，故永不增长）、`-sMALLOC=dlmalloc`、`--js-library hos/web/library.js`。
是"Emscripten 目标"，不是裸 wasm；用 `__EMSCRIPTEN__` 做分支（`rx_internal.h:16-26` 定义 `RX_WEB`）。

### 9.2 `hos/web/` 各文件职责
| 文件 | 行 | 作用 |
|---|---|---|
| `index.html` | 338 | 页面壳：Emscripten `Module` 配置（`arguments:['image.bin','--romfs','romfs','--user','/user']`）、IndexedDB 流水线缓存读写、`?env=/?save=/?autolog=/?aspect=`、键盘/指针锁/Gamepad→XInput 轮询、AudioWorklet 音频环、`_web_resize/_web_present_hotkey` 导出、`rxFrame` 计数器 |
| `library.js` | 20 | `--js-library` 注入：`rx_web_canvas_init` 在 worker 里建 `OffscreenCanvas`（`specialHTMLTargets['#rx']`）、resize、`rx_web_present` 向主线程 postMessage `rxFrame` |
| `win32.c` | 871 | Win32 的 pthread 实现：Handle/Event/SRWLOCK/ConditionVariable、`CreateThread` 后"下一个线程接管 canvas"（:151-178）、时间（`emscripten_get_now`）、虚拟内存、**由 guest 写标记驱动的 `GetWriteWatch/ResetWriteWatch`**（:390-427）、HTTP Range 取 RomFS（:428-472）、`romfs.json` 清单（:473-533）、文件层（saves 在内存）、`waveOut` → 16 位立体声环形缓冲（:837-870） |
| `windows.h` | 337 | 浏览器版的 Win32 头：锁/条件变量/原子内联实现，宏去语义（`__stdcall`、`__declspec(x)` 等） |
| `display.c` | 43 | `web_mouse[3]`/`web_pad[9]`（页面原子写）、`web_resize`→`rx_resize`、`web_present_hotkey`→`rx_present_hotkey`、`XInputGetState` 读 `web_pad` |
| `movie_web.c` | 392 | 代替 Media Foundation：自带 EBML/WebM 容器读取 + 浏览器 **WebCodecs VideoDecoder**（经 JSPI）解出 NV12，注释说明 RomFS 走 HTTP 分块、电影 ~55 MB 而宿主堆 ~570 MB |
| `stubs.c` | 224 | `hos_prof.c`/`movie_mf.c` 的替身与少量 CRT：`hos_process_memory`（mallinfo）、`hos_backtrace` 空、`HOS_DUMP_AFTER`、每 2 s/10 s 的 RomFS/内存日志 |
| `intrin.h`/`shellapi.h`/`mmsystem.h`/`xinput.h` | 4/3/25/23 | 极小的头替身（`__rdtsc`、`ShellExecuteA`、`timeBeginPeriod`+waveOut 声明、XInput 结构） |

### 9.3 线程模型（两条）
- 默认：`-sPROXY_TO_PTHREAD` + 渲染线程持有 transferred canvas；GPU 对象属于创建它的 worker。
- `RX_WEB_MT=1`（`rx_web_mt.h/.cpp`，330 行）：**所有 WebGPU 调用集中在一个"WebGPU 线程"**，
  因为浏览器里 WebGPU 对象归属创建它的 worker。其它线程通过 `on_gpu()`（同步，等待）/ `post_to_gpu_thread()`（异步）
  把手上的调用投递过去；`rx_web_mt.h:66-152` 是一张由 `scripts/web_mt_macros.py` 生成的**逐 API 宏替换表**
  （`#define wgpuQueueSubmit(...) ::rx::on_gpu("wgpuQueueSubmit", …)`）。⚠️ 该生成脚本**不在仓库里**（`a64-kit/scripts` 不存在）。

### 9.4 Web 与原生路径的分叉点（shader 相关）
| 分叉 | 位置 |
|---|---|
| 着色器文本前加 `diagnostic(off, derivative_uniformity);`（浏览器 Tint 强制导数一致性，naga 不强制） | `rx_pipecache.cpp:352-356`；而 `wgsl_emit_context.cpp:135` 本来就无条件加了同一行（web 上是**重复添加**） |
| 片元阶段不开 pthread 并行翻译（web 里 pthread 要页面主线程） | `rx_shader.cpp:633-635` |
| 16 sampler 共享 + `#ifdef __EMSCRIPTEN__` | `wgsl_emit_context.cpp:301-303` |
| 模块/流水线缓存改走 IndexedDB（页面侧）+ `rxcache/` | `index.html:71-118`、`rx_shader.cpp:755` |
| 编译器清单：`--use-port=emdawnwebgpu`；`-mtail-call` 对渲染器/suyu **禁用** | `build.py:677`、`build.py:671-672`（"LLVM 的 wasm tail call 把 suyu 的 IR emitter 编错：

  return_call 的被调用者读了调用者已释放的栈临时量，着色器立即数变成别的操作数，2026-10-15"） |
| 参数求值顺序：wasm 是左→右，MSVC 是右→左，导致比较变成 `x > x`、常量错乱 | `emit_wgsl.cpp:59-64`（用 `std::tuple` 花括号强制顺序——**这是 WGSL 后端里唯一的 wasm 兼容性补丁**） |

### 9.5 `tools/serve_web.py`（221 行）的用途
开发用静态服务器：`python a64-kit/tools/serve_web.py <build>/<name> <romfs> [port]`。
1. 服务 `build/<name>/web`（页面/wasm/js）与 `build/<name>/image/image.bin`；
2. 发 **COOP/COEP/CORP** 头（`Cross-Origin-Opener-Policy: same-origin` 等，:101-103）——没有它 `SharedArrayBuffer` 不可用，
   pthread 与音频环都起不来（`index.html:50-57` 会显式提示）；
3. `/romfs/` 支持 **Range 请求** + 一年 immutable 缓存；`/romfs.json` 是完整清单（20.8 MB，brotli 后 2.3 MB）；
4. 对 `.wasm/.js/.bin/.json/.html` 后台 brotli 预压（`<file>.br`，:53-82）；
5. `/saves/<name>.json|/<name>/<rel>` 提供只读存档种子（页面复制进自己的 `/user`）；
6. `PUT /upload/<name>` 收集页面日志/截图（`?autolog=`）；`/stats` 按类型统计流量。
⇒ 它只是**开发调试基础设施**，不是发布方案（无鉴权、绑 127.0.0.1）。

### 9.6 当前可运行性评估
- 构建路径存在且相当完整（`cmd_web` 有 LTO、JSPI、mt 变体、brotli 服务、存档/日志回路），但**没有被验证**：
  根 `README.md:82` 明确"浏览器功能与打包不由根 `build.bat` 承诺或验证"；`README.md:98-99` 把"浏览器新游戏卡加载屏"
  与"插帧抖动"列为**早期实验中的问题、待复测**。
- `a64-kit/README.md:5-7` 的状态行停在 "2026-10-11 milestone 2 … nothing boots yet"，与根 README（可玩）严重滞后 ⇒ 文档漂移。
- 需要的外部条件：emsdk（`EMSDK` 或 PATH）、支持 WebGPU 的浏览器、交叉隔离的服务（`serve_web.py`）。
- 结论：【推断】web 目标属于"可构建、历史上跑起来过、当前未验证"的状态；不能按 README 的 Windows 路径假设其可用。

---

## 10. 工具与 CI

`a64-kit/gpu/tools/` 四个工具的输入都是 `HOS_SHADER_DUMP=<dir>` 的产物（`hos/gpu.c:436-472`：
`<dir>/<stage>_<gpu va>.bin`，从 program header 起 64 KB，stage 0/1=顶点、5=像素）：

| 工具 | 行 | 验证什么 | 怎么跑（从代码推断，无脚本） |
|---|---|---|---|
| `shader_test.cpp` | 157 | 前端+IR 是否能把 dump 翻完；`--glsl`/`--wgsl` 选后端（WGSL 需 `-DHAVE_WGSL`），`--print`/`--out <dir>` 输出源码。**唯一使用 GLSL 后端的地方**（`build.py:443` 注释"the shader tests' reference only"）。Windows 下带 `dbghelp` 崩溃栈回溯（:28-63） | 手工 `clang++ shader_test.cpp + gpu_sources()` |
| `wgsl_check.cpp` | 150 | **端到端**：dump → 前端 → WGSL → `wgpu-native` 真正编译（与运行时同一个编译器），逐条打印失败原因 | 需链接 `third_party/wgpu-native`；无构建脚本 |
| `bcn_test.cpp` | 39 | `rx_bcn.h` 的 BC1/BC4/BC4Snorm 解码（含 1×1×2 3D 切片与有符号位型），用 assert | 纯头文件，`clang++ bcn_test.cpp` 即可 |
| `feedback_test.cpp` | 352 | 原生 WebGPU 端到端回归：`rx_core.cpp` 的绘制前附件快照（framebuffer fetch 语义），自带 stub 的 `hos_*` 与 guest 内存 | 需 wgpu-native |

**CI 现状：没有。** 仓库里不存在 `.github/`（`ls .github` 空，`find` 无 `*.yml`），也没有 Makefile/CMake/ctest；
`git ls-files` 576 个文件，提交历史只有 6 个 commit（`4315342 Upload all files` 为初始导入 + 若干 PR merge）。
四个工具**没有被任何脚本引用**（`grep -rn shader_test\|wgsl_check\|bcn_test\|feedback_test` 只命中它们自身），
只能手工编译运行 ⇒ 它们更像作者的私人验证工具，而不是可持续的测试资产。
`rx_web_mt.h:6` 提到的 `scripts/web_mt_macros.py`、`build.py:488` 提到的 `scripts/sample_profile.py` 都**不在仓库**中（工具链缺口）。

---

## 11. 设计取舍与理由（从代码注释可读出的意图）

1. **为什么是 WGSL 而不是 SPIR-V/GLSL**：`README.md:82` ——同一套渲染器与着色器后端既能跑原生 wgpu-native，也能跑浏览器 WebGPU。
   WGSL 后端因此必须"两边都能用"，这解释了为什么它处处向后端的交集妥协（无 subgroup、无 swizzle sampler、
   常量缓冲 ub 化、几何/细分直接判死）。
2. **为什么把 GLSL 内建变成 `var<private>`**：`wgsl_emit_context.cpp:18-20` 说得直白——这样指令发射器写起来和 GLSL 后端一样，
   只在 `EntryPoint` 一处做内外映射，改造成本最低（也再次说明它是派生的）。
3. **为什么 ub 化常量缓冲**：WGSL 没有 GLSL 那种"任意 bind point 的 uniform block"语义，WebGPU 每 binding 一个资源、
   每 group 有数量上限；把整 stage 的 cbuf 塞进一个 `array<vec4<u32>>` + 动态 offset，能让**一次 bind group 复用整个 draw**，
   也避免 64 KB 上限之外还要多开 binding（超限的落回 storage）。
4. **为什么顶点取数要 pull**：WebGPU 顶点格式表是封闭集合，TOTK 的 9 个 vertex buffer + A2B10G10R10 无法表达；
   pull 只在必要时启用（最多 3 路），其余走原生 fetch，兼顾性能。
5. **为什么翻译要和 draw 解耦到 worker**：`rx_shader.cpp:776-778` ——在渲染线程翻译是加载后卡顿的主因；
   两个 worker + "跳过需要未就绪程序的 draw"（和 pipeline 编译未完成时一致）换取不阻塞。
6. **为什么把 WGSL 文本做成 interned 字符串**：`rx_internal.h:406-408` ——每对程序 ~15 KB × 7000 对会吃掉数百 MB；
   按 hash 去重后"6954 个模块只有 773 个不同文本"（`rx_shader.cpp:401-402`）。
7. **为什么有 `d16q_/opq_/rt_/` 这类怪函数**：全是为了让小 GPU 的浮点/深度语义与 Maxwell 对齐，
   每条都有对应的 TOTK 具体症状注释——这是本项目最有价值的"踩坑知识"，也是最难维护的部分。

---

## 12. 风险与脆弱点（按影响排序）

1. **契约双写**：uniform block 的布局在 `wgsl_emit_context.cpp:158-178` 与 `rx_draw.cpp:660-672`（以及 NVN 版
   `rx_draw.cpp:3175` 起）各写一遍，没有任何静态断言或共享常量。任何一侧改动（加一个 lane、改 0x1000 上限、改 cbuf 顺序）
   都会静默产生读错数据、且只表现为"某些场景画面错"。
2. **warp/subgroup 退化实现**：`emit_wgsl_warp.cpp` 的 ballot/vote/shuffle 全是不正确语义，`gl_SubGroupInvocationARB` 恒 0；
   任何 TOTK 依赖 warp 协作的 compute kernel 都会算错。目前没有保护（不抛异常、只是静默错）。
3. **断言被改成"打印后继续"**：`common/assert.h:17-24` 的 `ASSERT_MSG` 只打印前 3 次并继续；
   `decode.cpp:49` 的 `ASSERT_MSG(false, "Invalid insn ...")` 于是返回 `Opcode::NOP`。
   这正好对应 `README.md:90` 记录的"启动日志里出现三条 shader-decoder assertion"——**它们是静默的错误 shader，
   不是崩溃**，排障时极易被当成噪音。建议：这类路径至少 `abort`（或记入统计并标红该 draw）。
4. **静默空桩**：`SAM/RAM` 只 `LOG_WARNING("(STUBBED)")`，而 `LOG_WARNING` 在未定义 `GPU_SHADER_LOG` 时被编译掉
   （`common/logging.h:6-14`）⇒ 默认构建里"指令被丢弃"完全无声。`IMAD/IMUL/PRMT/LD/ST` 等 60 条缺失指令若出现则抛异常（可观测），
   但 `SAM/RAM` 这类不抛的更难查。
5. **WGSL 体积与编译时间**：所有变量都在函数顶部声明、无 SSA 形式、每模块 ~15 KB；
   `rx_shader.cpp:397-402` 记录"WGSL 解析/校验占启动 36 s / 42 s"。这是当前性能的主要结构性成本。
6. **Web 目标的编译器地雷**：已记录两起 wasm 特有误编译（`build.py:671-672` 的 tail-call、`emit_wgsl.cpp:59-64` 的求值顺序）。
   这意味着**任何对 `emit_wgsl.cpp` 里参数消费顺序的重构都可能只在浏览器错**，且没有 CI 会抓到。
7. **升级 suyu 的成本极高**：这棵树的 246 个文件里，`common/` 三个核心头被替身替换、`video_core` 被剪到只剩格式/解码、
   `frontend/ir/attribute.h` 被改（`IsLegacyAttribute` 搬家）、`backend/wgsl` 是与 GLSL 后端手工同步的平行实现、
   `Settings` 只剩 5 个字段。上游若改了 IR opcode 集合或 descriptor 结构，会同时命中
   `emit_wgsl_instructions.h`(753 行的逐指令签名)、`var_alloc`、`shader_info`、`texture_pass`、渲染器的
   `stage_resources`——即**四层同时改**。实际可行的做法是继续"抄上游单个修复"，而不是合并上游。
8. **缺失的工具脚本**：`scripts/web_mt_macros.py`（生成 152 行 API 宏表）与 `scripts/sample_profile.py` 不在仓库，
   前者意味着 web mt 路径的任何新增 WebGPU 调用**只能手改宏表**，且没人能复现生成过程。
9. **无 CI + 无自动测试**：四个工具手工编译；没有任何对"生成 WGSL 能否编译"的自动回归。
   `wgsl_check` 是唯一能覆盖这条链的工具，值得接进 CI（仅需 wgpu-native + 一批 dump 样本）。
10. **文档漂移**：`a64-kit/README.md:5-7` 的"nothing boots yet"与根 README 的可玩状态矛盾；两处都会误导后来者。

---

## 13. 可验证的证据清单（最小复现路径）

```bash
# 规模与许可证面
find a64-kit/gpu/third_party/suyu -type f | wc -l                       # 246
cat a64-kit/gpu/third_party/suyu/shader_recompiler/**/*.cpp | wc -l      # 分目录见 §1
grep -rh 'SPDX-License-Identifier' a64-kit/gpu/third_party/suyu | sort | uniq -c   # 232/61/1/1/1
grep -rn 'Rexcomp' a64-kit/gpu/third_party/suyu                          # 11 处显式标记
head -5 a64-kit/gpu/third_party/suyu/shader_recompiler/backend/wgsl/wgsl_emit_context.cpp   # GPL-2.0 + "derived from GLSL"

# 指令覆盖
grep -c '^INST' a64-kit/gpu/third_party/suyu/shader_recompiler/frontend/maxwell/maxwell.inc   # 280
grep -c 'ThrowNotImplemented(Opcode::' .../translate/impl/not_implemented.cpp                 # 60

# 契约
sed -n '158,190p' .../backend/wgsl/wgsl_emit_context.cpp                 # ub 布局
sed -n '660,700p' a64-kit/gpu/render/rx_draw.cpp                         # 渲染器侧同构布局
sed -n '39,50p'   a64-kit/gpu/third_party/suyu/common/assert.h           # 断言只打印前 3 次

# 构建与 web
grep -n 'def gpu_sources\|def cmd_native\|def cmd_web' a64-kit/build.py
sed -n '625,722p' a64-kit/build.py                                       # web 目标与链接参数
ls .github 2>/dev/null || echo 'no CI'
git show 552fa10 --stat                                                  # WGSL 后端在仓库内仍被改动
```

**报告边界**：未运行任何构建或游戏（无游戏数据、无 MSVC/emsdk），所有"可运行/不可运行"的结论都只基于源码与文档交叉验证，
并已用【推断】标注。渲染器内部细节（draw 批处理、pipeline cache 格式、附件快照）属于 `gpu-render` 的范围，本报告只在
与 WGSL 契约交界处引用。
