"""
build_orderbook.py —— 订单簿重建的公共部分（深沪共用）
====================================================

整个项目的思路分两步：
    第 1 步  拼逐笔流：把 orders.csv（逐笔委托）和 trades.csv（逐笔成交）整理成同一张十列的表，
            排好先后顺序。两个交易所的原始数据长得不一样，这一步各写各的：
                sz_stock_build_orderbook.py   深交所（主板、创业板）
                sh_stock_build_orderbook.py   上交所（股票、ETF、可转债）
    第 2 步  建订单簿：沿着逐笔流一行一行回放，每一行改一次订单簿。
            只要逐笔流是同样的十列，这一步和交易所无关，所以放在本文件，深沪共用。

本文件里有：
    TICK_BY_TICK_COLUMNS   逐笔流的十列（深沪整理后都要长这样）
    build_order_book       回放：一行一行读逐笔流，返回订单簿
    OrderBook              订单簿：三本字典 orders（账本）、bids、asks（盘口）
    compare_with_snapshot  对账：订单簿前 10 档和交易所快照逐档比较
    check_tick_by_tick     检查逐笔流：行数、各类事件条数、顺序

为什么能共用：深交所是“先挂再扣”，谁和谁成交、撤了哪笔，交易所都写在记录里了，
订单簿不做撮合，只照着纸条加减。沪市在整理那一步把撤单、排序都改成深市的样子，
所以进到 OrderBook 时，两边没有区别。
"""

# 让类型标注可以写在定义之前（build_order_book 写在 OrderBook 前面，标注里要用到它）
from __future__ import annotations

import pandas as pd

# ============================================================
# 常量：逐笔流的十列
# ============================================================

# seq         先后顺序。深市：交易所序号，委托和成交共用；沪市：没有，合并排序后自己编
# time        时间，hhmmssmmm，例如 93059990 = 09:30:59.990
# event       这条逐笔是什么动作：ADD 新委托 / TRADE 成交 / CANCEL 撤单
# side        方向 B / S。ADD 和 CANCEL 有；TRADE 留空（成交涉及买卖两方）
# order_type  LIMIT 限价 / MARKET 市价 / BEST_OWN 本方最优。只有 ADD 有（沪市没有类型，统一 LIMIT）
# price       价格，整数，单位 0.0001 元
# qty         数量，股
# order_no    委托自己的订单号。只有 ADD 有（深市等于 seq；沪市撤单的订单号挪到 buy_no / sell_no）
# buy_no      成交或撤单关联的买方订单号。只有 TRADE、CANCEL 有（撤单时另一边是 0）
# sell_no     成交或撤单关联的卖方订单号。只有 TRADE、CANCEL 有
TICK_BY_TICK_COLUMNS = [
    "seq", "time", "event", "side", "order_type",
    "price", "qty", "order_no", "buy_no", "sell_no",
]


# ============================================================
# 第 1 层：回放。把整理好的逐笔流一行一行交给 OrderBook.apply_row，返回订单簿
# ============================================================
def build_order_book(tick_by_tick: pd.DataFrame, stop_time: int | None = None) -> OrderBook:
    """
    新建一个订单簿，从头读逐笔流，每一行交给 book.apply_row。
    stop_time 为 None 就读到最后；
    否则读到 time > stop_time 的那一行就停下（那一行不处理），返回此刻的订单簿。
    itertuples(index=False)：一次给一行，可以用 row.event、row.qty 取值；index=False 表示不要行号。
    """
    order_book = OrderBook()
    for row in tick_by_tick.itertuples(index=False):
        if stop_time is not None and row.time > stop_time:   # 超了就停
            break
        order_book.apply_row(row)                            # 走到这里，说明没超
    return order_book


# ============================================================
# 第 2 层：订单簿本身
# ============================================================

