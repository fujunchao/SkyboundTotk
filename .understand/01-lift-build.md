# 01 · Lift / 构建流水线：Switch AArch64 机器码 → Windows x64 exe

范围：`build_game.py`、`build.bat`、`a64-kit/build.py`、`a64-kit/lift/*`、`a64-kit/tools/{nso,nx,survey,toolchain}.py`、
`a64-kit/hos/nx_image.py`、`a64-kit/rt/{a64.h,a64_rt.c}`、`ports/zelda-totk/*`，以及为交代契约而读的 `hos/{hos.h,hos_main.c,hos_svc.c,hos_hostcall.c,hos_prof.c}`。
纯静态只读（无游戏数据、未运行构建）；标【推测】处是从代码反推的意图。

## 0. 结论速览

1. 不是模拟器：**构建期**把 `main`+`sdk` 全部 AArch64 代码翻成 C（文档记录 ~28 万函数 / 1.1 GB C，`a64-kit/README.md:70`），
   本地 clang 编成 exe。运行期无解释器、无 JIT、无页表——guest 内存是**一块扁平 4 GB 窗口**（`rt/a64.h:53-65`）。
2. 静态重编译的三件事分居三处：`hos/nx_image.py`（**我们是 rtld**：布局+重定位+符号绑定）、`lift/a64_lift.py`（函数边界与控制流）、
   `lift/a64emit.dll`（单条指令 → C，来自 suyu，GPL）。
3. 未实现的系统 API 不做二进制桩，而是**在构建期改调用目标**：导入槽被绑到 16 字节 trampoline 地址 → 提升成 `f_XXXXXXXX` thunk →
   尾调用 `imp_<mangled>`（HLE 或自动生成的日志桩）。
4. 复用判定是**全有或全无**的 SHA-256（`build_game.py:68-85`）：lift/hos/rt 源码、`build.py`、`nso.py`、`hot_funcs.txt` 或 ExeFS
   模块字节任一变化，都要求整份 `build/` 挪开重来；而 `native` 阶段是**逐目标文件增量**（`build.py:463-474`）。
5. 性能靠三处“离线事实”：`hot_funcs.txt`（400 函数，`code_hot.c` + FP 快路径内联）、`link_order.txt`（`/ORDER:@`）、以及每个
   间接调用点的内联缓存 + 可选单态去虚化。

## 1. 全景：五个阶段

```
game/exefs/{rtld,main,subsdk0..9,sdk}
  │ (A) tools/nso.py       NSO 解析：三段 LZ4 / MOD0 / dynamic / 符号 / 重定位 / init_array / eh_frame_hdr
  ▼
  Nso{image, segments, symbols, relocs, init_array, eh_funcs}
  │ (B) hos/nx_image.py    "we are rtld"：定址 + 重定位 + 绑定 + entry 集合
  ▼
build/totk/image/{image.bin, image.json, mod_patches.txt}
  │ (C) lift/a64_lift.py + a64emit.dll
  ▼
build/totk/gen/{code_NNN.c, a64_table.c, a64_hooks.c, a64_fpx.h, a64_suyu.{h,c}, a64_stubs.c, hos_image.c}
  │ (D) build.py native    clang -O3 + rt/ + hos/ + gpu/render + opus  →  build/totk/native/totk.exe
  ▼ (E) build_game.py package
dist/SkyboundTOTK/{SkyboundTOTK.exe, wgpu_native.dll, SDL2.dll, image.bin, image.json, romfs/}
```

| 阶段 | 命令 | 产物 | 参数 |
|---|---|---|---|
| A–C | `build.py lift totk --exefs game/exefs --skip subsdk0 --build build` | `image/`+`gen/` | `--skip/--hle/--comments/--mods/--hot/--max-func`（`build.py:758-770`） |
| D | `build.py native totk --build build --wgpu third_party/wgpu-native` | `totk.exe`+dll | `--opt`(默认 O3)/`--debug`/`-j`/`--wgpu`/`-o`（`build.py:776-784`） |
| — | `build.py stubs` / `web` / `run` | 只重写桩 / wasm 版 / 启动 | `build.py:625-739`（web 不在本报告范围） |

入口链：`build.bat:10` → `build_game.py:main` → `check_game`（`27-39`，验 `NSO0` 魔数、`main`/`sdk` build ID、两个 RomFS 目录）→
`check_tools`（`42-60`，**先编 `a64emit.dll`**，免得耗时 lift 完才发现没编译器）→ lift 或复用 → native → package（`88-105`）。

## 2. Stage 细节

### 2.1 `cmd_lift`（`build.py:365-413`）

1. `hle = {n[4:] for n in implemented()}`（`:372`）：扫 `hos/*.c` 的 `HLE(name)` 与 `void imp_x(Ctx`（`:154-162`）得“已有 C 实现的符号”。
2. `stubbed`（`:378-380`）：`main` 中**未定义**、名字形如 `nn::<OS 名字空间>::f`、且是函数的导入，减去已有 HLE。`OS_NAMESPACES` 共 32 个
   （audio/hid/oe/prepo/nfp/time/account/vi/swkbd/pl/err/codec/diag/…/ro，`:189-191`）——即“要说 IPC 的”那一圈；libc、`nn::util`、
   `nn::image`、`nn::mem`、std 是纯计算，**作为 lifted 代码跑**（`:186-188`）。NVN/`nv::` 故意不在内：真 NVN 驱动是被提升的 SDK 代码，
   只换 `NvOsDrv*`/`nvnBootstrapLoader`（`hos/nvdrv.c:579-606`、`hos/nvn_trace.c:1-19`）。
