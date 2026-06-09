# 14. v2.0 跨平台兼容 (Windows / macOS / Linux)

> Status: ✅ 已实施 + 17 跨平台 tests 全过
> 动机: 用户分享 8 张图 (Claude Code 在 Windows 跑失败: bash strip 反斜杠, WinError 2, .cmd wrapper)

## 0. 用户分享的 3 个 Windows 问题

| 问题 | 现象 | 根因 |
|------|------|------|
| **图 5, 6, 9** | `C:\Users\mtk20928\...` → `C:Usersmtk20928...` (反斜杠消失) | bash 把 `\` 当 escape char strip 掉 |
| **图 4** | `WinError 2: file not found` | 用名字查 CLI 找不到 (PATH 不含 .exe 路径) |
| **图 4** | 多 args 解析失败 | Windows .cmd / PowerShell wrapper 不解析多 args |

## 1. v2.0 跨平台设计 (4 道防线)

### 1.1 `pathlib.Path` 跨平台 (免疫问题 1)

v2.0 全用 `pathlib.Path` 不用 `os.path`:

```python
from pathlib import Path

# 创建 (自动适配平台分隔符)
p = Path("data") / "mailboxes" / "agent1.json"  # 跨平台
p = Path("/Users/foo/Project")  # macOS
p = Path(r"C:\Users\foo\Project")  # Windows (raw 字符串保留反斜杠)

# 写文件 (自动选 forward slash / backslash)
p.write_text("...")  # 不 strip 任何字符
```

**为什么 bash 会 strip 但 Path 不会**:
- bash: 命令行参数 `C:\Users\foo` 被 shell 解析, `\` 当 escape char 解释
- Path: Python 字符串字面量 (`r"C:\Users\foo"`), 不会 strip, 整字符串保留

### 1.2 `shutil.which()` 找 CLI (免疫问题 2)

```python
# v2.0 OpenCodeCLI
import shutil
def _find_cli(binary: str) -> str:
    found = shutil.which(binary)
    if found:
        return found
    raise FileNotFoundError(f"CLI binary '{binary}' not found in PATH. ...")
```

`shutil.which()` 在 Windows 下:
- 自动检查 `.exe` / `.cmd` / `.bat` 后缀
- 返回完整路径 (e.g. `C:\Program Files\opencode\opencode.exe`)
- PATH 不含时返回 None (不抛)

### 1.3 `subprocess` args list, 不传 shell (免疫问题 3)

```python
# v2.0 OpenCodeCLI.invoke
cmd = [self.binary, "run", prompt, "--model", self.model, "--format", "json"]
proc = await asyncio.create_subprocess_exec(*cmd, ...)
```

**关键**:
- `args` 是 list (Python 字符串), 不是 shell 字符串
- 不传 `shell=True` (默认 False)
- asyncio.create_subprocess_exec 直接调 OS `execvp` 系列, 绕过 shell 解释

Windows 下 .cmd wrapper 不解析多 args, 但 **execvp 直接调 binary** 不经 .cmd wrapper.

### 1.4 跨平台 `PosixPath` / `WindowsPath`

`pathlib.Path` 在创建时根据运行平台返回 `PosixPath` 或 `WindowsPath`:

| 平台 | Path(...) | 类型 | 行为 |
|------|----------|------|------|
| macOS | `Path("/foo/bar")` | `PosixPath` | forward slash |
| Windows | `Path("C:/foo")` | `WindowsPath` | backslash (自动转) |

## 2. v2.0 跨平台验证 (17 tests)

`tests/unit/runtime/test_cross_platform.py` — 5 类, 17 tests:

| 类别 | tests | 验证 |
|------|-------|------|
| `TestPathlibCrossPlatform` | 6 | Path 不 strip 反斜杠 / parts 保留 / write 正确 |
| `TestShutilWhich` | 2 | which 找 / 不存在返回 None |
| `TestSubprocessArgs` | 2 | args list 格式 / 无 shell 参数 |
| `TestV2FilesystemComponents` | 4 | Channel / Mailbox / Lock / Agent 全用 Path |
| `TestV2CliShutilWhich` | 2 | OpenCodeCLI._find_cli 找 / 抛 |
| `TestWorkspacePathWindowsStyle` | 1 | workspace_dir = Path 跨平台 |

跑: `pytest tests/unit/runtime/test_cross_platform.py -v`

## 3. 实际 Windows 跑 v2.0 (推荐步骤)

```powershell
# 1. 装 opencode
irm https://opencode.ai/install.ps1 | iex
# 验证:
where.exe opencode
# 期望: C:\Users\xxx\.local\bin\opencode.exe (或类似)

# 2. 装 Python 3.11+ (v2.0 要求)
python --version

# 3. 装 v2.0
git clone https://github.com/fundou1081/agents-chat-channel
cd agents-chat-channel
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 4. 跑
.venv\Scripts\python -m agents_chat.main init --data-dir .\data_v2
.venv\Scripts\python -m agents_chat.main run-all --data-dir .\data_v2 --agents qwencode claude --cli opencode
```

## 4. 常见 Windows 坑 (v2.0 已经避开)

| 坑 | v2.0 处理 |
|----|----------|
| `subprocess` 用 `shell=True` 传 args 字符串 | v2.0 用 `args` list + `shell=False` (隐式) |
| 路径拼接用 `os.path.join` (Windows 下用 `\`) | v2.0 全用 `Path / Path` (自动适配) |
| `os.path.exists()` 拼 path 时漏 `/` | v2.0 `Path("a") / "b"` 自动加 `/` |
| bash 反斜杠 escape (`\n` → `n`) | v2.0 raw string `Path(r"...")` 不会 |
| PATH 不含 `.exe` 后缀 | v2.0 `shutil.which()` 自动加 |
| Windows reserved names (`CON`, `PRN`) | v2.0 agent_id 检查 (alphanumeric + `_` + `-`) |
| 路径含空格 (e.g. `C:\Program Files\`) | v2.0 subprocess args list 不依赖 shell quote |

## 5. 已知限制

| 限制 | 说明 | 绕过 |
|------|------|------|
| Windows 路径里 `\` 在 POSIX 上是 1 个 part | 不影响 (只是 part 划分不同) | Windows 自动用 `\` 分 parts |
| v2 默认 opencode 用 minimax-m3-free (慢 30-60s) | 真 LLM 调用慢 | 换 qwen / OpenCode 别的 model |
| subprocess timeout 默认 300s | 一些 CLI 可能超时 | 改 `OpenCodeCLI(timeout_seconds=600)` |

## 6. 调试 Windows 跑 v2 问题

```python
# 加进 main.py
import shutil, platform
print(f"Platform: {platform.system()}")
print(f"opencode path: {shutil.which('opencode')}")
print(f"PATH: {os.environ.get('PATH', '')[:200]}")
```

## 7. 相关

- 图 4, 5, 6, 9 (用户分享) — Windows 经验
- `docs/13-pdr-architecture.md` — v2.0 4 组件架构
- `tests/unit/runtime/test_cross_platform.py` — 17 跨平台 tests
- 17 跨平台 tests + 207 老 tests = **224/224 全过**
