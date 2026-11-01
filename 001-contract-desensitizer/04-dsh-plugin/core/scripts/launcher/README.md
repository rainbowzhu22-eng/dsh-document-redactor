# 启动器说明

跨平台只有一个入口，Windows / macOS / Linux 通用：

```bash
python scripts/launcher/start.py                 # 启动并自动打开浏览器
python scripts/launcher/start.py --foreground    # 前台运行，日志留在终端，Ctrl-C 结束
```

默认模式会将服务放在后台运行，启动器完成健康检查后可关闭启动器窗口。`--foreground`模式下当前窗口承载本地服务，使用期间必须保持窗口开启；关闭窗口会中断网页上传、复核和导出。结束前台服务请按Ctrl+C，或在网页中点击“退出”。

启动器会自动完成：

1. 找带依赖的 Python；找不到就新建 `.venv` 并安装 `requirements.txt`（仅首次，约 1~2 分钟）
2. 释放 18800 端口、结束上一次的服务进程
3. 启动`contract_app_server.py`（首次安装依赖需要联网，安装完成后文书处理不联网；只监听`127.0.0.1`）
4. 健康检查通过后打开 `http://127.0.0.1:18800/`

停止：点击网页右上角「退出」，或在终端按 Ctrl-C（前台模式）。macOS安装器生成的「停止法律文书脱敏助手服务.app」会读取同一应用资源中的实际技能目录，并只调用`start.py --stop`；如果虚拟环境缺失，启动和停止入口均回退到系统Python。桌面入口不会按端口结束无法确认归属的进程。

macOS随包安装器会生成启动和停止两个`.app`，并在应用资源中写入实际技能目录；自定义`CONTRACT_DESENSITIZER_SKILLS_DIR`时不需要手工改入口。平台不支持随包生成应用时，仍可在本机创建快捷方式：macOS把`python <绝对路径>/start.py`存成`.command`文件，Windows存成`.bat`。
