import base64
import gc
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ==========================================
# GLOBAL CONFIG & LOGGING
# ==========================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("amazon_ranker.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("AmazonRanker")


# ==========================================
# GOOGLE AUTHENTICATION CLIENT
# ==========================================
def get_gspread_client():
    sa_key_env = os.getenv("GCP_SA_KEY")
    
    if sa_key_env:
        sa_key_str = sa_key_env.strip()
        
        # 1. Direct JSON parse
        try:
            creds_dict = json.loads(sa_key_str)
        except json.JSONDecodeError:
            # 2. Base64 decode
            try:
                decoded_bytes = base64.b64decode(sa_key_str)
                creds_dict = json.loads(decoded_bytes.decode("utf-8"))
            except Exception as e:
                raise ValueError(f"GCP_SA_KEY secret na to valid JSON hai na valid Base64 string. Error: {str(e)}")
                
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif os.path.exists("G_CREDENTIAL.json"):
        creds = Credentials.from_service_account_file("G_CREDENTIAL.json", scopes=SCOPES)
    else:
        raise FileNotFoundError("GCP_SA_KEY environment variable ya G_CREDENTIAL.json file nahi mili.")

    return gspread.authorize(creds)


# ==========================================
# DATA MODELS
# ==========================================
@dataclass
class TargetQuery:
    keyword: str
    target_brand: str


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


