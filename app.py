# ==== Playwright 브라우저 부트스트랩 (Cloud 안전모드) ====
import os, sys, subprocess, pathlib

# 브라우저 캐시 경로 고정
BROWSERS_DIR = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/home/appuser/.cache/ms-playwright"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR

def _chromium_missing() -> bool:
    p = pathlib.Path(BROWSERS_DIR)
    if not p.exists():
        return True
    patterns = [
        "**/chromium-*/chrome-linux/headless_shell",
        "**/chromium-*/chrome-linux/chrome",
        "**/chrome-linux/headless_shell",
        "**/chrome-linux/chrome",
    ]
    for pat in patterns:
        if list(p.glob(pat)):
            return False
    return True

def _ensure_playwright_chromium():
    try:
        if _chromium_missing() or os.environ.get("FORCE_PW_INSTALL") == "1":
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)
        except Exception as e2:
            raise RuntimeError(f"Playwright 브라우저 설치 실패: {e} / {e2}")

_ensure_playwright_chromium()
PLAYWRIGHT_LAUNCH_KW = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
# ==== /부트스트랩 끝 ====


# app.py — Streamlit + Playwright (UK ↔ SG)
# Network-first + DOM fallback + deep JSON (__NEXT_DATA__/window)
# GraphQL POST sniffing + text/plain JSON 파싱 + PDP 폴백
# 타임아웃/재시도/리소스차단 + 첫 모델 강제 비교 + SKU(점 앞 베이스) 허용
# Learn More 버튼/텍스트 판정: CSS 메트릭 기반(_classify_cta)
# Compare: 다국어/체크박스/label 승격/role/data-testid 기반 강인 탐색 + 상대좌표 위치 계산

import sys, asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import re, time, json
import pandas as pd
import streamlit as st
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# ========================= UI =========================
st.set_page_config(page_title="PLP 카드 비교 (UK ↔ SG)", layout="wide")
st.markdown("# 🧩 PLP 상품 카드 비교")
st.caption("Network-first + DOM fallback + deep JSON + GraphQL sniffing (retry/timeout)")

# --- URL 상태/히스토리 관리 ---
if "url_pairs" not in st.session_state:
    st.session_state.url_pairs = []

# 쿼리스트링 프리필 (?as=https://...&to=https://...)
try:
    qs = st.experimental_get_query_params()
except Exception:
    qs = {}
prefill_as = qs.get("as", [""])[0] if isinstance(qs, dict) else ""
prefill_to = qs.get("to", [""])[0] if isinstance(qs, dict) else ""

with st.sidebar:
    st.markdown("### 실행 옵션")
    fast_mode  = st.toggle("⚡ Fast Mode (이미지/폰트/애널리틱스 차단)", value=False)
    debug_log  = st.toggle("🐞 Debug 로그", value=True)
    viewport_choice = st.selectbox("뷰포트", ["desktop 1440x900", "desktop 1280x900", "mobile 390x844"], index=0)
    take_screens = st.checkbox("카드 스크린샷 저장", value=False)
    timeout_sec = st.number_input("⏱ 페이지 대기 시간(초)", min_value=5, max_value=60, value=30, step=1)
    retries     = st.slider("🔄 재시도 횟수", min_value=0, max_value=3, value=1)

# --- URL 입력 (폼) ---
with st.form("url_input_form", clear_on_submit=False):
    st.markdown("### 비교할 PLP 페이지 URL 입력")
    url_as = st.text_input("AS-IS URL (예: UK/원본)", value=prefill_as, placeholder="https://www.lg.com/uk/tvs-soundbars/oled-evo/")
    url_tb = st.text_input("TO-BE URL (예: SG/비교대상)", value=prefill_to, placeholder="https://www.lg.com/sg/tvs-soundbars/oled-evo/")

    cols = st.columns([1,1,1,2])
    with cols[0]:
        add_pair = st.form_submit_button("➕ 페어 추가")
    with cols[1]:
        clear_pairs = st.form_submit_button("🧹 초기화")
    with cols[2]:
        run_btn = st.form_submit_button("▶ 실행 (Network-first)")

# 입력 유효성 보조: 비어있으면 실행 방지
def _valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return bool(p.scheme in ("http", "https") and p.netloc)
    except Exception:
        return False

# 폼 액션 처리
if add_pair:
    if _valid_url(url_as) and _valid_url(url_tb):
        st.session_state.url_pairs = [{"as": url_as.strip(), "to": url_tb.strip()}]  # 단일 페어만 유지
        try:
            st.experimental_set_query_params(as=url_as.strip(), to=url_tb.strip())
        except Exception:
            pass
        st.success("URL 페어를 추가했습니다.")
    else:
        st.error("두 URL 모두 유효한 형식이어야 합니다 (http/https 포함).")

if clear_pairs:
    st.session_state.url_pairs = []
    try:
        st.experimental_set_query_params()
    except Exception:
        pass
    st.info("저장된 URL 페어를 초기화했습니다.")

# 폼 아래 미리보기
if st.session_state.url_pairs:
    st.markdown("#### 현재 선택된 URL 페어")
    st.write(st.session_state.url_pairs[0]["as"], " ↔ ", st.session_state.url_pairs[0]["to"])

