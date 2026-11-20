# WeChat-MP-Topic-Monitor

用自己的专题词库监测多个微信公众号。可按日期补抓历史发文、增量检查更新，并在本机查看入选理由、正文覆盖情况和待复核文章。文章、专题规则和采集进度保存在 SQLite，初次启动时账号和词库均为空。

## Windows 便携版

1. **解压并打开。** 将 Windows x64 发行 ZIP 完整解压到可写文件夹，双击 `启动自动监测.cmd`。包内已附 Python 和依赖，浏览器会打开本机设置页。
2. **添加公众号。** 在“抓文章与设置”填写自己取得的极致了 API Key，有附加码时一并填写。添加公众号的准确名称，启用需要采集的账号并保存。接口地址默认使用极致了，如需调整，展开页面底部的配置即可编辑。
3. **添加关键词。** 在“关键词”填写统一入选词、疑似词和排除词。只适用于某个号的入选词或排除词，可加在该号名下。输入后按回车或点添加，点 × 可移除。保存并重算后，库存文章会按新规则重新分类。
4. **抓文章并查看结果。** 首次补抓可选较短的日期范围并设页数上限。在免费预览中核对已覆盖日期、预计页数及费用，再确认抓取。在“筛选结果”分栏查看入选和疑似稿件，在“全部文章”查阅所有稿件、导出 Excel 用 CSV。需要复核时，可从页面下载 Markdown。接口试拉、确认后的采集和增量检查可能产生第三方费用，实际扣费以服务商账单为准。

断网时也可查看已有文章、编辑关键词并导出复核材料。Markdown 复核稿由本机生成。

程序仅监听本机回环地址。默认端口占用时另选空闲端口，启动命令会显示实际地址。首次启动使用手动采集。迁移时先双击 `停止并取消自启.cmd`，待程序结束后再复制整个解压目录。凭据位于本机 `data/config.json`，请妥善保管。

## 从源码启动

使用 Python 3.10 或更高版本。在项目根目录依次运行（Windows PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe monitor.py start
```

在 macOS 或 Linux 上，新建虚拟环境后用 `.venv/bin/python` 安装同一份依赖、运行同一入口。源码初始目录只含程序和示例，首次启动时生成 `data/`。如需 Windows 登录后自动启动，可运行 `python monitor.py start --autostart`。状态查询和停止命令分别为 `python monitor.py status` 和 `python monitor.py stop`。启动失败可查看 `data/monitor.log`。

## 导出与分享

双击 `导出分发包.cmd` 选择模式。便携安装导出的包继续携带已校验的运行库；源码安装导出的包使用接收端 Python。所有模式均包含通用源码、公共预设与许可证。

| 模式 | 收件人得到的内容 |
| --- | --- |
| 1 · 纯程序 | 初始模板和公共预设。首次启动为空库，适合公开分发。 |
| 2 · 历史库与无凭据配置 | 保留账号、专题规则、历史与全量 CSV。清空采集 Key、附加码、Webhook 和 AI Key，并切换为手动检查。文章和账号仍属于私人资料。 |
| 3 · 历史库与私人配置 | 保留历史和全部配置，包括凭据及检查间隔。确认后生成，只交给有权使用这些凭据的人。 |

两种历史包均使用 SQLite 在线备份，将待复核稿件、正文和抓取断点一并保存。CSV 与 SQLite 快照取自同一状态。模式 2 只清除专用凭据字段，请自行核对在专题提示词、正文、备注等自由文本中手动录入的信息。

## 构建 Windows x64 离线包

在可联网的 Windows x64 上，用带 pip 的标准 Python 运行：

```powershell
python -B tools/build_release.py
```

构建器下载固定版本的 CPython 嵌入包，校验其 SHA-256，并从锁定的 wheel 安装依赖。所有运行库均在临时目录组装。构建器会解压成品，启动程序，访问四个页面和所需的脚本，再停止临时服务。通过后在项目上一级生成 ZIP 并打印 SHA-256。可加 `--output 文件名.zip` 指定新文件。离线构建可同时给出 `--python-zip` 和 `--wheelhouse`。前者接收官方 `python-3.12.10-embed-amd64.zip`，后者接收 `tools/windows-runtime.lock` 对应的 wheel 目录。接收端解压后即可使用。

## 文件与扩展

- `config.example.json` 给出账号、接口、机器人通知和 AI 连接字段。工作配置按相同结构保存在本机。
- `presets/` 含空白专题和政策研究示例。预设仅提供起点，当前保存的专题规则随历史库迁移。
- `app/topic_engine.py` 实现通用词条与正则判定。`app/source_adapters.py` 定义历史接口协议，新增适配器时实现端点校验和逐页请求，在注册表登记后通过配置的 `provider` 选择。
- `data/wechat_mp.db` 保存文章、判定、断点和专题规则，`data/` 及便携运行库由 Git 忽略。

## 作者与许可

WeChat-MP-Topic-Monitor v1.0.0，作者占位为“adrcjwyf”，仓库占位为 `https://github.com/adrcjwyf/wechat-mp-topic-monitor`。发布者可在 `app/__version__.py` 设置正式署名和仓库地址，并同步更新 README、NOTICE 与版权声明。项目新增代码使用 Apache-2.0；保留的 WeRSS 代码使用 MIT。分发时保留 `LICENSE`、`NOTICE` 和 `app/LICENSE`，便携版内还附 Python 与依赖的许可证。