3. `NxImage(exefs, skip, hle|stubbed)`（`:381`，见 §4）；`mod_patches`（`:283-362`）把 `.pchtxt/.ips` 补丁在 lift 前写进镜像。
4. `check_hle`（`:217-225`）：每个 `imp_` 必须对应某模块定义/导入的符号，拼错直接 fail（防“HLE 没接上、真函数照跑”的静默错误）。
5. `img.write` → `image.bin/image.json`；`write_image_c` → `gen/hos_image.c`（§4.3）。
6. `Lifter.discover(max_insns=--max-func)`；删旧 `code_*.c`，并**删 `gen/entry_hooks.json`/`splices.json`**（`:400-404`，注释记录了 2026-10-20 的
   bug：留着状态文件会让“已应用”判定成立，所有 hook/splice 丢失）。
7. `L.write`、`a64_rtgen.write`、`gen_stubs`、`L.report()`；写 `indirect_br.txt`/`jump_tables_missing.txt` 两份覆盖率诊断。

### 2.2 `cmd_native`（`build.py:477-622`）

- 两组源码：`gen/*.c`（含 `a64_table/a64_hooks/a64_stubs/a64_suyu/hos_image`）用 `-O3 -g0 …`；`rt/*.c`+`hos/*.c` 固定 `-O2 -g`（`:539-541`）。
  增量依赖哈希不同：`headers_key(RT,gen)` vs `headers_key(RT,HOS,gen,gpu/render)`（`:518-519`）。
- **先** `apply_entry_hooks(gen,img_dir)`+`apply_splices(gen)`（`:516-517`）：ENTRY_HOOK/SPLICE 在 native/web 阶段才写进 lifted C，
  所以改一个 hook 只重编一个 `code_NNN.c`；两者都用状态文件比对，只在集合变化时改写。
- 渲染器（给了 `--wgpu`）：suyu shader_recompiler（去 glsl 后端）+ 4 个 video_core 文件 + `gpu/render/*.cpp`，clang++
  `-std=c++20 -O2 -DNDEBUG -flto=thin`（`:437-450, 555-568`）；否则编 `rx_none.c` 空壳。opus 单列 `-O2 -g0`（`:546-554`）。
- 任务按源文件大小**降序**提交（`:569`）+ 线程池；每文件先算 `sha1(源码+flags+依赖头哈希)`，命中 `.o.sha1` 则跳过（`:463-474`）。
- 链接：`@link.rsp` + `/STACK:268435456 /SUBSYSTEM:CONSOLE` + `/ORDER:@link_order.txt /IGNORE:4037` + 10 个 Windows 库 + wgpu
  `.dll.lib`（`:581-621`）；`wgpu_native.dll`/`SDL2.dll` 只在大小不同时复制（运行中的 exe 会锁住它们）。

## 3. `lift_identity`：何时可复用上一次 lift

`build_game.py:68-85` 依次哈希：① `a64-kit/{lift,hos,rt}` 下**所有** `.py/.c/.cpp/.h`（含 `suyu/arm64_to_c.h`、`hos/nx_image.py`）；
② `a64-kit/build.py`、`tools/nso.py`、`ports/zelda-totk/hot_funcs.txt`；③ exefs 里 `rtld,main,sdk,subsdk1..9` 的**字节内容**
（`subsdk0` 不在内——它被 `--skip`，即“只对真正参与的输入负责”）。

复用需同时满足（`:113-122`）：`image.json` 在、`gen/a64_suyu.h` 在、至少一个 `code_*.c`（= lift 没中途死掉）、
`build/totk/lift-input.sha256` 等于当前 identity、`image.json` 里 `main`/`sdk` build_id 仍等于 `EXPECTED_IDS`。
任一不满足且 `build/` 非空 → **直接报错要人挪开**（`:124-125`），没有增量重 lift。

- 好处：绝不会把“半套旧 C + 半套新镜像”链进 exe（静态重编译最灾难的错误）。
- 代价：**过度失效**——`hos/*.c` 改一行 HLE 函数体（不影响任何被提升的代码）也要整份重 lift；真正必需的是 `implemented()` 的**名字集合**、
  `ENTRY_HOOK/SPLICE` 宏、`lift/` 与 `rt/` 的头。反向倒是正确的：`link_order.txt`、`tools/{toolchain,nx,survey}.py`、`gpu/**`、
  `third_party/**` 不在哈希里（它们不影响 lift 产物）。

## 4. `image.bin` / `image.json`

### 4.1 NSO 解析（`tools/nso.py:35-121`）

`flags@0xC` 决定三段（`.text/.rodata/.data`）是否 LZ4 块压缩；`csize@0x60` 给压缩尺寸；`bss@0x3C`；`build_id@0x40..0x60`。
每段解压后断言长度 = memory size，按 memory offset 铺进 `bytearray`。`.text+4` → MOD0（相对自身给出 dynamic/bss/eh_frame_hdr/
module object）；解析 `DT_*`（`nso.py:24-27`）、符号表、`RELA`+`JMPREL`（`RELATIVE/ABS64/GLOB_DAT/JUMP_SLOT`，`:28`）、`INIT_ARRAY`、
`.eh_frame_hdr` 二分表（要求 `ver==1,tbl_enc==0x3B,cnt_enc==0x03`，`:108-121`）——这张表给出**每个有 unwind 信息的函数起点**，
是 §6.4 判函数边界的基础。模块顺序固定：`MODULE_ORDER=[rtld,main,subsdk0..9,sdk]`（`:32`）。

### 4.2 我们是 rtld（`hos/nx_image.py`）

- 定址（`:29-31,43-49`）：`MAIN_BASE=0x08000000`，模块间 1 MB 对齐顺序排布；`TRAMP_BASE=0x07000000`，每个落空导入 16 字节槽。
  整块镜像 = `bytearray(end - MAIN_BASE)`（含各模块 bss 零区，`nso.py:78-79` 已把 image 扩到 bss 尾）。
- 重定位（`:95-106`）：`RELATIVE → base+addend`；`ABS64/GLOB_DAT/JUMP_SLOT → resolve(sym)`。**这就是 PLT/GOT 被“收紧”的时刻**：
  lift 期 `plt_target` 读到的 GOT 槽里已是最终 guest 地址。
