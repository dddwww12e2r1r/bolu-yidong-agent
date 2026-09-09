# 波动率异动预警智能体

仓库前缀：`bolu-yidong-agent`

一个面向海外用户的中文加密市场风险观察工具：输入合约，优先读取 Hyperliquid Perpetual，失败时自动切换 OKX Swap，再失败时切换 Bybit Linear，计算 ATR、7 天年化历史波动率，并把当前状态分成平静、正常、活跃、剧烈四档。项目名称保留“币安作品6”，但数据不再依赖 Binance 接口。

## 核心功能

- 近 7 天历史波动率曲线
- 14 小时 ATR（以价格百分比展示）
- 当前 7 天波动率与近 30 天基准均值对比
- 波动率放大达到 1.8 倍或年化波动率达到 120% 时触发“波动率爆发”
- 深色、面向海外评委的风险监控界面
- Hyperliquid Perpetual 海外公开K线接口（优先）
- OKX Swap 海外公开K线接口（备用）
- Bybit Linear 海外公开K线接口（备用）

## 在线访问

已支持部署到海外公网环境。在线版本使用服务器端调用 Hyperliquid Perpetual 公共接口，失败时自动切换 OKX / Bybit；不依赖你的浏览器跨域权限。

## 本地启动

需要 Python 3.10+：

```bash
python agent.py --port 8002
```

浏览器打开：`http://127.0.0.1:8002`

## 说明

项目不需要 API Key，不自动交易。实时数据优先来自 Hyperliquid Perpetual，备用来自 OKX Swap 和 Bybit Linear；三个接口都不可达时才显示演示数据。指标用于市场观察与风险教育，不构成投资建议。
