import gc
import logging
import os
import random
import json
import re
import sys
import time
import zipfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set

import gspread
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Load .env file
load_dotenv()

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
    target_asin: Optional[str] = None


class StealthAmazonRanker:
    def __init__(
        self,
        marketplace_url: str = "https://www.amazon.com",
        zip_code: Optional[str] = "12345",
        max_pages: int = 8,
        max_retries: int = 3,
        proxy_list: Optional[List[str]] = None,
        decodo_host: Optional[str] = None,
        decodo_port: Optional[str] = None,
        decodo_user: Optional[str] = None,
        decodo_pass: Optional[str] = None,
    ):
        self.marketplace_url = marketplace_url.rstrip('/')
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.proxy_list = proxy_list or []
        self.driver: Optional[uc.Chrome] = None
        self.proxy_plugin_path: Optional[str] = None

        # Fetch Decodo Credentials from env or arguments
        self.decodo_host = decodo_host or os.getenv("DECODO_HOST", "")
        self.decodo_port = decodo_port or os.getenv("DECODO_PORT", "")
        self.decodo_user = decodo_user or os.getenv("DECODO_USER", "")
        self.decodo_pass = decodo_pass or os.getenv("DECODO_PASS", "")

        # Fallback to GitHub Secret if full string provided
        self.proxy_secret = os.getenv("PROXY_SERVER_SECRET", "")

        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ]

    def _create_proxy_extension(self, host, port, user, password) -> str:
        """Dynamically creates a Chrome Plugin to handle Decodo Proxy Authentication."""
        manifest_json = 
        {
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Decodo Proxy Auth",
            "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>", "webRequest", "webRequestBlocking"],
            "background": {"scripts": ["background.js"]},
            "minimum_chrome_version":"22.0.0"
        }
    

        background_js = f
        var config = {{
            mode: "fixed_servers",
            rules: {{
              singleProxy: {{
                scheme: "http",
                host: "{host}",
                port: parseInt({port})
              }},
              bypassList: ["localhost"]
            }}
          }};

        chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

        function callbackFn(details) {{
            return {{
                authCredentials: {{
                    username: "{user}",
                    password: "{password}"
                }}
            }};
        }}

        chrome.webRequest.onAuthRequired.addListener(
            callbackFn,
            {{urls: ["<all_urls>"]}},
            ['blocking']
        );
        

        plugin_file = 'decodo_proxy_plugin.zip'
        with zipfile.ZipFile(plugin_file, 'w') as zp:
            zp.writestr("manifest.json", manifest_json)
            zp.writestr("background.js", background_js)
        
        return os.path.abspath(plugin_file)

    def _init_stealth_driver(self):
        """Initializes a fresh Chrome instance with Decodo Proxy and Stealth settings."""
        if self.driver:
            self.close()

        # ALWAYS CREATE A FRESH CHROME OPTIONS OBJECT TO PREVENT REUSE ERRORS
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

        # Decodo Proxy (Option B) - Extension setup
        if self.decodo_host and self.decodo_port and self.decodo_user and self.decodo_pass:
            logger.info(f"Connecting via Decodo Proxy: {self.decodo_host}:{self.decodo_port}")
            self.proxy_plugin_path = self._create_proxy_extension(
                self.decodo_host, self.decodo_port, self.decodo_user, self.decodo_pass
            )
            options.add_extension(self.proxy_plugin_path)
        elif self.proxy_secret:
            clean_secret = self.proxy_secret.replace("http://", "").replace("https://", "")
            if "@" in clean_secret:
                auth, host_port = clean_secret.split("@")
                user, password = auth.split(":")
                host, port = host_port.split(":")
                self.proxy_plugin_path = self._create_proxy_extension(host, port, user, password)
                options.add_extension(self.proxy_plugin_path)
            else:
                options.add_argument(f"--proxy-server=http://{clean_secret}")
        else:
            logger.warning("[!] No Proxy Details Found in Environment!")

        # Initialize Browser Instance safely
        # Force Undetected Chromedriver to use Version 151
        # Force Undetected Chromedriver launch
        try:
            self.driver = uc.Chrome(options=options)
        except Exception as e:
            logger.warning(f"Standard Chrome launch failed ({e}), attempting subprocess fallback...")
            fallback_options = uc.ChromeOptions()
            fallback_options.add_argument("--headless=new")
            fallback_options.add_argument("--no-sandbox")
            fallback_options.add_argument("--disable-dev-shm-usage")
            fallback_options.add_argument(f"user-agent={random.choice(self.user_agents)}")
            if self.proxy_plugin_path:
                fallback_options.add_extension(self.proxy_plugin_path)
            self.driver = uc.Chrome(options=fallback_options, use_subprocess=True)

        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        window.chrome = { runtime: {} };
        """
        self.driver.execute_script(stealth_js)
        logger.info("Stealth Chrome Driver successfully loaded.")
        logger.info("Stealth Chrome Driver successfully loaded.")
        
    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

        if self.proxy_plugin_path and os.path.exists(self.proxy_plugin_path):
            try:
                os.remove(self.proxy_plugin_path)
            except Exception:
                pass

    def _detect_and_handle_block(self) -> bool:
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
        if not self.zip_code or not self.driver:
            return True

        try:
            logger.info(f"Setting location to US ZIP Code: {self.zip_code}")
            self.driver.get(self.marketplace_url)
            time.sleep(random.uniform(2.5, 3.5))

            if self._detect_and_handle_block():
                return False

            try:
                self.driver.add_cookie({"name": "i18n-prefs", "value": "USD", "domain": ".amazon.com"})
                self.driver.add_cookie({"name": "lc-main", "value": "en_US", "domain": ".amazon.com"})
            except Exception as e:
                logger.debug(f"Cookie injection warning: {str(e)}")

            try:
                curr_loc = self.driver.find_element(By.ID, "glow-ingress-line2").text
                if str(self.zip_code) in curr_loc or "New York" in curr_loc or "US" in curr_loc:
                    logger.info(f"[SUCCESS] Location verified in header: '{curr_loc}'")
                    return True
            except Exception:
                pass

            api_js = f
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
        

            try:
                api_res = self.driver.execute_async_script(api_js)
                if api_res and api_res.get("success"):
                    self.driver.refresh()
                    time.sleep(3.0)
                    new_loc = self.driver.find_element(By.ID, "glow-ingress-line2").text
                    if str(self.zip_code) in new_loc or "US" in new_loc:
                        logger.info(f"[SUCCESS] ZIP Code applied via API: '{new_loc}'")
                        return True
            except Exception as e:
                logger.warning(f"API injection failed, proceeding to UI fallback: {str(e)}")

            return True

        except Exception as e:
            logger.error(f"ZIP code update error: {str(e)}")
            return False

    def _human_scroll(self):
        for _ in range(random.randint(3, 5)):
            scroll_step = random.randint(400, 750)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_step});")
            time.sleep(random.uniform(0.4, 0.8))

    @staticmethod
    def _is_sponsored_item(element) -> bool:
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
    def _match_brand_or_asin(target_brand: str, title: str, item_soup, target_asin: Optional[str] = None) -> bool:
        item_asin = item_soup.get('data-asin', '').strip().upper()

        if target_asin and target_asin.strip():
            if item_asin == target_asin.strip().upper():
                logger.info(f"Matched by Target ASIN: {item_asin}")
                return True

        if not target_brand:
            return False

        target_str = target_brand.strip()
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
                    logger.warning(f"Block detected on Attempt {attempt}. Renewing session...")
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

                    if self._match_brand_or_asin(query.target_brand, title_text, item, target_asin=query.target_asin):
                        logger.info(f"[SUCCESS] Target Match: Brand='{query.target_brand}', ASIN='{query.target_asin}' | Organic Rank: {organic_counter}")
                        return organic_counter

                next_page = soup.select_one("a.s-pagination-next")
                if not next_page or "s-pagination-disabled" in next_page.get('class', []):
                    break

            if not block_occurred:
                return None

        return None


def safe_update_cell(worksheet, row: int, col: int, value: str, max_retries: int = 4):
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
        logger.error(f"Google Sheet connection error: {str(e)}")
        return

    all_rows = worksheet.get_all_values()
    if not all_rows:
        logger.error("Empty sheet!")
        return

    headers = list(all_rows[0])
    while headers and not headers[-1].strip():
        headers.pop()

    kw_col = next((i for i, h in enumerate(headers) if "keyword" in h.lower()), -1)
    brand_col = next((i for i, h in enumerate(headers) if "brand" in h.lower()), -1)
    asin_col = next((i for i, h in enumerate(headers) if "asin" in h.lower()), -1)

    if kw_col == -1 or brand_col == -1:
        logger.error("Sheet missing required 'Keyword' and 'Brand' headers.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    target_col_idx = len(headers) + 1

    if target_col_idx > worksheet.col_count:
        worksheet.add_cols(1)

    safe_update_cell(worksheet, 1, target_col_idx, now_str)
    logger.info(f"Created timestamp column '{now_str}' at Column Index {target_col_idx}")

    targets: List[TargetQuery] = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        kw = row[kw_col].strip() if len(row) > kw_col else ""
        brand = row[brand_col].strip() if len(row) > brand_col else ""
        asin = row[asin_col].strip() if (asin_col != -1 and len(row) > asin_col) else None
        if kw and brand:
            targets.append(TargetQuery(row_idx=r_idx, keyword=kw, target_brand=brand, target_asin=asin))

    total = len(targets)
    logger.info(f"Total Targets Loaded: {total}")

    try:
        ranker._init_stealth_driver()
        ranker.update_zip_code()

        for idx, t in enumerate(targets, 1):
            if idx > 1 and idx % 10 == 0:
                logger.info("Performing periodic session refresh...")
                ranker._init_stealth_driver()
                ranker.update_zip_code()

            rank = ranker.fetch_rank(t)
            rank_str = str(rank) if rank is not None else "NOT_FOUND"

            logger.info(f"[{idx}/{total}] Target: '{t.keyword}' | Brand: '{t.target_brand}' | ASIN: '{t.target_asin}' | Rank: {rank_str}")

            if safe_update_cell(worksheet, t.row_idx, target_col_idx, rank_str):
                logger.info(f"--> Saved to Sheet (Row {t.row_idx}, Col {target_col_idx})")

            gc.collect()
            time.sleep(random.uniform(3.0, 5.0))

    finally:
        ranker.close()
        logger.info("Ranking process completed cleanly.")


if __name__ == "__main__":
    CREDENTIALS_JSON = os.getenv("GCP_KEY_PATH", "gen-lang-client-0598815756-11b746f33e83.json")
    SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg")
    SHEET_NAME = os.getenv("SHEET_NAME", "rank_db")

    proxies =["user-spojetph7l-country-us:73G=71KddvkQucokHq@gate.decodo.com:10001"]

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
