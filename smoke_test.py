#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke_test.py —— 全套模块导入与核心逻辑冒烟验证(合成数据, 无需真实缓存/浏览器)"""
import os, sys, json, tempfile, csv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

ok = True
def check(name, cond, extra=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        ok = False

# 1) 语法编译
import py_compile, glob
for fp in glob.glob(os.path.join(HERE, "src", "*.py")):
    try:
        py_compile.compile(fp, doraise=True)
        print(f"  [PASS] compile {os.path.basename(fp)}")
    except Exception as e:
        print(f"  [FAIL] compile {os.path.basename(fp)}: {e}")
        ok = False

# 2) 定价引擎(运费感知)
import pricing
cfg = pricing.PricingConfig()
pb = pricing.suggest_price(8.0, 2.0, 3.0, cfg, category="发饰")
# 成本13, 发饰佣金5%+支付0.6% → 价=13/(1-0.056-0.25)=18.73
check("pricing suggest_price revenue", abs(pb.revenue - 18.73) < 0.05, f"={pb.revenue}")
check("pricing net_profit>0", pb.net_profit > 0, f"={pb.net_profit}")
# compute_profit 复盘: 售价20, 进货8, 进货运费2, 发货3, 佣金2%, 支付0.6%
pb2 = pricing.compute_profit(8, 2, 20, 3, 0.02, 0.006)
# 20 -8 -2 -3 -0.4 -0.12 = 6.48
check("compute_profit net", abs(pb2.net_profit - 6.48) < 0.01, f"={pb2.net_profit}")
check("compute_profit margin", abs(pb2.net_margin - 32.4) < 0.1, f"={pb2.net_margin}")

# 3) 选品评分
import models, selector
p = models.Product(item_id="T1", title="网红发夹女气质发卡头饰", price=8.0,
                   express_fee=2.0, sale_count_text="1万+", category="发饰")
s = selector.score_product(p, cfg)
check("selector suggest_price>cost", s["suggest_price"] > s["cost"], f"={s['suggest_price']}")
check("selector has grade", s["grade"] in ("强烈推荐","可上架","谨慎","淘汰"), s["grade"])
print("    选品:", s["grade"], "综合", s["total"], "建议价", s["suggest_price"], "净利", s["net_profit"], f"({s['net_margin']}%)")

# 4) 标题优化 + 上架清单
import title_opt, listing
r = title_opt.optimize(p, extra_terms=["发夹女款"])
check("title_opt has candidates", len(r["candidates"]) > 0)
cs = listing.build_listings([p], cfg, {"T1": ["发夹女款"]})
check("listing count", len(cs) == 1)
check("listing suggest_price", cs[0].suggest_price > 0, f"={cs[0].suggest_price}")
print("    优化标题:", cs[0].title)

# 5) 订单解析 + 利润分析(合成CSV)
orders_csv = os.path.join(tempfile.gettempdir(), "smoke_orders.csv")
with open(orders_csv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["订单号","商品ID","商品名称","数量","实付金额","运费","省份","订单状态"])
    w.writerow(["A1","T1","网红发夹女气质发卡头饰","2","36.0","0","浙江","已发货"])
    w.writerow(["A2","T1","网红发夹女气质发卡头饰","1","18.0","0","新疆","已发货"])
import orders as orders_mod, analysis
os_ = orders_mod.parse_orders(orders_csv)
check("orders parsed", len(os_) == 2, f"={len(os_)}")
os_ = orders_mod.enrich_orders(os_, [p], cfg)  # T1 关联到 p
k = analysis.store_kpi(os_)
check("kpi orders", k["orders"] == 2)
check("kpi linked", k["linked_rate"] == 100.0, f"={k['linked_rate']}")
print("    店铺KPI:", k)
# 新疆单应被标记偏远(发货运费+5)
remote_order = os_[1]
check("remote outbound surcharge", remote_order.outbound_shipping >= cfg.shipping.fixed_fee + cfg.shipping.remote_surcharge,
      f"={remote_order.outbound_shipping}")

# 6) 发货/开票
import shipping as shipping_mod, invoice as invoice_mod
ship = shipping_mod.build_ship_plan(os_, [p])
check("ship rows", len(ship) == 2)
inv = invoice_mod.build_invoice_records(os_, [p])
check("invoice rows", len(inv) == 2)
pv = invoice_mod.purchase_view(inv)
check("purchase view", len(pv) == 1)

# 7) 1688 采集器离线解析(不依赖浏览器)
import collector
sample_html = (
    '<html><head><title>测试发夹 - 1688</title></head><body>'
    '<script>window.__INITIAL_STATE__=JSON.parse("{\\"offer\\":{\\"subject\\":\\"\u7f51\u7ea2\u53d1\u5939\\",'
    '\\"priceRange\\":{\\"priceText\\":\\"2.50-8.00\\"},\\"offerId\\":\\"643237120099\\",'
    '\\"attributes\\":[{\\"name\\":\\"\u51c0\u91cd\\",\\"value\\":\\"0.05kg\\"}]}}");</script>'
    '</body></html>'
)
cd_ = collector.parse_1688_html(sample_html, item_id="643237120099")
check("collector item_id", cd_.get("item_id") == "643237120099", f"={cd_.get('item_id')}")
check("collector name", "发夹" in cd_.get("name", ""), f"={cd_.get('name')}")
check("collector price range min", abs(cd_.get("price", 0) - 2.5) < 0.01, f"={cd_.get('price')}")
# 解析结果能被 loader 正常消费
cp = loader.load_from_json_file if False else None  # 仅类型提示
from loader import parse_cyj_item
cp_obj = parse_cyj_item(cd_)
check("collector->loader weight", abs(cp_obj.weight_kg - 0.05) < 0.001, f"={cp_obj.weight_kg}")
# 本地 HTML 批解析
tmp_html = os.path.join(tempfile.gettempdir(), "smoke_1688.html")
open(tmp_html, "w", encoding="utf-8").write(sample_html)
ch = collector.collect_from_html_files([tmp_html])
check("collector html files", len(ch) == 1, f"={len(ch)}")

print("\n=== SMOKE RESULT:", "ALL PASS" if ok else "HAS FAILURES", "===")
sys.exit(0 if ok else 1)
