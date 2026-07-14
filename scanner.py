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

# Binance API 镜像节点（GitHub Actions 可能被主站封IP，多节点自动切换）
BINANCE_ENDPOINTS = [
    # (期货API, 现货API, 标签)
    ('https://fapi.binance.com/fapi/v1/klines', 'https://api.binance.com/api/v3/klines', 'Binance主站'),
    ('https://api1.binance.com/api/v3/klines', 'https://api1.binance.com/api/v3/klines', 'Binance-API1'),
    ('https://api2.binance.com/api/v3/klines', 'https://api2.binance.com/api/v3/klines', 'Binance-API2'),
    ('https://api3.binance.com/api/v3/klines', 'https://api3.binance.com/api/v3/klines', 'Binance-API3'),
    ('https://api.binance.us/api/v3/klines', 'https://api.binance.us/api/v3/klines', 'Binance-US'),
]

# Cloudflare Worker 代理（解决 GitHub Actions IP 被封问题）
WORKER_PROXY = 'https://tradingview-feishu.hongji1142317442.workers.dev/binance-api'


BYBIT_URL = 'https://api.bybit.com/v5/market/kline'


def _parse_klines(data: list, source_label: str) -> pd.DataFrame:
    """解析 Binance klines 格式数据"""
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


def _fetch_via_proxy(symbol: str, url: str, interval: str, limit: int) -> pd.DataFrame:
    """通过 Cloudflare Worker 代理请求 Binance API"""
    try:
        full_url = WORKER_PROXY + '?url=' + requests.utils.quote(
            url + '?' + requests.utils.urlencode({
                'symbol': symbol, 'interval': interval, 'limit': limit
            })
        )
        resp = requests.get(full_url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                df = _parse_klines(data, f'Worker代理')
                print(f"  ✅ Worker代理: {symbol} {len(df)} 根K线")
                return df
    except Exception:
        pass
    return None


def _fetch_binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """尝试所有 Binance 节点"""
    for futures_url, spot_url, label in BINANCE_ENDPOINTS:
        for url in [futures_url, spot_url]:
            try:
                resp = requests.get(url, params={
                    'symbol': symbol, 'interval': interval, 'limit': limit
                }, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        print(f"  ✅ {label}: {symbol} {len(data)} 根K线")
                        return _parse_klines(data, label)
                elif resp.status_code == 400:
                    break  # 交易对不存在，换节点
            except Exception:
                continue
    return None


def _fetch_bybit(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """从 Bybit 获取 K 线（备用数据源）"""
    # Bybit 使用不同的交易对名称
    bybit_interval = {'1m': '1', '3m': '3', '5m': '5', '15m': '15',
                      '30m': '30', '1h': '60', '4h': '240', '1d': 'D'}.get(interval, '15')
    try:
        resp = requests.get(BYBIT_URL, params={
            'category': 'linear',
            'symbol': symbol,
            'interval': bybit_interval,
            'limit': limit,
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('retCode') == 0 and data['result'].get('list'):
                klines = data['result']['list']
                klines.reverse()  # Bybit 返回最新的在前
                rows = []
                for k in klines:
                    rows.append({
                        'timestamp': int(k[0]),
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5]),
                    })
                df = pd.DataFrame(rows)
                print(f"  ✅ Bybit: {symbol} {len(df)} 根K线")
                return df
    except Exception:
        pass
    return None


def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 300) -> pd.DataFrame:
    """
    多数据源获取 OHLCV 数据
    优先级: Binance 多节点 → Bybit
    """
    interval = TIMEFRAME_MAP.get(timeframe, '15m')

    # 1. 尝试 Binance 所有节点（直连）
    df = _fetch_binance(symbol, interval, limit)
    if df is not None:
        return df

    # 2. 通过 Cloudflare Worker 代理（解决 GitHub Actions IP 被封）
    df = _fetch_via_proxy(symbol, 'https://fapi.binance.com/fapi/v1/klines', interval, limit)
    if df is not None:
        return df
    df = _fetch_via_proxy(symbol, 'https://api.binance.com/api/v3/klines', interval, limit)
    if df is not None:
        return df

    # 3. 备用：Bybit（仅主流币有）
    df = _fetch_bybit(symbol, interval, limit)
    if df is not None:
        return df

    print(f"  ❌ {symbol}: 所有数据源均获取失败")
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

    ts = datetime.fromtimestamp(int(df['timestamp'].iloc[-1]) / 1000, tz=timezone.utc)
    print(f"  最新K线: {ts.strftime('%Y-%m-%d %H:%M UTC')} | 收盘: {df['close'].iloc[-1]:.4f}")

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