- 绑定（`:52-93`）：按模块序建全局 `exports`。`hle` 里且被某模块**定义**的符号 → 在其定义地址放 hook（于是“游戏导入它”与
  “SDK 内部互调它”都进同一个 host 函数）；未定义且 weak → 0（同 rtld 语义）；其余未解析 → trampoline。
- entry 候选（`:125-152`）：模块代码段起点、`DT_INIT/DT_FINI`、全部 eh_frame 起点、导出函数、`init_array` 落在代码段的项、
  `RELATIVE/ABS64` 结果落在代码段的、全部 **BL 目标**。**故意不收 ADR/ADRP+ADD 目标**：TOTK 有 3,735 个这样的目标几乎全是
  jump table 基址标签，收进来会把函数切两半、两半互走（14.4M → 12.6M 指令，`:139-141`、`a64-kit/README.md:78-79`）。
- 写出（`:169-186`）：`image.bin`=`0x08000000` 起、**已重定位**的完整镜像；`image.json`=元数据
  `{base,end,tramp_base,modules[{name,base,size,build_id,init,fini,segments,bss}],skipped,hooks[{addr,c,sym}],unresolved,init_arrays,exports}`
  （`exports` 只收函数）。

**两者分工（容易看错）**：`image.bin` 是**运行期载荷**（`hos_main.c:197-215` 读进 guest 窗口并校验
`IMAGE_BASE+n == hos_image_end`）；`image.json` 是**构建期元数据**，只被 `build.py`（ENTRY_HOOK_SYM 查 exports、stubs 查 hooks）与
`build_game.py`（查 build_id）读。`image.json` **没有** entry 集合——entry 由 lift 时存活的 `NxImage` 对象直接带进 Lifter。

### 4.3 `gen/hos_image.c`（`build.py:255-280`）

从 `main.npdm` 的 META 头取主线程栈(`+0x1C`)、优先级(`+0xE`)、core(`+0xF`)；写出 `hos_image_end`、每模块 `DT_INIT` 与名字、
`nninitStartup/nnMain/_init_libc0/1/2/nnosInitialize/nvnBootstrapLoaderInternal` 地址，以及线程 start/exit hook。
后两者的找法很“逆向”（`:228-252`）：全模块扫 `mrs x1,tpidrro_el0; add x1,x1,#0x1f8; str x0,[x1]` 三条字
（`0xD53BD061/0x9107E021/0xF9000020`，`:236-239`），再解后续 ADRP+LDR 读 GOT 槽，取 nnmusl `__nnmusl_init_dso` 填的 per-thread
TLS hook。**这是硬编码字节序列依赖**（§13）。`hos_main.c:232-250` 用这些常量复刻 `nn::init::Start` 的顺序
（`_init_libc0 → nnosInitialize → _init_libc1 → nninitStartup → _init_libc2 → 各模块 DT_INIT → nnMain`）。

## 5. import/导出符号如何落到 `hos/` 的 C 实现（谁生成 stub）

| 情况 | 结果 |
|---|---|
| `hos/` 有 `HLE(name)`（`:18`）/`imp_name` | 进 `hle`；被某模块**定义** → 在定义地址挂 hook；纯导入 → 绑到 trampoline，trampoline 地址同样是 hook |
| 32 个 OS-facing 名字空间内的导入、但无实现 | 进 `stubbed` → 同样被 hook → `gen_stubs` 生成日志桩 |
| 其它未解析（非 weak） | 也给 trampoline → hook；`c_ident` 把 mangled 名清洗成 `imp_…` |

钩子落点（`a64_lift.py:1069-1072,1327-1339`）：入口名恒为 `f_<addr>`；对 hook 地址，`gen/a64_hooks.c` 生成
`void f_XXXXXXXX(Ctx*c){ TAILCALL(imp_<name>); }`。提升器 `discover` **跳过** hook 地址（`a64_lift.py:594-597`），但 `a64_table.c`
仍把 `f_<hook>` 放进函数表——所以**虚调用/间接调用打到被 HLE 的 SDK 函数时也进 HLE**。

stub（`build.py:174-183`）：

```c
/* nn::os::SleepThread() */
void imp__ZN2nn2os11SleepThreadEv(Ctx *A64_NOESC c) { static int seen; a64_unimplemented(c, "_ZN2nn2os11SleepThreadEv", &seen); }
```

桩体是 weak 的 `a64_unimplemented`（`rt/a64_rt.c:220-226`）：**只打一次**日志（带 lr）并把 `x0` 置 0。设计声明即 `build.py:10-11`：
“每个没有 HLE 的导入都拿到 stub，于是游戏第一天就能链接，日志告诉你下一步实现什么”。新增 HLE 后跑 `build.py stubs` 即可
（C 名 `imp_<mangled>` 不变，`:729-731`），**不需要重 lift**。

## 6. `a64_lift.py`：lifting 算法

### 6.1 模型（`:1-18`）
每个 guest 函数 = `void f_XXXXXXXX(Ctx *__restrict A64_NOESC c)`；“函数” = 一个 entry 加上**不跨越其它 entry** 可达的代码。
任意地址可作 entry（多收只花体积、不损正确性），跨 entry 的边/落穿变**尾调用**（因此 shared tails 让指令计数超过真实代码量）。
映射：`B/B.cond/CBZ/TBZ→goto`、`BL→写 LR 的 C 调用`、`BLR→A64_ICALL`、`RET→return`、`BR→switch`（有表）或 `A64_IJUMP`（无表）、
`SVC→a64_svc`、`BRK/HLT/UDF/译不了→a64_trap`。

### 6.2 分类（`:38-62`）
`classify()` 用位掩码判 10 类：`b/bl`、`b.cond`、`cbz`、`tbz`、`br`、`blr`、`ret`、`svc`、`trap`(BRK/HLT)、`isb`、`ic ivau`；其余交 `a64emit`。

