# 04 · WebGPU 渲染器（a64-kit/gpu/render/rx_*）

> 阅读范围：`rx.h`、`rx_internal.h`、`rx_core/draw/texture/texture_gpu/buffer/compute/shader/record/debug/pipecache/pipecache_native/present_fx/framegen/bcn.h/bc7/none/nvn.*/web_mt.*`，合计约 24k 行；另读上游 `hos/gpu.c`、`hos/nvn_native.c`、`hos/display.c`、`a64-kit/build.py` 的对接点。结论尽量带 `文件:行`；无法证实的标【推测】。
> 方法：先用 `rx.h` / `rx_internal.h` 建立接口与数据结构全貌，再按"入口 → 状态映射 → 资源 → pipeline → 批次 → 呈现"精读实现，最后 grep 反向调用点确认每个符号真的被用。

---

## 1. 一句话结论

这是一个 **"两层前端 + 一套共享下层"** 的手写 WebGPU 渲染器：前端把两套互不相同的图形 API 状态（模拟 Maxwell 寄存器 / 原生 NVN 对象）各自翻译成同一套 WebGPU 描述（pipeline key、bind group、render pass、draw）；下层是"每批次一个 encoder + 一次 submit"的批处理机，配 guest 内存镜像缓冲、按内容缓存的 bind group/pipeline，以及两条后台编译线程。它 **没有 BeginFrame/EndFrame**：帧边界由"提交(submit)"和"呈现(present)"两个外部事件定义。

---

## 2. 模块地图

| 文件 | 行 | 职责 |
|---|---|---|
| `rx.h` | 61 | 唯一 C ABI（`rx.h:16-57`）；文件头声明线程模型："每个入口都可能被任意线程调用，渲染器自己串行化"（`rx.h:6`） |
| `rx_internal.h` | 791 | 数据结构总纲 + 全部跨文件内部接口（见 §4） |
| `rx_core.cpp` | 4189 | device/surface、Image 与 render target、clear、批次/提交/回读、present、全部 `rx_*` C 入口、格式映射表 |
| `rx_draw.cpp` | 4026 | 状态→pipeline/绑定/draw；pipeline 编译池；bind group 缓存；`nvn_draw` |
| `rx_texture.cpp` | 2331 | TIC/TSC → TexInfo → 解码（block-linear/ASTC/BCn/packed16）、采样器、纹理绑定 memo |
| `rx_texture_gpu.cpp` | 1477 | compute shader 在 GPU 上做 ASTC 解码 / block-linear unswizzle |
| `rx_buffer.cpp` | 713 | buffer cache：guest 内存分段镜像 + 页级脏跟踪 |
| `rx_compute.cpp` | 568 | B1C0 launch（QMD）→ compute pass |
| `rx_shader.cpp` | 1548 | suyu 重编译器 + WGSL 后端；模块共享；翻译工作线程；NVN 程序变体 |
| `rx_record.cpp` | 753 | 延迟命令录制：encoder 调用记成 word 流，交提交线程回放 |
| `rx_pipecache*.cpp` | 486+491 | 管线缓存（原生 `rxcache/pipelines.bin` / 浏览器 IndexedDB） |
| `rx_present_fx.cpp` | 438 | 呈现矩形、FSR1（EASU+RCAS）、设置与热键 |
| `rx_framegen.cpp` | 577 | 光流插帧（自研，非 AMD FG） |
| `rx_debug.cpp` | 733 | 帧 dump、PNG、RenderDoc 集成 |
| `rx_nvn.cpp/.h` | 1389/235 | 原生 NVN 记录重放（默认路径） |
| `rx_bcn.h` / `rx_bc7.cpp` | 113/201 | BC1–BC5 CPU 解码、BC7 编码 |
| `rx_none.c` | 22 | 无 WebGPU 构建的空实现（§12） |
| `rx_web_mt.*` | 330/153 | 浏览器多线程胶水（T5 深读；此处只看接口） |

---

## 3. 两个上游入口（关键）

系统里同时存在 **两套图形 API 前端**，它们 **不共享状态结构**，只共享下层。开关在 `hos/nvn_native.c:2237`：`HOS_NVN_NATIVE` 默认 **1**，即原生 NVN 层是默认路径，Maxwell 模拟是回退路径。

### 3.1 入口 A：模拟 Maxwell B197 命令处理器（`HOS_NVN_NATIVE=0`）

- 上游 `hos/gpu.c` 跑 GPFIFO 命令流，把 3D 引擎寄存器写进 suyu 的 `Maxwell3D::Regs` 镜像；提交开始时先 `rx_begin_submission_written`（`hos/gpu.c:1041`，实现在 `rx_core.cpp:3908`），present 时调 `rx_present`（`hos/gpu.c:839,893`）。
- 只有一张 0x1000 项位图表里的方法会回调渲染器（`rx_core.cpp:909-923`，导出为 `rx_3d_methods` `rx_core.cpp:3653`）：24 个方法 + 5 个 stage × 8 个常量缓冲绑定写。**其余状态不追踪事件，在 draw 时直接从寄存器文件现读**（`rx_3d_method` 的 switch 只处理 clear/draw/index/barrier/render-enable/zpass，`rx_core.cpp:3682-3759`）。
- `0xFFFF` 表示 MME 宏结束，此时把累积的 instanced draw 发出去（`rx_core.cpp:3678-3680` → `draw_deferred` `rx_core.cpp:837`）。
- draw 装配是 suyu `DrawManager` 的复刻：`draw_begin/draw_end/draw_index_small/draw_array_instanced/push_inline_index`（`rx_core.cpp:871-907`）。
- 最终落到 `void draw(Channel&, const Regs&, const DrawParams&)`（`rx_draw.cpp:1790`）。

### 3.2 入口 B：原生 NVN 记录重放（默认）

