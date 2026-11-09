"""KCI → RIMS 성과 자동 반입 (단일 파일)

RIMS 관리자 로그인 → KCI 데이터반입(소속기관 검색어마다 검색·중복선택해제·반입시작)
→ KCI 저자매핑 → 성과추출(KCI만 체크) → 전체추출

준비:  pip install selenium python-dotenv  (없으면 처음 실행 때 자동 설치)
실행:  파일을 더블클릭하거나 run_kci.bat 실행
       python kci_rims_automation.py                  KCI 전 과정
       python kci_rims_automation.py --map-dry-run    확인만 (반입·저장·추출 안 함)
       python kci_rims_automation.py --no-map         KCI 반입까지만

※ wos_rims_automation.py 와 같은 코드에서 만든 파일입니다 (저자매핑·성과추출 기준 동일).
기록·오류 캡처는 이 파일 옆 logs 폴더에 남습니다.
"""
import argparse
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path



def _ensure_packages():
    """더블클릭 실행 대비: selenium / python-dotenv 가 없으면 처음 한 번 자동 설치."""
    import importlib.util
    import subprocess
    missing = [pip for mod, pip in (("selenium", "selenium"), ("dotenv", "python-dotenv"))
               if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"필요한 프로그램을 설치합니다: {', '.join(missing)} (처음 한 번만, 1~2분 걸릴 수 있음)")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_packages()

from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import (NoAlertPresentException,
                                        StaleElementReferenceException,
                                        TimeoutException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 가 없어도 동작
    def load_dotenv(*a, **k):
        return False

log = logging.getLogger("kci_rims")


# ============================================================
# 설정
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ── RIMS 계정 ───────────────────────────────────────────
# 아래 따옴표 안에 직접 넣으세요. 비워 두면 .env 파일의 RIMS_ID / RIMS_PW 를 사용합니다.
RIMS_ID = "" or os.getenv("RIMS_ID", "")
RIMS_PW = "" or os.getenv("RIMS_PW", "")

# ── URL ─────────────────────────────────────────────────
WOS_URL = "https://www.webofscience.com/wos/woscc/smart-search"
WOS_ADVANCED_URL = "https://www.webofscience.com/wos/woscc/advanced-search"  # 버튼을 못 찾을 때 대체 경로
RIMS_LOGIN_URL = "https://rims.krri.re.kr/index/login.do"

# ── 경로 ────────────────────────────────────────────────
# 비워 두면 C:\Users\<사용자>\Downloads 를 사용
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR") or (Path.home() / "Downloads"))
ARCHIVE_DIR = BASE_DIR / "archive"   # 업로드 끝난 파일을 날짜 붙여 보관
LOG_DIR = BASE_DIR / "logs"          # 실행 기록 + 오류 화면 캡처

# ── 브라우저 ────────────────────────────────────────────
BROWSER = os.getenv("BROWSER", "chrome").lower()   # chrome | edge
HEADLESS = False                                   # 화면에 띄워서 진행 (사용자 결정)

# ── 대기 시간(초) ───────────────────────────────────────
WAIT_TIMEOUT = 15          # 일반 요소 대기 (명세서: WebDriverWait 15초)
LONG_TIMEOUT = 60          # WOS 첫 접속(IP 인증)·검색 결과 로딩
DOWNLOAD_TIMEOUT = 180     # savedrecs.txt 다운로드 완료 대기
UPLOAD_TIMEOUT = 120       # RIMS 반입 완료 알림 대기

# ── WOS 검색 조건 ───────────────────────────────────────
WOS_AFFILIATION_KEYWORD = "krri"
WOS_AFFILIATION_NAME = "Korea Railroad Research Institute (KRRI)"
WOS_EXPORT_MAX = 1000      # WOS 1회 내보내기 한도("No more than 1000 records at a time")

# ── 화면 문구 (띄어쓰기는 무시하고 비교) ────────────────
TEXT_WOS_ADVANCED = "Advanced Search"
TEXT_WOS_FIELD_DEFAULT = "Topic"
TEXT_WOS_FIELD_AFFIL = "Affiliation"
TEXT_WOS_SEARCH = "Search"
TEXT_WOS_REFINE = "Refine"
TEXT_WOS_EXPORT = "Export"
TEXT_WOS_TAB_DELIMITED = "Tab delimited file"
TEXT_WOS_RECORDS_FROM = "Records from"
TEXT_WOS_FULL_RECORD = "Full Record"

TEXT_RIMS_ADMIN = "관리자"            # 로그인 후 선택하는 '관리자'
TEXT_RIMS_MENU = "데이터반입"          # 상단 주황색 메뉴
TEXT_RIMS_SUBMENU = "WOS데이터반입"    # 하위 메뉴
TEXT_RIMS_TAB_FORMAT = "탭으로구분된형식"
TEXT_RIMS_IMPORT_BUTTON = "데이터반입"  # 하단 실행 버튼

# ── WOS 저자매핑 (3단계) ────────────────────────────────
TEXT_RIMS_MAP_SUBMENU = "WOS저자매핑"   # 데이터반입 하위 메뉴
TEXT_MAP_STATUS_LABEL = "작업상태"
TEXT_MAP_TODO = "미작업"
TEXT_MAP_SAVE = "저장"
TEXT_MAP_DONE = "작업완료"
# 왼쪽 '저자' 표와 오른쪽 '연구자' 표의 열 제목 (화면 그대로)
# (화면의 열 제목을 빠짐없이 적어야 값이 옆 칸으로 잘못 읽히지 않음)
MAP_AUTHOR_COLS = ["저자명(약어)", "저자명", "연구자명(매핑)", "사번(매핑)", "이메일(매핑)", "역할"]
MAP_RESEARCHER_COLS = ["성명(한글)", "성명(영문)", "단과대명", "학과명", "입사일", "퇴직일", "사번", "이메일"]
# KRRI 소속 판단: 'Korea Railroad Res Inst', 'Korea Railroad Research Institute', 'KRRI'
# ('Korea Railroad Corp' = 코레일은 제외)
KRRI_AFFIL_RE = re.compile(r"korea\s*railroad\s*res|\bkrri\b|한국철도기술연구원", re.I)
IMPORT_DONE_TIMEOUT = 600  # 반입 처리(상태 '완료')를 기다리는 최대 시간(초)

# ── 성과추출 (4단계) ────────────────────────────────────
TEXT_RIMS_EXTRACT = "성과추출"        # 데이터반입 하위 메뉴(없으면 화면의 버튼/링크)
TEXT_EXTRACT_SOURCE = "WOS"           # 기본 체크돼 있어야 하는 항목
TEXT_EXTRACT_ALL = "전체추출"          # 마무리 버튼
EXTRACT_TIMEOUT = 600                 # 전체추출 완료 알림 대기(초)

# ── KCI 논문 반입 (--kci) ───────────────────────────────
TEXT_RIMS_KCI_SUBMENU = "KCI데이터반입"
TEXT_RIMS_KCI_MAP_SUBMENU = "KCI저자매핑"
KCI_AFFIL_KEYWORDS = ["", ""]  # 각각 검색해서 각각 반입
TEXT_KCI_AFFIL_LABEL = "소속기관"      # '저자 소속기관' 입력칸 앞 글자
TEXT_KCI_PUBDATE_LABEL = "발행연월"    # 시작 칸에 올해 01월(예: 202601)
TEXT_KCI_DEDUP = "중복선택해제"
TEXT_KCI_START = "반입시작"
MAP_MAX_PAPERS = 500       # 한 번에 확인할 최대 논문 수 (무한 반복 방지)


# ============================================================
# 다운로드 파일 관리
# ============================================================
PATTERN = "savedrecs*"
PARTIAL_SUFFIXES = (".crdownload", ".tmp", ".part", ".partial")


def clean_old_files(download_dir: Path) -> int:
    """실행 전 다운로드 폴더의 savedrecs* 파일(미완성 포함)을 모두 삭제."""
    removed = 0
    for f in Path(download_dir).glob(PATTERN):
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            log.warning("삭제 실패: %s (%s)", f, e)
    # 크롬은 미완성 파일을 'Unconfirmed 123.crdownload'로 만들기도 함
    for f in Path(download_dir).glob("*.crdownload"):
        try:
            f.unlink()
        except OSError:
            pass
    log.info("기존 savedrecs 파일 %d개 삭제", removed)
    return removed


def snapshot(download_dir: Path) -> set[Path]:
    return set(Path(download_dir).glob(PATTERN))


def _has_partial(download_dir: Path) -> bool:
    """savedrecs 를 받는 중인 미완성 파일이 있는지 (폴더에 남은 다른 미완성 파일은 무시)."""
    return any(p.suffix.lower() in PARTIAL_SUFFIXES
               and p.name.lower().startswith(("savedrecs", "unconfirmed", ".com.google.chrome"))
               for p in Path(download_dir).iterdir())


def wait_for_new_download(download_dir: Path, before: set[Path], timeout: int) -> Path:
    """before 이후 새로 생긴 savedrecs*.txt 가 완전히 저장될 때까지 대기 후 경로 반환.

    완료 조건: 미완성 확장자 파일이 없고, 파일 크기가 0보다 크며 2회 연속 같음.
    """
    download_dir = Path(download_dir)
    deadline = time.time() + timeout
    last_size, stable = -1, 0
    while time.time() < deadline:
        new = [p for p in snapshot(download_dir) - before
               if p.suffix.lower() == ".txt"]
        if new and not _has_partial(download_dir):
            target = max(new, key=lambda p: p.stat().st_mtime)
            size = target.stat().st_size
            if size > 0 and size == last_size:
                stable += 1
                if stable >= 2:
                    with open(target, encoding="utf-8-sig", errors="ignore") as fh:
                        n = max(sum(1 for line in fh if line.strip()) - 1, 0)  # 첫 줄은 항목명
                    log.info("다운로드 완료: %s (%s bytes, 논문 %d건)", target.name, f"{size:,}", n)
                    return target
            else:
                stable = 0
            last_size = size
        time.sleep(1)  # 파일 시스템 폴링 간격 (화면 요소 대기가 아님)
    seen = sorted(p.name for p in download_dir.iterdir() if p.name.lower().startswith(("savedrecs", "unconfirmed")))
    raise TimeoutError(f"{timeout}초 안에 savedrecs 다운로드가 끝나지 않았습니다: {download_dir} (보이는 파일: {seen or '없음'})")


def archive(files: list[Path], archive_dir: Path) -> list[Path]:
    """업로드 끝난 파일을 archive/YYYY-MM-DD_HHMMSS_<원래이름> 으로 이동."""
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    moved = []
    for f in files:
        dest = archive_dir / f"{stamp}_{Path(f).name.replace(' ', '')}"
        shutil.move(str(f), dest)
        moved.append(dest)
        log.info("보관: %s", dest)
    return moved


# ============================================================
# 브라우저·대기 도우미
# ============================================================
CLICKABLE_TAGS = ("button", "a", "span", "li", "label", "div", "mat-option", "p", "td")


def create_driver(download_dir: Path) -> WebDriver:
    """다운로드 폴더를 지정한 브라우저 생성 (Selenium 4.6+ 가 드라이버 자동 설치)."""
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,  # 1000건 초과 시 연속 다운로드 허용
    }
    if BROWSER == "edge":
        opts = webdriver.EdgeOptions()
    else:
        opts = webdriver.ChromeOptions()
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--start-maximized")
    opts.add_argument("--window-size=1600,1000")
    opts.add_argument("--lang=en-US")  # WOS 화면을 영어로 고정 (버튼 문구 일치용)
    if HEADLESS:
        opts.add_argument("--headless=new")
    driver = webdriver.Edge(options=opts) if BROWSER == "edge" else webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(LONG_TIMEOUT)
    return driver


def _text_xpath(text: str, tags=CLICKABLE_TAGS, exact=True) -> str:
    """띄어쓰기·줄바꿈을 무시하고 문구로 요소를 찾는 XPath."""
    key = text.replace(" ", "")
    norm = "translate(normalize-space(.),'  ','')"
    cond = f"{norm}='{key}'" if exact else f"contains({norm},'{key}')"
    return " | ".join(f"//{t}[{cond}]" for t in tags)


