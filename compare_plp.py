# -*- coding: utf-8 -*-
import os, re, time, math, json, uuid, pathlib, yaml
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Tuple
import pandas as pd
from rapidfuzz import fuzz
from playwright.sync_api import sync_playwright

OUT = pathlib.Path("outputs"); OUT.mkdir(exist_ok=True)

def _clean(s:str)->str:
    if not s: return ""
    return re.sub(r"\s+", " ", str(s)).strip()

def _to_num(s:str)->float:
    if not s: return math.nan
    t = re.sub(r"[^\d.,-]", "", s)
    if t.count(",") and t.count("."):
        if t.rfind(",") > t.rfind("."): 
            t = t.replace(".", "").replace(",", ".")
        else: 
            t = t.replace(",", "")
    else:
        t = t.replace(",", "")
    try:
        return float(t)
    except:
        return math.nan

def _read_config(config_path:str)->Dict[str,Any]:
    with open(config_path,"r",encoding="utf-8") as f:
        return yaml.safe_load(f)

def _accept_cookies(page, selector):
    if not selector: return
    try:
        page.wait_for_selector(selector, timeout=4000)
        page.locator(selector).first.click()
    except: 
        pass

def _autoscroll(page, step=1400, max_steps=20, settle_ms=1000):
    prev = 0
    for _ in range(max_steps):
        page.evaluate(f"window.scrollBy(0,{step});")
        page.wait_for_timeout(settle_ms)
        h = page.evaluate("document.body.scrollHeight")
        if h == prev: break
        prev = h
    page.evaluate("window.scrollTo(0,0);")

@dataclass
class Card:
    page: str
    url: str
    idx: int
    model_code: str
    title: str=""
    price_text: str=""
    discount_text: str=""
    members_text: str=""
    installment_text: str=""
    rating_text: str=""
    review_count_text: str=""
    badges_text: str=""
    shipping_text: str=""
    cta_text: str=""
    img_alt: str=""
    img_src: str=""
    shot: str=""

def _extract_model(text:str, patterns:List[str])->str:
    if not text: return ""
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return m.group(0).upper()
    return ""

def _crawl_page(pw, cfg_page:Dict[str,Any], defaults:Dict[str,Any], model_patterns:List[str])->List[Card]:
    name = cfg_page["name"]; url = cfg_page["url"]; sel = cfg_page["selectors"]
    view = defaults.get("viewport", {"width":1440,"height":900})
    shots = cfg_page.get("shots", defaults.get("shots", True))
    max_cards = cfg_page.get("max_cards", defaults.get("max_cards", 120))
    scroll = {**defaults.get("scroll",{}), **cfg_page.get("scroll",{})}

    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport=view, locale=defaults.get("locale","en-GB"))
    page = ctx.new_page()
    page.goto(url, wait_until=defaults.get("wait_until","networkidle"), timeout=120000)
    _accept_cookies(page, cfg_page.get("accept_cookie_selector",""))
    _autoscroll(page, scroll.get("step_px",1400), scroll.get("max_steps",20), scroll.get("settle_ms",1200))

    page.wait_for_selector(sel["card"], timeout=15000)
    cards = page.locator(sel["card"])
    n = min(cards.count(), max_cards)

    outdir = OUT / f"{name}"
    if shots: outdir.mkdir(exist_ok=True)

    rows: List[Card] = []
    for i in range(n):
        c = cards.nth(i)
        def t(s): 
            if not s: return ""
            try:
                el=c.locator(s).first
                return _clean(el.inner_text()) if el.count() else ""
            except: 
                return ""
        def a(s,attr):
            if not s: return ""
            try:
                el=c.locator(s).first
                return el.get_attribute(attr) or "" if el.count() else ""
            except: 
                return ""

        title = t(sel["title"])
        price = t(sel["price"])
        discount = t(sel.get("discount",""))
        members = t(sel.get("members",""))
        inst = t(sel.get("installment",""))
        rating = t(sel.get("rating",""))
        rcount = t(sel.get("review_count",""))
        badges = t(sel.get("badges",""))
        shipping = t(sel.get("shipping",""))
        cta = t(sel.get("cta",""))
        img_alt = a(sel.get("image",""), "alt")
        img_src = a(sel.get("image",""), "src") or a(sel.get("image",""),"data-src")

        model = _extract_model(" ".join([title, badges, img_alt]), model_patterns)

        shot = ""
        if shots:
            fname = f"{i:03d}_{uuid.uuid4().hex[:6]}.png"
            try:
                c.screenshot(path=str(outdir/fname))
                shot = f"{name}/{fname}"
            except: 
                pass

        rows.append(Card(
            page=name, url=url, idx=i, model_code=model,
            title=title, price_text=price, discount_text=discount,
            members_text=members, installment_text=inst,
            rating_text=rating, review_count_text=rcount,
            badges_text=badges, shipping_text=shipping,
            cta_text=cta, img_alt=img_alt, img_src=img_src, shot=shot
        ))

    ctx.close(); browser.close()
    return rows