- 上游 `hos/nvn_native.c` 把每个 NVN 调用写成 host 内存里的命令记录（设计说明见 `rx_nvn.h:1-12`；对象布局沿用 MIT 的 d2r-mobile NVN 层，见 NOTICE.md:13）。
- `rx_nvn.cpp` 在专用 "NVN replay" 线程上按提交顺序重放（`rx_nvn.cpp:1005-1013` 说明为何要独立线程：早先在游戏帧线程上重放是开放世界约 14 ms/帧）。`run_submit` 的顺序：等写扫描 → `rx_begin_submission_written` → `replay(...)` → `rx_flush`（`rx_nvn.cpp:1089-1118`）；present 就是 `rx_present`（`rx_nvn.cpp:1120-1125`）。
- 重放器把记录分成"状态类"（`NOP_BIND_VATTR/VSTREAM/VBUF/BUFS/TEX/SAMP/IMG`、`NOP_STENCIL/POLY_OFFSET/BLEND_COLOR/PRIMITIVE_RESTART/DEPTH_CLAMP/RASTER_DISCARD/RENDER_ENABLE/RENDER_COND/BARRIER`）与"工作类"（`clear_*`、`NOP_DRAW/DRAW_IDX/DISPATCH`、`copy_b2t/t2b/t2t`），switch 在 `rx_nvn.cpp:760-990`；未识别记录只打一次日志（`rx_nvn.cpp:991`）。
- 状态容器是 `NvnState`（`rx_internal.h:577-619`）：一整份"队列状态"（NVN 单队列、跨 submit 保持）——6 阶段 × 16 UBO/SSBO、6×128 纹理/采样器、8 个 target 的 blend、poly/ds/chmask/ms、16 个 vertex attribute + stream + buffer range、render target 集合、viewport/scissor/stencil/blend color，以及两个缓存代次 `prog_epoch` 与 `vtx_hash`。
- 最终落到 `void nvn_draw(NvnState&, const NvnDraw&)`（`rx_draw.cpp:3310`）。

### 3.3 共享 vs 独立（"是否同一套代码"）

**不统一**：路径 A 用 `Tegra::Engines::Maxwell3D::Regs`（`rx_internal.h:52`；寄存器偏移由 `RX_REG` = `offsetof/4` 从 suyu 头算出，`rx_internal.h:54`）；路径 B 从不构造 `Regs`，程序翻译也走另一入口（`nvn_translate` / `NvnEnvironment`，`rx_shader.cpp:1155`）。

| 维度 | A：Maxwell（`Regs`） | B：NVN（`NvnState`） |
|---|---|---|
| 状态→WGPU 枚举 | `compare_op/stencil_op/blend_op/blend_factor` `rx_draw.cpp:25-96` | `nvn_compare/nvn_stencil_op/nvn_blend_factor/nvn_blend_op` `rx_draw.cpp:3218-3277` |
| 附件 | `draw_attachments` `rx_core.cpp:941,973` | `nvn_attachments` `rx_core.cpp:3129` |
| 顶点属性/拉取 | `vertex_format/vertex_input/pull_plan` `rx_draw.cpp:99,1361`、`rx_shader.cpp:452` | `nvn_attribute/nvn_vertex_input/nvn_pull_plan` `rx_draw.cpp:2522,2693,2619` |
| 每阶段资源/布局 | `stage_resources` `rx_draw.cpp:641` | `nvn_stage_resources` `rx_draw.cpp:2986` |
| 程序/变体 | `programs_for_draw` `rx_shader.cpp:874` | `nvn_programs` `rx_shader.cpp:1354` |
| 纹理绑定 | `texture_for_handle` `rx_texture.cpp:1627` | `nvn_texture` `rx_texture.cpp:2262` |
| 视图矩阵/重构 | `view_transform` `rx_draw.cpp:1748` | 内联在 `nvn_draw` `rx_draw.cpp:3484-3498` |
| 一次性目标判定 | `one_off_targets` `rx_core.cpp:742` | `nvn_one_off` `rx_draw.cpp:3278` |

**共享的下半层**：`Stream/stream_alloc`、buffer cache（`guest_buffer`）、`draw_storage`、`bind_group` 缓存与 `shared_layout`、`get_pipeline` 与编译池、`render_pass/begin_pass/end_render_pass`、延迟录制、`snapshot_for_feedback`、target 回写、帧 dump、`ShaderModule/ensure_module`、原生管线缓存。两条路径的 draw 函数**逐行同构**（对比 `rx_draw.cpp:2119-2409` 与 `rx_draw.cpp:3562-3800`），差异只在"从哪个字段读状态"。这是本模块最重要的结构事实：**状态翻译是两份实现，机制只有一份**。

---

## 4. 数据结构总纲（`rx_internal.h`）

- **设备**：`Device`（`rx_internal.h:358-367`）= instance/adapter/device/queue + limits + 能力位（`bc, norm16, float32_filterable, depth32_stencil8, depth_clip_control, early_depth_test, timestamps`）+ 适配器描述串（进窗口标题，`rx_core.cpp:4132`）。
- **锁**：`RecursiveLock` 包装 Windows `SRWLOCK`，理由写在 `rx_internal.h:369-379`（"每个渲染器方法取一次锁，一次 draw 取好几次"），实现见 `rx_core.cpp:70-84`。除解码/翻译等后台线程外几乎所有入口都在 `mu` 下。
- **通道**：`Channel`（`rx_internal.h:170-184`）= 5×18 个 `ConstBuffer`（address/size/enabled）+ 正在装配的 draw（`draw_mode/draw_indexed/instance_count/inline_index/topology`）+ render enable 条件 `render_on` + `zpass` 遮挡估计。`DrawParams`（`:187-196`）是一次 draw 的最终参数。
- **附件**：`Attachments`（`:201-208`）= 8 个 color `Image*` + depth + 唯一尺寸；相等比较决定"是否复用当前 pass"（`rx_core.cpp:1562-1572`）。
- **每批次上传流**：`Stream`（`:273-284`）= 一个 GPU buffer + 本批次独占的 CPU 内存 + `used/capacity`；`alloc` 撞顶返回 `UINT64_MAX`，由 `stream_alloc` 触发 `flush_batch`（`rx_core.cpp:1462-1476`）。两个流：`stream_ro`（原生 96 MB：uniform/vertex/index/只读 storage）、`stream_rw`（32 MB：可写 storage）（`rx_core.cpp:502-504`）。
- **缓冲引用**：`BufferRef{buffer,offset,size}`（`:300-303`）= guest 区间在缓存里的落点。
- **图像**：`Image`（`:82-124`）是核心聚合体：身份 `id`、guest/gpu 地址、尺寸/层级、`pixel_format`（suyu 的）与 `format`（WebGPU 的）、`tex/attachment`、`rendered`（GPU 写过 → 内容只在纹理里）、`storage`（ASTC compute 解码产物）、回写元数据（`bl_height/bl_depth/reg_width/pitch_linear/storage_limit`）、`content_hash/decoded_seq/stale` 脏判定、`version/sub_versions/sub_addresses/sub_images`（由多个 render target 拼装的纹理）、`sampled_copy`（附件反馈快照）、`views`（视图缓存）。
- **着色器/程序**：`ShaderModule`（`:403-429`）持 `Shader::Info`（反射）、WGSL 文本（interned，`text_id` 全局唯一）、`text_hash`（会话间稳定的管线键成分）、`generic_loads()`（32 属性掩码，一次算好）、`creating` 互斥。`ProgramPair`（`:441-445`）= vs+fs 一起翻译（插值接口必须一致）。
- **拉取计划**：`PullPlan`（`:449-458`）描述"WebGPU 顶点取数表达不了、改由顶点着色器自读内存"的属性（最多 3 条流）。
- **compute launch**：`ComputeLaunch`（`:465-477`）。
- **纹理绑定**：`TexKind{Float,UnfilterableFloat,Depth,Uint,Sint}` + `BoundTexture`（`:485-496`，含 `slots[32]`：swizzle/border/compare/bias 打包进 uniform block）。
- **管线描述**：`PipelineDesc`（`:524-536`）= 管线键 + 两个 WGSL 文本哈希 + 打包布局 + 顶点缓冲/属性 + 颜色目标 + blend + primitive + depth-stencil；它同时就是**缓存文件记录格式**（§8）。
- **NVN 状态**：`NvnState`（`:577-619`，见 §3.2）。
- **统计**：`FrameSpike`（`:719-723`）记录慢帧归因（解码/等待/裁剪/上传字节/bind group/翻译/管线）。
- **延迟录制**：`Recording`（`rx_record.cpp:31-35`）= `words`（64 位字命令流）+ `pass_ends`（可切分点）+ `refs`（AddRef 保住的对象）；接口在 `rx_internal.h:741-787`。

