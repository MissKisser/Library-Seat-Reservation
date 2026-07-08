# SeatBot — 超星图书馆座位自动预约

详见 [设计文档](../specs/2026-07-08-library-seat-reservation-design.md) 与 [实施计划](../plans/2026-07-08-library-seat-reservation.md)。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入账号
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
```

## 已知风险

- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置, 超星风控可能识别为作弊, 账号可能被拉入黑名单
- 多账号并发: 同一 IP 多账号, 容易被识别为脚本

## 命令

```bash
python -m seatbot run --config config.yaml
python -m seatbot once --account zhangsan --action all
python -m seatbot login --account zhangsan
python -m seatbot status
python -m seatbot init-db
```