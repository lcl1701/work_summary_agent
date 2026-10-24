# Java 8 Update 511 安装来源核查

核查日期：2026-10-22。事件发生时间均为北京时间，2026-10-21。

结论：现存记录中没有找到本次工程任务发起 Java 下载、启动安装器或操作 Java 安装窗口的证据。直接日志确认安装走的是 Java 自动更新流程，且“选择安装”和 MSI 开始都早于本轮首条已记录 Codex 工具调用。具体点击者或父进程仍未取得直接证据，因此不能把安装归给用户或 Codex。

## 可核实的安装链路

| 时间 | 原始记录 | 能说明什么 |
| --- | --- | --- |
| 18:34:08 | `C:\Users\zlxwl\AppData\Local\Temp\jre-8u511-windows-au.exe` 创建时间 | Java 更新安装包在 22:48 前已经存在；文件时间本身不标明下载操作者 |
| 22:47:03 | `jusched.log` 第 76215 行：`CMDLINE = NOSTARTMENU="0" /installmethod="jau"` | 安装器使用 Java 更新安装方式，进程号为 12752 |
| 22:47:05.165 | `jusched.log` 第 76288—76289 行：`AU mode, don't show change folder` | 安装器进入自动更新模式 |
| 22:47:20.838 | `jusched.log` 第 76306—76307 行：`The user chose to install Java.` | 安装器收到选择安装的交互；这句话没有标识实际操作者，不能据此认定是用户或 Codex 点击 |
| 22:48:02 | Windows Application/MsiInstaller，事件 1040，记录号 33396 | Java MSI 安装事务开始，客户端进程号 12752 |
| 22:48:14.462 | 本次 Codex 会话工具调用：读取 computer-use 技能说明及工程配置进度 | 这是本次核对窗口中最早的已记录 Codex 调用，发生在安装选择和 MSI 开始之后 |
| 22:48:28 | Windows Application/MsiInstaller，事件 1033，记录号 33399 | Java 8 Update 511 安装成功，目录为 `C:\Program Files\Java\jre1.8.0_511\` |
| 22:49:35.336 | Codex 浏览器调用：点击飞书“确认开通权限” | 该调用的目标是飞书开发者后台的群成员权限，不是 Java 安装窗口 |
| 22:50:14 | Windows Application/MsiInstaller，事件 1033，记录号 33409 | Java Auto Updater 2.8.511.9 安装成功，旧 2.8.211.12 版本在此前卸载 |
| 22:50:32.682 | `jusched.log` 第 77833—77834 行：`The Setup Complete dialog is closed` | Java 完成窗口关闭；同样未记录具体点击者 |

Java 原始专用日志：`C:\Users\zlxwl\AppData\Local\Temp\jusched.log`。

## 证据文件

- `data/java-audit-installer.json`：指定窗口内的 Java 安装器记录摘录，含原日志行号；排除了安装遥测内容。
- `data/java-audit-windows-installer.json`：上述 Java MSI 事件的时间、事件号、记录号及相关说明。
- `data/java-audit-codex.json`：本次会话、关联子任务及同一时段其他 Codex 工具记录的限定审计结果。包含原始日志路径、行号与覆盖限制。
- `data/java-audit-all-sessions.json`：补充扫描本机现存全部 30 份会话 JSONL，不依赖可能滞后的文件修改时间；仅检查 22:40—22:52:59 内的工具文本记录，排除图片二进制内容。
- `data/java-audit-process.json`：Windows 进程创建日志的可读性检查结果。

## Codex 记录交叉核对

本次会话与 9 个关联子任务共检查到截至 22:52:59 的 697 次唯一工具调用和 697 条结果，没有发现 Java 安装、Java 包下载或 Java 安装窗口操作的记录。

为避免遗漏另一个会话，又扫描本机现存全部 30 份会话 JSONL，约 67.8 MB。严格限定 22:40—22:52:59 窗口后，共 12 次工具调用及 12 条结果；Java/JRE/JDK、Java 安装器、包管理器或安装包下载相关文本命中为 0，读取错误为 0。没有输出其他会话的用户消息内容。

主会话原始日志为 `C:\Users\zlxwl\.codex\sessions\2026\10\19\rollout-2026-10-19T22-50-26-01a154a4-739b-77c3-9c91-6ce5bd7a1fe9.jsonl`。第 3264 行对应 22:48:14.462 的只读文档调用，第 3283 行对应 22:49:35.336 的飞书权限确认。

此前按文件修改时间筛选的补扫范围不足，已由这次不依赖修改时间的全文件窗口扫描补齐。核查只说明现存日志内的操作，不把日志缺失或系统权限限制当作绝对排除依据。

## 归因边界

Java 日志中的自动更新模式和安装选择是直接证据，但 `user` 只是安装器对交互结果的通用描述，不能识别人、自动化程序或父进程。

此次查询无法取得安装进程 12752 的父进程创建记录：Sysmon 日志不存在，Security 日志的原生命令读取返回错误码 5（拒绝访问）。不能把读取失败当作系统没有记录，也不能据此断言某人执行或未执行安装。

工程使用 Python 标准库、SQLite 和网页技术，不依赖 Java。这只能说明 Java 不是工程运行依赖，不能单凭这一点认定是谁安装的。

结合工具审计与系统时间线，现有证据不支持由本次工程任务执行 Java 安装。仍未取得操作者或父进程的直接证据，不能据此断言具体是谁点击了“安装”。

核查过程没有安装、卸载 Java，也没有更改 Java 路径、系统审计或其他系统设置。
