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
from typing import List, Optional

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("amazon_production_ranker.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("AmazonRanker")


@dataclass
class TargetQuery:
    row_idx: int
    keyword: str
    target_brand: str


class AmazonOrganicRanker:
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
        self.current_proxy: Optional[str] = None
        self.driver: Optional[uc.Chrome] = None

    def _get_random_proxy(self) -> Optional[str]:
        if not self.proxy_list:
            return None
        return random.choice(self.proxy_list)

    def _init_driver(self):
        if self.driver:
            self.close()

        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--lang=en-US,en;q=0.9")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        # Rotate Proxy if available
        self.current_proxy = self._get_random_proxy()
        if self.current_proxy:
            options.add_argument(f"--proxy-server={self.current_proxy}")
            logger.info(f"Initializing browser with Proxy: {self.current_proxy}")
        else:
            logger.info("Initializing browser without proxy (Direct IP).")

        self.driver = uc.Chrome(options=options)
        logger.info("Undetected Chrome Browser successfully initialized.")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            logger.info("Chrome Browser instance closed.")

    def _check_for_bot_detection(self) -> bool:
        if not self.driver:
            return False

        page_source = self.driver.page_source.lower()
        title = self.driver.title.lower()

        captcha_indicators = [
            "robot check",
            "enter the characters you see below",
            "sorry, we just need to make sure you're not a robot"
        ]

        if any(indicator in page_source or indicator in title for indicator in captcha_indicators):
            logger.warning(f"[!] CAPTCHA / Bot Check Detected on Proxy: {self.current_proxy or 'Direct IP'}")
            return True
        return False

    def update_and_verify_zip(self) -> bool:
        if not self.zip_code:
            return True

        for attempt in range(1, self.max_retries + 1):
            logger.info(f"Setting ZIP Code to '{self.zip_code}' (Attempt {attempt}/{self.max_retries})...")
            try:
                self.driver.get(self.marketplace_url)
                time.sleep(random.uniform(3.0, 5.0))

                if self._check_for_bot_detection():
                    logger.info("Rotating proxy due to bot detection during ZIP update...")
                    self._init_driver()
                    continue

                try:
                    location_btn = WebDriverWait(self.driver, 7).until(
                        EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
                    )
                    location_btn.click()
                    time.sleep(random.uniform(1.5, 2.5))

                    zip_input = WebDriverWait(self.driver, 7).until(
                        EC.visibility_of_element_located((By.ID, "GLUXZipUpdateInput"))
                    )
                    zip_input.clear()

                    for char in str(self.zip_code):
                        zip_input.send_keys(char)
                        time.sleep(random.uniform(0.08, 0.18))

                    time.sleep(random.uniform(0.8, 1.5))
                    zip_input.send_keys(Keys.ENTER)
                    time.sleep(random.uniform(2.0, 3.0))

                    apply_btn = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']")
                    if apply_btn:
                        self.driver.execute_script("arguments[0].click();", apply_btn[0])
                        time.sleep(random.uniform(2.0, 3.5))

                    self.driver.refresh()
                    time.sleep(random.uniform(2.5, 4.0))
                    logger.info(f"ZIP Code successfully applied: '{self.zip_code}'.")
                    return True

                except Exception as zip_e:
                    logger.warning(f"ZIP modal interaction skipped: {str(zip_e)}. Continuing search directly...")
                    return True

            except Exception as e:
                logger.warning(f"ZIP Update Attempt {attempt} Failed: {str(e)}")

        return False

    def _scroll_entire_page(self):
        for _ in range(4):
            scroll_by = random.randint(500, 800)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_by});")
            time.sleep(random.uniform(0.5, 0.9))

    @staticmethod
    def _is_non_organic_placement(element) -> bool:
        component_type = element.get('data-component-type', '')
        if component_type in ['s-ads-creative-desktop', 'sp-sponsored-result', 's-shopping-ad-widget', 's-video-widget']:
            return True

        classes = element.get('class', [])
        class_str = ' '.join(classes).lower()
        if any(ad_cls in class_str for ad_cls in ['adholder', 's-sponsored-header', 'puis-sponsored-label-text']):
            return True

        if element.select('.s-sponsored-label-info-icon, .puis-sponsored-label-text, .s-label-popover-default'):
            return True

        return False

    @staticmethod
    def _is_brand_match(target_brand: str, raw_title: str, item_soup) -> bool:
        if not target_brand:
            return False

        target_raw = target_brand.strip()
        item_asin = item_soup.get('data-asin', '').strip().upper()
        if target_raw.upper() == item_asin:
            return True

        target_norm = re.sub(r'[^a-z0-9]', '', target_raw.lower())
        if not target_norm:
            return False

        title_norm = re.sub(r'[^a-z0-9]', '', raw_title.lower())
        if target_norm in title_norm:
            return True

        brand_attr = re.sub(r'[^a-z0-9]', '', item_soup.get('data-brand', '').lower())
        if brand_attr and (target_norm in brand_attr or brand_attr in target_norm):
            return True

        brand_selectors = [
            ".s-line-clamp-1",
            ".a-size-base-plus",
            "span.a-size-base.a-color-secondary",
            ".a-row.a-size-base",
            "h2 + div"
        ]
        for sel in brand_selectors:
            for elem in item_soup.select(sel):
                elem_text = re.sub(r'[^a-z0-9]', '', elem.get_text(strip=True).lower())
                if elem_text and (target_norm in elem_text or elem_text in target_norm):
                    return True

        return False

    def fetch_organic_rank(self, query: TargetQuery) -> Optional[int]:
        if not self.driver:
            self._init_driver()
            self.update_and_verify_zip()

        cumulative_organic_count = 0

        for page_num in range(1, self.max_pages + 1):
            encoded_keyword = urllib.parse.quote_plus(query.keyword)
            search_url = (
                f"{self.marketplace_url}/s?k={encoded_keyword}"
                if page_num == 1
                else f"{self.marketplace_url}/s?k={encoded_keyword}&page={page_num}"
            )

            try:
                self.driver.get(search_url)
            except Exception as e:
                logger.error(f"Navigation error: {str(e)}. Re-initializing driver...")
                self._init_driver()
                self.update_and_verify_zip()
                self.driver.get(search_url)

            time.sleep(random.uniform(3.0, 5.0))

            # Debug Check: See Page Title
            page_title = self.driver.title
            logger.info(f"Page {page_num} Title: '{page_title}'")

            if self._check_for_bot_detection():
                logger.warning("Bot block detected. Rotating proxy...")
                self._init_driver()
                self.update_and_verify_zip()
                return None

            self._scroll_entire_page()

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            result_items = soup.select("div[data-asin]")

            # Debug Check: Total listings captured on page
            valid_items = [it for it in result_items if it.get('data-asin', '').strip()]
            logger.info(f"Page {page_num}: Found {len(valid_items)} ASIN containers.")

            if len(valid_items) == 0:
                logger.warning("0 products loaded on page! Amazon is blocking or page failed to load.")

            for item in result_items:
                asin = item.get('data-asin', '').strip()
                if not asin or len(asin) != 10:
                    continue

                if self._is_non_organic_placement(item):
                    continue

                cumulative_organic_count += 1

                title_el = (
                    item.select_one("h2 a span") or 
                    item.select_one("h2 span") or 
                    item.select_one("a.a-link-normal span.a-text-normal") or 
                    item.select_one("h2 a") or 
                    item.select_one(".a-size-base-plus")
                )
                title = title_el.get_text(strip=True) if title_el else "N/A"

                if self._is_brand_match(query.target_brand, title, item):
                    logger.info(f"MATCH FOUND! ASIN: {asin} | Title: {title[:40]}... | Rank: {cumulative_organic_count}")
                    return cumulative_organic_count

            next_btn = soup.select_one("a.s-pagination-next")
            if not next_btn or "s-pagination-disabled" in next_btn.get('class', []):
                break

        logger.info(f"Scanned total {cumulative_organic_count} organic listings across {self.max_pages} pages. Brand '{query.target_brand}' not present.")
        return None


