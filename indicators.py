"""
TradingView 指标 Python 实现
- Smart Money Concepts [LuxAlgo]
- Support Resistance Channels [LonesomeTheBlue]
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import Enum


# ============================================================
# 数据结构
# ============================================================

class Direction(Enum):
    BULLISH = 1
    BEARISH = -1


@dataclass
class SwingPivot:
    """摆动枢轴点"""
    price: float
    bar_index: int
    timestamp: int  # ms
    level_type: str  # 'HH', 'HL', 'LH', 'LL'

    @property
    def is_high(self) -> bool:
        return self.level_type in ('HH', 'LH')

    @property
    def is_low(self) -> bool:
        return self.level_type in ('HL', 'LL')


@dataclass
class OrderBlock:
    """订单块"""
    bar_high: float
    bar_low: float
    bar_time: int  # ms
    bias: Direction  # BULLISH = 做多参考, BEARISH = 做空参考
    is_broken: bool = False


@dataclass
class Signal:
    """交易信号"""
    symbol: str
    signal_name: str          # 信号英文名，如 'Bullish Internal OB Breakout'
    signal_label: str         # 信号中文名
    direction: str            # 'long' / 'short' / 'neutral'
    price: float
    timestamp: int            # ms


# ============================================================
# 通用工具函数
# ============================================================

def find_pivots(high: np.ndarray, low: np.ndarray, period: int) -> Tuple[List[SwingPivot], List[SwingPivot]]:
    """
    检测枢轴高低点
    返回 (pivot_highs, pivot_lows)
    """
    pivot_highs = []
    pivot_lows = []
    n = len(high)

    for i in range(period, n - period):
        # Pivot High: 当前 high 是周围 period 根K线的最高点
        is_ph = True
        for j in range(i - period, i + period + 1):
            if j == i:
                continue
            if high[j] >= high[i]:
                is_ph = False
                break

        if is_ph:
            pivot_highs.append(SwingPivot(
                price=float(high[i]),
                bar_index=i,
                timestamp=0,  # 后续填入
                level_type='HH'  # 初始标记，后续根据上下文修正
            ))

        # Pivot Low: 当前 low 是周围 period 根K线的最低点
        is_pl = True
        for j in range(i - period, i + period + 1):
            if j == i:
                continue
            if low[j] <= low[i]:
                is_pl = False
                break

        if is_pl:
            pivot_lows.append(SwingPivot(
                price=float(low[i]),
                bar_index=i,
                timestamp=0,
                level_type='LL'
            ))

    return pivot_highs, pivot_lows


def classify_pivots(pivots: List[SwingPivot]) -> List[SwingPivot]:
    """为枢轴点标记 HH/HL/LH/LL"""
    if len(pivots) < 2:
        return pivots

    for i in range(1, len(pivots)):
        prev = pivots[i - 1]
        curr = pivots[i]

        if curr.price > prev.price:
            if curr.is_high:
                curr.level_type = 'HH'
            else:
                curr.level_type = 'HL'
        else:
            if curr.is_high:
                curr.level_type = 'LH'
            else:
                curr.level_type = 'LL'

    return pivots


# ============================================================
# Smart Money Concepts 核心逻辑
# ============================================================

class SmartMoneyConcepts:
    """
    LuxAlgo Smart Money Concepts 指标实现

    检测信号：
    - Swing BOS / CHoCH
    - Internal BOS / CHoCH
    - Internal Order Block breakout
    - Swing Order Block breakout
    """

    def __init__(self, swing_length: int = 50, internal_length: int = 5):
        self.swing_length = swing_length
        self.internal_length = internal_length

        # 状态跟踪
        self.swing_trend: Direction = Direction.BULLISH
        self.internal_trend: Direction = Direction.BULLISH

        self.last_swing_high: Optional[SwingPivot] = None
        self.last_swing_low: Optional[SwingPivot] = None
        self.last_internal_high: Optional[SwingPivot] = None
        self.last_internal_low: Optional[SwingPivot] = None

        self.swing_obs: List[OrderBlock] = []
        self.internal_obs: List[OrderBlock] = []

        self._prev_swing_high_crossed = False
        self._prev_swing_low_crossed = False
        self._prev_internal_high_crossed = False
        self._prev_internal_low_crossed = False

    def analyze(self, df: pd.DataFrame) -> List[Signal]:
        """
        分析K线数据，返回检测到的信号列表

        Args:
            df: DataFrame with columns [timestamp, open, high, low, close, volume]
        """
        signals = []
        n = len(df)

        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        timestamp = df['timestamp'].values.astype(np.int64)

        # ── 1. 检测 Swing 枢轴点 ──
        swing_highs, swing_lows = find_pivots(high, low, self.swing_length)

        # ── 2. 检测 Internal 枢轴点 ──
        internal_highs, internal_lows = find_pivots(high, low, self.internal_length)

        # ── 3. 处理 Swing 结构 ──
        signals += self._process_structure(
            high, low, close, timestamp, n,
            swing_highs, swing_lows,
            is_internal=False
        )

        # ── 4. 处理 Internal 结构 ──
        signals += self._process_structure(
            high, low, close, timestamp, n,
            internal_highs, internal_lows,
            is_internal=True
        )

        # ── 5. 检测 Order Block 突破 ──
        signals += self._check_order_block_breakouts(
            high, low, close, timestamp, n
        )

        return signals

    def _process_structure(
        self, high: np.ndarray, low: np.ndarray, close: np.ndarray,
        timestamp: np.ndarray, n: int,
        pivot_highs: List[SwingPivot], pivot_lows: List[SwingPivot],
        is_internal: bool
    ) -> List[Signal]:
        """处理结构突破检测"""
        signals = []

        # 获取最近的枢轴高点和低点
        recent_high = pivot_highs[-1] if pivot_highs else None
        recent_low = pivot_lows[-1] if pivot_lows else None

        if recent_high is None or recent_low is None:
            return signals

        prefix = 'Swing' if not is_internal else 'Internal'
        trend = self.swing_trend if not is_internal else self.internal_trend

        # 检查最近5根已完成K线（股票代币波动低，单根K线难触发）
        check_start = max(0, n - 6)

        for i in range(check_start, n):
            # 看涨突破：收盘价突破前一个摆动高点
            if close[i] > recent_high.price:
                tag = 'CHoCH' if trend == Direction.BEARISH else 'BOS'
                signals.append(Signal(
                    symbol='',
                    signal_name=f'Bullish {tag}',
                    signal_label=f'看涨 {tag}（{prefix}结构）',
                    direction='long',
                    price=float(close[i]),
                    timestamp=int(timestamp[i])
                ))

                # 创建看涨 Order Block
                self._create_order_block(
                    high, low, timestamp, recent_high.bar_index,
                    Direction.BULLISH, is_internal
                )

                if not is_internal:
                    self.swing_trend = Direction.BULLISH
                else:
                    self.internal_trend = Direction.BULLISH
                break  # 只取最新信号

            # 看跌突破：收盘价跌破前一个摆动低点
            if close[i] < recent_low.price:
                tag = 'CHoCH' if trend == Direction.BULLISH else 'BOS'
                signals.append(Signal(
                    symbol='',
                    signal_name=f'Bearish {tag}',
                    signal_label=f'看跌 {tag}（{prefix}结构）',
                    direction='short',
                    price=float(close[i]),
                    timestamp=int(timestamp[i])
                ))

                # 创建看跌 Order Block
                self._create_order_block(
                    high, low, timestamp, recent_low.bar_index,
                    Direction.BEARISH, is_internal
                )

                if not is_internal:
                    self.swing_trend = Direction.BEARISH
                else:
                    self.internal_trend = Direction.BEARISH
                break

        return signals

    def _create_order_block(
        self, high: np.ndarray, low: np.ndarray,
        timestamp: np.ndarray, pivot_bar_index: int,
        bias: Direction, is_internal: bool
    ):
        """在枢轴点位置创建 Order Block"""
        if pivot_bar_index < 0 or pivot_bar_index >= len(high):
            return

        ob = OrderBlock(
            bar_high=float(high[pivot_bar_index]),
            bar_low=float(low[pivot_bar_index]),
            bar_time=int(timestamp[pivot_bar_index]),
            bias=bias,
        )

        ob_list = self.internal_obs if is_internal else self.swing_obs
        ob_list.append(ob)

        # 限制数量
        max_obs = 20
        if len(ob_list) > max_obs:
            ob_list.pop(0)

    def _check_order_block_breakouts(
        self, high: np.ndarray, low: np.ndarray,
        close: np.ndarray, timestamp: np.ndarray, n: int
    ) -> List[Signal]:
        """检测 Order Block 被突破"""
        signals = []
        # 检查最近5根K线
        check_start = max(0, n - 6)
        start = check_start

        # 检查 Internal OB
        for ob in self.internal_obs[:]:
            if ob.is_broken:
                continue

            for i in range(start, n):
                prefix = 'Internal'

                # 看跌 OB：价格突破到 OB 上方 → 看跌 OB 突破
                if ob.bias == Direction.BEARISH:
                    if high[i] > ob.bar_high:
                        ob.is_broken = True
                        signals.append(Signal(
                            symbol='',
                            signal_name='Bearish Internal OB Breakout',
                            signal_label='🔴 红色OK块 · 内部订单块突破',
                            direction='short',
                            price=float(high[i]),
                            timestamp=int(timestamp[i])
                        ))
                        break

                # 看涨 OB：价格跌破 OB 下方 → 看涨 OB 突破
                elif ob.bias == Direction.BULLISH:
                    if low[i] < ob.bar_low:
                        ob.is_broken = True
                        signals.append(Signal(
                            symbol='',
                            signal_name='Bullish Internal OB Breakout',
                            signal_label='🔵 蓝色OK块 · 内部订单块突破',
                            direction='long',
                            price=float(low[i]),
                            timestamp=int(timestamp[i])
                        ))
                        break

        # 检查 Swing OB
        for ob in self.swing_obs[:]:
            if ob.is_broken:
                continue

            for i in range(start, n):
                prefix = 'Swing'

                if ob.bias == Direction.BEARISH:
                    if high[i] > ob.bar_high:
                        ob.is_broken = True
                        signals.append(Signal(
                            symbol='',
                            signal_name='Bearish Swing OB Breakout',
                            signal_label='🔴 红色OK块 · Swing订单块突破',
                            direction='short',
                            price=float(high[i]),
                            timestamp=int(timestamp[i])
                        ))
                        break

                elif ob.bias == Direction.BULLISH:
                    if low[i] < ob.bar_low:
                        ob.is_broken = True
                        signals.append(Signal(
                            symbol='',
                            signal_name='Bullish Swing OB Breakout',
                            signal_label='🔵 蓝色OK块 · Swing订单块突破',
                            direction='long',
                            price=float(low[i]),
                            timestamp=int(timestamp[i])
                        ))
                        break

        return signals


# ============================================================
# Support Resistance Channels 核心逻辑
# ============================================================

class SupportResistanceChannels:
    """
    LonesomeTheBlue Support Resistance Channels 指标实现

    检测信号：
    - Support Broken（支撑跌破）
    - Resistance Broken（压力突破）
    """

    def __init__(
        self, pivot_period: int = 10, channel_width_pct: float = 5.0,
        min_strength: int = 1, max_sr: int = 6, loopback: int = 290
    ):
        self.pivot_period = pivot_period
        self.channel_width_pct = channel_width_pct
        self.min_strength = min_strength
        self.max_sr = max_sr
        self.loopback = loopback

        # 状态
        self._prev_channels: List[Tuple[float, float]] = []  # (top, bottom)
        self._prev_close: Optional[float] = None

    def analyze(self, df: pd.DataFrame) -> List[Signal]:
        """
        分析K线数据，返回支撑/压力突破信号
        """
        signals = []
        n = len(df)

        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        timestamp = df['timestamp'].values.astype(np.int64)
        bar_index = np.arange(n)

        # ── 1. 检测枢轴点 ──
        pivot_highs, pivot_lows = find_pivots(high, low, self.pivot_period)

        # ── 2. 收集所有枢轴价格 ──
        pivot_vals = []
        for ph in pivot_highs:
            if ph.bar_index > n - self.loopback:
                pivot_vals.append(ph.price)
        for pl in pivot_lows:
            if pl.bar_index > n - self.loopback:
                pivot_vals.append(pl.price)

        if len(pivot_vals) < 2:
            return signals

        pivot_vals = np.array(pivot_vals)

        # ── 3. 计算通道宽度 ──
        lookback_start = max(0, n - 300)
        price_range = float(np.max(high[lookback_start:]) - np.min(low[lookback_start:]))
        cwidth = price_range * self.channel_width_pct / 100.0

        if cwidth <= 0:
            return signals

        # ── 4. 构建支撑/压力通道 ──
        channels = self._build_channels(pivot_vals, cwidth)

        # ── 5. 检测突破 ──
        if self._prev_close is not None and len(channels) > 0:
            current_close = float(close[-1])
            in_channel = False

            for top, bottom in channels:
                if bottom <= current_close <= top:
                    in_channel = True
                    break

            if not in_channel:
                for top, bottom in channels:
                    # 压力突破：之前收盘在通道内/下方，现在突破顶部
                    if self._prev_close <= top and current_close > top:
                        signals.append(Signal(
                            symbol='',
                            signal_name='Resistance Broken',
                            signal_label='🔼 压力位突破',
                            direction='neutral',
                            price=current_close,
                            timestamp=int(timestamp[-1])
                        ))
                        break

                    # 支撑跌破：之前收盘在通道内/上方，现在跌破底部
                    if self._prev_close >= bottom and current_close < bottom:
                        signals.append(Signal(
                            symbol='',
                            signal_name='Support Broken',
                            signal_label='🔽 支撑位跌破',
                            direction='neutral',
                            price=current_close,
                            timestamp=int(timestamp[-1])
                        ))
                        break

        self._prev_close = float(close[-1])
        self._prev_channels = channels

        return signals

    def _build_channels(self, pivot_vals: np.ndarray, cwidth: float) -> List[Tuple[float, float]]:
        """
        构建支撑/压力通道
        返回 [(top, bottom), ...] 按强度排序
        """
        n = len(pivot_vals)
        sr_list = []  # [(top, bottom, strength)]

        for i in range(n):
            lo = hi = pivot_vals[i]
            strength = 0

            for j in range(n):
                cpp = pivot_vals[j]
                if cpp <= hi and (hi - cpp) <= cwidth:
                    lo = min(lo, cpp)
                    strength += 20
                elif cpp >= lo and (cpp - lo) <= cwidth:
                    hi = max(hi, cpp)
                    strength += 20

            if strength >= self.min_strength * 20:
                sr_list.append((hi, lo, strength))

        # 去重：合并重叠的通道，保留最强的
        sr_list.sort(key=lambda x: -x[2])  # 按强度降序

        result = []
        used = set()

        for i, (hi, lo, strength) in enumerate(sr_list):
            if i in used:
                continue
            result.append((hi, lo))
            if len(result) >= self.max_sr:
                break

            # 标记重叠的通道为已使用
            for j in range(i + 1, len(sr_list)):
                if j in used:
                    continue
                j_hi, j_lo, _ = sr_list[j]
                if (j_hi <= hi and j_hi >= lo) or (j_lo <= hi and j_lo >= lo):
                    used.add(j)

        return result
