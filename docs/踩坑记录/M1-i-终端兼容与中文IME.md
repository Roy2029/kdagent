# M1-i 系列踩坑记录：终端兼容与中文 IME（conhost）

> 版本范围：`a12207a`（方案 A 禁 Kitty）→ `7b339b3`（IME 二次修复）
> 环境：Windows 10 19045 · 用户终端 = **conhost（cmd 窗口，未装 Windows Terminal）** · Textual 8.2.8
> 结论：**中文 IME ✅ / backspace ✅ / 系统剪贴板复制粘贴 ✅ / 鼠标 SGR ✅ / 方向键 ✅**（2026-08-15 用户实测确认）

---

## 0. 时间线（三条失败路径 → 收敛）

```
方案 A（禁 Kitty）            → 失败：conhost 忽略 Kitty，no-op
根因修正（传统输入层）        → backspace/复制粘贴/鼠标/方向键全 OK，中文仍失败
IME 二次修复（VT 过滤误杀）   → 中文恢复 ✅
```

**最大教训**：一次只解决一个"看起来的根因"是错的。本问题是**三个根因叠加**，前两个修复后第三个才暴露。每次只改一处、让用户实测，才能逐步收敛——不要一口气改三处然后无法归因。

---

## 1. 踩坑清单

### 坑 1：环境误判——以为用户用 Windows Terminal，实际是 conhost

- **现象**：方案 A（禁用 `\x1b[>1u`）落地后中文依旧失败。
- **排查**：conhost **永远不支持** Kitty 键盘协议（Windows Terminal 1.25 Preview 2026-03 才加入），`\x1b[>1u` 被 conhost 直接忽略 → 方案 A 是 no-op，与 Kitty 无关。
- **根因**：开发前提建立在"用户用 Windows Terminal"上，而用户实际跑在 cmd（conhost），从未装 WT。此前询问终端类型的回答曾误导（"Windows Terminal"）→ 被实测证伪。
- **修复**：`patch_windows_input` 过滤 `\x1b[>1u` 仅作无害清理；真实修复转向传统输入层（坑 2）。
- **教训**：平台/环境能力假设**必须用实测现象验证**，不能靠口头确认。环境先行的第一步应该是 `cmd /c ver` + 确认 conhost/WT，而不是假设。

### 坑 2：Textual 强制 `ENABLE_VIRTUAL_TERMINAL_INPUT` → conhost IME 缺陷（第一真根因）

