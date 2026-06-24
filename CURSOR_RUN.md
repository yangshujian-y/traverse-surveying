# Cursor 运行说明

如果在 Cursor 中运行时报错：

```text
ModuleNotFoundError: No module named '_tkinter'
ModuleNotFoundError: No module named 'PIL'
```

原因通常是 Cursor 默认使用了 Homebrew 的 Python，例如：

```text
/opt/homebrew/Cellar/python@3.14/...
```

这个 Python 没有安装 Tkinter 图形界面组件，也没有 Pillow。

本项目已配置 Cursor/VS Code 默认使用可运行的 Python：

```text
/Users/Zhuanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

## 推荐运行方式

在 Cursor 终端中运行：

```bash
./run.command
```

或直接运行：

```bash
/Users/Zhuanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 archive_image_tool.py
```

## 手动选择解释器

如果 Cursor 仍然报错：

1. 按 `Cmd + Shift + P`
2. 输入 `Python: Select Interpreter`
3. 选择：

```text
/Users/Zhuanz/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

然后重新运行。