class OrderBook:
    """一只股票的订单簿：三张字典。从上往下：入口 apply_row → 三种动作 → 内部扣减 → 查询。"""

    def __init__(self) -> None:
        self.orders = {}     # 订单号 -> [方向, 价格, 剩余量]；不挂盘口的单（市价单）价格为 None
        self.bids = {}       # 价格 -> 买单总量
        self.asks = {}       # 价格 -> 卖单总量
        self.problems = {
            "找不到订单号": 0,
            "剩余量变负": 0,
            "本方最优单到达时本方没有挂单": 0,
        }

    # ---------- 入口：处理逐笔流的一行 ----------

    def apply_row(self, row) -> None:
        """
        处理逐笔流的一行：看 row.event，调用上面三个方法之一。
        这是“逐笔流”和“订单簿”之间的接口：逐笔流的列名以后变了，只改这里。
            ADD     -> add_order(row.order_no, row.side, row.order_type, row.price, row.qty)
            TRADE   -> apply_trade(row.buy_no, row.sell_no, row.qty)
            CANCEL  -> apply_cancel(撤买单用 row.buy_no，撤卖单用 row.sell_no, row.qty)
        """
        if row.event == 'ADD':
            self.add_order(row.order_no, row.side, row.order_type, row.price, row.qty)
        elif row.event == 'TRADE':
            self.apply_trade(row.buy_no, row.sell_no, row.qty)
        elif row.event == 'CANCEL':
            order_no = row.buy_no if row.side == 'B' else row.sell_no
            self.apply_cancel(order_no, row.qty)
        else:
            raise ValueError(f"未知的 event: {row.event}")

    # ---------- 改状态：三种动作 ----------
    def _add_to_level(self, side: str, price: int, qty: int) -> None:
        """在买盘（side="B"）或卖盘（side="S"）的某个价位上加 qty。
        qty 可以是负数，表示减；减到 0 就把这个价位删掉。"""
        if side == "B":
            order_side = self.bids
        else:
            order_side = self.asks

        if price in order_side:
            order_side[price] += qty
        else:
            order_side[price] = qty

        if order_side[price] <= 0:
            del order_side[price]

    def _reduce_order(self, order_no: int, qty: int) -> None:
        """
        给一笔委托扣 qty：
            1. 订单号不在 orders 里：problems["找不到订单号"] 加 1，直接返回
            2. 剩余量减 qty；变成负数就 problems["剩余量变负"] 加 1
            3. 价格不是 None（挂在盘口上）：对应价位也扣 qty（调用 _add_to_level，传负数）
            4. 剩余量 <= 0：从 orders 删掉
        """
        if order_no not in self.orders:
            self.problems["找不到订单号"] += 1
            return

        side, price, remaining_qty = self.orders[order_no]
        remaining_qty -= qty

        if remaining_qty < 0:
            self.problems["剩余量变负"] += 1

        if price is not None:
            self._add_to_level(side, price, -qty)

        if remaining_qty <= 0:
            del self.orders[order_no]
        else:
            self.orders[order_no][2] = remaining_qty
        
    

    def add_order(self, order_no: int, side: str, order_type: str, price: int, qty: int) -> None:
        """
        新委托：先算出挂单价格，再登记到 orders，最后挂到盘口。
            LIMIT     用 price
            BEST_OWN  用本方最优价（买单 best_bid()，卖单 best_ask()）；本方没有挂单就记一次 problems，价格当 None
            MARKET    价格为 None，只登记，不挂盘口，订单流后面会有成交或撤单来扣它的剩余量
        """
        # 1. 算出挂单价格（你写到这里了；建议用新名字 px，不覆盖原来的 price）
        if order_type == "LIMIT":
            price = price
        elif order_type == "BEST_OWN":
            if side == "B":
                price = self.best_bid()
            else:
                price = self.best_ask()
            if price is None:
                self.problems["本方最优单到达时本方没有挂单"] += 1
        elif order_type == "MARKET":
            price = None

        # 2. 登记到 orders：
        self.orders[order_no] = [side, price, qty]
        # 3. 挂到盘口：
        if price is not None:
            self._add_to_level(side, price, qty)

    def apply_trade(self, buy_no: int, sell_no: int, qty: int) -> None:
        """成交：买方、卖方两笔委托各扣 qty（调用 _reduce_order 两次）。"""
        self._reduce_order(buy_no, qty)
        self._reduce_order(sell_no, qty)
        
    def apply_cancel(self, order_no: int, qty: int) -> None:
        """撤单：被撤的那笔委托扣 qty（调用 _reduce_order 一次）。"""
        self._reduce_order(order_no, qty)


    # ---------- 查状态 ----------
    # 现在用 max / min 扫一遍；以后要提速（缓存、树、数组），只改这两个方法

    # 先判断这本字典是不是空的，空就返回 None。
    # 不空：买盘取最高价（bids 的键里取最大），卖盘取最低价（asks 的键里取最小）。
    def best_bid(self) -> int | None:
        if self.bids:
            return max(self.bids.keys())
        else:
            return None

    def best_ask(self) -> int | None:
        if self.asks:
            return min(self.asks.keys())
        else:
            return None

    def top_levels(self, n: int = 10) -> tuple[list, list]:
        """
        买卖各前 n 档，返回 (买盘, 卖盘)，每个是 [(价格, 数量), ...]。
        买盘按价格从高到低，卖盘按价格从低到高。
        """
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks
        
