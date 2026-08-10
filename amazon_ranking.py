from __future__ import annotations

import gc
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

MARKETPLACE_URL = "https://www.amazon.com"
ZIP_CODE = "12345"

MAX_PAGES = 8
MAX_KEYWORD_ATTEMPTS = 3
PAGE_LOAD_TIMEOUT = 35

BATCH_SIZE = 10
BATCH_DELAY_SECONDS = 12
DRIVER_RESTART_INTERVAL = 25

# If True, an Amazon result page with suspiciously few products is rejected
# instead of being accepted as a valid "not ranked" page.
MIN_EXPECTED_RESULTS_PAGE_1 = 3

LOG_FILE = "amazon_production_ranker.log"


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

logger = logging.getLogger("AmazonRanker")


# ---------------------------------------------------------------------------
# DATA TYPES
# ---------------------------------------------------------------------------

class RankStatus(str, Enum):
    FOUND = "FOUND"
    NOT_RANKED = "NOT_RANKED"
    BLOCKED = "BLOCKED"
    CAPTCHA = "CAPTCHA"
    PAGE_ERROR = "PAGE_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    ZIP_UNVERIFIED = "ZIP_UNVERIFIED"
    RETRY = "RETRY"
    INPUT_ERROR = "INPUT_ERROR"


@dataclass
class TargetQuery:
    row_idx: int
    keyword: str
    brand: str
    asin: str = ""


@dataclass
class RankResult:
    status: RankStatus
    rank: Optional[int] = None
    asin: str = ""
    keyword: str = ""
    message: str = ""
    pages_scanned: int = 0
    organic_results_scanned: int = 0
    confidence: str = "LOW"

    @property
    def sheet_value(self) -> str:
        if self.status == RankStatus.FOUND and self.rank is not None:
            return str(self.rank)
        if self.status == RankStatus.NOT_RANKED:
            return "NOT_RANKED"
        return self.status.value


# ---------------------------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------------------------

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)


def normalize_asin(value: str) -> str:
    value = (value or "").strip().upper()

    # Accept a raw ASIN or an Amazon URL containing /dp/ASIN.
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", value, re.I)
    if match:
        return match.group(1).upper()

    if ASIN_RE.fullmatch(value):
        return value

    return ""


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


# ---------------------------------------------------------------------------
# AMAZON RANKER
# ---------------------------------------------------------------------------

