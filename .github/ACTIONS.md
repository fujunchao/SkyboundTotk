# 用 GitHub Actions 编译 SkyboundTOTK

两条线，别混：

| workflow | 跑在哪 | 需要游戏数据 | 产出 | 触发 |
|---|---|---|---|---|
| [`ci.yml`](workflows/ci.yml) | `windows-2022`（GitHub 托管，免费） | **否** | 编译检查结论 | push / PR / 手动 |
| [`build.yml`](workflows/build.yml) | **self-hosted**（或付费大规格 runner） | **是**（放在 runner 上） | `totk.exe` + `image.bin` | 手动 / 打 `v*` tag |

托管 runner 跑不了完整构建的三个硬原因（详见 [PROJECT_UNDERSTANDING.md](../PROJECT_UNDERSTANDING.md) §7）：
**① 只有 14 GB 盘**（构建要 45 GB + 游戏本体）；**② 游戏数据不能进仓库/secret**（版权，且 secret 上限 48 KB）；
**③ 单 job 6 小时上限，且产物含游戏派生数据不能公开上传**。

---

## 一、`ci.yml`：零配置，已可直接跑

做什么：

1. `python -m compileall` 全部 Python（`a64-kit/`、`ports/`、`build_game.py`）。
2. 构建 `a64emit.dll`（suyu 的单指令翻译器）并翻译 8 种指令形态 + 检查 4 段生成的运行时非空 —— [`a64-kit/tests/emit_smoke.py`](../a64-kit/tests/emit_smoke.py)，本地也能跑：`python a64-kit/tests/emit_smoke.py`。
3. **渲染器 + suyu 的编译检查**：源码清单直接取自 `build.py` 的 `gpu_sources()`（不会与真实构建漂移），
   用 `clang++ --target=x86_64-pc-windows-msvc -std=c++20 -fsyntax-only` 过一遍 **约 74k 行 C++**（渲染器 24k + suyu 48k）。

做不了：lifted 代码与 `hos/` —— 它们 include `gen/a64_suyu.h`、`gen/a64_fpx.h`，这两个头只有 lift 才生成。

注意：镜像钉在 `windows-2022`（`-latest` 会漂到新镜像，而这个项目的构建对 clang 版本敏感）。

---

## 二、`build.yml`：完整构建

### 1. 准备 runner（一台 Windows 机器）

- 磁盘 **≥ 80 GB**：游戏 ~17 GB + 中间文件 ~50 GB（`build_root` 单独一个盘最好）。
- 软件：Git、Python 3.10+（也可让 `setup-python` 装）、**LLVM 的 `clang`/`clang++`/`lld` 在 PATH**（或装 emsdk 并设 `EMSDK`）、
  VS2022 Build Tools（MSVC + Windows SDK，clang 需要它们的头/库）。
- 注册：仓库 **Settings → Actions → Runners → New self-hosted runner → Windows x64**，按页面命令执行
  `config.cmd --labels self-hosted,windows,x64`；想常驻就用 `svc.cmd install` + `svc.cmd start`（否则要保持登录/终端）。

### 2. 把游戏文件放在 runner 上（永不入库）

解包好的 1.0.0 目录，例如 `D:\totk\game\exefs\{main,sdk,rtld,...}` 与 `D:\totk\game\romfs\{ActorSystem,Event,...}`（结构见 [README.md](../README.md)）。
`build.yml` 只把 `game_dir\exefs` 传给 `build.py lift --exefs`，**不碰 romfs**（只有打包 `dist/` 才需要，CI 默认不打包）。

### 3. 触发

```bash
gh workflow run build.yml \
  -f runner=self-hosted \
  -f game_dir=D:\totk\game \
  -f build_root=D:\ci\build \
  -f opt=O3
```

或网页 Actions → build → Run workflow。打 `v*` tag 会用默认输入自动跑一次。

### 4. 缓存与复用（这是 CI 比本地强的地方）

- `build_root` 是**持久目录**：`lift-input.sha256` 记录 lift 输入指纹（lift/hos/rt 源码 + `build.py` + `nso.py` +
  `hot_funcs.txt` + ExeFS 模块字节），一致就跳过 lift，不一致就自动删掉重建（不用像 README 那样手动挪目录）。