# ============================================================
# 第 3 层：检查用的工具（对账、逐笔流检查）
# ============================================================
def compare_with_snapshot(book: OrderBook, snapshot: pd.Series) -> pd.DataFrame:
    """
    订单簿前 10 档和一张交易所快照逐档比较，返回 10 行的对账表，列为：
        档位, 我们的买价, 我们的买量, 快照买价, 快照买量,
              我们的卖价, 我们的卖量, 快照卖价, 快照卖量, 是否一致
    快照的列名见 README_数据说明.md：bid_px1..10、bid_qty1..10、ask_px1..10、ask_qty1..10。
    我们的订单簿不足 10 档时，缺的档位价格和数量都记为 0（快照里也是 0）。
    """
    # 1. 我们订单簿的前 10 档：bids / asks 都是 [(价格, 数量), ...]
    bids, asks = book.top_levels(10)

    rows = []
    for i in range(10):
        # 2. 我们的第 i+1 档；订单簿不足 10 档就补 (0, 0)
        our_bid_price, our_bid_qty = bids[i] if i < len(bids) else (0, 0)
        our_ask_price, our_ask_qty = asks[i] if i < len(asks) else (0, 0)

        # 3. 快照的第 i+1 档（列名从 1 开始数）
        snap_bid_price = int(snapshot[f"bid_px{i + 1}"])
        snap_bid_qty = int(snapshot[f"bid_qty{i + 1}"])
        snap_ask_price = int(snapshot[f"ask_px{i + 1}"])
        snap_ask_qty = int(snapshot[f"ask_qty{i + 1}"])

        # 4. 四个数全部相等才算这一档一致
        same = (our_bid_price == snap_bid_price and our_bid_qty == snap_bid_qty
                and our_ask_price == snap_ask_price and our_ask_qty == snap_ask_qty)

        rows.append({
            "档位": i + 1,
            "我们的买价": our_bid_price, "我们的买量": our_bid_qty,
            "快照买价": snap_bid_price, "快照买量": snap_bid_qty,
            "我们的卖价": our_ask_price, "我们的卖量": our_ask_qty,
            "快照卖价": snap_ask_price, "快照卖量": snap_ask_qty,
            "是否一致": same,
        })
    return pd.DataFrame(rows)


def check_tick_by_tick(tick_by_tick: pd.DataFrame) -> dict:
    """
    检查逐笔流，返回第 1 步验收需要的各项数字。

    返回的字典里有：
        总行数、各类动作条数、各类委托条数、seq 是否重复、时间倒退次数
    """
    result = {}

    # 1. 总行数
    result["总行数"] = len(tick_by_tick)

    # 2. 各类动作条数：ADD / TRADE / CANCEL
    for name in ["ADD", "TRADE", "CANCEL"]:
        result[f"动作 {name}"] = int((tick_by_tick["event"] == name).sum())

    # 3. 新委托里各类委托条数：LIMIT / MARKET / BEST_OWN
    adds = tick_by_tick[tick_by_tick["event"] == "ADD"]
    for name in ["LIMIT", "MARKET", "BEST_OWN"]:
        result[f"委托 {name}"] = int((adds["order_type"] == name).sum())

    # 4. seq 有没有重复
    result["seq 不重复"] = bool(tick_by_tick["seq"].is_unique)

    # 5. 按 seq 排好后，时间有没有往回走
    time_diff = tick_by_tick["time"].diff()
    result["时间倒退次数"] = int((time_diff < 0).sum())

    return result