### 6.3 函数体切分（`:659-695`）
从 entry 直线推进，遇 entry/代码段外即停；`b` 收边停；条件跳收边继走；`br` 先试 `jump_table`（成功则收全部目标为边）否则停；
`ret/trap` 停。`discover`（`:593-617`）走一遍全部 entry 后做**跳转表第二遍**（“A 表是从另一个 switch 的边找到的”，`:599-610`）。
`split_large`（`:619-657`）用差分数组找“无分支跨越”的切点，每 `max_insns/2` 一刀；TOTK 上只找到 60 个，所以默认 `--max-func 0`。

### 6.4 跳转表识别（`:698-858`，本文件最精巧处）
在 `br` 前 32 条指令窗口内做**符号执行**：ADRP/ADR→常数，ADD #imm / ADD Xm,LSL→加，`LDR*` 寄存器偏移→`("ld",基址,索引,size,signed,shift)`，
其它写 `Xd` 的指令把该寄存器置未知；同时旁路跟踪**索引上界**（`CMP/CCMP #imm`→`bounds`，随后 `B.HI/B.HS` 给出 `count`；
`AND #(2^k-1)`、`MOV Wd,Wm` 传播上界）。目标 = `base + (entry<<k)`，表项 `b/h/i/q`（有符号再加 `B/H/I/Q`）。

三层兜底（注释 `:700-706` 逐条列了 TOTK 真实形状）：表地址被提到窗口外（循环不变量）→ `hoisted_constant` 按地址序回扫，或
`function_constant` 要求“全函数只给该寄存器赋过一个 ADRP+ADD 常数”；表地址被 spill/reload → `reaching_constants`（`:897-954`）
沿 CFG 反向走，跟随 `MOV`、`STR/LDR [sp,#off]`、`LDP/STP` 槽位；同一寄存器在不同路径装**不同表**→ 取可达常数并集（`jt_multi_table`）。
`br xN` 指向代码常数（`adr x19,label … br x19`）→ 单目标（`:784-789`）；否则退回 `adr_targets`（全函数所有 `adr` 候选，`:991-1009`），
switch 恒留 default 兜底。目标合法性靠 `function_extent`（`:1040-1045`，用 eh_frame 起点二分）+ `img.in_code`；有上界则严格读，
越界判“读到函数外”并退化为“读到条目离开函数为止”（`:845-857`）。诊断计数器落到 `jump_tables_missing.txt`。

### 6.5 PLT/GOT（`:1048-1067`）
识别 `adrp x16; ldr x17,[x16,#off]; add x16,x16,#off2; br x17` 四指令桩，按**已重定位**的 GOT 槽取值，若值是 entry 就记
`plt[stub]=(target,slot)`（`callee` 最多追 4 层）。于是 `bl plt` 直接变成对真目标的调用；间接跳进 PLT 桩时
（`emit_function:1164-1169`）还先把 `x16/x17` 填成硬件上的样子再尾调用。

### 6.6 生成的 C 形状（`emit_function`，`:1156-1255`）

```c
void f_08704b7c(Ctx *__restrict A64_NOESC c) {
    L_08704b7c:;
    if ((c->c && !c->z)) { goto L_08704bb0; }
    c->x[30] = 0x8704b90ULL; f_0869bc0c(c);
    { uint64_t _t = c->x[9]; c->x[30] = 0x8704bd4ULL; A64_ICALL(c, _t); }
    switch (c->x[8]) { case 0x8704c00ULL: goto L_08704c00; default: A64_IJUMP(c, c->x[8]); return; }
    return;
}
```

标签只在有边/entry 处发；条件直接用 `COND[]`（`:28-30`，读 `c->n/z/c/v`）；`cbz/tbz` 对 `rt==31` 特判常量 0；`blr` 若该点有
`A64_DEVIRT` 档案则先比单态目标否则 `A64_ICALL`（`:1218-1227`）；落穿目标非下一条时补 `transfer`。`--comments` 加 `/* pc: word */` 与符号名。

### 6.7 逐指令翻译的 ctypes 协议
`lift/a64emit.cpp` 是 suyu `arm64_to_c.h` 的薄壳：`a64_config(translate_all,fpx)`（`:22-30`）设 `g_translate_all=1`（连 suyu 交给 JIT
的形态也静态翻，我们没有 JIT）、`g_emit_fpx=1`（FP 走 FPX1 快路径+精确回退）、关掉 fastmem/guard/block-chain。
`a64_translate_batch(insns,pcs,n,out,cap,offs,flags)`（`:35-48`）批量翻、返回所需字节数（cap 不足调用方加大重试）；`flags` 两位：
bit0 落进 `recomp_unhandled`、bit1 `Translate` 关闭了块（终止符）。`a64emit.py:31-52` 用 `ctypes.CDLL`（先经 `build_emit.build()`，按
源码 sha1 缓存）分批（默认 65536）调用，用 `offs` 切出每指令 C 文本（latin-1 解码）。`runtime_text(0..3)`（`a64emit.cpp:50-60`）取
suyu 的 runtime 头/C、FP estimate、FPX 精确体，供 `a64_rtgen.py` 落盘。
**分工**：`a64emit` 只管“不结束基本块的单条指令”，函数/控制流/调用/跳表/svc 全在 Python；所以若某指令返回 TERMINATOR 就
`SystemExit`（`a64_lift.py:1188-1189`）——那是分类表漏了形态的硬错误。

### 6.8 指令文本的六道后处理（`:1268`；同一 word 只翻一次，`:1259-1266`）
1. `outline_fpx`（`:77-85`）：把内联的 FPX1 守卫收敛成 `a64_fpx_<fast>_<exact>(c,…)` 一次调用（`a64_fpx.h` 每形态一个 helper）。
   理由：TOTK 有 87k 个守卫，光它们就让 clang -O1 在 20 个函数上跑 >10 分钟（`:69-71`）。