def wait_visible(driver, by, value, timeout=WAIT_TIMEOUT) -> WebElement:
    return WebDriverWait(driver, timeout).until(EC.visibility_of_element_located((by, value)))


def wait_clickable(driver, by, value, timeout=WAIT_TIMEOUT) -> WebElement:
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))


def find_by_text(driver, text, tags=CLICKABLE_TAGS, exact=True, timeout=WAIT_TIMEOUT) -> WebElement:
    """문구가 일치하는 '보이는' 요소 중 가장 안쪽(자식이 같은 문구를 갖지 않는) 요소를 반환."""
    xpath = _text_xpath(text, tags, exact)

    def _pick(d):
        cands = [e for e in d.find_elements(By.XPATH, xpath) if _safe_displayed(e)]
        return cands[-1] if cands else False  # 문서 순서상 마지막 = 가장 안쪽

    return WebDriverWait(driver, timeout).until(_pick)


def click_text(driver, text, tags=CLICKABLE_TAGS, exact=True, timeout=WAIT_TIMEOUT) -> WebElement:
    el = find_by_text(driver, text, tags, exact, timeout)
    safe_click(driver, el)
    return el


def safe_click(driver, el: WebElement):
    """일반 클릭이 가려져 실패하면 스크롤 후 JS 클릭으로 재시도."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: el.is_enabled())
        el.click()
    except Exception:  # 가려짐(ElementClickIntercepted)·Stale 등
        driver.execute_script("arguments[0].click();", el)


def _safe_displayed(el) -> bool:
    try:
        return el.is_displayed()
    except StaleElementReferenceException:
        return False


def switch_to_frame_containing(driver, xpath, timeout=WAIT_TIMEOUT) -> bool:
    """본문 또는 iframe(1~2단계) 안에서 xpath 요소가 있는 곳으로 전환. 찾으면 True."""

    def _search(d):
        d.switch_to.default_content()
        if d.find_elements(By.XPATH, xpath):
            return True
        for i, _ in enumerate(d.find_elements(By.TAG_NAME, "iframe")):
            d.switch_to.default_content()
            frames = d.find_elements(By.TAG_NAME, "iframe")
            if i >= len(frames):
                break
            d.switch_to.frame(frames[i])
            if d.find_elements(By.XPATH, xpath):
                return True
            for j, _ in enumerate(d.find_elements(By.TAG_NAME, "iframe")):
                inner = d.find_elements(By.TAG_NAME, "iframe")
                if j >= len(inner):
                    break
                d.switch_to.frame(inner[j])
                if d.find_elements(By.XPATH, xpath):
                    return True
                d.switch_to.parent_frame()
        d.switch_to.default_content()
        return False

    try:
        return WebDriverWait(driver, timeout).until(_search)
    except TimeoutException:
        return False


# RIMS가 쓰는 화면 안 확인창(dhtmlx). 보이는 상자의 문구와 '확인' 쪽 버튼을 찾음
_DLG_JS = r"""
const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
                   return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
const boxes = [...document.querySelectorAll(
  '.dhtmlx_modal_box, .dhx_modal_box, .dhtmlx-alert, .dhtmlx-confirm, .dhx_alert, .dhx_popup--window, .dhx_message, .dhx_window, [role=alertdialog]')]
  .filter(vis);