---

## 5. 每帧流程（没有 BeginFrame/EndFrame）

帧边界由外部事件给出，`frame_number` 只在 present 里自增（`rx_core.cpp:3985,3988`）。三个真实阶段：

1. **提交(submission)**：`rx_begin_submission_written`（`rx_core.cpp:3908-3920`）→ `flush_batch()` → `buffer_cache_apply_written(pages)` / `buffer_cache_scan()`（CPU 写过的页作废，`rx_buffer.cpp:433-455`）。之后该提交的每条命令驱动 `rx_3d_method` 或 NVN `replay`。
2. **批次(batch)**：录制期只往 encoder/Recording 写命令；`flush_batch`（`rx_core.cpp:2045-2101`）切出 `Batch`（encoder 或 Recording + uploads + writebacks + 待释放对象 + afters），`push_batch`（`:1987`）交提交线程；`run_batch`（`:1781-1905`）在提交线程上：queue write 上传 → 视情况把 Recording 切段并行 finish（`:1793-1814`）→ `wgpuQueueSubmit` → 有回读则 `start_readback`（`:1681`）。
3. **呈现(present)**：`rx_present(guest,w,h)`（`rx_core.cpp:3942-4130`）。

`rx_present` 的实际顺序（最接近 "EndFrame+Present" 的东西）：

```
rx_present
 ├ flush_batch()                                     rx_core.cpp:3945
 ├ rdoc_present()（RenderDoc 捕获）                   :3947
 ├ watch_buffer_frame / frame dump 的 wait+dump       :3949-3954
 ├ sync_dispatch_writes(); install_decoded_textures() :3960-3961
 ├ native_cache_step()（管线预热推进）                 :3966
 ├ report_period()（RX_STATS 每 5 s）                 :3967,1288
 ├ frame_number++ / dump 触发                          :3978-3989
 ├ trim_decoded_textures / retire_unused_segments      :3990-3993
 │  / trim_pipelines / memory_report / spare 清理       :3993-3997
 ├ im = image_at(guest)（窗口缓冲对应哪张 RT）          :3998
 ├ screenshot / flash_probe                            :4111-4112
 ├ （无窗口：queue_present({}) 保持帧节奏）             :4113-4116
 └ queue_present([view,...]{ present_on_submit_thread }) :4128
        └ flush_batch(); Batch{after=fn, present=true} → 提交线程 :2203-2209
             └ present_on_submit_thread                        :2196
                  ├ F7 且 framegen_frame(...) 成功 → 交显示线程 :2198
                  └ surface_present(view, sw,sh, ww,wh, ...)     :2129
                        ├ 尺寸变了 → configure_surface            :2132-2133
                        ├ getCurrentTexture（失败→重配并放弃）      :2135-2144
                        ├ present_fx_draw(...)（FSR1/双线性）       :2149 → rx_present_fx.cpp:356
                        └ wgpuSurfacePresent + present_pacing()     :2188-2190
```

要点：present 本身也走"批次"通道（`queue_present` 先 flush 再推一个 `present=true` 的 Batch），因此 **呈现与命令提交同序**，且提交线程是唯一碰 surface 的线程（帧插值开启时换成显示线程）。

---

## 6. 资源层

### 6.1 上传流（每批次独占的 CPU 内存）
`take_stream_memory/give_stream_memory`（`rx_core.cpp:243-308`）用 `VirtualAlloc` 池化，10 s 闲页 `VirtualFree(MEM_DECOMMIT)`，因此"批次写自己的内存、提交线程上传"无需加锁。

### 6.2 buffer cache（guest 内存镜像）
`rx_buffer.cpp:1-14` 是设计说明：4 GB guest 空间切成 16 MB 段、每 8 MB 一个起点，保证"任意 ≤8 MB 区间落在一个 buffer 内"，于是顶点/索引/只读 storage **零拷贝直接绑定**。页级有效位（4 KB/位）+ `MEM_WRITE_WATCH`/`GetWriteWatch` 脏跟踪（`:346-430`），`A64_WRITE_TRACK` 变体用 `a64_dirty` 位图（`:348-372`）。GPU 写过的页带影子副本，支持"CPU 写与 GPU 结果按字合并"（`merge_cpu_write` `:126-167`）。段可退休（`:229-262`）。

### 6.3 纹理
- 描述：`tex_info(TICEntry)`（`rx_texture.cpp:167-218`）把 TIC 转成 `TexInfo`（格式、维度、层、mip、block 尺寸、tile_width_spacing、pitch/linear）。block-linear 的层大小/层偏移是 suyu 布局算术的移植（`:42-152`；`subresource_offset` `:429-440`；`guest_size` `:442-451`）。
- 解码：`decode()`（`:519-985`）先用 `conversion()`（`:222-261`）判断"原样上传 / CPU 转换 / 不支持"；再选路：ASTC 与 4–16 字节块纹理**倾向交给 GPU**（`astc_gpu`/`swizzle_gpu` 条件 `:563-568`，实现在 `rx_texture_gpu.cpp:1324,1371`），否则 CPU `UnswizzleTexture`（`:772`）+ ASTC 解压 + 按 layer 多线程并行（`:700-757`）。设备无 `TextureCompressionBC` 时 BCn 用 `rx_bcn.h` 软解（`:778-800`）。
- 缓存：解码结果按 TIC 内容哈希存 `decoded`，并用 1 MB 桶索引 `decoded_by_bucket` 让"写一页"只触碰相关纹理（`:317-338`）；`textures_written`（`:1514`）+ `sample_hash`（`:416-426`）做内容/序列脏判定；`trim_decoded_textures`（`:1446`）按预算回收。
- 由 render target 拼装的纹理（bloom 金字塔、立方体面、3D 切片）：`assemble_from_render_targets`（`:1322`）、`assemble_3d_slices`（`:1300`，3D 切片按 `nvn_slice_key` 归档，`rx_internal.h:140`）。
- 采样器：`sampler_for(TSCEntry, mode)`（`:1169-1202`）以 TSC 全文哈希缓存；`mode` 区分"按头过滤 / 强制非过滤 / 比较采样"。`kind_of`（`:1131-1143`）决定 bind group 的 sampleType（`UnfilterableFloat` 的三个来源：无 `float32_filterable` 的 32 位浮点、浏览器上的 16 位归一化）。
- 绑定 memo：`texture_for_handle`（`rx_texture.cpp:1627-1759`）是三层缓存（header 对前置缓存 + 8192 槽 memo + decoded 代次）；命中条件 = 同一 header 对、地址上的 image 未变、无解码/丢弃、GPU 映射未变、内存未被写。
- 占位纹理：`dummy()/make_dummy`（`:1204-1279`）按需造 1×1 的 2D/1D/3D/depth/uint/sint 变体，depth 用 render pass 清成 1.0。

