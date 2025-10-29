# save_naver_blog_category_to_pdf.py
# Python 3.10+ / Selenium 4+

import argparse
import base64
import random
import re
import time
import urllib.parse as urlparse
from time import localtime, strftime
from pathlib import Path
from typing import List, Optional, Set, Tuple

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


# -------------------- 유틸 --------------------
def safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180] if len(s) > 180 else s

def clean_title(title: str) -> str:
    t = title.strip()
    # 뒤꼬리 제거: " : 네이버 블로그" / " _ 네이버 블로그" / " - 네이버 블로그"
    t = re.sub(r"\s*[:\-_]\s*네이버\s*블로그\s*$", "", t)
    # 중복 공백 정리
    t = re.sub(r"\s{2,}", " ", t)
    return t

def join_abs(u: str) -> str:
    if u.startswith("http"):
        return u
    return urlparse.urljoin("https://blog.naver.com/", u)

def human_delay(base_sec: float):
    time.sleep(max(0.2, base_sec + random.uniform(-0.15, 0.25)))

def canonical_key_from_url(u: str) -> Optional[str]:
    m = re.search(r"logNo=(\d+)", u)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{6,})$", u)
    return m.group(1) if m else None

def parse_blog_id_logno(u: str) -> Tuple[Optional[str], Optional[str]]:
    blog_id = None
    log_no = canonical_key_from_url(u)
    m = re.search(r"[?&]blogId=([^&]+)", u)
    if m:
        blog_id = m.group(1)
    if not blog_id:
        m2 = re.search(r"m\.blog\.naver\.com/([^/]+)/(\d+)", u)
        if m2:
            blog_id, log_no = m2.group(1), m2.group(2)
    return blog_id, log_no


# ------ index.txt 기반 중복/목차 관리 ------
def load_done_keys_from_index(index_path: Path) -> Set[str]:
    keys: Set[str] = set()
    if index_path.exists():
        for ln in index_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if not parts:
                continue
            k = parts[0].strip()
            if k.isdigit():
                keys.add(k)
            else:
                k2 = canonical_key_from_url(k)
                if k2:
                    keys.add(k2)
    return keys

def append_index_row(index_path: Path, log_no: str, date_str: str, title: str, filename: str, url: str):
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(f"{log_no}\t{date_str}\t{title}\t{filename}\t{url}\n")


# -------------------- 크롬 드라이버 --------------------
def build_driver(method: str,
                 download_dir: Path,
                 user_data_dir: Optional[str] = None,
                 profile_dir: Optional[str] = None,
                 headless_devtools: bool = True) -> webdriver.Chrome:
    chrome_opts = Options()
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--window-size=1280,2000")
    chrome_opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    if user_data_dir:
        chrome_opts.add_argument(f"--user-data-dir={user_data_dir}")
    if profile_dir:
        chrome_opts.add_argument(f"--profile-directory={profile_dir}")

    if method == "devtools":
        if headless_devtools:
            chrome_opts.add_argument("--headless=new")
    elif method == "kiosk":
        chrome_opts.add_argument("--kiosk-printing")
        prefs = {
            "savefile.default_directory": str(download_dir.resolve()),
            "printing.print_preview_sticky_settings.appState":
                '{"recentDestinations":[{"id":"Save as PDF","origin":"local","account":""}],'
                '"selectedDestinationId":"Save as PDF","version":2,"isHeaderFooterEnabled": false}'
        }
        chrome_opts.add_experimental_option("prefs", prefs)

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_opts)


# -------------------- 프레임 전환 --------------------
def try_switch_to_mainframe(driver: webdriver.Chrome) -> bool:
    try:
        driver.switch_to.default_content()
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        target = None
        for fr in frames:
            name = (fr.get_attribute("name") or "").lower()
            _id = (fr.get_attribute("id") or "").lower()
            if "mainframe" in name or "mainframe" in _id or name == "main" or _id == "main":
                target = fr
                break
        if not target and frames:
            for fr in frames:
                if fr.is_displayed():
                    target = fr
                    break
            target = target or frames[0]
        if target:
            driver.switch_to.frame(target)
            return True
        return False
    except Exception:
        return False


