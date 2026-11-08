# PDD 商家工作台 / 1688 货源采集上架工具

面向拼多多（PDD）商家的桌面工具，用于把 1688 货源**采集 → 定价 → 生成上架清单 → 一键发布**到拼多多后台。
本仓库只含**源代码**；登录态、抓取数据与打包产物均不入库（见 `.gitignore`）。

## 功能

- **1688 采集**（`src/collector.py`）：在线登录态抓取主图、详情图（含 `itemcdn.tmall.com` 异步描述 CDN 补全）、SKU 维度/组合、属性、逐款进价与库存、运费。
- **定价**（`src/pricing.py` + `pricing_config.json`）：保本反算 + 净利率保底，输出单买价、拼单价、商品参考价。
- **上架清单**（`src/listing.py` + `src/loader.py`）：合并图片、生成 CSV 清单。
- **PDD 发布**（`src/pdd_publish.py`）：自动填 SKU 价格/库存/单买价/拼单价、商品参考价、属性、图片上传。
- **一键搬迁脚本**（`migrate_one.py`）：`python migrate_one.py <1688链接或商品ID>` 完成采集→清单→核对报告；支持多链接与 `--no-collect`（复用本地数据）、`--summary-only`（只出汇总总表）。
- **桌面 GUI**（`src/gui.py`）：左侧竖向图标侧栏（可折叠）、采集/上架/资料核对等模块。

## 目录

```
src/                核心模块（采集 / 定价 / 上架 / 发布 / GUI）
migrate_one.py      单品一键全量搬迁脚本
app_main.py         主程序入口
launcher_main.py    免安装版启动器
make_icon.py        图标生成
build_portable.py   打包免安装绿色版（PyInstaller onedir）
PDDWorkbench.spec   PyInstaller 构建配置
pricing_config.json 定价参数（佣金/运费/净利率）
publish_config.json 发布参数（库存上限/图片上限/折扣等）
assets/             应用图标素材
```

## 下载免安装版（GitHub Releases）

免安装绿色版（双击即用，无需 Python / pip）由 **GitHub Actions 自动构建**，并作为
**Release 附件**分发，不再随源码仓库提交打包产物。提供两个版本：

| 版本 | 标签 | 适用场景 |
|---|---|---|
| **纯代码版（默认）** | [`v1.0.0`](https://github.com/aleilei-8/pdd-seller-suite/releases/tag/v1.0.0) | 正式使用。**不含任何业务数据**。 |
| **示例数据 Demo 版** | [`demo-v1.0.0`](https://github.com/aleilei-8/pdd-seller-suite/releases/tag/demo-v1.0.0) | 想直接体验界面与数据格式。内置【示例/脱敏】数据。 |

> ⚠️ **纯代码版首次使用需自行采集**：该版本只含程序本身，打开后 `data/` 为空，
> 请先在软件的「采集1688链接」里采集你自己的真实 1688 商品（结果落盘到本地 `data/`，不入库），
> 才能看到货源、定价与上架清单。不要指望开箱即有现成商品。
>
> 🧪 **Demo 版说明**：内置的 `collected_1688.json` / `freight_rules.*` / `sample_*` 等
> 均为【示例/脱敏】占位数据（商品ID、价格、图片、运费都是示例），**不是真实货源或真实运费**，
> 仅供演示流程与数据格式，正式运营请用纯代码版并采集自有商品。

下载步骤：
1. 打开仓库的 **Releases** 页面：仓库 → `Releases` → 选择上面任一版本。
2. 下载对应 `pdd-workbench-*-<版本>.zip`，解压整个文件夹即可使用。
3. 首次运行双击「启动工作台.exe」，并按提示登录一次 1688 / 拼多多。

本地也可自行打包标准版：`python build_portable.py`（产物在 `dist/PDD工作台_免安装版/`）。

## 使用

1. 安装依赖：`pip install playwright customtkinter pandas openpyxl`
2. 准备 1688 登录态：在 GUI 点「登录1688」扫码，会话落盘到本地 `data/`（不入库）。
3. 单品搬迁：`python migrate_one.py https://detail.1688.com/offer/<ID>.html`
4. 发布：在 GUI 打开工作台，点「发布」终验（本工具不自动提交到拼多多）。

## CI 与发版

- 工作流：`.github/workflows/build-portable.yml`，固定跑在 `windows-latest`
  （免安装版是 Windows 专属：PyInstaller onedir → `.exe` + VBScript 启动器）。
- **自动发布**：推送一个 `v*` 标签（如 `v1.0.0`）即触发构建，并把 zip 作为该标签的
  Release 附件发布：
  ```bash
  git tag v1.0.0
  git push origin v1.0.0
  ```
- **手动构建验证**：在 Actions 页点 `Build Portable` → `Run workflow`，构建出的 zip
  会作为 Artifact 提供下载（14 天保留），不会创建 Release。
- 标准版不含登录态 / 离线浏览器，可安全公开分发；如需离线浏览器用
  `python build_portable.py --with-browsers` 本地生成。

## 说明

- 本工具用于**商家自有店铺**的货源管理与上架效率提升。
- 采集与发布须遵守 1688 / 拼多多平台规则；请仅用于你有权操作的账号与商品。
- 登录态、`data/`、`dist/` 等敏感/大体积内容已通过 `.gitignore` 排除，**切勿手动提交**。

## License

[MIT](LICENSE)