### 6.4 bind group 布局推导
布局由**着色器反射**驱动（`stage_resources`，`rx_draw.cpp:641-1089`），且布局项被压成 32 位整数参与管线键（`pack_layout/unpack_layout` `:427-453`）：`binding(0-9) | visibility(10-11) | kind(12-13) | 类型相关位`。绑定顺序必须与 WGSL 后端一致（注释 `:473-476`）：

```
group0 (vertex)            group1 (fragment)
 binding 0 = uniform block（dynamic offset）
   布局：[rescale 4×vec4][每纹理 32B slots][装得下的 const buffer]
 binding 1.. = 其余 const buffer（read-only storage，按 batch 复制一次）
            = storage buffer（可写者进 stream_rw；只读者就地绑 buffer cache）
            = 每纹理两个绑定：texture + sampler（浏览器从第 17 个起复用 sampler）
```

特殊点：uniform block 有 `UbMemo` 复用（`:975-1030`，256 槽，按 info 指针/batch/内容比对）；可写 storage 每 batch 每区间只复制一次并共享（`rw_copies`，`draw_storage` `:503-639`）；对"本 pass 的像素着色器已写该区间"的只读读取会先 `end_render_pass("pe_storage_hazard")`（`:530-533`）保住顺序语义。bind group 缓存按"布局指针 + 全部 entry 内容"哈希（`bind_group` `:1264-1324`），前面还有 64 槽 last 缓存与 4096 槽 recent 缓存（注释：纯 map 查找的 cache miss 约 3%）。回收见 `trim_bind_groups`（`:1196-1262`）：只丢"键里含即将释放 view/buffer"的组，阈值 20000，丢的组延迟到回放后释放，避免 `BindGroup ... is no longer alive`（踩坑记录 `:1189-1191`）。

---

## 7. NVN/Maxwell → WebGPU 状态映射表在哪

**固定功能状态在 `rx_draw.cpp`，一张张 switch 表**（A 路径）：

| NVN/Maxwell 概念 | 映射函数/位置 | WebGPU 目标 |
|---|---|---|
| 深度/模板比较 | `compare_op` `rx_draw.cpp:25-37` | `WGPUCompareFunction` |
| 模板操作 | `stencil_op` `:39-51` | `WGPUStencilOperation` |
| blend 方程 | `blend_op` `:53-62` | `WGPUBlendOperation` |
| blend 因子 | `blend_factor` `:64-96`（双源 blending 降级并告警 `:81-93`） | `WGPUBlendFactor` |
| 顶点属性格式 | `vertex_format` `:99-154`（+ `decode_attribute` `:169-230` 处理 WebGPU 没有的打包格式；`fetchable` `:236-246` 判定能否硬件取数） | `WGPUVertexFormat` |
| primitive/光栅 | `draw()` `:2119-2150` | topology/frontFace/cullMode/unclippedDepth |
| depth-stencil | `draw()` `:2151-2194` | `WGPUDepthStencilState`（含 depthBias、模板 front/back；`normalize` 把"无写掩码的模板操作"清成 Keep，`:2180-2189`） |
| 颜色目标/blend/write mask | `draw()` `:2195-2260` | `WGPUColorTargetState` 数组 |
| viewport/scissor | `view_transform` `:1748-1784` + `:2301-2321` | viewport 恒为整个附件（`:2358` 设 `0,0,w,h`），矩形限制交给 scissor |
| 渲染使能/条件 | `render_enabled` `rx_core.cpp:1061-1080`（条件模式读 GPU 释放的 64 位值） | 跳过 draw/clear |
| 拓扑扩展 | `expand_topology` `rx_draw.cpp:1553-1591`（Quads/QuadStrip/Fan/Polygon/LineLoop 用索引展开） | `WGPUPrimitiveTopology` |

**纹理/渲染目标格式映射（三张表串起来用）**：

| 层 | 位置 | 说明 |
|---|---|---|
| NVN format → suyu `PixelFormat` | `rx_nvn.cpp:52` 起（`nvn_pixel_format`） | B 路径专用：R/RG/RGBA 8/16/32、sRGB、D16/D24/D32F/D24S8、RGB565/BGR565/RGB10A2/R11G11B10F/RGB9E5F、DXT1/3/5… |
| Maxwell RT format → `PixelFormat` | `rx_core.cpp:517,540`（suyu `PixelFormatFromRenderTargetFormat/FromDepthFormat`） | A 路径 |
| `PixelFormat` → `WGPUTextureFormat` | `rx_core.cpp:2657-2744`（`wgpu_format`） | **唯一大表**：无等价者降级（16 位打包→RGBA8、R32G32B32F→RGBA32Float、无 BC 特性时 BC→RGBA8、ASTC→RGBA8）；`dev.norm16` 决定 16 位 unorm/snorm 用真格式还是 `RGBA16Float` |
| sRGB 双视图 | `srgb_pair` `rx_core.cpp:2620-2636` | 创建时带 `viewFormats=pair`（`rx_core.cpp:2597-2601`、`rx_texture.cpp:607-611`），让"按 sRGB 写、按原始读"是同一块内存 |
| depth/stencil 判定 | `is_depth_format/has_stencil` `rx_core.cpp:2638-2655` | |

---

## 8. 着色器、pipeline 与管线缓存