# -------------------- JSON API로 전체 목록 수집 --------------------
def enumerate_category_via_api(blog_id: str,
                               category_no: str,
                               count_per_page: int = 30,
                               debug: bool = False) -> List[str]:
    s = requests.Session()
    s.headers.update({
        "Referer": f"https://blog.naver.com/{blog_id}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    })

    page = 1
    seen = set()
    out = []
    total_reported = None

    while True:
        params = {
            "blogId": blog_id,
            "categoryNo": category_no,
            "parentCategoryNo": 0,
            "currentPage": page,
            "countPerPage": count_per_page,
        }
        r = s.get("https://blog.naver.com/PostTitleListAsync.naver", params=params, timeout=20)
        r.raise_for_status()

        j = {}
        try:
            j = r.json()
        except Exception:
            if debug:
                print(f"[API] JSON parse error at page={page}, retrying once...")
            time.sleep(0.5)
            r = s.get("https://blog.naver.com/PostTitleListAsync.naver", params=params, timeout=20)
            r.raise_for_status()
            j = r.json()

        if total_reported is None:
            total_reported = j.get("totalCount")

        posts = j.get("postList", []) or []
        if debug:
            print(f"[API] page={page} got {len(posts)} (total={total_reported})")

        if not posts:
            break

        added = 0
        for p in posts:
            log_no = p.get("logNo")
            if not log_no:
                continue
            if log_no not in seen:
                seen.add(log_no)
                out.append(f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}")
                added += 1

        if added == 0:
            break

        page += 1
        time.sleep(0.25)

    if debug:
        print(f"[INFO] API 수집 합계: {len(out)} (서버 totalCount={total_reported})")
    return out


# -------------------- 날짜 파싱 --------------------
MD_PATTERN = r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(?!\d)"  # 9.7. (연도 없음)

DATE_PATTERNS = [
    r"(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})",             # 2025-09-07 / 2025.09.07 / 2025/09/07
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",       # 2025년 9월 7일
    r"\b(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\b",      # 20250907123012
    r"\b(\d{4})(\d{2})(\d{2})\b",                           # 20250907
]

def is_plausible_ymd(y: int, m: int, d: int) -> bool:
    curr = localtime().tm_year
    if not (2005 <= y <= curr + 1):
        return False
    if not (1 <= m <= 12) or not (1 <= d <= 31):
        return False
    return True

def _fmt_if_valid(y: str, mo: str, d: str) -> Optional[str]:
    yi, mi, di = int(y), int(mo), int(d)
    if is_plausible_ymd(yi, mi, di):
        return f"{yi:04d}-{mi:02d}-{di:02d}"
    return None

def normalize_date(text: str, fallback_year: Optional[int] = None) -> Optional[str]:
    if not text:
        return None
    # 1) YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD
    m = re.search(DATE_PATTERNS[0], text)
    if m:
        out = _fmt_if_valid(m.group(1), m.group(2), m.group(3))
        if out:
            return out
    # 2) YYYY년 M월 D일
    m = re.search(DATE_PATTERNS[1], text)
    if m:
        out = _fmt_if_valid(m.group(1), m.group(2), m.group(3))
        if out:
            return out
    # 3) 14자리 20250907123012
    m = re.search(DATE_PATTERNS[2], text)
    if m:
        out = _fmt_if_valid(m.group(1), m.group(2), m.group(3))
        if out:
            return out
    # 4) 8자리 20250907
    m = re.search(DATE_PATTERNS[3], text)
    if m:
        out = _fmt_if_valid(m.group(1), m.group(2), m.group(3))
        if out:
            return out
    # 5) '9.7.' → fallback_year 적용
    m = re.search(MD_PATTERN, text)
    if m and fallback_year:
        out = _fmt_if_valid(str(fallback_year), m.group(1), m.group(2))
        if out:
            return out
    return None

def parse_publish_text(txt: str, fallback_year: int) -> Optional[str]:
    """
    예: '2025. 8. 31. 23:00' → 2025-08-31
    """
    if not txt:
        return None
    # 공백/점 다양한 케이스 흡수
    m = re.search(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})", txt)
    if m:
        return _fmt_if_valid(m.group(1), m.group(2), m.group(3))
    # 연도 없는 '9.7.'도 허용
    m = re.search(MD_PATTERN, txt)
    if m:
        return _fmt_if_valid(str(fallback_year), m.group(1), m.group(2))
    return None

def fetch_static_html(url: str, timeout: float = 10.0) -> Optional[str]:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://blog.naver.com/",
        }
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return None