for (const b of boxes) {
  const tEl = b.querySelector('.dhtmlx_popup_text, .dhx_alert__content, .dhx_message__text, .dhx_alert__header');
  const text = (tEl || b).innerText.trim();
  const btns = [...b.querySelectorAll('div, button, a, span, input[type=button]')].filter(vis);
  const label = e => (e.value || e.innerText || '').trim();
  const yes = ['확인', '예', 'OK', 'Ok', 'Yes', '적용', '반입', '반입시작'];
  let btn = b.querySelector('[result="true"]')
         || btns.find(e => yes.includes(label(e)) && !e.querySelector('div,button,a,span'))
         || btns.find(e => yes.includes(label(e)))
         || btns.find(e => ['닫기', 'Close'].includes(label(e)));
  if (btn) return {text, btn: btn.closest('button, a, [result], .dhtmlx_popup_button, .dhx_button') || btn};
}
return null;
"""


def _page_dialog(driver):
    try:
        return driver.execute_script(_DLG_JS)
    except Exception:
        return None


def accept_alerts(driver, timeout=3) -> list[str]:
    """뜨는 알림창(브라우저 기본 + RIMS 화면 안 확인창)을 모두 '확인' 누르고 문구 목록을 반환."""
    texts = []
    while True:
        def _any(d):
            try:
                d.switch_to.alert
                return "native"
            except NoAlertPresentException:
                pass
            dlg = _page_dialog(d)
            return dlg if dlg else False
        try:
            found = WebDriverWait(driver, timeout, poll_frequency=0.5).until(_any)
        except TimeoutException:
            return texts
        if found == "native":
            try:
                alert = driver.switch_to.alert
                texts.append(alert.text)
                log.info("알림창: %s", alert.text)
                alert.accept()
            except NoAlertPresentException:
                pass
        else:
            text = " ".join(found["text"].split())
            texts.append(text)
            log.info("화면 확인창: %s → 확인", text)
            try:
                found["btn"].click()
            except Exception:
                driver.execute_script("arguments[0].click();", found["btn"])
        timeout = 3


def wait_no_cover(driver, timeout=LONG_TIMEOUT):
    """화면을 덮는 회색 덮개(조회중·확인창)가 사라질 때까지 대기. 확인창이면 확인을 누름."""
    cover_js = """return [...document.querySelectorAll('.dhx_modal_cover, .dhtmlx_modal_cover, .dhx_cover, .dhx-cover')]
                  .some(e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
                               return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; });"""
    def _clear(d):
        if _page_dialog(d):
            accept_alerts(d, timeout=1)
        try:
            return not d.execute_script(cover_js)
        except Exception:
            return True
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.5).until(_clear)
    except TimeoutException:
        log.warning("화면 덮개가 %d초 동안 사라지지 않았습니다.", timeout)


def switch_to_newest_window(driver):
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])


def save_screenshot(driver, name: str) -> Path | None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
        driver.switch_to.default_content()
        driver.save_screenshot(str(path))
        log.info("화면 캡처: %s", path)
        html = path.with_suffix(".html")  # 실제 화면 구조 확인용
        html.write_text(driver.page_source, encoding="utf-8")
        log.info("화면 HTML 저장: %s", html)
        return path
    except Exception as e:  # 캡처 실패가 본래 오류를 가리지 않도록
        log.warning("화면 캡처 실패: %s", e)
        return None


# ============================================================
# 1단계: Web of Science
# ============================================================
def _dismiss_cookie_banner(driver):
    """쿠키 동의 배너가 버튼을 가리는 경우 닫기 (없으면 통과)."""
    for text in ("Accept all cookies", "Accept All Cookies", "Accept all", "I accept"):
        try:
            click_text(driver, text, tags=("button",), timeout=2)
            log.info("쿠키 배너 닫음")
            return
        except TimeoutException:
            continue


# ── [단계 1-1] 접속 + Advanced Search ───────────────────
def open_advanced_search(driver):
    driver.get(WOS_URL)
    # IP 인증 후 메인 화면이 뜰 때까지 대기
    WebDriverWait(driver, LONG_TIMEOUT).until(
        EC.presence_of_element_located((By.XPATH, "//*[contains(normalize-space(.),'Advanced Search')]")))
    _dismiss_cookie_banner(driver)
    try:
        click_text(driver, TEXT_WOS_ADVANCED, tags=("a", "button", "span"))
    except TimeoutException:
        log.warning("Advanced Search 버튼을 못 찾아 주소로 직접 이동")
        driver.get(WOS_ADVANCED_URL)
    # 그림 2: 검색 필드 드롭다운(Topic)이 보이면 진입 완료
    find_by_text(driver, TEXT_WOS_FIELD_DEFAULT, timeout=LONG_TIMEOUT)
    log.info("[1-1] Advanced Search 진입")


# ── [단계 1-2] Affiliation = KRRI 검색 ──────────────────
def search_affiliation(driver):
    # 그림 2: 'Topic' 드롭다운 → 'Affiliation'
    click_text(driver, TEXT_WOS_FIELD_DEFAULT)
    click_text(driver, TEXT_WOS_FIELD_AFFIL)

    # 검색어 입력창: placeholder가 'Example:' 로 시작하는 입력칸
    box = wait_clickable(driver, By.XPATH,
                         "//input[starts-with(@placeholder,'Example') or contains(@aria-label,'Search box')]")
    box.clear()
    box.send_keys(WOS_AFFILIATION_KEYWORD)

    # 그림 3: 자동완성 'Korea Railroad Research Institute (KRRI)' 클릭
    option = find_by_text(driver, WOS_AFFILIATION_NAME,
                          tags=("mat-option", "li", "span", "div", "button", "a"),
                          timeout=WAIT_TIMEOUT)
    safe_click(driver, option)
    log.info("[1-2] Affiliation 선택: %s", WOS_AFFILIATION_NAME)

    # 오른쪽 아래 'Search' 버튼 (돋보기 아이콘 글자가 붙어 있어 '포함'으로 찾음, 그림 3)
    search_xp = ("//button[contains(normalize-space(.),'{0}')"
                 " and not(contains(.,'Smart')) and not(contains(.,'Advanced'))"
                 " and not(contains(.,'Search in')) and not(contains(.,'Assistant'))"
                 " and not(contains(.,'Clear'))]").format(TEXT_WOS_SEARCH)
    try:
        btn = WebDriverWait(driver, WAIT_TIMEOUT).until(
            lambda d: ([e for e in d.find_elements(By.XPATH, search_xp) if e.is_displayed() and e.is_enabled()] or [False])[-1])
        safe_click(driver, btn)
    except TimeoutException:
        log.warning("Search 버튼을 못 찾아 검색창에서 Enter로 검색")
        box.send_keys(Keys.ENTER)
    _wait_results(driver)


def _wait_results(driver) -> int:
    """'N results from ...' 문구가 뜰 때까지 기다리고 N 반환."""
    def _count(d):
        m = re.search(r"([\d,]+)\s+results?\s+from", d.find_element(By.TAG_NAME, "body").text)
        return int(m.group(1).replace(",", "")) if m else False

    n = WebDriverWait(driver, LONG_TIMEOUT).until(_count)
    log.info("검색 결과 %d건", n)
    return n


# ── [단계 1-3] 당해연도 필터 ────────────────────────────
def refine_current_year(driver, year: int) -> int:
    """그림 4·5: Publication Years 에서 year 체크 → 같은 구역의 Refine 클릭. 결과 건수 반환."""
    before = _wait_results(driver)
    y = str(year)
    year_label = WebDriverWait(driver, WAIT_TIMEOUT).until(
        EC.presence_of_element_located((By.XPATH,
            "//*[normalize-space(.)='Publication Years']"
            f"/following::*[(self::label or self::mat-checkbox or self::span) and normalize-space(.)='{y}'][1]")))
    safe_click(driver, year_label)

    # 체크한 연도 바로 아래에 있는 첫 번째 Refine 버튼 (그림 5)
    refine = wait_clickable(driver, By.XPATH,
        "//*[normalize-space(.)='Publication Years']"
        f"/following::*[normalize-space(.)='{y}'][1]"
        f"/following::button[normalize-space(.)='{TEXT_WOS_REFINE}'][1]")
    safe_click(driver, refine)

    # 화면에 'Refined By: Publication Years: 2026' 이 보일 때까지 대기 (그림 6)
    WebDriverWait(driver, LONG_TIMEOUT).until(
        lambda d: f"Publication Years: {y}" in d.find_element(By.TAG_NAME, "body").text)
    n = _wait_results(driver)
    log.info("[1-3] %s년 필터 적용: %d건 (필터 전 %d건)", y, n, before)
    return n


# ── [단계 1-4] Tab delimited 내보내기 ───────────────────
def _control_value(driver, el) -> str:
    return driver.execute_script(
        "const e=arguments[0]; return (e.value || e.textContent || '').trim();", el) or ""


def _select_full_record(driver, dialog):
    """'Record Content:' 아래 칸을 Full Record 로 바꿈. 선택 상자·버튼형·입력칸형 모두 처리."""
    key = TEXT_WOS_FULL_RECORD
    control = WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: dialog.find_element(By.XPATH,
        ".//*[contains(normalize-space(text()),'Record Content')]"
        "/following::*[self::select or self::input or self::button or self::mat-select"
        " or @role='combobox' or @aria-haspopup][1]"))
    if _control_value(driver, control) == key:
        return  # 이미 Full Record
    if control.tag_name.lower() == "select":
        from selenium.webdriver.support.ui import Select
        Select(control).select_by_visible_text(key)
    else:
        safe_click(driver, control)
        opt_xp = (f"//*[(@role='option' or self::mat-option or self::li or self::option)"
                  f" and normalize-space(.)='{key}']")  # 'Full Record and Cited References' 제외
        try:
            opt = WebDriverWait(driver, WAIT_TIMEOUT).until(
                lambda d: ([e for e in d.find_elements(By.XPATH, opt_xp) if e.is_displayed()] or [False])[-1])
            safe_click(driver, opt)
        except TimeoutException:
            if control.tag_name.lower() != "input":
                raise
            control.send_keys(Keys.CONTROL, "a")  # 입력칸형이면 직접 입력 후 Enter
            control.send_keys(key)
            control.send_keys(Keys.ENTER)
    WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: _control_value(d, control) == key)


def export_range(driver, start: int, end: int):
    """그림 6·7: Export → Tab delimited file → Records from start to end, Full Record → Export."""
    # 결과 목록 위 'Export ⌄' 버튼 (아이콘 문구가 붙을 수 있어 앞부분 일치, 'Export Refine' 은 제외)
    export_xp = (f"//button[starts-with(normalize-space(.),'{TEXT_WOS_EXPORT}') and not(contains(.,'Refine'))"
                 " and not(ancestor::*[@role='dialog'])]")
    export_btn = WebDriverWait(driver, WAIT_TIMEOUT).until(
        lambda d: ([e for e in d.find_elements(By.XPATH, export_xp) if e.is_displayed()] or [False])[0])
    safe_click(driver, export_btn)
    log.info("[1-4] Export 버튼 클릭")
    click_text(driver, TEXT_WOS_TAB_DELIMITED, exact=False,
               tags=("button", "a", "span", "li", "div"))
    log.info("[1-4] Tab delimited file 선택")

    # 팝업: 숨은 팝업 요소(쿠키·도움말 등)와 헷갈리지 않도록 제목 문구로 찾음 (그림 7)
    dialog_xp = ("//*[contains(normalize-space(text()),'Export Records')]"
                 "/ancestor::*[contains(normalize-space(.),'Record Content')][1]")
    dialog = WebDriverWait(driver, WAIT_TIMEOUT).until(
        lambda d: ([e for e in d.find_elements(By.XPATH, dialog_xp) if e.is_displayed()] or [False])[0])
    log.info("[1-4] Export 팝업 열림")

    # ① Record Options: 'Records from:' 라디오 (실제 라디오는 숨겨져 있을 수 있어 JS로 확인)
    radio_ok = driver.execute_script("""
        const dlg = arguments[0], key = arguments[1];
        const radios = [...dlg.querySelectorAll('input[type=radio]')];
        let r = radios.find(x => {
            const box = x.closest('label, mat-radio-button, [role=radio], div');
            return box && box.textContent.replace(/\\s/g,'').includes(key);
        }) || radios[radios.length - 1];
        if (!r) return false;
        const box = r.closest('label, mat-radio-button') || r;
        box.click();
        if (!r.checked) r.click();
        return r.checked;
    """, dialog, TEXT_WOS_RECORDS_FROM.replace(" ", ""))
    if not radio_ok:  # 라디오 입력이 없는 구조면 문구를 직접 클릭
        safe_click(driver, find_by_text(driver, TEXT_WOS_RECORDS_FROM, exact=False,
                                        tags=("label", "span", "mat-radio-button", "div")))
    log.info("[1-4] Records from 선택")

    # ② from / to 입력칸: 'Record Content' 칸과 라디오를 뺀 보이는 입력칸 중 앞의 2개
    def _range_inputs(d):
        els = [e for e in dialog.find_elements(By.XPATH,
               ".//input[not(@type='radio') and not(@type='checkbox') and not(@type='hidden')"
               " and not(@role='combobox') and not(@aria-autocomplete) and not(@readonly)]")
               if e.is_displayed() and e.is_enabled()]
        return els if len(els) >= 2 else False
    inputs = WebDriverWait(driver, WAIT_TIMEOUT).until(_range_inputs)
    for el, val in ((inputs[0], start), (inputs[1], end)):
        el.click()
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(str(val))
    log.info("[1-4] 범위 입력: %d ~ %d", start, end)

    # ③ Record Content: Full Record
    _select_full_record(driver, dialog)
    log.info("[1-4] Full Record 선택")

    # 팝업 안의 Export 버튼
    btn = WebDriverWait(driver, WAIT_TIMEOUT).until(
        lambda d: ([e for e in dialog.find_elements(By.XPATH,
                    f".//button[starts-with(normalize-space(.),'{TEXT_WOS_EXPORT}')]")
                    if e.is_displayed() and e.is_enabled()] or [False])[-1])
    safe_click(driver, btn)

    # 팝업이 닫히면 다운로드 시작
    def _closed(d):
        try:
            return not dialog.is_displayed()
        except StaleElementReferenceException:
            return True
    WebDriverWait(driver, LONG_TIMEOUT).until(_closed)
    log.info("[1-4] 내보내기 요청: %d ~ %d", start, end)


def download_current_year(driver, download_dir: Path, year: int | None = None) -> list[Path]:
    """WOS 전 과정 실행. 다운로드된 savedrecs 파일 목록 반환 (1000건 넘으면 여러 개)."""
    year = year or datetime.now().year
    open_advanced_search(driver)
    search_affiliation(driver)
    total = refine_current_year(driver, year)
    if total == 0:
        log.warning("%d년 실적이 0건이라 내보낼 것이 없습니다.", year)
        return []

    files = []
    for start in range(1, total + 1, WOS_EXPORT_MAX):
        end = min(start + WOS_EXPORT_MAX - 1, total)
        before = snapshot(download_dir)
        export_range(driver, start, end)
        files.append(wait_for_new_download(download_dir, before, DOWNLOAD_TIMEOUT))
    return files


# ============================================================
# 2단계: RIMS 업로드
# ============================================================
# ── [단계 2-1] 로그인 ───────────────────────────────────
def login(driver):
    if not RIMS_ID or not RIMS_PW:
        raise RuntimeError("RIMS 아이디/비밀번호가 비어 있습니다. 파일 맨 위 RIMS_ID/RIMS_PW 또는 .env 에 넣어 주세요.")
    driver.get(RIMS_LOGIN_URL)
    log.info("[2-1] RIMS 로그인 화면 접속")

    # 로그인 칸이 iframe 안에 있어도 찾음
    if not switch_to_frame_containing(driver, "//input[@type='password']", timeout=LONG_TIMEOUT):
        raise TimeoutException("RIMS 로그인 화면에서 비밀번호 입력칸을 찾지 못했습니다.")
    pw = wait_clickable(driver, By.CSS_SELECTOR, "input[type='password']")
    # 비밀번호 칸 바로 앞의 텍스트 입력칸 = 아이디 칸
    uid = driver.find_element(By.XPATH,
        "(//input[@type='password'])[1]/preceding::input"
        "[not(@type='hidden') and not(@type='checkbox') and not(@type='radio') and not(@type='password')][1]")
    uid.clear()
    uid.send_keys(RIMS_ID)
    pw.clear()
    pw.send_keys(RIMS_PW)
    log.info("[2-1] 아이디/비밀번호 입력")
    pw.send_keys(Keys.ENTER)
    alerts = accept_alerts(driver, timeout=2)  # 비밀번호 오류·변경 안내 등

    def _logged_in(d):
        """본문과 iframe 어디에도 보이는 비밀번호 칸이 없으면 로그인된 것으로 봄."""
        try:
            d.switch_to.default_content()
            scopes = [None] + list(range(len(d.find_elements(By.TAG_NAME, "iframe"))))
            for i in scopes:
                d.switch_to.default_content()
                if i is not None:
                    d.switch_to.frame(d.find_elements(By.TAG_NAME, "iframe")[i])
                if [e for e in d.find_elements(By.CSS_SELECTOR, "input[type='password']") if e.is_displayed()]:
                    return False
            return True
        except Exception:
            return False
        finally:
            try:
                d.switch_to.default_content()
            except Exception:
                pass
    try:
        WebDriverWait(driver, 5).until(_logged_in)
    except TimeoutException:
        # Enter로 안 넘어가면 로그인 버튼 클릭
        btn_xp = ("//button[contains(normalize-space(.),'로그인') or contains(translate(normalize-space(.),'LOGIN','login'),'login')]"
                  " | //input[(@type='submit' or @type='button' or @type='image')"
                  " and (contains(@value,'로그인') or contains(translate(@value,'LOGIN','login'),'login') or contains(@alt,'로그인'))]"
                  " | //a[normalize-space(.)='로그인' or contains(@onclick,'login') or contains(@href,'login')][not(contains(@href,'login.do'))]")
        switch_to_frame_containing(driver, "//input[@type='password']", timeout=5)
        btns = [e for e in driver.find_elements(By.XPATH, btn_xp) if e.is_displayed()]
        if not btns:
            raise TimeoutException(f"로그인 버튼을 찾지 못했습니다. 알림: {alerts}")
        log.info("[2-1] Enter로 안 넘어가 로그인 버튼 클릭")
        safe_click(driver, btns[0])
        alerts += accept_alerts(driver, timeout=2)
        try:
            WebDriverWait(driver, LONG_TIMEOUT).until(_logged_in)
        except TimeoutException:
            raise TimeoutException(f"로그인 후 다음 화면으로 넘어가지 않습니다. 알림: {alerts}") from None
    driver.switch_to.default_content()
    log.info("[2-1] RIMS 로그인 완료")


# ── [단계 2-2] 관리자 → 데이터반입 → WOS 데이터반입 ─────
def open_data_menu(driver, submenu: str):
    """상단 주황색 메뉴 '데이터반입' → 하위 메뉴 클릭 (마우스를 올려야 열리는 경우 대비)."""
    switch_to_frame_containing(driver, _text_xpath(TEXT_RIMS_MENU), timeout=LONG_TIMEOUT)
    # 같은 글자의 하단 '데이터반입' 실행 버튼을 누르지 않도록 화면 맨 위의 것(상단 메뉴)만 사용
    xp = _text_xpath(TEXT_RIMS_MENU, tags=("a", "span", "li"))
    menu = WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: min(
        [e for e in d.find_elements(By.XPATH, xp) if _safe_displayed(e)] or [None],
        key=lambda e: e.rect["y"] if e else 0) or False)
    ActionChains(driver).move_to_element(menu).perform()
    try:
        sub = find_by_text(driver, submenu, tags=("a", "span", "li"), timeout=3)
    except TimeoutException:
        safe_click(driver, menu)
        switch_to_frame_containing(driver, _text_xpath(submenu))
        sub = find_by_text(driver, submenu, tags=("a", "span", "li"))
    safe_click(driver, sub)


def select_admin(driver):
    """로그인 후 '관리자' 선택 → 관리자 메인 화면."""
    switch_to_frame_containing(driver, _text_xpath(TEXT_RIMS_ADMIN))
    click_text(driver, TEXT_RIMS_ADMIN, timeout=LONG_TIMEOUT)
    accept_alerts(driver, timeout=2)
    switch_to_newest_window(driver)  # 새 창으로 열리는 경우 대비
    log.info("관리자 화면 선택")


def go_to_wos_import(driver):
    # 로그인 후 '관리자' 선택 → 관리자 메인 화면
    switch_to_frame_containing(driver, _text_xpath(TEXT_RIMS_ADMIN))
    click_text(driver, TEXT_RIMS_ADMIN, timeout=LONG_TIMEOUT)
    accept_alerts(driver, timeout=2)
    switch_to_newest_window(driver)  # 새 창으로 열리는 경우 대비
    log.info("관리자 화면 선택")
    open_data_menu(driver, TEXT_RIMS_SUBMENU)

    # 그림 8: 'File Upload' 영역이 보이면 도착
    if not switch_to_frame_containing(driver, "//*[contains(normalize-space(.),'File Upload')]",
                                      timeout=LONG_TIMEOUT):
        raise TimeoutException("WOS 데이터반입 화면(File Upload)을 찾지 못했습니다.")
    log.info("[2-2] WOS 데이터반입 화면 진입")


def upload_files(driver, files: list[Path]) -> list[str]:
    """그림 8: 파일 형식 확인 → Add files 로 파일 첨부 → 데이터반입 클릭 → 알림 확인."""
    # 1. File Format: '탭으로 구분된 형식(Win, UTF-8)' 선택 (기본값이지만 확인)
    try:
        fmt = find_by_text(driver, TEXT_RIMS_TAB_FORMAT, exact=False,
                           tags=("label", "span", "td", "div"), timeout=5)
        radio = fmt.find_elements(By.XPATH, ".//input[@type='radio'] | preceding-sibling::input[@type='radio'][1]")
        if radio and not radio[0].is_selected():
            safe_click(driver, radio[0])
    except TimeoutException:
        log.warning("파일 형식 라디오를 찾지 못해 기본값으로 진행")

    # 2. File Upload: 'Add files' 뒤에 숨은 파일 입력칸에 경로를 직접 넣음 (윈도우 파일 창을 띄우지 않음)
    file_input = WebDriverWait(driver, WAIT_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")))
    driver.execute_script(
        "arguments[0].style.display='block'; arguments[0].style.visibility='visible';"
        "arguments[0].style.opacity=1; arguments[0].removeAttribute('hidden');", file_input)
    file_input.send_keys("\n".join(str(Path(f).resolve()) for f in files))
    for f in files:  # 목록에 파일 이름이 나타날 때까지 대기
        find_by_text(driver, Path(f).name, exact=False, tags=("span", "div", "td", "li", "a"))
    log.info("파일 첨부: %s", ", ".join(Path(f).name for f in files))

    # 3. 하단 '데이터반입' 버튼 (상단 메뉴와 같은 문구라 button/a 중 마지막 것)
    done_before = _count_done_jobs(driver)
    btn = find_by_text(driver, TEXT_RIMS_IMPORT_BUTTON, tags=("button", "a", "span"))
    safe_click(driver, btn)

    # 4. 확인/완료 알림창 수신
    alerts = accept_alerts(driver, timeout=UPLOAD_TIMEOUT)
    if not alerts:
        # 알림창이 없으면 아래 작업 목록에 오늘 날짜 + 파일명이 나타나는지로 확인
        today = datetime.now().strftime("%Y-%m-%d")
        name = Path(files[0]).name
        WebDriverWait(driver, UPLOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH,
                f"//tr[contains(normalize-space(.),'{today}') and contains(normalize-space(.),'{name}')]")))
        log.info("작업 목록에서 반입 확인")
    log.info("[2-2] 데이터 반입 실행 완료")
    wait_import_done(driver, done_before)
    return alerts


def _count_done_jobs(driver) -> int:
    """아래 작업 목록에서 오늘 날짜이면서 상태가 '완료'인 줄 수."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        return len(driver.find_elements(By.XPATH,
            f"//tr[contains(normalize-space(.),'{today}') and contains(normalize-space(.),'완료')]"))
    except Exception:
        return 0


