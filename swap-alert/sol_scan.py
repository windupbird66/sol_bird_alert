import sys
import asyncio
import logging
import time
import re
from datetime import datetime
from typing import List, Optional, Dict
from collections import defaultdict
import requests
import pandas as pd
from sqlalchemy import create_engine, text
import traceback
import os
import json

from solana.rpc.api import Client
from solders.pubkey import Pubkey

# 日志设置
logging.basicConfig(
    level=logging.WARNING,  # 设置基础日志级别为 WARNING
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('solana_monitor.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# 创建自定义日志过滤器


class LogFilter(logging.Filter):
    def filter(self, record):
        # 过滤掉 HTTP Request 相关的日志
        return "HTTP Request" not in record.getMessage()


logger = logging.getLogger(__name__)
logger.addFilter(LogFilter())
logger.setLevel(logging.INFO)  # 只有程序主要日志保持 INFO 级别


class Config:
    """配置类"""
    RPC_ENDPOINT = "https://solana-mainnet.g.alchemy.com/v2/0mrzBQkP9BEv6o817E8JM2zyAKzgEAZW"  # RPC 端点

    # DEX 程序 IDs
    JUPITER_PROGRAM_IDS = [
        "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uJvfo",  # Jupiter v4
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"   # Jupiter v6
    ]
    RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

    # 交易阈值 (SOL)
    MIN_SWAP_AMOUNT = 10

    # 数据库设置
    DB_URL = 'sqlite:///solana_swaps.db'

    # Token API
    JUPITER_TOKEN_API = "https://token.jup.ag/all"
    
    # 监控的地址列表
    MONITORED_ADDRESSES = [
        {"address": "CNudZYFgpbT26fidsiNrWfHeGTBMMeVWqruZXsEkcUPc", "name": "DNF小号"}
    ]
    
    @classmethod
    def load_addresses_from_file(cls, filepath):
        """从文件中加载地址列表"""
        if not os.path.exists(filepath):
            logger.error(f"地址文件不存在: {filepath}")
            return
        
        try:
            # 清空当前列表
            cls.MONITORED_ADDRESSES = []
            
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                    
                # 尝试分割备注和地址
                parts = line.split()
                if len(parts) >= 2:
                    # 最后一部分是地址，前面所有内容合并为备注
                    address = parts[-1]
                    name = ' '.join(parts[:-1])
                    
                    # 验证地址格式(简单检查)
                    if len(address) >= 32:
                        cls.MONITORED_ADDRESSES.append({
                            "address": address,
                            "name": name
                        })
                        logger.info(f"已添加监控地址: {name} → {address}")
            
            logger.info(f"成功从文件导入 {len(cls.MONITORED_ADDRESSES)} 个监控地址")
        except Exception as e:
            logger.error(f"加载地址文件错误: {str(e)}")
            logger.debug(traceback.format_exc())

    @classmethod
    def print_all_addresses(cls):
        """打印所有监控地址"""
        logger.info("=" * 50)
        logger.info("所有监控地址:")
        logger.info("=" * 50)
        for idx, addr in enumerate(cls.MONITORED_ADDRESSES, 1):
            logger.info(f"{idx:3}. {addr['name']} → {addr['address']}")
        logger.info("=" * 50)


class SwapMonitor:
    def __init__(self):
        self.client = Client(Config.RPC_ENDPOINT)
        self.token_cache = {}
        self.engine = create_engine(Config.DB_URL)
        self.stats = defaultdict(int)
        self.last_cache_refresh = 0

    def refresh_token_cache(self):
        """刷新代币缓存"""
        try:
            # 记录缓存更新开始时间
            start_time = time.time()

            response = requests.get(Config.JUPITER_TOKEN_API, timeout=10)
            if response.status_code == 200:
                tokens = response.json()

                # 比较新旧缓存
                new_tokens = {token["address"]: token for token in tokens}
                added_tokens = set(new_tokens.keys()) - \
                    set(self.token_cache.keys())
                updated_tokens = set()

                for addr, token in new_tokens.items():
                    if addr in self.token_cache:
                        old_token = self.token_cache[addr]
                        if token != old_token:
                            updated_tokens.add(addr)

                # 更新缓存
                self.token_cache = new_tokens

                # 记录更新统计
                update_time = time.time() - start_time
                logger.info(
                    f"代币缓存已更新 ({update_time:.2f}秒):\n"
                    f"  总代币数: {len(self.token_cache)}\n"
                    f"  新增代币: {len(added_tokens)}\n"
                    f"  更新代币: {len(updated_tokens)}"
                )

                # 保存到文件
                with open('token_cache.txt', 'w', encoding='utf-8') as f:
                    for token in sorted(self.token_cache.values(), key=lambda x: x.get('symbol', '')):
                        f.write(
                            f"{token['address']}: {token.get('name', 'Unknown')} ({token.get('symbol', 'UNKNOWN')})\n")

                # 更新时间戳
                self.last_cache_refresh = time.time()

        except Exception as e:
            logger.error(f"更新代币缓存失败: {str(e)}")
            logger.debug(traceback.format_exc())

    def get_token_info(self, address: str) -> dict:
        """获取代币信息"""
        token = self.token_cache.get(address)
        if token:
            return {
                "address": address,
                "name": token.get("name", "Unknown"),
                "symbol": token.get("symbol", "Unknown"),
                "decimals": token.get("decimals", 9)
            }
        return {
            "address": address,
            "name": "Unknown",
            "symbol": "Unknown",
            "decimals": 9
        }

    def create_tables(self):
        """创建数据库表"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS swaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot INTEGER,
                    program_id TEXT,
                    swap_amount REAL,
                    input_token_address TEXT,
                    input_token_symbol TEXT,
                    output_token_address TEXT,
                    output_token_symbol TEXT,
                    timestamp REAL,
                    is_monitored_address BOOLEAN DEFAULT 0,
                    monitored_address_name TEXT
                )
            """))
            conn.commit()

    def save_swap(self, swap_data: dict):
        """保存交易记录"""
        df = pd.DataFrame([swap_data])
        df.to_sql('swaps', self.engine, if_exists='append', index=False)
        logger.info(
            f"保存交易: {swap_data['swap_amount']:.2f} SOL - "
            f"{swap_data['input_token_symbol']} -> {swap_data['output_token_symbol']}"
        )

    def find_token_transfers(self, tx, account_keys) -> List[dict]:
        """分析代币转账"""
        if not (tx.meta and tx.meta.log_messages):
            return []

        tokens = []
        try:
            # 检查代币余额变化
            if hasattr(tx.meta, 'post_token_balances') and tx.meta.post_token_balances:
                for balance in tx.meta.post_token_balances:
                    if hasattr(balance, 'mint'):
                        mint = str(balance.mint)
                        token_info = self.get_token_info(mint)
                        if token_info["name"] != "Unknown":
                            tokens.append(token_info)
                            logger.debug(
                                f"Found token from balances: {token_info['name']} ({token_info['symbol']})")

            # 分析程序调用
            for log in tx.meta.log_messages:
                if any(keyword in log for keyword in [
                    "Instruction: Transfer",
                    "Instruction: Swap",
                    "Program TokenkegQfe",
                    "Program log: Instruction: Swap",
                    "Program log: Swap"
                ]):
                    if hasattr(tx.transaction, 'message') and hasattr(tx.transaction.message, 'instructions'):
                        for instruction in tx.transaction.message.instructions:
                            if hasattr(instruction, 'accounts'):
                                for account_idx in instruction.accounts:
                                    if account_idx < len(account_keys):
                                        account = str(
                                            account_keys[account_idx])
                                        token_info = self.get_token_info(
                                            account)
                                        if token_info["name"] != "Unknown":
                                            if token_info not in tokens:  # 避免重复
                                                tokens.append(token_info)
                                                logger.debug(
                                                    f"Found token from instruction: {token_info['name']} ({token_info['symbol']})")

            # 检查是否找到足够的代币
            if len(tokens) >= 2:
                #logger.info(f"Found {len(tokens)} tokens in transaction")
                return tokens
            else:
                #logger.debug(f"Not enough tokens found: {len(tokens)}")
                return []

        except Exception as e:
            logger.error(f"分析代币转账错误: {str(e)}")
            logger.debug(traceback.format_exc())
            return []

    async def monitor_transactions(self):
        """监控交易"""
        last_processed_slot = None

        while True:
            try:
                current_slot = self.client.get_slot().value
                if last_processed_slot is None:
                    start_slot = current_slot - 5
                else:
                    start_slot = last_processed_slot + 1

                end_slot = min(current_slot, start_slot + 10)

                for slot in range(start_slot, end_slot + 1):
                    try:
                        block = self.client.get_block(
                            slot,
                            max_supported_transaction_version=0
                        ).value

                        if not block or not hasattr(block, 'transactions'):
                            continue

                        # 只在发现重要事件时输出日志
                        for tx_index, tx in enumerate(block.transactions):
                            try:
                                if not (tx.transaction and tx.transaction.message):
                                    continue

                                account_keys = [
                                    str(key) for key in tx.transaction.message.account_keys]
                                
                                # 检查是否与监控地址相关
                                is_monitored_address_involved = False
                                monitored_address_name = None
                                for monitored_addr in Config.MONITORED_ADDRESSES:
                                    if monitored_addr["address"] in account_keys:
                                        is_monitored_address_involved = True
                                        monitored_address_name = monitored_addr["name"]
                                        break

                                is_dex = (
                                    any(id in account_keys for id in Config.JUPITER_PROGRAM_IDS) or
                                    Config.RAYDIUM_PROGRAM_ID in account_keys
                                )

                                # 如果不是与DEX相关的交易，而且也不涉及监控地址，则跳过
                                if not (is_dex or is_monitored_address_involved):
                                    continue

                                if tx.meta and tx.meta.post_balances and tx.meta.pre_balances:
                                    sol_change = max(
                                        abs((post - pre) / 1e9)
                                        for pre, post in zip(tx.meta.pre_balances, tx.meta.post_balances)
                                    )

                                    # 如果是监控地址的交易，忽略最小金额限制
                                    if is_monitored_address_involved or sol_change > Config.MIN_SWAP_AMOUNT:
                                        if is_monitored_address_involved:
                                            logger.info(f"监控地址交易 [{monitored_address_name}]: {sol_change:.2f} SOL")
                                            
                                        else:
                                            logger.info(f"大额交易: {sol_change:.2f} SOL")
                                        
                                        tokens = self.find_token_transfers(
                                            tx, account_keys)
                                            
                                        if tokens and len(tokens) >= 2:
                                            input_symbol = tokens[0]["symbol"]
                                            output_symbol = tokens[-1]["symbol"]
                                            
                                            logger.info(f"找到代币对: {input_symbol} -> {output_symbol}")
                                            
                                            # SOL地址和USDC地址
                                            sol_symbols = ["SOL", "WSOL"]
                                            usdc_symbols = ["USDC", "USDT"]
                                            
                                            # 如果是监控地址的交易，不过滤SOL和稳定币
                                            if not is_monitored_address_involved:
                                                # 如果输入和输出代币都是SOL或稳定币，则跳过
                                                is_sol_stable_swap = (
                                                    (input_symbol in sol_symbols and output_symbol in sol_symbols) or
                                                    (input_symbol in usdc_symbols and output_symbol in usdc_symbols) or
                                                    (input_symbol in sol_symbols and output_symbol in usdc_symbols) or
                                                    (input_symbol in usdc_symbols and output_symbol in sol_symbols)
                                                )
                                                
                                                if is_sol_stable_swap:
                                                    logger.info(
                                                        f"跳过SOL/稳定币交易: {sol_change:.2f} SOL - "
                                                        f"{input_symbol} -> {output_symbol}"
                                                    )
                                                    continue
                                            else:
                                                logger.info(f"监控地址交易 [{monitored_address_name}]: {input_symbol} -> {output_symbol}")
                                            
                                            swap_data = {
                                                "slot": slot,
                                                "program_id": str(account_keys[0]),
                                                "swap_amount": sol_change,
                                                "input_token_address": tokens[0]["address"],
                                                "input_token_symbol": input_symbol,
                                                "output_token_address": tokens[-1]["address"],
                                                "output_token_symbol": output_symbol,
                                                "timestamp": time.time(),
                                                "is_monitored_address": is_monitored_address_involved,
                                                "monitored_address_name": monitored_address_name
                                            }
                                            
                                            try:
                                                self.save_swap(swap_data)
                                                if is_monitored_address_involved:
                                                    logger.info(
                                                        f"监控地址交易已保存 [{monitored_address_name}]: {input_symbol} -> {output_symbol}"
                                                    )
                                                else:
                                                    logger.info(
                                                        f"代币交换已保存: {input_symbol} -> {output_symbol}"
                                                    )
                                            except Exception as save_error:
                                                logger.error(f"保存交换记录错误: {str(save_error)}")

                            except Exception as tx_error:
                                logger.error(f"交易处理错误: {str(tx_error)}")

                        last_processed_slot = slot

                    except Exception as block_error:
                        logger.error(f"区块处理错误: {str(block_error)}")
                        continue

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"监控错误: {str(e)}")
                await asyncio.sleep(5)

    async def run(self):
        """运行监控"""
        logger.info("启动 Solana 交易监控...")
        self.create_tables()
        self.refresh_token_cache()

        try:
            await self.monitor_transactions()
        except KeyboardInterrupt:
            logger.info("监控已停止")
        except Exception as e:
            logger.error(f"运行错误: {traceback.format_exc()}")


