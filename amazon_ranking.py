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
        logging.FileHandler("amazon_simple_ranker.log", encoding="utf-8")
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
        max_pages: int = 5
    ):
        self.marketplace_url = marketplace_url.rstrip('/')
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.driver: Optional[uc.Chrome] = None

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
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        self.driver = uc.Chrome(options=options, version_main=150)
        logger.info("Chrome Browser successfully initialized.")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            logger.info("Chrome Browser closed.")

    def update_zip_code(self):
        if not self.zip_code or not self.driver:
            return

        logger.info(f"Setting ZIP Code to '{self.zip_code}'...")
        try:
            self.driver.get(self.marketplace_url)
            time.sleep(3)

            location_btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
            )
            location_btn.click()
            time.sleep(1.5)

            zip_input = WebDriverWait(self.driver, 5).until(
                EC.visibility_of_element_located((By.ID, "GLUXZipUpdateInput"))
            )
            zip_input.clear()
            zip_input.send_keys(str(self.zip_code))
            zip_input.send_keys(Keys.ENTER)
            time.sleep(2)

            apply_btn = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']")
            if apply_btn:
                self.driver.execute_script("arguments[0].click();", apply_btn[0])
                time.sleep(2)

            self.driver.refresh()
            time.sleep(2)
            logger.info(f"ZIP Code applied: '{self.zip_code}'.")
        except Exception as e:
            logger.warning(f"ZIP update skipped: {str(e)}")

    def _scroll_page(self):
        for _ in range(3):
            self.driver.execute_script("window.scrollBy(0, 600);")
            time.sleep(0.5)

    @staticmethod
    def _is_non_organic(element) -> bool:
        component_type = element.get('data-component-type', '')
        if component_type in ['s-ads-creative-desktop', 'sp-sponsored-result', 's-shopping-ad-widget', 's-video-widget']:
            return True

        if element.select('.s-sponsored-label-info-icon, .puis-sponsored-label-text'):
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

        return False

    def fetch_organic_rank(self, query: TargetQuery) -> Optional[int]:
        if not self.driver:
            self._init_driver()

        cumulative_organic_count = 0
        seen_asins = set()

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
                logger.error(f"Failed to open search URL: {str(e)}")
                return None

            time.sleep(random.uniform(2.5, 4.0))
            self._scroll_page()

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            result_items = soup.select("div[data-component-type='s-search-result']")

            if not result_items:
                result_items = soup.select("div.s-result-item[data-asin]")

            logger.info(f"Page {page_num}: Found {len(result_items)} search items.")

            for item in result_items:
                asin = item.get('data-asin', '').strip()

                if not asin or len(asin) != 10 or asin in seen_asins:
                    continue

                if self._is_non_organic(item):
                    continue

                seen_asins.add(asin)
                cumulative_organic_count += 1

                title_el = (
                    item.select_one("h2 a span") or 
                    item.select_one("h2 span") or 
                    item.select_one("h2 a")
                )
                title = title_el.get_text(strip=True) if title_el else ""

                if self._is_brand_match(query.target_brand, title, item):
                    logger.info(f"MATCH FOUND! ASIN: {asin} | Rank: {cumulative_organic_count}")
                    return cumulative_organic_count

            next_btn = soup.select_one("a.s-pagination-next")
            if not next_btn or "s-pagination-disabled" in next_btn.get('class', []):
                break

        logger.info(f"Scanned {cumulative_organic_count} items. Brand '{query.target_brand}' not found.")
        return None


def get_gspread_client(json_key_path: str):
    session = Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
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
    ranker: AmazonOrganicRanker
):
    if not os.path.exists(json_key_path):
        logger.error(f"Credentials JSON file missing at '{json_key_path}'!")
        return

    client = get_gspread_client(json_key_path)

    try:
        if len(spreadsheet_id_or_name) > 30 and "/" not in spreadsheet_id_or_name:
            sheet = client.open_by_key(spreadsheet_id_or_name)
        else:
            sheet = client.open(spreadsheet_id_or_name)
        worksheet = sheet.worksheet(target_sheet_name)
    except Exception as e:
        logger.error(f"Google Sheet open error: {str(e)}")
        return

    all_rows = worksheet.get_all_values()
    if not all_rows:
        logger.error("Sheet is empty.")
        return

    headers = all_rows[0]
    kw_col_idx = next((i for i, h in enumerate(headers) if "keyword" in h.lower()), -1)
    brand_col_idx = next((i for i, h in enumerate(headers) if "brand" in h.lower()), -1)

    if kw_col_idx == -1 or brand_col_idx == -1:
        logger.error("Headers 'Keyword' or 'Brand' not found.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    date_col_idx = len(headers) + 1

    worksheet.update_cell(1, date_col_idx, now_str)
    logger.info(f"Added date column '{now_str}' at column {date_col_idx}")

    targets: List[TargetQuery] = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        kw = row[kw_col_idx].strip() if len(row) > kw_col_idx else ""
        brand = row[brand_col_idx].strip() if len(row) > brand_col_idx else ""
        if kw and brand:
            targets.append(TargetQuery(row_idx=r_idx, keyword=kw, target_brand=brand))

    total_keywords = len(targets)
    logger.info(f"Total Keywords: {total_keywords}")

    ranker._init_driver()
    ranker.update_zip_code()

    for idx, target in enumerate(targets, 1):
        rank_found = ranker.fetch_organic_rank(target)
        rank_val = rank_found if rank_found is not None else "NOT_FOUND"

        logger.info(f"[{idx}/{total_keywords}] Keyword: '{target.keyword}' | Brand: '{target.target_brand}' | Rank: {rank_val}")

        try:
            worksheet.update_cell(target.row_idx, date_col_idx, str(rank_val))
            logger.info(f"Saved -> Row {target.row_idx}, Col {date_col_idx}")
        except Exception as e:
            logger.error(f"Sheet write error: {str(e)}")

        gc.collect()
        time.sleep(random.uniform(2.0, 4.0))

    ranker.close()
    logger.info("Completed successfully!")


if __name__ == "__main__":
    CREDENTIALS_JSON = "gcp_key.json"
    SPREADSHEET_ID_OR_NAME = "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg"
    TARGET_SHEET_NAME = "rank_db"

    ranker = AmazonOrganicRanker(
        marketplace_url="https://www.amazon.com",
        zip_code="12345",
        max_pages=5
    )

    process_rank_db_sheet(
        json_key_path=CREDENTIALS_JSON,
        spreadsheet_id_or_name=SPREADSHEET_ID_OR_NAME,
        target_sheet_name=TARGET_SHEET_NAME,
        ranker=ranker
    )