# ========================= 공통 상수/유틸 =========================
MODEL_PATTERNS = [
    r"(?:OLED|QNED|NANO)\d{2,3}[A-Z0-9]+(?:[.\-][A-Z0-9]+)*",
    r"[LU][A-Z]{1,2}\d{2,3}[A-Z0-9]{1,4}",
    r"\b[A-Z]{2,}\d{2,}\b"
]
UNKNOWN = "Unknown"
UNSET = object()  # CTA 초기 상태 보호용

PRODUCT_ALLOW = ("/api/","/v1/","/v2/","/graphql","/search","/catalog","/commerce",
                 "/product","/plp","/listing","/lgecom","/pim","/sku","/model","/category")
ANALYTICS_BLOCK = ("mpulse.net","onetrust.com","omtrdc.net","adobedtm.com",
                   "googletagmanager.com","google-analytics.com","hotjar.com","doubleclick.net")

PRIMARY_BTN_RE   = re.compile(r"\b(c-button--primary|c-btn--primary)\b", re.I)
SECONDARY_BTN_RE = re.compile(r"\b(c-button--secondary|c-btn--secondary)\b", re.I)

def _is_blocked_analytics(url: str) -> bool:
    return any(d in url.lower() for d in ANALYTICS_BLOCK)

def _looks_like_product_api(url: str) -> bool:
    return any(k in url.lower() for k in PRODUCT_ALLOW)

def _guess_market_and_lang(url: str):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").strip("/")
        first = (path.split("/", 1)[0] if path else "").lower()
        market = first if len(first) in (2,3) else ""
        if not market:
            if host.endswith(".sg"): market="sg"
            elif host.endswith(".uk") or host.endswith(".co.uk"): market="uk"
            elif host.endswith(".de"): market="de"
            elif host.endswith(".fr"): market="fr"
            elif host.endswith(".it"): market="it"
            elif host.endswith(".es"): market="es"
        market = market or "uk"
        lang = {
            "uk":"en-GB,en;q=0.8","sg":"en-SG,en;q=0.8","de":"de-DE,de;q=0.9,en;q=0.7",
            "fr":"fr-FR,fr;q=0.9,en;q=0.7","it":"it-IT,it;q=0.9,en;q=0.7","es":"es-ES,es;q=0.9,en;q=0.7"
        }.get(market,"en-GB,en;q=0.8")
        tz  = {"uk":"Europe/London","sg":"Asia/Singapore","de":"Europe/Berlin",
               "fr":"Europe/Paris","it":"Europe/Rome","es":"Europe/Madrid"}.get(market,"Europe/London")
        geo = {
            "uk":{"latitude":51.5074,"longitude":-0.1278,"accuracy":50},
            "sg":{"latitude":1.3521,"longitude":103.8198,"accuracy":50},
            "de":{"latitude":52.52,"longitude":13.405,"accuracy":50},
            "fr":{"latitude":48.8566,"longitude":2.3522,"accuracy":50},
            "it":{"latitude":41.9028,"longitude":12.4964,"accuracy":50},
            "es":{"latitude":40.4168,"longitude":-3.7038,"accuracy":50},
        }.get(market)
        return market, lang, tz, geo
    except Exception:
        return "uk","en-GB,en;q=0.8","Europe/London",{"latitude":51.5074,"longitude":-0.1278,"accuracy":50}

def norm_model(m: str) -> str:
    if not m: return ""
    u = re.sub(r"[\s.\-_/]+","", m.upper())
    return u.replace("OLEDTV","OLED").replace("QNEDTV","QNED").replace("NANOTV","NANO")

def same_model(a: str, b: str) -> bool:
    na, nb = norm_model(a), norm_model(b)
    if not na or not nb: return False
    if na == nb: return True
    na2 = re.sub(r"[A-Z]{2,3}$","",na)
    nb2 = re.sub(r"[A-Z]{2,3}$","",nb)
    return bool(na2 and nb2 and na2 == nb2)

def _viewport(vp: str):
    if "mobile" in vp: return {"width":390,"height":844}
    if "1280"  in vp: return {"width":1280,"height":900}
    return {"width":1440,"height":900}

# ========================= 셀렉터/대기 =========================
CARD_SEL = ("li.product-grid__item, .product-card, article.product, .product-list__item, "
            "[data-model], [data-sku], [data-product-id], [data-modelcode]")
BUY_SEL   = "button[class*='buy' i], a[class*='buy' i], [aria-label*='buy' i], [data-cta*='buy' i]"
LEARN_SEL = ("a[class*='learn' i], [aria-label*='learn' i], [data-cta*='learn' i], "
             "a:has-text('Learn more'), a:has-text('Find out more'), "
             "a.c-button, a.btn, button.c-button, button.btn, a[href*='/p/'], a[href*='/product']")
CMP_SEL   = ("a[class*='compare' i], button[class*='compare' i], [aria-label*='compare' i], "
             "input[type='checkbox'][name*='compare' i], input[type='checkbox'][id*='compare' i], "
             "label[for*='compare' i]")

LG_LEARN_SEL = ".c-button--secondary, .c-btn--secondary, a.c-button--secondary, button.c-button--secondary"
LG_BUY_SEL   = ".c-button--primary,  .c-btn--primary,  a.c-button--primary,  button.c-button--primary"

