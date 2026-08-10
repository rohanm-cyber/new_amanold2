import gc
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Undetected Chromedriver import for Anti-Bot Bypass
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ==========================================
# LOGGING CONFIGURATION
# ==========================================
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
    keyword: str
    target_brand: str
    manual_rank: str


@dataclass
class RankResult:
    timestamp: str
    keyword: str
    zip_code: str
    brand_name: str
    asin: str
    product_title: str
    page_number: int
    position_on_page: int
    global_organic_rank: int
    manual_rank: str
    total_listings_scanned: int


class AmazonOrganicRanker:
    def __init__(
        self,
        marketplace_url: str = "https://www.amazon.com",
        zip_code: Optional[str] = "12345",
        max_pages: int = 5,
        max_retries: int = 3
    ):
        self.marketplace_url = marketplace_url.rstrip('/')
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.driver: Optional[uc.Chrome] = None

    def _init_driver(self, proxy: Optional[str] = None):
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
    
            # Apply Proxy if available
            if proxy:
                options.add_argument(f"--proxy-server={proxy}")
    
            # Remove fixed version_main=150 as it crashes if Chrome auto-updates on CI
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
            logger.warning("[!] CAPTCHA / Bot Check Detected on Amazon!")
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
                    time.sleep(random.uniform(5.0, 8.0))
                    continue

                # Safely try setting ZIP code without breaking scraper if modal fails
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

    def _is_brand_match(self, target_brand: str, raw_title: str, item_soup) -> bool:
        target_clean = re.sub(r'[^a-z0-9]', '', target_brand.lower())
        if not target_clean:
            return False

        title_clean = re.sub(r'[^a-z0-9]', '', raw_title.lower())
        if target_clean in title_clean:
            return True

        brand_selectors = [
            ".s-line-clamp-1", 
            ".a-size-base-plus", 
            ".a-row.a-size-base",
            "span.a-size-base.a-color-secondary",
            ".a-color-base"
        ]
        for sel in brand_selectors:
            for elem in item_soup.select(sel):
                elem_text = re.sub(r'[^a-z0-9]', '', elem.get_text(strip=True).lower())
                if target_clean in elem_text:
                    return True

        raw_brand_attr = re.sub(r'[^a-z0-9]', '', item_soup.get('data-brand', '').lower())
        if target_clean in raw_brand_attr:
            return True

        item_text_clean = re.sub(r'[^a-z0-9]', '', item_soup.get_text().lower())
        if target_clean in item_text_clean:
            return True

        return False

    def search_and_rank(self, query: TargetQuery) -> RankResult:
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

            if self._check_for_bot_detection():
                logger.warning("Bot block detected during keyword search. Restarting driver session...")
                self._init_driver()
                self.update_and_verify_zip()
                return self._build_empty_result(query, cumulative_organic_count)

            self._scroll_entire_page()

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            result_items = soup.select("div[data-component-type='s-search-result']")

            page_organic_position = 0

            for item in result_items:
                if self._is_non_organic_placement(item):
                    continue

                asin = item.get('data-asin', '').strip()
                if not asin:
                    for link in item.select("a[href*='/dp/']"):
                        href = link.get('href', '')
                        if '/dp/' in href:
                            try:
                                asin = href.split('/dp/')[1].split('/')[0].split('?')[0].upper()
                                if len(asin) == 10:
                                    break
                            except Exception:
                                pass

                if not asin or len(asin) != 10:
                    continue

                page_organic_position += 1
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
                    return RankResult(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        keyword=query.keyword,
                        zip_code=self.zip_code or "N/A",
                        brand_name=query.target_brand,
                        asin=asin,
                        product_title=title,
                        page_number=page_num,
                        position_on_page=page_organic_position,
                        global_organic_rank=cumulative_organic_count,
                        manual_rank=query.manual_rank,
                        total_listings_scanned=cumulative_organic_count
                    )

            next_btn = soup.select_one("a.s-pagination-next")
            if not next_btn or "s-pagination-disabled" in next_btn.get('class', []):
                break

        return self._build_empty_result(query, cumulative_organic_count)

    def _build_empty_result(self, query: TargetQuery, total_scanned: int = 0) -> RankResult:
        return RankResult(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            keyword=query.keyword,
            zip_code=self.zip_code or "N/A",
            brand_name=query.target_brand,
            asin="NOT_FOUND",
            product_title="N/A",
            page_number=-1,
            position_on_page=-1,
            global_organic_rank=-1,
            manual_rank=query.manual_rank,
            total_listings_scanned=total_scanned
        )


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


