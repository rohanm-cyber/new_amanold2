import gc
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
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
    asin: Optional[str] = None


class StealthAmazonRanker:
    def __init__(
        self,
        marketplace_url: str = "https://www.amazon.com",
        zip_code: Optional[str] = "12345",  # Strictly set to 12345
        max_pages: int = 8,
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

    @staticmethod
    def _detect_installed_chrome_major_version() -> Optional[int]:
        """Detects the installed Chrome/Chromium major version so undetected_chromedriver
        fetches a chromedriver build that actually matches the local browser, instead of
        guessing the latest available build and crashing on a version mismatch."""
        candidates = []
        try:
            exe = uc.find_chrome_executable()
            if exe:
                candidates.append(exe)
        except Exception:
            pass
        candidates += ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]

        for exe in candidates:
            try:
                if os.name == "nt":
                    # --version on Windows binaries often doesn't print to stdout reliably;
                    # query via WMIC as a fallback, else try direct invocation first.
                    try:
                        out = subprocess.check_output(
                            [exe, "--version"], stderr=subprocess.STDOUT, timeout=5
                        ).decode(errors="ignore")
                    except Exception:
                        out = subprocess.check_output(
                            ["wmic", "datafile", "where",
                             f"name='{exe.replace(chr(92), chr(92)*2)}'",
                             "get", "Version", "/value"],
                            stderr=subprocess.STDOUT, timeout=5
                        ).decode(errors="ignore")
                else:
                    out = subprocess.check_output(
                        [exe, "--version"], stderr=subprocess.STDOUT, timeout=5
                    ).decode(errors="ignore")

                match = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
                if match:
                    return int(match.group(1))
            except Exception:
                continue
        return None

    def _init_stealth_driver(self):
        """Initializes Chrome with Auto Chrome Version Detection and Stealth Overrides."""
        if self.driver:
            self.close()

        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"user-agent={random.choice(self.user_agents)}")
        
        width = random.choice([1366, 1440, 1536, 1920])
        height = random.choice([768, 900, 864, 1080])
        options.add_argument(f"--window-size={width},{height}")

        proxy = self._get_random_proxy()
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")
            logger.info(f"Connecting via Proxy: {proxy}")

        chrome_major = 150  # Pinned for now, per explicit request
        logger.info(f"Using pinned Chrome major version: {chrome_major}")
        self.driver = uc.Chrome(options=options, version_main=chrome_major)
        
        stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = { runtime: {} };
        """
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_js})
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
            "503 service unavailable", "aws waf"
        ]

        if any(signal in page_text or signal in title for signal in block_signals):
            logger.warning("[!] Anti-Bot Triggered (CAPTCHA/WAF Page Detected)!")
            return True

        return False

    def update_zip_code(self) -> bool:
        """Forces Amazon location strictly to US ZIP code 12345 from Indian IP using Cookies, API, and UI Fallbacks."""
        if not self.zip_code or not self.driver:
            return True

        try:
            logger.info(f"Setting location to US ZIP Code: {self.zip_code}")
            
            # Step 1: Base Load & Cookie Preset
            self.driver.get(self.marketplace_url)
            time.sleep(random.uniform(2.5, 3.5))

            if self._detect_and_handle_block():
                return False

            # Inject USD and US Locale Cookies directly into session
            try:
                self.driver.add_cookie({"name": "i18n-prefs", "value": "USD", "domain": ".amazon.com"})
                self.driver.add_cookie({"name": "lc-main", "value": "en_US", "domain": ".amazon.com"})
            except Exception as e:
                logger.debug(f"Cookie injection warning: {str(e)}")

            # Check if header is already set to US/12345
            try:
                curr_loc = self.driver.find_element(By.ID, "glow-ingress-line2").text
                if str(self.zip_code) in curr_loc:
                    logger.info(f"[SUCCESS] Location verified in header: '{curr_loc}'")
                    return True
            except Exception:
                pass

            # Step 2: Direct Amazon Internal Glow API Address Injection
            logger.info("Injecting ZIP payload via Amazon Glow Endpoint...")
            api_js = f"""
            var callback = arguments[arguments.length - 1];
            var csrfToken = "";
            try {{
                var inputs = document.querySelectorAll("input[name='anti-csrftoken-a2z']");
                if (inputs.length > 0) csrfToken = inputs[0].value;
            }} catch(e) {{}}

            var params = new URLSearchParams();
            params.append('locationType', 'LOCATION_INPUT');
            params.append('zipCode', '{self.zip_code}');
            params.append('storeContext', 'generic');
            params.append('deviceType', 'web');
            params.append('pageType', 'Gateway');
            params.append('actionSource', 'glow');

            fetch('/portal-migration/hz/glow/address-change?actionSource=glow', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'x-requested-with': 'XMLHttpRequest',
                    'anti-csrftoken-a2z': csrfToken
                }},
                body: params.toString()
            }}).then(res => res.json())
              .then(data => callback({{success: true, data: data}}))
              .catch(err => callback({{success: false, error: err.toString()}}));
            """

            try:
                api_res = self.driver.execute_async_script(api_js)
                if api_res and api_res.get("success"):
                    self.driver.refresh()
                    time.sleep(3.0)
                    new_loc = self.driver.find_element(By.ID, "glow-ingress-line2").text
                    if str(self.zip_code) in new_loc:
                        logger.info(f"[SUCCESS] ZIP Code applied via API: '{new_loc}'")
                        return True
            except Exception as e:
                logger.warning(f"API injection failed, proceeding to UI fallback: {str(e)}")

            # Step 3: UI Automation Fallback (For handling Country Dropdown vs ZIP Input)
            logger.info("Triggering Location UI Modal...")
            loc_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
            )
            self.driver.execute_script("arguments[0].click();", loc_btn)
            time.sleep(2.5)

            # Check if "Enter a US zip code" link exists (Common on Indian IPs)
            try:
                change_zip_link = self.driver.find_elements(By.ID, "GLUXChangePostalCodeLink")
                if change_zip_link and change_zip_link[0].is_displayed():
                    self.driver.execute_script("arguments[0].click();", change_zip_link[0])
                    time.sleep(1.5)
            except Exception:
                pass

            # Search for ZIP Input field
            zip_input = None
            zip_selectors = [
                "input#GLUXZipUpdateInput",
                "input[id*='GLUXZipUpdateInput']",
                "#GLUXZipUpdateInput_0"
            ]
            for sel in zip_selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if el.is_displayed():
                        zip_input = el
                        break
                if zip_input:
                    break

            # If ZIP input is not visible, change Country Dropdown to United States first
            if not zip_input:
                logger.info("ZIP Input not visible. Changing Country Dropdown to 'United States'...")
                dropdown_btn = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXCountryList_dropdown, #GLUXCountryList span.a-button-text")
                if dropdown_btn and dropdown_btn[0].is_displayed():
                    self.driver.execute_script("arguments[0].click();", dropdown_btn[0])
                    time.sleep(1.5)

                    us_options = self.driver.find_elements(By.XPATH, "//a[contains(text(), 'United States')] | //a[contains(@data-value, 'US')]")
                    if us_options:
                        self.driver.execute_script("arguments[0].click();", us_options[0])
                        time.sleep(1.5)

                    done_btn = self.driver.find_elements(By.CSS_SELECTOR, "button[name='glowDoneButton'], #GLUXCountryUpdate input")
                    if done_btn and done_btn[0].is_displayed():
                        self.driver.execute_script("arguments[0].click();", done_btn[0])
                        time.sleep(3.0)

                    # Re-open location modal after switching country
                    loc_btn = self.driver.find_element(By.ID, "nav-global-location-slot")
                    self.driver.execute_script("arguments[0].click();", loc_btn)
                    time.sleep(2.0)

                    # Re-evaluate ZIP input
                    for sel in zip_selectors:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        for el in elements:
                            if el.is_displayed():
                                zip_input = el
                                break
                        if zip_input:
                            break

            # Enter ZIP 12345 with JavaScript Events
            if zip_input:
                self.driver.execute_script("arguments[0].value = '';", zip_input)
                for char in str(self.zip_code):
                    zip_input.send_keys(char)
                    time.sleep(0.08)

                # Dispatch events so Amazon's React app activates the Apply button
                self.driver.execute_script("""
                    var el = arguments[0];
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                """, zip_input)
                time.sleep(1.0)

                # Click Apply/Submit
                apply_btns = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit'], #GLUXZipUpdate-announce, input[aria-labelledby='GLUXZipUpdate-announce']")
                if apply_btns and apply_btns[0].is_displayed():
                    self.driver.execute_script("arguments[0].click();", apply_btns[0])
                else:
                    zip_input.send_keys(Keys.ENTER)

                time.sleep(3.0)

                # Click Continue/Done Modal confirmation button
                confirm_btns = self.driver.find_elements(By.CSS_SELECTOR, "button[name='glowDoneButton'], #GLUXConfirmClose, .a-popover-footer #GLUXConfirmClose-announce")
                if confirm_btns and confirm_btns[0].is_displayed():
                    self.driver.execute_script("arguments[0].click();", confirm_btns[0])
                    time.sleep(2.0)

                self.driver.refresh()
                time.sleep(3.5)

                final_loc = self.driver.find_element(By.ID, "glow-ingress-line2").text
                logger.info(f"[SUCCESS] Final Amazon Header Location: '{final_loc}'")
                return True

            logger.error("Failed to find or input ZIP code in UI.")
            return False

        except Exception as e:
            logger.error(f"ZIP code update error: {str(e)}")
            return False
    def _human_scroll(self):
        """Simulates natural user scrolling to load lazy-loaded elements."""
        for _ in range(random.randint(3, 5)):
            scroll_step = random.randint(400, 750)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_step});")
            time.sleep(random.uniform(0.4, 0.8))

    @staticmethod
    def _is_sponsored_item(element) -> bool:
        """Filters out Sponsored Ads, Carousels, and Video Widgets using updated selectors."""
        comp_type = element.get('data-component-type', '')
        if comp_type in [
            's-ads-creative-desktop', 'sp-sponsored-result',
            's-shopping-ad-widget', 's-video-widget', 's-brand-story-widget'
        ]:
            return True

        if element.select('.s-sponsored-label-info-icon, .puis-sponsored-label-text, .s-label-popover-default, [aria-label*="Sponsored"]'):
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

    def _verify_asin_live(self, asin: str) -> bool:
        """Fallback check used when a keyword scan finds no match: search Amazon
        directly by ASIN to confirm the listing is still live/discoverable at all,
        independent of whether it ranks for the given keyword."""
        if not asin or not self.driver:
            return False

        try:
            asin_clean = asin.strip().upper()
            url = f"{self.marketplace_url}/s?k={urllib.parse.quote_plus(asin_clean)}"
            self.driver.get(url)
            time.sleep(random.uniform(2.5, 4.0))

            if self._detect_and_handle_block():
                logger.warning(f"Block detected during ASIN verification for '{asin_clean}'.")
                return False

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            items = soup.select("div[data-component-type='s-search-result']")
            if not items:
                items = soup.select("div.s-result-item[data-asin]")

            for item in items:
                if item.get('data-asin', '').strip().upper() == asin_clean:
                    return True

            return False
        except Exception as e:
            logger.warning(f"ASIN verification error for '{asin}': {str(e)}")
            return False

    def _resolve_not_found(self, query: TargetQuery) -> tuple:
        """Called after a full keyword scan completes without a match. If the row
        has an ASIN, falls back to a direct ASIN search so the sheet can tell apart
        'not ranking for this keyword' from 'ASIN not discoverable / possibly delisted'."""
        if query.asin:
            logger.info(f"Keyword scan found nothing for '{query.keyword}'. Verifying ASIN '{query.asin}' directly...")
            if self._verify_asin_live(query.asin):
                return None, "NOT_FOUND_ASIN_LIVE"
            return None, "NOT_FOUND_ASIN_MISSING"
        return None, "NOT_FOUND"

    def fetch_rank(self, query: TargetQuery) -> tuple:
        """Fetches organic rank with auto-retry and driver renewal on blocks.
        Returns (rank, status): rank is None when not found, and status explains
        why — including an ASIN-based fallback check via _resolve_not_found."""
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

                time.sleep(random.uniform(3.0, 5.0))

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
                        logger.info(f"[SUCCESS] ASIN/Brand Match: '{query.target_brand}' | Organic Rank: {organic_counter}")
                        return organic_counter, "FOUND"

                next_page = soup.select_one("a.s-pagination-next")
                if not next_page or "s-pagination-disabled" in next_page.get('class', []):
                    break

            if not block_occurred:
                return self._resolve_not_found(query)

            cooldown = attempt * random.uniform(8.0, 12.0)
            logger.info(f"Cooling down {cooldown:.1f}s before retry attempt {attempt + 1}...")
            time.sleep(cooldown)

        return self._resolve_not_found(query)


def safe_update_cell(worksheet, row: int, col: int, value: str, max_retries: int = 4):
    """Saves cell data to Google Sheets with rate-limit protection."""
    for attempt in range(1, max_retries + 1):
        try:
            worksheet.update_cell(row, col, value)
            return True
        except Exception as e:
            if "429" in str(e) or "QUOTA_EXCEEDED" in str(e):
                wait_time = attempt * 5
                logger.warning(f"Google Sheets Rate Limit hit. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to update row {row}, col {col}: {str(e)}")
                break
    return False


def get_gspread_client(json_key_path: str):
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(json_key_path, scopes=scope)
    client = gspread.Client(auth=creds)

    # Mount retry adapter onto the client's own AuthorizedSession instead of
    # replacing it — a plain requests.Session() has no OAuth credentials
    # attached, which was causing every Sheets API call to fail auth.
    # In gspread 6.x the session lives under client.http_client.session.
    retries = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    client.http_client.session.mount("https://", HTTPAdapter(max_retries=retries))
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
        logger.error(f"Google Sheet connection error: {str(e)}")
        return

    all_rows = worksheet.get_all_values()
    if not all_rows:
        logger.error("Empty sheet!")
        return

    headers = list(all_rows[0])
    while headers and not headers[-1].strip():
        headers.pop()

    if not headers:
        logger.error("No valid headers found in row 1!")
        return

    kw_col = next((i for i, h in enumerate(headers) if "keyword" in h.lower()), -1)
    brand_col = next((i for i, h in enumerate(headers) if "brand" in h.lower()), -1)
    asin_col = next((i for i, h in enumerate(headers) if "asin" in h.lower()), -1)

    if kw_col == -1 or brand_col == -1:
        logger.error("Sheet missing required 'Keyword' and 'Brand' headers.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    target_col_idx = len(headers) + 1

    if target_col_idx > worksheet.col_count:
        logger.info("Expanding Google Sheet grid (+1 Column)...")
        worksheet.add_cols(1)

    safe_update_cell(worksheet, 1, target_col_idx, now_str)
    logger.info(f"Created timestamp column '{now_str}' at Column Index {target_col_idx}")

    targets: List[TargetQuery] = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        kw = row[kw_col].strip() if len(row) > kw_col else ""
        brand = row[brand_col].strip() if len(row) > brand_col else ""
        asin = row[asin_col].strip() if asin_col != -1 and len(row) > asin_col else ""
        if kw and brand:
            targets.append(TargetQuery(row_idx=r_idx, keyword=kw, target_brand=brand, asin=asin or None))

    total = len(targets)
    logger.info(f"Total Targets Loaded: {total}")

    try:
        ranker._init_stealth_driver()
        ranker.update_zip_code()

        for idx, t in enumerate(targets, 1):
            try:
                if idx > 1 and idx % 10 == 0:
                    logger.info("Performing periodic session refresh...")
                    ranker._init_stealth_driver()
                    ranker.update_zip_code()

                rank, status = ranker.fetch_rank(t)
                rank_str = str(rank) if rank is not None else status

                logger.info(f"[{idx}/{total}] Target: '{t.keyword}' | Brand: '{t.target_brand}' | Rank: {rank_str}")

                if safe_update_cell(worksheet, t.row_idx, target_col_idx, rank_str):
                    logger.info(f"--> Saved to Sheet (Row {t.row_idx}, Col {target_col_idx})")
            except Exception as e:
                logger.error(f"[{idx}/{total}] Unhandled error on '{t.keyword}': {str(e)}. Skipping to next keyword.")
                safe_update_cell(worksheet, t.row_idx, target_col_idx, "SCAN_ERROR")

            gc.collect()
            time.sleep(random.uniform(3.0, 5.0))

    finally:
        ranker.close()
        logger.info("Ranking process completed cleanly.")


if __name__ == "__main__":
    CREDENTIALS_JSON = "gcp_key.json"
    SPREADSHEET_ID = "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg"
    SHEET_NAME = "rank_db"

    proxies = []
    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r", encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]

    stealth_ranker = StealthAmazonRanker(
        marketplace_url="https://www.amazon.com",
        zip_code="12345",  # Strictly set to 12345
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
