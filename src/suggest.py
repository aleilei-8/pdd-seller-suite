#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
suggest.py —— 拼多多搜索下拉联想词抓取(免费推流信号)

原理:
  拼多多下拉联想接口 search_suggest 需要反爬参数 anti_content(由页面 JS 实时生成),
  无法直接 HTTP 调用。因此用 Playwright 真实浏览器:
    1. 打开移动端首页(搜索条是 div, 非 input)
    2. 定位顶部可见搜索条并原生点击 → 打开搜索浮层(浮层内才是真正的 input)
    3. 在浮层 input 输入关键词 → 页面自动带 anti_content 发起 search_suggest
    4. 监听 response, 抓取 search_suggest 的 JSON, 解析 suggest 数组
  一次浏览器会话可批量查询多个关键词, 复用浮层。

返回: list[str] 联想词(买家真实搜索词, 即拼多多"推流词")
说明: 这是免费信号, 无需任何付费 API。
"""
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

MOBILE_HOME = "https://mobile.yangkeduo.com/"
OVERLAY_INPUT = "input[type=search]"
SUGGEST_URL_FRAGMENT = "search_suggest"


class SuggestClient:
    """维护一个浏览器会话, 批量抓取下拉联想词"""

    def __init__(self, headless: bool = True):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=headless)
        self.ctx = self.browser.new_context(
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Mobile/15E148 Safari/604.1"),
        )
        self.page = self.ctx.new_page()
        self._captured = []
        self.page.on("response", self._on_resp)
        self.page.goto(MOBILE_HOME, wait_until="networkidle", timeout=30000)
        self.page.wait_for_timeout(1200)
        self._open_overlay()

    def _on_resp(self, resp):
        u = resp.url
        if SUGGEST_URL_FRAGMENT in u:
            try:
                if "json" in resp.headers.get("content-type", ""):
                    self._captured.append(resp.text())
            except Exception:
                pass

    def _open_overlay(self):
        """定位顶部可见搜索条(宽>200, 短占位文本)并原生点击, 打开搜索浮层。
        若浮层未出现则重试, 保证后续 suggest 拿到可见 input。"""
        for _ in range(3):
            self.page.evaluate("""()=>{
                const els=[...document.querySelectorAll('*')];
                let best=null;
                for(const el of els){
                    const t=(el.textContent||'').trim();
                    const r=el.getBoundingClientRect();
                    const vis = r.width>0 && r.height>0 &&
                                getComputedStyle(el).visibility!=='hidden' &&
                                getComputedStyle(el).display!=='none';
                    if(vis && r.top<110 && r.width>200 && t.length>0 && t.length<10){
                        if(!best || (r.width*r.height) < (best.r.width*best.r.height)) best={el,r,t};
                    }
                }
                if(best){ best.el.click(); }
            }""")
            try:
                self.page.locator(OVERLAY_INPUT).first.wait_for(state="visible", timeout=2000)
                return
            except Exception:
                self.page.wait_for_timeout(800)

    def suggest(self, keyword: str, timeout: int = 8000) -> list:
        """抓取单个关键词的下拉联想词。

        注意: 拼多多联想请求在 headless 下有明显延迟(输入后约 3-5s 才发出),
        且 app 有输入防抖, 故 timeout 默认 8s; 等待时长不足会导致本词请求"漏到"下一词。
        """
        self._captured = []
        inp = self.page.locator(OVERLAY_INPUT).first
        # 确保浮层已打开(input 可见), 否则重试点击搜索条
        try:
            inp.wait_for(state="visible", timeout=4000)
        except Exception:
            self._open_overlay()
        if not (inp.count() and inp.is_visible()):
            return []
        inp.click()
        # 清空: 受控组件必须用键盘全选删除, fill 只改 DOM 不改 React state
        inp.press("Control+a")
        inp.press("Backspace")
        self.page.keyboard.type(keyword, delay=40)

        # 关键: 拼多多联想请求在 headless 下会滞后(首次输入往往不发请求),
        # 若本次未取到该关键词的结果, 就删掉末字符再重输一次,
        # 触发新一轮 onChange 把请求"唤醒"发出(实测 1-2 次即可生效)。
        got = None
        for i in range(max(1, int(timeout / 1000))):
            time.sleep(1.0)
            for t in self._captured:
                try:
                    d = json.loads(t)
                except Exception:
                    continue
                if d.get("query") == keyword and d.get("suggest"):
                    got = d
            if got:
                break
            self.page.keyboard.press("Backspace")
            if keyword:
                self.page.keyboard.type(keyword[-1], delay=40)
        if got:
            return got.get("suggest", [])
        # 兜底: 返回最后一个含 suggest 的响应
        for t in reversed(self._captured):
            try:
                d = json.loads(t)
                if d.get("suggest"):
                    return d["suggest"]
            except Exception:
                continue
        return []

    def suggest_batch(self, keywords: list, delay: float = 0.4) -> dict:
        """批量抓取, 返回 {keyword: [联想词]}"""
        out = {}
        for kw in keywords:
            out[kw] = self.suggest(kw)
            time.sleep(delay)
        return out

    def close(self):
        try:
            self.page.close()
            self.ctx.close()
            self.browser.close()
            self.pw.stop()
        except Exception:
            pass


def fetch_suggestions(keyword: str) -> list:
    """一次性抓取单个关键词(单次会话, 用完即关)"""
    c = SuggestClient(headless=True)
    try:
        return c.suggest(keyword)
    finally:
        c.close()


# ===== 推流词清洗三道关 =====
# 第 1 关: 跨品类词根(联想会把同名词的其他类目带进来, 例: 搜"皇冠"→"丰田皇冠车用品")
CROSS_CATEGORY_NOISE = {
    "车用品", "汽车", "丰田", "本田", "宝马", "奔驰", "奥迪", "大众", "特斯拉",
    "轮胎", "机油", "电动车", "摩托车", "自行车",
    "手机", "手机壳", "平板", "电脑", "笔记本", "钥匙", "门锁", "空调",
    "冰箱", "洗衣机", "家具", "瓷砖",
    "各种各样", "幼儿园", "道具", "小学生",
}
# 第 2 关: 领域词根(说明联想已经跑到完全无关的行业)
DOMAIN_NOISE = {
    # 食品/生鲜
    "曲奇", "巧克力", "饼干", "糖果", "牛奶", "零食", "茶叶", "火锅", "烤肉",
    "泡面", "饮料", "奶茶", "咖啡", "月饼", "坚果", "榴莲", "车厘子",
    # 数码家电
    "数据线", "充电宝", "蓝牙", "耳机", "音响", "监控", "摄像头", "路由器", "显卡",
    # 美妆工具/假睫毛
    "双眼皮", "美目", "假睫毛", "睫毛夹", "指甲油", "美甲", "纹身", "染发", "烫发",
    # 文具书包
    "校服", "书包", "文具", "铅笔", "橡皮", "试卷",
    # 玩具模型
    "直升机", "积木", "拼装", "无人机", "奥特曼",
    # 宠物
    "猫粮", "狗粮", "猫砂", "宠物",
    # 农资
    "种子", "花盆", "化肥", "农药",
    # 节庆/无关场景
    "春联", "红包", "寿宴", "丧事",
    # 电动工具/五金(多义词重灾区: "水钻"既是饰品也是水钻打孔机)
    "打孔机", "打孔", "防尘", "防水袋", "支架", "万能", "电钻", "钻头", "胶枪",
    "车床", "砂轮", "切割", "焊接", "工业", "螺丝", "电锤", "配件万能",
    # 玩具/拼装
    "拼图", "积木", "奥特曼", "芭比", "过家家", "遥控",
}
# 第 3 关: 泛泛修饰词(对搜索无增益, 白占标题字数, 拼多多也不据此分词加权)
GENERIC_NOISE = {
    "各种各样", "自己动手", "手工制作", "直播", "主播", "装扮", "大全",
    "批发", "包邮", "正品", "官方", "旗舰", "定制", "现货", "特价", "清仓",
    "上档次", "轻奢", "高档", "ins风", "网红", "爆款", "新款", "一件",
    "整容", "变性", "不掉", "甩不掉", "重复使用", "神器", "置物",
}
TERM_NOISE = CROSS_CATEGORY_NOISE | DOMAIN_NOISE | GENERIC_NOISE

# 人群/客群标记: 推流词里出现 = 该词面向特定人群。
# 若商品侧完全没有这个人群的标记, 说明联想跑到了别的客群, 会引来错误的流量。
PERSONA_FLAGS = {
    "male":  ("男士", "男款", "男生", "中年男", "背头"),
    "elder": ("老年", "老人", "中老年", "老年人"),
    "bride": ("新娘", "婚鞋", "秀禾", "敬酒服"),
    "baby":  ("婴儿", "宝宝", "新生儿", "产妇", "尿不湿"),
    "kids":  ("女童", "男童", "小学生", "校服", "周岁"),
}

# 数值/人群区间词: "45到50岁" "6到12岁" 这类对商品无标识意义
NUM_RANGE_RE = re.compile(r"\d+\s*(?:到|-|至|—)\s*\d+")
AGE_RE = re.compile(r"\d+\s*岁|\d+\s*后|老年|中年人")
# 首尾为数字(如 "6到12岁" "3件套"), 说明是规格串而非有效词
EDGE_DIGIT_RE = re.compile(r"^\d|\d$")


def clean_suggest_terms(suggestions: list, max_terms: int = 12, max_len: int = 7) -> list:
    """把下拉联想短语提炼为可用于标题的推流长尾词。

    联想词形如 "女士发卡发夹 年轻漂亮 2026款 网红", 直接入标题太长且含时效词,
    故按空格切碎 -> 剔除年份/跨品类/领域/泛词/数值区间/单字/非语种 -> 按频次排序。
    """
    freq = {}
    for s in suggestions or []:
        for seg in re.split(r"\s+", str(s or "")):
            seg = seg.strip()
            if not (2 <= len(seg) <= max_len):
                continue
            if re.search(r"(20\d{2}|19\d{2})", seg):            # 年份/年份款, 会过期
                continue
            if re.search(r"[^\u4e00-\u9fa5A-Za-z0-9]", seg):    # 只留中英文数字
                continue
            if re.fullmatch(r"[\dA-Za-z]+", seg):               # 纯英数无意义
                continue
            if EDGE_DIGIT_RE.search(seg):                       # 首尾数字=规格串
                continue
            if NUM_RANGE_RE.search(seg) or AGE_RE.search(seg):  # 数值/人群区间
                continue
            if any(n in seg for n in TERM_NOISE):               # 跨品类/领域/泛词
                continue
            freq[seg] = freq.get(seg, 0) + 1
    ordered = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [w for w, _ in ordered[:max_terms]]


REJECT = -1.0   # 硬剔除(不参与阈值比较)

# 通用属性/场景修饰词: 这些词在商品侧往往没有对应的字面, 但它们是真实的高频搜索词,
# 不能因为"无支撑"就误杀。与场景类别词(办公用品/康复肩托)不同, 它们只是修饰。
ATTR_WHITE_TOKENS = {
    # 规格/材质/功能
    "一次性", "医用", "透气", "夏天", "夏日", "冬季", "春秋", "薄款", "加厚", "超薄",
    "超强", "强力", "高温", "防水", "防滑", "可洗", "亲肤", "便携", "隐形", "无痕",
    "加大", "大码", "小号", "长效", "弹力", "磨砂", "光面",
    # 泛人群/泛场景(不互斥)
    "学生", "孕妇", "运动", "瑜伽", "跑步", "舞蹈", "演出", "生日", "party",
    # 效果
    "显瘦", "显白", "聚拢", "防走光", "防凸点", "聚拢款", "超自然",
}

# 通用款式/部位词: 描述卖点但商品侧未必有对应字面, 误杀成本高, 单独放行
STYLE_WHITE_TOKENS = {
    "后脑勺", "刘海", "碎发", "盘发", "丸子头", "马尾", "边夹", "侧边", "抓夹",
    "古风", "汉服", "森系", "复古", "蝴蝶结", "珍珠", "水钻", "卡通", "可爱",
    "高颜值", "的气质", "一字", "纽扣",
}

# 太泛的词不能作为"类目支撑": 商品属性里常有"用品/工具", 会让
# "双面胶办公用品" 之类的跨类目词假装获得支撑而漏网。
WEAK_SUPPORT = {
    "用品", "产品", "神器", "工具", "套装", "专用", "配件", "家用", "收纳",
}


def _has_support(rest: str, pool: str) -> bool:
    """rest 是否存在 ≥2字连续片段, 且在商品侧池子里(排除过泛的弱支撑词)"""
    for i in range(len(rest) - 1):
        bg = rest[i:i + 2]
        if bg in pool and bg not in WEAK_SUPPORT:
            return True
    return False


def _persona_mismatch(term: str, product_words) -> bool:
    """人群错配: 推流词面向某客群, 而商品侧完全没有该客群标记。

    例: 儿童发饰商品 → "男士发箍背头" / "头饰新娘"  必须剔除,
        因为标题一旦带上这些词, 吸引来的是完全不会下单的人群。
    """
    joined = "".join(product_words or [])
    if not joined:
        return False
    for _grp, flags in PERSONA_FLAGS.items():
        if any(f in term for f in flags):
            # 商品自己就是这个客群 → 不算错配
            if not any(f in joined for f in flags):
                return True
    return False


def score_relevance(term: str, product_words, core_words=None,
                    support_text: str = "") -> float:
    """单条推流词与商品的相关性分(≥min_score 才入标题; <0 表示硬剔除)。

    判定顺序(由严到宽):
      0. 人群错配(男士/老年/新娘 vs 女童商品) → REJECT
      1. 命中核心词(≥2字):
         rest(去掉核心词后的尾巴) 若满足任一 → 通过:
            a. rest 有与商品侧(含原标题)≥2字的连续片段重合   e.g. "胸贴一次性无痕"
            b. rest 含通用属性词(一次性/医用/透气...)          e.g. "一次性乳贴医用"
            c. rest ≤2字(短尾巴误杀成本高, 直接放行)          e.g. "双面胶 学生"
         否则 rest ≥3字且全无支撑 → REJECT(典型: "水滴流水摆件" "双面胶办公用品")
      2. 未命中核心词 → 与商品侧词的字重合率 overlap 最大值 × 1.5
    """
    if _persona_mismatch(term, product_words):
        return REJECT

    words = [w for w in (product_words or []) if w]
    pool = (support_text or "") + "".join(words)
    cores = [c for c in (core_words or []) if c and len(c) >= 2]
    hits = [c for c in cores if c in term]
    if hits:
        rest = term
        for c in hits:
            rest = rest.replace(c, "")
        if not rest:
            return 2.0
        # a. ≥2字连续片段获得支撑(排除过泛的弱支撑词)
        if _has_support(rest, pool):
            return 2.0
        # b. 通用属性/款式修饰词
        if any(t in rest for t in ATTR_WHITE_TOKENS | STYLE_WHITE_TOKENS):
            return 1.6
        # c. 短尾巴放行(≤2字), 长尾巴判定为跑偏
        return 2.0 if len(rest) <= 2 else REJECT

    chs = set(term)
    if not chs:
        return 0.0
    best = 0.0
    for w in words:
        inter = len(set(w) & chs)
        if not inter:
            continue
        rat = inter / len(chs)          # 推流词里有多大比例出现在该商品词里
        if rat > best:
            best = rat
    return round(best * 1.5, 3)


def filter_relevant_terms(terms, product_words, core_words=None,
                          min_score: float = 0.55, return_dropped: bool = False,
                          support_text: str = ""):
    """保留与商品同类目的推流词, 剔除同形异义污染。

    min_score=0.55 等价于要求未命中核心词的词, 其字重合率 ≥ ~0.37。
    """
    kept, dropped = [], []
    for t in terms or []:
        sc = score_relevance(t, product_words, core_words, support_text)
        if sc >= min_score:
            kept.append(t)
        else:
            dropped.append((t, sc))
    return (kept, dropped) if return_dropped else kept


def build_enriched_terms(raw_suggestions: list, core_words, product_words=None,
                         max_terms: int = 10, min_score: float = 0.55,
                         support_text: str = "", return_dropped: bool = False):
    """一步到位: 原始联想词 → 清洗 → 相关性过滤 → 最终可入标题的推流词。

    support_text 建议传入商品原标题(连同 '/' 等符号无害), 用于给
    "核心词+尾巴" 形式的长尾词提供 ≥2字连续片段的支撑判定。
    """
    words = list(core_words or []) + list(product_words or [])
    # 商品侧参与比较的词去重去空, 短于一字的忽略
    pw = [w for w in dict.fromkeys(words) if w and len(w) >= 2]
    cand = clean_suggest_terms(raw_suggestions, max_terms=24)
    kept, dropped = filter_relevant_terms(cand, pw, core_words,
                                          min_score=min_score,
                                          support_text=support_text,
                                          return_dropped=True)
    return (kept[:max_terms], dropped) if return_dropped else kept[:max_terms]


def suggest_for_keywords(keywords: list, headless: bool = True, verbose: bool = False) -> dict:
    """批量抓取多个关键词的联想词, 返回 {keyword: [联想词]}"""
    c = SuggestClient(headless=headless)
    try:
        return c.suggest_batch(keywords)
    finally:
        c.close()


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "发夹"
    print(f"抓取 '{kw}' 的下拉联想词:")
    words = fetch_suggestions(kw)
    for w in words:
        print("  ", w)
    print("\n提炼为推流长尾词:")
    print("  ", clean_suggest_terms(words))