_FIELDS = [
    ("price_text","가격"), ("discount_text","할인율/세이빙"),
    ("members_text","멤버십"), ("installment_text","할부"),
    ("shipping_text","배송"), ("cta_text","CTA"),
    ("badges_text","배지/프로모션"), ("rating_text","평점"),
    ("review_count_text","리뷰수"), ("title","상품명")
]

def _diff_cards(df_as:pd.Series, df_tb:pd.Series)->Dict[str,Any]:
    out = {"model_code": df_as.model_code}
    for col, _label in _FIELDS:
        left = _clean(df_as[col]); right = _clean(df_tb[col])
        if col=="price_text":
            ln, rn = _to_num(left), _to_num(right)
            if not math.isnan(ln) and not math.isnan(rn):
                out["price_diff_abs"] = rn - ln
                out["price_diff_pct"] = None if ln==0 else (rn-ln)/ln*100
        sim = fuzz.token_set_ratio(left, right) if (left or right) else 100
        out[col+"_sim"] = sim
        out[col+"_asis"] = left
        out[col+"_tobe"] = right
    return out

def _save_html(pair_rows:List[Dict[str,Any]], shots:Dict[str,Tuple[str,str]], path:pathlib.Path):
    html = ["<html><head><meta charset='utf-8'><style>",
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;font-family:Arial}th{background:#fafafa}",
            ".fail{background:#ffe8e8}.ok{background:#eaffea}.center{text-align:center}",
            ".shots{display:flex;gap:8px}.shots img{height:180px;border:1px solid #ddd;border-radius:8px}",
            "</style></head><body>",
            "<h2>PLP 카드 비교 리포트</h2>"]

    for row in pair_rows:
        model = row["model_code"] or "(Unmatched)"
        html.append(f"<h3>{model}</h3>")
        s_as, s_tb = shots.get(model, ("",""))
        if s_as or s_tb:
            html += ["<div class='shots'>",
                     f"<div><div class='center'><b>ASIS</b></div><img src='{s_as}'/></div>",
                     f"<div><div class='center'><b>TOBE</b></div><img src='{s_tb}'/></div>",
                     "</div>"]
        html.append("<table><tr><th>항목</th><th>AS-IS</th><th>TO-BE</th><th>유사도(%)</th></tr>")
        for col,label in _FIELDS:
            asis, tobe, sim = row[col+'_asis'], row[col+'_tobe'], row[col+'_sim']
            cls = "ok" if sim>=85 else "fail"
            html.append(f"<tr class='{cls}'><td>{label}</td><td>{asis}</td><td>{tobe}</td><td class='center'>{sim}</td></tr>")
        if "price_diff_abs" in row and row["price_diff_abs"] is not None:
            pdiff = row["price_diff_pct"]
            html.append(f"<tr><td><b>가격 차이</b></td><td colspan='3'>Abs: {row['price_diff_abs']:.0f} | Pct: {pdiff:+.2f}%</td></tr>")
        html.append("</table><hr/>")
    html.append("</body></html>")
    path.write_text("\n".join(html), encoding="utf-8")

def run_compare(config_path: str = "config.yml") -> dict:
    """크롤링 + 비교 실행, outputs 폴더에 저장 후 요약 반환"""
    cfg = _read_config(config_path)
    defaults = cfg.get("defaults", {})
    patterns = cfg.get("model_patterns", [])

    with sync_playwright() as pw:
        all_cards: List[Card] = []
        for page_cfg in cfg["pages"]:
            all_cards += _crawl_page(pw, page_cfg, defaults, patterns)

    df = pd.DataFrame([asdict(c) for c in all_cards])
    ts = time.strftime("%Y%m%d_%H%M%S")
    raw_path = OUT/f"raw_{ts}.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")

    df_as = df[df.page=="ASIS"].copy()
    df_tb = df[df.page=="TOBE"].copy()

    rep_as = df_as.sort_values("idx").drop_duplicates("model_code", keep="first").set_index("model_code")
    rep_tb = df_tb.sort_values("idx").drop_duplicates("model_code", keep="first").set_index("model_code")

    common = [m for m in rep_as.index if m and m in rep_tb.index]
    pairs = []
    shot_map = {}
    for m in common:
        row = _diff_cards(rep_as.loc[m], rep_tb.loc[m])
        pairs.append(row)
        shot_map[m] = (rep_as.loc[m,"shot"], rep_tb.loc[m,"shot"])

    only_as = [m for m in rep_as.index if m and m not in rep_tb.index]
    only_tb = [m for m in rep_tb.index if m and m not in rep_as.index]

    diff_csv = OUT/f"diff_{ts}.csv"
    pd.DataFrame(pairs).to_csv(diff_csv, index=False, encoding="utf-8-sig")

    html = OUT/f"report_{ts}.html"
    _save_html(pairs, shot_map, html)

    summary_cols = ["model_code"] + [c+"_sim" for c,_ in _FIELDS]
    summary = pd.DataFrame(pairs)[summary_cols] if pairs else pd.DataFrame(columns=summary_cols)

    return {
        "summary": summary,
        "csv_path": str(diff_csv),
        "html_path": str(html),
        "raw_path": str(raw_path),
        "unmatched_as_is": only_as,
        "unmatched_to_be": only_tb
    }

if __name__ == "__main__":
    print(run_compare("config.yml"))
