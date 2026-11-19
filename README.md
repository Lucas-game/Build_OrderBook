# Build_OrderBook：A 股逐笔数据重建订单簿

用 L2 逐笔委托和逐笔成交，按顺序回放，重建出一只证券一天中任意时刻的订单簿，并和交易所十档快照逐档对账。支持深交所（主板、创业板）和上交所（股票、ETF、可转债）。

## 思路

1. **拼逐笔流**：把 `orders.csv`（逐笔委托）和 `trades.csv`（逐笔成交）整理成同一张十列的表，排好先后。两个交易所的原始数据记录方式不同，各写各的。
2. **建订单簿**：沿逐笔流一行一行回放，每一行改一次订单簿（三本字典：`orders` 账本，`bids` / `asks` 盘口）。这一步深沪共用。

| 文件 | 作用 |
| --- | --- |
| `lob_rebuild/src/build_orderbook.py` | 公共：回放 `build_order_book`、订单簿 `OrderBook`、对账 `compare_with_snapshot` |
| `lob_rebuild/src/sz_stock_build_orderbook.py` | 深交所：委托和成交共用 seq，按 seq 合并 |
| `lob_rebuild/src/sh_stock_build_orderbook.py` | 上交所：没有 seq、ADD 是剩余量、有幽灵订单，用订单号造顺序 |
| `lob_rebuild/notebooks/订单簿重建演示.ipynb` | 综合演示 |
| `README_数据说明.md` | 数据说明：原始数据 → 逐笔流 → 订单簿 |
| `SZ_订单簿重建教程.html` / `SH_订单簿重建教程.html` | 深市、沪市教程（下载后用浏览器打开） |

## 运行

```bash
cd lob_rebuild
pip install -r requirements.txt
python3 src/sz_stock_build_orderbook.py
python3 src/sh_stock_build_orderbook.py
```

数据文件不随仓库发布。把数据放进仓库根目录的 `data/`，例如 `data/SZ_000001_20260302/`（文件夹说明见 `data/README.md`），每个文件夹里有 `orders.csv`、`trades.csv`、`snapshots.csv`（统一格式，价格单位 0.0001 元）。

## 验证结果（收盘一刻十档对账）

| 数据 | 收盘十档 | 剩余量变负 |
| --- | --- | --- |
| 深市 000001、300750 | 10 / 10 | 0 |
| 沪市 603353（股票）、518880（ETF）、110093（可转债） | 10 / 10 | 0 |
