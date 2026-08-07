import gc
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import gspread
from bs4 import BeautifulSoup, Tag
from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
ZIP_CODE: str = "12345"
MARKETPLACE_URL: str = "https://www.amazon.com"
MAX_PAGES: int = 5
MAX_KEYWORD_RETRIES: int = 3
SESSION_MAX_KEYWORDS: int = 10
MIN_KEYWORD_DELAY: float = 2.0
MAX_KEYWORD_DELAY: float = 5.0
DEBUG_MODE: bool = True

# Optional: Add proxies in format "http://user:pass@ip:port" or "http://ip:port".
# If empty, the system runs with a standard single/multi browser session setup.
PROXY_LIST: List[str] = []

# Status Definitions
STATUS_FOUND: str = "FOUND"
STATUS_NOT_FOUND: str = "NOT_FOUND"
STATUS_RETRY_REQUIRED: str = "RETRY_REQUIRED"
STATUS_BLOCKED: str = "BLOCKED"
STATUS_ERROR: str = "ERROR"

# Setup Logger
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("amazon_organic_ranker_v3.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("RankerEngine")


# ==========================================
# DATA STRUCTURES
# ==========================================
@dataclass
class TargetQuery:
    keyword: str
    target_brand: str
    manual_rank: Optional[int] = None


@dataclass
class RankResult:
    timestamp: str
    keyword: str
    target_brand: str
    asin: str
    product_title: str
    page_number: int
    position_on_page: int
    global_organic_rank: Optional[int]
    status: str
    attempt: int
    session_id: str
    error_reason: str = ""
    manual_rank: Optional[int] = None
    rank_difference: Optional[str] = None


# ==========================================
# BROWSER SESSION MANAGEMENT
# ==========================================
class BrowserSession:
    """Encapsulates a single Chrome WebDriver instance with health tracking."""
    
    def __init__(self, session_id: str, proxy: Optional[str] = None):
        self.session_id = session_id
        self.proxy = proxy
        self.driver: Optional[webdriver.Chrome] = None
        self.is_healthy: bool = True
        self.keywords_processed: int = 0
        self.zip_verified: bool = False

    def init_driver(self) -> bool:
        """Initializes Chrome driver with optimized arguments and proxy config."""
        try:
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("window-size=1920,1080")
            options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            if self.proxy:
                options.add_argument(f"--proxy-server={self.proxy}")

            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(30)
            logger.info(f"[{self.session_id}] Browser instance initialized. Proxy: {self.proxy or 'None'}")
            return True
        except Exception as e:
            logger.error(f"[{self.session_id}] Failed to launch Chrome: {str(e)}")
            self.is_healthy = False
            return False

    def verify_and_set_zip(self) -> bool:
        """Establishes and confirms target ZIP code on Amazon.com."""
        if not self.driver and not self.init_driver():
            return False

        logger.info(f"[{self.session_id}] Configuring ZIP Code '{ZIP_CODE}'...")
        try:
            self.driver.get(MARKETPLACE_URL)
            time.sleep(2)

            if detect_captcha(self.driver.page_source):
                logger.warning(f"[{self.session_id}] CAPTCHA detected during ZIP initialization.")
                self.is_healthy = False
                return False

            # Click location selector
            loc_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
            )
            loc_btn.click()

            # Enter ZIP
            zip_input = WebDriverWait(self.driver, 10).until(
                EC.visibility_of_element_located((By.ID, "GLUXZipUpdateInput"))
            )
            zip_input.clear()
            zip_input.send_keys(ZIP_CODE)
            zip_input.send_keys(Keys.ENTER)
            time.sleep(1.5)

            # Click Submit/Apply if present
            for sel in ["#GLUXZipUpdate input[type='submit']", "#GLUXZipUpdate-announce"]:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                    self.driver.execute_script("arguments[0].click();", btn)
                    break
                except Exception:
                    pass

            time.sleep(2)

            # Confirm dialog
            for sel in ["input[aria-labelledby*='GLUXConfirmClose']", "#GLUXConfirmClose", "button[name='glowDoneButton']"]:
                try:
                    c_btn = WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    self.driver.execute_script("arguments[0].click();", c_btn)
                    break
                except Exception:
                    pass

            time.sleep(2)
            self.driver.refresh()
            time.sleep(2)

            # Strict Verification: Verify ZIP string exists in location container
            page_src = self.driver.page_source
            loc_widget_src = ""
            try:
                loc_widget = self.driver.find_element(By.ID, "nav-global-location-slot")
                loc_widget_src = loc_widget.text
            except Exception:
                pass

            if ZIP_CODE in loc_widget_src or ZIP_CODE in page_src:
                logger.info(f"[{self.session_id}] ZIP '{ZIP_CODE}' successfully confirmed in DOM.")
                self.zip_verified = True
                return True

            logger.warning(f"[{self.session_id}] ZIP verification check failed in DOM.")
            self.is_healthy = False
            return False

        except Exception as e:
            logger.error(f"[{self.session_id}] Exception during ZIP setup: {str(e)}")
            self.is_healthy = False
            return False

    def close(self):
        """Safely closes Chrome instance."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        logger.info(f"[{self.session_id}] Browser session closed safely.")


class SessionPool:
    """Manages creation, health checking, and recycling of browser sessions."""
    
    def __init__(self, proxies: List[str]):
        self.proxies = proxies
        self.proxy_index = 0
        self.session_counter = 0
        self.current_session: Optional[BrowserSession] = None

    def _get_next_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        proxy = self.proxies[self.proxy_index % len(self.proxies)]
        self.proxy_index += 1
        return proxy

    def get_healthy_session(self) -> BrowserSession:
        """Returns active session or creates a new one if current is unhealthy/exhausted."""
        if self.current_session:
            if (self.current_session.is_healthy and 
                self.current_session.zip_verified and 
                self.current_session.keywords_processed < SESSION_MAX_KEYWORDS):
                return self.current_session
            else:
                logger.info(f"[{self.current_session.session_id}] Recycling session "
                            f"(Healthy={self.current_session.is_healthy}, "
                            f"Keywords={self.current_session.keywords_processed}).")
                self.current_session.close()
                self.current_session = None

        self.session_counter += 1
        session_id = f"SESS-{self.session_counter:03d}"
        proxy = self._get_next_proxy()
        
        session = BrowserSession(session_id=session_id, proxy=proxy)
        if session.verify_and_set_zip():
            self.current_session = session
            return session
        else:
            session.close()
            logger.warning(f"[{session_id}] Session initialization failed. Trying replacement...")
            return self.get_healthy_session()

    def close_all(self):
        """Clean shutdown of session pool."""
        if self.current_session:
            self.current_session.close()
            self.current_session = None


# ==========================================
# HELPER PARSING FUNCTIONS
# ==========================================
def detect_captcha(page_source: str) -> bool:
    """Checks page HTML for Amazon CAPTCHA / Bot detection markers."""
    page_lower = page_source.lower()
    triggers = [
        "robot check",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "api-services-support@amazon.com"
    ]
    return any(trig in page_lower for trig in triggers)


def is_valid_search_page(soup: BeautifulSoup) -> bool:
    """Ensures page is a valid Amazon search result page."""
    if detect_captcha(str(soup)):
        return False
    
    # Check for search results container or result cards
    has_results = soup.select("div[data-component-type='s-search-result']")
    has_no_results_banner = soup.select_one("div.s-no-outline") or "did not match any products" in soup.get_text().lower()
    
    return bool(has_results or has_no_results_banner)


def extract_asin(card: Tag) -> Optional[str]:
    """Extracts valid 10-character Amazon ASIN from product card."""
    asin = card.get('data-asin', '').strip()
    if asin and re.match(r'^B[0-9A-Z]{9}$', asin):
        return asin

    links = card.select("a[href*='/dp/']")
    for link in links:
        href = link.get('href', '')
        match = re.search(r'/dp/([B0-9A-Z]{10})', href)
        if match:
            return match.group(1)
    return None


def extract_product_title(card: Tag) -> str:
    """Extracts title string from product card."""
    for sel in ["h2 a span", "h2 a", ".a-size-base-plus.a-color-base", ".a-size-medium.a-color-base.a-text-normal"]:
        el = card.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return "N/A"


def extract_product_brand(card: Tag) -> str:
    """Extracts product brand name using table overview, attributes, or bylines."""
    # 1. Check overview table row (tr.po-brand)
    po_brand_row = card.select_one("tr.po-brand")
    if po_brand_row:
        val_span = po_brand_row.select_one("td.a-span9 span, span.po-break-word")
        if val_span and val_span.get_text(strip=True):
            return val_span.get_text(strip=True)

    # 2. Card data attribute
    data_brand = card.get('data-brand', '').strip()
    if data_brand:
        return data_brand

    # 3. Byline store text
    byline = card.select_one("#bylineInfo, .a-row.a-size-base.a-color-secondary .a-size-base")
    if byline and byline.get_text(strip=True):
        raw = byline.get_text(strip=True)
        return re.sub(r'^(Visit the|Brand:)\s*', '', raw, flags=re.IGNORECASE).strip()

    return ""


def is_sponsored_product(card: Tag) -> bool:
    """Accurately detects if result card is a Sponsored placement."""
    component_type = card.get('data-component-type', '')
    if component_type in ['s-ads-creative-desktop', 'sp-sponsored-result', 's-shopping-ad-widget']:
        return True

    classes = " ".join(card.get('class', [])).lower()
    if any(c in classes for c in ['adholder', 's-sponsored-header', 'puis-sponsored-label-text']):
        return True

    if card.select(".s-sponsored-label-info-icon, .puis-sponsored-label-text, .s-label-popover-default"):
        return True

    first_text = card.get_text().lower()[:150]
    return "sponsored" in first_text


def is_non_product_element(card: Tag) -> bool:
    """Filters out non-organic elements (Carousels, Video, Editorial, Widgets)."""
    cel_widget = card.get('data-cel-widget', '').lower()
    non_product_widgets = [
        's-blended-spons', 's-sponsored', 'search-results_ad', 
        'carousel', 'editorial-recommendations', 'highly-rated', 
        'top-brands', 'video'
    ]
    if any(w in cel_widget for w in non_product_widgets):
        return True

    asin = extract_asin(card)
    return asin is None


def normalize_brand(b_str: str) -> str:
    """Normalizes brand string for comparison."""
    s = re.sub(r'[^a-z0-9]', '', s) # Saare spaces aur special chars hatana
    return s.strip()
    if not b_str:
        return ""
    s = b_str.lower()
    s = re.sub(r'\b(store|official|brand|inc|llc|co|corp)\b', '', s)
    s = re.sub(r'[^a-z0-9]', '', s)
    return s.strip()


def is_brand_match(target_brand: str, extracted_brand: str, title: str) -> bool:
    """Verifies brand match via extracted brand name or product title."""
    norm_target = normalize_brand(target_brand)
    if not norm_target:
        return False

    if extracted_brand:
        norm_extracted = normalize_brand(extracted_brand)
        if norm_target in norm_extracted or norm_extracted in norm_target:
            return True

    norm_title = normalize_brand(title)
    return norm_target in norm_title


def get_next_page_url(soup: BeautifulSoup) -> Optional[str]:
    """Parses actual Amazon pagination 'Next' link."""
    next_btn = soup.select_one("a.s-pagination-next")
    if next_btn and next_btn.get('href') and "s-pagination-disabled" not in next_btn.get('class', []):
        href = next_btn['href']
        return f"{MARKETPLACE_URL}{href}" if href.startswith('/') else href
    return None


# ==========================================
# CORE KEYWORD PROCESSOR WITH RETRY LOGIC
# ==========================================
def process_keyword_attempt(
    target: TargetQuery, 
    session: BrowserSession, 
    attempt_num: int
) -> Tuple[str, List[RankResult], str]:
    """
    Executes a single keyword search pass.
    Returns: (STATUS, List[RankResult], ErrorReason)
    """
    logger.info(f"[{session.session_id}] [ATTEMPT {attempt_num}/{MAX_KEYWORD_RETRIES}] "
                f"Processing Keyword: '{target.keyword}' | Brand: '{target.target_brand}'")

    global_organic_counter = 0
    seen_asins: Set[str] = set()
    matched_results: List[RankResult] = []
    page_num = 1
    
    current_url = f"{MARKETPLACE_URL}/s?k={urllib.parse.quote_plus(target.keyword)}"

    while current_url and page_num <= MAX_PAGES:
        logger.info(f"[{session.session_id}] Fetching Page {page_num}: {current_url}")
        
        try:
            session.driver.get(current_url)
        except WebDriverException as e:
            logger.error(f"[{session.session_id}] Page load exception on page {page_num}: {str(e)}")
            session.is_healthy = False
            return STATUS_RETRY_REQUIRED, [], f"Page load failure: {str(e)}"

        # Smooth scroll to trigger lazy rendering
        for step in range(1, 5):
            session.driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {step / 4});")
            time.sleep(0.3)

        page_src = session.driver.page_source
        soup = BeautifulSoup(page_src, 'html.parser')

        # Check for CAPTCHA
        if detect_captcha(page_src):
            logger.warning(f"[{session.session_id}] CAPTCHA encountered on Page {page_num}.")
            session.is_healthy = False
            return STATUS_BLOCKED, [], "Amazon CAPTCHA Intercepted"

        # Check for search page validity
        if not is_valid_search_page(soup):
            logger.warning(f"[{session.session_id}] Invalid search page DOM rendered on Page {page_num}.")
            session.is_healthy = False
            return STATUS_RETRY_REQUIRED, [], "Invalid SERP DOM Structure"

        # Parse robust product cards only
        raw_cards = soup.select("div[data-component-type='s-search-result']")
        
        position_on_page = 0
        for card in raw_cards:
            position_on_page += 1
            asin = extract_asin(card)
            title = extract_product_title(card)

            # Skip non-product modules or cards without ASIN
            if is_non_product_element(card) or not asin:
                continue

            # Deduplicate ASINs
            if asin in seen_asins:
                if DEBUG_MODE:
                    logger.debug(f"P{page_num} | Pos {position_on_page:02d} | ASIN: {asin} | DUPLICATE -> SKIPPED")
                continue

            seen_asins.add(asin)

            is_sponsored = is_sponsored_product(card)
            current_organic_rank = None
            
            if not is_sponsored:
                global_organic_counter += 1
                current_organic_rank = global_organic_counter

            extracted_brand = extract_product_brand(card)
            brand_matched = is_brand_match(target.target_brand, extracted_brand, title)

            if DEBUG_MODE:
                sp_str = "YES" if is_sponsored else "NO"
                rnk_str = str(current_organic_rank) if current_organic_rank else "-"
                mtc_str = "YES" if brand_matched else "NO"
                print(f"DEBUG: [{session.session_id}] P{page_num:02d} | Pos {position_on_page:02d} | ASIN: {asin} | "
                      f"Brand: '{extracted_brand}' | Sponsored: {sp_str} | OrganicRank: {rnk_str} | Match: {mtc_str}")

            # Store match if Organic + Requested Brand Match
            if not is_sponsored and brand_matched:
                diff_str = None
                if target.manual_rank is not None and current_organic_rank is not None:
                    diff = current_organic_rank - target.manual_rank
                    diff_str = f"{diff:+d}" if diff != 0 else "0"

                res = RankResult(
                    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    keyword=target.keyword,
                    target_brand=target.target_brand,
                    asin=asin,
                    product_title=title,
                    page_number=page_num,
                    position_on_page=position_on_page,
                    global_organic_rank=current_organic_rank,
                    status=STATUS_FOUND,
                    attempt=attempt_num,
                    session_id=session.session_id,
                    manual_rank=target.manual_rank,
                    rank_difference=diff_str
                )
                matched_results.append(res)

        # Pagination handling
        next_url = get_next_page_url(soup)
        if next_url:
            current_url = next_url
            page_num += 1
            time.sleep(random.uniform(MIN_KEYWORD_DELAY, MAX_KEYWORD_DELAY))
        else:
            logger.info(f"[{session.session_id}] Reached end of pagination at Page {page_num}.")
            current_url = None

    session.keywords_processed += 1

    # Evaluation
    if matched_results:
        return STATUS_FOUND, matched_results, ""
    else:
        # Genuinely scanned valid pages without finding target brand
        return STATUS_NOT_FOUND, [
            RankResult(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                keyword=target.keyword,
                target_brand=target.target_brand,
                asin="N/A",
                product_title="N/A",
                page_number=0,
                position_on_page=0,
                global_organic_rank=None,
                status=STATUS_NOT_FOUND,
                attempt=attempt_num,
                session_id=session.session_id,
                manual_rank=target.manual_rank
            )
        ], ""


def process_keyword(target: TargetQuery, session_pool: SessionPool) -> List[RankResult]:
    """
    Coordinates keyword execution across retries and session switching.
    Guarantees temporary Amazon failures NEVER output as NOT_FOUND.
    """
    last_status = STATUS_ERROR
    last_reason = "Unknown Error"
    
    for attempt in range(1, MAX_KEYWORD_RETRIES + 1):
        session = session_pool.get_healthy_session()
        
        status, results, reason = process_keyword_attempt(target, session, attempt)
        
        if status in [STATUS_FOUND, STATUS_NOT_FOUND]:
            logger.info(f"[SUCCESS] Keyword '{target.keyword}' processed. Final Status: {status}")
            return results
        
        logger.warning(f"[RETRY NEEDED] Keyword '{target.keyword}' Attempt {attempt} failed "
                       f"with Status: {status} | Reason: '{reason}'")
        
        last_status = status
        last_reason = reason
        
        # Backoff delay before retry
        time.sleep(3.0)

    # If all retries exhausted, return non-NOT_FOUND error status
    logger.error(f"[EXHAUSTED] All retries failed for keyword '{target.keyword}'. Status: {last_status}")
    return [
        RankResult(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            keyword=target.keyword,
            target_brand=target.target_brand,
            asin="N/A",
            product_title="N/A",
            page_number=0,
            position_on_page=0,
            global_organic_rank=None,
            status=last_status,
            attempt=MAX_KEYWORD_RETRIES,
            session_id="N/A",
            error_reason=last_reason,
            manual_rank=target.manual_rank
        )
    ]


# ==========================================
# GOOGLE SHEETS PIPELINE
# ==========================================
def run_pipeline(
    json_path: str = "gen-lang-client-0598815756-6dffccb5fb8e.json",
    spreadsheet_name: str = "Keywords_Research",
    input_sheet_name: str = "Keywords_input",
    output_sheet_name: str = "Keywords_output"
):
    """Full end-to-end execution pipeline with Google Sheets integration."""
    if not os.path.exists(json_path):
        logger.error(f"Credentials file missing: {json_path}")
        return

    logger.info("Connecting to Google Sheets API...")
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(json_path, scopes=scope)
    client = gspread.authorize(creds)

    sheet = client.open(spreadsheet_name)
    in_ws = sheet.worksheet(input_sheet_name)
    records = in_ws.get_all_records()

    targets: List[TargetQuery] = []
    for r in records:
        kw = str(r.get("Keyword", "") or r.get("keyword", "")).strip()
        br = str(r.get("Brand", "") or r.get("brand", "")).strip()
        man = r.get("Manual Rank", None) or r.get("manual_rank", None)
        man_val = int(man) if man and str(man).isdigit() else None
        if kw and br:
            targets.append(TargetQuery(keyword=kw, target_brand=br, manual_rank=man_val))

    if not targets:
        logger.warning("No targets loaded from input sheet.")
        return

    logger.info(f"Loaded {len(targets)} keyword targets for execution.")

    # Prepare Output Sheet
    try:
        out_ws = sheet.worksheet(output_sheet_name)
    except gspread.WorksheetNotFound:
        out_ws = sheet.add_worksheet(title=output_sheet_name, rows="500", cols="14")

    out_ws.clear()
    headers = [
        "Timestamp", "Keyword", "ASIN","Page Number", "Position on Page"]
    
    out_ws.append_row(headers)

    session_pool = SessionPool(proxies=PROXY_LIST)

    try:
        for idx, target in enumerate(targets, 1):
            logger.info(f"========== [KEYWORD {idx}/{len(targets)}] '{target.keyword}' ==========")
            results = process_keyword(target, session_pool)
            
            for res in results:
                out_ws.append_row([
                    res.timestamp,
                    res.keyword,
                    res.target_brand,
                    res.asin,
                    res.product_title,
                    res.page_number if res.page_number > 0 else "",
                    res.position_on_page if res.position_on_page > 0 else "",
                    res.global_organic_rank if res.global_organic_rank else "",
                    res.status,
                    res.attempt,
                    res.session_id,
                    res.error_reason,
                    res.manual_rank if res.manual_rank else "",
                    res.rank_difference if res.rank_difference else ""
                ])
                logger.info(f"WRITTEN TO SHEET -> ASIN: {res.asin} | OrganicRank: {res.global_organic_rank} | Status: {res.status}")
            
            gc.collect()
            time.sleep(random.uniform(MIN_KEYWORD_DELAY, MAX_KEYWORD_DELAY))

    finally:
        session_pool.close_all()
        logger.info("Pipeline Execution Complete. All sessions closed safely.")


if __name__ == "__main__":
    run_pipeline()
