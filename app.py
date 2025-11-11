# ==== Playwright 브라우저 부트스트랩 (Cloud 안전모드) ====
import os, sys, subprocess, pathlib

# 브라우저 캐시 경로 고정
BROWSERS_DIR = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/home/appuser/.cache/ms-playwright"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR  # 설치/실행 모두 동일 경로 사용

def _chromium_missing() -> bool:
    p = pathlib.Path(BROWSERS_DIR)
    if not p.exists():
        return True
    # 폴더 구조가 버전마다 달라서, 'headless_shell' 또는 'chrome' 유무로 판별
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
    # 네트워크 허용된 환경이면 매 실행 시 점검 후 필요 시만 설치
    try:
        if _chromium_missing() or os.environ.get("FORCE_PW_INSTALL") == "1":
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        # 마지막 시도로 전체 설치
        subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

_ensure_playwright_chromium()

# 런치 옵션 강제(컨테이너/Cloud에서 필요한 경우가 많음)
PLAYWRIGHT_LAUNCH_KW = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}

# (선택) 설치 상태 로깅
try:
    print("[PW] browsers path =", BROWSERS_DIR, " / chromium exists? =", (not _chromium_missing()))
except Exception:
    pass
# ==== /부트스트랩 끝 ====


# app.py — Streamlit + Playwright (UK ↔ SG)
# Network-first + DOM fallback + deep JSON (__NEXT_DATA__/window)
# GraphQL POST sniffing + text/plain JSON 파싱 + PDP 폴백
# 타임아웃/재시도/리소스차단 + 첫 모델 강제 비교 + SKU(점 앞 베이스) 허용
# Learn More 버튼/텍스트 판정: CSS 메트릭 기반(_classify_cta) 추가

import sys, asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os, re, time, json
import pandas as pd
import streamlit as st
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# =========================
# UI 헤더 & 옵션
# =========================
st.set_page_config(page_title="PLP 카드 비교 (UK ↔ SG)", layout="wide")
st.markdown("# 🧩 PLP 상품 카드 비교")
st.caption("Network-first + DOM fallback + deep JSON + GraphQL sniffing (with retry/timeout)")

with st.sidebar:
    st.markdown("### 실행 옵션")
    fast_mode = st.toggle("⚡ Fast Mode (빠른 시도)", value=True, help="이미지·폰트·애널리틱스 차단으로 수집 속도 향상")
    debug_log = st.toggle("🐞 Debug 로그", value=True)
    viewport_choice = st.selectbox("뷰포트", ["desktop 1440x900", "desktop 1280x900", "mobile 390x844"], index=0)
    take_screens = st.checkbox("카드 스크린샷 저장", value=False)
    timeout_sec = st.number_input("⏱ 페이지 대기 시간(초)", min_value=5, max_value=60, value=25, step=1)
    retries = st.slider("🔄 재시도 횟수", min_value=0, max_value=3, value=1)

col1, col2 = st.columns(2)
with col1:
    url_as = st.text_input("AS-IS URL (UK/원본)", "https://www.lg.com/uk/tvs-soundbars/oled-evo/")
with col2:
    url_tb = st.text_input("TO-BE URL (SG/비교대상)", "https://www.lg.com/sg/tvs-soundbars/oled-evo/")

run_btn = st.button("실행 (Network-first)")

# =========================
# 정규식/유틸
# =========================
MODEL_PATTERNS = [
    r"(?:OLED|QNED|NANO)\d{2,3}[A-Z0-9]+(?:[.\-][A-Z0-9]+)*",   # OLED65G4, NANO86...
    r"[LU][A-Z]{1,2}\d{2,3}[A-Z0-9]{1,4}",                     # UQ80, LM57, LR63...
    r"\b[A-Z]{2,}\d{2,}\b"                                     # 백업 패턴
]
UNKNOWN = "Unknown"