def get_robust_gspread_client(json_key_path: str):
    session = Session()
    retries = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(json_key_path, scopes=scope)
    client = gspread.Client(auth=creds)
    client.session = session
    return client


def process_rank_db_sheet(
    json_key_path: str,
    spreadsheet_id_or_name: str,
    target_sheet_name: str,
    ranker: AmazonOrganicRanker,
    batch_size: int = 10,
    batch_delay_seconds: float = 15.0,
    driver_restart_interval: int = 15
):
    if not os.path.exists(json_key_path):
        if os.path.exists("Credentials.json"):
            json_key_path = "Credentials.json"
        elif os.path.exists("credentials.json"):
            json_key_path = "credentials.json"
        else:
            logger.error(f"Credentials JSON file not found at '{json_key_path}'!")
            return

    client = get_robust_gspread_client(json_key_path)
    
    try:
        if len(spreadsheet_id_or_name) > 30 and "/" not in spreadsheet_id_or_name:
            sheet = client.open_by_key(spreadsheet_id_or_name)
        else:
            sheet = client.open(spreadsheet_id_or_name)
    except Exception as e:
        logger.error(f"Failed to open Google Spreadsheet: {str(e)}")
        return

    try:
        worksheet = sheet.worksheet(target_sheet_name)
    except gspread.WorksheetNotFound:
        logger.error(f"Sheet '{target_sheet_name}' not found!")
        return

    all_rows = worksheet.get_all_values()
    if not all_rows:
        logger.error("No data found in sheet.")
        return

    headers = all_rows[0]
    kw_col_idx = next((i for i, h in enumerate(headers) if "keyword" in h.lower()), -1)
    brand_col_idx = next((i for i, h in enumerate(headers) if "brand" in h.lower()), -1)

    if kw_col_idx == -1 or brand_col_idx == -1:
        logger.error("Could not locate 'Keyword' or 'Brand' header columns in the sheet.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    
    date_col_idx = len(headers)
    worksheet.update_cell(1, date_col_idx + 1, now_str)
    logger.info(f"Created new timestamp header column '{now_str}' at Column Index {date_col_idx + 1}.")

    targets: List[TargetQuery] = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        kw = row[kw_col_idx].strip() if len(row) > kw_col_idx else ""
        brand = row[brand_col_idx].strip() if len(row) > brand_col_idx else ""
        if kw and brand:
            targets.append(TargetQuery(row_idx=r_idx, keyword=kw, target_brand=brand))

    total_keywords = len(targets)
    logger.info(f"Found {total_keywords} keywords to rank.")

    ranker._init_driver()
    ranker.update_and_verify_zip()

    cell_updates = []

    for idx, target in enumerate(targets, 1):
        if idx > 1 and idx % driver_restart_interval == 0:
            logger.info(f"=== Periodic Proxy & Driver Rotation (Keyword {idx}/{total_keywords}) ===")
            ranker._init_driver()
            ranker.update_and_verify_zip()

        rank_found = ranker.fetch_organic_rank(target)
        rank_val = rank_found if rank_found is not None else "NOT_FOUND"

        logger.info(
            f"[{idx}/{total_keywords}] Keyword: '{target.keyword}' | Brand: '{target.target_brand}' | Rank: {rank_val}"
        )

        cell_updates.append(gspread.Cell(target.row_idx, date_col_idx + 1, rank_val))
        gc.collect()

        if idx % batch_size == 0 and idx < total_keywords:
            logger.info("Batch completed. Updating Google Sheets in bulk...")
            worksheet.update_cells(cell_updates)
            cell_updates.clear()
            
            delay = batch_delay_seconds + random.uniform(2.0, 5.0)
            logger.info(f"--- Pausing for {round(delay, 1)}s... ---")
            time.sleep(delay)
        else:
            time.sleep(random.uniform(3.0, 5.0))

    if cell_updates:
        worksheet.update_cells(cell_updates)

    ranker.close()
    logger.info("Rank update task successfully completed!")


if __name__ == "__main__":
    CREDENTIALS_JSON = "gcp_key.json"
    SPREADSHEET_ID_OR_NAME = "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg"
    TARGET_SHEET_NAME = "rank_db"

    ZIP_CODE = "12345"
    MAX_PAGE_LIMIT = 8

    # Proxy list load karna (Aap yahan apne proxies ki list de sakte hain ya file se read kar sakte hain)
    # Format: "http://username:password@ip:port" ya "http://ip:port"
    PROXY_POOL = []
    
    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r") as f:
            PROXY_POOL = [line.strip() for line in f if line.strip()]

    ranker = AmazonOrganicRanker(
        marketplace_url="https://www.amazon.com",
        zip_code=ZIP_CODE,
        max_pages=MAX_PAGE_LIMIT,
        proxy_list=PROXY_POOL
    )

    process_rank_db_sheet(
        json_key_path=CREDENTIALS_JSON,
        spreadsheet_id_or_name=SPREADSHEET_ID_OR_NAME,
        target_sheet_name=TARGET_SHEET_NAME,
        ranker=ranker,
        batch_size=10,
        batch_delay_seconds=10.0,
        driver_restart_interval=15
    )
