# KK 缺失 MOD 批量安装器

这是一个面向 Koikatu（KK）的本地工具，完成以下流程：

1. 扫描 `UserData` 中的人物卡、坐标卡和 Studio 场景。
2. 从 UniversalAutoResolver 数据中提取所需 zipmod GUID。
3. 扫描 `mods` 下已安装 zipmod 的 `manifest.xml`，并识别迁移 GUID。
4. 导出缺失模组 JSON。
5. 根据公开 BetterRepack KKEC 文件索引解析下载地址。
6. 使用 `.partial` 文件断点下载，验证包内 GUID 后原子安装。

工具不会破解登录、读取原 ModResolver 的账号数据、删除现有模组或覆盖同名文件。

## 图形界面

双击：

```text
run_gui.cmd
```

首次启动会在本目录创建独立的 `.venv` 并安装 `msgpack`，不会修改游戏的 Python 环境。

### 便携版 EXE

`dist/KK-Mod-Helper.exe` 是单文件便携版，使用者无需安装 Python。程序会在 EXE 所在目录创建 `data` 和 `reports`，因此请把 EXE 放在普通可写文件夹中，不要直接放入 `Program Files`。

开发者需要重新打包时，双击 `build_portable.cmd`。构建结果不会提交到 Git。

界面不再要求用户选择 JSON 文件：

1. “追加解析”复用角色卡和已安装 MOD 缓存，只读取新增、删除或发生变化的文件。
2. “完整解析”忽略扫描缓存，重新检查全部角色卡、场景和已安装 MOD。
3. 两种方式都会自动生成 JSON、解析下载地址并载入任务表。
4. 双击任务行可勾选或取消；点击“并发下载选中项”后最多同时下载 4 个文件。
5. 失败任务会写入同目录的 `_install_results.json`，再次启动后仍会显示；可点击“只选失败任务”重新下载。
6. 选择过的游戏目录会保存在本地 `data/settings.json`，下次启动自动恢复；该文件不会上传到 Git。

安装位置固定为：

```text
<游戏目录>\mods\ModResolver_Downloads\<网站分类>\
```

`.zipmod` 不会被解压。

## 命令行

创建环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

初始化当前目录映射：

```powershell
.\.venv\Scripts\python.exe -m kk_mod_helper.cli bootstrap `
  --report "<现有缺失模组报告.json>" `
  --url-cache "<现有公开 URL 索引.json>"
```

扫描：

```powershell
.\.venv\Scripts\python.exe -m kk_mod_helper.cli scan --game "<游戏目录>"
```

解析下载地址：

```powershell
.\.venv\Scripts\python.exe -m kk_mod_helper.cli resolve `
  --report ".\reports\missing_mods_时间.json" `
  --url-cache ".\data\KKEC_urls.json"
```

安装必须明确确认：

```powershell
.\.venv\Scripts\python.exe -m kk_mod_helper.cli install `
  --game "<游戏目录>" `
  --report ".\reports\missing_mods_时间_resolved.json" `
  --confirm INSTALL
```

## 解析策略与限制

- 已知 GUID 优先从本地 `catalog.json` 精确解析。
- 现有报告包含文件名时，按文件名和分类精确匹配公开文件索引。
- 新 GUID 可以通过 HTTP Range 只读取远程 zipmod 的中央目录和 `manifest.xml`，不必下载整个文件进行识别。
- 图形界面解析下载地址使用 8 路并发，每个 GUID 最多选择 5 个高相关候选，全局最多探测 40 个文件。
- 公开 URL 索引使用 8 路并发更新，并降低界面日志刷新频率；更新中断时会合并保留旧索引。
- 下载使用 4 路并发，支持 `.partial` 断点续传；每个失败任务自动尝试 3 次。
- 无法可靠解析的 GUID 会保留为 `not_found`，不会猜测下载或安装。
- 第一版只处理 Sideloader `.zipmod`；不会自动安装 BepInEx 插件 DLL。

相关公开格式参考：

- [IllusionMods/BepisPlugins Sideloader manifest 实现](https://github.com/IllusionMods/BepisPlugins/blob/master/src/Core_Sideloader/Core.Sideloader.Manifest.cs)
- [IllusionMods/KKManager](https://github.com/IllusionMods/KKManager)
- [BetterRepack KKEC 公开目录](https://sideload.betterrepack.com/download/KKEC/)