def wait_import_done(driver, done_before: int):
    """반입 처리가 끝나 작업 목록에 '완료' 줄이 하나 늘 때까지 대기 (저자매핑 목록이 그 뒤에 생김)."""
    log.info("[2-3] 반입 처리 완료 대기 (최대 %d초)", IMPORT_DONE_TIMEOUT)
    try:
        WebDriverWait(driver, IMPORT_DONE_TIMEOUT, poll_frequency=5).until(
            lambda d: _count_done_jobs(d) > done_before)
        log.info("[2-3] 반입 처리 완료")
    except TimeoutException:
        log.warning("[2-3] 작업 목록에서 '완료'를 확인하지 못했습니다. 그대로 저자매핑을 진행합니다.")


def upload_to_rims(driver, files: list[Path]) -> list[str]:
    login(driver)
    go_to_wos_import(driver)
    return upload_files(driver, files)


# ============================================================
# 실행
# ============================================================
FAIL_WORDS = ("실패", "오류", "에러", "error", "fail")


def _has_fail(alerts) -> bool:
    """알림 문구에 실패 표현이 있는지. '에러: 0건', '오류 0건', '실패: 0건' 같은 0건 요약은 실패로 보지 않음."""
    for a in alerts:
        t = re.sub(r"(실패|오류|에러|error|fail)\w*\s*[:：]?\s*0\s*(건|개)?(?!\d)", "", a, flags=re.I)
        if any(w in t.lower() for w in FAIL_WORDS):
            return True
    return False


# ============================================================
# 3단계: WOS 저자매핑
# ============================================================
_GRID_JS = r"""
// 열 제목 글자의 화면 위치를 기준으로 표를 읽음 (표를 만드는 방식과 상관없이 동작)
// args: headers(열 제목 목록), yMin, yMax  →  [{num, cells{열:값}, sub[아래줄들], el}]
const headers = arguments[0], yMin = arguments[1] || 0, yMax = arguments[2] || 1e9;
const norm = s => (s || '').replace(/\s+/g, '');
const visible = el => {
  if (!el) return false;
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
};
function leaf(name) {
  const key = norm(name);
  return [...document.querySelectorAll('body *')].filter(e =>
    norm(e.textContent) === key && visible(e) &&
    ![...e.children].some(c => norm(c.textContent) === key)).map(e => { const b = e.getBoundingClientRect();
      return {top: b.top, bottom: b.bottom, left: b.left, right: b.right, width: b.width, height: b.height, el: e}; })
    .filter(r => r.top >= yMin && r.top <= yMax);
}
const first = leaf(headers[0]);
if (!first.length) return null;
const h0 = first[0];
const cols = [];
for (const name of headers) {
  const cands = leaf(name).filter(r => Math.abs((r.top + r.bottom) / 2 - (h0.top + h0.bottom) / 2) < 12);
  if (!cands.length) continue;
  cands.sort((a, b) => Math.abs(a.left - h0.left) - Math.abs(b.left - h0.left));
  cols.push({name, r: cands[0]});
}
// '#' 열: 첫 열 제목 왼쪽, 같은 높이에서 가장 가까운 '#'
const sharp = leaf('#').filter(r => Math.abs((r.top + r.bottom) / 2 - (h0.top + h0.bottom) / 2) < 12 && r.right <= h0.left + 2)
  .sort((a, b) => b.left - a.left)[0];
if (!sharp) return null;
cols.unshift({name: '#', r: sharp});
const xMin = Math.min(...cols.map(c => c.r.left)) - 15, xMax = Math.max(...cols.map(c => c.r.right)) + 15;
// 이 표의 영역: 열 제목들을 모두 품되, 옆 표의 제목(arguments[3])은 품지 않는 가장 큰 상자
const exclude = arguments[3];
const hasHeader = (el, name) => [...el.querySelectorAll('*')].some(e => norm(e.textContent) === norm(name) && ![...e.children].some(c => norm(c.textContent) === norm(name)));
let root = document.body;
if (exclude) {
  root = cols[0].r.el;
  while (root.parentElement && !cols.every(c => root.contains(c.r.el))) root = root.parentElement;
  while (root.parentElement && root.parentElement !== document.body && !hasHeader(root.parentElement, exclude)) root = root.parentElement;
}
const headBottom = Math.max(...cols.map(c => c.r.bottom));
const center = r => (r.left + r.right) / 2;
// 화면에 보이는 글자 조각 수집
const nodes = [];
const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
while (tw.nextNode()) {
  const t = tw.currentNode, txt = t.textContent.trim();
  if (!txt) continue;
  const pe = t.parentElement;
  if (!pe || ['SCRIPT', 'STYLE', 'OPTION', 'SELECT', 'TEXTAREA'].includes(pe.tagName) || !visible(pe) || !root.contains(pe)) continue;
  const rg = document.createRange(); rg.selectNodeContents(t);
  const r = rg.getBoundingClientRect();
  if (!r.width || r.top < headBottom - 1 || r.top > yMax || center(r) < xMin || center(r) > xMax) continue;
  nodes.push({txt, r, el: pe});
}
const sc = cols[0].r;
const anchors = nodes.filter(n => /^\d+$/.test(n.txt) &&
  cols.reduce((b, c) => Math.abs(center(c.r) - center(n.r)) < Math.abs(center(b.r) - center(n.r)) ? c : b).name === '#')
  .sort((a, b) => a.r.top - b.r.top);
const rows = anchors.map(a => ({num: parseInt(a.txt, 10), cells: {}, sub: [], el: a.el, top: a.r.top, h: a.r.height}));
for (const n of nodes) {
  if (anchors.includes(n)) continue;
  let row = null;
  for (const r of rows) if (r.top <= n.r.top + 4) row = r;
  if (!row) continue;
  if (n.r.top - row.top < row.h * 0.8) {
    const col = cols.slice(1).reduce((b, c) => Math.abs(center(c.r) - center(n.r)) < Math.abs(center(b.r) - center(n.r)) ? c : b);
    row.cells[col.name] = ((row.cells[col.name] || '') + ' ' + n.txt).trim();
  } else if (n.r.top - row.top < row.h * 5) {   // 저자 줄 바로 아래 몇 줄만 (표 아래 쪽수 표시 제외)
    row.sub.push(n.txt);
  }
}
// 화면 구조로도 읽기: 저자 줄 묶음(tr 등)과 바로 뒤에 딸린 줄들의 글자에서 칸 값이 아닌 줄 = 소속
const anchorEls = rows.map(r => r.el);
for (const r of rows) {
  const rc = r.el.closest('tr, [role=row]') || (r.el.parentElement && r.el.parentElement.parentElement);
  if (!rc) continue;
  const parts = [rc.innerText || ''];
  let sib = rc.nextElementSibling, k = 0;
  while (sib && k < 3 && !anchorEls.some(x => sib.contains(x))) { parts.push(sib.innerText || ''); sib = sib.nextElementSibling; k++; }
  const known = new Set([String(r.num), ...Object.values(r.cells)].map(norm));
  for (const line of parts.join('\n').split(/[\n\t]+/)) {
    const t = line.trim();
    if (t.length <= 1 || known.has(norm(t)) || r.sub.includes(t) || /^Records from|rows per page|^Page \d/i.test(t)) continue;
    if (Object.values(r.cells).some(v => norm(v) && norm(t).includes(norm(v)) && norm(t).length <= norm(v).length + 4)) continue;
    r.sub.push(t);
  }
}
return rows.map(r => ({num: r.num, cells: r.cells, sub: r.sub, el: r.el}));
"""


