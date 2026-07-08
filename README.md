# SeatBot — 超星图书馆座位自动预约 (座位保护模式)

> 让某个**目标座位** (`target_seat_num`) 在 08:00-22:00 全天显示"已有人预约", 阻止他人抢走。
> 多守护账号 + 错时段重叠覆盖 = 全天护城河。

详见 [设计文档](docs/superpowers/specs/2026-07-08-library-seat-reservation-design.md) 与 [实施计划](docs/superpowers/plans/2026-07-08-library-seat-reservation.md)。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入 target_seat_num + 至少 2 个守护账号 + 错时段 slots
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
#   - 首页: Gantt 覆盖图 (列=30min 时段, 行=守护账号)
#   - 目标座位: 修改 target_seat_num
#   - 守护账号: 增删改 + 测试登录
#   - 覆盖报告: 检查失守时段
```

## 工作原理

1. **目标座位** (`library.target_seat_num`): 你实际要坐的座位号 (例如 84)
2. **守护账号** (1-N 个): 程序替你抢目标座位的辅助超星账号
3. **时段 slots**: 每账号配置若干 `"HH:MM-HH:MM"`, 程序自动拆为 ≤2h 子段
4. **覆盖**: 所有账号的 slots 合并应覆盖 08:00-22:00; 建议相邻账号首尾重叠 30min, 保证无缝衔接
5. **每日 14:00 起**: 程序自动抢次日全部时段
6. **签到/签退**: 每个时段 start_time 后 `pre_sign_duration`(默认 20min) 内签到, 接近 end_time 时签退并续约下一段

## 已知风险

- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置, 超星风控可能识别为作弊, 守护账号可能被拉入黑名单
- 多账号并发: 同一 IP 多账号, 容易被识别为脚本
- 用户已明确接受这些风险

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
# 编辑 config.yaml: 填入 target_seat_num + 守护账号 + 时段
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