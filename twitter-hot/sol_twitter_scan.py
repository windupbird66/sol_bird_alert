import os
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

from dotenv import load_dotenv
import asyncio
import sqlite3
import logging
import traceback
from datetime import datetime
import random
import json
import sys
from transformers import pipeline
from transformers import logging as transformers_logging
from playwright.async_api import async_playwright
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from httpx import HTTPStatusError
import warnings

# 設置警告和日誌
warnings.filterwarnings('ignore')

import tensorflow as tf
tf.get_logger().setLevel('ERROR')

# 禁用 Transformers 的 INFO 訊息
transformers_logging.set_verbosity_error()

# 禁用 HTTP 請求的 INFO 訊息
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("solana.rpc.async_api").setLevel(logging.WARNING)

load_dotenv()

# Solana RPC endpoint
SOLANA_RPC = os.getenv('SOLANA_RPC')
PUMP_FUN_PROGRAM_IDS = [
    Pubkey.from_string(os.getenv('PUMP_FUN_PROGRAM_ID')),
]
TWITTER_USERNAME = os.getenv('TWITTER_USERNAME')
TWITTER_PASSWORD = os.getenv('TWITTER_PASSWORD')
TWITTER_EMAIL = os.getenv('TWITTER_EMAIL')

# 檢查必要的環境變量
required_env_vars = ['SOLANA_RPC', 'PUMP_FUN_PROGRAM_ID', 'TWITTER_USERNAME', 'TWITTER_PASSWORD', 'TWITTER_EMAIL']
missing_env_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_env_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_env_vars)}")

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('token_detector.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def adapt_datetime(ts):
    return ts.isoformat()


sqlite3.register_adapter(datetime, adapt_datetime)


class SolanaTokenDetector:
    def __init__(self, search_limit: int = 100):
        self.search_limit = search_limit
        self.conn = sqlite3.connect("solana_tokens.db")
        self._init_db()
        logger.info("Loading NLP model...")
        self.sentiment_analyzer = pipeline(
            "sentiment-analysis",
            model="distilbert-base-uncased-finetuned-sst-2-english"
        )
        self.max_retries = 5
        self.retry_delay = 3
        self.cookie_file = "twitter_cookies.json"

    def _init_db(self):
        with self.conn:
            table_check = self.conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='tokens'
            """).fetchone()

            if not table_check:
                self.conn.execute("""
                    CREATE TABLE tokens (
                        mint_address TEXT PRIMARY KEY,
                        program_id TEXT DEFAULT '',
                        first_seen DATETIME,
                        transaction_signature TEXT DEFAULT '',
                        social_analyzed INTEGER DEFAULT 0
                    )
                """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS social_data (
                    mint_address TEXT,
                    timestamp DATETIME,
                    mentions INTEGER,
                    sentiment REAL,
                    FOREIGN KEY(mint_address) REFERENCES tokens(mint_address)
                )
            """)

    async def save_cookies(self, context):
        cookies = await context.cookies()
        with open(self.cookie_file, 'w') as f:
            json.dump(cookies, f)
        logger.info("Cookies 已保存")

    async def load_cookies(self, context):
        try:
            if os.path.exists(self.cookie_file):
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                await context.add_cookies(cookies)
                logger.info("Cookies 已加載")
                return True
            return False
        except Exception as e:
            logger.error(f"加載 Cookies 失敗: {str(e)}")
            return False

    async def login_twitter(self, page):
        try:
            context = page.context
            cookies_loaded = await self.load_cookies(context)

            if cookies_loaded:
                await page.goto('https://x.com/home')
                # 添加截图调试
                await page.screenshot(path="twitter_page.png")
                # 获取页面HTML内容
                page_content = await page.content()
                with open("twitter_page.html", "w", encoding="utf-8") as f:
                    f.write(page_content)
                logger.info("已保存页面截图和HTML内容用于调试")
                
                # 使用更宽松的等待条件
                try:
                    await page.wait_for_selector('body', timeout=10000)
                    logger.info("页面已加载")
                    return True
                except Exception:
                    pass
                
                # 其余代码保持不变...

            logger.info("⚠️ Cookies 失效，執行手動登入")
            await page.goto('https://x.com/i/flow/login')
            await page.wait_for_selector('input[autocomplete="username"]', timeout=10000)

            await page.fill('input[autocomplete="username"]', TWITTER_USERNAME)
            await page.keyboard.press('Enter')

            try:
                await page.wait_for_selector('input[data-testid="ocfEnterTextTextInput"]', timeout=5000)
                await page.fill('input[data-testid="ocfEnterTextTextInput"]', TWITTER_EMAIL)
                await page.keyboard.press('Enter')
            except Exception:
                pass

            await page.wait_for_selector('input[name="password"]', timeout=10000)
            await page.fill('input[name="password"]', TWITTER_PASSWORD)
            await page.keyboard.press('Enter')

            await page.wait_for_function(
                """() => window.location.hostname === 'x.com'""",
                timeout=10000
            )

            await self.save_cookies(context)
            logger.info("手動登入成功，已儲存新的 Cookies")
            return True

        except Exception as e:
            logger.error(f"Twitter 登錄失敗: {str(e)}")
            return False

    def get_unanalyzed_tokens(self):
        with self.conn:
            cur = self.conn.execute("""
                SELECT mint_address 
                FROM tokens 
                WHERE social_analyzed = 0
                ORDER BY first_seen DESC
            """)
            return [row[0] for row in cur.fetchall()]

    async def analyze_social_data_batch(self, mint_addresses):
        if not mint_addresses:
            logger.info("沒有需要分析的代幣")
            return

        logger.info(f"開始批量分析 {len(mint_addresses)} 個代幣的社交數據")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={'width': 720, 'height': 480},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )

                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)

                page = await context.new_page()

                if not await self.login_twitter(page):
                    logger.error("Twitter 登錄失敗，退出分析")
                    await browser.close()
                    return

                for mint_address in mint_addresses:
                    try:
                        logger.info(f"分析代幣: {mint_address}")
                        results = []

                        await page.goto(f"https://x.com/search?q={mint_address}&src=typed_query&f=live")
                        await asyncio.sleep(3)

                        try:
                            await page.wait_for_selector('div[role="main"]', timeout=30000)

                            scroll_attempts = 10
                            last_tweet_count = 0
                            for _ in range(scroll_attempts):
                                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                                await asyncio.sleep(2)

                                tweets = await page.query_selector_all("[data-testid='tweet']")
                                if len(tweets) == last_tweet_count:
                                    break

                                last_tweet_count = len(tweets)

                            tweets = await page.query_selector_all("[data-testid='tweet']")
                            logger.info(f"找到 {len(tweets)} 條推文")

                            for tweet in tweets[:self.search_limit]:
                                try:
                                    tweet_text_element = await tweet.query_selector("[data-testid='tweetText']")
                                    if tweet_text_element:
                                        content = await tweet_text_element.inner_text()
                                        sentiment = self.sentiment_analyzer(content[:512])[0]
                                        sentiment_score = sentiment["score"] if sentiment["label"] == "POSITIVE" else 1 - sentiment["score"]
                                        results.append({
                                            "sentiment": sentiment_score
                                        })
                                except Exception as e:
                                    logger.warning(f"處理推文出錯: {str(e)}")
                                    continue

                            total_mentions = len(results)
                            avg_sentiment = sum(r["sentiment"] for r in results) / total_mentions if total_mentions else 0

                            with self.conn:
                                self.conn.execute("""
                                    INSERT INTO social_data 
                                    (mint_address, timestamp, mentions, sentiment)
                                    VALUES (?, ?, ?, ?)
                                """, (mint_address, datetime.now(), total_mentions, avg_sentiment))

                                self.conn.execute("""
                                    UPDATE tokens 
                                    SET social_analyzed = 1 
                                    WHERE mint_address = ?
                                """, (mint_address,))

                            logger.info(f"完成代幣 {mint_address} 的分析")

                        except Exception as e:
                            logger.error(f"搜索代幣 {mint_address} 時出錯: {str(e)}")
                            continue

                        await asyncio.sleep(2)

                    except Exception as e:
                        logger.error(f"處理代幣 {mint_address} 時出錯: {str(e)}")
                        continue

                await browser.close()
                logger.info("完成所有代幣的社交分析")

        except Exception as e:
            logger.error(f"批量社交分析過程中出錯: {str(e)}")
            logger.error(traceback.format_exc())

    async def get_transaction_details(self, client: AsyncClient, sig: str):
        try:
            tx_details = await client.get_transaction(
                sig,
                encoding="jsonParsed",
                max_supported_transaction_version=0,
                commitment="confirmed"
            )

            if not tx_details or not tx_details.value:
                return None

            tx_value = tx_details.value
            logger.info(f"=== 分析交易 {sig} ===")

            if hasattr(tx_value, 'transaction'):
                encoded_tx = tx_value.transaction
                if hasattr(encoded_tx, 'meta'):
                    meta = encoded_tx.meta
                    if hasattr(meta, 'pre_token_balances'):
                        pre_balances = meta.pre_token_balances
                        if pre_balances:
                            for balance in pre_balances:
                                if hasattr(balance, 'mint'):
                                    mint_address = balance.mint
                                    logger.info(f"找到 Mint 地址: {mint_address}")
                                    return {
                                        'mint_address': mint_address,
                                        'program_id': str(PUMP_FUN_PROGRAM_IDS[0]),
                                        'signature': sig
                                    }
            return None

        except Exception as e:
            logger.error(f"處理交易時出錯: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    async def fetch_pumpfun_new_tokens(self):
        self.latest_mints = []
        async with AsyncClient(SOLANA_RPC) as client:
            try:
                logger.info("開始獲取 Pump.fun 最近的交易...")
                for program_id in PUMP_FUN_PROGRAM_IDS:
                    logger.info(f"正在檢查程序 ID: {program_id}")
                    try:
                        response = await self._retry_with_backoff(
                            client.get_signatures_for_address,
                            program_id,
                            limit=10
                        )

                        if not response or not response.value:
                            logger.info("未找到任何交易")
                            continue

                        logger.info(f"找到 {len(response.value)} 筆交易")

                        for tx in response.value:
                            try:
                                token_info = await self.get_transaction_details(client, tx.signature)
                                if token_info:
                                    mint_address = str(token_info['mint_address'])
                                    logger.info(f"處理 Mint 地址: {mint_address}")

                                    with self.conn:
                                        self.conn.execute("""
                                            INSERT OR IGNORE INTO tokens 
                                            (mint_address, program_id, first_seen, transaction_signature) 
                                            VALUES (?, ?, ?, ?)
                                        """, (
                                            mint_address,
                                            str(token_info['program_id']),
                                            datetime.now(),
                                            str(token_info['signature'])
                                        ))
                                    
                                    self.latest_mints.append(mint_address)
                                    logger.info("已保存到數據庫")

                            except Exception as e:
                                logger.error(f"處理交易時出錯: {str(e)}")
                                continue

                    except Exception as e:
                        logger.error(f"獲取程序交易時出錯: {str(e)}")
                        continue

            except Exception as e:
                logger.error(f"處理 Pump.fun 交易時出錯: {str(e)}")
                traceback.print_exc()

    async def _retry_with_backoff(self, func, *args, **kwargs):
        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if isinstance(e, HTTPStatusError) and e.response.status_code == 429:
                    if attempt < self.max_retries - 1:
                        delay = (self.retry_delay * (2 ** attempt)) + random.uniform(0, 1)
                        logger.warning(f"Rate limit hit, retrying in {delay:.2f} seconds...")
                        await asyncio.sleep(delay)
                        continue
                logger.error(f"API 錯誤: {str(e)}")
                raise
        return None

    def generate_report(self):
        if not hasattr(self, "latest_mints") or not self.latest_mints:
            print("\n本次運行沒有新的 Mint 地址")
            return

        print("\n=== 本次新找到的 Mint 地址 ===")

        with self.conn:
            for mint_address in self.latest_mints:
                mint_address = str(mint_address)  
                cur = self.conn.execute("""
                    SELECT 
                        SUM(s.mentions) as total_mentions, 
                        AVG(s.sentiment) as avg_sentiment
                    FROM tokens t
                    LEFT JOIN social_data s ON t.mint_address = s.mint_address
                    WHERE t.mint_address = ?
                """, (mint_address,))
                
                result = cur.fetchone()
                total_mentions = result[0] if result[0] is not None else 0
                avg_sentiment = result[1] if result[1] is not None else 0.00

                print(f"\nMint 地址: {mint_address}")
                print(f"社交提及: {total_mentions}")
                print(f"情感指數: {avg_sentiment:.2f}/1.0")

async def main():
    detector = SolanaTokenDetector(search_limit=50)

    # 獲取新代幣
    await detector.fetch_pumpfun_new_tokens()

    # 獲取未分析的代幣列表
    unanalyzed_tokens = detector.get_unanalyzed_tokens()

    if unanalyzed_tokens:
        logger.info(f"發現 {len(unanalyzed_tokens)} 個未分析的代幣")
        # 批量分析社交數據
        await detector.analyze_social_data_batch(unanalyzed_tokens)
    else:
        logger.info("沒有需要分析的新代幣")

    # 生成報告
    detector.generate_report()


if __name__ == "__main__":
    asyncio.run(main())