# RIMS 저자매핑 팝업의 표 데이터(dhtmlx authorGrid / rschGrid)를 직접 읽음.
# 소속 줄은 처음에 접혀 있다가 1.5초 뒤 펼쳐지므로 화면 글자 대신 데이터에서 읽는 것이 정확함.
_GRID_API_JS = r"""
const which = arguments[0];
const g = which === 'author' ? window.authorGrid : window.rschGrid;
if (!g || typeof g.getRowsNum !== 'function') return null;
const n = g.getRowsNum();
const ncol = g.getColumnsNum ? g.getColumnsNum() : 0;
const idx = id => { try { const i = g.getColIndexById(id); return (i === undefined || i === null || i < 0) ? -1 : i; } catch (e) { return -1; } };
const lab = i => { try { return String(g.getColLabel(i) || '').replace(/<[^>]*>/g, '').replace(/\s+/g, ''); } catch (e) { return ''; } };
const col = (ids, label) => { for (const id of ids) { const i = idx(id); if (i >= 0) return i; }
  if (label) for (let i = 0; i < ncol; i++) if (lab(i) === label.replace(/\s+/g, '')) return i; return -1; };
const txt = v => { const d = document.createElement('div'); d.innerHTML = v == null ? '' : String(v);
  return (d.innerText || d.textContent || '').replace(/\u00a0/g, ' ').trim(); };
const cell = (id, c) => { try { return g.cells(id, c); } catch (e) { return null; } };
const val = (id, c) => { if (c < 0) return ''; const x = cell(id, c); try { return x ? txt(x.getValue()) : ''; } catch (e) { return ''; } };
const td = (id, c) => { const x = cell(id, c); return (x && x.cell) || (g.getRowById ? g.getRowById(id) : null); };
const rows = [];
if (which === 'author') {
  const cSeq = col(['authrSeq'], '#'), cAbrv = col(['authrAbrv'], '저자명(약어)'), cNm = col(['authrNm'], '저자명'),
        cRN = col(['rePername'], '연구자명(매핑)'), cRP = col(['rePerno'], '사번(매핑)');
  let cAdr = -1;
  for (let i = 0; i < ncol; i++) { try { if (g.getColType(i) === 'sub_row') { cAdr = i; break; } } catch (e) {} }
  if (cAdr < 0) cAdr = col(['auuthorsAdres', 'authorsAdres', 'adres'], '');
  if (cNm < 0) return null;
  for (let i = 0; i < n; i++) {
    const id = g.getRowId(i);
    let num = parseInt(val(id, cSeq), 10);
    if (isNaN(num)) { const p = String(id).split(';'); num = parseInt(p[1] || (i + 1), 10); }
    let adr = val(id, cAdr);
    // 소속 칸에 주소(/workbench/findAuthrAddressAjax.do?…)가 들어 있으면 펼칠 때처럼 그 주소에서 소속 내용을 읽어 옴 (읽기만 함)
    if (/^(\/|https?:)/.test(adr) && /\.do|\?/.test(adr)) {
      try {
        const x = new XMLHttpRequest(); x.open('GET', adr, false); x.send(null);
        adr = (x.status === 200) ? txt(x.responseText.replace(/<(br|\/td|\/th|\/tr|\/div|\/p|\/li)[^>]*>/gi, '\n')) : '';
      } catch (e) { adr = ''; }
    }
    rows.push({num, cells: {'저자명(약어)': val(id, cAbrv), '저자명': val(id, cNm), '연구자명(매핑)': val(id, cRN), '사번(매핑)': val(id, cRP)},
               sub: adr ? adr.split(/\n+/).map(x => x.trim()).filter(Boolean) : [], el: td(id, cNm >= 0 ? cNm : 0)});
  }
} else {
  const c = {num: col(['authrSeq'], '#'), kor: col(['korNm'], '성명(한글)'), eng: col(['engNm'], '성명(영문)'), clg: col(['clgNm'], '단과대명'),
             dept: col(['deptKor', 'deptEng'], '학과명'), apt: col(['aptmDate'], '입사일'), ret: col(['retrDate'], '퇴직일'),
             no: col(['userId'], '사번'), mail: col(['email'], '이메일')};
  if (c.kor < 0 || c.no < 0) return null;
  for (let i = 0; i < n; i++) {
    const id = g.getRowId(i);
    rows.push({num: parseInt(val(id, c.num), 10), cells: {'성명(한글)': val(id, c.kor), '성명(영문)': val(id, c.eng), '단과대명': val(id, c.clg),
               '학과명': val(id, c.dept), '입사일': val(id, c.apt), '퇴직일': val(id, c.ret), '사번': val(id, c.no), '이메일': val(id, c.mail)},
               sub: [], el: td(id, c.kor)});
  }
}
return rows;
"""


def parse_grid(driver, headers, y_min=0, y_max=None) -> list[dict]:
    # ① 저자매핑 팝업이면 표 데이터를 직접 읽음
    which = "author" if headers is MAP_AUTHOR_COLS else "rsch" if headers is MAP_RESEARCHER_COLS else None
    if which:
        try:
            api_rows = driver.execute_script(_GRID_API_JS, which)
        except Exception:
            api_rows = None
        if api_rows is not None:
            return [r for r in api_rows if r.get("el") is not None]
    # ② 화면 글자로 읽기. 저자 표와 연구자 표가 나란히 있어 서로의 글자가 섞이지 않도록 옆 표의 제목을 경계로 씀
    exclude = (MAP_RESEARCHER_COLS[0] if headers is MAP_AUTHOR_COLS else
               MAP_AUTHOR_COLS[0] if headers is MAP_RESEARCHER_COLS else None)
    rows = driver.execute_script(_GRID_JS, headers, y_min, y_max, exclude)
    return rows or []


def _header_top(driver, name) -> float | None:
    return driver.execute_script("""
        const key = arguments[0].replace(/\\s+/g,'');
        const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\\s+/g,'');
        const els = [...document.querySelectorAll('body *')].filter(e =>
            own(e).includes(key) && e.getBoundingClientRect().height > 0);
        return els.length ? els[0].getBoundingClientRect().top : null;""", name)


def _norm_name(name: str) -> str:
    return re.sub(r"[\s\-.·]", "", (name or "")).lower()


def click_button(driver, text: str, timeout=WAIT_TIMEOUT):
    """버튼 클릭: ① 글자 정확히 일치 ② 아이콘 글자가 붙은 경우(예: '✖ 중복선택해제') 그 글자를 포함한
    가장 짧은 버튼 ③ 글자 없는 아이콘이면 title/alt/value. (띄어쓰기 무시)"""
    key = text.replace(" ", "")
    try:
        el = find_by_text(driver, text, tags=("button", "a", "span", "div", "li"), timeout=3)
    except TimeoutException:
        def _pick(d):
            norm = "translate(normalize-space(.),' \u00a0','')"
            els = [e for e in d.find_elements(By.XPATH,
                   f"//button[contains({norm},'{key}')] | //a[contains({norm},'{key}')]"
                   f" | //*[contains(translate(@title,' ',''),'{key}') or contains(translate(@alt,' ',''),'{key}')]"
                   f" | //input[(@type='button' or @type='submit') and contains(translate(@value,' ',''),'{key}')]")
                   if _safe_displayed(e)]
            if not els:
                return False
            return min(els, key=lambda e: len((e.text or e.get_attribute("title") or e.get_attribute("value") or "").strip()))
        try:
            el = WebDriverWait(driver, timeout).until(_pick)
        except TimeoutException:
            raise TimeoutException(f"'{text}' 버튼을 찾지 못했습니다.") from None
    safe_click(driver, el)
    return el


_BLUE_JS = r"""
// 검색 영역 안에서 파란 배경의 큰 칸(RIMS 파란 돋보기)을 찾음
const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, '');
const label = [...document.querySelectorAll('body *')].find(e => own(e).includes(arguments[0]) && e.getBoundingClientRect().height > 0);
if (!label) return null;
const area = label.closest('form') || (label.closest('table') && label.closest('table').parentElement) || label.parentElement;
const blue = e => {
  for (let x = e, i = 0; x && i < 3; x = x.parentElement, i++) {
    const m = getComputedStyle(x).backgroundColor.match(/\d+/g);
    if (m && m.length >= 3 && (m.length < 4 || +m[3] > 0)) {
      const [r, g, b] = m.map(Number);
      if (b > 150 && b > r + 50 && b > g + 10) return true;
      if (!(r === 0 && g === 0 && b === 0)) return false;   // 다른 색 배경을 만나면 중단
    }
  }
  return false;
};
const lr = label.getBoundingClientRect(), ly = (lr.top + lr.bottom) / 2;
const cands = [...area.querySelectorAll('*')].filter(e => {
  const r = e.getBoundingClientRect();
  // 검색 칸 줄 높이 근처의 파란 칸만 ('반입작업' 탭, '① 대상확인' 막대 제외)
  const near = r.top <= ly + 40 && r.bottom >= ly - 40;
  return r.width >= 20 && r.height >= 30 && near && blue(e) && !['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName);
}).sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
return cands.length ? cands[0] : null;
"""


def dump_page(driver, name: str):
    """화면 구조를 logs/screens/ 에 저장 (막혔을 때 실제 구조를 보고 고치기 위함)."""
    try:
        d = LOG_DIR / "screens"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.html"
        path.write_text(driver.page_source, encoding="utf-8")
        path.with_suffix(".txt").write_text(driver.page_source, encoding="utf-8")  # 채팅 첨부용 같은 내용
        log.info("화면 구조 저장: %s (.txt 도 함께)", path.name)
    except Exception as e:
        log.warning("화면 구조 저장 실패: %s", e)


def _click_search(driver, anchor=None, where="저자매핑"):
    """돋보기(검색) 버튼 클릭. ① 검색 영역의 파란 칸 ② 이름/글자로 찾은 가장 가까운 돋보기."""
    anchor = anchor or TEXT_MAP_STATUS_LABEL
    blue = driver.execute_script(_BLUE_JS, anchor)
    if blue is not None:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", blue)
        try:
            ActionChains(driver).move_to_element(blue).click().perform()  # 사람이 누르듯 칸 한가운데 클릭
        except Exception:
            driver.execute_script("arguments[0].click();", blue)
        log.info("[%s] 파란 돋보기 클릭", where)
        return accept_alerts(driver, timeout=2)
    btn = driver.execute_script("""
        const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\\s+/g,'');
        const label = [...document.querySelectorAll('body *')].find(e =>
            own(e).includes(arguments[0]) && e.getBoundingClientRect().height > 0);
        if (!label) return null;
        // 화면 순서와 상관없이 기준 글자에서 화면상 가장 가까운 돋보기 (파란 돋보기 칸이 표 오른쪽에 세로로 걸친 경우 대비)
        const all = [...document.querySelectorAll('button, a, input[type=button], input[type=image], input[type=submit], i, span, img, td[onclick], div[onclick], th[onclick], [role=button]')];
        const KW = /search|srch|검색|조회|magnif|돋보기|btn_find|find|zoom|glass/;
        const isSearch = e => {
            if (e.getBoundingClientRect().width <= 0) return false;
            const attrs = [typeof e.className === 'string' ? e.className : '', e.title, e.getAttribute('alt'), e.getAttribute('onclick'),
                           e.id, e.getAttribute('value'), e.getAttribute('src')].join(' ').toLowerCase();
            if (KW.test(attrs)) return true;
            // 글자로는 '검색'·'조회' 같은 짧은 버튼 글자만 인정 ('검색 조건 입력 후 검색 버튼을…' 같은 안내문 제외)
            const t = e.children.length ? '' : e.textContent.replace(/\\s+/g, '');
            return t.length > 0 && t.length <= 6 && KW.test(t);
        };
        const clickable = e => e.closest('button, a, input, [onclick], [role=button]') || e;
        // 검색 칸과 같은 표(검색 영역) 안의 돋보기를 먼저 고름
        const area = label.closest('form') || (label.closest('table') && label.closest('table').parentElement) || label.parentElement;
        const lr = label.getBoundingClientRect(), ly = (lr.top + lr.bottom) / 2, lx = (lr.left + lr.right) / 2;
        const cands = [...new Set(all.filter(isSearch).map(clickable))].filter(e => e.getBoundingClientRect().width > 0)
            .map(e => { const r = e.getBoundingClientRect();
                        const dy = Math.max(0, r.top - ly, ly - r.bottom), dx = Math.abs((r.left + r.right) / 2 - lx);
                        return {e, d: dy * 3 + dx, dy, inside: area && area.contains(e) ? 0 : 1}; })
            .filter(c => c.dy < 250).sort((a, b) => (a.inside - b.inside) || (a.d - b.d));
        return cands.length ? cands[0].e : null;""", anchor)
    if btn is None:
        raise TimeoutException(f"{where} 화면에서 돋보기(검색) 버튼을 찾지 못했습니다.")
    safe_click(driver, btn)
    return accept_alerts(driver, timeout=2)


