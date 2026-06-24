# Windows 打包说明

Windows 的 `.exe` 需要在 Windows 系统上打包生成。把本文件夹复制到 Windows 电脑后操作：

1. 安装 Python 3.11 或 3.12，并勾选 “Add Python to PATH”。
2. 双击 `build_windows.bat`。
3. 打包完成后，执行文件在：

```text
dist\归档图片批量复制与页码工具.exe
```

如果双击 bat 后提示找不到 `py`，请先确认 Windows 已安装 Python，并在命令行执行：

```bat
python --version
```

也可以把 `build_windows.bat` 里的 `py -3` 改成 `python`。

## 没有 Windows 电脑时

本项目已经包含 GitHub Actions 配置：

```text
.github/workflows/build-windows.yml
```

把项目上传到 GitHub 后：

1. 打开仓库页面。
2. 进入 `Actions`。
3. 选择 `Build Windows EXE`。
4. 点击 `Run workflow`。
5. 等运行完成后，在页面底部 `Artifacts` 下载 `windows-exe`。

下载后里面就是：

```text
归档图片批量复制与页码工具.exe
```