def wait_until_ready(page, idle_ms: int, debug=False) -> bool:
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms); return True
    except Exception:
        if debug: st.write("[debug] networkidle 미도달 → 대체 경로 진행")
    for state in ["load","domcontentloaded"]:
        try:
            page.wait_for_load_state(state, timeout=idle_ms); return True
        except Exception: pass
    for sel in [CARD_SEL,".product-grid",".product-grid__items",".product-list",
                "[data-product-id]","[data-model]","[data-sku]"]:
        try:
            page.wait_for_selector(sel, timeout=max(2000, idle_ms//2)); return True
        except Exception: pass
    return False

# ========================= Compare 다국어 키워드/유틸 =========================
COMPARE_TEXTS = [
    "compare", "비교", "vergleich", "comparer", "comparar", "confronta",
    "比較", "비교하기", "비교함", "vergelijk", "comparação"
]
def _contains_compare_text(s: str) -> bool:
    if not s: return False
    low = s.lower()
    return any(tok in low for tok in COMPARE_TEXTS)

# ========================= JSON 파서 =========================
def extract_models_from_json(obj, out_rows):
    def _emit(model_str, title=""):
        if not model_str: return
        m = str(model_str).strip()
        base = m.split('.')[0].upper()
        alnum = re.sub(r"[^A-Za-z0-9]","", base)
        if re.fullmatch(r"\d+", base): return
        if len(alnum) < 4: return
        if not (re.search(r"[A-Za-z]", base) and re.search(r"\d", base)):
            hit=None
            for pat in MODEL_PATTERNS:
                mm = re.search(pat, m, re.I)
                if mm: hit=mm.group(0).upper(); break
            if not hit: return
            base = hit
        if re.match(r"^MD\d+$", base, re.I): return
        out_rows.append({"Model": base, "Title": title or ""})
    if isinstance(obj, dict):
        title = obj.get("name") or obj.get("title") or ""
        for key in ("modelCode","model","code"):
            if key in obj and obj[key]: _emit(obj[key], title)
        if "sku" in obj and obj["sku"]: _emit(obj["sku"], title)
        for _,v in obj.items():
            if isinstance(v,str):
                for pat in MODEL_PATTERNS:
                    mm = re.search(pat, v, re.I)
                    if mm: out_rows.append({"Model":mm.group(0).upper(),"Title":title})
            elif isinstance(v,(dict,list)):
                extract_models_from_json(v, out_rows)
    elif isinstance(obj, list):
        for it in obj: extract_models_from_json(it, out_rows)

# ========================= CTA 유틸 =========================
def _btn_shape(el):
    if not el: return UNKNOWN
    cls = (el.get_attribute("class") or "").lower()
    style = (el.get_attribute("style") or "").lower()
    if "rounded-none" in cls or "square" in cls or "border-radius: 0" in style:
        return "Squared"
    if "rounded" in cls or "pill" in cls or "9999px" in style or "20px" in style:
        return "Rounded"
    return UNKNOWN

_METRICS_JS = """
(el)=>{
  function collect(node){
    const cs = getComputedStyle(node);
    const px = (v)=>parseFloat(v)||0;
    const radNum = (v)=>{
      if(!v) return 0;
      v = v.toString();
      if (v.includes('%')) return 9999;
      const n = parseFloat(v);
      return isFinite(n) ? n : 0;
    };
    const corners = [
      radNum(cs.borderTopLeftRadius),
      radNum(cs.borderTopRightRadius),
      radNum(cs.borderBottomRightRadius),
      radNum(cs.borderBottomLeftRadius)
    ];
    const avgRadius = corners.reduce((a,b)=>a+b,0)/(corners.length||1);
    const padX = px(cs.paddingLeft) + px(cs.paddingRight);
    const bg = cs.backgroundColor || "rgba(0,0,0,0)";
    const m = bg.match(/rgba?\\(([^)]+)\\)/);
    let alpha = 0;
    if (m){
      const p = m[1].split(",").map(s=>parseFloat(s));
      alpha = (p.length===4 ? (isFinite(p[3])?p[3]:0) : ((p[0]+p[1]+p[2])>0?1:0));
    }
    const borderW = ["Top","Right","Bottom","Left"].map(s=>px(cs["border"+s+"Width"])).reduce((a,b)=>a+b,0)/4;
    const deco = cs.textDecorationLine || "";
    const cls  = (node.className || "").toString().toLowerCase();
    const tag  = (node.tagName || "").toLowerCase();
    const hasIcon = !!(node.querySelector("svg,i,[class*='icon'],[class*='ico'],[class*='chevron']"));
    return { nodeTag: tag, cls, padX, avgRadius, alpha, borderW, deco, hasIcon };
  }
  let node = el, best = collect(el), depth = 0;
  while (node && depth < 3){
    node = node.parentElement;
    if(!node) break;
    const m = collect(node);
    const scoreBest = (best.padX>0) + (best.alpha>0.05) + (best.borderW>=1) + (best.avgRadius>=6) + (best.cls.includes('btn')||best.cls.includes('cta'));
    const scoreNew  = (m.padX>0) + (m.alpha>0.05) + (m.borderW>=1) + (m.avgRadius>=6) + (m.cls.includes('btn')||m.cls.includes('cta'));
    if (scoreNew > scoreBest) best = m;
    depth++;
  }
  return best;
}
"""

def _classify_cta(page, locator):
    if not locator or locator.count()==0:
        return ("Unknown", "Unknown")
    try:
        m = locator.evaluate(_METRICS_JS)
        is_button_like = (
            "btn" in m["cls"] or "button" in m["cls"] or "cta" in m["cls"] or
            m["nodeTag"] == "button" or
            (m["padX"] >= 10 and (m["alpha"] > 0.02 or m["borderW"] >= 1 or m["avgRadius"] >= 6))
        )
        shape = "Rounded" if m["avgRadius"] >= 10 else ("Squared" if m["avgRadius"] >= 1 else "Unknown")
        typ = "Button+Icon" if (is_button_like and m.get("hasIcon")) else ("Button" if is_button_like else "Text")
        return (typ, shape if typ.startswith("Button") else "Unknown")
    except Exception:
        return ("Text", "Unknown")

def _promote_clickable(el):
    try:
        promoted = el.locator("xpath=ancestor-or-self::a | ancestor-or-self::button").first
        return promoted if promoted and promoted.count() > 0 else el
    except Exception:
        return el

def _rounded_from_class_or_css(page, el):
    if not el or el.count() == 0:
        return "Unknown"
    try:
        cls = (el.get_attribute("class") or "").lower()
        if PRIMARY_BTN_RE.search(cls) or SECONDARY_BTN_RE.search(cls) \
           or "c-button" in cls or "c-btn" in cls or "pill" in cls or "rounded" in cls:
            return "Rounded"
        js = r"""
        (node)=>{
          if(!node) return {avg:0,h:0,maxR:0};
          const takeR = (cs)=>[
            parseFloat(cs.borderTopLeftRadius)||0,
            parseFloat(cs.borderTopRightRadius)||0,
            parseFloat(cs.borderBottomRightRadius)||0,
            parseFloat(cs.borderBottomLeftRadius)||0
          ];
          const maxRin = (el)=>{
            if(!el) return 0;
            const cs = getComputedStyle(el);
            const rs = takeR(cs);
            let maxR = Math.max(...rs);
            try{
              const b = getComputedStyle(el,"::before");
              const a = getComputedStyle(el,"::after");
              if(b){ maxR = Math.max(maxR, ...takeR(b)); }
              if(a){ maxR = Math.max(maxR, ...takeR(a)); }
            }catch(e){}
            return maxR;
          };
          let maxR = 0, targetH = 0;
          const visit = (el, depth=0)=>{
            if(!el || depth>2) return;
            try{
              const r = el.getBoundingClientRect();
              const h = r.height||0;
              const mr = maxRin(el);
              if(mr > maxR){
                maxR = mr; targetH = h;
              }
            }catch(e){}
            const pref = el.querySelectorAll?.(".c-button__inner, .c-btn__inner, .inner, a, button, span, div") || [];
            let i=0;
            for(const c of pref){ if(i++>12) break; visit(c, depth+1); }
          };
          visit(node,0);
          const cs0 = getComputedStyle(node);
          const rs0 = [
            parseFloat(cs0.borderTopLeftRadius)||0,
            parseFloat(cs0.borderTopRightRadius)||0,
            parseFloat(cs0.borderBottomRightRadius)||0,
            parseFloat(cs0.borderBottomLeftRadius)||0
          ];
          const rect0 = node.getBoundingClientRect();
          const avg = (rs0.reduce((a,b)=>a+b,0)/4)||0;
          const h0  = rect0.height||0;
          return {avg:avg, h:Math.max(h0,targetH), maxR:maxR};
        }
        """
        m = el.evaluate(js)
        if not m: return "Unknown"
        R = max(float(m.get("avg") or 0), float(m.get("maxR") or 0))
        H = float(m.get("h") or 0)
        ratio = (R/H) if H>0 else 0.0
        if R >= 12 or ratio >= 0.25:
            return "Rounded"
        if R >= 1:
            return "Squared"
        return "Unknown"
    except Exception:
        return "Unknown"

# ========================= Compare 위치 계산 =========================
_COMPARE_POS_JS = r"""
(el, cardHint)=>{
  if(!el) return {horiz:"Unknown", vert:"Unknown"};
  const lift=(node)=>{
    if(!node) return null;
    if(node.tagName && node.tagName.toLowerCase()==='input'){
      const id=node.getAttribute('id');
      if(id){
        const lab=document.querySelector(`label[for="${id}"]`);
        if(lab) return lab;
      }
      return node.parentElement || node;
    }
    const host=node.closest && node.closest("a,button,label,[role='button'],[role='checkbox']");
    return host || node;
  };
  el = lift(el);

  const CARD_SEL = [
    "li.product-grid__item", "article.product", ".product-card", ".product-list__item",
    "[data-product-id]", "[data-model]", "[data-sku]", "[data-modelcode]"
  ].join(", ");
  const isCardLike=(n)=>{
    if(!n) return false;
    if(n.matches && n.matches(CARD_SEL)) return true;
    const cls=(n.className||"").toString().toLowerCase();
    return /(product|card|grid|tile|list)/.test(cls);
  };
  let bestCard = null, bestArea = Infinity;
  let cur = el;
  for (let hop=0; cur && hop<10; hop++, cur=cur.parentElement){
    if(!(cur instanceof Element)) break;
    if(!isCardLike(cur)) continue;
    const r = cur.getBoundingClientRect();
    const area = Math.max(1, r.width) * Math.max(1, r.height);
    if (r.width < 160 || r.height < 80) continue;
    if (area < bestArea){ bestArea = area; bestCard = cur; }
  }
  let card = bestCard || cardHint || el.parentElement || document.body;

  const cr = card.getBoundingClientRect();
  const cs = getComputedStyle(card);
  const pl = parseFloat(cs.paddingLeft)||0, pr=parseFloat(cs.paddingRight)||0;
  const pt = parseFloat(cs.paddingTop)||0,  pb=parseFloat(cs.paddingBottom)||0;
  const w  = Math.max(1, cr.width  - (pl+pr));
  const h  = Math.max(1, cr.height - (pt+pb));
  const x0 = cr.left + pl, y0 = cr.top + pt;

  const er = el.getBoundingClientRect();
  const xC = ((er.left + er.right)/2) - x0;
  const yC = ((er.top  + er.bottom)/2) - y0;
  const isTiny = (er.width <= 96 && er.height <= 48);

  const textish = (el.innerText||el.textContent||"").toLowerCase();
  const aria = (el.getAttribute("aria-label")||"").toLowerCase();
  const cls  = (el.className||"").toString().toLowerCase();
  const looksCompare = /compare|비교|vergleich|comparer|comparar|confronta/.test(textish+aria+cls);
  const node = (el.tagName && el.tagName.toLowerCase()==='input') ? el : el.querySelector && el.querySelector("input[type='checkbox']");
  const isCheckbox = !!node;

  const dist = (a,b)=>Math.abs(a-b);
  let cand = [
    {pos:"Left",   d: dist(xC, 0)},
    {pos:"Center", d: dist(xC, w/2)},
    {pos:"Right",  d: dist(xC, w)}
  ];
  cand.sort((a,b)=>a.d-b.d);
  let horiz = cand[0].pos;

  if ((looksCompare || isCheckbox) && xC <= w*0.55) {
    horiz = "Left";
  } else if (isTiny && xC <= w*0.58) {
    horiz = "Left";
  } else {
    if (looksCompare && cand[0].pos==="Center" && xC <= w*0.6){
      horiz = "Left";
    }
  }
  const vert = (yC < h*0.33) ? "Top" : (yC > h*0.67 ? "Bottom" : "Middle");
  return {horiz, vert};
}
"""
def _compare_position(page, cmp_loc, card_loc=None):
    try:
        if not cmp_loc or cmp_loc.count()==0: return UNKNOWN
        card_el = card_loc.element_handle() if (card_loc and card_loc.count()>0) else None
        pos = page.evaluate(_COMPARE_POS_JS, cmp_loc.element_handle(), card_el)
        if not pos: return UNKNOWN
        return f"{pos['vert']}-{pos['horiz']}"
    except Exception:
        return UNKNOWN

# ========================= Compare Locator (강화) =========================
def _find_compare_locator(page, card_locator=None):
    scope = card_locator if (card_locator and card_locator.count() > 0) else page

    # 1) input:checkbox → label[for] 승격
    try:
        cb = scope.locator("input[type='checkbox'][name*='compare' i], input[type='checkbox'][id*='compare' i]")
        if cb.count() == 0:
            cb = scope.locator("input[type='checkbox']")
        if cb.count() > 0:
            el = cb.first
            try:
                _id = el.get_attribute("id") or ""
                if _id:
                    lbl = page.locator(f"label[for='{_id}']")
                    if lbl.count() > 0:
                        return lbl.first
            except Exception:
                pass
            return el
    except Exception:
        pass

    # 2) data-속성 / role 토글류
    try:
        cand = scope.locator(
            "[data-compare], [data-testid*='compare' i], [data-test*='compare' i], "
            "[role='switch'][aria-label*='compare' i], [role='checkbox'][aria-label*='compare' i]"
        )
        if cand.count() > 0:
            return cand.first
    except Exception:
        pass

    # 3) 텍스트/aria/class 내 compare류 키워드
    try:
        cand = scope.locator("a, button, label, [role='button']")
        n = cand.count()
        for i in range(min(n, 50)):
            el = cand.nth(i)
            try:
                txt = (el.inner_text() or "").strip()
            except Exception:
                txt = ""
            aria = (el.get_attribute("aria-label") or "")
            cls  = (el.get_attribute("class") or "")
            if _contains_compare_text(txt) or _contains_compare_text(aria) or _contains_compare_text(cls):
                return el
    except Exception:
        pass

    # 4) 마지막: 카드 내 임의 checkbox/토글
    try:
        cand = scope.locator("input[type='checkbox'], [role='switch'], [role='checkbox']")
        if cand.count() > 0:
            return cand.first
    except Exception:
        pass

    return None

# ========================= 템플릿 단독 판정(보정) =========================
def classify_template_sample(url: str):
    VIEWPORT={"width":1280,"height":900}; NAV_TMO, IDLE_TMO = 35000, 9000
    result={"LearnMore_Type":"Text","LearnMore_Shape":UNKNOWN,"BuyNow_Shape":"Squared","Compare_Pos":"Center"}
    with sync_playwright() as p:
        browser = p.chromium.launch(**PLAYWRIGHT_LAUNCH_KW)
        ctx = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TMO)
            try: page.wait_for_load_state("networkidle", timeout=IDLE_TMO)
            except Exception: pass

            # Buy Now
            buy = page.locator(LG_BUY_SEL + ", " + BUY_SEL).first
            if buy and buy.count()>0:
                buy = _promote_clickable(buy)
                _, shape = _classify_cta(page, buy)
                if shape in ("Unknown","Squared"):
                    try:
                        bcls = (buy.get_attribute("class") or "").lower()
                        if PRIMARY_BTN_RE.search(bcls): shape = "Rounded"
                    except Exception: pass
                result["BuyNow_Shape"] = shape if shape!=UNKNOWN else _rounded_from_class_or_css(page, buy)

            # Learn More
            learn = page.locator(LG_LEARN_SEL + ", " + LEARN_SEL).first
            if learn and learn.count()>0:
                learn = _promote_clickable(learn)
                t, s = _classify_cta(page, learn)
                result["LearnMore_Type"]  = "Button" if t in ("Text","Unknown") else t
                result["LearnMore_Shape"] = s if s!=UNKNOWN else _rounded_from_class_or_css(page, learn)

            # Compare
            cmpb = _find_compare_locator(page, None)
            if cmpb:
                result["Compare_Pos"] = _compare_position(page, cmpb, None)
        except Exception:
            pass
        ctx.close(); browser.close()
    return result

# ========================= 메인 수집기 =========================
def fetch_models(url: str, max_models=50):
    market, accept_lang, tz, geo = _guess_market_and_lang(url)
    vp = _viewport(viewport_choice)
    payloads, dom_models = [], []
    NAV_TMO = int(timeout_sec*1000)
    IDLE_TMO = int(max(5, timeout_sec-4)*1000)

    with sync_playwright() as p:
        browser = p.chromium.launch(**PLAYWRIGHT_LAUNCH_KW)
        ctx = browser.new_context(
            viewport=vp,
            extra_http_headers={"Accept-Language": accept_lang},
            locale=accept_lang.split(",")[0],
            timezone_id=tz, geolocation=geo, permissions=["geolocation"],
            ignore_https_errors=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"),
        )
        page = ctx.new_page()
        ctx.set_default_timeout(NAV_TMO); page.set_default_timeout(NAV_TMO)

        if fast_mode:
            def _should_block(req_url: str, rtype: str)->bool:
                ul=req_url.lower()
                if any(d in ul for d in ANALYTICS_BLOCK): return True
                if rtype in ("image","font","media"): return True
                return False
            page.route("**/*", lambda route:
                       route.abort() if _should_block(route.request.url, route.request.resource_type)
                       else route.continue_())

        def on_resp(resp):
            try:
                ul=resp.url.lower()
                if _is_blocked_analytics(ul): return
                if not _looks_like_product_api(ul): return
                ct=(resp.headers.get("content-type") or "").lower()
                body=None
                if "json" in ct:
                    try: body=resp.json()
                    except Exception: body=None
                if body is None:
                    try:
                        txt=resp.text()
                        if txt and "{" in txt and len(txt)<2_000_000:
                            body=json.loads(txt)
                    except Exception: body=None
                if body is None:
                    try:
                        req=resp.request
                        if req and req.method.lower()=="post":
                            pdata=req.post_data or ""
                            if "{" in pdata and len(pdata)<2_000_000:
                                body=json.loads(pdata)
                    except Exception: pass
                if body is not None:
                    payloads.append({"url":resp.url, "data":body})
                    if debug_log: st.write({"captured_json": resp.url})
            except Exception: pass
        page.on("response", on_resp)

        if debug_log:
            def on_req(req):
                ul=(req.url or "").lower()
                if _looks_like_product_api(ul) and not _is_blocked_analytics(ul):
                    st.write({"xhr/fetch": req.method, "url": req.url[:300]})
            page.on("request", on_req)

        last_err, ready = None, False
        for attempt in range(retries+1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TMO)
                ready = wait_until_ready(page, IDLE_TMO, debug_log)
                if ready: break
            except Exception as e:
                last_err = e
            if debug_log: st.write(f"[debug] goto 재시도 {attempt+1}/{retries} (ready={bool(ready)})")
        else:
            if last_err: raise last_err

        for sel in ["button[id*='accept']","button[aria-label*='Accept']",".onetrust-accept-btn-handler",
                    "button:has-text('Accept all')","button:has-text('Accept All')",
                    "button:has-text('Alle akzeptieren')","button:has-text('Aceptar todo')","button:has-text('Aceptar todas')"]:
            try: page.locator(sel).first.click(timeout=1200); break
            except Exception: pass

        # 더보기(간단)
        try:
            for btn_text in ["Show more","더보기","Load more","See more","Mehr anzeigen","Voir plus","Ver más","Mostra altro"]:
                page.get_by_text(btn_text, exact=False).first.click(timeout=1200)
        except Exception: pass

        try:
            for _ in range(3 if fast_mode else 6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(600)
            page.evaluate("window.scrollTo(0,0)")
        except Exception: pass

        # deep JSON
        try:
            txt = page.locator("script#__NEXT_DATA__").first.inner_text(timeout=2000)
            data = json.loads(txt); tmp=[]
            extract_models_from_json(data, tmp)
            dom_models.extend({"Model":r["Model"].upper()} for r in tmp)
        except Exception: pass
        try:
            raw = page.evaluate("() => (window.__NEXT_DATA__ || window.__APOLLO_STATE__ || null)")
            if raw:
                tmp=[]; extract_models_from_json(raw, tmp)
                dom_models.extend({"Model":r["Model"].upper()} for r in tmp)
        except Exception: pass

        # DOM fallback
        try:
            cards = page.locator(CARD_SEL); c = min(cards.count(), 80)
            for i in range(c):
                card=cards.nth(i); blob=""
                try: blob += card.inner_text()+" "
                except Exception: pass
                links = card.locator("a[href]"); lcnt=min(links.count(),10)
                for li in range(lcnt):
                    try:
                        href = links.nth(li).get_attribute("href") or ""
                        blob += " "+href; slug = href.split("/")[-1]
                        for pat in MODEL_PATTERNS:
                            m=re.search(pat, slug, re.I)
                            if m: dom_models.append({"Model":m.group(0).upper()}); break
                    except Exception: pass
                for el in card.locator("img, a, *").all()[:30]:
                    for a in ("alt","aria-label","href","src","data-model","data-modelcode","data-sku","data-product-id"):
                        try:
                            v=el.get_attribute(a)
                            if v: blob += f" {v}"
                        except Exception: pass
                for pat in MODEL_PATTERNS:
                    m=re.search(pat, blob, re.I)
                    if m: dom_models.append({"Model":m.group(0).upper()}); break
        except Exception: pass

        # CTA 추정 (강화) — Learn/Buy/Compare
        cta_types={
            "LearnMore_Type": UNSET,
            "LearnMore_Shape": UNSET,
            "BuyNow_Shape": UNSET,
            "Compare_Pos": UNSET
        }
        try:
            cards = page.locator(CARD_SEL); n=min(cards.count(),2)
            if n>0:
                card = cards.nth(0)

                # Buy Now
                buy = card.locator(LG_BUY_SEL + ", " + BUY_SEL).first
                if buy and buy.count()>0:
                    buy = _promote_clickable(buy)
                    _, buy_shape = _classify_cta(page, buy)
                    if buy_shape in ("Unknown","Squared"):
                        try:
                            bcls = (buy.get_attribute("class") or "").lower()
                            if PRIMARY_BTN_RE.search(bcls): buy_shape = "Rounded"
                        except Exception: pass
                    if cta_types["BuyNow_Shape"] is UNSET or cta_types["BuyNow_Shape"] == UNKNOWN:
                        cta_types["BuyNow_Shape"] = buy_shape if buy_shape!=UNKNOWN else _rounded_from_class_or_css(page, buy)

                # Learn More
                learn = card.locator(LG_LEARN_SEL + ", " + LEARN_SEL).first
                if learn and learn.count()>0:
                    learn = _promote_clickable(learn)
                    lm_type, lm_shape = _classify_cta(page, learn)
                    if cta_types["LearnMore_Type"] is UNSET or cta_types["LearnMore_Type"] == UNKNOWN:
                        cta_types["LearnMore_Type"]  = "Button" if lm_type in ("Text","Unknown") else lm_type
                    if cta_types["LearnMore_Shape"] is UNSET or cta_types["LearnMore_Shape"] == UNKNOWN:
                        cta_types["LearnMore_Shape"] = lm_shape if lm_shape!=UNKNOWN else _rounded_from_class_or_css(page, learn)

                # Compare (강화된 탐색)
                cmpb = _find_compare_locator(page, card)
                if cmpb:
                    cta_types["Compare_Pos"] = _compare_position(page, cmpb, card)

                # 디버그 출력
                if debug_log:
                    try:
                        st.write({"compare_found": bool(cmpb)})
                        if cmpb:
                            st.write({
                                "cmp_tag": cmpb.evaluate("(n)=>n.tagName"),
                                "cmp_cls": cmpb.get_attribute("class"),
                                "cmp_aria": cmpb.get_attribute("aria-label"),
                                "cmp_text": (cmpb.inner_text() or "")[:120]
                            })
                    except Exception:
                        pass

        except Exception: pass

        if take_screens:
            try:
                path=f"plp_sample_{int(time.time())}.png"
                page.screenshot(path=path, full_page=True); st.caption(f"스크린샷 저장: {path}")
            except Exception: pass

        ctx.close(); browser.close()

    # 네트워크 JSON walk
    results=[]
    def walk(o):
        if isinstance(o, dict):
            model=o.get("modelCode") or o.get("model") or o.get("code") or o.get("sku")
            title=o.get("name") or o.get("title"); row={}
            if model: row["Model"]=str(model).upper()
            if title: row["Title"]=str(title)
            if row: results.append(row)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)

    for pl in payloads:
        try: walk(pl["data"])
        except Exception: pass

    # 합치기/필터
    bundle=[]
    if results: bundle.extend(results)
    if dom_models: bundle.extend(dom_models)
    seen=set(); out=[]
    for r in bundle:
        m=r.get("Model") or ""; t=r.get("Title","")
        candidate = (m.split(".")[0].upper() if m else "")
        if not candidate:
            mm=re.search(r"(?:OLED|QNED|NANO)\d{2,3}[A-Z0-9\-]+", t or "", re.I)
            if mm: candidate=mm.group(0).upper()
        if not candidate: continue
        if re.fullmatch(r"\d+", candidate): continue
        alnum=re.sub(r"[^A-Za-z0-9]","", candidate)
        if len(alnum)<4: continue
        if not (re.search(r"[A-Za-z]", candidate) and re.search(r"\d", candidate)):
            hit=None
            for pat in MODEL_PATTERNS:
                mm=re.search(pat, candidate, re.I)
                if mm: hit=mm.group(0).upper(); break
            if not hit: continue
            candidate=hit
        if re.match(r"^MD\d+$", candidate, re.I): continue
        nm=norm_model(candidate)
        if nm in seen: continue
        seen.add(nm)
        out.append({"Model":candidate})
        if len(out)>=max_models: break

    if debug_log:
        st.write(f"[debug] payloads={len(payloads)} dom={len(dom_models)} unique={len(out)}")
        if len(out)==0 and payloads:
            st.write({"sample_payload_urls":[p["url"] for p in payloads[:5]]})

    # 부족 시 템플릿 보정
    need_strict = any(v in (UNSET, UNKNOWN) for v in (cta_types.get("LearnMore_Type",UNSET),
                                                      cta_types.get("BuyNow_Shape",UNSET)))
    if need_strict:
        try:
            strict=classify_template_sample(url)
            for k,v in strict.items():
                if cta_types.get(k, UNSET) in (UNSET, UNKNOWN):
                    cta_types[k]=v
        except Exception: pass

    # 기본값 보정
    defaults = {
        "LearnMore_Type": "Text",
        "LearnMore_Shape": UNKNOWN,
        "BuyNow_Shape":   "Squared",
        "Compare_Pos":    "Bottom-Left"
    }
    for k, dv in defaults.items():
        if cta_types.get(k, UNSET) is UNSET or cta_types.get(k) == UNKNOWN:
            cta_types[k] = dv

    return out, cta_types

# ========================= 매칭/표시 =========================
def match_rows(a_models, b_models, a_types, b_types, want=2):
    rows, used_b = [], set()
    def _lm_str(types):
        t = types.get("LearnMore_Type", UNKNOWN)
        s = types.get("LearnMore_Shape", UNKNOWN)
        return f"{t}" + (f" ({s})" if s not in (None, "", UNKNOWN) else "")
    for a in a_models:
        best=None
        for j,b in enumerate(b_models):
            if j in used_b: continue
            if same_model(a["Model"], b["Model"]): best=(j,b); break
        if best:
            j,b=best; used_b.add(j)
            rows.append({
                "Model (AsIs)": a["Model"],
                "Model (ToBe)": b["Model"],
                "Learn More (AsIs)": _lm_str(a_types),
                "Learn More (ToBe)": _lm_str(b_types),
                "Buy Now (AsIs)": a_types.get("BuyNow_Shape", UNKNOWN),
                "Buy Now (ToBe)": b_types.get("BuyNow_Shape", UNKNOWN),
                "Compare (AsIs)": a_types.get("Compare_Pos", UNKNOWN),
                "Compare (ToBe)": b_types.get("Compare_Pos", UNKNOWN),
                "Match": "Matched"
            })
        if len(rows)>=want: break

    if not rows and a_models and b_models:
        rows.append({
            "Model (AsIs)": a_models[0]["Model"],
            "Model (ToBe)": b_models[0]["Model"],
            "Learn More (AsIs)": _lm_str(a_types),
            "Learn More (ToBe)": _lm_str(b_types),
            "Buy Now (AsIs)": a_types.get("BuyNow_Shape", UNKNOWN),
            "Buy Now (ToBe)": b_types.get("BuyNow_Shape", UNKNOWN),
            "Compare (AsIs)": a_types.get("Compare_Pos", UNKNOWN),
            "Compare (ToBe)": b_types.get("Compare_Pos", UNKNOWN),
            "Match": "FirstInCategory"
        })
    return rows

# ========================= 실행 =========================
if run_btn:
    # URL 검증/로딩
    if st.session_state.url_pairs:
        url_as = st.session_state.url_pairs[0]["as"]
        url_tb = st.session_state.url_pairs[0]["to"]
    # 폼에서 바로 실행 눌렀지만 페어가 없다면 폼값으로 시도
    if (not st.session_state.url_pairs) and _valid_url(url_as) and _valid_url(url_tb):
        st.session_state.url_pairs = [{"as": url_as.strip(), "to": url_tb.strip()}]
        try:
            st.experimental_set_query_params(as=url_as.strip(), to=url_tb.strip())
        except Exception:
            pass

    if not st.session_state.url_pairs:
        st.error("실행하려면 유효한 두 개의 URL을 먼저 입력해주세요.")
    else:
        url_as = st.session_state.url_pairs[0]["as"]
        url_tb = st.session_state.url_pairs[0]["to"]

        st.info("네트워크 응답 기반으로 PLP 데이터를 수집 중입니다...")
        try:
            a_models, a_types = fetch_models(url_as, max_models=80)
            b_models, b_types = fetch_models(url_tb, max_models=80)
            st.caption(f"[AS-IS] 수집 {len(a_models)}건 / [TO-BE] 수집 {len(b_models)}건")
            rows = match_rows(a_models, b_models, a_types, b_types, want=2)
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)
            st.success(f"✅ {len(df)}개 비교행 출력 (UK ↔ SG)")
            if debug_log:
                st.write("a_types:", a_types, " / b_types:", b_types)
        except Exception as e:
            st.error(f"실행 중 오류: {type(e).__name__}: {e}")