def _select_todo_status(driver):
    """'작업상태'를 '미작업'으로 (선택 상자·라디오·버튼 모두 처리)."""
    label_xp = f"//*[normalize-space(text())='{TEXT_MAP_STATUS_LABEL}' or contains(normalize-space(text()),'{TEXT_MAP_STATUS_LABEL}')]"
    if not switch_to_frame_containing(driver, label_xp, timeout=LONG_TIMEOUT):
        raise TimeoutException("저자매핑 화면에서 '작업상태'를 찾지 못했습니다.")
    from selenium.webdriver.support.ui import Select
    # 선택 상자: '작업상태' 안쪽 또는 바로 뒤
    sels = driver.find_elements(By.XPATH,
        f"({label_xp})[1]/descendant::select | ({label_xp})[1]/following::select[position()<=2]")
    sels = [e for e in sels if any(TEXT_MAP_TODO in o.text for o in e.find_elements(By.TAG_NAME, "option"))]
    if sels:
        opt = [o for o in sels[0].find_elements(By.TAG_NAME, "option") if TEXT_MAP_TODO in o.text][0]
        Select(sels[0]).select_by_visible_text(opt.text)
        if TEXT_MAP_TODO not in Select(sels[0]).first_selected_option.text:
            raise RuntimeError("작업상태를 '미작업'으로 바꾸지 못했습니다.")
    else:
        # 라디오/버튼형: '작업상태' 근처(저자 표보다 위, 300px 이내)의 '미작업'만 누름.
        # 아래쪽 툴바의 '미작업' 버튼(논문 상태를 미작업으로 되돌림)은 절대 누르지 않음
        label = driver.find_element(By.XPATH, f"({label_xp})[1]")
        limit = min(label.rect["y"] + 300, (_header_top(driver, MAP_AUTHOR_COLS[0]) or 1e9) - 60)
        cands = [e for e in driver.find_elements(By.XPATH,
                 f"({label_xp})[1]/following::*[normalize-space(.)='{TEXT_MAP_TODO}' and not(*[normalize-space(.)='{TEXT_MAP_TODO}'])]"
                 f" | ({label_xp})[1]/descendant::*[normalize-space(.)='{TEXT_MAP_TODO}' and not(*[normalize-space(.)='{TEXT_MAP_TODO}'])]")
                 if _safe_displayed(e) and e.rect["y"] < limit]
        if not cands:
            raise RuntimeError("'작업상태' 근처에서 '미작업' 선택 항목을 찾지 못했습니다.")
        safe_click(driver, cands[0])
    log.info("[3-1] 작업상태: %s", TEXT_MAP_TODO)


def _paper_rows(driver) -> list[dict]:
    """미작업 논문 목록 (저자 표 위쪽에 있는 첫 번째 번호 목록)."""
    y_max = _header_top(driver, MAP_AUTHOR_COLS[0]) or 1e9
    status_top = _header_top(driver, TEXT_MAP_STATUS_LABEL) or 0
    for head in ("#", "No", "NO", "번호", "순번"):
        cols = driver.execute_script("""
            const yMin = arguments[1], yMax = arguments[2], key = arguments[0];
            const hs = [...document.querySelectorAll('body *')].filter(e => e.textContent.trim() === key &&
                e.children.length === 0 && e.getBoundingClientRect().height > 0 &&
                e.getBoundingClientRect().top > yMin && e.getBoundingClientRect().top < yMax);
            if (!hs.length) return null;
            const h = hs[0], hr = h.getBoundingClientRect();
            // 같은 줄의 다른 열 제목들
            let row = h.parentElement;
            for (let i = 0; i < 4 && row && row.children.length < 3; i++) row = row.parentElement;
            const names = [...row.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.trim()
                && Math.abs(e.getBoundingClientRect().top - hr.top) < 12 && e.getBoundingClientRect().left > hr.left)
                .map(e => e.textContent.trim());
            return {names, top: hr.top};""", head, status_top, y_max)
        if cols and cols["names"]:
            # 위쪽 검색 칸의 같은 글자(예: 논문명)와 헷갈리지 않도록 목록 제목 줄 높이부터만 찾음
            js = _GRID_JS.replace("leaf('#')", f"leaf({head!r})")
            rows = driver.execute_script(js, cols["names"], cols["top"] - 3, y_max) or []
            if rows:
                return rows
    return []


def _wait_authors(driver, prev_sig):
    """논문을 클릭한 뒤 왼쪽 저자 표가 새로 채워질 때까지 대기."""
    def _ready(d):
        rows = parse_grid(d, MAP_AUTHOR_COLS)
        sig = tuple((r["num"], r["cells"].get("저자명", "")) for r in rows)
        return rows if rows and sig != prev_sig else False
    try:
        return WebDriverWait(driver, WAIT_TIMEOUT).until(_ready)
    except TimeoutException:
        return parse_grid(driver, MAP_AUTHOR_COLS)


def _name_match(rec: dict, name: str) -> bool:
    """저자명이 추천 연구자의 성명(영문) 또는 성명(한글)과 같으면 True (KCI는 한글 이름도 나옴)."""
    key = _norm_name(name)
    kor = rec["cells"].get("성명(한글)", "") or ""
    cands = {_norm_name(rec["cells"].get("성명(영문)")), _norm_name(kor)} | {_norm_name(t) for t in kor.split()}
    return bool(key) and key in cands


def _choose_researcher(recs: list[dict], name: str) -> tuple[dict | None, str]:
    """시스템 추천 중 연결할 연구자 1명 선택. (연구자, 사유) — 못 고르면 (None, 사유).

    - 추천 1명이고 영문명이 같으면 그 사람
    - 영문명이 같은 추천이 여러 명이면 퇴직일이 비어 있는(재직 중) 사번이 딱 1명일 때만 그 사번
    """
    if not recs:
        return None, "시스템 추천 없음"
    same = [r for r in recs if _name_match(r, name)]
    if not same:
        return None, f"추천 이름 '{recs[0]['cells'].get('성명(한글)', '')} {recs[0]['cells'].get('성명(영문)', '')}' 불일치"
    if len(same) == 1 and len(recs) == 1:
        return same[0], ""
    active = [r for r in same if not r["cells"].get("퇴직일", "").strip()]
    if len(active) == 1:
        return active[0], f"[추천 {len(recs)}명 중 재직자]"
    return None, f"시스템 추천 {len(recs)}명(재직 {len(active)}명)으로 하나를 고를 수 없음"


_STRUCT_LOGGED = False


def map_one_paper(driver, authors: list[dict], dry_run: bool) -> tuple[int, int]:
    """한 논문의 저자 중 조건을 모두 만족하는 저자만 시스템 추천 연구자와 연결.

    반환: (이번에 연결한 수, 연결 안 됐거나 확인 안 된 KRRI 저자 수, KRRI 저자 수)
    """
    mapped = 0
    left = 0   # 연결되지 않았거나, 이미 연결됐지만 맞는지 확인되지 않은 KRRI 저자
    krri = 0   # KRRI 소속 저자 수
    n_krri = sum(1 for a in authors if KRRI_AFFIL_RE.search(" ".join(a["sub"])))
    global _STRUCT_LOGGED
    if authors and not any(a["sub"] for a in authors) and not _STRUCT_LOGGED:
        _STRUCT_LOGGED = True  # 소속을 하나도 못 읽으면 첫 저자 줄 주변 구조를 한 번 기록 (원인 확인용)
        try:
            snip = driver.execute_script("""
                const el = arguments[0];
                let x = el; for (let i = 0; i < 4 && x.parentElement; i++) x = x.parentElement;
                const nx = x.nextElementSibling;
                return 'ROW4UP=' + x.outerHTML.slice(0, 2500) + ' ||NEXT=' + (nx ? nx.outerHTML.slice(0, 1200) : '없음');""",
                authors[0]["el"])
            log.info("    [구조 진단] %s", " ".join((snip or "").split()))
        except Exception as e:
            log.info("    [구조 진단] 실패: %s", e)
    log.info("    저자 %d명 읽음 / KRRI 소속 %d명: %s", len(authors), n_krri,
             "; ".join(f"{a['num']} {a['cells'].get('저자명', '?')} [{' / '.join(a['sub'])[:40] or '소속 못 읽음'}]"
                       for a in authors)[:300] or "저자 표를 읽지 못함")
    for a in authors:
        num, name = a["num"], a["cells"].get("저자명", "")
        affil = " ".join(a["sub"])
        if not KRRI_AFFIL_RE.search(affil):
            continue  # KRRI 소속이 아니면 시스템 추천이 있어도 연결하지 않음
        krri += 1
        if a["cells"].get("연구자명(매핑)") or a["cells"].get("사번(매핑)"):
            # 이미 연결된 저자: 같은 번호의 추천 중 같은 사번이 있고 영문명이 같으면 맞게 연결된 것
            emp_m = a["cells"].get("사번(매핑)", "").strip()
            ok = emp_m and any(r["num"] == num and r["cells"].get("사번", "").strip() == emp_m
                               and _name_match(r, name)
                               for r in parse_grid(driver, MAP_RESEARCHER_COLS))
            if ok:
                log.info("    %d번 %s: 이미 맞게 연결됨 (%s %s)", num, name,
                         a["cells"].get("연구자명(매핑)", ""), emp_m)
            else:
                log.info("    %d번 %s: 이미 연결돼 있으나 추천 목록과 맞는지 확인 안 됨 (%s %s)",
                         num, name, a["cells"].get("연구자명(매핑)", ""), emp_m)
                left += 1
            continue
        left += 1  # 아래에서 연결에 성공하면 다시 뺌
        recs = [r for r in parse_grid(driver, MAP_RESEARCHER_COLS) if r["num"] == num]
        rec, why = _choose_researcher(recs, name)
        if rec is None:
            log.info("    %d번 %s: %s → 건너뜀", num, name, why)
            continue
        emp = rec["cells"].get("사번", "")
        who = f"{rec['cells'].get('성명(한글)', '')}({emp}){' ' + why if why else ''}"
        if dry_run:
            log.info("    %d번 %s → %s 연결 예정 [확인만]", num, name, who)
            mapped += 1
            left -= 1
            continue
        # 왼쪽 저자 선택 → 오른쪽 추천 연구자 더블클릭
        safe_click(driver, a["el"])
        rec2 = [r for r in parse_grid(driver, MAP_RESEARCHER_COLS)
                if r["num"] == num and r["cells"].get("사번", "") == emp]
        if len(rec2) != 1:
            log.warning("    %d번 %s: 저자 선택 후 추천 목록이 바뀌어 건너뜀", num, name)
            continue
        def _is_mapped(d):
            return any(r["num"] == num and (r["cells"].get("연구자명(매핑)") or r["cells"].get("사번(매핑)"))
                       for r in parse_grid(d, MAP_AUTHOR_COLS))
        el = rec2[0]["el"]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        # 왼쪽 저자(노란색) 선택 상태에서 오른쪽 추천 연구자를 한 번 클릭 → 사번이 들어감
        ActionChains(driver).click(el).perform()
        accept_alerts(driver, timeout=1)
        ok = False
        try:
            WebDriverWait(driver, 3).until(_is_mapped)
            ok = True
        except TimeoutException:
            # 한 번 클릭으로 안 들어가면 더블클릭
            rec3 = [r for r in parse_grid(driver, MAP_RESEARCHER_COLS)
                    if r["num"] == num and r["cells"].get("사번", "") == emp]
            if rec3:
                ActionChains(driver).double_click(rec3[0]["el"]).perform()
                accept_alerts(driver, timeout=2)
                try:
                    WebDriverWait(driver, WAIT_TIMEOUT).until(_is_mapped)
                    ok = True
                except TimeoutException:
                    pass
        if ok:
            log.info("    %d번 %s → %s 연결", num, name, who)
            mapped += 1
            left -= 1
        else:
            log.warning("    %d번 %s: 클릭 후 사번이 들어가지 않아 연결 확인 안 됨", num, name)
    return mapped, left, krri


