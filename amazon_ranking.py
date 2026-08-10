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
from typing import List, Optional, Set

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("amazon_stealth_ranker.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("UltimateAmazonRanker")


@dataclass
class TargetQuery:
    row_idx: int
    keyword: str
    target_brand: str


class StealthAmazonRanker:
    def __init__(
        self,
        marketplace_url: str = "https://www.amazon.com",
        zip_code: Optional[str] = "12345",
        max_pages: int = 5,
        max_retries: int = 3,
        proxy_list: Optional[List[str]] = None
    ):
        self.marketplace_url = marketplace_url.rstrip('/')
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.proxy_list = proxy_list or []
        self.driver: Optional[uc.Chrome] = None

        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ]

    def _get_random_proxy(self) -> Optional[str]:
        return random.choice(self.proxy_list) if self.proxy_list else None

    def _init_stealth_driver(self):
        """Initializes Chrome with Advanced Stealth Overrides."""
        if self.driver:
            self.close()

        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"user-agent={random.choice(self.user_agents)}")
        
        # Viewport randomization to defeat fingerprinting
        width = random.choice([1366, 1440, 1536, 1920])
        height = random.choice([768, 900, 864, 1080])
        options.add_argument(f"--window-size={width},{height}")

        proxy = self._get_random_proxy()
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")
            logger.info(f"Connecting via Proxy: {proxy}")

        self.driver = uc.Chrome(options=options,version_main=150)
        
        # Anti-Bot JS Injection
        stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = { runtime: {} };
        """
        self.driver.execute_script(stealth_js)
        logger.info("Stealth Chrome Driver successfully loaded.")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _detect_and_handle_block(self) -> bool:
        """Detects Captcha, AWS WAF, and Robot Check Pages."""
        if not self.driver:
            return True

        try:
            page_text = self.driver.page_source.lower()
            title = self.driver.title.lower()
        except Exception:
            return True

        block_signals = [
            "robot check", "enter the characters you see below",
            "type the characters you see in this image",
            "sorry, we just need to make sure you're not a robot",
            "503 service unavailable", "aws waf", "api error"
        ]

        if any(signal in page_text or signal in title for signal in block_signals):
            logger.warning("[!] Anti-Bot Triggered (CAPTCHA/WAF Page Detected)!")
            return True

        return False

    def update_zip_code(self) -> bool:
        """Sets target ZIP code with human-like interactions."""
        if not self.zip_code or not self.driver:
            return True

        try:
            self.driver.get(self.marketplace_url)
            time.sleep(random.uniform(3.0, 5.0))

            if self._detect_and_handle_block():
                return False

            try:
                loc_btn = WebDriverWait(self.driver, 6).until(
                    EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
                )
                loc_btn.click()
                time.sleep(random.uniform(1.5, 2.5))

                zip_input = WebDriverWait(self.driver, 6).until(
                    EC.visibility_of_element_located((By.ID, "GLUXZipUpdateInput"))
                )
                zip_input.clear()

                for char in str(self.zip_code):
                    zip_input.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))

                zip_input.send_keys(Keys.ENTER)
                time.sleep(random.uniform(2.0, 3.0))

                apply_btn = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']")
                if apply_btn:
                    self.driver.execute_script("arguments[0].click();", apply_btn[0])
                    time.sleep(random.uniform(2.0, 3.0))

                self.driver.refresh()
                time.sleep(random.uniform(2.5, 4.0))
                logger.info(f"ZIP Code set to '{self.zip_code}'.")
                return True
            except Exception:
                logger.warning("ZIP modal missed, continuing search...")
                return True
        except Exception as e:
            logger.error(f"ZIP code update failed: {str(e)}")
            return False

    def _human_scroll(self):
        """Simulates natural user scrolling to load lazy-loaded elements."""
        for _ in range(random.randint(3, 5)):
            scroll_step = random.randint(400, 750)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_step});")
            time.sleep(random.uniform(0.4, 0.8))

    @staticmethod
    def _is_sponsored_item(element) -> bool:
        """Filters out Sponsored Ads, Carousels, and Video Widgets."""
        comp_type = element.get('data-component-type', '')
        if comp_type in ['s-ads-creative-desktop', 'sp-sponsored-result', 's-shopping-ad-widget', 's-video-widget']:
            return True

        if element.select('.s-sponsored-label-info-icon, .puis-sponsored-label-text, .s-label-popover-default'):
            return True

        return False

    @staticmethod
    def _match_brand_or_asin(target: str, title: str, item_soup) -> bool:
        """Matches Target Brand or Exact ASIN against current listing."""
        if not target:
            return False

        target_str = target.strip()
        item_asin = item_soup.get('data-asin', '').strip().upper()

        if target_str.upper() == item_asin:
            return True

        norm_target = re.sub(r'[^a-z0-9]', '', target_str.lower())
        norm_title = re.sub(r'[^a-z0-9]', '', title.lower())

        if norm_target and norm_target in norm_title:
            return True

        brand_attr = re.sub(r'[^a-z0-9]', '', item_soup.get('data-brand', '').lower())
        if brand_attr and norm_target in brand_attr:
            return True

        return False

    def fetch_rank(self, query: TargetQuery) -> Optional[int]:
        """Fetches organic rank with auto-retry and driver renewal on blocks."""
        for attempt in range(1, self.max_retries + 1):
            if not self.driver:
                self._init_stealth_driver()
                self.update_zip_code()

            seen_asins: Set[str] = set()
            organic_counter = 0
            block_occurred = False

            for page in range(1, self.max_pages + 1):
                kw_encoded = urllib.parse.quote_plus(query.keyword)
                url = (
                    f"{self.marketplace_url}/s?k={kw_encoded}"
                    if page == 1
                    else f"{self.marketplace_url}/s?k={kw_encoded}&page={page}"
                )

                try:
                    self.driver.get(url)
                except Exception as e:
                    logger.error(f"Navigation error: {str(e)}")
                    block_occurred = True
                    break

                time.sleep(random.uniform(3.0, 5.5))

                if self._detect_and_handle_block():
                    logger.warning(f"Block detected on Attempt {attempt}. Renewing browser session...")
                    self._init_stealth_driver()
                    self.update_zip_code()
                    block_occurred = True
                    break

                self._human_scroll()

                soup = BeautifulSoup(self.driver.page_source, 'html.parser')
                items = soup.select("div[data-component-type='s-search-result']")
                if not items:
                    items = soup.select("div.s-result-item[data-asin]")

                for item in items:
                    asin = item.get('data-asin', '').strip()
                    if not asin or len(asin) != 10 or asin in seen_asins:
                        continue

                    if self._is_sponsored_item(item):
                        continue

                    seen_asins.add(asin)
                    organic_counter += 1

                    title_el = item.select_one("h2 a span") or item.select_one("h2 span")
                    title_text = title_el.get_text(strip=True) if title_el else ""

                    if self._match_brand_or_asin(query.target_brand, title_text, item):
                        logger.info(f"[SUCCESS] ASIN: {asin} | Organic Rank: {organic_counter}")
                        return organic_counter

                next_page = soup.select_one("a.s-pagination-next")
                if not next_page or "s-pagination-disabled" in next_page.get('class', []):
                    break

            if not block_occurred:
                return None  # Not found within max pages

        return None


def get_gspread_client(json_key_path: str):
    session = Session()
    retries = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(json_key_path, scopes=scope)
    client = gspread.Client(auth=creds)
    client.session = session
    return client


def process_rankings(
    json_key_path: str,
    spreadsheet_id: str,
    sheet_name: str,
    ranker: StealthAmazonRanker
):
    if not os.path.exists(json_key_path):
        logger.error(f"Missing Service Account JSON at '{json_key_path}'!")
        return

    client = get_gspread_client(json_key_path)

    try:
        sheet = client.open_by_key(spreadsheet_id) if len(spreadsheet_id) > 30 else client.open(spreadsheet_id)
        worksheet = sheet.worksheet(sheet_name)
    except Exception as e:
        logger.error(f"Google Sheet error: {str(e)}")
        return

    all_rows = worksheet.get_all_values()
    if not all_rows:
        logger.error("Empty sheet!")
        return

    headers = all_rows[0]
    kw_col = next((i for i, h in enumerate(headers) if "keyword" in h.lower()), -1)
    brand_col = next((i for i, h in enumerate(headers) if "brand" in h.lower()), -1)

    if kw_col == -1 or brand_col == -1:
        logger.error("Sheet must contain 'Keyword' and 'Brand' headers.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    target_col_idx = len(headers) + 1

    # AUTO EXPAND GRID IF LIMIT EXCEEDED
    if target_col_idx > worksheet.col_count:
        logger.info(f"Expanding Google Sheet grid (+1 Col)...")
        worksheet.add_cols(1)

    worksheet.update_cell(1, target_col_idx, now_str)
    logger.info(f"Created timestamp column '{now_str}' at Index {target_col_idx}")

    targets: List[TargetQuery] = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        kw = row[kw_col].strip() if len(row) > kw_col else ""
        brand = row[brand_col].strip() if len(row) > brand_col else ""
        if kw and brand:
            targets.append(TargetQuery(row_idx=r_idx, keyword=kw, target_brand=brand))

    total = len(targets)
    logger.info(f"Total Targets Loaded: {total}")

    ranker._init_stealth_driver()
    ranker.update_zip_code()

    for idx, t in enumerate(targets, 1):
        # Refresh browser periodically to drop tracking cookies
        if idx > 1 and idx % 10 == 0:
            logger.info("Performing periodic driver refresh...")
            ranker._init_stealth_driver()
            ranker.update_zip_code()

        rank = ranker.fetch_rank(t)
        rank_str = str(rank) if rank is not None else "NOT_FOUND"

        logger.info(f"[{idx}/{total}] Target: '{t.keyword}' | Brand: '{t.target_brand}' | Rank: {rank_str}")

        # LIVE DIRECT WRITE TO SHEETS (Zero Data Loss)
        try:
            worksheet.update_cell(t.row_idx, target_col_idx, rank_str)
            logger.info(f"--> Saved to Sheet (Row {t.row_idx}, Col {target_col_idx})")
        except Exception as e:
            logger.error(f"Sheets update failed on Row {t.row_idx}: {str(e)}")

        gc.collect()
        time.sleep(random.uniform(3.0, 6.0))

    ranker.close()
    logger.info("All keywords successfully processed!")


if __name__ == "__main__":
    CREDENTIALS_JSON = "gcp_key.json"
    SPREADSHEET_ID = "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg"
    SHEET_NAME = "rank_db"

    # Optional: Load proxies from proxies.txt if available
    proxies = []
    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r") as f:
            proxies = [line.strip() for line in f if line.strip()]

    stealth_ranker = StealthAmazonRanker(
        marketplace_url="https://www.amazon.com",
        zip_code="12345",
        max_pages=5,
        max_retries=3,
        proxy_list=proxies
    )

    process_rankings(
        json_key_path=CREDENTIALS_JSON,
        spreadsheet_id=SPREADSHEET_ID,
        sheet_name=SHEET_NAME,
        ranker=stealth_ranker
    )