def extract_date_from_html_text(html: str, fallback_year: Optional[int]) -> Optional[str]:
    if not html:
        return None
    # 0) #postListBody 내부의 se_publishDate 가장 먼저 시도
    m = re.search(
        r'<div[^>]+id=["\']postListBody["\'][^>]*>.*?<span[^>]*class="[^"]*se_publishDate[^"]*"[^>]*>(.*?)</span>',
        html, flags=re.IGNORECASE | re.DOTALL
    )
    if m:
        inner = re.sub(r"<[^>]+>", "", m.group(1))
        got = parse_publish_text(inner, fallback_year or localtime().tm_year)
        if got:
            return got
    # 1) 메타
    for pat in [
        r'<meta[^>]+property=["\']og:regDate["\'][^>]+content=["\'](\d{8,14})["\']',
        r'<meta[^>]+name=["\']og:regDate["\'][^>]+content=["\'](\d{8,14})["\']',
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m2 = re.search(pat, html, flags=re.IGNORECASE)
        if m2:
            got = normalize_date(m2.group(1), fallback_year=fallback_year)
            if got:
                return got
    # 2) JSON-LD
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html, flags=re.IGNORECASE)
    if m:
        got = normalize_date(m.group(1), fallback_year=fallback_year)
        if got:
            return got
    # 3) time[datetime]
    m = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if m:
        got = normalize_date(m.group(1), fallback_year=fallback_year)
        if got:
            return got
    # 4) 본문 텍스트
    return normalize_date(html, fallback_year=fallback_year)


# -------------------- 제목/날짜 추출 --------------------
def extract_title_and_date(driver: webdriver.Chrome, url: str, fallback_year: int) -> Tuple[str, str, bool]:
    used_upload_today = False

    # 제목
    title = ""
    try:
        ogt = driver.execute_script(
            "return (document.querySelector(\"meta[property='og:title']\")||{}).content || ''"
        )
        title = (ogt or "").strip()
    except Exception:
        pass
    if not title:
        try:
            title = (driver.title or "").strip()
        except Exception:
            title = "post"
    title = clean_title(title)

    # 레이지 보조
    try:
        driver.execute_script("window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.2));")
        time.sleep(0.3)
    except Exception:
        pass

    # 0) 가장 먼저: #postListBody 내부의 se_publishDate 우선
    raw_pub = ""
    try:
        raw_pub = driver.execute_script("""
        const getTxt = (root, sel) => {
          const el = root ? root.querySelector(sel) : null;
          return el ? (el.innerText || el.textContent || '') : '';
        };
        const body = document.querySelector('#postListBody') || document;
        const tries = [];
        tries.push(getTxt(body, 'span.se_publishDate.pcol2'));  // 1순위
        tries.push(getTxt(body, 'span.se_publishDate'));        // 2순위
        tries.push(getTxt(body, '.se_publishDate'));            // 3순위
        return tries.filter(Boolean).join(' | ');
        """)
    except Exception:
        pass
    date_norm = parse_publish_text(raw_pub, fallback_year)

    # 1) 데스크톱 메타/DOM/JSON-LD
    if not date_norm:
        try:
            raw = driver.execute_script("""
            const pick = sel => { const el = document.querySelector(sel);
              return el ? (el.content || el.getAttribute('content') || el.innerText || '') : ''; };
            const tries = [];
            tries.push(pick('meta[property="og:regDate"]'));
            tries.push(pick('meta[name="og:regDate"]'));
            tries.push(pick('meta[property="og:article:published_time"]'));
            tries.push(pick('meta[name="article:published_time"]'));
            tries.push(pick('time[datetime]'));
            tries.push(pick('.se-date'));
            tries.push(pick('#postViewArea .date'));
            tries.push(pick('.blog2_container .date'));
            tries.push(pick('[class*="publish"]'));
            tries.push(pick('[class*="date"]'));
            const ld = document.querySelector('script[type="application/ld+json"]');
            if (ld) { tries.push(ld.textContent || ''); }
            tries.push(document.body ? document.body.innerText.slice(0, 6000) : '');
            return tries.filter(Boolean).join(' | ');
            """)
            date_norm = normalize_date(raw, fallback_year=fallback_year)
        except Exception:
            pass

    # 2) 모바일 DOM (Selenium)
    if not date_norm:
        blog_id, log_no = parse_blog_id_logno(url)
        if blog_id and log_no:
            mobile_url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
            try:
                driver.get(mobile_url)
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(0.4)
                js = """
                const getTxt = (root, sel) => {
                  const el = root ? root.querySelector(sel) : null;
                  return el ? (el.innerText || el.textContent || '') : '';
                };
                const root = document.querySelector('#postListBody') || document;
                const pick = sel => { const el = document.querySelector(sel);
                  return el ? (el.content || el.getAttribute('content') || el.innerText || '') : ''; };
                const tries = [];
                tries.push(getTxt(root, 'span.se_publishDate.pcol2'));
                tries.push(getTxt(root, 'span.se_publishDate'));
                tries.push(getTxt(root, '.se_publishDate'));
                tries.push(pick('meta[property="og:regDate"]'));
                tries.push(pick('meta[name="og:regDate"]'));
                tries.push(pick('meta[property="article:published_time"]'));
                tries.push(pick('meta[name="article:published_time"]'));
                tries.push(pick('time[datetime]'));
                tries.push(document.body ? document.body.innerText.slice(0, 8000) : '');
                return tries.filter(Boolean).join(' | ');
                """
                raw_m = driver.execute_script(js)
                # 먼저 span 텍스트에서 시도
                span_try = re.split(r"\s*\|\s*", raw_m)[0]
                date_norm = parse_publish_text(span_try, fallback_year)
                if not date_norm:
                    date_norm = normalize_date(raw_m, fallback_year=fallback_year)
            except Exception:
                pass

    # 3) 정적 폴백 (requests)
    if not date_norm:
        html = fetch_static_html(url)
        if html:
            date_norm = extract_date_from_html_text(html, fallback_year=fallback_year)
    if not date_norm:
        blog_id, log_no = parse_blog_id_logno(url)
        if blog_id and log_no:
            html_m = fetch_static_html(f"https://m.blog.naver.com/{blog_id}/{log_no}")
            if html_m:
                date_norm = extract_date_from_html_text(html_m, fallback_year=fallback_year)

    # 4) 최후 폴백: 오늘 날짜 (업로드날짜)
    if not date_norm:
        date_norm = strftime("%Y-%m-%d")
        used_upload_today = True

    return (title or "post"), date_norm, used_upload_today