- `native` 阶段本身**逐目标文件增量**（每文件 `.o.sha1`），所以连跑两次，第二次只重编改动过的文件。
- 完整性检查：`image.json` + `gen/a64_suyu.h` + 至少一个 `code_*.c` 才算"lift 完整"，中断的 lift 不会被误用。

### 5. 产物

artifact `SkyboundTOTK-<run_number>`（只保留 7 天）：
`totk.exe`、`wgpu_native.dll`、`SDL2.dll`、`image.bin`、`image.json`，以及诊断用的
`mod_patches.txt`、`gen/indirect_br.txt`、`gen/jump_tables_missing.txt`（后两个能看出跳转表恢复的覆盖度）。

跑起来：把这几个文件和你的 `romfs/` 放进同一个文件夹，双击 `totk.exe`（改名 `SkyboundTOTK.exe` 也行，程序按 exe 所在目录找文件）。

### 6. 常见失败

| 报错 | 处理 |
|---|---|
| `no TOTK 1.0.0 ExeFS at ...` | `game_dir` 指错，或多了一层 `exefs/exefs` |
| `the build needs about 50 GB free` | 清盘，或把 `build_root` 指到另一块盘 |
| `clang not found` | LLVM 没装/没进 PATH（`where clang`）；或用 emsdk 并设 `EMSDK` |
| `ModuleNotFoundError: lz4` | `python -m pip install lz4`（与 workflow 用的同一个 Python） |
| lift 中途被杀 | 下次运行会自动清掉不完整目录并重 lift |
| job 6 小时超时 | `opt=O1` 先出包；或换核数更多的 runner |
| 渲染器编译失败但本地能过 | runner 的 LLVM 版本不同；把 `setup` 换成固定版本，或改用 emsdk 的 clang |

---

## 三、可选：付费大规格 runner

GitHub Team/Enterprise 的 larger runner 可以给到 300 GB 级磁盘，把 `-f runner=windows-2022-8core` 这类标签传进去即可；
但托管 runner **没有你的游戏文件**，必须在 job 里先把它们取下来（例如把压缩包放私有对象存储，用 secret 里的临时签名 URL）：

```yaml
      - name: Fetch the game files (private storage, never committed)
        if: ${{ env.TOTK_GAME_URL != '' }}
        shell: powershell
        env:
          TOTK_GAME_URL: ${{ secrets.TOTK_GAME_URL }}
          GAME_DIR: ${{ env.GAME_DIR }}
        run: |
          Invoke-WebRequest -Uri $env:TOTK_GAME_URL -OutFile $env:RUNNER_TEMP\game.7z
          & "C:\Program Files\7-Zip\7z.exe" x $env:RUNNER_TEMP\game.7z "-o$env:GAME_DIR" -y
```

代价：每次运行下载/解压 ~17 GB，流量与时间都要算；磁盘、缓存（`actions/cache` 每仓库 10 GB）撑不住 `build_root`，等于每次都从头 lift。

---

## 四、合规（重要）

- 仓库保持**私有**：artifact 里有 `image.bin`/`totk.exe`，含游戏派生数据；公开放出去就是侵权。
  `build.yml` 已经守了一道：**公开仓库不会上传 artifact**（构建产物只留在 runner 的 `build_root` 里），
  要上传必须显式 `-f upload_public=true`（不建议），或者把仓库改成私有。
- 永远不要提交 `game/`、`build/`、`dist/`（[`.gitignore`](../.gitignore) 已覆盖，但 PR 里最容易夹带）；
  提交前自查 `git ls-files | Select-String -Pattern '^(game|build|dist)/'`。
- 一旦分发构建产物：`a64emit.dll`、生成的运行时、提升出的 C、整个 exe 因 suyu/yuzu 部分是 **GPL-2.0/3.0**，
  需要提供完整对应源码；MIT 只覆盖自有代码。

---

## 五、和本地 `build_game.py` 的差异

| | 本地 `build_game.py` | CI `build.yml` |
|---|---|---|
| 打包 `dist/`（含 16 GB RomFS 拷贝） | 是 | **否**（省 16 GB 和数分钟；需要时本地补一次） |
| 交互式 `pause`（`build.bat`） | 有 | 不用 `build.bat`，直接调 `build.py` |
| 断点续跑 | 手动挪 `build/` | 自动按指纹清理 |
| 缓存位置 | `<repo>/build` | `build_root`（建议仓库外） |