async def main():
    monitor = SwapMonitor()
    
    # 加载地址文件
    address_file = "213123.txt"
    current_dir = os.path.abspath(os.path.dirname(__file__))
    address_file_path = os.path.join(current_dir, address_file)
    
    logger.info(f"尝试从 {address_file_path} 加载地址文件")
    
    if os.path.exists(address_file_path):
        logger.info(f"找到地址文件: {address_file_path}")
        Config.load_addresses_from_file(address_file_path)
        
        # 打印所有监控地址
        if Config.MONITORED_ADDRESSES:
            Config.print_all_addresses()
        else:
            logger.warning(f"地址文件存在但未加载任何地址，请检查文件格式")
    else:
        logger.warning(f"未找到地址文件: {address_file_path}")
        
        # 创建示例文件
        try:
            with open(address_file_path, 'w', encoding='utf-8') as f:
                f.write("# 格式: 备注 地址\n")
                f.write("# 一行一个地址，'#'开头的行会被忽略\n")
                f.write("DNF小号 CNudZYFgpbT26fidsiNrWfHeGTBMMeVWqruZXsEkcUPc\n")
            logger.info(f"已创建示例地址文件: {address_file_path}")
        except Exception as e:
            logger.error(f"创建示例文件失败: {str(e)}")
    
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常: {str(e)}")
        logger.debug(traceback.format_exc())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已终止")