- **翻译**：`translate()`（`rx_shader.cpp:206-233`）用 `GuestEnvironment`（`:57-124`，从 guest 内存读程序/常量缓冲/纹理头）跑 suyu 的 `Maxwell::TranslateProgram` → IR → `Shader::Backend::WGSL::EmitWGSL`（`build_pair` `:624-774`）。vs/fs **必须成对翻译**（插值接口一致，`:1-4`），`parallel` 时把 fs 翻译与 emit 各丢 `std::async`（`:635-705`）。
- **程序键**：`stage_hash`（`:252-296`，0x50+0x400 字节，先 memcmp 再哈希；注释记录了"按地址+页写缓存"失败过）；`pair_key`（`:588-618`）再混入"顶点属性类型/unpack、拉取计划、颜色输出类型"。命中靠 `programs_for_draw` 的 2048 槽 memo（`:874-946`，用页写序列号免 memcmp，注释：memcmp 曾占渲染线程 3%）。
- **模块共享**：`ensure_module`（`:396-431`）按 interned text id 共享 `WGPUShaderModule`；注释给实测（初始空岛：6954 个模块 → 773 个不同文本），故"每个不同文本只解析一次"。
- **管线键**：`get_pipeline`（`rx_draw.cpp:1607-1691`）用 `hash_bytes(key)` 查 `pipelines`；键顺序：程序文本哈希（会话间稳定 → 可磁盘缓存）→ 每目标 format/writeMask/是否 blend（+blend 6 字段）→ 顶点输入 key → 哨兵 `0xDDDD` + primitive → 哨兵 `0xEEEE` + 两阶段布局哈希（`draw` `:2201-2278`）。
- **编译并发**：`PipelineWorkers`（`:369-415`）默认 **2** 线程（注释：6 个时在初始空岛数秒内全部死锁，2026-10-13）。未编译完的管线让该 draw 被 `skip("pipeline compiling")`（`:2299`）；只有 `draw_must_run`（一次性目标，`rx_core.cpp:742`）才就地偷活并最多等 5 s（`:2283-2298`）。浏览器走 `wgpuDeviceCreateRenderPipelineAsync`（`:328-355`）。
- **翻译并发**：2 个 `translation_worker`（`rx_shader.cpp:789-834`），未翻译好的程序先跳过 draw（`:858-868`）。这两处"先跳过、下帧再来"就是 **"卡顿但不出错"** 的核心取舍（README:56 的"首次遇到场景卡顿"即此）。
- **原生管线缓存**（`rx_pipecache_native.cpp`）：
  - 文件 `<exe>/rxcache/pipelines.bin`（`:63-71`）。
  - 头 12 字节：`kMagic="RXPN"(0x4E505852)`、`kVersion=1`、`signature()`（`:37-43`，由若干 WebGPU 结构体的 `sizeof` 线性组合 → 换 wgpu 头或记录格式即自动重建）。
  - 记录流 `{char type; uint32 len; payload}`（`add_record` `:229-234`）。类型：`'T'`（WGSL 文本：`u64 hash + bytes`）、`'P'`（管线，字段级序列化 `put_desc` `:93-150` / `get_desc` `:152-227`）、`'C'`（compute 程序 WGSL）、`'U'`（本会话用到的管线键：`u32 session + u64[]`）。
  - 只回放"最近 N 个会话用过"的管线（默认 4，`:297-305`），因为每条约 390 KB 宿主内存、3783 条曾达 1.2 GB（`:11-14`）；单次启动最多造 6000 条（`:453`）；文件超 400 MB 直接重建（`:316-322`）；每 5 s 追加保存（`:437-440`）。
  - compute 程序的模块由**读文件线程**直接建（`:326-342`），替渲染线程省下首次 dispatch 的 WGSL 解析（约 270 ms）。
  - 浏览器版（`rx_pipecache.cpp`）同构但 `kMagic="RXPC"`、路径 `/rxcache/pipelines.bin` 与 `/rxcache/seed.bin`（IndexedDB，`:1-9,26-37`），并按偏移增量读回以省 JS 堆（`:68-84`）。

---

## 9. 呈现链：缩放、FSR1、插帧、热键

- **矩形/letterbox**：`present_rect`（`rx_present_fx.cpp:339-354`）取"保持帧宽高比的最大居中矩形"；`stretch` 或（ultrawide 且 `hos_view_aspect>0`）时铺满。
- **FSR1**：`present_fx_draw`（`:356-400`）。条件是 `fsr && (r.w>sw || r.h>sh)`（只有放大才用 FSR）。两趟全屏三角形：
  1. **EASU** 画进 `st.mid`（RGBA8Unorm，`ensure_mid` `:286-302`），参数 `con0={sw/rw, sh/rh, 0.5*sw/rw-0.5, 0.5*sh/rh-0.5}`（`:386-389`），WGSL 在 `:125-178`（12 tap、方向/边长加权、钳到 4 邻域防振铃）；
  2. **RCAS** 画进窗口，`rcas.x=exp2(-sharpness)`（`:394-399`），WGSL 在 `:181-195`（5 tap、lobe 限幅）。
  非放大走 `bilinear`（`:198-204`）。每趟都 `SetViewport+SetScissorRect` 到目标矩形（`draw_pass` `:305-336`），所以 letterbox 黑边由 clear 得到。
- **插帧(F7)**：`rx_framegen.cpp`。WGSL 在 `:33-145`，调度在 `framegen_frame` `:470-576`。链路：`copy`（存当前帧副本）→ `luma`（1/4 尺寸亮度，4×4 盒滤波）→ `flow`（16×16 块匹配，搜索 ±8 个 1/4 纹素 = ±32 像素，SAD + 0.04×|v| 惩罚）→ `median`（3×3 中值）→ `synth`（按 t 双向采样混合）。多帧用 `RX_FRAMEGEN_MULT`（默认 2，上限 4）。调度：显示线程 `display_main`（`:351-396`）按"最近帧间隔滑动平均"均匀摊开"做的帧 + 真帧"，下一真帧先到就丢弃剩余（`:384-389`）。限制写在 `:10-12`：无运动向量→图像法光流；UI/快速运动拖影；>100 ms 帧间隔不插值（`:491`）；"显示滞后约一帧"。
- **热键**（两处）：
  - 键位捕获/分发在 `hos/display.c:199-211`：F11 全屏（`:201`）；`VK_F6..VK_F10` 映射成 6..10 调 `rx_present_hotkey(key)`（`:202-203`）；F6 额外 `rx_resize` 重新取 aspect（`:204-208`）；结果写窗口标题 4 s（`:209-210`）。浏览器另有 `web_present_hotkey`（`hos/web/display.c:21`）。
  - 语义在 `rx_present_fx.cpp:413-438`：6=ultrawide、7=framegen、8=FSR、9=锐度循环（`kSharpness{0,0.25,0.5,1,2}` `:39`）、10=fit/stretch；改完 `save_settings`。
  - **持久化** `<user>/present.txt`（`settings_path()` `:47-49`），键 `fsr/sharpness/stretch/framegen/ultrawide`（`:74-80`）；读取顺序"环境变量默认 → 文件覆盖"（`:50-73`），玩家选择优先。`ultrawide` 影响 `hos_view_aspect` 并调 `hos_ultrawide_apply()`（`:404-408`）。