class AmazonOrganicRanker:
    """
    Deterministic ranking engine.

    Important:
    - We do NOT infer a technical failure as NOT_RANKED.
    - We count unique ASINs only.
    - Target ASIN is preferred over brand/title matching.
    """

    def __init__(
        self,
        marketplace_url: str = MARKETPLACE_URL,
        zip_code: Optional[str] = ZIP_CODE,
        max_pages: int = MAX_PAGES,
        max_retries: int = MAX_KEYWORD_ATTEMPTS,
        proxy_list: Optional[List[str]] = None,
    ):
        self.marketplace_url = marketplace_url.rstrip("/")
        self.zip_code = zip_code
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.proxy_list = proxy_list or []

        self.driver: Optional[webdriver.Chrome] = None
        self.current_proxy: Optional[str] = None
        self.zip_verified = False

    # ------------------------------------------------------------------
    # DRIVER
    # ------------------------------------------------------------------

    def _choose_proxy(self) -> Optional[str]:
        if not self.proxy_list:
            return None
        return random.choice(self.proxy_list)

    def _init_driver(self) -> None:
        self.close()

        options = webdriver.ChromeOptions()
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-US")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")

        # Do not depend on a hard-coded Chrome version.
        # Selenium Manager will select the compatible driver.
        if self.proxy_list:
            self.current_proxy = self._choose_proxy()
            if self.current_proxy:
                options.add_argument(f"--proxy-server={self.current_proxy}")
                logger.info("Starting Chrome with configured proxy.")
        else:
            self.current_proxy = None

        self.driver = webdriver.Chrome(options=options,chrome=150)
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        logger.info("Chrome driver initialized.")

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None
        self.zip_verified = False

    # ------------------------------------------------------------------
    # PAGE HEALTH
    # ------------------------------------------------------------------

    def _page_text(self) -> Tuple[str, str]:
        if not self.driver:
            return "", ""

        try:
            return (
                (self.driver.title or "").lower(),
                (self.driver.page_source or "").lower(),
            )
        except Exception:
            return "", ""

    def detect_page_state(self) -> Optional[RankStatus]:
        title, source = self._page_text()

        if not title and not source:
            return RankStatus.PAGE_ERROR

        captcha_markers = (
            "enter the characters you see below",
            "captcha",
            "robot check",
            "type the characters",
            "sorry, we just need to make sure you're not a robot",
        )

        block_markers = (
            "503 service unavailable",
            "500 internal server error",
            "request was rejected",
            "api error",
        )

        if any(x in title or x in source for x in captcha_markers):
            return RankStatus.CAPTCHA

        if any(x in title or x in source for x in block_markers):
            return RankStatus.BLOCKED

        # Amazon sometimes serves an error page with no useful results.
        if "dogs of amazon" in source:
            return RankStatus.PAGE_ERROR

        return None

    # ------------------------------------------------------------------
    # ZIP
    # ------------------------------------------------------------------

    def update_and_verify_zip(self) -> bool:
        """
        Best-effort ZIP verification.

        The important change from the old code is:
        if the ZIP interaction fails, we return False instead of pretending
        the ZIP was applied.
        """
        if not self.zip_code:
            self.zip_verified = True
            return True

        if not self.driver:
            self._init_driver()

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "Setting ZIP %s (attempt %d/%d)",
                    self.zip_code,
                    attempt,
                    self.max_retries,
                )

                self.driver.get(self.marketplace_url)
                time.sleep(random.uniform(2.0, 3.5))

                state = self.detect_page_state()
                if state:
                    logger.warning("Cannot set ZIP; page state=%s", state.value)
                    self.close()
                    self._init_driver()
                    continue

                location = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.ID, "nav-global-location-slot")
                    )
                )
                location.click()

                time.sleep(random.uniform(1.0, 2.0))

                zip_input = WebDriverWait(self.driver, 10).until(
                    EC.visibility_of_element_located(
                        (By.ID, "GLUXZipUpdateInput")
                    )
                )

                zip_input.clear()
                zip_input.send_keys(str(self.zip_code))

                submit = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "#GLUXZipUpdate input[type='submit']",
                )

                if submit:
                    self.driver.execute_script(
                        "arguments[0].click();", submit[0]
                    )
                else:
                    zip_input.send_keys(Keys.ENTER)

                time.sleep(random.uniform(2.0, 3.0))

                # Try to close location modal if still open.
                close_buttons = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-action='a-popover-close']",
                )
                if close_buttons:
                    try:
                        close_buttons[0].click()
                    except Exception:
                        pass

                # Reload marketplace and inspect location text.
                self.driver.get(self.marketplace_url)
                time.sleep(random.uniform(2.0, 3.0))

                state = self.detect_page_state()
                if state:
                    logger.warning("ZIP verification page state=%s", state.value)
                    continue

                body = self.driver.find_element(By.TAG_NAME, "body").text.lower()

                # Amazon can show the ZIP in the location text. Exact UI varies,
                # so this is treated as a positive signal, not a guarantee.
                if str(self.zip_code).lower() in body:
                    self.zip_verified = True
                    logger.info("ZIP %s appears to be active.", self.zip_code)
                    return True

                # If Amazon did not expose the ZIP in body text, do not claim
                # verification.
                logger.warning("ZIP could not be positively verified.")

            except Exception as exc:
                logger.warning("ZIP attempt failed: %s", exc)

            time.sleep(2)

        self.zip_verified = False
        return False

    # ------------------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------------------

    def build_search_url(self, keyword: str, page: int) -> str:
        encoded = urllib.parse.quote_plus(keyword)

        url = f"{self.marketplace_url}/s?k={encoded}"

        if page > 1:
            url += f"&page={page}"

        return url

    def load_search_page(self, keyword: str, page: int) -> Tuple[Optional[BeautifulSoup], Optional[RankStatus]]:
        if not self.driver:
            self._init_driver()

        url = self.build_search_url(keyword, page)

        try:
            logger.info("Opening page %d: %s", page, url)
            self.driver.get(url)

            # Let dynamic result components settle.
            time.sleep(random.uniform(2.5, 4.5))

        except TimeoutException:
            logger.warning("Page load timeout.")
            return None, RankStatus.PAGE_ERROR

        except WebDriverException as exc:
            logger.warning("Browser navigation failed: %s", exc)
            return None, RankStatus.PAGE_ERROR

        state = self.detect_page_state()
        if state:
            return None, state

        try:
            WebDriverWait(self.driver, 12).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div[data-asin]")
                )
            )
        except TimeoutException:
            logger.warning("No ASIN containers appeared.")
            return None, RankStatus.PARSE_ERROR

        source = self.driver.page_source
        soup = BeautifulSoup(source, "html.parser")

        state = self.detect_page_state()
        if state:
            return None, state

        return soup, None

    # ------------------------------------------------------------------
    # RESULT EXTRACTION
    # ------------------------------------------------------------------

    @staticmethod
    def is_non_organic_placement(element) -> bool:
        """
        Multi-signal sponsored detection.

        Amazon markup changes, therefore no single CSS class is trusted.
        """

        component = (element.get("data-component-type") or "").lower()
        classes = " ".join(element.get("class", [])).lower()

        # Common ad/component signals.
        component_signals = (
            "ads",
            "sponsored",
            "shopping-ad",
            "video-widget",
        )

        class_signals = (
            "s-sponsored",
            "s-sponsored-header",
            "puis-sponsored",
            "adholder",
            "ad-feedback",
        )

        if any(x in component for x in component_signals):
            return True

        if any(x in classes for x in class_signals):
            return True

        # Text-level signal.
        text = element.get_text(" ", strip=True).lower()

        if re.search(r"\bsponsored\b", text):
            return True

        # Known nested sponsored labels.
        sponsored_selectors = (
            ".puis-sponsored-label-text",
            ".s-sponsored-label-info-icon",
            ".s-label-popover-default",
            "[aria-label*='Sponsored']",
        )

        for selector in sponsored_selectors:
            if element.select_one(selector):
                return True

        return False

    @staticmethod
    def extract_asin(element) -> str:
        asin = normalize_asin(element.get("data-asin", ""))

        if asin:
            return asin

        # Fallback: find /dp/ASIN or /gp/product/ASIN inside links.
        for anchor in element.select("a[href]"):
            href = anchor.get("href", "")
            asin = normalize_asin(href)
            if asin:
                return asin

        return ""

    @staticmethod
    def extract_title(element) -> str:
        selectors = (
            "h2 a span",
            "h2 span",
            "h2",
            "a.a-link-normal span.a-text-normal",
        )

        for selector in selectors:
            node = element.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text

        return ""

    @staticmethod
    def extract_results(soup: BeautifulSoup) -> List[Dict[str, str]]:
        """
        Extract one logical product per unique ASIN.

        This is intentionally separate from rank counting.
        """

        results: List[Dict[str, str]] = []
        seen_asins = set()

        containers = soup.select("div[data-asin]")

        for element in containers:
            asin = AmazonOrganicRanker.extract_asin(element)

            if not asin:
                continue

            if asin in seen_asins:
                continue

            seen_asins.add(asin)

            results.append(
                {
                    "asin": asin,
                    "title": AmazonOrganicRanker.extract_title(element),
                    "sponsored": (
                        "1"
                        if AmazonOrganicRanker.is_non_organic_placement(element)
                        else "0"
                    ),
                }
            )

        return results

    # ------------------------------------------------------------------
    # TARGET MATCHING
    # ------------------------------------------------------------------

    @staticmethod
    def target_matches(target: TargetQuery, result: Dict[str, str]) -> Tuple[bool, str]:
        """
        ASIN is authoritative.

        Brand/title fallback exists only for backwards compatibility.
        """

        result_asin = normalize_asin(result.get("asin", ""))

        if target.asin:
            return result_asin == target.asin, "ASIN"

        if not target.brand:
            return False, "NONE"

        brand = compact_text(target.brand)
        title = compact_text(result.get("title", ""))

        if brand and brand in title:
            return True, "BRAND_TITLE_FALLBACK"

        return False, "NONE"

    # ------------------------------------------------------------------
    # RANKING
    # ------------------------------------------------------------------

    def fetch_organic_rank(self, target: TargetQuery) -> RankResult:
        if not target.keyword:
            return RankResult(
                status=RankStatus.INPUT_ERROR,
                keyword=target.keyword,
                asin=target.asin,
                message="Keyword is empty.",
            )

        if target.asin and not ASIN_RE.fullmatch(target.asin):
            return RankResult(
                status=RankStatus.INPUT_ERROR,
                keyword=target.keyword,
                asin=target.asin,
                message="Invalid ASIN format.",
            )

        if not self.driver:
            self._init_driver()

        # ZIP must be explicitly verified before accepting rank data.
        if self.zip_code and not self.zip_verified:
            if not self.update_and_verify_zip():
                return RankResult(
                    status=RankStatus.ZIP_UNVERIFIED,
                    keyword=target.keyword,
                    asin=target.asin,
                    message="ZIP could not be verified.",
                )

        last_error: Optional[RankStatus] = None

        for attempt in range(1, self.max_retries + 1):
            logger.info(
                "Keyword '%s' attempt %d/%d",
                target.keyword,
                attempt,
                self.max_retries,
            )

            organic_rank = 0
            seen_global_asins = set()
            pages_scanned = 0

            try:
                for page in range(1, self.max_pages + 1):
                    soup, error_state = self.load_search_page(
                        target.keyword,
                        page,
                    )

                    if error_state:
                        last_error = error_state

                        # Technical states should retry. They are never
                        # converted to NOT_RANKED.
                        if error_state in {
                            RankStatus.BLOCKED,
                            RankStatus.CAPTCHA,
                            RankStatus.PAGE_ERROR,
                            RankStatus.PARSE_ERROR,
                        }:
                            logger.warning(
                                "Page %d failed with %s.",
                                page,
                                error_state.value,
                            )

                            self.close()
                            self._init_driver()

                            if self.zip_code and not self.update_and_verify_zip():
                                return RankResult(
                                    status=RankStatus.ZIP_UNVERIFIED,
                                    keyword=target.keyword,
                                    asin=target.asin,
                                    message="ZIP lost during recovery.",
                                    pages_scanned=pages_scanned,
                                )

                            break

                        break

                    results = self.extract_results(soup)

                    if page == 1 and len(results) < MIN_EXPECTED_RESULTS_PAGE_1:
                        logger.warning(
                            "Page 1 has only %d parsed results; rejecting page.",
                            len(results),
                        )
                        last_error = RankStatus.PARSE_ERROR
                        break

                    pages_scanned += 1

                    page_has_products = False

                    for result in results:
                        asin = result["asin"]

                        if asin in seen_global_asins:
                            continue

                        seen_global_asins.add(asin)
                        page_has_products = True

                        # Sponsored results never increase organic rank.
                        if result["sponsored"] == "1":
                            continue

                        organic_rank += 1

                        matched, method = self.target_matches(target, result)

                        if matched:
                            confidence = (
                                "HIGH"
                                if method == "ASIN"
                                else "MEDIUM"
                            )

                            logger.info(
                                "FOUND keyword='%s' ASIN='%s' rank=%d method=%s",
                                target.keyword,
                                asin,
                                organic_rank,
                                method,
                            )

                            return RankResult(
                                status=RankStatus.FOUND,
                                rank=organic_rank,
                                asin=asin,
                                keyword=target.keyword,
                                message=f"Matched using {method}.",
                                pages_scanned=pages_scanned,
                                organic_results_scanned=organic_rank,
                                confidence=confidence,
                            )

                    if not page_has_products:
                        # No products is a parser/search problem, not
                        # NOT_RANKED.
                        last_error = RankStatus.PARSE_ERROR
                        break

                    # Pagination existence.
                    next_button = soup.select_one("a.s-pagination-next")

                    if not next_button:
                        break

                    classes = " ".join(next_button.get("class", []))
                    if "s-pagination-disabled" in classes.lower():
                        break

                else:
                    # Completed all pages normally.
                    pass

                # If the entire requested scan completed without technical
                # failure, NOT_RANKED is a valid result.
                if pages_scanned > 0 and last_error is None:
                    logger.info(
                        "NOT_RANKED keyword='%s' pages=%d organic=%d",
                        target.keyword,
                        pages_scanned,
                        organic_rank,
                    )

                    return RankResult(
                        status=RankStatus.NOT_RANKED,
                        asin=target.asin,
                        keyword=target.keyword,
                        message="Target was not present in validated organic results.",
                        pages_scanned=pages_scanned,
                        organic_results_scanned=organic_rank,
                        confidence="HIGH" if target.asin else "MEDIUM",
                    )

            except (StaleElementReferenceException, WebDriverException) as exc:
                logger.warning("Browser error: %s", exc)
                last_error = RankStatus.PAGE_ERROR

            except Exception as exc:
                logger.exception("Unexpected ranking error: %s", exc)
                last_error = RankStatus.PARSE_ERROR

            # Recovery before next attempt.
            if attempt < self.max_retries:
                logger.info("Recovering browser before retry...")
                self.close()
                time.sleep(random.uniform(2.0, 4.0))
                self._init_driver()

                if self.zip_code and not self.update_and_verify_zip():
                    return RankResult(
                        status=RankStatus.ZIP_UNVERIFIED,
                        keyword=target.keyword,
                        asin=target.asin,
                        message="ZIP could not be restored after retry.",
                    )

                time.sleep(random.uniform(2.0, 4.0))

        return RankResult(
            status=last_error or RankStatus.RETRY,
            asin=target.asin,
            keyword=target.keyword,
            message="Ranking could not be validated after retries.",
        )


# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

def get_robust_gspread_client(json_key_path: str):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_file(
        json_key_path,
        scopes=scopes,
    )

    return gspread.authorize(creds)


def find_header(headers: List[str], candidates: Tuple[str, ...]) -> int:
    normalized = [normalize_text(h) for h in headers]

    for candidate in candidates:
        candidate_norm = normalize_text(candidate)

        for idx, header in enumerate(normalized):
            if header == candidate_norm:
                return idx

    # Contains fallback.
    for candidate in candidates:
        candidate_norm = normalize_text(candidate)

        for idx, header in enumerate(normalized):
            if candidate_norm in header:
                return idx

    return -1


def ensure_column(
    worksheet,
    headers: List[str],
    name: str,
) -> int:
    idx = find_header(headers, (name,))

    if idx >= 0:
        return idx + 1

    new_col = len(headers) + 1

    if new_col > worksheet.col_count:
        worksheet.add_cols(max(1, new_col - worksheet.col_count))

    worksheet.update_cell(1, new_col, name)
    headers.append(name)

    return new_col


def process_rank_db_sheet(
    json_key_path: str,
    spreadsheet_id_or_name: str,
    target_sheet_name: str,
    ranker: AmazonOrganicRanker,
    batch_size: int = BATCH_SIZE,
    batch_delay_seconds: float = BATCH_DELAY_SECONDS,
    driver_restart_interval: int = DRIVER_RESTART_INTERVAL,
) -> None:

    if not os.path.exists(json_key_path):
        fallback = (
            "Credentials.json"
            if os.path.exists("Credentials.json")
            else "credentials.json"
        )

        if os.path.exists(fallback):
            json_key_path = fallback
        else:
            raise FileNotFoundError(
                f"Credentials file not found: {json_key_path}"
            )

    client = get_robust_gspread_client(json_key_path)

    if len(spreadsheet_id_or_name) > 30 and "/" not in spreadsheet_id_or_name:
        spreadsheet = client.open_by_key(spreadsheet_id_or_name)
    else:
        spreadsheet = client.open(spreadsheet_id_or_name)

    worksheet = spreadsheet.worksheet(target_sheet_name)

    all_rows = worksheet.get_all_values()

    if not all_rows:
        raise ValueError("Google Sheet is empty.")

    headers = all_rows[0]

    kw_idx = find_header(
        headers,
        ("keyword", "keywords", "search term"),
    )

    brand_idx = find_header(
        headers,
        ("brand", "brand name"),
    )

    asin_idx = find_header(
        headers,
        ("asin", "target asin", "product asin"),
    )

    if kw_idx < 0:
        raise ValueError("Keyword column not found.")

    if brand_idx < 0:
        logger.warning(
            "Brand column not found. ASIN mode is strongly recommended."
        )

    # Create timestamp + diagnostic columns.
    timestamp = datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%d %I:%M %p"
    )

    result_col = ensure_column(
        worksheet,
        headers,
        timestamp,
    )

    status_col = ensure_column(
        worksheet,
        headers,
        f"{timestamp} STATUS",
    )

    confidence_col = ensure_column(
        worksheet,
        headers,
        f"{timestamp} CONFIDENCE",
    )

    pages_col = ensure_column(
        worksheet,
        headers,
        f"{timestamp} PAGES",
    )

    # Build targets.
    targets: List[TargetQuery] = []

    for row_idx, row in enumerate(all_rows[1:], start=2):
        keyword = (
            row[kw_idx].strip()
            if kw_idx < len(row)
            else ""
        )

        brand = (
            row[brand_idx].strip()
            if brand_idx >= 0 and brand_idx < len(row)
            else ""
        )

        asin = (
            normalize_asin(row[asin_idx])
            if asin_idx >= 0 and asin_idx < len(row)
            else ""
        )

        if keyword:
            targets.append(
                TargetQuery(
                    row_idx=row_idx,
                    keyword=keyword,
                    brand=brand,
                    asin=asin,
                )
            )

    logger.info("Loaded %d keyword targets.", len(targets))

    if not targets:
        logger.warning("No usable keywords found.")
        return

    ranker._init_driver()

    if ranker.zip_code:
        if not ranker.update_and_verify_zip():
            raise RuntimeError(
                "ZIP could not be verified. Refusing to collect rank data."
            )

    pending_updates = []

    try:
        for index, target in enumerate(targets, start=1):

            if index > 1 and index % driver_restart_interval == 0:
                logger.info(
                    "Scheduled browser restart at keyword %d/%d.",
                    index,
                    len(targets),
                )

                ranker.close()
                ranker._init_driver()

                if ranker.zip_code and not ranker.update_and_verify_zip():
                    logger.error(
                        "ZIP verification failed after scheduled restart."
                    )

            result = ranker.fetch_organic_rank(target)

            logger.info(
                "[%d/%d] %s | ASIN=%s | STATUS=%s | RANK=%s | CONF=%s",
                index,
                len(targets),
                target.keyword,
                target.asin or "-",
                result.status.value,
                result.rank if result.rank is not None else "-",
                result.confidence,
            )

            pending_updates.extend(
                [
                    gspread.Cell(
                        target.row_idx,
                        result_col,
                        result.sheet_value,
                    ),
                    gspread.Cell(
                        target.row_idx,
                        status_col,
                        result.status.value,
                    ),
                    gspread.Cell(
                        target.row_idx,
                        confidence_col,
                        result.confidence,
                    ),
                    gspread.Cell(
                        target.row_idx,
                        pages_col,
                        str(result.pages_scanned),
                    ),
                ]
            )

            if index % batch_size == 0 or index == len(targets):
                if pending_updates:
                    logger.info(
                        "Writing %d cells to Google Sheets...",
                        len(pending_updates),
                    )

                    worksheet.update_cells(
                        pending_updates,
                        value_input_option="USER_ENTERED",
                    )

                    pending_updates.clear()

                if index < len(targets):
                    delay = batch_delay_seconds + random.uniform(1, 4)
                    logger.info(
                        "Batch pause: %.1f seconds.",
                        delay,
                    )
                    time.sleep(delay)

            gc.collect()

    finally:
        ranker.close()

    logger.info("Ranking job completed.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    CREDENTIALS_JSON = "gcp_key.json"

    SPREADSHEET_ID_OR_NAME = (
        "1cTaEFedbs2VbaJN_3MFnn7K4AxYtWY5Cf-ZJ3BUWLeg"
    )

    TARGET_SHEET_NAME = "rank_db"

    # IMPORTANT:
    # This code does not treat a failed ZIP update as success.
    ZIP_CODE = "12345"

    # Scan up to this many Amazon result pages.
    MAX_PAGE_LIMIT = 8

    # Optional proxies.
    # Format examples depend on your proxy provider.
    # Keep empty if not needed.
    PROXY_POOL: List[str] = []

    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r", encoding="utf-8") as proxy_file:
            PROXY_POOL = [
                line.strip()
                for line in proxy_file
                if line.strip()
            ]

    ranker = AmazonOrganicRanker(
        marketplace_url="https://www.amazon.com",
        zip_code=ZIP_CODE,
        max_pages=MAX_PAGE_LIMIT,
        max_retries=3,
        proxy_list=PROXY_POOL,
    )

    process_rank_db_sheet(
        json_key_path=CREDENTIALS_JSON,
        spreadsheet_id_or_name=SPREADSHEET_ID_OR_NAME,
        target_sheet_name=TARGET_SHEET_NAME,
        ranker=ranker,
        batch_size=10,
        batch_delay_seconds=12,
        driver_restart_interval=25,
    )
