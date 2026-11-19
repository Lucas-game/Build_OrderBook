"""
sz_stock_build_orderbook.py —— 深交所：拼逐笔流
==============================================

整体思路（详见同目录的 build_orderbook.py）：
    第 1 步  拼逐笔流（本文件）：orders.csv + trades.csv → 十列逐笔流，按 seq 排序
    第 2 步  建订单簿（公共文件 build_orderbook.py）：一行一行回放，和快照对账

适用范围：深交所股票，主板（例如 000001）和创业板（例如 300750）都能用，
         两者撮合规则和数据格式相同。上交所在 sh_stock_build_orderbook.py。

运行：在 lob_rebuild/ 目录下 python3 src/sz_stock_build_orderbook.py

深市数据的特点（拼逐笔流为什么简单）：
    1. 委托和成交共用一套 seq（交易所发的排队号），合起来按 seq 一排，就是真实先后。
    2. 撤单在 trades.csv 里：被撤的一边写订单号，另一边是 0，价格是 0。
    3. 每个订单都有一行 ADD（完整委托量），哪怕它一进门就全部成交。
    4. 有委托类型：LIMIT 挂委托价；BEST_OWN 挂本方一档（价格字段不可信）；
       MARKET 不挂盘口。这些在公共的 OrderBook.add_order 里处理。

已验证：000001（2026-03-02）、300750（2022-04-26）收盘十档和快照逐档一致，
       三项异常计数都是 0。
"""

from __future__ import annotations

from pathlib import Path
import pandas as pd

# 公共部分：逐笔流的十列、回放、订单簿、对账、检查
# 直接运行本文件时，src/ 就在 Python 的搜索路径里，可以直接 import 同目录的文件
from build_orderbook import (
    TICK_BY_TICK_COLUMNS, OrderBook, build_order_book,
    compare_with_snapshot, check_tick_by_tick,
)

# ============================================================
# 常量
# ============================================================

# 交易时段（深交所）：名称、开始时间、结束时间（含开始，不含结束）
SESSIONS = [
    ("开盘集合竞价 9:15-9:25",  91500000,  92500000),
    ("开盘撮合与等待 9:25-9:30", 92500000,  93000000),
    ("上午连续竞价 9:30-11:30", 93000000, 113000000),
    ("午间休市 11:30-13:00",   113000000, 130000000),
    ("下午连续竞价 13:00-14:57", 130000000, 145700000),
    ("收盘集合竞价 14:57-15:00", 145700000, 150000001),   # 15:00:00.000 的收盘成交也算在内
]

# ============================================================
# 第 0 层：整个流程
# ============================================================

def main() -> None:
    """整个流程：拼逐笔流 → 回放全天建订单簿 → 和收盘快照对账。"""
    # 本文件在 L2 数据/lob_rebuild/src/ 下，数据在 L2 数据/data/ 下：往上走 2 层到 L2 数据/
    folder = Path(__file__).resolve().parents[2] / "data" / "SZ_000001_20260302"

    # 第 1 步：逐笔流
    print(f"读取：{folder}")
    tick_by_tick = load_sz_tick_by_tick(str(folder))
    print("\n检查结果：")
    for key, value in check_tick_by_tick(tick_by_tick).items():
        print(f"  {key}: {value}")
    print("\n按时段统计：")
    print(count_by_session(tick_by_tick).to_string(index=False))

    # 第 2 步：订单簿，回放全天（公共函数 build_order_book）
    book = build_order_book(tick_by_tick)

    # 收盘对账：和最后一张快照比
    snapshots = pd.read_csv(folder / "snapshots.csv")
    print("\n收盘对账：")
    print(compare_with_snapshot(book, snapshots.iloc[-1]).to_string(index=False))
    print("\n异常计数：", book.problems)

# ============================================================
# 第 1 层：把 orders.csv 和 trades.csv 整理成逐笔流
# 中间加 orders_to_tick_by_tick 和 trades_to_tick_by_tick 两个函数，是因为直接 concat 会补 NaN，
# 整数订单号变成小数，还混进用不到的列。先各自整理成同样的十列，再拼。
# ============================================================

def read_orders(folder: str) -> pd.DataFrame:
    """读取逐笔委托 orders.csv。"""
    path = Path(folder) / "orders.csv"
    # keep_default_na=False：空字符串保持为空字符串，不要变成 NaN
    return pd.read_csv(path, keep_default_na=False, dtype={"side": str, "order_type": str})