# ==========================================
# SCRAPER CORE CLASS
# ==========================================
class AmazonOrganicRanker:
    def __init__(
        self,
        marketplace_url: str = "https://www.amazon.com",
        zip_code: Optional[str] = "12345",
        max_pages: int = 20,
        max_retries: int = 3
    ):
        self.marketplace_url = marketplace_url.rstrip('/')
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.driver: Optional[webdriver.Chrome] = None

    def _init_driver(self):
        """Chrome Driver initialization with fallback mechanism"""
        if self.driver is not None:
            return

        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--blink-settings=imagesEnabled=false')
        options.add_argument('--lang=en-US,en;q=0.9')

        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        options.add_argument(f'user-agent={user_agent}')

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.page_load_strategy = 'eager'

        try:
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            logger.warning(f"ChromeDriverManager failed: {e}. Trying default system driver...")
            self.driver = webdriver.Chrome(options=options)

        if self.driver:
            self.driver.set_page_load_timeout(45)
            self.driver.set_script_timeout(45)
        else:
            raise RuntimeError("Failed to initialize Chrome WebDriver.")

    def close(self):
        if self.driver:
            logger.info("Closing Chrome Browser.")
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _check_for_bot_detection(self) -> bool:
        if not self.driver:
            return False
        try:
            page_source = (self.driver.page_source or "").lower()
            title = (self.driver.title or "").lower()

            captcha_indicators = [
                "robot check",
                "enter the characters you see below",
                "sorry, we just need to make sure you're not a robot"
            ]

            if any(indicator in page_source or indicator in title for indicator in captcha_indicators):
                logger.warning("[!] CAPTCHA Detected on Amazon!")
                return True
        except Exception as e:
            logger.error(f"Error checking bot detection: {e}")
        return False

    def update_and_verify_zip(self) -> bool:
        if not self.driver:
            self._init_driver()

        if not self.zip_code or not self.driver:
            return True

        for attempt in range(1, self.max_retries + 1):
            logger.info(f"Setting ZIP Code to '{self.zip_code}' (Attempt {attempt}/{self.max_retries})...")
            try:
                self.driver.get(self.marketplace_url)
                time.sleep(2)

                if self._check_for_bot_detection():
                    time.sleep(5)
                    continue

                location_btn = WebDriverWait(self.driver, 15).until(
                    EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
                )
                location_btn.click()

                zip_input = WebDriverWait(self.driver, 15).until(
                    EC.visibility_of_element_located((By.ID, "GLUXZipUpdateInput"))
                )
                zip_input.clear()
                zip_input.send_keys(self.zip_code)
                time.sleep(1)

                zip_input.send_keys(Keys.ENTER)
                time.sleep(2)

                apply_selectors = [
                    "#GLUXZipUpdate input[type='submit']",
                    "#GLUXZipUpdate-announce",
                    "input[aria-labelledby='GLUXZipUpdate-announce']"
                ]
                for selector in apply_selectors:
                    try:
                        apply_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                        self.driver.execute_script("arguments[0].click();", apply_btn)
                        break
                    except Exception:
                        pass

                time.sleep(3)

                continue_selectors = [
                    "input[aria-labelledby*='GLUXConfirmClose']",
                    "#GLUXConfirmClose",
                    "button[name='glowDoneButton']",
                    ".a-popover-footer input"
                ]
                for selector in continue_selectors:
                    try:
                        continue_btn = WebDriverWait(self.driver, 3).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        self.driver.execute_script("arguments[0].click();", continue_btn)
                        break
                    except Exception:
                        pass

                try:
                    self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                except Exception:
                    pass

                time.sleep(3)
                self.driver.refresh()
                time.sleep(3)
                logger.info(f"ZIP Code updated to '{self.zip_code}'.")
                return True

            except Exception as e:
                logger.error(f"ZIP Update Error: {str(e)}")

        return True

    @staticmethod
    def _normalize_brand_string(brand: str) -> str:
        if not brand:
            return ""
        normalized = brand.lower()
        normalized = re.sub(r'\b(store|official|inc|llc|co|direct)\b', '', normalized)
        return normalized.strip()

    def _is_brand_match(self, target_brand: str, raw_title: str, raw_brand_attr: Optional[str] = None) -> bool:
        if not target_brand or not raw_title:
            return False

        norm_target = self._normalize_brand_string(target_brand)
        
        if raw_brand_attr:
            norm_brand_attr = self._normalize_brand_string(raw_brand_attr)
            if norm_target in norm_brand_attr or norm_brand_attr in norm_target:
                return True

        clean_title = raw_title.lower()
        target_words = norm_target.split()
        
        if all(re.search(rf"\b{re.escape(word)}\b", clean_title) for word in target_words):
            return True

        return False

    def _verify_pdp_brand(self, pdp_url: str, target_brand: str) -> bool:
        if not target_brand or not self.driver:
            return False

        current_window = self.driver.current_window_handle
        try:
            self.driver.execute_script("window.open(arguments[0], '_blank');", pdp_url)
            time.sleep(1.5)
            self.driver.switch_to.window(self.driver.window_handles[-1])

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            brand_sources = []

            byline = soup.select_one("#bylineInfo")
            if byline:
                brand_sources.append(byline.get_text(strip=True))

            overview_rows = soup.select("#productOverview_feature_div tr")
            for row in overview_rows:
                if "brand" in row.get_text().lower():
                    brand_sources.append(row.get_text(strip=True))

            meta_brand = soup.select_one("meta[name='title']")
            if meta_brand and meta_brand.get("content"):
                brand_sources.append(meta_brand.get("content", ""))

            combined_pdp_text = " ".join(brand_sources)
            norm_target = self._normalize_brand_string(target_brand)
            norm_pdp_text = self._normalize_brand_string(combined_pdp_text)

            return norm_target in norm_pdp_text

        except Exception as e:
            logger.error(f"Error checking PDP brand match: {str(e)}")
            return False
        finally:
            try:
                if self.driver and len(self.driver.window_handles) > 1:
                    self.driver.close()
                    self.driver.switch_to.window(current_window)
            except Exception:
                pass

    @staticmethod
    def _is_non_organic_placement(element) -> bool:
        if not element:
            return True

        component_type = element.get('data-component-type', '') or ''
        if component_type in ['s-ads-creative-desktop', 'sp-sponsored-result', 's-shopping-ad-widget', 's-video-widget']:
            return True

        classes = element.get('class', []) or []
        class_str = ' '.join(classes).lower()
        if any(ad_cls in class_str for ad_cls in ['adholder', 's-sponsored-header', 'puis-sponsored-label-text']):
            return True

        cel_widget = (element.get('data-cel-widget', '') or '').lower()
        if any(ad_kw in cel_widget for ad_kw in ['s-blended-spons', 's-sponsored', 'search-results_ad']):
            return True

        if element.select('.s-sponsored-label-info-icon, .puis-sponsored-label-text, .s-label-popover-default'):
            return True

        return False

    def search_and_rank(self, query: TargetQuery) -> RankResult:
        if not self.driver:
            self._init_driver()

        cumulative_organic_count = 0
        seen_asins = set()

        for page_num in range(1, self.max_pages + 1):
            logger.info(f"Processing Page {page_num}/{self.max_pages} for keyword: '{query.keyword}'")
            encoded_keyword = urllib.parse.quote_plus(query.keyword)
            search_url = f"{self.marketplace_url}/s?k={encoded_keyword}" if page_num == 1 else f"{self.marketplace_url}/s?k={encoded_keyword}&page={page_num}"

            try:
                if self.driver:
                    self.driver.get(search_url)
            except TimeoutException:
                logger.warning(f"Timeout loading search page {page_num}, attempting DOM read anyway...")
                try:
                    if self.driver:
                        self.driver.execute_script("window.stop();")
                except Exception:
                    pass

            if self._check_for_bot_detection():
                logger.error("Terminating workflow due to CAPTCHA.")
                return self._build_empty_result(query)

            try:
                if self.driver:
                    self.driver.execute_script("window.scrollBy(0, 800);")
            except Exception:
                pass

            try:
                if self.driver:
                    WebDriverWait(self.driver, 12).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-component-type='s-search-result']")),
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div.s-result-item[data-asin]"))
                        )
                    )
            except TimeoutException:
                logger.warning(f"Timeout waiting for elements on page {page_num}.")

            soup = BeautifulSoup(self.driver.page_source if self.driver else "", 'html.parser')
            result_items = soup.select("div[data-component-type='s-search-result']") or soup.select("div.s-result-item[data-asin]")

            page_organic_position = 0

            for item in result_items:
                if not item:
                    continue

                extracted_asin = (item.get('data-asin', '') or '').strip()

                if not extracted_asin or extracted_asin in seen_asins or self._is_non_organic_placement(item):
                    continue

                seen_asins.add(extracted_asin)
                page_organic_position += 1
                cumulative_organic_count += 1

                title_el = item.select_one("h2 a span") or item.select_one("h2 a") or item.select_one(".a-size-base-plus.a-color-base")
                title = title_el.get_text(strip=True) if title_el else "N/A"

                raw_brand_attr = item.get('data-brand', '') or ''
                is_match = self._is_brand_match(query.target_brand, title, raw_brand_attr)

                if not is_match and query.target_brand:
                    title_link = item.select_one("h2 a")
                    if title_link and title_link.get("href"):
                        pdp_href = title_link.get("href", "")
                        full_pdp_url = f"{self.marketplace_url}{pdp_href}" if pdp_href.startswith("/") else pdp_href
                        logger.info(f"Checking Organic Rank #{cumulative_organic_count} [ASIN: {extracted_asin}] via Deep PDP...")
                        is_match = self._verify_pdp_brand(full_pdp_url, query.target_brand)

                if is_match:
                    logger.info(f"MATCH CONFIRMED! Brand '{query.target_brand}' found at Rank #{cumulative_organic_count} [ASIN: {extracted_asin}]")

                    return RankResult(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        keyword=query.keyword,
                        zip_code=self.zip_code or "N/A",
                        brand_name=query.target_brand,
                        asin=extracted_asin,
                        product_title=title,
                        page_number=page_num,
                        position_on_page=page_organic_position,
                        global_organic_rank=cumulative_organic_count
                    )

            next_btn = soup.select_one("a.s-pagination-next")
            if not next_btn or "s-pagination-disabled" in (next_btn.get('class', []) or []):
                break

            time.sleep(2)

        logger.info(f"No product found for brand '{query.target_brand}' across {self.max_pages} pages.")
        return self._build_empty_result(query)

    def _build_empty_result(self, query: TargetQuery) -> RankResult:
        return RankResult(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            keyword=query.keyword,
            zip_code=self.zip_code or "N/A",
            brand_name=query.target_brand,
            asin="NOT_FOUND",
            product_title="N/A",
            page_number=-1,
            position_on_page=-1,
            global_organic_rank=-1
        )