def _finish_paper(driver, n: int, left: int, krri: int, dry_run: bool, st: dict) -> bool:
    """한 논문의 연결 결과로 저장/작업완료 처리. 작업완료했으면 True."""
    complete = krri > 0 and left == 0   # KRRI 저자가 있고 모두 맞게 연결됨
    if dry_run:
        if n or complete:
            log.info("[3-2]   → %s%s [확인만]", f"{n}명 연결 예정, " if n else "이미 모두 맞게 연결됨, ",
                     "작업완료 예정" if complete else f"KRRI 저자 {left}명 미연결 → 저장만 예정")
            st["authors"] += n
            st["done" if complete else "saved"] += 1
        return False
    if not n and not complete:
        return False
    click_button(driver, TEXT_MAP_SAVE)
    alerts = accept_alerts(driver, timeout=3)
    if complete:  # KRRI 저자를 모두 (맞게) 연결했을 때만 작업완료
        click_button(driver, TEXT_MAP_DONE)
        alerts += accept_alerts(driver, timeout=3)
    if _has_fail(alerts):
        raise RuntimeError(f"저장/작업완료 중 오류 알림: {alerts}")
    st["authors"] += n
    if complete:
        log.info("[3-2]   → %s저장 후 작업완료", f"{n}명 연결, " if n else "이미 모두 맞게 연결돼 있어 ")
        st["done"] += 1
        return True
    log.info("[3-2]   → %d명 연결, KRRI 저자 %d명이 남아 저장만 함 (작업완료 안 함)", n, left)
    st["saved"] += 1
    return False


def _map_inline(driver, dry_run: bool, st: dict, first_row: dict):
    """(저자 표가 같은 화면에 나오는 경우) 목록에서 논문을 하나씩 클릭하며 처리."""
    def _key(r):
        return max(r["cells"].values(), key=len) if r["cells"] else str(r["num"])
    prev_sig, row, clicked = None, first_row, True
    for _ in range(MAP_MAX_PAPERS):
        if row is None:
            try:
                rows = WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: _paper_rows(d) or False)
            except TimeoutException:
                rows = []
            # 논문 구분은 가장 긴 칸(논문명)으로 — 상태 칸이 바뀌어도 같은 논문으로 봄
            row = next((r for r in rows if _key(r) not in st["seen"]), None)
            if row is None:
                break
            clicked = False
        title = _key(row)
        st["seen"].add(title)
        log.info("[3-2] 논문 %d: %s", len(st["seen"]), title[:80])
        if not clicked:
            safe_click(driver, row["el"])
        authors = _wait_authors(driver, prev_sig)
        prev_sig = tuple((r["num"], r["cells"].get("저자명", "")) for r in authors)
        n, left, krri = map_one_paper(driver, authors, dry_run)
        if _finish_paper(driver, n, left, krri, dry_run, st):
            _click_search(driver)  # 작업완료된 논문이 빠진 목록으로 새로고침
            prev_sig = None
        row = None


def _popup_info(driver) -> tuple[str, str]:
    """팝업의 (논문명, 작업상태). 상태는 '논문정보 (KCI : 작업완료)' 의 괄호 안 ':' 뒤."""
    title = driver.execute_script("""
        const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\\s+/g,'');
        const lab = [...document.querySelectorAll('th, td, label, span, div, dt')].find(e => own(e) === '논문명' && e.getBoundingClientRect().height > 0);
        if (!lab) return '';
        let v = lab.nextElementSibling;
        while (v && !v.textContent.trim()) v = v.nextElementSibling;
        return v ? v.textContent.trim() : '';""") or ""
    body = driver.find_element(By.TAG_NAME, "body").text
    m = re.search(r"논문정보\s*\(([^)]*)\)", body)
    status = m.group(1).split(":")[-1].strip() if m else ""
    return title, status


def _popup_next(driver, title: str) -> bool:
    """팝업 오른쪽 위 › (다음 논문) 클릭. 논문이 바뀌면 True."""
    btn = driver.execute_script("""
        const W = window.innerWidth;
        const els = [...document.querySelectorAll('a, button, span, i, img, input[type=button], input[type=image], [onclick], [role=button]')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.top < 200 && r.left > W / 2; });
        const kw = e => /next|다음|arrow_r|arr_r|btn_r|chevron-right|›|〉|>/i.test([typeof e.className === 'string' ? e.className : '',
            e.title, e.getAttribute('alt'), e.getAttribute('onclick'), e.id, e.getAttribute('src'),
            (e.children.length ? '' : e.textContent.trim())].join(' '));
        const clickable = e => e.closest('a, button, [onclick], [role=button], input') || e;
        const right = list => list.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0];
        const hits = els.filter(kw).map(clickable);
        if (hits.length) return right(hits);
        // 표시가 없으면 위쪽 오른쪽 끝의 작은 버튼 (‹ › 두 개 중 오른쪽)
        const small = els.map(clickable).filter(e => { const r = e.getBoundingClientRect(); return r.width < 60 && r.height < 60; });
        return small.length ? right(small) : null;""")
    if btn is None:
        log.info("[3-2] 다음(›) 버튼을 찾지 못해 끝냅니다.")
        return False
    safe_click(driver, btn)
    accept_alerts(driver, timeout=1)  # '마지막 논문입니다' 등
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: _popup_info(d)[0] not in ("", title))
        return True
    except TimeoutException:
        return False


def _map_in_popup(driver, dry_run: bool, st: dict):
    """저자매핑 팝업에서 › 로 논문을 넘기며 처리. 상태가 '미작업'인 논문만 연결·저장한다."""
    prev_sig = None
    for _ in range(MAP_MAX_PAPERS):
        authors = _wait_authors(driver, prev_sig)
        prev_sig = tuple((r["num"], r["cells"].get("저자명", "")) for r in authors)
        title, status = _popup_info(driver)
        key = title or str(prev_sig)
        if key in st["seen"]:
            break  # 한 바퀴 돌아 처음 논문으로 돌아옴
        st["seen"].add(key)
        log.info("[3-2] 논문 %d: %s%s", len(st["seen"]), title[:80], f" ({status})" if status else "")
        if status and TEXT_MAP_TODO not in status:
            log.info("[3-2]   → %s 상태라 건너뜀", status)
        else:
            n, left, krri = map_one_paper(driver, authors, dry_run)
            _finish_paper(driver, n, left, krri, dry_run, st)
        if not _popup_next(driver, title):
            break


def map_authors(driver, dry_run: bool = False, submenu: str | None = None) -> tuple[int, int]:
    """데이터반입 → WOS 저자매핑 → 미작업 검색 → 목록 전체를 차례로 처리. (작업완료 논문 수, 연결 저자 수)"""
    driver.switch_to.default_content()
    open_data_menu(driver, submenu or TEXT_RIMS_MAP_SUBMENU)
    _select_todo_status(driver)
    _click_search(driver)
    log.info("[3-1] 미작업 목록 검색%s", " [확인만 모드: 저장하지 않음]" if dry_run else "")
    wait_no_cover(driver)
    dump_page(driver, "저자매핑_목록")

    st = {"seen": set(), "done": 0, "authors": 0, "saved": 0}
    try:
        rows = WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: _paper_rows(d) or False)
    except TimeoutException:
        rows = []
    if not rows:
        log.info("[3-1] 미작업 논문이 없습니다.")
    else:
        main = driver.current_window_handle
        before = set(driver.window_handles)
        safe_click(driver, rows[0]["el"])   # 1번 논문 클릭
        try:  # 실제 RIMS는 저자매핑 화면이 팝업 창(mappingPopup.do)으로 열림
            WebDriverWait(driver, 8).until(lambda d: set(d.window_handles) - before)
            popup = (set(driver.window_handles) - before).pop()
        except TimeoutException:
            popup = None
        if popup:
            driver.switch_to.window(popup)
            try:
                WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: parse_grid(d, MAP_AUTHOR_COLS))
            except TimeoutException:
                pass
            dump_page(driver, "저자매핑_팝업")
            log.info("[3-1] 저자매핑 팝업 창에서 작업 (› 버튼으로 다음 논문)")
            try:
                _map_in_popup(driver, dry_run, st)
            finally:
                try:
                    driver.close()
                except Exception:
                    pass
                driver.switch_to.window(main)
        else:
            _map_inline(driver, dry_run, st, first_row=rows[0])
    seen, done_papers, saved_only, done_authors = st["seen"], st["done"], st["saved"], st["authors"]
    log.info("[3-3] 저자매핑 끝: 논문 %d건 확인, %d건 작업완료, %d건 저장만(확인 필요), 저자 %d명 연결",
             len(seen), done_papers, saved_only, done_authors)
    return done_papers, done_authors


# ============================================================
# 4단계: 성과추출
# ============================================================
def _open_extract(driver, source: str = TEXT_EXTRACT_SOURCE):
    """데이터반입 → 성과추출 (하위 메뉴에 없으면 화면의 '성과추출' 버튼/링크)."""
    try:
        open_data_menu(driver, TEXT_RIMS_EXTRACT)
    except TimeoutException:
        driver.switch_to.default_content()
        switch_to_frame_containing(driver, _text_xpath(TEXT_RIMS_EXTRACT), timeout=WAIT_TIMEOUT)
        click_text(driver, TEXT_RIMS_EXTRACT, tags=("a", "button", "span", "li", "div"))
    accept_alerts(driver, timeout=1)
    src_xp = f"//*[normalize-space(text())='{source}']"
    if not switch_to_frame_containing(driver, src_xp, timeout=LONG_TIMEOUT):
        raise TimeoutException(f"성과추출 화면에서 '{source}' 항목을 찾지 못했습니다.")
    log.info("[4-1] 성과추출 화면 진입")


def _ensure_wos_checked(driver):
    """'WOS' 체크박스가 꺼져 있으면 켬 (기본은 켜져 있음)."""
    _set_source(driver, TEXT_EXTRACT_SOURCE, True)


def _set_source(driver, key: str, want: bool):
    """성과추출의 데이터 구분(WOS/KCI 등) 체크를 켜거나 끔. 라디오 방식이면 켜기만 함."""
    state = driver.execute_script("""
        const key = arguments[0], want = arguments[1];
        const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
        const labels = [...document.querySelectorAll('body *')].filter(e => own(e) === key && e.getBoundingClientRect().height > 0);
        for (const l of labels) {
            const sel = 'input[type=checkbox], input[type=radio]';
            let cb = l.querySelector(sel)
                  || (l.htmlFor && document.getElementById(l.htmlFor))
                  || (l.previousElementSibling && l.previousElementSibling.matches(sel) && l.previousElementSibling)
                  || (l.nextElementSibling && l.nextElementSibling.matches(sel) && l.nextElementSibling)
                  || l.parentElement.querySelector(sel);
            if (!cb) continue;
            if (cb.type === 'radio' && !want) return 'radio';
            if (cb.checked === want) return 'already';
            cb.click();
            return cb.checked === want ? 'changed' : 'failed';
        }
        return 'not_found';""", key, want)
    if state == "failed":
        raise RuntimeError(f"성과추출 화면에서 {key} 체크를 {'켜지' if want else '끄지'} 못했습니다.")
    msg = {"already": "켜져 있음" if want else "꺼져 있음",
           "changed": "꺼져 있어 켬" if want else "켜져 있어 끔",
           "radio": "하나만 고르는 방식이라 그대로 둠",
           "not_found": "체크박스를 찾지 못함(기본값으로 진행)"}[state]
    log.info("[4-1] %s 체크: %s", key, msg)


def _extract_count(driver) -> int | None:
    """돋보기 검색 후 화면에 나온 추출 대상 건수 (못 읽으면 None)."""
    text = driver.find_element(By.TAG_NAME, "body").text
    # '총 3건', '추출대상 : 3건' 처럼 분명한 표현만 인정 (다른 'N건' 글자로 잘못 판단하지 않도록)
    m = re.search(r"(?:총|전체|대상|추출)\s*[:：]?\s*([\d,]+)\s*건", text)
    if m:
        return int(m.group(1).replace(",", ""))
    ids = set(re.findall(r"\bART\d{6,}\b", text))  # KCI 논문ID 개수 (KCI 반입 결과 목록)
    return len(ids) if ids else None