def fetch_keywords_and_sync_results(
    json_key_path: str,
    spreadsheet_name: str,
    input_sheet_name: str,
    output_sheet_name: str,
    ranker: AmazonOrganicRanker,
    batch_size: int = 20,
    batch_delay_seconds: float = 15.0,
    driver_restart_interval: int = 30
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
    sheet = client.open(spreadsheet_name)

    try:
        input_worksheet = sheet.worksheet(input_sheet_name)
    except gspread.WorksheetNotFound:
        logger.error(f"Sheet '{input_sheet_name}' not found!")
        return

    rows = input_worksheet.get_all_values()
    if not rows or len(rows) < 2:
        logger.error("No data found in input sheet.")
        return

    headers_in = [str(h).strip().lower() for h in rows[0]]
    kw_idx = next((i for i, h in enumerate(headers_in) if "keyword" in h), -1)
    brand_idx = next((i for i, h in enumerate(headers_in) if "brand" in h), -1)
    manual_idx = next((i for i, h in enumerate(headers_in) if "manual" in h), -1)

    targets = []
    for row in rows[1:]:
        kw = row[kw_idx].strip() if kw_idx != -1 and len(row) > kw_idx else ""
        brand = row[brand_idx].strip() if brand_idx != -1 and len(row) > brand_idx else ""
        m_rank = row[manual_idx].strip() if manual_idx != -1 and len(row) > manual_idx else "N/A"

        if kw and brand:
            targets.append(TargetQuery(keyword=kw, target_brand=brand, manual_rank=m_rank))

    if not targets:
        logger.warning("[!] No valid keywords found in input sheet.")
        return

    total_keywords = len(targets)
    logger.info(f"Loaded {total_keywords} valid targets for processing.")

    try:
        output_worksheet = sheet.worksheet(output_sheet_name)
    except gspread.WorksheetNotFound:
        output_worksheet = sheet.add_worksheet(title=output_sheet_name, rows=str(total_keywords + 100), cols="10")

    output_worksheet.clear()
    
    headers = [
        "Timestamp", "Keyword", "Brand Name", "ASIN", 
        "Page Number", "Global Organic Rank", "Manual Rank", "Total Listings Scanned"
    ]
    output_worksheet.append_row(headers)

    ranker._init_driver()
    ranker.update_and_verify_zip()

    for idx, target in enumerate(targets, 1):
        if idx > 1 and idx % driver_restart_interval == 0:
            logger.info(f"=== Periodic Driver Restart (Keyword {idx}/{total_keywords}) ===")
            ranker._init_driver()
            ranker.update_and_verify_zip()

        res = ranker.search_and_rank(target)

        output_worksheet.append_row([
            res.timestamp,
            res.keyword,
            res.brand_name,
            res.asin,
            res.page_number,
            res.global_organic_rank,
            res.manual_rank,
            res.total_listings_scanned
        ])

        logger.info(
            f"[{idx}/{total_keywords}] Live Saved -> Keyword: '{target.keyword}' | "
            f"Brand: '{target.target_brand}' | ASIN: {res.asin} | Rank: {res.global_organic_rank} | "
            f"Scanned: {res.total_listings_scanned}"
        )

        del res
        gc.collect()

        if idx % batch_size == 0 and idx < total_keywords:
            delay = batch_delay_seconds + random.uniform(2.0, 5.0)
            logger.info(f"--- Batch Completed ({idx}/{total_keywords}). Pausing for {round(delay, 1)}s... ---")
            time.sleep(delay)
        else:
            time.sleep(random.uniform(3.0, 5.0))

    ranker.close()
    logger.info("Processing complete!")


if __name__ == "__main__":
    CREDENTIALS_JSON = "gcp_key.json"
    SPREADSHEET_NAME = "Keywords_Research"
    INPUT_SHEET_NAME = "Keywords_input"
    OUTPUT_SHEET_NAME = "Keywords_output"

    ZIP_CODE = "12345"
    MAX_PAGE_LIMIT = 5

    ranker = AmazonOrganicRanker(
        marketplace_url="https://www.amazon.com",
        zip_code=ZIP_CODE,
        max_pages=MAX_PAGE_LIMIT
    )

    fetch_keywords_and_sync_results(
        json_key_path=CREDENTIALS_JSON,
        spreadsheet_name=SPREADSHEET_NAME,
        input_sheet_name=INPUT_SHEET_NAME,
        output_sheet_name=OUTPUT_SHEET_NAME,
        ranker=ranker,
        batch_size=20,
        batch_delay_seconds=15.0,
        driver_restart_interval=30
    )
