# SeatBot — 超星图书馆座位自动预约

详见 [设计文档](docs/superpowers/specs/2026-07-08-library-seat-reservation-design.md) 与 [实施计划](docs/superpowers/plans/2026-07-08-library-seat-reservation.md)。

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

## 部署

### 本地 (Windows / macOS / Linux)

```bash
git clone <repo>
cd Library-Seat-Reservation
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium         # enc fallback 用的浏览器
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入手机号/密码/座位
python -m seatbot init-db
python -m seatbot run               # 访问 http://localhost:8080
```

### 云服务器 (Linux + Docker)

```bash
# 在服务器上:
git clone <repo> && cd Library-Seat-Reservation
mkdir -p data logs
cp config.example.yaml data/config.yaml
# 上传 / 编辑 data/config.yaml
docker compose up -d
# 访问 http://<server-ip>:8080
```

### Linux systemd

```bash
sudo useradd -r -s /bin/false seatbot
sudo cp -r . /opt/seatbot
sudo chown -R seatbot:seatbot /opt/seatbot
cd /opt/seatbot && sudo -u seatbot python -m venv .venv
sudo -u seatbot /opt/seatbot/.venv/bin/pip install -e .
sudo cp systemd/seatbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seatbot
sudo journalctl -u seatbot -f
```

## 安全提示

- `config.yaml` 含明文账号密码, `chmod 600 config.yaml`
- Web 面板无鉴权, **仅限内网访问**
- 建议在前面套 nginx + basic auth 或仅监听 127.0.0.1

## 已知风险

- 纯 HTTP 签到, 缺少蓝牙/位置, 超星风控可能识别为作弊 → 账号可能进黑名单
- 多账号并发, 同一 IP 多账号, 容易被识别为脚本
- 用户已明确接受这些风险

## 开发

```bash
pytest -v                   # 单元测试
pytest -m integration -v    # 集成测试 (需真实账号)
ruff check seatbot tests
```