---

## 10. 同步与多线程模型

| 线程 | 谁起 | 职责 |
|---|---|---|
| 游戏/解析线程 | 游戏 | 调 C 入口；`rx_3d_method` / NVN `replay` 在 `mu` 下录制 |
| `GPU commands` | `hos/gpu.c:822-850` | 顺序跑提交与 present（A 路径） |
| `NVN replay` | `rx_nvn.cpp:1134` | 重放 NVN 记录；B 路径的"渲染线程" |
| `NVN write scan` | `rx_nvn.cpp:1052` | 提前 `GetWriteWatch`（约 2 ms，省 replay 线程 11%，`:1041-1043`） |
| `GPU submit` | `rx_core.cpp:1936` | finish + 上传 + submit + present + 帧数节流 |
| `GPU finish` ×(N-1) | `rx_core.cpp:1755` | 并行回放/结束大 batch 的各段（默认 3 个 encoder，`RX_FINISH_THREADS`） |
| `pipeline compiler` ×2 | `rx_draw.cpp:380-388` | WGSL→module→pipeline 编译 |
| translation worker ×2 | `rx_shader.cpp:824` | Maxwell 程序翻译 + WGSL 生成 |
| `rx display` | `rx_framegen.cpp:485` | 插帧的定时呈现 |
| `flash probe` | `rx_core.cpp:2322` | `RX_FLASH_LOG` 的异步帧回读异常检测 |

同步机制：递归 SRW 锁 `mu`；三级水位 `batch_serial / batch_serial_run / gpu_done_serial`（`rx_core.cpp:1627-1629`）；`serial_index[1024]` 记每批最后一次 submission index，`wait_batch` 用 `wgpuDevicePoll(wait=true,&index)` 精确等待（`:3612-3641`）。背压三处：`submit_queue` <64 批且 `presents_queued` <2（`push_batch` `:2003`）；每帧 present 后 `throttle_gpu_frames` 保证 GPU 不落后 3 帧（`:1912-1934`，注释：不设限时"已提交内存每分钟涨 2–3 GB"）。GPU→CPU 回读：`writeback_after_batch` 记区间 → 批次后 `start_readback` 拷 staging 并 `mapAsync` → `apply_readback` 只把**变化的字**写回 guest（`:1654-1722`）。需要 CPU 立刻看到 GPU 写入时（间接 draw 参数等）走 `rx_gpu_writes_pending` / `rx_sync_gpu_writes`（`:3795-3863`，会 flush+等 GPU，是明确同步点）。

---

## 11. wgpu-native 初始化与 backend 选择

`init_device`（`rx_core.cpp:323-511`）：

1. instance：原生用 `WGPUInstanceExtras` 指定 `backends`——`RX_BACKEND=vulkan/dx12/gl`，缺省 `WGPUInstanceBackend_Primary`（`:342-347`）；`RX_VALIDATION` 加 Validation|Debug（`:348`）。浏览器用 `#screen` canvas 造 surface（`:331-338`），原生用 HWND（`:356-364`）。
2. adapter：`featureLevel=Core`、`HighPerformance`、绑 surface；回调 + 最多 5000 次 `ProcessEvents` 自旋（`:367-383`）。
3. 可选特性"有就请求"（`:392-420`）：BC 压缩、Float32Filterable、Depth32FloatStencil8、RG11B10UfloatRenderable、DepthClipControl、IndirectFirstInstance、Float32Blendable、TextureFormatsTier1、DualSourceBlending、ClipDistances，加 wgpu 扩展 `TextureFormat16bitNorm(0x7FFF0001)`、`ShaderEarlyDepthTest`（仅原生）。
4. limits：**直接照抄适配器上限**（`wgpuAdapterGetLimits` → `requiredLimits`，`:421-428`）并打日志（`:447-456`）；`RX_SSBO_ALIGN` 可造出浏览器同款 256 对齐做本地验证（`:452-454`）。
5. surface：优先 `BGRA8Unorm`/`RGBA8Unorm`（"窗口缓冲存编码值，不能是 sRGB"，`:469-474`）；present mode 优先 Mailbox → Immediate → Fifo（理由：游戏自己在 nn_vi 缓冲队列里配速，呈现不该等 vblank，`:475-489`）。
6. 之后 `create_blit()`（无 FSR/调试时的直拷贝管线 `:162-232`）、两个 stream、`pipecache_start()`（浏览器）、`native_cache_start()`（原生）、`astc_gpu_on()`（ASTC compute 管线也在此建好，因为 error scope 属 device `:501-509`）。无窗口走 headless 分支（`:458-466`）。

---

## 12. 覆盖度：实现了什么、降级了什么、桩了什么

**已实现（有代码 + 注释里的实测）**：双前端；最多 8 颜色 target + depth/stencil；deferred clear 折叠进 loadOp（`rx_core.cpp:561-628`）；部分 clear（scissor/分量/stencil 位）用三角形模拟并缓存管线（`:2752-2848`）；附件反馈（采样正在写的 RT）用 pre-draw 快照（`:2472-2522`，按 texture barrier epoch 复用）；compute（QMD 解析 `rx_compute.cpp:22-46`）；buffer cache；ASTC/BCn/packed16 解码与 GPU 侧解码；target 回写 guest（`writeback_targets` `:3457-3577`）；FSR1；插帧；帧 dump/PNG；RenderDoc；两级管线缓存。

**明确降级或拒绝（带日志与开关）**：

| 项 | 位置 | 行为 |
|---|---|---|
| 双顶点程序(VertexA)、tess/geometry | `rx_shader.cpp:837-845` | 不画，打一次日志 |
| 双源 blending | `rx_draw.cpp:81-93` | 降级成第一源 |
| images / texture buffer | `rx_draw.cpp:774-779`、`rx_compute.cpp:94-98` | 整 draw/dispatch 放弃 |
| 格式无 WebGPU 等价 | `rx_core.cpp:2576-2578`、`rx_texture.cpp:521-524` | 丢弃 + 日志 |
| 16 位归一化 RT | `rx_core.cpp:2565-2575` | 无 `norm16` 时用 `R16Float` 等承载，像素着色器自己 clamp（`rx_draw.cpp:2039-2060`） |
| 16 位深度 | `rx_core.cpp:642-647` + `rx_draw.cpp:2036-2038` | 量化到 16 位台阶(d16q)使 EQUAL 测试仍成立 |
| 各 target 尺寸必须一致 | `rx_core.cpp:994-1017` | 尺寸不符的目标从 pass 摘掉 |
| 顶点缓冲数上限(浏览器 8，TOTK 用 9) | `rx_shader.cpp:472-500` | 多出的流改"顶点着色器自取" |
| 浏览器 sampler 上限 16 / 不可过滤纹理 | `rx_draw.cpp:782-787,957-961`、`rx_texture.cpp:1137-1142` | 复用 sampler / 强制非过滤 sampler |
| 浏览器禁止重叠可写 storage | `rx_compute.cpp:57-88` | 绑定别名 + 程序变体 |
| 浏览器 sRGB 与 storage 冲突 | `rx_texture.cpp:1559` | 该视图砍掉 storage usage |
| 仅单采样（`sampleCount=1`） | `rx_core.cpp:2596`、`rx_draw.cpp:325` | 无 MSAA 路径 |