PRODUCT_ALLOW = (
    "/api/", "/v1/", "/v2/", "/graphql", "/search", "/catalog", "/commerce",
    "/product", "/plp", "/listing", "/lgecom", "/pim", "/sku", "/model", "/category"
)
ANALYTICS_BLOCK = (
    "mpulse.net", "onetrust.com", "omtrdc.net", "adobedtm.com",
    "googletagmanager.com", "google-analytics.com", "hotjar.com", "doubleclick.net"
)

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
        market = first if len(first) in (2, 3) else ""
        if not market:
            if host.endswith(".sg"): market = "sg"
            elif host.endswith(".uk") or host.endswith(".co.uk"): market = "uk"
            elif host.endswith(".de"): market = "de"
            elif host.endswith(".fr"): market = "fr"
            elif host.endswith(".it"): market = "it"
            elif host.endswith(".es"): market = "es"
        market = market or "uk"
        lang = {
            "uk": "en-GB,en;q=0.8", "sg": "en-SG,en;q=0.8", "de": "de-DE,de;q=0.9,en;q=0.7",
            "fr": "fr-FR,fr;q=0.9,en;q=0.7", "it": "it-IT,it;q=0.9,en;q=0.7", "es": "es-ES,es;q=0.9,en;q=0.7"
        }.get(market, "en-GB,en;q=0.8")
        tz = {
            "uk": "Europe/London", "sg": "Asia/Singapore", "de": "Europe/Berlin",
            "fr": "Europe/Paris", "it": "Europe/Rome", "es": "Europe/Madrid"
        }.get(market, "Europe/London")
        geo = {
            "uk": {"latitude": 51.5074, "longitude": -0.1278, "accuracy": 50},
            "sg": {"latitude": 1.3521, "longitude": 103.8198, "accuracy": 50},
            "de": {"latitude": 52.52, "longitude": 13.405, "accuracy": 50},
            "fr": {"latitude": 48.8566, "longitude": 2.3522, "accuracy": 50},
            "it": {"latitude": 41.9028, "longitude": 12.4964, "accuracy": 50},
            "es": {"latitude": 40.4168, "longitude": -3.7038, "accuracy": 50},
        }.get(market)
        return market, lang, tz, geo
    except Exception:
        return "uk", "en-GB,en;q=0.8", "Europe/London", {"latitude": 51.5074, "longitude": -0.1278, "accuracy": 50}

def norm_model(m: str) -> str:
    if not m: return ""
    u = re.sub(r"[\s.\-_/]+", "", m.upper())
    u = u.replace("OLEDTV", "OLED").replace("QNEDTV", "QNED").replace("NANOTV", "NANO")
    return u

def same_model(a: str, b: str) -> bool:
    na, nb = norm_model(a), norm_model(b)
    if not na or not nb: return False
    if na == nb: return True
    na2 = re.sub(r"[A-Z]{2,3}$", "", na)
    nb2 = re.sub(r"[A-Z]{2,3}$", "", nb)
    return bool(na2 and nb2 and na2 == nb2)

def _viewport(vp: str):
    if "mobile" in vp: return {"width": 390, "height": 844}
    if "1280" in vp: return {"width": 1280, "height": 900}
    return {"width": 1440, "height": 900}

# =========================
# 준비 대기 헬퍼
# =========================
CARD_SEL = (
    "li.product-grid__item, .product-card, article.product, .product-list__item, "
    "[data-model], [data-sku], [data-product-id], [data-modelcode]"
)
BUY_SEL   = "button[class*='buy'], a[class*='buy'], [aria-label*='buy'], [data-cta*='buy']"
LEARN_SEL = "a[class*='learn'], [aria-label*='learn'], [data-cta*='learn'], a[href*='/p/'], a[href*='/product']"
CMP_SEL   = "a[class*='compare'], button[class*='compare'], [aria-label*='compare']"