def read_trades(folder: str) -> pd.DataFrame:
    """读取逐笔成交 trades.csv（深市的撤单也在这里）。"""
    path = Path(folder) / "trades.csv"
    return pd.read_csv(path, keep_default_na=False)


def orders_to_tick_by_tick(orders: pd.DataFrame) -> pd.DataFrame:
    """把逐笔委托整理成逐笔流的格式：每一行都是一笔新委托（ADD）。"""
    part = pd.DataFrame()
    part["seq"] = orders["seq"].astype("int64")
    part["time"] = orders["time"].astype("int64")
    part["event"] = "ADD"
    part["side"] = orders["side"]
    part["order_type"] = orders["order_type"]
    part["price"] = orders["price"].astype("int64")
    part["qty"] = orders["qty"].astype("int64")
    # Int64（大写 I）是能放空值的整数类型，不会因为有空值变成小数
    part["order_no"] = orders["order_no"].astype("Int64")
    # 新委托没有关联的买卖方订单号，留空
    part["buy_no"] = pd.array([pd.NA] * len(orders), dtype="Int64")
    part["sell_no"] = pd.array([pd.NA] * len(orders), dtype="Int64")
    return part


def trades_to_tick_by_tick(trades: pd.DataFrame) -> pd.DataFrame:
    """把逐笔成交整理成逐笔流的格式：每一行是成交（TRADE）或撤单（CANCEL）。"""
    part = pd.DataFrame()
    part["seq"] = trades["seq"].astype("int64")
    part["time"] = trades["time"].astype("int64")
    part["event"] = trades["event"]
    part["order_type"] = ""
    part["price"] = trades["price"].astype("int64")
    part["qty"] = trades["qty"].astype("int64")
    part["order_no"] = pd.array([pd.NA] * len(trades), dtype="Int64")
    part["buy_no"] = trades["buy_no"].astype("Int64")
    part["sell_no"] = trades["sell_no"].astype("Int64")

    # 撤单的方向：深市撤单只有一边订单号不为 0，哪边不为 0 就是撤哪边的单
    #   buy_no 不为 0  -> 撤的是买单，方向 B
    #   sell_no 不为 0 -> 撤的是卖单，方向 S
    # 成交涉及买卖两方，方向留空
    part["side"] = ""
    is_cancel = part["event"] == "CANCEL"
    cancel_buy = is_cancel & (part["buy_no"] != 0)
    cancel_sell = is_cancel & (part["sell_no"] != 0)
    part.loc[cancel_buy, "side"] = "B"
    part.loc[cancel_sell, "side"] = "S"
    return part


def load_sz_tick_by_tick(folder: str) -> pd.DataFrame:
    """
    读一只深市股票一天的数据，返回按 seq 排序的逐笔流。

    参数：
        folder: 数据文件夹，例如 "../data/SZ_000001_20260302"
    返回：
        逐笔流，列为 TICK_BY_TICK_COLUMNS
    """

    orders = read_orders(folder)
    trades = read_trades(folder)

    exchange = orders['exchange'].iloc[0]
    if exchange != 'SZ':
        raise ValueError(f'暂不支持交易所 {exchange}，目前只支持深交所（SZ）')
    
    order_part = orders_to_tick_by_tick(orders)
    trade_part = trades_to_tick_by_tick(trades)

    tick_by_tick = pd.concat([order_part, trade_part], ignore_index=True)
    tick_by_tick = tick_by_tick.sort_values("seq", ignore_index=True)
    return tick_by_tick[TICK_BY_TICK_COLUMNS]


# ============================================================
# 第 2 层：检查用的工具（深市专用）
# ============================================================

def count_by_session(tick_by_tick: pd.DataFrame) -> pd.DataFrame:
    """按交易时段统计各类动作的条数。"""
    rows = []
    for name, start, end in SESSIONS:
        part = tick_by_tick[(tick_by_tick["time"] >= start) & (tick_by_tick["time"] < end)]
        rows.append({
            "时段": name,
            "ADD": int((part["event"] == "ADD").sum()),
            "TRADE": int((part["event"] == "TRADE").sum()),
            "CANCEL": int((part["event"] == "CANCEL").sum()),
            "合计": len(part),
        })
    table = pd.DataFrame(rows)

    # 不在任何时段里的逐笔，单独列一行，方便发现异常
    covered = int(table["合计"].sum())
    table.loc[len(table)] = {"时段": "其他时间", "ADD": None, "TRADE": None,
                             "CANCEL": None, "合计": len(tick_by_tick) - covered}
    return table


if __name__ == "__main__":
    main()