2. `vectorize_fpx/vectorize_fcmp`（`:126-142,370-386`）：2/4 lane FADD/FSUB/FMUL、FCMGE/FCMGT/FCMEQ、标量 FCMP 变成一次 SSE + “每 lane 都
   满足精确性前提”的检查，任一 lane 不满足就整段走原精确块（`a64_vfpx_*/a64_vfcmp`）。
3. `widen_vstores`（`:154-167`）：整个 Q 寄存器的写合成一次 16 字节 store（空 asm 阻止窄化），避免 store-forwarding stall。
4. `pack_flags`（`:251-261`，需 `A64_PACKED_FLAGS=1`）：四个 NZCV 字节合成一次——**实测更慢，默认关**（72.5→71.1 fps）。
5. `drop_flag_stores`（`:1086-1154`）：函数内 per-instruction 活跃性（调用杀 4 flag、离开函数的边读 4 flag），删掉没人读的 flag store。
6. `stack_stores/frame_stores`（`:173-206`）：以 SP(`x[31]`)/帧指针(`x29`) 为基址的 store 改 `_sp` 变体**不标脏页**（GPU 不读 guest 栈；
   占 TOTK store 的 1/4 与 88k 处，实测 66.9→67.4 fps、exe −2 MB）。

## 7. 生成物清单与规模

| 文件 | 内容 | 生成者 |
|---|---|---|
| `gen/code_NNN.c` | 提升函数；每文件累积 ~`CHUNK_BYTES=24 MB` 文本就切（`:31,1275-1285`）；头只有 `#include "a64.h"` + 被调用者 extern 声明 | `Lifter.write` |
| `gen/code_hot.c` | `hot_funcs.txt` 里的函数单独成文件（`A64_HOT_CHUNK=1`），可给不同编译参数 | `:1288-1317` |
| `gen/a64_table.c` | `a64_fn_count/a64_fn_addr[]`(升序)`/a64_fn_ptr[]` —— `a64_lookup` 的二分表 | `_write_table` `:1327-1334` |
| `gen/a64_hooks.c` | 每个 hook 地址一个 `f_XXXXXXXX` thunk = `TAILCALL(imp_…)` | `:1335-1339` |
| `gen/a64_fpx.h` | 只含本次用到的 (fast,exact) helper + SSE 向量化 + `a64_set_nzcv/a64_vst*` | `fpx_header` `:541-571` |
| `gen/a64_suyu.{h,c}` | suyu runtime“去宿主桥”版：删 `RecompHostMem`、`g_module_base`、save/load 原型与布局 pin，全 context 形参加 `A64_NOESC`；C 侧含 FPX1 精确体、AES S 盒、SMULH/UMULH | `a64_rtgen.py:27-62` |
| `gen/a64_stubs.c` | 未实现 import 的日志桩 | `gen_stubs` `build.py:174-183` |
| `gen/hos_image.c` | 启动常量（§4.3） | `write_image_c` |
| `gen/functions.txt` | `<addr> <指令数>` 每函数一行 | `:1322-1324` |
| `gen/indirect_br.txt` / `jump_tables_missing.txt` | 仍是间接跳转的 BR / 没找到表的 BR | `build.py:410-413` |
| `gen/entry_hooks.json` / `splices.json` | hook/splice“已应用”状态（lift 后删除以强制重放） | `build.py:63-151` |

**`ports/zelda-totk/hot_funcs.txt`**：402 行 = 2 行注释 + 400 个 `f_<addr> <samples>`，来自两次 `HOS_PROFILE_PCS` 城堡跑分
（文件头自述“lifted-code 时间的 75%”）。`build.py lift` 默认传给 `A64_HOT_FUNCS`（`build.py:368-370`），`hot_functions()` 只取第一列，
用 `HOT_FPX` 把这些函数里的 `a64_fpx_*` 换成 `a64_fpx_hot_*`（快路径 inline、慢路径仍调用，`:509-520,1296-1298`）。动机：不内联则每次
浮点运算都要 spill/reload guest 寄存器；全内联则 exe +40% 且更慢。它**属于 lift 输入**，被 identity 覆盖。

**`ports/zelda-totk/link_order.txt`**：4,694 行 `f_<addr>`（无注释），供 `-Wl,/ORDER:@… /IGNORE:4037`（`build.py:599-609`）配合
`-ffunction-sections`（每函数独立 section）让热函数共页/共 cache line；注释记录“游戏线程每帧 −1.4% CPU（3 轮，2026-10-22）”。
注意注释写 2,966 个函数而文件现在 4,694 行（profile 更新过，注释没跟）。它**只影响链接**，不在 lift identity 内。

## 8. 编译与链接参数（及理由）

lifted 组（`build.py:500,537-545`）：`--target=x86_64-pc-windows-msvc -fms-extensions -D_CRT_SECURE_NO_WARNINGS -I gen -I rt
-Wno-everything -fno-strict-aliasing -mcx16 -DA64_WRITE_TRACK -O3 -g0 -mllvm -rotation-max-header-size=0 -ffunction-sections
-mllvm -capture-tracking-max-uses-to-explore=5000`。

- **默认 `-O3`**（`build.py:777-780`；`a64-kit/README.md:27` 写的 `-O1` 已过时）：O1→O2 雨天 58.8→60 fps；O2→O3（ThinLTO 链接也 O3）
  城堡 66.7→69.7 fps（3 轮交错），exe +11 MB。`--opt O1` 保留给“想快点出包”。
- **关 LoopRotate**（`build.py:530-531`；`a64-kit/README.md:82`）：LoopRotate 复制循环头，在 goto 密集的巨型函数上占 306 s 里的 120 s，
  把可复制头大小上限设 0 后降到 197 s。
