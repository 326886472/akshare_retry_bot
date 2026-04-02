import csv
import json
import math
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


# =========================
# Models
# =========================

@dataclass
class Tick:
    ts: datetime
    price: float


@dataclass
class Trade:
    symbol: str
    side: str
    qty: int
    price: float
    ts: datetime
    reason: str = ""


@dataclass
class Position:
    symbol: str
    qty: int = 0
    avg_cost: float = 0.0


# =========================
# Broker Abstractions
# =========================

class BrokerBase:
    def place_market_order(self, symbol: str, side: str, qty: int, market_price: float, reason: str = "") -> Trade:
        raise NotImplementedError

    def equity(self, last_price: float) -> float:
        raise NotImplementedError

    def current_position(self) -> Position:
        raise NotImplementedError


class PaperBroker(BrokerBase):
    def __init__(self, starting_cash: float = 100000.0):
        self.cash = starting_cash
        self.position = Position(symbol="DEMO")
        self.trades: List[Trade] = []

    def current_position(self) -> Position:
        return self.position

    def place_market_order(self, symbol: str, side: str, qty: int, market_price: float, reason: str = "") -> Trade:
        if qty <= 0:
            raise ValueError("qty must be positive")

        if self.position.symbol != symbol:
            self.position.symbol = symbol

        if side == "BUY":
            cost = qty * market_price
            if cost > self.cash:
                max_affordable = int(self.cash // market_price)
                if max_affordable <= 0:
                    raise ValueError("现金不足，无法买入")
                qty = max_affordable
                cost = qty * market_price
            new_qty = self.position.qty + qty
            if new_qty <= 0:
                raise ValueError("买入后持仓非法")
            if self.position.qty == 0:
                new_avg = market_price
            else:
                new_avg = (self.position.avg_cost * self.position.qty + cost) / new_qty
            self.cash -= cost
            self.position.qty = new_qty
            self.position.avg_cost = new_avg

        elif side == "SELL":
            qty = min(qty, self.position.qty)
            if qty <= 0:
                raise ValueError("无可卖持仓")
            proceeds = qty * market_price
            self.cash += proceeds
            self.position.qty -= qty
            if self.position.qty == 0:
                self.position.avg_cost = 0.0
        else:
            raise ValueError("side must be BUY or SELL")

        trade = Trade(symbol=symbol, side=side, qty=qty, price=market_price, ts=datetime.now(), reason=reason)
        self.trades.append(trade)
        return trade

    def equity(self, last_price: float) -> float:
        return self.cash + self.position.qty * last_price


class LiveBrokerStub(BrokerBase):
    def __init__(self):
        self._position = Position(symbol="DEMO")
        self._cash = 100000.0

    def current_position(self) -> Position:
        return self._position

    def place_market_order(self, symbol: str, side: str, qty: int, market_price: float, reason: str = "") -> Trade:
        raise NotImplementedError("LiveBrokerStub 只是占位接口。请替换成你的券商真实下单逻辑。")

    def equity(self, last_price: float) -> float:
        return self._cash + self._position.qty * last_price


# =========================
# Data Feed
# =========================

class CSVFeed:
    def __init__(self, path: str):
        self.ticks: List[Tick] = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = {c.lower(): c for c in reader.fieldnames or []}
            time_col = None
            for cand in ("timestamp", "datetime", "date", "time", "ts"):
                if cand in cols:
                    time_col = cols[cand]
                    break
            price_col = None
            for cand in ("close", "price", "last", "last_price"):
                if cand in cols:
                    price_col = cols[cand]
                    break
            if not price_col:
                raise ValueError("CSV 需要至少包含 close/price/last 列")

            idx = 0
            for row in reader:
                raw_ts = row.get(time_col) if time_col else None
                if raw_ts:
                    ts = self._parse_ts(raw_ts)
                else:
                    ts = datetime.now() + timedelta(seconds=idx)
                price = float(row[price_col])
                self.ticks.append(Tick(ts=ts, price=price))
                idx += 1

        if not self.ticks:
            raise ValueError("CSV 没有有效数据")
        self._i = 0

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        fmts = [
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d",
        ]
        for fmt in fmts:
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
        return datetime.now()

    def next_tick(self) -> Optional[Tick]:
        if self._i >= len(self.ticks):
            return None
        t = self.ticks[self._i]
        self._i += 1
        return t


class SimulatedFeed:
    def __init__(self, start_price: float = 100.0):
        self.price = start_price
        self.ts = datetime.now()
        self.step = 0

    def next_tick(self) -> Optional[Tick]:
        shock = math.sin(self.step / 7.0) * 0.8 + math.cos(self.step / 11.0) * 0.5
        drift = 0.05 if self.step % 60 < 35 else -0.03
        self.price = max(1.0, self.price + drift + shock * 0.15)
        self.ts += timedelta(seconds=1)
        self.step += 1
        return Tick(ts=self.ts, price=round(self.price, 3))


class AKShareAHistoryFeed:
    def __init__(self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq", period: str = "daily"):
        try:
            import akshare as ak
        except Exception as e:
            raise ImportError(
                "未安装 akshare。请先运行: pip install akshare pandas lxml html5lib requests"
            ) from e

        cleaned_symbol = (symbol or "").strip()
        if not cleaned_symbol.isdigit() or len(cleaned_symbol) != 6:
            raise ValueError("AKShare 模式当前仅支持 6 位 A 股代码，例如 000001、600519")

        start_date = _normalize_date_yyyymmdd(start_date) or "20240101"
        end_date = _normalize_date_yyyymmdd(end_date) or datetime.now().strftime("%Y%m%d")
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")

        df, source_name = self._fetch_with_retry_and_fallback(
            ak=ak,
            symbol=cleaned_symbol,
            period=period,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )

        if df is None or df.empty:
            raise ValueError("AKShare 没有返回数据。请检查代码、日期区间或网络。")

        date_col = self._pick_column(df.columns, ["日期", "date", "Date"])
        close_col = self._pick_column(df.columns, ["收盘", "close", "Close"])
        if not date_col or not close_col:
            raise ValueError(f"无法识别 AKShare 返回列：{list(df.columns)}")

        ticks: List[Tick] = []
        for _, row in df.iterrows():
            raw_ts = row[date_col]
            raw_px = row[close_col]
            if raw_ts is None or raw_px is None:
                continue
            ts = _coerce_datetime(raw_ts)
            price = float(raw_px)
            if price <= 0:
                continue
            ticks.append(Tick(ts=ts, price=price))

        if not ticks:
            raise ValueError("AKShare 返回的数据为空或价格无效")

        self.ticks = ticks
        self._i = 0
        self.meta = {
            "symbol": cleaned_symbol,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
            "rows": len(ticks),
            "source": source_name,
        }

    @staticmethod
    def _symbol_to_prefixed(symbol: str) -> str:
        if symbol.startswith(("6", "5", "9")):
            return f"sh{symbol}"
        if symbol.startswith(("4", "8")):
            return f"bj{symbol}"
        return f"sz{symbol}"

    @classmethod
    def _fetch_with_retry_and_fallback(cls, ak, symbol: str, period: str, start_date: str, end_date: str, adjust: str):
        last_error = None
        retry_waits = [1.0, 2.0, 4.0]
        for wait_s in retry_waits:
            try:
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                )
                if df is not None and not df.empty:
                    return df, "stock_zh_a_hist"
            except Exception as e:
                last_error = e
            time.sleep(wait_s)

        # 备用接口: 新浪日线
        try:
            prefixed = cls._symbol_to_prefixed(symbol)
            df = ak.stock_zh_a_daily(
                symbol=prefixed,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df is not None and not df.empty:
                return df, "stock_zh_a_daily"
        except Exception as e:
            last_error = e

        raise RuntimeError(
            f"AKShare 拉取失败：{last_error}；已尝试 stock_zh_a_hist 重试和 stock_zh_a_daily 备用接口"
        ) from last_error

    @staticmethod
    def _pick_column(columns, candidates):
        lower_map = {str(c).lower(): c for c in columns}
        for candidate in candidates:
            if candidate in columns:
                return candidate
            if str(candidate).lower() in lower_map:
                return lower_map[str(candidate).lower()]
        return None

    def next_tick(self) -> Optional[Tick]:
        if self._i >= len(self.ticks):
            return None
        t = self.ticks[self._i]
        self._i += 1
        return t


# =========================
# Strategy Engine
# =========================

class MovingAverageStrategy:
    def __init__(
        self,
        symbol: str,
        short_window: int = 10,
        long_window: int = 30,
        lot_size: int = 100,
        max_position_pct: float = 0.5,
        stop_loss_pct: float = 0.05,
    ):
        if short_window >= long_window:
            raise ValueError("短均线必须小于长均线")
        self.symbol = symbol
        self.short_window = short_window
        self.long_window = long_window
        self.lot_size = lot_size
        self.max_position_pct = max_position_pct
        self.stop_loss_pct = stop_loss_pct
        self.prices: Deque[float] = deque(maxlen=max(long_window + 5, 200))
        self.last_signal = "FLAT"

    def on_tick(self, tick: Tick, broker: BrokerBase) -> Tuple[Optional[str], Optional[int], str]:
        self.prices.append(tick.price)
        if len(self.prices) < self.long_window:
            return None, None, "等待均线样本"

        short_ma = statistics.fmean(list(self.prices)[-self.short_window:])
        long_ma = statistics.fmean(list(self.prices)[-self.long_window:])
        position = broker.current_position()

        if position.qty > 0 and position.avg_cost > 0:
            if tick.price <= position.avg_cost * (1 - self.stop_loss_pct):
                return "SELL", position.qty, f"触发止损 {self.stop_loss_pct:.1%}"

        if short_ma > long_ma and self.last_signal != "LONG":
            max_equity = broker.equity(tick.price)
            target_value = max_equity * self.max_position_pct
            target_qty = int(target_value // tick.price)
            target_qty = (target_qty // self.lot_size) * self.lot_size
            buy_qty = max(0, target_qty - position.qty)
            if buy_qty >= self.lot_size:
                self.last_signal = "LONG"
                return "BUY", buy_qty, f"均线金叉 {short_ma:.2f}>{long_ma:.2f}"
            self.last_signal = "LONG"
        elif short_ma < long_ma and self.last_signal != "FLAT":
            if position.qty > 0:
                self.last_signal = "FLAT"
                return "SELL", position.qty, f"均线死叉 {short_ma:.2f}<{long_ma:.2f}"
            self.last_signal = "FLAT"

        return None, None, f"观察中 short={short_ma:.2f} long={long_ma:.2f}"


class TradingEngine(threading.Thread):
    def __init__(self, feed, broker: BrokerBase, strategy: MovingAverageStrategy, app_state, poll_interval: float = 1.0):
        super().__init__(daemon=True)
        self.feed = feed
        self.broker = broker
        self.strategy = strategy
        self.app_state = app_state
        self.poll_interval = poll_interval
        self._running = threading.Event()
        self._running.set()

    def stop(self):
        self._running.clear()

    def run(self):
        while self._running.is_set():
            tick = self.feed.next_tick()
            if tick is None:
                self.app_state.log("数据结束，策略已停止")
                self.app_state.running = False
                return

            decision, qty, reason = self.strategy.on_tick(tick, self.broker)
            trade = None
            error = None
            if decision and qty:
                try:
                    trade = self.broker.place_market_order(
                        symbol=self.strategy.symbol,
                        side=decision,
                        qty=qty,
                        market_price=tick.price,
                        reason=reason,
                    )
                except Exception as e:
                    error = str(e)

            equity = self.broker.equity(tick.price)
            position = self.broker.current_position()
            self.app_state.update_market(
                tick=tick,
                equity=equity,
                cash=getattr(self.broker, "cash", None),
                position_qty=position.qty,
                avg_cost=position.avg_cost,
                reason=reason,
                trade=trade,
                error=error,
                decision=decision,
                qty=qty,
            )
            time.sleep(self.poll_interval)


# =========================
# Shared App State
# =========================

class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine: Optional[TradingEngine] = None
        self.running = False
        self.last_price = None
        self.last_ts = None
        self.equity = None
        self.cash = None
        self.position_qty = 0
        self.avg_cost = 0.0
        self.reason = "未启动"
        self.logs: List[str] = []
        self.price_series: List[Tuple[str, float]] = []
        self.equity_series: List[Tuple[str, float]] = []
        self.config = {
            "symbol": "000001",
            "short_window": 10,
            "long_window": 30,
            "stop_loss_pct": 0.05,
            "max_position_pct": 0.50,
            "starting_cash": 100000.0,
            "mode": "paper",
            "data_source": "akshare",
            "csv_path": "",
            "ak_start_date": (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
            "ak_end_date": datetime.now().strftime("%Y-%m-%d"),
            "ak_adjust": "qfq",
            "poll_interval": 0.25,
        }
        self.log("程序启动完成。默认是 AKShare 历史日线回放 + 浏览器界面。")

    def log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-300:]

    def update_market(self, tick: Tick, equity: float, cash, position_qty: int, avg_cost: float, reason: str, trade, error, decision, qty):
        with self.lock:
            self.last_price = tick.price
            self.last_ts = tick.ts.strftime("%Y-%m-%d %H:%M:%S")
            self.equity = round(equity, 2)
            self.cash = round(cash, 2) if cash is not None else None
            self.position_qty = position_qty
            self.avg_cost = round(avg_cost, 4)
            self.reason = reason
            label = tick.ts.strftime("%m-%d") if tick.ts.hour == 0 and tick.ts.minute == 0 else tick.ts.strftime("%m-%d %H:%M")
            self.price_series.append((label, tick.price))
            self.equity_series.append((label, equity))
            self.price_series = self.price_series[-400:]
            self.equity_series = self.equity_series[-400:]
        if trade:
            self.log(f"成交：{trade.side} {trade.qty} 股 @ {trade.price:.3f} | {trade.reason}")
        elif error:
            self.log(f"下单失败：{error}")
        else:
            self.log(f"行情：{tick.ts.strftime('%Y-%m-%d')} 价={tick.price:.3f} | {reason}")

    def stop(self):
        with self.lock:
            engine = self.engine
            self.engine = None
            self.running = False
        if engine:
            engine.stop()
            self.log("策略已停止。")

    def start(self, params: dict):
        self.stop()
        symbol = (params.get("symbol") or "000001").strip()
        short_window = int(params.get("short_window") or 10)
        long_window = int(params.get("long_window") or 30)
        stop_loss_pct = float(params.get("stop_loss_pct") or 0.05)
        max_position_pct = float(params.get("max_position_pct") or 0.5)
        starting_cash = float(params.get("starting_cash") or 100000)
        mode = (params.get("mode") or "paper").strip()
        data_source = (params.get("data_source") or "akshare").strip()
        csv_path = (params.get("csv_path") or "").strip()
        ak_start_date = (params.get("ak_start_date") or "").strip()
        ak_end_date = (params.get("ak_end_date") or "").strip()
        ak_adjust = (params.get("ak_adjust") or "qfq").strip()
        poll_interval = float(params.get("poll_interval") or (0.25 if data_source == "akshare" else 1.0))

        strategy = MovingAverageStrategy(
            symbol=symbol,
            short_window=short_window,
            long_window=long_window,
            stop_loss_pct=stop_loss_pct,
            max_position_pct=max_position_pct,
            lot_size=100,
        )

        if data_source == "csv":
            if not csv_path:
                raise ValueError("你选择了 CSV 模式，但没有填写 CSV 路径")
            if not os.path.exists(csv_path):
                raise FileNotFoundError(f"CSV 路径不存在：{csv_path}")
            feed = CSVFeed(csv_path)
            self.log(f"使用 CSV 回放模式：{csv_path}")
        elif data_source == "simulated":
            feed = SimulatedFeed(start_price=100.0)
            self.log("使用内置模拟行情模式。")
        else:
            feed = AKShareAHistoryFeed(
                symbol=symbol,
                start_date=ak_start_date,
                end_date=ak_end_date,
                adjust=ak_adjust,
                period="daily",
            )
            self.log(
                f"使用 AKShare A股历史回放：{feed.meta['symbol']} {feed.meta['start_date']}~{feed.meta['end_date']} {feed.meta['adjust']} 共 {feed.meta['rows']} 条 | 源={feed.meta.get('source','unknown')}"
            )

        if mode == "paper":
            broker: BrokerBase = PaperBroker(starting_cash=starting_cash)
            self.log("当前模式：模拟盘自动执行。")
        else:
            broker = LiveBrokerStub()
            self.log("当前模式：实盘接口占位。请先改造代码接入券商 API。")

        with self.lock:
            self.last_price = None
            self.last_ts = None
            self.equity = None
            self.cash = None
            self.position_qty = 0
            self.avg_cost = 0.0
            self.reason = "启动中"
            self.price_series = []
            self.equity_series = []
            self.config = {
                "symbol": symbol,
                "short_window": short_window,
                "long_window": long_window,
                "stop_loss_pct": stop_loss_pct,
                "max_position_pct": max_position_pct,
                "starting_cash": starting_cash,
                "mode": mode,
                "data_source": data_source,
                "csv_path": csv_path,
                "ak_start_date": ak_start_date,
                "ak_end_date": ak_end_date,
                "ak_adjust": ak_adjust,
                "poll_interval": poll_interval,
            }
            self.running = True
        engine = TradingEngine(feed=feed, broker=broker, strategy=strategy, app_state=self, poll_interval=poll_interval)
        with self.lock:
            self.engine = engine
        engine.start()
        self.log(
            f"策略启动：symbol={symbol}, source={data_source}, short={short_window}, long={long_window}, stop={stop_loss_pct:.1%}, max_pos={max_position_pct:.1%}"
        )

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "last_price": self.last_price,
                "last_ts": self.last_ts,
                "equity": self.equity,
                "cash": self.cash,
                "position_qty": self.position_qty,
                "avg_cost": self.avg_cost,
                "reason": self.reason,
                "logs": list(self.logs),
                "price_series": list(self.price_series),
                "equity_series": list(self.equity_series),
                "config": dict(self.config),
            }


APP = AppState()


# =========================
# Web UI
# =========================

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>量化自动执行原型（AKShare 版）</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #f6f7fb; color: #222; }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 18px; }
    .card { background: white; border-radius: 14px; box-shadow: 0 6px 18px rgba(0,0,0,.08); padding: 16px; margin-bottom: 16px; }
    h1 { font-size: 24px; margin: 0 0 12px; }
    h2 { font-size: 18px; margin: 0 0 12px; }
    .grid { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; }
    label { display: flex; flex-direction: column; font-size: 13px; gap: 6px; }
    input, select, button { font: inherit; padding: 10px 12px; border: 1px solid #d0d5dd; border-radius: 10px; }
    button { cursor: pointer; }
    .actions { display: flex; gap: 10px; align-items: end; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; }
    .stat { background: #fafbff; border: 1px solid #e8ebf4; border-radius: 12px; padding: 12px; }
    .stat .k { color: #667085; font-size: 12px; }
    .stat .v { font-size: 20px; margin-top: 6px; font-weight: 700; }
    .chart-wrap { display: grid; grid-template-columns: 1fr; gap: 16px; }
    svg { width: 100%; height: 260px; background: #fbfcff; border: 1px solid #e8ebf4; border-radius: 12px; }
    pre { background: #0d1117; color: #d6e2ff; border-radius: 12px; padding: 12px; overflow: auto; height: 260px; margin: 0; }
    .hint { color: #667085; font-size: 13px; margin-top: 8px; }
    .status { display: inline-block; padding: 4px 10px; border-radius: 999px; background: #eef2ff; font-size: 12px; }
    @media (max-width: 980px) { .grid, .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); } }
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>量化自动执行原型（AKShare 版）</h1>
    <div class="hint">默认用 AKShare 拉取 A 股历史日线并做回放；不依赖 Tkinter，适合 macOS 浏览器运行。</div>
  </div>

  <div class="card">
    <h2>控制面板 <span id="runStatus" class="status">未启动</span></h2>
    <form id="startForm" class="grid">
      <label>代码<input name="symbol" value="000001" /></label>
      <label>数据源
        <select name="data_source" id="dataSource">
          <option value="akshare" selected>AKShare A股历史</option>
          <option value="csv">CSV 回放</option>
          <option value="simulated">内置模拟</option>
        </select>
      </label>
      <label>短均线<input name="short_window" type="number" value="10" /></label>
      <label>长均线<input name="long_window" type="number" value="30" /></label>
      <label>止损<input name="stop_loss_pct" type="number" step="0.01" value="0.05" /></label>
      <label>最大仓位<input name="max_position_pct" type="number" step="0.01" value="0.50" /></label>
      <label>初始资金<input name="starting_cash" type="number" step="100" value="100000" /></label>
      <label>模式<select name="mode"><option value="paper">模拟盘</option><option value="live">实盘接口占位</option></select></label>
      <label>开始日期<input name="ak_start_date" value="2025-01-01" /></label>
      <label>结束日期<input name="ak_end_date" value="2026-03-31" /></label>
      <label>复权
        <select name="ak_adjust">
          <option value="qfq" selected>前复权 qfq</option>
          <option value="hfq">后复权 hfq</option>
          <option value="">不复权</option>
        </select>
      </label>
      <label style="grid-column: span 2;">CSV 路径（CSV 模式才需要）<input name="csv_path" placeholder="例如 /Users/you/data/prices.csv" /></label>
      <label>轮询间隔(秒)<input name="poll_interval" type="number" step="0.05" value="0.25" /></label>
      <div class="actions">
        <button type="submit">启动</button>
        <button type="button" id="stopBtn">停止</button>
      </div>
    </form>
    <div class="hint">AKShare 模式当前走个股日线回放，只支持 6 位 A 股代码，如 000001、600519。ETF 或指数可先用 CSV 模式。</div>
  </div>

  <div class="card">
    <h2>状态总览</h2>
    <div class="stats">
      <div class="stat"><div class="k">最新价</div><div class="v" id="lastPrice">-</div></div>
      <div class="stat"><div class="k">权益</div><div class="v" id="equity">-</div></div>
      <div class="stat"><div class="k">现金</div><div class="v" id="cash">-</div></div>
      <div class="stat"><div class="k">持仓</div><div class="v" id="position">-</div></div>
      <div class="stat"><div class="k">状态</div><div class="v" id="reason" style="font-size:14px">-</div></div>
    </div>
  </div>

  <div class="card chart-wrap">
    <div>
      <h2>价格走势</h2>
      <svg id="priceChart" viewBox="0 0 1000 260"></svg>
    </div>
    <div>
      <h2>权益曲线</h2>
      <svg id="equityChart" viewBox="0 0 1000 260"></svg>
    </div>
  </div>

  <div class="card">
    <h2>日志</h2>
    <pre id="logs"></pre>
  </div>
</div>
<script>
async function postForm(url, formData) {
  const body = new URLSearchParams(formData);
  const res = await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'request failed');
  return data;
}

function drawChart(svg, series, title) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const w = 1000, h = 260, pad = 30;
  const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bg.setAttribute('x', 0); bg.setAttribute('y', 0); bg.setAttribute('width', w); bg.setAttribute('height', h); bg.setAttribute('fill', '#fbfcff');
  svg.appendChild(bg);

  if (!series || series.length < 2) {
    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', 30); txt.setAttribute('y', 40); txt.setAttribute('fill', '#667085'); txt.textContent = title + '：等待数据';
    svg.appendChild(txt); return;
  }
  const vals = series.map(x => x[1]);
  const min = Math.min(...vals); const max = Math.max(...vals);
  const span = (max - min) || 1;
  let points = '';
  series.forEach((p, i) => {
    const x = pad + (i * (w - pad*2) / (series.length - 1));
    const y = h - pad - ((p[1] - min) / span) * (h - pad*2);
    points += `${x},${y} `;
  });
  const axis = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  axis.setAttribute('d', `M ${pad} ${pad} L ${pad} ${h-pad} L ${w-pad} ${h-pad}`);
  axis.setAttribute('stroke', '#cfd4dc'); axis.setAttribute('fill', 'none');
  svg.appendChild(axis);
  const pl = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  pl.setAttribute('points', points.trim());
  pl.setAttribute('stroke', '#2563eb'); pl.setAttribute('stroke-width', '2'); pl.setAttribute('fill', 'none');
  svg.appendChild(pl);

  const labels = [
    {x: 8, y: pad + 5, t: max.toFixed(2)},
    {x: 8, y: h - pad + 4, t: min.toFixed(2)},
    {x: pad, y: h - 8, t: series[0][0]},
    {x: w - 90, y: h - 8, t: series[series.length - 1][0]},
  ];
  labels.forEach(item => {
    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', item.x); txt.setAttribute('y', item.y); txt.setAttribute('fill', '#667085'); txt.setAttribute('font-size', '12'); txt.textContent = item.t;
    svg.appendChild(txt);
  });
}

function setText(id, value) { document.getElementById(id).textContent = value ?? '-'; }

async function refreshState() {
  const res = await fetch('/state');
  const state = await res.json();
  document.getElementById('runStatus').textContent = state.running ? '运行中' : '已停止';
  setText('lastPrice', state.last_price == null ? '-' : Number(state.last_price).toFixed(3));
  setText('equity', state.equity == null ? '-' : Number(state.equity).toFixed(2));
  setText('cash', state.cash == null ? '外部接口' : Number(state.cash).toFixed(2));
  setText('position', `${state.position_qty} @ ${Number(state.avg_cost || 0).toFixed(3)}`);
  setText('reason', state.reason || '-');
  document.getElementById('logs').textContent = (state.logs || []).join('\\n');
  drawChart(document.getElementById('priceChart'), state.price_series || [], '价格走势');
  drawChart(document.getElementById('equityChart'), state.equity_series || [], '权益曲线');
}

document.getElementById('startForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const fd = new FormData(e.target);
    await postForm('/start', fd);
    refreshState();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById('stopBtn').addEventListener('click', async () => {
  try {
    await postForm('/stop', new FormData());
    refreshState();
  } catch (err) {
    alert(err.message);
  }
});

refreshState();
setInterval(refreshState, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(INDEX_HTML)
            return
        if parsed.path == "/state":
            self._json(APP.snapshot())
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        params = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        try:
            if parsed.path == "/start":
                APP.start(params)
                self._json({"ok": True})
                return
            if parsed.path == "/stop":
                APP.stop()
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, status=404)
        except Exception as e:
            APP.log(f"操作失败：{e}")
            self._json({"error": str(e)}, status=400)

    def log_message(self, format, *args):
        return


def _normalize_date_yyyymmdd(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 8:
        return digits
    raise ValueError("日期请用 YYYY-MM-DD 或 YYYYMMDD")


def _coerce_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime()
        except Exception:
            pass
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.now()


def main():
    host = "127.0.0.1"
    port = 8765
    print(f"量化 Web AKShare 版已启动: http://{host}:{port}")
    print("在浏览器里打开上面的地址即可。按 Ctrl+C 停止。")
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.stop()
        server.server_close()


if __name__ == "__main__":
    main()
