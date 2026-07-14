#!/usr/bin/env python3
"""
交易信号扫描器
- 从 Binance 拉取 15 分钟 K 线数据
- 运行 Smart Money Concepts + Support Resistance Channels 指标
- 检测到信号后推送到飞书群
"""

import sys
import io
import time
from datetime import datetime, timezone
from typing import Dict, List

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import requests
import pandas as pd

from config import (
    SYMBOLS, TIMEFRAME, LOOKBACK_BARS, FEISHU_WEBHOOK_URL,
    SWING_LENGTH, INTERNAL_LENGTH,
    PIVOT_PERIOD, CHANNEL_WIDTH_PCT, MIN_STRENGTH, MAX_SR, LOOPBACK,
)
from indicators import SmartMoneyConcepts, SupportResistanceChannels, Signal, Direction
from feishu import send_signal


# ============================================================
# Binance 数据获取
# ============================================================

# K线周期映射 (Pine Script → Binance API)
TIMEFRAME_MAP = {
    '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m',
    '30m': '30m', '1h': '1h', '2h': '2h', '4h': '4h',
    '1d': '1d', '1w': '1w',
}

BINANCE_FUTURES_URL = 'https://fapi.binance.com/fapi/v1/klines'
BINANCE_SPOT_URL = 'https://api.binance.com/api/v3/klines'


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 300) -> pd.DataFrame:
    """
    从 Binance 获取 OHLCV 数据（优先期货，fallback 现货）

    Args:
        symbol: 交易对（如 ETHUSDT）
        timeframe: K线周期
        limit: K线数量
    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume]
    """
    interval = TIMEFRAME_MAP.get(timeframe, '15m')

    # 先尝试期货 API
    for url, market_type in [(BINANCE_FUTURES_URL, '期货'), (BINANCE_SPOT_URL, '现货')]:
        try:
            resp = requests.get(url, params={
                'symbol': symbol,
                'interval': interval,
                'limit': limit,
            }, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    print(f"  ✅ {market_type} {symbol}: {len(data)} 根K线")
                    df = pd.DataFrame(data, columns=[
                        'timestamp', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                        'taker_buy_quote', 'ignore'
                    ])
                    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                    df['timestamp'] = df['timestamp'].astype('int64')
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    return df
                else:
                    # 可能是错误响应，尝试下一个数据源
                    continue
            elif resp.status_code == 400:
                # 交易对不存在，跳过
                continue
        except Exception as e:
            continue

    print(f"  ❌ {symbol}: 期货和现货均获取失败")
    return None


# ============================================================
# 主扫描逻辑
# ============================================================

def scan_symbol(symbol: str) -> List[Signal]:
    """
    扫描单个交易对的所有信号

    Args:
        symbol: 交易对名称
    Returns:
        检测到的信号列表
    """
    print(f"\n{'='*50}")
    print(f"🔍 扫描 {symbol} ...")

    # 1. 获取数据
    df = fetch_ohlcv(symbol, TIMEFRAME, LOOKBACK_BARS)
    if df is None or len(df) < 100:
        print(f"  ⚠️ {symbol} 数据不足，跳过")
        return []

    print(f"  获取到 {len(df)} 根K线 | 最新: {datetime.fromtimestamp(df['timestamp'].iloc[-1]/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | 收盘: {df['close'].iloc[-1]:.4f}")

    # 2. 初始化指标
    smart_money = SmartMoneyConcepts(
        swing_length=SWING_LENGTH,
        internal_length=INTERNAL_LENGTH
    )
    sr_channels = SupportResistanceChannels(
        pivot_period=PIVOT_PERIOD,
        channel_width_pct=CHANNEL_WIDTH_PCT,
        min_strength=MIN_STRENGTH,
        max_sr=MAX_SR,
        loopback=LOOPBACK
    )

    # 3. 运行指标
    sm_signals = smart_money.analyze(df)
    sr_signals = sr_channels.analyze(df)

    # 4. 合并信号，填入 symbol
    all_signals = sm_signals + sr_signals
    for s in all_signals:
        s.symbol = symbol

    return all_signals


def main():
    """主函数：扫描所有交易对，推送信号"""
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  交易信号扫描器 v1.0                      ║")
    print(f"║  周期: {TIMEFRAME} | 交易对: {len(SYMBOLS)}个       ║")
    print(f"║  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}          ║")
    print(f"╚══════════════════════════════════════════╝")

    # 诊断：检查飞书 Webhook 是否配置
    if FEISHU_WEBHOOK_URL:
        masked = FEISHU_WEBHOOK_URL[:30] + '...' + FEISHU_WEBHOOK_URL[-10:]
        print(f"📡 飞书 Webhook: {masked}")
    else:
        print(f"⚠️  飞书 Webhook 未配置！请在 GitHub Secrets 中设置 FEISHU_WEBHOOK_URL")

    total_signals = []

    for symbol in SYMBOLS:
        try:
            signals = scan_symbol(symbol)
            total_signals.extend(signals)
        except Exception as e:
            print(f"  ❌ {symbol} 扫描异常: {e}")
            continue

    # ── 汇总 ──
    print(f"\n{'='*50}")
    print(f"📊 扫描完成: {len(total_signals)} 个信号")

    if total_signals:
        print(f"\n  🚀 推送信号到飞书...")
        success_count = 0
        for sig in total_signals:
            ts = datetime.fromtimestamp(sig.timestamp / 1000, tz=timezone.utc)
            ts_str = ts.strftime('%Y-%m-%d %H:%M UTC')

            ok = send_signal(
                symbol=sig.symbol,
                signal_label=sig.signal_label,
                direction=sig.direction,
                price=sig.price,
                timestamp_str=ts_str,
                interval="15"
            )
            if ok:
                success_count += 1

        print(f"\n  ✅ 成功推送: {success_count}/{len(total_signals)}")
    else:
        print(f"\n  💤 本轮无信号")

    print(f"\n  下次扫描: 15分钟后")


# ============================================================
# 入口
# ============================================================

if __name__ == '__main__':
    main()
