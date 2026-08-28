# SeatBot 文档总览

本目录是项目文档的唯一存放位置。按照文档规范：**除本 README 外，`docs/` 下所有文档均不进入版本控制**，仅保留在本地。

## 目录结构

```
docs/
├── README.md                                  # 本文件（唯一入库的文档）
├── 2026-08-26-cross-day-booking-fix-plan.md   # 跨天预约修复计划（本地）
└── superpowers/
    ├── plans/      # 实施计划（本地）
    │   ├── 2026-07-08-library-seat-reservation.md
    │   └── 2026-07-09-frontend-enterprise-redesign.md
    ├── research/   # 技术调研（本地）
    │   └── 2026-07-08-chaoxing-seat-api.md
    └── specs/      # 设计规格（本地）
        ├── 2026-07-08-frontend-redesign-design.md
        ├── 2026-07-08-library-seat-reservation-design.md
        ├── 2026-07-09-chaoxing-api-reference.md
        ├── 2026-07-09-frontend-enterprise-redesign-design.md
        └── 2026-07-09-multi-seat-full-coverage-design.md
```

## 规范要点

1. 除根目录 `README.md`、本文件、`AGENTS.md`、`.github/` 模板与 `LICENSE` 外，任何文档不得提交到 Git。
2. 新增文档放入 `docs/` 对应子文件夹（如 `docs/api/`、`docs/admin/`、`docs/user/`），并在本 README 更新目录。
3. 文档命名须体现用途。