def wait_until_ready(page, idle_ms: int, debug=False) -> bool:
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms)
        return True
    except Exception:
        if debug: st.write("[debug] networkidle 미도달 → 대체 경로 진행")
    for state in ["load", "domcontentloaded"]:
        try:
            page.wait_for_load_state(state, timeout=idle_ms)
            return True
        except Exception:
            pass
    for sel in [CARD_SEL, ".product-grid", ".product-grid__items", ".product-list",
                "[data-product-id]", "[data-model]", "[data-sku]"]:
        try:
            page.wait_for_selector(sel, timeout=max(2000, idle_ms // 2))
            return True
        except Exception:
            pass
    return False

# =========================
# JSON 재귀 파서 (✅ sku 허용 + 점 앞 베이스 모델 보정)
# =========================
def extract_models_from_json(obj, out_rows):
    """
    JSON 객체를 재귀 탐색하여 모델 후보를 out_rows에 추가.
    우선순위: modelCode > model > code > sku(필터링) > 문자열 내 정규식 히트
    """
    def _emit(model_str, title=""):
        if not model_str:
            return
        m = str(model_str).strip()
        base = m.split('.')[0].upper()  # 점(.) 앞 베이스 모델
        alnum = re.sub(r"[^A-Za-z0-9]", "", base)

        # 노이즈 필터
        if re.fullmatch(r"\d+", base):  # 숫자만
            return
        if len(alnum) < 4:
            return
        if not (re.search(r"[A-Za-z]", base) and re.search(r"\d", base)):
            # 그래도 전체 문자열 내 TV 패턴이 있으면 허용
            hit = None
            for pat in MODEL_PATTERNS:
                mm = re.search(pat, m, re.I)
                if mm:
                    hit = mm.group(0).upper()
                    break
            if not hit:
                return
            base = hit
        if re.match(r"^MD\d+$", base, re.I):  # Bazaarvoice id 제거
            return
        out_rows.append({"Model": base, "Title": title or ""})

    if isinstance(obj, dict):
        title = obj.get("name") or obj.get("title") or ""
        # 우선 필드
        for key in ("modelCode", "model", "code"):
            if key in obj and obj[key]:
                _emit(obj[key], title)
        # ✅ sku 허용 (필터링 적용)
        if "sku" in obj and obj["sku"]:
            _emit(obj["sku"], title)

        # 문자열 필드 정규식 히트
        for k, v in obj.items():
            if isinstance(v, str):
                for pat in MODEL_PATTERNS:
                    mm = re.search(pat, v, re.I)
                    if mm:
                        out_rows.append({"Model": mm.group(0).upper(), "Title": title})
            elif isinstance(v, (dict, list)):
                extract_models_from_json(v, out_rows)

    elif isinstance(obj, list):
        for it in obj:
            extract_models_from_json(it, out_rows)

# =========================
# CTA 유틸
# =========================
def _btn_shape(el):
    if not el: return UNKNOWN
    cls = (el.get_attribute("class") or "").lower()
    style = (el.get_attribute("style") or "").lower()
    if "rounded-none" in cls or "square" in cls or "border-radius: 0" in style:
        return "Squared"
    if "rounded" in cls or "pill" in cls or "9999px" in style or "20px" in style:
        return "Rounded"
    return UNKNOWN

# === CTA 판단용 CSS 메트릭 수집(JS) ===
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

  // 자신 + 상위 3단계까지 스캔하여 가장 '버튼스러운' 메트릭을 선택
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

def classify_template_sample(url: str):
    # 간단 보정용(필요 시만 호출)
    VIEWPORT = {"width": 1280, "height": 900}
    NAV_TMO, IDLE_TMO = 35000, 9000
    result = {"LearnMore_Type": "Text", "BuyNow_Shape": "Squared", "Compare_Pos": "Center"}
    with sync_playwright() as p:
        browser = p.chromium.launch(**PLAYWRIGHT_LAUNCH_KW)
        ctx = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TMO)
            try:
                page.wait_for_load_state("networkidle", timeout=IDLE_TMO)
            except Exception:
                pass
            # Learn More도 메트릭으로 판정
            learn = page.locator(LEARN_SEL).first
            if learn and learn.count() > 0:
                lm_type, lm_shape = _classify_cta(page, learn)
                result["LearnMore_Type"]  = lm_type
                result["LearnMore_Shape"] = lm_shape

            buy = page.locator(BUY_SEL).first
            if buy:
                try:
                    r = buy.evaluate('''(el)=>{
                        const cs = getComputedStyle(el);
                        const vals = (cs.borderRadius || '0').split('/').join(' ')
                            .split(' ').map(x => parseFloat(x) || 0);
                        const avg = vals.reduce((a,b)=>a+b,0)/(vals.length||1);
                        return avg;
                    }''')
                    result["BuyNow_Shape"] = "Rounded" if float(r) >= 10 else "Squared"
                except Exception:
                    result["BuyNow_Shape"] = _btn_shape(buy)
        except Exception:
            pass
        ctx.close(); browser.close()
    return result

# =========================
# 메인 수집기
# =========================
def fetch_models(url: str, max_models=50):
    market, accept_lang, tz, geo = _guess_market_and_lang(url)
    vp = _viewport(viewport_choice)

    payloads, dom_models = [], []

    NAV_TMO = int(timeout_sec * 1000)
    IDLE_TMO = int(max(5, timeout_sec - 4) * 1000)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(**PLAYWRIGHT_LAUNCH_KW)
        ctx = browser.new_context(
            viewport=vp,
            extra_http_headers={"Accept-Language": accept_lang},
            locale=accept_lang.split(",")[0],
            timezone_id=tz,
            geolocation=geo,
            permissions=["geolocation"],
            ignore_https_errors=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"),
        )
        page = ctx.new_page()
        ctx.set_default_timeout(NAV_TMO)
        page.set_default_timeout(NAV_TMO)

        # Fast mode: 이미지/폰트/애널리틱스 차단
        if fast_mode:
            def _should_block(req_url: str, rtype: str) -> bool:
                ul = req_url.lower()
                if any(d in ul for d in ANALYTICS_BLOCK): return True
                if rtype in ("image", "font", "media"): return True
                return False
            page.route("**/*", lambda route:
                       route.abort() if _should_block(route.request.url, route.request.resource_type)
                       else route.continue_())

        # 네트워크 응답 훅
        def on_resp(resp):
            try:
                ul = resp.url.lower()
                if _is_blocked_analytics(ul): return
                if not _looks_like_product_api(ul): return
                ct = (resp.headers.get("content-type") or "").lower()
                body = None
                if "json" in ct:
                    try: body = resp.json()
                    except Exception: body = None
                if body is None:
                    try:
                        txt = resp.text()
                        if txt and "{" in txt and len(txt) < 2_000_000:
                            body = json.loads(txt)
                    except Exception: body = None
                if body is None:
                    try:
                        req = resp.request
                        if req and req.method.lower() == "post":
                            pdata = req.post_data or ""
                            if "{" in pdata and len(pdata) < 2_000_000:
                                body = json.loads(pdata)
                    except Exception:
                        pass
                if body is not None:
                    payloads.append({"url": resp.url, "data": body})
                    if debug_log:
                        st.write({"captured_json": resp.url})
            except Exception:
                pass
        page.on("response", on_resp)

        if debug_log:
            def on_req(req):
                ul = (req.url or "").lower()
                if _looks_like_product_api(ul) and not _is_blocked_analytics(ul):
                    st.write({"xhr/fetch": req.method, "url": req.url[:300]})
            page.on("request", on_req)

        # 이동 + 재시도
        last_err, ready = None, False
        for attempt in range(retries + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TMO)
                ready = wait_until_ready(page, IDLE_TMO, debug_log)
                if ready: break
            except Exception as e:
                last_err = e
            if debug_log:
                st.write(f"[debug] goto 재시도 {attempt+1}/{retries} (ready={bool(ready)})")
        else:
            if last_err:
                raise last_err

        # 쿠키 배너 닫기
        for sel in [
            "button[id*='accept']","button[aria-label*='Accept']",".onetrust-accept-btn-handler",
            "button:has-text('Accept all')","button:has-text('Accept All')",
            "button:has-text('Alle akzeptieren')","button:has-text('Aceptar todo')","button:has-text('Aceptar todas')",
        ]:
            try: page.locator(sel).first.click(timeout=1200); break
            except Exception: pass

        # 더보기
        try:
            for btn_text in ["Show more","더보기","Load more","See more","Mehr anzeigen","Voir plus","Ver más","Mostra altro"]:
                page.get_by_text(btn_text, exact=False).first.click(timeout=1200)
        except Exception: pass

        # 스크롤
        try:
            for _ in range(3 if fast_mode else 6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(600)
            page.evaluate("window.scrollTo(0,0)")
        except Exception: pass

        # __NEXT_DATA__/window 파싱
        try:
            txt = page.locator("script#__NEXT_DATA__").first.inner_text(timeout=2000)
            data = json.loads(txt)
            tmp_rows = []
            extract_models_from_json(data, tmp_rows)
            dom_models.extend({"Model": r["Model"].upper()} for r in tmp_rows)
        except Exception:
            pass
        try:
            raw = page.evaluate("() => (window.__NEXT_DATA__ || window.__APOLLO_STATE__ || null)")
            if raw:
                tmp_rows = []
                extract_models_from_json(raw, tmp_rows)
                dom_models.extend({"Model": r["Model"].upper()} for r in tmp_rows)
        except Exception:
            pass

        # DOM 폴백 + 링크 slug 추출
        try:
            cards = page.locator(CARD_SEL)
            c = min(cards.count(), 80)
            for i in range(c):
                card = cards.nth(i)
                blob = ""
                try: blob += card.inner_text() + " "
                except Exception: pass

                # 링크 href에서 slug 추출
                links = card.locator("a[href]")
                lcnt = min(links.count(), 10)
                for li in range(lcnt):
                    try:
                        href = links.nth(li).get_attribute("href") or ""
                        blob += " " + href
                        slug = href.split("/")[-1]
                        for pat in MODEL_PATTERNS:
                            m = re.search(pat, slug, re.I)
                            if m:
                                dom_models.append({"Model": m.group(0).upper()})
                                break
                    except Exception:
                        pass

                # 데이터 속성 추가 스캔
                for el in card.locator("img, a, *").all()[:30]:
                    for a in ("alt","aria-label","href","src","data-model","data-modelcode","data-sku","data-product-id"):
                        try:
                            v = el.get_attribute(a)
                            if v: blob += f" {v}"
                        except Exception: pass

                for pat in MODEL_PATTERNS:
                    m = re.search(pat, blob, re.I)
                    if m:
                        dom_models.append({"Model": m.group(0).upper()})
                        break
        except Exception:
            pass

        # PDP 1건 폴백
        try:
            if not dom_models:
                cards = page.locator(CARD_SEL)
                if cards.count() > 0:
                    first_card = cards.nth(0)
                    link = first_card.locator("a[href]").first
                    if link and link.count() > 0:
                        href = link.get_attribute("href") or ""
                        if href and href.startswith("/"):
                            parts = page.url.split("/", 3)
                            base = parts[0] + "//" + parts[2]
                            href = base + href
                        pdp = ctx.new_page()
                        pdp.set_default_timeout(NAV_TMO)
                        pdp_ready = False
                        for attempt in range(retries + 1):
                            try:
                                pdp.goto(href, wait_until="domcontentloaded", timeout=NAV_TMO)
                                pdp_ready = wait_until_ready(pdp, IDLE_TMO, debug_log)
                                if pdp_ready: break
                            except Exception:
                                pass
                        if not pdp_ready and debug_log:
                            st.write("[debug] PDP 폴백 준비 미완료, 그래도 ld+json 시도")

                        try:
                            ld_cnt = pdp.locator("script[type='application/ld+json']").count()
                            for i in range(min(ld_cnt, 10)):
                                try:
                                    raw = pdp.locator("script[type='application/ld+json']").nth(i).inner_text(timeout=800)
                                    data = json.loads(raw)
                                    tmp = []
                                    extract_models_from_json(data, tmp)
                                    for r in tmp:
                                        dom_models.append({"Model": r["Model"].upper()})
                                except Exception:
                                    pass

                            html = ""
                            try: html = pdp.content()
                            except Exception: pass
                            if html:
                                for pat in MODEL_PATTERNS:
                                    for m in re.findall(pat, html, flags=re.I):
                                        dom_models.append({"Model": m.upper()})
                        finally:
                            pdp.close()
        except Exception:
            pass

        # CTA 샘플 추정(메트릭 기반)
        cta_types = {"LearnMore_Type": UNKNOWN, "LearnMore_Shape": UNKNOWN,
                     "BuyNow_Shape": UNKNOWN, "Compare_Pos": UNKNOWN}
        try:
            cards = page.locator(CARD_SEL); n = min(cards.count(), 2)
            if n > 0:
                card = cards.nth(0)
                buy = card.locator(BUY_SEL).first
                learn = card.locator(LEARN_SEL).first
                cmpb = card.locator(CMP_SEL).first

                if learn and learn.count() > 0:
                    lm_type, lm_shape = _classify_cta(page, learn)
                    cta_types["LearnMore_Type"]  = lm_type
                    cta_types["LearnMore_Shape"] = lm_shape

                cta_types["BuyNow_Shape"] = _btn_shape(buy) if buy and buy.count() > 0 else UNKNOWN
                cta_types["Compare_Pos"] = "Center" if cmpb and cmpb.count() > 0 else UNKNOWN
        except Exception:
            pass

        if take_screens:
            try:
                path = os.path.join(os.getcwd(), f"plp_sample_{int(time.time())}.png")
                page.screenshot(path=path, full_page=True)
                st.caption(f"스크린샷 저장: {path}")
            except Exception:
                pass

        ctx.close(); browser.close()

    # 네트워크 JSON walk
    results = []
    def walk(o):
        if isinstance(o, dict):
            model = o.get("modelCode") or o.get("model") or o.get("code") or o.get("sku")
            title = o.get("name") or o.get("title")
            row = {}
            if model: row["Model"] = str(model).upper()
            if title: row["Title"] = str(title)
            if row: results.append(row)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)

    for pl in payloads:
        try: walk(pl["data"])
        except Exception: pass

    # ===== 합치기 + 노이즈 필터 (✅ sku 베이스 모델 보정 포함) =====
    bundle = []
    if results: bundle.extend(results)
    if dom_models: bundle.extend(dom_models)

    seen = set(); out = []
    for r in bundle:
        m = r.get("Model") or ""
        t = r.get("Title", "")

        # 점(.)이 있는 SKU면 베이스 모델 우선
        candidate = (m.split(".")[0].upper() if m else "")
        if not candidate:
            mm = re.search(r"(?:OLED|QNED|NANO)\d{2,3}[A-Z0-9\-]+", t or "", re.I)
            if mm: candidate = mm.group(0).upper()
        if not candidate:
            continue

        # 노이즈 필터
        if re.fullmatch(r"\d+", candidate):  # 숫자만
            continue
        alnum = re.sub(r"[^A-Za-z0-9]", "", candidate)
        if len(alnum) < 4:
            continue
        if not (re.search(r"[A-Za-z]", candidate) and re.search(r"\d", candidate)):
            # 그래도 TV 패턴 맞으면 통과
            hit = None
            for pat in MODEL_PATTERNS:
                mm = re.search(pat, candidate, re.I)
                if mm:
                    hit = mm.group(0).upper()
                    break
            if not hit:
                continue
            candidate = hit
        if re.match(r"^MD\d+$", candidate, re.I):  # BV ID 제외
            continue

        nm = norm_model(candidate)
        if nm in seen: 
            continue
        seen.add(nm)
        out.append({"Model": candidate})
        if len(out) >= max_models:
            break

    if debug_log:
        st.write(f"[debug] payloads={len(payloads)} dom={len(dom_models)} unique={len(out)}")
        if len(out) == 0 and payloads:
            st.write({"sample_payload_urls": [p["url"] for p in payloads[:5]]})

    # CTA 보정(필요 시 샘플 페이지로 재판정)
    if any(v == UNKNOWN for v in (cta_types["LearnMore_Type"], cta_types["BuyNow_Shape"])):
        try:
            strict = classify_template_sample(url)
            for k, v in strict.items():
                if cta_types.get(k, UNKNOWN) == UNKNOWN:
                    cta_types[k] = v
        except Exception:
            pass
    for k, default in (("LearnMore_Type","Text"), ("LearnMore_Shape", UNKNOWN),
                       ("BuyNow_Shape","Squared"), ("Compare_Pos","Center")):
        if cta_types.get(k) in (None, "", UNKNOWN):
            cta_types[k] = default

    return out, cta_types

# =========================
# 매칭 (동일 모델 우선, 없으면 각 페이지 첫 번째 모델 비교)
# =========================
def match_rows(a_models, b_models, a_types, b_types, want=2):
    rows, used_b = [], set()
    def _lm_str(types):
        t = types.get("LearnMore_Type", UNKNOWN)
        s = types.get("LearnMore_Shape", UNKNOWN)
        return f"{t}" + (f" ({s})" if s not in (None, "", UNKNOWN) else "")
    for a in a_models:
        best = None
        for j, b in enumerate(b_models):
            if j in used_b: continue
            if same_model(a["Model"], b["Model"]):
                best = (j, b); break
        if best:
            j, b = best
            used_b.add(j)
            rows.append({
                "Model (AsIs)": a["Model"],
                "Model (ToBe)": b["Model"],
                "Learn More (AsIs)": _lm_str(a_types),
                "Learn More (ToBe)": _lm_str(b_types),
                "Buy Now (AsIs)": a_types.get("BuyNow_Shape", UNKNOWN),
                "Buy Now (ToBe)": b_types.get("BuyNow_Shape", UNKNOWN),
                "Match": "Matched"
            })
        if len(rows) >= want: break

    # 동일 모델 전혀 없으면 각 페이지 첫 번째 모델로 비교 1건 생성
    if not rows and a_models and b_models:
        rows.append({
            "Model (AsIs)": a_models[0]["Model"],
            "Model (ToBe)": b_models[0]["Model"],
            "Learn More (AsIs)": _lm_str(a_types),
            "Learn More (ToBe)": _lm_str(b_types),
            "Buy Now (AsIs)": a_types.get("BuyNow_Shape", UNKNOWN),
            "Buy Now (ToBe)": b_types.get("BuyNow_Shape", UNKNOWN),
            "Match": "FirstInCategory"
        })
    return rows

# =========================
# 실행
# =========================
if run_btn:
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