- **`-capture-tracking-max-uses-to-explore=5000`**（`:453-460`）+ `A64_NOESC`（`rt/a64.h:32-45`）：LLVM 只看指针前 100 次使用判逃逸，
  超了就假设“任何 guest store 都可能改 context”；hot 城堡函数用了 ~4200 次，5000 覆盖它（context 载入 535→280）；再往上
  （100000）会让 `code_004` 496→1625 s。
- **`-mcx16`**：让 `unsigned __int128` 的 `__atomic_compare_exchange` 内联成 `cmpxchg16b`（`rt/a64_rt.c:286-289` 的 128 位 STXP）
  【推测：这就是该 flag 的用途】。**`-fno-strict-aliasing`**：lifted 代码经 `uint8_t*`/`uint64_t*` 混访 guest 内存。
- **`-DA64_WRITE_TRACK`**（`:505-510`）：guest store 自标脏页，替代 Windows `GetWriteWatch`（对 GPU 看到的 ~2 GB 扫 20-50 ms，
  游戏在 fence 等 → 大天空岛走路每几秒 ~55 ms 卡顿；改后 ~0.08 ms）。`RX_SOFT_WRITE_WATCH=0` 回退。
- runtime 组固定 `-O2 -g`；渲染器 `-std=c++20 -O2 -g -DNDEBUG -flto=thin`，wasm 上排除 `-mtail-call`（注释：2026-10-15 wasm tail call
  曾把 suyu IR emitter 编错，shader 立即数变成别的操作数）。
- 链接（`:619-621`）：`-fuse-ld=lld -B<lld-link 目录>`（`tools/toolchain.py:29-44`：把 `lld` 复制成 `lld-link.exe` 才会选 COFF 驱动；
  clang 对 MSVC target 忽略 `--ld-path` 会去调 MS link.exe，而它读不了渲染器的 ThinLTO bitcode）；`/STACK:268435456`（guest 的 SP 是模拟的
  `x[31]`，但 C 调用吃宿主栈，深调用链没有 `musttail` 就必须有大栈【推测】）；`/SUBSYSTEM:CONSOLE`（日志走 stderr）；
  `/ORDER:@`+`/IGNORE:4037`（`link_order.txt` 里已不存在的函数名会产生 LNK4037）。
- `RX_LIFTED_LTO=1`（`:481-483,610-618`）：把 lifted 代码也做成 ThinLTO，优化搬到链接期，于是 LoopRotate/capture 上限要作为
  `-Wl,/mllvm:…` 传给链接并配 `/lldltocache`（注释：大模块 ~3 GB）。**默认关**。
- 默认关且注释带实测结论的其它开关：`RX_FPX_INLINE`（12→80 min、exe 277→388 MB、帧率更差）、`RX_NO_GUEST_MARKS`、`RX_MARCH`
  （两次实测无收益）、`RX_HOT_CFLAGS`、`RX_PROFILE_LINES`/`RX_SAMPLE_PROFILE`、`RX_NO_LTO`。

## 9. guest 模型契约：`rt/a64.h` + `rt/a64_rt.c`

```c
typedef struct GuestContext {          /* rt/a64.h:14-29 */
  uint64_t x[32];       /* x[31] 就是 SP（suyu 的译法） */
  uint64_t pc;          /* 只在停线程（trap/unhandled）时写 */
  uint8_t n, z, c, v;   /* NZCV 各一字节且紧邻——pack_flags 才敢一次 store */
  int halted;  uint64_t vreg[32][2];  uint64_t tpidr_el0, tpidrro_el0;  uint64_t fpcr, fpsr;
  uint64_t excl_addr, excl_lo, excl_hi; uint32_t excl_size; int excl_valid;  void *thread;
} GuestContext;  typedef GuestContext Ctx;
```

- **内存**：`A64_PTR(a)=A64_MEM+(uint32_t)a`，`A64_MEM` 原生固定 `0x100000000000`（16 TB，避开 Windows 默认映射），wasm 下为 0
  （`a64.h:57-65`）。load/store 是 inline `memcpy`/解引用（`:90-110`），**没有页表、没有软 TLB**；`store*_sp` 不标脏页；
  `_WIN32` 下 `a64_dirty[1<<20]` 是 4 GB/4 KB 的页标记，用 release store 标（`:78-85`）。
- **调用约定**：`typedef void (*A64Fn)(Ctx*)`；`TAILCALL(f)=__attribute__((musttail)) return f(c)`（wasm 无 tail call 时退化为调用+return，`:116-120`）。
- **间接调用**：`A64_ICALL/A64_IJUMP` = 每调用点两个 `static uint64_t` 内联缓存，条目高 32 位是 guest 地址、低 32 位是“函数指针相对
  `&a64_lookup` 的偏移”（一次比较 + 一次间接跳转，`:123-147`）。miss 走 `a64_icall_site`（`a64_rt.c:175-209`）：试第二槽 →
  `a64_lookup`（`:36-63`，直接映射 `lookup_cache[1<<18]` + 二分 `a64_fn_addr`）→ `a64_lookup_host`（OS 层运行期造的宿主函数，主要是 NVN
  proc）→ `a64_call_miss` → `a64_on_trap`。
- **trap**：`a64_trap` 记 `pc/halted` 后调 weak `a64_on_trap`（默认 abort；`hos/hos_thread.c:243` 覆盖成带上下文的报告）。
- **SVC**：weak `a64_svc`（`a64_rt.c:211-215`）默认当 trap；`hos/hos_svc.c:246` 覆盖成 Horizon svc 表（SetHeapSize、QueryMemory、线程优先级/
  亲和性、ExitThread…；注释称“游戏自己一条 svc 都没有，SDK 有 ~60 个包装”）。
- **独占访问**：LDXR 记 (addr,size,value)，STXR 用 CAS 比对，因此**真实宿主线程**间也安全（dynarmic 模型）；`ldxp/stxp` 在 64 位
  int128 下用 `__atomic_compare_exchange_n`（`:258-299`）。**CNTPCT** 用 QPC 换算 19.2 MHz（`:302-327`）。
