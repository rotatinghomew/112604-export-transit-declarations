# 整车拼报拆单与分票核放

拼报和行级核放结果决定逐车出区依据及离场后处置。

`samples/` 保存原始合成样本和字段约定，《交换资料约定》说明同一对象的编号、
时间及来源。现在仓库已提供代理、报关复核、场站共用的接续办理服务
（纯 Python 标准库）：归并口径校验、复核失效、逐行回告、拆单重报、
唯一有效依据、撤回与离场后关务处置、组班/替车逐验、待卸待留场清单和
单证岗申报行对账。完整规则与接口对照见 [`业务规则.md`](业务规则.md)。

## 运行环境

使用 Python 3.11.2。服务与资料检查均只用标准库，无需下载安装包。由项目根目录
运行以下命令，可以检查合成资料能否解析、引用能否对应，并运行业务规则测试：

```sh
python3 -m unittest discover -s tests -v
```

## 看样本结论

```sh
python3 -m yardflow                         # 中文值班/单证报告（全部资料最晚时点）
python3 -m yardflow --at 2026-11-27T02:05:00Z
python3 -m yardflow --at 2026-11-27T03:20:00Z   # V311 的 P-18 已过期
python3 -m yardflow --line A-700/L1         # 单证岗按申报行对账
python3 -m yardflow --json                  # 完整 JSON（可重定向存档）
python3 -m yardflow.server --port 8080      # 可选：HTTP/JSON 服务与看板
```

样本讲清了四辆车的处置：

- **V310**：凭 P-18 于 01:48 实际离场，01:52 口岸只撤回该车的 P-18 证据
  （K2 回执 02:01 才上传）。过岗时依据有效，属离场后撤回，交**关务处置**，
  任何时点都不改回在库可装。
- **V311**：与 V310 同属 A-700/L1、共用 P-18，撤回只列 V310，故 V311 依据仍有效，
  03:10（`until`）前可核放；拖过有效期后进入“已装车无依据——待卸/留场”，
  须重新取得依据。
- **V312**：发票改版（8703-B→8703-A），A-700/L2 被要求修订并最终
  `closed_without_permit`；重新复核后可拆出与 V313 合报新行。
- **V313**：已在库签认但尚未复核申报，没有出场依据。

## 在代码中使用

```python
from yardflow import Service, load_project

svc = load_project("samples")
svc.releasable_vins("2026-11-27T02:05:00Z")      # ['V311']
svc.unsupported_staged("2026-11-27T03:20:00Z")   # V311 / permit_expired
svc.line_dossier("A-700", "L1")                  # 组成车、实用 permit、待办、后续处置
svc.split_and_redeclare("A-700", "L2", "A-701", "L1", merge_vins=["V313"])
```

规范实现位于 `yardflow/service.py`；根目录 `service.py` 以及 `app.py`、
`engine.py`、`domain.py` 等是同一 API 的兼容入口。