- **现象**：中文输入失败（IME 无法正常组合/提交）。
- **排查**：逐层对比 Claude Code（Node/ink）为何同 cmd 中文正常 → 两者唯一输入层差异 = 是否设置 VT 输入模式。
- **根因**：Textual `enable_application_mode` 把 stdin 强制设 `ENABLE_VIRTUAL_TERMINAL_INPUT`（[win32.py:157](../.venv/Lib/site-packages/textual/drivers/win32.py#L157)）。**conhost 在 VT 输入模式下对中文 IME 组合提交处理有缺陷**；Claude Code 不设该模式、走传统 `ReadConsoleInputW` 直读 Unicode 字符，故同 cmd 中文正常。
- **修复**：monkeypatch `enable_application_mode` → **传统事件输入模式**（即时读键，无 LINE/ECHO/PROCESSED + 窗口/鼠标事件），与 Node/ink 完全一致：
  ```
  ENABLE_WINDOW_INPUT | ENABLE_MOUSE_INPUT | ENABLE_EXTENDED_FLAGS
  ```
- **连带行为**：无 PROCESSED_INPUT 时 Ctrl+C 以 KEY_EVENT(`\x03`) 进入而非信号，Textual 正常解析——这是传统模式的正确行为，不是 bug。
- **教训**：同环境不同工具行为差异，先比"输入层配置"这一层，往往就是根因。

### 坑 3：IME 确认汉字被原 VT 过滤误杀（第二真根因，最隐蔽）

- **现象**：传统输入层落地后，其他键全 OK，**唯独中文死**——已排除所有配置问题。
- **排查**：所有普通键/鼠标都正常流动，说明事件流是通的；差异只在 IME 提交路径 → 审查 KEY_EVENT 分支的每个条件。
- **根因**：conhost 传统模式把 IME 确认汉字以 **`wVirtualKeyCode=0` + 非空 `UnicodeChar`** 的 KEY_EVENT 提交（拼音阶段不发事件，确认后整串提交，每字一个 VK=0 事件）。而 event monitor 从 Textual 原版（VT 输入模式）copy 的过滤：
  ```python
  if key_event.dwControlKeyState and key_event.wVirtualKeyCode == 0:
      continue  # IME 辅助事件，无字符
  ```
  该条件在 VT 模式下用于跳过 IME 辅助事件；传统模式下 IME 确认事件 VK=0 **且** dwControlKeyState 常非零 → **恰好被此过滤误杀**。
- **修复**：`_should_drop_key_event(vkey, unicode_char)` —— 仅丢弃「无 VK **且** 无字符」的空事件，IME 汉字保留：
  ```python
  return vkey == 0 and unicode_char in ("", "\x00")
  ```
- **教训**：**从原代码 copy 的过滤/分支往往隐含平台前提**（这里是 VT 输入模式 vs 传统模式）。移植时对每个条件追问"这个判断在什么模式下才成立"。过滤器是"宁可错杀"逻辑，移植到不同平台时破坏性最大。

### 坑 4：backspace 失效

- **现象**：无法删除输入框文本。
- **根因**：conhost 传统 backspace 发送 `\x08`(BS)，Textual 解析为 `ctrl+h`；TextArea 只有 `backspace`(=\x7f) 绑定 → 不匹配。
- **修复**：ChatInput 加 `Binding("ctrl+h", "delete_left", "删除", priority=True)` 兜底，两路可删。
- **教训**：终端发送的控制字符与 Textual 按键解析的映射必须**实测确认**（\x7f→backspace、\x08→ctrl+h），不要按直觉假设。

### 坑 5：输入框内复制/粘贴失效

- **现象**：非输入框复制可以，输入框内容无法复制、也无法粘贴。
- **根因**：Textual 默认进程内剪贴板（非系统）；且 App 层 `ctrl+c = request_quit` 会抢键。
- **修复**：pyperclip 接**系统剪贴板**（复制/粘贴自定义 handler）；ChatInput 显式 `priority=True` 绑定 `ctrl+c→copy`、`ctrl+v→paste`，规避 App 层抢键。
- **教训**：Textual 剪贴板默认不接系统；绑定抢占看优先级（priority）与焦点组件 vs App 层。

### 坑 6：`_CONTROL_RE` 误杀中文字节

- **现象**：中文/backspace 相关异常。
- **根因**：过滤正则 `[\x00-\x1f\x7f-\x9f]` 会把 backspace(`\x08`) 和**中文 UTF-8 续字节**（`\x80-\x9f`）一并丢弃。
- **修复**：收窄为 `[\x1b\x9b]`（仅 ESC/8-bit CSI 头），放行 `\x08`/`\x7f`/中文 UTF-8 续字节。
- **教训**：字节级范围过滤会误伤多字节 UTF-8；面向控制序列过滤时只保留真正需要拦截的字节（ESC 与 CSI 头），其余放行交给下游解析。

### 坑 7：Textual 默认日志不落盘

- **现象**：加了 `self.app.log.debug` 诊断，但看不到输出。
- **根因**：Textual 日志仅 `TEXTUAL_LOG` 环境变量或 Devtools 连接时才写文件（[constants.py:122](../.venv/Lib/site-packages/textual/constants.py#L122)）。
- **修复**：`KDA_INPUT_DEBUG=1` 时独立写 `.kdagent/input-debug.log`（EventMonitor 内 `os.makedirs` + append），finally 关闭。
- **教训**：调试输出要选**独立可靠的落点**，不要依赖框架的可选日志通道。

### 坑 8：弹窗居中选择器（M1-i 周边）

- **现象**：确认弹窗不居中。
- **根因**：基类 `Screen`/`ModalScreen` 选择器被 Textual 8.2.8 覆盖不生效。
- **修复**：用具体类选择器 `ConfirmDialog, ExitDialog { align: center middle }`。
- **教训**：Textual 8.x 全局选择器会被组件默认样式覆盖，定位样式须用具体类。

### 坑 9：`git add -A` 误提交本地脚本

- **现象**：`run_kdagent.bat`（含用户本地路径）被带入仓库。
- **修复**：`git rm --cached` + `git commit --amend` 移除，保留为 untracked。
- **教训**：提交前 `git status` 检查 untracked；含本机路径的启动脚本用 `.gitignore` 或直接不入库。

### 坑 10：工具链细节（mypy / ruff）

| 坑 | 修复 |
|---|---|
| `sys.__stdin__` 类型为 `TextIOWrapper \| None` | `assert sys.__stdin__ is not None` 收窄 |
| 多余的 `# type: ignore[method-assign]` | mypy 会报 unused ignore，只给真正需要的行加 |
| `open()` 跨生命周期不能 `with` | `# noqa: SIM115` 并注释原因 |
| 函数内 import 顺序 | ruff I001 → 按 stdlib/第三方分组排序 |

---

## 2. 沉淀：conhost 传统模式输入机制（本项目实测结论）

| 机制 | 结论 |
|---|---|
| IME 整串提交 | 拼音阶段**不发** KEY_EVENT；确认后每字一个 **VK=0 + UnicodeChar** 事件 |
| 特殊键 | VK 码需手动翻译为 xterm 序列：方向键 `\x1b[D`/`\x1b[1;N{D}`、编辑键 `\x1b[3~` 等、功能键 `\x1bOP`/`\x1b[15~` |
| 鼠标 | MOUSE_EVENT → SGR 序列 `\x1b[<b;x;yM`/`m`（否则点击无效） |
| 修饰键本身 | 无输出，translate 返回 None 丢弃 |
| Ctrl+C | 无 PROCESSED_INPUT → KEY_EVENT(`\x03`)，Textual 正常解析 |
| Textual binding 继承 | `_merge_bindings` 遍历 MRO 合并父类 BINDINGS，**子类不会覆盖父类绑定**（ChatInput 不丢 TextArea 绑定）；可打印字符由组件 `check_consume_key` 消费 |

## 3. 关键文件

- [src/kdagent/compat.py](../../src/kdagent/compat.py) —— 兼容层核心：`filter_kitty_enable` / `translate_key_event` / `translate_mouse_event` / `_should_drop_key_event` / `_compat_enable_application_mode` / `_compat_event_monitor_run` / `patch_windows_input`
- [src/kdagent/ui/app.py](../../src/kdagent/ui/app.py) —— ChatInput 绑定（ctrl+h 兜底 / ctrl+c / ctrl+v）、`_CONTROL_RE`
- [tests/test_compat.py](../../tests/test_compat.py) —— 翻译纯函数 + win32 patch 生效测试