# -------------------- PDF 저장 --------------------
def save_post_as_pdf_devtools(driver: webdriver.Chrome, url: str, out_dir: Path, fallback_year: int) -> Optional[Tuple[Path, str, str]]:
    driver.get(url)
    try_switch_to_mainframe(driver)
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".se-main-container")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "#postViewArea")),
                EC.presence_of_element_located((By.TAG_NAME, "article")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "body")),
            )
        )
    except Exception:
        pass
    human_delay(0.8)

    title_check = driver.title or ""
    if ("네이버" in title_check and "로그인" in title_check) or ("로그인" in title_check):
        print(f"[SKIP] Login page: {url}")
        return None

    title, date_norm, used_upload_today = extract_title_and_date(driver, url, fallback_year=fallback_year)
    date_for_name = f"{date_norm}(업로드날짜)" if used_upload_today else date_norm

    base = f"{date_for_name}_{safe_filename(title)}"
    fpath = out_dir / f"{base}.pdf"

    if fpath.exists():
        _, log_no = parse_blog_id_logno(url)
        suffix = f"_{log_no}" if log_no else ""
        fpath = out_dir / f"{base}{suffix}.pdf"

    pdf = driver.execute_cdp_cmd("Page.printToPDF", {
        "landscape": False, "printBackground": True, "scale": 1.0,
        "paperWidth": 8.27, "paperHeight": 11.69,
        "marginTop": 0.4, "marginBottom": 0.4,
        "marginLeft": 0.4, "marginRight": 0.4,
        "preferCSSPageSize": False,
    })
    with open(fpath, "wb") as f:
        f.write(base64.b64decode(pdf["data"]))
    return fpath, title, date_for_name