- `a64.h` 末尾 `#include "a64_suyu.h"`/`"a64_fpx.h"`（`:152-153`）——**生成的头文件被契约头包含**，所以 `a64_rtgen.py` 必须在
  `rt/a64.h` 定义完 Ctx 与访存原语之后再引入 suyu 的东西（`a64_rtgen.py:2-8`）。

## 10. 为什么整个 guest 都在 4 GB 以下

1. **一次寻址**：`A64_MEM+(uint32_t)addr`（`a64.h:65`）——base 是编译期常量，能折进寻址模式，不必每次从全局重载；访存即宿主访存。
2. **地址能塞进 32 位**：`a64_lookup` 一进来就 `if (addr>>32 || !addr) return NULL`（`a64_rt.c:37`）；调用点缓存打包成
   `target<<32|delta32`（`a64.h:141`、`a64_rt.c:50,198`）——只有 <4 GB 才成立。
3. **同一份 lifted C 能编到 wasm32**：web 版 `A64_MEM_ZERO` 让“guest 地址 = wasm 地址”，宿主数据链在 guest 窗口之上
   （`-sGLOBAL_BASE`，`build.py:628-637,702-711`；`hos_main.c:23-30,108-110`）。>32 位就直接崩。
4. **渲染器把 guest 内存当一段连续窗口**：`hos_guest_base()/hos_guest_size()`（`hos_main.c:133-134`）被 `gpu/render/rx_buffer.cpp:78-96`
   按 16 MB 段/8 MB 步长切块、按页标脏上传——地址就是窗口内偏移。
5. **我们是 rtld，地址由我们发**，所以“全部 <4 GB”是**可保证**而非赌一把：`MAIN_BASE=0x08000000`、`TRAMP_BASE=0x07000000`、
   system region `0x01000000-0x07000000`、heap `0x10000000`（`hos.h:8-12,36-38`）、地址区从 `HOS_SPACE_END=0xFFFF0000` 往上（`hos.h:52`）。
   顺带 `0x08000000` 正是 Horizon code 区基址（`hos_svc.c:27` 的 `GetInfo(AslrRegionAddress)` 也返回它，`hos_svc.c:231` 的
   `QueryMemory` 也用它），游戏自己的内存查询因此自洽。
- 代价：guest 内存上限 4 GB（默认只报 3285 MB，`hos/hos_mem.c:44`）；任何 ≥4 GB 的 guest 地址会**静默别名**。这是最硬的一条不变量。

## 11. 设计取舍（作者写在注释里的判断）

| 取舍 | 选择 | 理由 |
|---|---|---|
| 入口宁多勿少 | 任意地址可作 entry，跨 entry 边变尾调用 | 正确性 by construction，多收只花体积（`a64_lift.py:4-7`） |
| 但**不收** ADR/ADRP 目标 | 只收 eh_frame/exports/init_array/RELATIVE/BL | 3,735 个多余 entry 把函数切成互走的两半（`nx_image.py:139-141`） |
| PLT 构建期消解 | 读已重定位的 GOT | 运行期没有动态链接器，留桩无意义（`a64_lift.py:10-12,1048-1067`） |
| FP 语义 | 保留 suyu 的 FPX1：原生快路径 + 位精确回退 | `FPCR/FPSR` 必须位精确，否则物理/动画会漂（`arm64_to_c.h:6516,6577`） |
| 未实现 API | 日志桩而非链接失败 | 先能跑、日志驱动补实现（`build.py:10-11`） |
| 系统调用 | HLE 抬到 `nn::` 边界，svc 只兜底 | 只有“该由上层 HLE 掉”的残留才到 svc（`hos_svc.c:1-5`） |
| 优化预算 | 投在指令选择/内联/布局等每指令生效处 | §8 几乎每个开关都带 “X→Y fps / exe ±Z MB / 编译 ±W min” 实测 |
| 增量 | lift 全有或全无；native 逐文件；hook/splice 每次 native 重放 | 避免半套镜像，同时让“加一个 hook”只重编一个文件 |

## 12. 覆盖度：实现了什么 / 桩了什么

**已实现（构建链）**：NSO 全解析（三段 LZ4、MOD0、dynamic、符号、RELA/JMPREL、init_array、eh_frame_hdr）；多模块 rtld 布局与重定位
（`RELATIVE/ABS64/GLOB_DAT/JUMP_SLOT`，weak=0 语义）；函数发现 + 控制流 + 跳转表（三层兜底）+ PLT；SVC/BRK/HLT/UDF 出口；
PC 相对指令按 pc 去重；ENTRY_HOOK / SPLICE_AFTER(_EACH) 两种注入机制（6 个按地址的 `ENTRY_HOOK(0x…)` + 12 个 `ENTRY_HOOK_SYM`、
9 个 `SPLICE_AFTER` 含 1 个 `_EACH`）；
`.pchtxt/.ips/IPS32` 模组补丁编入镜像；native 与 web 两套链接。

**桩/未实现（按设计）**：
- 无 HLE 的 OS-facing import → `a64_stubs.c` 日志桩（打印一次，返回 0）。
- 无法翻译的字 → `a64_trap`（`a64_lift.py:1184-1187`）。文档记录 TOTK 的 868,649 个不同字里**114 个不可译**（代码段里的数据或 A57
  不会走的 LSE 路径，`a64-kit/README.md:71`）。
- 27,126 个 `br` 保持间接（`a64-kit/README.md:72`）：~26,750 是虚函数尾调用，~380 是未校验形状。
- `ic ivau` 直接忽略（`a64_lift.py:1248-1249`：“no self-modifying code”）——所以**运行期不能打代码补丁**，这也是模组必须编进镜像的原因。
- 128 位 `ldxp/stxp` 在无 int128 平台不保证 single-copy atomicity（`a64_rt.c:275`）。

