# akshare_retry_bot
日k级别金叉死叉量化复盘模拟器，把你意难平的交易操作告诉机器人，看结果会如何

# 量化 Web AKShare 版

这是一个浏览器界面的量化自动执行原型，默认使用 **AKShare 拉取 A 股历史日线数据** 做回放。

## 功能

- 浏览器界面，不依赖 Tkinter
- AKShare A 股历史日线回放
- CSV 回放
- 内置模拟行情
- 均线策略示例
- 模拟盘自动执行
- 实盘接口占位（需你自己接券商 API）
- 价格曲线 / 权益曲线 / 日志

## 安装

```bash
pip install -r requirements_quant_web_bot_akshare.txt
```

## 运行

```bash
python3 quant_web_bot_akshare.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## AKShare 模式说明

当前 AKShare 模式使用：

```python
ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20250101", end_date="20260331", adjust="qfq")
```

适合输入：

- 000001
- 600519
- 300750

当前版本 **只支持 6 位 A 股股票代码**。ETF、指数、港股没有接进来。

## 常见问题

### 1. 点启动后报未安装 akshare

执行：

```bash
pip install akshare pandas lxml html5lib requests
```

### 2. 点启动后提示没有返回数据

检查：

- 股票代码是否正确
- 日期区间是否合理
- 网络是否能访问 AKShare 对应数据源

### 3. 为什么不是实时行情

这个版本是 **历史数据回放**，不是实盘行情订阅。这样更适合先验证策略和自动执行流程。

## 风险提示

- 默认是模拟盘，不会真实下单
- `live` 模式只是占位，必须自行接入券商 API
- 当前策略只是演示用，不代表可盈利
