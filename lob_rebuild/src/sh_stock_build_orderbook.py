"""
sh_stock_build_orderbook.py —— 上交所：拼逐笔流
==============================================

整体思路（详见同目录的 build_orderbook.py）：
    第 1 步  拼逐笔流（本文件）：orders.csv + trades.csv → 十列逐笔流，自己造顺序
    第 2 步  建订单簿（公共文件 build_orderbook.py）：一行一行回放，和快照对账

适用范围：上交所的股票（603353）、ETF（518880）、可转债（110093）。
运行：在 lob_rebuild/ 目录下 python3 src/sh_stock_build_orderbook.py

沪市和深市数据的不同（已在三份样本上核对）：
    1. 撤单在 orders.csv 里（event=CANCEL），order_no 是被撤订单的号，自带价格和方向；
       还有 STATUS 行（交易阶段切换），不是订单。
    2. 没有委托和成交共用的 seq（排队号），两个文件只能先按 time 排，
       同一毫秒内的先后要用订单号自己定规则。
    3. 没有 order_type，所有 ADD 都带真实挂单价，统一当 LIMIT。
    4. 股票、ETF 的 ADD 数量是“剩余量”：先成交，剩下的才写 ADD；
       一进门就全部成交的订单根本没有 ADD（幽灵订单）。可转债的 ADD 是完整量。

所以本文件做三件事，让逐笔流进到 OrderBook 时和深市没有区别：
    - 撤单装扮成深市的样子：被撤订单号放进 buy_no 或 sell_no，另一边填 0；
    - 按（time, key, rank）排序，再编 seq：
          key  = ADD 用自己的订单号；TRADE 用较大的订单号（主动方）；CANCEL 用被撤订单号
          rank = key 相同时谁先：股票 / ETF 成交在 ADD 前，可转债 ADD 在成交前，撤单最后
    - trade_before_add 参数区分股票 / ETF（True）和可转债（False）。

验收看两样：剩余量变负 = 0，收盘十档和快照一致。
“找不到订单号”在沪市大部分是幽灵订单，属于正常现象。
已验证：603353、518880、110093 收盘十档 10/10，剩余量变负 0。
排序规则是从三份样本里归纳的，还没有逐张快照验证。
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
# 第 0 层：整个流程
# ============================================================

# 三份沪市样本，以及各自该传的 trade_before_add
SAMPLES = [
    ("SH_603353_20260302", True),    # 股票：ADD 是剩余量，成交在前
    ("SH_518880_20260302", True),    # ETF：同股票
    ("SH_110093_20260302", False),   # 可转债：ADD 是完整量，ADD 在前
]


def main() -> None:
    """整个流程：拼逐笔流 → 回放全天建订单簿 → 和收盘快照对账。三份沪市样本依次跑。"""
    # 本文件在 L2 数据/lob_rebuild/src/ 下，数据在 L2 数据/data/ 下：往上走 2 层到 L2 数据/
    data_root = Path(__file__).resolve().parents[2] / "data"

    for name, trade_before_add in SAMPLES:
        folder = data_root / name
        print(f"\n======== {name}（trade_before_add={trade_before_add}）========")

        # 第 1 步：拼逐笔流
        tick_by_tick = load_sh_tick_by_tick(str(folder), trade_before_add)
        for key, value in check_tick_by_tick(tick_by_tick).items():
            print(f"  {key}: {value}")

        # 第 2 步：订单簿，回放全天（公共函数 build_order_book）
        book = build_order_book(tick_by_tick)

        # 收盘对账：和最后一张快照比
        snapshots = pd.read_csv(folder / "snapshots.csv")
        table = compare_with_snapshot(book, snapshots.iloc[-1])
        print(table.to_string(index=False))
        print("十档一致：", int(table["是否一致"].sum()), "/ 10")
        print("异常计数：", book.problems, "（沪市“找不到订单号”大部分是幽灵订单，属正常）")


# ============================================================
# 第 1 层：把 orders.csv 和 trades.csv 合并成逐笔流
#   1. 读两个文件
#   2. 各自整理成和深市一样的十列（列不同不能直接拼，见深市文件的说明）
#   3. 拼起来
#   4. 沪市没有 seq：按（时间, key, rank）排好，再编一个 seq
# ============================================================

def load_sh_tick_by_tick(folder: str, trade_before_add: bool = True) -> pd.DataFrame:
    """
    读一只沪市证券一天的数据，返回和深市同样列的逐笔流。

    参数：
        folder:           数据文件夹，例如 "../data/SH_603353_20260302"
        trade_before_add: 同一个 key 里，成交是否排在 ADD 前面。
                          股票、ETF 的 ADD 写的是成交后的剩余量 -> True（默认，可以不写）；
                          可转债的 ADD 是完整委托量 -> False（必须手动传）。

    注意：trade_before_add 由调用本函数的人决定，程序不会自动判断。
        这个参数会原样往下传：
            load_sh_tick_by_tick → sort_sh_tick_by_tick → add_sort_columns
        到 add_sort_columns 里才真正用来选 rank 对照表。
        传错的后果：110093（可转债）按默认 True 回放，收盘十档从 10/10 变成 0/10。
        以后写 main() 时，可以按证券代码前缀自动判断（例如 110 开头是可转债）。
    """
    # 1. 读
    orders = read_sh_orders(folder)
    trades = read_sh_trades(folder)

    exchange = orders["exchange"].iloc[0]
    if exchange != "SH":
        raise ValueError(f"这里只处理上交所（SH），读到的是 {exchange}")

    # 2. 各自整理成十列
    order_part = sh_orders_to_tick_by_tick(orders)
    trade_part = sh_trades_to_tick_by_tick(trades)

    # 3. 拼起来
    tick_by_tick = pd.concat([order_part, trade_part], ignore_index=True)

    # 4. 排序并编 seq
    return sort_sh_tick_by_tick(tick_by_tick, trade_before_add)


def read_sh_orders(folder: str) -> pd.DataFrame:
    """读取沪市逐笔委托 orders.csv（里面有 ADD、CANCEL、STATUS 三种行）。"""
    path = Path(folder) / "orders.csv"
    # keep_default_na=False：空字符串保持为空字符串，不要变成 NaN
    return pd.read_csv(path, keep_default_na=False, dtype={"side": str})


def read_sh_trades(folder: str) -> pd.DataFrame:
    """读取沪市逐笔成交 trades.csv（全部是 TRADE）。"""
    path = Path(folder) / "trades.csv"
    return pd.read_csv(path, keep_default_na=False)


def sh_orders_to_tick_by_tick(orders: pd.DataFrame) -> pd.DataFrame:
    """
    把沪市委托 orders.csv 整理成逐笔流的十列。

    原始文件里有三种行，处理方式：
        STATUS  交易阶段切换（开盘、收盘等），不是订单 -> 丢掉
        ADD     新委托。沪市 ADD 都带真实挂单价 -> 委托类型当 LIMIT
        CANCEL  撤单。原始的 order_no 是“被撤订单的号” -> 挪到 buy_no / sell_no，
                改成和深市撤单一样的格式，OrderBook.apply_row 就不用改

    例子（整理前 -> 整理后）：
        ADD    B  order_no=2       ->  order_no=2       buy_no=空       sell_no=空
        CANCEL B  order_no=134805  ->  order_no=空      buy_no=134805   sell_no=0
        CANCEL S  order_no=489777  ->  order_no=空      buy_no=0        sell_no=489777

    写法：每一列都是“先给所有行填默认值，再用 .loc[条件, 列] 只改符合条件的行”。
    """
    # ---------- 1. 去掉 STATUS，准备筛选条件 ----------
    # reset_index：去掉 STATUS 后行号会断开（比如少了 0），重新从 0 编号，
    # 这样原始表 orders 和新表 part 的行号一一对应，下面 .loc 才不会改错行
    orders = orders[orders["event"] != "STATUS"].reset_index(drop=True)
    n = len(orders)

    # 两列 True/False（布尔 Series），行号和 orders 对齐，用来挑出 ADD 行、撤单行
    is_add = orders["event"] == "ADD"
    is_cancel = orders["event"] == "CANCEL"

    # part：正在一列一列填写的新表，最后就是逐笔流
    part = pd.DataFrame()

    # ---------- 2. 原样照抄的列 ----------
    # seq：沪市没有委托和成交共用的排队号，先全部留空，合并排序后再编
    #      Int64（大写 I）是能放空值的整数类型，不会因为有空值变成小数
    part["seq"] = pd.array([pd.NA] * n, dtype="Int64")
    part["time"] = orders["time"].astype("int64")
    part["event"] = orders["event"]
    part["side"] = orders["side"]
    part["price"] = orders["price"].astype("int64")
    part["qty"] = orders["qty"].astype("int64")   # 股票、ETF 的 ADD 是成交后的剩余量

    # ---------- 3. order_type：ADD 填 LIMIT，撤单留空 ----------
    # 先全部填空（撤单要的就是空），再只把 ADD 行改成 LIMIT
    part["order_type"] = ""
    part.loc[is_add, "order_type"] = "LIMIT"

    # ---------- 4. order_no：只有 ADD 有 ----------
    # 逐笔流的约定（和深市一样）：order_no = 新委托自己的订单号。
    # 撤单行原始的 order_no 是被撤订单的号，不放这里，第 5 步放进 buy_no / sell_no
    part["order_no"] = pd.array([pd.NA] * n, dtype="Int64")
    part.loc[is_add, "order_no"] = orders.loc[is_add, "order_no"]

    # ---------- 5. buy_no / sell_no：只有撤单有 ----------
    # 先全部留空（ADD 行要的就是空）
    part["buy_no"] = pd.array([pd.NA] * n, dtype="Int64")
    part["sell_no"] = pd.array([pd.NA] * n, dtype="Int64")

    # 两个条件：& 表示“并且”，两边都为 True 才是 True
    cancel_buy = is_cancel & (orders["side"] == "B")    # 撤的是买单
    cancel_sell = is_cancel & (orders["side"] == "S")   # 撤的是卖单

    # 撤买单：被撤订单号放进 buy_no，另一边 sell_no 填 0（深市撤单就是这个样子）
    part.loc[cancel_buy, "buy_no"] = orders.loc[cancel_buy, "order_no"]
    part.loc[cancel_buy, "sell_no"] = 0
    # 撤卖单：反过来
    part.loc[cancel_sell, "sell_no"] = orders.loc[cancel_sell, "order_no"]
    part.loc[cancel_sell, "buy_no"] = 0

    # 列的顺序在最后 sort_sh_tick_by_tick 里统一按 TICK_BY_TICK_COLUMNS 排
    return part


def sh_trades_to_tick_by_tick(trades: pd.DataFrame) -> pd.DataFrame:
    """把沪市成交整理成逐笔流的十列：每一行是 TRADE，方向留空（成交涉及买卖两方）。"""
    n = len(trades)
    part = pd.DataFrame()
    part["seq"] = pd.array([pd.NA] * n, dtype="Int64")
    part["time"] = trades["time"].astype("int64")
    part["event"] = "TRADE"
    part["side"] = ""
    part["order_type"] = ""
    part["price"] = trades["price"].astype("int64")
    part["qty"] = trades["qty"].astype("int64")
    part["order_no"] = pd.array([pd.NA] * n, dtype="Int64")
    part["buy_no"] = trades["buy_no"].astype("Int64")
    part["sell_no"] = trades["sell_no"].astype("Int64")
    return part


def add_sort_columns(tick_by_tick: pd.DataFrame, trade_before_add: bool = True) -> pd.DataFrame:
    """
    给每一行加两列排序用的辅助列（单独拿出来，方便在 Notebook 里看）：
        key   这一行在“订单号这条线”上的位置。订单号按到达顺序递增，所以能当先后用。
              ADD           -> 自己的订单号
              TRADE         -> 买卖双方较大的订单号（较大的一方是后到的主动方）
              CANCEL        -> 被撤的订单号（另一边是 0，取较大值正好就是它）
        rank  key 相同时谁先（数字小的排前面）：
              股票 / ETF：成交 0，ADD 1，撤单 2（先成交，剩余量才挂上去）
              可转债：    ADD 0，成交 1，撤单 2（ADD 是完整量，先挂再扣）

    本函数只贴 key 和 rank 两列，不排序；排序在 sort_sh_tick_by_tick 里。
    trade_before_add 是从最外层 load_sh_tick_by_tick 一路传下来的，用来选下面哪张 rank 对照表。
    """
    result = tick_by_tick.copy()
    is_add = result["event"] == "ADD"

    bigger_no = result[["buy_no", "sell_no"]].max(axis=1)
    result["key"] = bigger_no
    result.loc[is_add, "key"] = result.loc[is_add, "order_no"]
    result["key"] = result["key"].astype("int64")

    # rank 对照表：event -> 数字。股票 / ETF 传 True，可转债传 False，两张表只是 TRADE 和 ADD 对调
    if trade_before_add:
        rank_of = {"TRADE": 0, "ADD": 1, "CANCEL": 2}
    else:
        rank_of = {"ADD": 0, "TRADE": 1, "CANCEL": 2}
    # .map(对照表)：event 列每个值都去表里查一下，换成对应的数字
    result["rank"] = result["event"].map(rank_of)
    return result


def sort_sh_tick_by_tick(tick_by_tick: pd.DataFrame, trade_before_add: bool = True) -> pd.DataFrame:
    """
    沪市没有 seq：按（time, key, rank）排序，再从 1 开始编 seq。
    trade_before_add 从 load_sh_tick_by_tick 传进来，这里不用，原样交给 add_sort_columns。
    time 是唯一的全局时钟，key 和 rank 只负责分出同一毫秒里谁先谁后。
    """
    result = add_sort_columns(tick_by_tick, trade_before_add)
    # kind="stable"：三列都相同的行保持原来的先后，不会被随便打乱
    result = result.sort_values(["time", "key", "rank"], kind="stable", ignore_index=True)
    result["seq"] = pd.array(range(1, len(result) + 1), dtype="Int64")
    return result[TICK_BY_TICK_COLUMNS]


if __name__ == "__main__":
    main()