**`rx_none.c`（无渲染器构建）**：`a64-kit/build.py:520-526` 在没有 `--wgpu`/`WGPU_NATIVE` 时改链 `rx_none.c`（`build.py:526`，命令行选项 `build.py:783`）。它把 `rx.h` 每个符号实现成空操作，`rx_active()` 返回 0、`rx_describe()` 返回 `off`（`rx_none.c:1-22`）。于是 `hos/display.c` 走"自己画窗口缓冲"的分支（`hos/display.c:155-159` 的 `renderer: off` 标题），`hos/gpu.c` 里 `rx_methods` 为 NULL 即跳过全部渲染调用，程序仍能启动/加载 RomFS（与 README:57 的说明一致）。运行时同效开关 `RX_OFF=1`（`rx_core.cpp:3303`）。

---

## 13. 性能瓶颈与脆弱点

**结构性的每帧/每 draw 成本**

1. **每 draw 重建 uniform block 与常量缓冲副本**：`stage_resources` 在 `stream_ro` 里为每 draw 分配 uniform block（`:975-1030` 的 UbMemo 只是缓解），放不进 block 的 cbuf 每 batch 复制一次（`:680-713`），只读 SSBO 走"就地绑/否则复制"（`draw_storage` `:534-639`）。
2. **每 draw 一次 hash+memcmp 找 bind group / pipeline**，缓存到 20000 组就要整表裁剪（`rx_draw.cpp:1196-1262`；注释：旧实现"每约 3 s 一次 25 ms 卡顿"）。
3. **同步等待**：`writeback_targets` 在原生路径逐目标 `wait_submitted` + `while(!done) wgpuDevicePoll`（`rx_core.cpp:3461,3569`，注释"城堡处约 35 次/秒，每次都停下回放"）；`rx_sync_gpu_writes`（`:3828-3863`，注释：TOTK 每帧 invalidate，逐次同步曾把帧率砍半，见 `rx_nvn.cpp:1385`）；`throttle_gpu_frames`（`:1912`）。
4. **`GetWriteWatch` 扫描**：`rx_buffer.cpp:405-420` 有 >20 ms 慢扫描告警；`rx_nvn.cpp:1041-1043` 记约 2 ms/次、占 replay 线程 11%。
5. **encoder finish 与 submit**：注释量化"wgpu 在 finish 时把 encoder 转成 Vulkan 命令，占 GPU 线程约 1/3 时间"（`rx_core.cpp:1574-1576`）；单线程时"雨天开放世界 9 ms/帧"（`:1724-1727`），故有 RX_FINISH_THREADS=3 分段并行 + `recording_parts` 在 pass 边界均分（`rx_record.cpp:146-169`）。
6. **pass 切换**：`RX_PASS_SPLIT` 默认 256 draw 断一次 pass（`rx_core.cpp:1507-1524`）；NVN barrier 默认**不**断 pass（`rx_nvn.cpp:876-887`，注释：每帧约 97 次断 pass 的代价）。
7. **首次遇到即跳过**：pipeline/翻译未就绪 → draw 被跳过（§8），表现为"进新区域/首帧掉帧"而非错误。
8. **内存**：管线每条约 390 KB、文件可能 1.2 GB（`rx_pipecache_native.cpp:11-14`）；段 buffer 16 MB/段（`rx_buffer.cpp:37-38`）；decoded 纹理按预算回收（`rx_texture.cpp:1446`）。

**脆弱点（改这里要小心）**

- **强耦合 suyu 内部结构**：`Maxwell3D::Regs` 字段偏移（`RX_REG`=`offsetof/4` `rx_internal.h:52-54`）、`TICEntry/TSCEntry` 布局、`Shader::Info` 反射字段名——换 suyu 版本会静默错位。
- **句柄复用与生命周期**：已释放 view/bind group/pipeline 必须延后释放（`retire_view`/`release_*_later`，`rx_internal.h:125-134`），注释记录了 `BindGroup ... is no longer alive` 崩溃（`rx_draw.cpp:1189-1191`）。
- **job 里的指针回填**：`PipelineJob.buffers[i].attributes` 指向 `job->attributes[i].data()`（`rx_draw.cpp:304-309`），job 必须在 finish 前存活（靠"取出后同步 finish"保证）。
- **静态/thread_local 缓存遍地**：`draw_attachments`、`programs_for_draw`、`texture_for_handle`、`bind_group`、`PassState` 都是 `static`——多线程下靠 `mu` 串行才安全（契约就是 `rx.h:6`）。
- **`rx_begin_submission` 已是死代码**（只剩 `rx_begin_submission_written` 被调用，`rx_core.cpp:3787`；全仓 grep 只有定义与 `rx_none.c` 的桩）。
- **环境开关极多**（`RX_*` 数百个，`env_flag` 每处 `static` 只读一次，`rx_core.cpp:1255-1282`）：调试友好，但"同一份代码多种行为"，回归时必须记清用了哪些开关。

**与 README 已知问题的对应**

