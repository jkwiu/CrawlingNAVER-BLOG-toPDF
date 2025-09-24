# save_naver_blog_category_to_pdf.py
# Python 3.10+ / Selenium 4+

import argparse
import base64
import json
import random
import re
import time
import urllib.parse as urlparse
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

def load_done_keys(path: Path) -> Set[str]:
    keys: Set[str] = set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln.isdigit():
                keys.add(ln)
            else:
                k = canonical_key_from_url(ln)
                if k:
                    keys.add(k)
    return keys

def append_done_key(path: Path, key_or_url: str):
    k = key_or_url if key_or_url.isdigit() else canonical_key_from_url(key_or_url)
    if not k:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(k + "\n")


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


# -------------------- 카테고리 탐색(한 번만 써서 cat_no 알아옴) --------------------
def get_all_categories(driver: webdriver.Chrome, blog_id: str) -> List[Tuple[str, str, str]]:
    home = f"https://blog.naver.com/{blog_id}"
    driver.get(home)
    try_switch_to_mainframe(driver)
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    human_delay(1.0)

    elems = driver.find_elements(By.CSS_SELECTOR, "a, li, span, div, button")
    out, seen = [], set()
    for el in elems:
        txt = (el.text or "").strip()
        href = el.get_attribute("href") or el.get_attribute("data-url") or ""
        onclick = el.get_attribute("onclick") or ""
        title = el.get_attribute("title") or ""
        aria = el.get_attribute("aria-label") or ""
        data_cat = (
            el.get_attribute("data-category-no")
            or el.get_attribute("data-categoryno")
            or el.get_attribute("data-cate-no")
            or ""
        )
        data_param = el.get_attribute("data-parameter") or ""

        blob = f"{href} {onclick} {data_cat} {data_param}"
        m = re.search(r"categoryNo\s*=?\s*['\"]?(\d+)", blob) or re.search(r"changeCategory\(\s*(\d+)\s*\)", blob)
        if not m:
            continue
        cat_no = m.group(1)

        name = txt or title or aria or f"cat_{cat_no}"
        key = (name, cat_no)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            (
                name,
                join_abs(href) if href else f"https://blog.naver.com/PostList.naver?blogId={blog_id}&from=postList&categoryNo={cat_no}",
                cat_no,
            )
        )
    return out

def find_category_link_exact(driver: webdriver.Chrome, blog_id: str, category_key: str) -> Tuple[str, str]:
    cats = get_all_categories(driver, blog_id)
    for (text, href, cat_no) in cats:
        if text == category_key:
            return join_abs(href), cat_no
    for (text, href, cat_no) in cats:
        if category_key in text:
            return join_abs(href), cat_no
    raise RuntimeError(f"카테고리 '{category_key}'를 찾지 못했습니다.")


# -------------------- JSON API로 전체 목록 수집 --------------------
def enumerate_category_via_api(blog_id: str,
                               category_no: str,
                               count_per_page: int = 30,
                               debug: bool = False) -> List[str]:
    """
    동적 렌더링/캐시 문제를 피하려고 네이버 제목 목록 API 사용.
    페이지 수 입력 없이, postList가 빌 때까지 순회.
    """
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
            # 간혹 JSON parse 실패 시 재시도 한 번
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

        # 새로 추가된 게 없으면 중단
        if added == 0:
            break

        page += 1
        time.sleep(0.25)  # rate limit 완화

    if debug:
        print(f"[INFO] API 수집 합계: {len(out)} (서버 totalCount={total_reported})")
    return out


# -------------------- PDF 저장 --------------------
def save_post_as_pdf_devtools(driver: webdriver.Chrome, url: str, out_dir: Path) -> Optional[Path]:
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

    title = driver.title or "post"
    if ("네이버" in title and "로그인" in title) or ("로그인" in title):
        print(f"[SKIP] Login page: {url}")
        return None

    fpath = out_dir / f"{safe_filename(title)}.pdf"
    if fpath.exists():
        print(f"[SKIP] Exists: {fpath.name}")
        return None

    pdf = driver.execute_cdp_cmd(
        "Page.printToPDF",
        {
            "landscape": False,
            "printBackground": True,
            "scale": 1.0,
            "paperWidth": 8.27,   # A4 inch
            "paperHeight": 11.69,
            "marginTop": 0.4,
            "marginBottom": 0.4,
            "marginLeft": 0.4,
            "marginRight": 0.4,
            "preferCSSPageSize": False,
        },
    )
    data = base64.b64decode(pdf["data"])
    with open(fpath, "wb") as f:
        f.write(data)
    return fpath