def extract_performance(driver, dry_run: bool = False, source: str = TEXT_EXTRACT_SOURCE) -> int | None:
    """성과추출 → 데이터 구분 체크(source만) → 돋보기 → 전체추출. 추출 대상 건수 반환."""
    driver.switch_to.default_content()
    _open_extract(driver, source)
    if source == TEXT_EXTRACT_SOURCE:
        _ensure_wos_checked(driver)
    else:
        _set_source(driver, source, True)               # 예: KCI 켜기
        _set_source(driver, TEXT_EXTRACT_SOURCE, False)  # WOS 끄기
    before = driver.find_element(By.TAG_NAME, "body").text
    search_alerts = _click_search(driver, anchor=source, where="성과추출") or []
    try:  # 검색 결과로 화면이 바뀔 때까지 잠깐 대기
        WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: d.find_element(By.TAG_NAME, "body").text != before)
    except TimeoutException:
        pass
    wait_no_cover(driver)
    dump_page(driver, "성과추출_검색결과")
    n = _extract_count(driver)
    if n is None:  # 'KCI 실적정보 추출 대상 건수는 [0]건 입니다.' 처럼 확인창으로 나오는 경우
        m = next((re.search(r"\[?\s*([\d,]+)\s*\]?\s*건", a) for a in search_alerts
                  if "대상" in a and re.search(r"\d+\s*\]?\s*건", a)), None)
        n = int(m.group(1).replace(",", "")) if m else None
    log.info("[4-2] 추출 대상: %s", f"{n}건" if n is not None else "건수를 읽지 못함")
    if n == 0:
        log.info("[4-3] 추출할 성과가 없어 전체추출을 누르지 않습니다.")
        return 0
    if dry_run:
        log.info("[4-3] 전체추출 예정 [확인만]")
        return n
    click_button(driver, TEXT_EXTRACT_ALL)
    alerts = accept_alerts(driver, timeout=EXTRACT_TIMEOUT)
    if _has_fail(alerts):
        raise RuntimeError(f"전체추출 중 오류 알림: {alerts}")
    log.info("[4-3] 전체추출 완료%s", f" (알림: {' / '.join(alerts)})" if alerts else "")
    return n


# ============================================================
# KCI 논문 반입 (--kci): 로그인·관리자 → KCI 데이터반입 → KCI 저자매핑 → 성과추출(KCI)
# ============================================================
def _input_after_label(driver, label: str):
    """label 글자 안쪽 또는 바로 뒤의 첫 글자 입력칸."""
    xp_label = f"//*[contains(translate(normalize-space(text()),' ',''),'{label}')]"
    cands = driver.find_elements(By.XPATH,
        f"({xp_label})[1]/descendant::input[not(@type) or @type='text' or @type='search' or @type='number']"
        f" | ({xp_label})[1]/following::input[not(@type) or @type='text' or @type='search' or @type='number'][position()<=3]")
    cands = [e for e in cands if _safe_displayed(e) and e.is_enabled()]
    if not cands:
        raise TimeoutException(f"KCI 데이터반입 화면에서 '{label}' 입력칸을 찾지 못했습니다.")
    return cands[0]


def _type_into(driver, el, value: str):
    wait_no_cover(driver)
    try:
        el.click()
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(value)
    except Exception:  # 그래도 가려져 있으면 아래에서 값을 직접 넣음
        pass
    if (el.get_attribute("value") or "").strip() != value:
        # 기본값(예: 이번 달 202611)이 남았거나 입력이 잘린 경우 값을 직접 넣음
        driver.execute_script("""arguments[0].value = arguments[1];
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));""", el, value)
    actual = (el.get_attribute("value") or "").strip()
    if actual != value:
        raise RuntimeError(f"입력칸에 '{value}'를 넣지 못했습니다 (현재 값: '{actual}')")
    return actual


def kci_import(driver, year: int, dry_run: bool = False) -> int:
    """데이터반입 → KCI 데이터반입. 소속기관 검색어마다: 입력 → 발행연월 시작 → 돋보기 → 중복선택해제 → 반입시작."""
    driver.switch_to.default_content()
    open_data_menu(driver, TEXT_RIMS_KCI_SUBMENU)
    label_xp = f"//*[contains(translate(normalize-space(text()),' ',''),'{TEXT_KCI_AFFIL_LABEL}')]"
    if not switch_to_frame_containing(driver, label_xp, timeout=LONG_TIMEOUT):
        raise TimeoutException("KCI 데이터반입 화면에서 '저자 소속기관'을 찾지 못했습니다.")
    log.info("[K-1] KCI 데이터반입 화면 진입")
    start_ym = f"{year}01"
    total = 0
    for kw in KCI_AFFIL_KEYWORDS:
        _type_into(driver, _input_after_label(driver, TEXT_KCI_AFFIL_LABEL), kw)
        date_el = _input_after_label(driver, TEXT_KCI_PUBDATE_LABEL)
        before_val = (date_el.get_attribute("value") or "").strip()
        _type_into(driver, date_el, start_ym)
        log.info("[K-2] 발행연월 시작: '%s' → '%s'", before_val, (date_el.get_attribute("value") or "").strip())
        before = driver.find_element(By.TAG_NAME, "body").text
        def _searched(d):  # 검색되면 '(N선택 / 전체 N건)' 이 바뀌거나 결과 목록(ART…)이 나옴
            t = d.find_element(By.TAG_NAME, "body").text
            return t != before and (re.search(r"전체\s*[\d,]+\s*건", t) or re.search(r"\bART\d{6,}", t))
        _click_search(driver, anchor=TEXT_KCI_AFFIL_LABEL, where="KCI 데이터반입")
        wait_no_cover(driver)
        try:
            WebDriverWait(driver, 30).until(_searched)
        except TimeoutException:
            log.warning("[K-2] 돋보기를 눌렀는데 검색 결과가 나오지 않아 Enter로 다시 검색")
            try:
                _input_after_label(driver, TEXT_KCI_AFFIL_LABEL).send_keys(Keys.ENTER)
                wait_no_cover(driver)
                WebDriverWait(driver, 30).until(_searched)
            except TimeoutException:
                dump_page(driver, "KCI반입_검색안됨")
                raise RuntimeError("KCI 데이터반입 검색이 되지 않아 반입하지 않고 멈춥니다. "
                                   "logs/screens 의 KCI반입_검색안됨 파일을 보내 주세요.") from None
        dump_page(driver, "KCI반입_검색결과")
        search_alerts = accept_alerts(driver, timeout=2)  # 검색 후 화면 확인창(예: 조회 결과 없음)
        wait_no_cover(driver)
        n = _extract_count(driver)
        if n is None and any("없" in a for a in search_alerts):
            n = 0
        log.info("[K-2] 소속기관 '%s', 발행연월 %s~ 검색: %s", kw, start_ym, f"{n}건" if n is not None else "건수를 읽지 못함")
        if n == 0:
            log.info("[K-2]   → 반입할 논문이 없어 건너뜀")
            continue
        if dry_run:
            log.info("[K-2]   → 중복선택해제 후 반입시작 예정 [확인만]")
            continue
        click_button(driver, TEXT_KCI_DEDUP)
        alerts = accept_alerts(driver, timeout=3)
        log.info("[K-2]   → 중복선택해제")
        click_button(driver, TEXT_KCI_START)
        alerts += accept_alerts(driver, timeout=UPLOAD_TIMEOUT)
        wait_no_cover(driver, timeout=UPLOAD_TIMEOUT)  # 반입 진행 중 덮개
        alerts += accept_alerts(driver, timeout=3)
        if not alerts:
            log.warning("[K-2]   → 반입시작 후 확인창이 보이지 않았습니다. RIMS 반입이력에서 반입 여부를 확인하세요.")
        if _has_fail(alerts):
            raise RuntimeError(f"KCI 반입 중 오류 알림: {alerts}")
        log.info("[K-2]   → 반입시작 완료%s", f" (알림: {' / '.join(alerts)})" if alerts else "")
        total += n or 0
    return total


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return log_file


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KCI → RIMS 성과 자동 반입")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--wos-only", action="store_true", help="WOS 다운로드까지만")
    g.add_argument("--rims-only", nargs="+", metavar="FILE", help="지정 파일을 RIMS에 업로드만")
    g.add_argument("--map-only", action="store_true", help="RIMS 저자매핑 + 성과추출만 (WOS 다운로드·업로드 생략)")
    g.add_argument("--extract-only", action="store_true", help="RIMS 성과추출만")
    g.add_argument("--kci", action="store_true", help="KCI 논문: KCI 데이터반입 → KCI 저자매핑 → 성과추출(KCI)")
    ap.add_argument("--no-map", action="store_true", help="업로드 후 저자매핑·성과추출을 하지 않음")
    ap.add_argument("--map-dry-run", action="store_true", help="저자매핑·성과추출을 확인만 하고 저장/추출하지 않음")
    ap.add_argument("--year", type=int, default=datetime.now().year, help="대상 연도 (기본: 올해)")
    ap.add_argument("--no-pause", action="store_true", help="끝난 뒤 Enter 대기 안 함 (배치 파일용)")
    args = ap.parse_args(argv)

    log_file = setup_logging()
    log = logging.getLogger("main")
    log.info("=== 시작: 대상 연도 %d, 다운로드 폴더 %s ===", args.year, DOWNLOAD_DIR)

    driver = None
    step = "준비"
    try:
        if args.kci:
            log.info("=== KCI 논문 반입%s ===", " [확인만]" if args.map_dry_run else "")
            driver = create_driver(DOWNLOAD_DIR)
            step = "RIMS"
            login(driver)
            select_admin(driver)
            step = "KCI반입"
            kci_import(driver, args.year, args.map_dry_run)
            if not args.no_map:
                step = "KCI매핑"
                map_authors(driver, args.map_dry_run, TEXT_RIMS_KCI_MAP_SUBMENU)
                step = "성과추출"
                extract_performance(driver, args.map_dry_run, source="KCI")
            log.info("=== 정상 종료 ===")
            return 0
        if args.map_only or args.extract_only:
            driver = create_driver(DOWNLOAD_DIR)
            step = "RIMS"
            login(driver)
            select_admin(driver)
            if args.map_only:
                step = "매핑"
                map_authors(driver, args.map_dry_run)
            step = "성과추출"
            extract_performance(driver, args.map_dry_run)
            log.info("=== 정상 종료 ===")
            return 0
        if args.rims_only:
            files = [Path(f) for f in args.rims_only]
            missing = [str(f) for f in files if not f.is_file()]
            if missing:
                raise FileNotFoundError(f"파일이 없습니다: {', '.join(missing)}")
        else:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            clean_old_files(DOWNLOAD_DIR)

        driver = create_driver(DOWNLOAD_DIR)

        if not args.rims_only:
            step = "WOS"
            files = download_current_year(driver, DOWNLOAD_DIR, args.year)
            if not files:
                log.info("올릴 파일이 없어 종료합니다.")
                return 0
            if args.wos_only:
                log.info("WOS 다운로드만 완료: %s", ", ".join(map(str, files)))
                return 0

        step = "RIMS"
        alerts = upload_to_rims(driver, files)
        if _has_fail(alerts):
            raise RuntimeError(f"RIMS 알림에 오류 문구가 있습니다: {alerts}")

        if not args.rims_only:
            step = "보관"
            archive(files, ARCHIVE_DIR)

        if not args.no_map:
            step = "매핑"
            map_authors(driver, args.map_dry_run)
            step = "성과추출"
            extract_performance(driver, args.map_dry_run)
        log.info("=== 정상 종료 ===")
        return 0

    except Exception:
        log.exception("[%s 단계] 실패 — 기록 파일: %s", step, log_file)
        if driver:
            save_screenshot(driver, f"error_{step}")
        return 1
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    try:
        code = main(["--kci"] + [x for x in sys.argv[1:] if x != "--kci"])  # 이 파일은 항상 KCI 과정
    except SystemExit as e:          # 잘못된 옵션 등
        code = e.code if isinstance(e.code, int) else 1
    except Exception:                # 예상 못 한 오류도 창에 보여 줌
        import traceback
        traceback.print_exc()
        code = 1
    if "--no-pause" not in sys.argv:
        print()
        print("완료되었습니다." if code == 0 else "실패했습니다. 위 메시지와 logs 폴더를 확인하세요.")
        input("Enter 키를 누르면 창이 닫힙니다...")
    sys.exit(code)