- **阴影/着色器问题（README:90）**：`rx_shader.cpp:563-584` 专门留了 `RX_DEBUG_SHADOWS_LIT`——把 WGSL 里所有 `textureSampleCompare(Level)` 包成恒等 1，说明作者当时也在用"让比较采样恒亮"定位阴影问题，即该问题在渲染器侧**未闭环**。README 的"启动时三条 shader-decoder 断言"应在 suyu 前端【推测】（rx_* 内只有 `rx_nvn.h` 的尺寸 `_Static_assert`）。
- **地形拉伸（README:91）/错误地形纹理（README:92）**：与"RT 尺寸裁剪"和"由 render target 拼装纹理"的启发性逻辑相关——`rt_image` 只在"差 <64/8 像素时裁到 clip"（`rx_core.cpp:520-525`）；`assemble_from_render_targets` 若 `sub_versions` 判定漏更新会取到旧子资源（`rx_texture.cpp:1322-1402`）；`texture_for_handle` 的 memo 命中条件极多，漏一个就沿用过期内容（`rx_texture.cpp:1701-1722`）。【推测：这类 bug 大概率出在这两处判定】
- **武器轮盘背景变黑（README:93）**：背景是"模糊的" → 需要 bloom/mip 金字塔被正确拼装；`rx_texture.cpp:1803-1804` 的注释正好记过同类症状（"只取 level 0 → 模糊层缺失、粒子变方块"）。【推测】
- **F6 ultrawide 拉伸而非加宽（README:76,94）**：`present_window_size` 只在 `ultrawide && aspect>16:9` 时把 aspect 交给游戏（`rx_present_fx.cpp:404-408`），而 `present_rect` 在 ultrawide 下**强制 stretch**（`:342`）——两者同时生效时"看起来就是被拉伸"。这是与 README 描述最吻合、最可验证的点。
- **融合武器图标黑（README:97）**：与 `trim_decoded_textures` + `texture_for_handle` memo 的时序有关；`rx_texture.cpp:1739-1742` 就是"解码未完成先给占位、且不记忆"的处理。

---

## 14. 可验证的证据（如何复核）

1. **后端与特性**：窗口标题来自 `rx_describe`（`rx_core.cpp:4132`，返回 `<adapter> (<backend>)`）；启动日志 `rx: WebGPU on ... surface %ux%u format ... present mode ...`（`:508-509`）。
2. **每帧预算**：`RX_STATS=1` 每 5 s 打印 fps、draws/帧、t_draw/t_flush/t_present/t_translate/t_decode/t_pipeline、bind group 数、上传 MB，以及提交线程的 encoder 数/耗时（`rx_core.cpp:1288-1335`）。
3. **慢帧归因**：`HOS_FRAME_HIST` 打印 >18 ms 的帧及 decode/wait/trim/upload/bind group/翻译/管线明细（`:3928-3940`）。
4. **队列水位**：`RX_LOG_GPU_AHEAD=1` 打印提交线程等待与"落后几帧"（`:1924-1932`）。
5. **画面对不对**：`RX_DUMP_FRAME=<n>` + `RX_DUMP_DIR` 出一帧的 pass/draw/RT(PNG) 轨迹（`rx_debug.cpp:484-536,588` 起）；`RX_SCREENSHOT=<dir>` 每 5 s 一帧 PNG（`rx_core.cpp:1337` 起）。
6. **管线缓存效果**：`rxcache/pipelines.bin` 存在并增长；日志 `rx: pipeline cache: N shader texts, M pipelines to make in the background, K compute programs`（`rx_pipecache_native.cpp:323-324`）与"made in the background in X s"（`:442-445`）；`RX_NO_PIPELINE_CACHE=1` 做 A/B。
7. **两条前端对照**：`HOS_NVN_NATIVE=0` 切回 Maxwell 模拟路径（`hos/nvn_native.c:2237`），对比同场景 draw 数与画面，即可检验"两份状态前端"的等价性——这是当前最值得做的正确性实验。

---

## 15. 三条跨范围假设的核对（T1/T3 提出）

**① "SP store 不标脏页 → GPU 不读 guest 栈"：假设需改写，但暴露了另一个真问题。**
本地内存（CRS 栈）在本实现里**根本不落在 guest 内存**：WGSL 后端把程序头的 local memory 声明成着色器私有数组 `var lmem: array<u32, N>`（`a64-kit/gpu/third_party/suyu/shader_recompiler/backend/wgsl/emit_wgsl.cpp:535-536`），尺寸来自程序头 `sph.LocalMemorySize()+shader_local_memory_crs_size`（`rx_shader.cpp:66,96-98`）。所以"栈写入是否标脏页"不成立——不需要标脏，也不会被 GPU 读到。**但**：图形阶段 `SharedMemorySize()` 恒返回 0（`rx_shader.cpp:99-101`），只有 compute 才报 `shared_size`（`rx_shader.cpp:166-168`，取自 QMD `q[17]&0x3FFFF`，`rx_compute.cpp:32`）。即 Maxwell **图形**程序里的 shared memory / `barrier` 语义没有承载物【推测：若 TOTK 的后处理/阴影/粒子程序用了 shared memory，会静默算错且无日志】。compute 侧有上限钳制（`rx_shader.cpp:1047-1051`：shared ≤64 KB、local ≤1 MB，超限告警）。

**② "异步重放落后 1 帧且顶点/索引不快照"：基本成立，代码自己写明了窗口。**
- 落后量：`RX_NVN_FRAMES_AHEAD` 默认 1（`rx_nvn.cpp:1030-1034`），注释直说"游戏最多比重放快一帧"（`:1006-1012`）。
- 不快照：顶点/索引在**重放时刻**从活内存读——索引 `gpu_read`（`rx_draw.cpp:1945`）、缓存模式绑 `guest_buffer`（`rx_draw.cpp:1930,3400`），后者需要时直接从 `hos_guest_base()` 上传（`rx_buffer.cpp:334`）。被快照的只有**命令记录本身**（`rx_nvn.h:7` 的 "recording copies"）。
- 保护到什么程度：fence 在"重放走过"就放行，而不是 GPU 跑完（`rx_nvn.cpp:1275-1295`；要等 GPU 需 `RX_NVN_GPU_FENCES=1` 走 `wait_batch`）。因此"游戏在 fence 后复用内存"是安全的（重放已读过），但"提交之后、重放之前有人写这块内存"会被读到新内容：写扫描在 push 时取页（`hos/gpu.c:862-869`），注释自承"submit 与 scan 之间写的页会被提前看到（无害）"。**结论：不是每帧必错，而是存在一个"读到的不是提交时刻内容"的窗口**，现实路径是"非提交线程的写入 + buffer-cache 就地绑定"（stream 路径在上传时即固化）。

**③ "RENDER_COND 在重放时刻读计数器"：成立，且是作者已知问题（有专门诊断开关）。**
`NOP_RENDER_COND` 在重放时读 `r.addr` 与 `+16` 两个 64 位值再比较（`rx_nvn.cpp:853-856`）；若游戏在这之后才写计数器，条件会算错。为此留了 `RX_NVN_COND_RECHECK=1`：present 时重读并对比，命中就打印 "`read X at the replay, Y at the present (written after it)`"（`rx_nvn.cpp:632-651,1120-1123`）。**这条日志就是该假设的现成验证手段**：开开关跑一段，有输出即证明时序敏感。对照 A 路径的等价逻辑 `render_enabled`（`rx_core.cpp:1061-1080`，在 mode/override 被写时求值一次，`rx_core.cpp:3692-3694`）——两侧都是"早求值 + 事后不可纠正"，只是 A 路径没有 recheck 诊断。