def save_post_as_pdf_kiosk(driver: webdriver.Chrome, url: str, out_dir: Path, fallback_year: int) -> Optional[Tuple[Path, str, str]]:
    driver.get(url)
    try_switch_to_mainframe(driver)
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".se-main-container")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "#postViewArea")),
                EC.presence_of_element_located((By.TAG_NAME, "article")),
            )
        )
    except Exception:
        pass
    human_delay(0.5)

    title_check = driver.title or ""
    if ("네이버" in title_check and "로그인" in title_check) or ("로그인" in title_check):
        print(f"[SKIP] Login page: {url}")
        return None

    title, date_norm, used_upload_today = extract_title_and_date(driver, url, fallback_year=fallback_year)
    date_for_name = f"{date_norm}(업로드날짜)" if used_upload_today else date_norm

    base = f"{date_for_name}_{safe_filename(title)}"
    fpath = out_dir / f"{base}.pdf"
    if fpath.exists():
        _, log_no = parse_blog_id_logno(url)
        suffix = f"_{log_no}" if log_no else ""
        fpath = out_dir / f"{base}{suffix}.pdf"

    driver.execute_script("window.print();")
    for _ in range(40):
        pdfs = sorted(out_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if pdfs:
            latest = pdfs[0]
            if latest != fpath:
                try:
                    latest.rename(fpath)
                except Exception:
                    pass
            return fpath, title, date_for_name
        time.sleep(0.5)
    return None


# -------------------- 메인 --------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--blog-id", required=False)
    p.add_argument("--category-key", default="경제/주식/국제정세/사회")
    p.add_argument("--out", default="./naver_pdfs")
    p.add_argument("--method", choices=["devtools", "kiosk"], default="devtools")
    p.add_argument("--rate-sleep", type=float, default=1.2)
    p.add_argument("--urls-file", help="줄바꿈으로 URL을 담은 텍스트 파일")
    # 세션/프로필(선택)
    p.add_argument("--user-data-dir")
    p.add_argument("--profile-dir")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--category-no", help="카테고리 번호를 이미 아는 경우 직접 지정(예: 21)")
    p.add_argument("--index-file", default="index.txt", help="목차 파일명(기본: index.txt)")
    p.add_argument("--fallback-year", type=int, help="연도가 없는 'M.D.' 패턴에 적용할 연도(기본=현재연도)")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    index_path = out_dir / args.index_file
    done_keys = load_done_keys_from_index(index_path)

    if not args.fallback_year:
        args.fallback_year = localtime().tm_year

    driver = None
    try:
        driver = build_driver(args.method, out_dir, args.user_data_dir, args.profile_dir, headless_devtools=True)

        # URL 파일 모드
        if args.urls_file:
            with open(args.urls_file, "r", encoding="utf-8") as f:
                url_list = [ln.strip() for ln in f if ln.strip()]
            print(f"[INFO] URL file loaded: {len(url_list)} urls")

            # 중복 제거(순서 유지)
            seen = set()
            tmp = []
            for u in url_list:
                k = canonical_key_from_url(u) or u
                if k in seen:
                    continue
                seen.add(k)
                tmp.append(u)
            url_list = [u for u in tmp if (canonical_key_from_url(u) not in done_keys)]

            total_saved = 0
            for i, u in enumerate(url_list, 1):
                try:
                    if args.method == "devtools":
                        ret = save_post_as_pdf_devtools(driver, u, out_dir, fallback_year=args.fallback_year)
                    else:
                        ret = save_post_as_pdf_kiosk(driver, u, out_dir, fallback_year=args.fallback_year)
                    if ret:
                        fpath, title, date_for_name = ret
                        log_no = canonical_key_from_url(u) or ""
                        if log_no and log_no in done_keys:
                            print(f"[SKIP] Already indexed logNo={log_no}")
                        else:
                            append_index_row(index_path, log_no, date_for_name, title, fpath.name, u)
                            if log_no:
                                done_keys.add(log_no)
                        total_saved += 1
                        print(f"[{i}/{len(url_list)}] Saved: {fpath.name}")
                    else:
                        print(f"[{i}/{len(url_list)}] Skipped")
                except Exception as e:
                    print(f"[WARN] 실패: {u} -> {e}")
                human_delay(args.rate_sleep)
            print(f"\n[DONE] 총 저장: {total_saved}")
            return

        # (카테고리 모드는 필요 시 추가 구현)
        if not args.blog_id and not args.urls_file:
            print("[WARN] Neither --urls-file nor --blog-id provided.")

    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()