**引用存在但仓库缺失**：`a64-kit/tests/insn_test.py`（`a64-kit/README.md:33,52` 声称 574 形态/38,494 状态与 Unicorn 零差异）、
`ports/zelda-totk/PORT.md`（`hos.h:8`、`a64-kit/README.md:3` 引用）、`scripts/{devirt.py,sample_profile.py,bench_castle.sh}`
（`a64_rt.c:110`、`a64_lift.py:523`、`build.py:488` 引用；`hot_funcs.txt`/`link_order.txt` 即其产物）。
⇒ 本报告能核对“机制”，但**不能核对**“指令语义零差异”与 profile 数字，只能转述文档。

## 13. 风险与脆弱点

1. **版本绑死**：`EXPECTED_IDS` 硬编码 1.0.0 的 `main`/`sdk` build ID（`build_game.py:17-20`）；ENTRY_HOOK/SPLICE_AFTER 是**具体地址**，
   `hot_funcs.txt`/`link_order.txt` 是**具体 `f_xxxxxxxx`**，`thread_hooks` 甚至依赖三条具体指令字（`build.py:236-239`）。换版本这些全失效，
   且不会优雅降级（SPLICE 找不到锚点直接 `sys.exit`，`build.py:148-150`）。
2. **全有或全无的 lift 缓存**：中断一次就得手动挪 `build/`（已写进 README:46）；`hos/*.c` 改一行注释也会触发数小时重 lift。
   好的一面：`implemented()` 只按名字集合影响 lift，所以“改了 HLE 语义忘了重链接”不会发生（native 必重编 runtime 组）。
3. **可重现性边界**：lift 输出本身确定（entry 排序、`sorted()` 输出、trampoline 按模块/重定位序分配），但 ① `a64emit.dll` 是本地编译产物
   （按源码 sha1 缓存），不同 clang 版本可能产出不同 C；② exe 行为取决于编译器版本与 `-O3`+`ThinLTO`，仓库不锁版本；
   ③ git 历史被压缩（6 个提交，“Upload all files”），无法审计演进。
4. **编译资源**：额外 45 GB（README:17）、1.1 GB C、100-200 s/(25 MB 文件)、42+ 个 code 文件、LTO 链接 ~3 GB 内存；`-O3` 让全量重编
   十几分钟起（`build.py:530-536,777-780`），单线程巨型函数是主要长尾。
5. **语义风险集中区**：
   - 跳转表恢复是**启发式**（`hoisted_constant` 依赖“地址序≈时间序”这一 LLVM 布局假设，`a64_lift.py:1011-1014`）。恢复失败时 `A64_IJUMP`
     仍正确；但恢复**多收**目标会把数据当代码提升，只能靠 `jump_tables_missing.txt` 与运行期表现发现。
   - `dead_flags` 假定“调用 clobber NZCV、RET 不读 flag”（`a64_lift.py:209-214`）——ABI 事实，但若某导出函数跨调用读 flag 就会静默错。
   - `_sp` store 不标脏页假定“GPU 永不读 guest 栈”；若游戏把栈上临时缓冲交给 NVN 上传，会读到旧数据且极难定位。
   - 六道后处理依赖 suyu 生成文本的**精确字符串形状**（`a64_lift.py:72-74,91-97,102-107,121-123` 全是正则）。升级 `arm64_to_c.h` 会让它们
     静默不匹配 = 只丢优化，但也会让 `fpx_header` 少生成 helper → 编译报错（较好的失败模式）。
   - 未覆盖指令是**硬停**（trap），不会静默跳过——失败模式偏安全。
6. **4 GB 不变量**：一切 guest 地址 <4 GB 由 loader/HLE 保证；将来若需 >4 GB（大 heap/mod 资源），`A64_PTR`、icall 缓存打包、wasm32 等价性
   **三处同时**要改。
7. **文档漂移**：`a64-kit/README.md:27` 说 `-O1`（现默认 `-O3`）；`build.py:602` 说 link_order 2,966 个函数（现 4,694）；README 的
   “12→80 min / 277→388 MB”等是特定机器上的历史测量，不是当前保证。

## 14. 可验证的证据（只读命令）

```bash
wc -l a64-kit/build.py a64-kit/lift/a64_lift.py a64-kit/lift/suyu/arm64_to_c.h a64-kit/rt/a64.h a64-kit/rt/a64_rt.c  # 793/1359/8081/199/327
grep -c '^HLE(' a64-kit/hos/*.c | awk -F: '{s+=$2} END{print s}'                       # 396 个 HLE 实现
grep -rn '^ENTRY_HOOK(' a64-kit/hos/*.c | wc -l                                       # 6（totk_cull/cores/title 的按地址 hook）
grep -rn '^ENTRY_HOOK_SYM' a64-kit/hos/*.c | wc -l                                      # 12（nn_libc 6、nn_image_debug 6）
grep -rn '^SPLICE_AFTER' a64-kit/hos/*.c | wc -l                                        # 9（含 1 个 _EACH）
grep -c '^f_' ports/zelda-totk/hot_funcs.txt ports/zelda-totk/link_order.txt            # 400 / 4694
git ls-files | grep -i 'insn_test\|PORT.md\|scripts/'                                   # 空：§12 列的辅助件确实不在仓库
python a64-kit/tools/nso.py game/exefs /tmp/nso-out     # 需自备 ExeFS：打印每模块符号/重定位/eh_frame 统计
python a64-kit/build.py lift totk --exefs game/exefs --skip subsdk0 --build /tmp/b      # 需 lz4+clang：打印 §6.4/§7 的那组计数
```

> 分界：渲染器（`gpu/render/`）、`hos/` 的 HLE 语义细节、SDL/pad/audio、RomFS 与模组运行期、`hos_mem.c` 分配器内部不在本报告范围，
> 只在“契约”与“构建输入”必要处提及。