# ==========================================
# REAL-TIME LIVE GOOGLE SHEETS INTEGRATION
# ==========================================
def fetch_keywords_and_sync_results(
    spreadsheet_name: str,
    input_sheet_name: str,
    output_sheet_name: str,
    ranker: AmazonOrganicRanker,
    batch_size: int = 100,
    batch_delay_seconds: float = 60.0
):
    client = get_gspread_client()
    sheet = client.open(spreadsheet_name)

    try:
        input_worksheet = sheet.worksheet(input_sheet_name)
    except gspread.WorksheetNotFound:
        logger.error(f"Sheet '{input_sheet_name}' not found!")
        return

    records = input_worksheet.get_all_records()

    targets = []
    for row in records:
        kw = str(row.get("Keyword", "") or row.get("keyword", "")).strip()
        brand = str(row.get("Brand", "") or row.get("brand", "") or row.get("Brand Name", "")).strip()
        if kw and brand:
            targets.append(TargetQuery(keyword=kw, target_brand=brand))

    if not targets:
        logger.warning("No keywords found in input sheet.")
        return

    logger.info(f"Total {len(targets)} keywords loaded successfully.")

    try:
        output_worksheet = sheet.worksheet(output_sheet_name)
    except gspread.WorksheetNotFound:
        output_worksheet = sheet.add_worksheet(title=output_sheet_name, rows="100", cols="20")

    output_worksheet.clear()
    headers = ["Timestamp", "Keyword", "ASIN", "Page Number", "Global Organic Rank"]
    output_worksheet.append_row(headers)

    ranker._init_driver()
    ranker.update_and_verify_zip()

    total_keywords = len(targets)

    for idx, target in enumerate(targets, 1):
        res = ranker.search_and_rank(target)

        output_worksheet.append_row([
            res.timestamp,
            res.keyword,
            res.asin,
            res.page_number,
            res.global_organic_rank
        ])

        print(f"[{idx}/{total_keywords}] Live Saved -> Keyword: '{target.keyword}' | ASIN: {res.asin} | Rank: {res.global_organic_rank}")

        del res
        gc.collect()

        if idx % batch_size == 0 and idx < total_keywords:
            print(f"\n--- Batch pause: Processed {idx}/{total_keywords} keywords. Pausing for {int(batch_delay_seconds)} seconds... ---\n")
            time.sleep(batch_delay_seconds)
        else:
            time.sleep(3)

    ranker.close()
    logger.info("All processing complete! All rows saved live.")


# ==========================================
# RUNNER / ENTRY POINT
# ==========================================
if __name__ == "__main__":
    SPREADSHEET_NAME = "Keywords_Research"
    INPUT_SHEET_NAME = "Keywords_input"
    OUTPUT_SHEET_NAME = "Keywords_output"

    ZIP_CODE = "12345"
    MAX_PAGE_LIMIT = 10

    ranker = AmazonOrganicRanker(
        marketplace_url="https://www.amazon.com",
        zip_code=ZIP_CODE,
        max_pages=MAX_PAGE_LIMIT
    )

    fetch_keywords_and_sync_results(
        spreadsheet_name=SPREADSHEET_NAME,
        input_sheet_name=INPUT_SHEET_NAME,
        output_sheet_name=OUTPUT_SHEET_NAME,
        ranker=ranker,
        batch_size=100,
        batch_delay_seconds=60.0
    )