def save_post_as_pdf_kiosk(driver: webdriver.Chrome, url: str, out_dir: Path) -> Optional[Path]:
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

    title = driver.title or "post"
    if ("네이버" in title and "로그인" in title) or ("로그인" in title):
        print(f"[SKIP] Login page: {url}")
        return None

    fpath = out_dir / f"{safe_filename(title)}.pdf"
    if fpath.exists():
        print(f"[SKIP] Exists: {fpath.name}")
        return None

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
            return fpath
        time.sleep(0.5)
    return None


# -------------------- 메인 --------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--blog-id", required=False)
    # 기본 카테고리: 요청하신 '경제/주식/국제정세/사회'
    p.add_argument("--category-key", default="경제/주식/국제정세/사회")
    p.add_argument("--out", default="./naver_pdfs")
    p.add_argument("--method", choices=["devtools", "kiosk"], default="devtools")
    p.add_argument("--rate-sleep", type=float, default=1.2)
    p.add_argument("--urls-file", help="줄바꿈으로 URL을 담은 텍스트 파일")
    # 세션/프로필(선택)
    p.add_argument("--user-data-dir")
    p.add_argument("--profile-dir")
    p.add_argument("--debug", action="store_true")
    # 고급: categoryNo를 직접 지정하고 싶을 때
    p.add_argument("--category-no", help="카테고리 번호를 이미 아는 경우 직접 지정(예: 21)")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 중복 방지: 이미 처리한 logNo 스킵
    done_path = out_dir / "done_urls.txt"
    done_keys = load_done_keys(done_path)

    driver = None
    try:
        driver = build_driver(args.method, out_dir, args.user_data_dir, args.profile_dir, headless_devtools=True)

        # URL 파일 모드
        if args.urls_file:
            with open(args.urls_file, "r", encoding="utf-8") as f:
                url_list = [ln.strip() for ln in f if ln.strip()]
            print(f"[INFO] URL file loaded: {len(url_list)} urls")
            url_list = [u for u in url_list if (canonical_key_from_url(u) not in done_keys)]
            total_saved = 0
            for i, u in enumerate(url_list, 1):
                try:
                    if args.method == "devtools":
                        path = save_post_as_pdf_devtools(driver, u, out_dir)
                    else:
                        path = save_post_as_pdf_kiosk(driver, u, out_dir)
                    if path:
                        total_saved += 1
                        print(f"[{i}/{len(url_list)}] Saved: {path.name}")
                        k = canonical_key_from_url(u)
                        if k and k not in done_keys:
                            append_done_key(done_path, k)
                            done_keys.add(k)
                    else:
                        print(f"[{i}/{len(url_list)}] Skipped")
                except Exception as e:
                    print(f"[WARN] 실패: {u} -> {e}")
                human_delay(args.rate_sleep)
            print(f"\n[DONE] 총 저장: {total_saved}")
            return

        # 카테고리 모드
        if not args.blog_id:
            p.error("--blog-id is required when not using --urls-file")

        # 1) categoryNo 확보 (이미 알면 --category-no 사용)
        if args.category_no:
            cat_no = args.category_no
            print(f"[INFO] categoryNo 직접 지정 = {cat_no}")
        else:
            _, cat_no = find_category_link_exact(driver, args.blog_id, args.category_key)
            print(f"[INFO] 카테고리 '{args.category_key}' -> categoryNo={cat_no}")

        # 2) JSON API로 전체 수집 (페이지수 인자 불필요)
        links = enumerate_category_via_api(args.blog_id, cat_no, debug=args.debug)
        print(f"[INFO] API로 수집된 글 수: {len(links)}")

        # 수집 링크 덤프
        dump_path = out_dir / f"collected_{safe_filename(args.category_key)}_{cat_no}_ALL.txt"
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write("\n".join(links))

        # 3) 이미 완료된 logNo 제외
        links = [u for u in links if (canonical_key_from_url(u) not in done_keys)]
        print(f"[INFO] 중복/완료 제외 후: {len(links)}")

        # 4) PDF 저장
        total_saved = 0
        for i, u in enumerate(links, 1):
            try:
                if args.method == "devtools":
                    path = save_post_as_pdf_devtools(driver, u, out_dir)
                else:
                    path = save_post_as_pdf_kiosk(driver, u, out_dir)
                if path:
                    total_saved += 1
                    print(f"[{i}/{len(links)}] Saved: {path.name}")
                    k = canonical_key_from_url(u)
                    if k and k not in done_keys:
                        append_done_key(done_path, k)
                        done_keys.add(k)
                else:
                    print(f"[{i}/{len(links)}] Skipped")
            except Exception as e:
                print(f"[WARN] 실패: {u} -> {e}")
            human_delay(args.rate_sleep)
        print(f"\n[DONE] 총 저장: {total_saved}")

    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()
