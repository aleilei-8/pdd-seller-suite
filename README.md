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

## 使用

1. 安装依赖：`pip install playwright customtkinter pandas openpyxl`
2. 准备 1688 登录态：在 GUI 点「登录1688」扫码，会话落盘到本地 `data/`（不入库）。
3. 单品搬迁：`python migrate_one.py https://detail.1688.com/offer/<ID>.html`
4. 发布：在 GUI 打开工作台，点「发布」终验（本工具不自动提交到拼多多）。

## 说明

- 本工具用于**商家自有店铺**的货源管理与上架效率提升。
- 采集与发布须遵守 1688 / 拼多多平台规则；请仅用于你有权操作的账号与商品。
- 登录态、`data/`、`dist/` 等敏感/大体积内容已通过 `.gitignore` 排除，**切勿手动提交**。

## License

[MIT](LICENSE)
