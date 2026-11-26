"""整车拼报拆单与分票核放 —— 代理 / 报关复核 / 场站接续办理服务。

只用 Python 标准库。核心对象：

* ``Invoice`` 行记录（含 revision，有效版本取最大 revision）
* ``YardPresence`` 场站在库核验
* ``Receipt`` 口岸按受理号、申报行发回的回告（authorized / asked_to_amend /
  revoked / closed_without_permit）
* ``FleetEvent`` 班车回执（staged / departed / unloaded）

所有查询都可以传 ``at``（ISO 字符串或 datetime）得到某一时点的确定结论；
不传时使用显式 clock，再否则取全部资料中的最晚时间。

设计要点（对应业务约定）：

* 同一申报行只能归并申报主体(exporter)、贸易方式、商品编码、计量单位一致，
  且资料已经复核、场站在库核对过的车；金额按实际发票行合计核对。
* 发票改版或代理授权变化会使既有复核失效，必须重新确认受影响资料。
* 一份核放(permit)在同一时刻对同一 VIN 至多有一份有效依据；出现两份时判为
  依据冲突，不允许挑一份放行。
* 回告只覆盖其 event 中列出的 VIN 集合；revoked 只撤回所列 VIN 的证据。
* 班车事实按业务发生时间 ``at`` 还原，延迟上传的 departed 回执也要按实际
  过岗时间判断；已经离场是终态，证据事后被撤回只产生关务处置，绝不回退为
  在库可装。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 常量与错误码
# ---------------------------------------------------------------------------

DISPOSITIONS = {"asked_to_amend", "authorized", "revoked", "closed_without_permit"}
FLEET_ACTIONS = {"staged", "departed", "unloaded"}

# 归并申报行必须完全一致的口径字段。申报主体在交换资料里即出口企业 exporter。
GROUP_FIELDS = ("exporter", "trade", "commodity", "unit")
GROUP_LABELS = {
    "exporter": "申报主体/出口企业",
    "trade": "贸易方式",
    "commodity": "商品编码",
    "unit": "计量单位",
}

# 问题码 -> 中文说明（单证岗 / 值班员界面可直接展示）。
ISSUE_TEXT = {
    "unknown_vin": "资料中没有该车的发票记录",
    "yard_not_verified": "缺少场站在库 VIN 核验，不能提交申报/核放",
    "yard_sighting_in_future": "场站核验时间晚于办理时间",
    "already_departed": "该车已经过岗离场，不能改回在库或重新排班",
    "already_on_run": "该车已排在另一辆班车上，须先卸车或并班",
    "not_on_run": "该车不在该班车上",
    "run_not_confirmed": "班车尚未完成逐辆核验确认",
    "no_permit": "尚未取得口岸准予核放",
    "permit_not_effective": "核放回告尚未到生效时间",
    "permit_expired": "核放回告已过有效期限，须重新取得有效依据",
    "permit_revoked": "核放证据已被口岸撤回，不得继续使用",
    "multiple_valid_bases": "同一辆车同时存在两份有效出场依据，须先查清旧依据",
    "basis_conflict": "出场依据冲突未解决",
    "permit_mismatch": "出场实际使用的核放与当前唯一有效依据不一致",
    "unknown_permit": "使用了口岸回告中不存在的核放编号",
    "departed_on_revoked_permit": "车辆凭已被撤回的证据出区，按异常出区交关务处置",
    "departed_on_expired_permit": "车辆凭已过期的回执出区，按异常出区交关务处置",
    "departed_without_valid_permit": "车辆出区时没有有效核放依据，按异常出区交关务处置",
    "permit_revoked_after_exit": "车辆实际离场后核放证据被撤回，交关务处置，不得改回在库可装",
    "invoice_revision_unconfirmed": "发票行已改版，既有确认失效，须按当前版本重新复核",
    "invoice_revision_needs_review": "发票存在多个版本，须按最新 revision 复核确认",
    "agent_authorization_changed": "代理授权发生变化，须重新确认受影响资料",
    "materials_not_confirmed": "申报资料尚未经复核确认",
    "grouping_mismatch": "归并口径不一致，不能合入同一申报行",
    "amount_mismatch": "申报金额与实际发票行金额合计不一致",
    "agent_not_authorized": "该代理不是车辆发票资料上当前持有授权的企业",
    "line_not_submittable": "申报行存在未满足的提交条件",
    "line_pending_port_result": "口岸尚未给清该行结果，不能假定旧行车辆已核放或窗口已关闭",
    "line_amend_requested": "口岸要求该行修订后再报",
    "line_closed_without_permit": "该行已被口岸关闭且未准予，所列 VIN 原未核放行程结束，须重新申报",
    "materials_changed_after_submission": "申报后发票/授权资料发生变化，旧回告只覆盖提交时资料，受影响车辆须改单或重新申报",
    "staged_without_basis": "已装上班车但没有本批有效核放依据，须查明待卸或留场处理",
    "yard_hold": "车辆被场站留场挂起，解除前不能排班核放",
    "receipt_conflict": "口岸回告与已记录申报行不一致",
    "duplicate_event": "重复事件",
}


class RuleViolation(Exception):
    """办理动作违反业务规则。``code`` 对应 ISSUE_TEXT 中的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None, details: dict | None = None):
        super().__init__(message or ISSUE_TEXT.get(code, code))
        self.code = code
        self.message = message or ISSUE_TEXT.get(code, code)
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.message, "details": self.details}


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ts_text(value: datetime | str | None) -> str | None:
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return ts_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(value)
    return value


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


@dataclass
class Confirmation:
    vin: str
    fingerprint: tuple
    reviewer: str | None
    at: datetime
    yard_seen: datetime | None
    yard_clerk: str | None

    def to_dict(self) -> dict:
        return _jsonable(
            {
                "vin": self.vin,
                "fingerprint": list(self.fingerprint),
                "reviewer": self.reviewer,
                "at": self.at,
                "yard_seen": self.yard_seen,
                "yard_clerk": self.yard_clerk,
            }
        )


class Service:
    """代理、报关复核、场站共用的接续办理服务。

    数据保存在内存中；交换资料通过 ``ingest_*`` / :meth:`load_directory` 进入，
    办理动作（复核、归并、提交、组班、替车、处理）走同一套规则。
    """

    def __init__(self, clock: str | datetime | None = None):
        self._clock = parse_ts(clock)
        # 交换资料（按记录键去重，重复投递幂等）
        self.invoice_rows: dict[tuple, dict] = {}
        self.yard: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.fleet: dict[str, dict] = {}
        # 办理记录
        self.confirmations: dict[str, Confirmation] = {}
        self.agent_overrides: dict[str, tuple[str, datetime]] = {}
        self.holds: dict[str, dict] = {}
        self.submissions: dict[str, dict] = {}
        self.line_index: dict[tuple[str, str], dict] = {}
        self.runs: dict[str, dict] = {}
        self._manual_seq = 0

    # ----- 时钟 -----------------------------------------------------------

    def _resolve_at(self, at: str | datetime | None) -> datetime:
        dt = parse_ts(at)
        if dt is not None:
            return dt
        if self._clock is not None:
            return self._clock
        candidates: list[datetime] = []
        for row in self.yard.values():
            candidates.append(row["seen_dt"])
        for row in self.receipts.values():
            candidates.append(row["at_dt"])
        for row in self.fleet.values():
            candidates.append(row["at_dt"])
            up = parse_ts(row.get("uploaded"))
            if up:
                candidates.append(up)
        if candidates:
            return max(candidates)
        return datetime.now(timezone.utc)

    def set_clock(self, clock: str | datetime) -> None:
        self._clock = parse_ts(clock)

    # =====================================================================
    # 资料接入
    # =====================================================================

    def load_directory(self, directory: str | Path) -> dict:
        """读取约定的四个 JSONL feed；返回各类记录条数。"""
        root = Path(directory)
        counts = {}
        loaders = {
            "invoices": self.ingest_invoice,
            "customs_receipts": self.ingest_receipt,
            "fleet_reports": self.ingest_fleet,
            "yard_presence": self.ingest_yard_presence,
        }
        for name, loader in loaders.items():
            path = root / f"{name}.jsonl"
            n = 0
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        loader(json.loads(line))
                        n += 1
            counts[name] = n
        return counts

    def ingest_invoice(self, row: dict) -> dict:
        key = (row["invoice"], row["line"], row["vin"], row["revision"])
        if key in self.invoice_rows:
            return self.invoice_rows[key]
        stored = dict(row)
        self.invoice_rows[key] = stored
        # 新发票版本/代理变化会使既有复核失效：这里不删记录，而由 fingerprint
        # 比对判定 stale；同时保留审计轨迹。
        return stored

    def ingest_yard_presence(self, row: dict) -> dict:
        vin = row["vin"]
        stored = dict(row)
        stored["seen_dt"] = parse_ts(row["seen"])
        self.yard[vin] = stored  # 记录键为 vin，后到即当前实车核验
        return stored

    def ingest_receipt(self, row: dict) -> dict:
        event = row["event"]
        if event in self.receipts:
            return self.receipts[event]
        if row["disposition"] not in DISPOSITIONS:
            raise RuleViolation("receipt_conflict", f"未知 disposition: {row['disposition']}")
        stored = dict(row)
        stored["at_dt"] = parse_ts(row["at"])
        stored["until_dt"] = parse_ts(row.get("until"))
        stored["vins"] = list(row.get("vins", []))
        self.receipts[event] = stored
        key = (row["submission"], row["line"])
        if key not in self.line_index:
            self._observe_line(row["submission"], row["line"], first_receipt=stored)
        line = self.line_index[key]
        line["receipt_events"].append(event)
        if row["disposition"] == "authorized" and row.get("permit"):
            line["permit"] = row["permit"]
        return stored

    def ingest_fleet(self, row: dict) -> dict:
        event = row["event"]
        if event in self.fleet:
            return self.fleet[event]
        if row["action"] not in FLEET_ACTIONS:
            raise RuleViolation("receipt_conflict", f"未知 action: {row['action']}")
        stored = dict(row)
        stored["at_dt"] = parse_ts(row["at"])
        stored["uploaded_dt"] = parse_ts(row.get("uploaded"))
        self.fleet[event] = stored
        # 同步维护 run 档案（事实层）
        run = row["run"]
        run_rec = self.runs.setdefault(
            run, {"run": run, "source": "fleet", "members": {}, "events": []}
        )
        run_rec["events"].append(event)
        return stored

    def _observe_line(self, submission: str, line_id: str, first_receipt: dict) -> dict:
        """口岸回告里出现、但本服务没有申报快照的行，按回告 VIN 集合建档。"""
        vins = list(first_receipt.get("vins", []))
        grouping = self._grouping_for_vins(vins)
        try:
            amount_total = self.invoice_total(vins) if vins else None
        except RuleViolation:
            amount_total = None
        record = {
            "submission": submission,
            "line": line_id,
            "external": True,
            "created_at": first_receipt["at_dt"],
            "agent": None,
            "vins": vins,
            "snapshot": {},
            "grouping": grouping,
            "amount_cents": amount_total,
            "declared_amount_cents": None,
            "receipt_events": [],
            "permit": None,
            "provenance": None,
        }
        self.submissions.setdefault(
            submission, {"submission": submission, "at": first_receipt["at_dt"], "agent": None,
                         "lines": {}}
        )
        self.submissions[submission]["lines"][line_id] = record
        self.line_index[(submission, line_id)] = record
        return record

    # =====================================================================
    # 发票 / 口径
    # =====================================================================

    def invoice_revisions(self, vin: str) -> list[dict]:
        rows = [r for r in self.invoice_rows.values() if r["vin"] == vin]
        return sorted(rows, key=lambda r: (r["revision"], str(r["invoice"]), r["line"]))

    def current_invoice(self, vin: str) -> dict | None:
        revisions = self.invoice_revisions(vin)
        return revisions[-1] if revisions else None

    def invoice_total(self, vins: Iterable[str]) -> int:
        total = 0
        for vin in vins:
            row = self.current_invoice(vin)
            if row is None:
                raise RuleViolation("unknown_vin", details={"vin": vin})
            total += int(row["USD_cents"])
        return total

    def effective_agent(self, vin: str) -> str | None:
        if vin in self.agent_overrides:
            return self.agent_overrides[vin][0]
        row = self.current_invoice(vin)
        return row["agent"] if row else None

    def authorize_agent(self, vin: str, agent: str, at: str | datetime | None = None) -> None:
        """登记新的代理授权；授权变化会让该车既有复核失效（fingerprint 变化）。"""
        when = self._resolve_at(at)
        self.agent_overrides[vin] = (agent, when)

    def _materials_fingerprint(self, vin: str) -> tuple:
        row = self.current_invoice(vin)
        if row is None:
            raise RuleViolation("unknown_vin", details={"vin": vin})
        return (
            row["invoice"],
            row["line"],
            row["revision"],
            row["exporter"],
            row["trade"],
            row["commodity"],
            row["unit"],
            row["USD_cents"],
            self.effective_agent(vin),
        )

    def _grouping_for_vins(self, vins: list[str]) -> dict | None:
        rows = [self.current_invoice(v) for v in vins]
        if not rows or any(r is None for r in rows):
            return None
        first = rows[0]
        return {field: first[field] for field in GROUP_FIELDS}

    def check_grouping(self, vins: list[str]) -> dict:
        """核对若干 VIN 能否归并到同一申报行（只看四项口径，不看车型近似）。"""
        errors: list[dict] = []
        rows: dict[str, dict] = {}
        for vin in vins:
            row = self.current_invoice(vin)
            if row is None:
                errors.append({"code": "unknown_vin", "vin": vin})
                continue
            rows[vin] = row
        mismatches: list[dict] = []
        if len(rows) >= 2:
            base_vin = next(iter(rows))
            base = rows[base_vin]
            for field in GROUP_FIELDS:
                bad = [v for v, r in rows.items() if r[field] != base[field]]
                if bad:
                    mismatches.append(
                        {
                            "field": field,
                            "label": GROUP_LABELS[field],
                            "base_vin": base_vin,
                            "base_value": base[field],
                            "other_vins": bad,
                            "other_values": {v: rows[v][field] for v in bad},
                        }
                    )
        if mismatches:
            errors.append({"code": "grouping_mismatch", "mismatches": mismatches})
        return {
            "ok": not errors,
            "vins": list(vins),
            "grouping": self._grouping_for_vins(vins) if not mismatches else None,
            "mismatches": mismatches,
            "errors": errors,
        }

    def prepare_line(self, vins: list[str], amount_cents: int | None = None,
                     agent: str | None = None, at: str | datetime | None = None,
                     require_confirmed: bool = True) -> dict:
        """申报行预检：口径、复核、在库、代理授权、金额一次核清。"""
        when = self._resolve_at(at)
        result: dict[str, Any] = {"ok": True, "vins": list(vins), "checks": [], "errors": []}

        grouping = self.check_grouping(vins)
        result["grouping"] = grouping.get("grouping")
        if not grouping["ok"]:
            result["ok"] = False
            result["errors"].extend(grouping["errors"])

        # 金额按实际发票行（当前有效版本）逐辆合计
        try:
            total = self.invoice_total(vins)
            result["invoice_total_cents"] = total
            if amount_cents is not None and int(amount_cents) != total:
                result["ok"] = False
                result["errors"].append(
                    {"code": "amount_mismatch", "declared_cents": amount_cents,
                     "invoice_total_cents": total}
                )
            result["amount_cents"] = amount_cents if amount_cents is not None else total
        except RuleViolation as exc:
            result["ok"] = False
            result["errors"].append(exc.to_dict())

        for vin in vins:
            item = {"vin": vin, "problems": []}
            row = self.current_invoice(vin)
            if row is None:
                item["problems"].append("unknown_vin")
            else:
                if require_confirmed and not self.is_confirmed(vin):
                    item["problems"].append(self.confirmation_problem(vin) or "materials_not_confirmed")
                yard = self.yard.get(vin)
                if yard is None:
                    item["problems"].append("yard_not_verified")
                elif yard["seen_dt"] > when:
                    item["problems"].append("yard_sighting_in_future")
                car_agent = self.effective_agent(vin)
                if agent is not None and car_agent != agent:
                    item["problems"].append("agent_not_authorized")
            if item["problems"]:
                result["ok"] = False
                result["errors"].append({"vin": vin, "codes": item["problems"]})
            result["checks"].append(item)
        return result

    # =====================================================================
    # 报关复核 + 场站在库核对
    # =====================================================================

    def confirmation_problem(self, vin: str) -> str | None:
        """当前资料下复核是否已失效；返回问题码或 None。"""
        conf = self.confirmations.get(vin)
        if conf is None:
            return "materials_not_confirmed"
        try:
            fingerprint = self._materials_fingerprint(vin)
        except RuleViolation:
            return "unknown_vin"
        if conf.fingerprint != fingerprint:
            # 区分发票变化与代理授权变化，便于单证岗列待办
            row = self.current_invoice(vin)
            old = dict(
                zip(
                    ("invoice", "line", "revision", "exporter", "trade", "commodity",
                     "unit", "USD_cents", "agent"),
                    conf.fingerprint,
                )
            )
            invoice_keys = ("invoice", "line", "revision", "exporter", "trade",
                             "commodity", "unit", "USD_cents")
            if any(old.get(k) != row.get(k) for k in invoice_keys):
                return "invoice_revision_unconfirmed"
            return "agent_authorization_changed"
        return None

    def is_confirmed(self, vin: str) -> bool:
        return self.confirmation_problem(vin) is None

    def confirm_materials(self, vin: str, reviewer: str | None = None,
                          at: str | datetime | None = None,
                          require_yard: bool = True) -> Confirmation:
        """验核人核对场站在库 VIN 后复核确认当前发票/申报资料。"""
        when = self._resolve_at(at)
        if self.current_invoice(vin) is None:
            raise RuleViolation("unknown_vin", details={"vin": vin})
        yard = self.yard.get(vin)
        if require_yard:
            if yard is None:
                raise RuleViolation("yard_not_verified", details={"vin": vin})
            if yard["seen_dt"] > when:
                raise RuleViolation("yard_sighting_in_future", details={"vin": vin})
        conf = Confirmation(
            vin=vin,
            fingerprint=self._materials_fingerprint(vin),
            reviewer=reviewer,
            at=when,
            yard_seen=yard["seen_dt"] if yard else None,
            yard_clerk=yard["clerk"] if yard else None,
        )
        self.confirmations[vin] = conf
        return conf

    # 贴近不同角色用语的别名
    review = confirm_materials
    confirm = confirm_materials

    # =====================================================================
    # 代理申报：受理号 / 申报行 / 拼报 / 拆单
    # =====================================================================

    def create_submission(self, submission: str, lines: Iterable, *,
                          agent: str | None = None,
                          at: str | datetime | None = None,
                          submit: bool = True) -> dict:
        """建立一个受理号下的若干申报行。

        ``lines`` 每项为 ``(line_id, [vins])`` 或
        ``{"line": "L1", "vins": [...], "amount_cents": 123}``。
        任一行不满足归并/复核/在库/金额/授权条件则整体拒绝（不产生半截申报）。
        """
        when = self._resolve_at(at)
        normalized: list[dict] = []
        for spec in lines:
            if isinstance(spec, dict):
                normalized.append(spec)
            else:
                line_id, vins = spec
                normalized.append({"line": line_id, "vins": list(vins)})

        previews = []
        for spec in normalized:
            preview = self.prepare_line(
                spec["vins"],
                amount_cents=spec.get("amount_cents"),
                agent=spec.get("agent", agent),
                at=when,
            )
            preview["line"] = spec["line"]
            previews.append(preview)
        bad = [p for p in previews if not p["ok"]]
        if bad:
            raise RuleViolation(
                "line_not_submittable",
                details={"lines": _jsonable(bad)},
            )

        sub = self.submissions.setdefault(
            submission,
            {"submission": submission, "at": when, "agent": agent, "lines": {}},
        )
        sub["at"] = when
        sub["agent"] = agent or sub.get("agent")
        created = []
        for spec, preview in zip(normalized, previews):
            line_id = spec["line"]
            vins = list(spec["vins"])
            record = {
                "submission": submission,
                "line": line_id,
                "external": False,
                "created_at": when,
                "agent": spec.get("agent", agent),
                "vins": vins,
                "snapshot": {
                    vin: {
                        "fingerprint": list(self._materials_fingerprint(vin)),
                        "invoice": self.current_invoice(vin),
                    }
                    for vin in vins
                },
                "grouping": preview["grouping"],
                "amount_cents": preview["invoice_total_cents"],
                "declared_amount_cents": preview.get("amount_cents"),
                "receipt_events": [],
                "permit": None,
                "submitted": submit,
                "provenance": spec.get("provenance"),
            }
            sub["lines"][line_id] = record
            self.line_index[(submission, line_id)] = record
            created.append(record)
        return {"submission": submission, "lines": [r["line"] for r in created], "at": when}

    def record_submission(self, submission: str, lines: Iterable, *,
                          agent: str | None = None,
                          at: str | datetime | None = None) -> dict:
        """补录一个外部已经提交的受理号/申报行（不强制本地复核状态）。

        用于把样本中的 A-700 等已有受理资料接入本服务；规则结论仍以口岸回告为准。
        """
        when = self._resolve_at(at)
        normalized = []
        for spec in lines:
            if isinstance(spec, dict):
                normalized.append(dict(spec))
            else:
                line_id, vins = spec
                normalized.append({"line": line_id, "vins": list(vins)})
        sub = self.submissions.setdefault(
            submission, {"submission": submission, "at": when, "agent": agent, "lines": {}}
        )
        out = []
        for spec in normalized:
            line_id = spec["line"]
            vins = list(spec["vins"])
            total = self.invoice_total(vins) if all(self.current_invoice(v) for v in vins) else None
            record = {
                "submission": submission,
                "line": line_id,
                "external": True,
                "created_at": parse_ts(spec.get("at")) or when,
                "agent": spec.get("agent", agent),
                "vins": vins,
                "snapshot": spec.get("snapshot", {}),
                "grouping": self._grouping_for_vins(vins),
                "amount_cents": total,
                "declared_amount_cents": spec.get("amount_cents", total),
                "receipt_events": [],
                "permit": None,
                "submitted": True,
                "provenance": spec.get("provenance"),
            }
            sub["lines"][line_id] = record
            self.line_index[(submission, line_id)] = record
            out.append(line_id)
        return {"submission": submission, "lines": out}

    def split_and_redeclare(self, old_submission: str, old_line: str,
                            new_submission: str, new_line: str,
                            rejected_vins: list[str] | None = None,
                            merge_vins: list[str] | None = None, *,
                            agent: str | None = None,
                            amount_cents: int | None = None,
                            at: str | datetime | None = None,
                            submit: bool = True) -> dict:
        """修订/拆单：把被驳车辆从旧行拆出，与其他有效业务合报为新行。

        前置条件只依据口岸明确回告：旧行/车辆处于 asked_to_amend 或
        closed_without_permit（或调用方显式指定 rejected_vins 且这些车没有
        仍有效的依据）。服务不会臆测旧行里哪些车已核放、窗口是否关闭。
        合入的其他车同样要满足归并、复核、在库、金额规则。
        """
        when = self._resolve_at(at)
        old = self.line_index.get((old_submission, old_line))
        if old is None:
            raise RuleViolation("receipt_conflict",
                                details={"submission": old_submission, "line": old_line})
        old_vins = list(old["vins"])
        if rejected_vins is None:
            rejected_vins = [v for v in old_vins
                             if self.vin_portrait(v, when)["line_outcome"] in
                             ("amend_requested", "closed_without_permit")]
            if not rejected_vins:
                raise RuleViolation(
                    "line_pending_port_result",
                    f"{old_submission}/{old_line} 尚没有 asked_to_amend / "
                    "closed_without_permit 的明确结果；哪些车已核放、窗口是否关闭"
                    "必须等口岸回告，或由调用方显式指定 rejected_vins",
                )
        rejected_vins = list(rejected_vins or [])
        merge_vins = list(merge_vins or [])
        for vin in rejected_vins:
            if vin not in old_vins:
                raise RuleViolation("receipt_conflict",
                                    f"{vin} 不在 {old_submission}/{old_line} 行内")
            basis = self.basis_for(vin, when)
            if basis["state"] == "unique":
                raise RuleViolation(
                    "multiple_valid_bases",
                    f"{vin} 在拆单时点仍持有有效依据 {basis['permit']}；"
                    "口岸未明确结束旧依据前不能再报出第二张依据",
                    details={"basis": basis},
                )
        combined = rejected_vins + merge_vins
        if not combined:
            raise RuleViolation("line_not_submittable", "拆单后没有可申报车辆")
        provenance = {"split_from": f"{old_submission}/{old_line}"}
        result = self.create_submission(
            new_submission,
            [{"line": new_line, "vins": combined, "amount_cents": amount_cents,
              "provenance": provenance}],
            agent=agent, at=when, submit=submit,
        )
        return result

    # =====================================================================
    # 口岸回告 / permit 依据
    # =====================================================================

    def permit_timeline(self, permit: str, vin: str) -> dict:
        """某 permit 对某 VIN 的授权与撤回时间线。

        撤回回告通常带同一 permit；若撤回记录 permit 为空但明确列出该 VIN，
        按约定它结束所列 VIN 的原证据，也计入该 VIN 现有 permit 的撤回。
        """
        grants, revokes = [], []
        for row in self.receipts.values():
            if vin not in row.get("vins", []):
                continue
            row_permit = row.get("permit") or ""
            if row["disposition"] == "authorized":
                if permit and row_permit != permit:
                    continue
                grants.append(row)
            elif row["disposition"] == "revoked":
                if row_permit and permit and row_permit != permit:
                    continue
                revokes.append(row)
        grants.sort(key=lambda r: r["at_dt"])
        revokes.sort(key=lambda r: r["at_dt"])
        grant = grants[-1] if grants else None
        revoked_at = revokes[0]["at_dt"] if revokes else None
        untils = [g["until_dt"] for g in grants if g.get("until_dt") is not None]
        return {
            "permit": permit,
            "vin": vin,
            "known": grant is not None or bool(revokes),
            "authorized_at": grants[0]["at_dt"] if grants else None,
            "until": max(untils) if untils else None,
            "grant_event": grant["event"] if grant else None,
            "revoked_at": revoked_at,
            "revoke_events": [r["event"] for r in revokes],
            "grant_count": len(grants),
        }

    def basis_for(self, vin: str, at: str | datetime | None = None) -> dict:
        """出场依据判定的 JSON 可序列化版本（时间字段为 ISO 字符串）。"""
        return _jsonable(self._basis_for(vin, at))

    def _basis_for(self, vin: str, at: str | datetime | None = None) -> dict:
        """返回某时点 VIN 的出场依据判定。

        state 取值：unique（恰好一份有效）/ none（无有效，code 说明原因）/
        conflict（两份及以上有效）。
        """
        when = self._resolve_at(at)
        active: list[dict] = []
        histories: list[dict] = []
        seen_permits: set[str] = set()
        for row in self.receipts.values():
            if row["disposition"] != "authorized":
                continue
            permit = row.get("permit")
            if not permit or permit in seen_permits:
                continue
            if vin not in row.get("vins", []):
                continue
            seen_permits.add(permit)
            tl = self.permit_timeline(permit, vin)
            histories.append(tl)
            if tl["authorized_at"] is not None and tl["authorized_at"] > when:
                continue  # 尚未生效
            if tl["revoked_at"] is not None and tl["revoked_at"] <= when:
                continue
            if tl["until"] is not None and when > tl["until"]:
                continue
            active.append({
                "permit": permit,
                "authorized_at": tl["authorized_at"],
                "until": tl["until"],
            })
        if len(active) == 1:
            return {"state": "unique", "permit": active[0]["permit"],
                    "basis": active[0], "active": active, "history": histories, "at": when}
        if len(active) > 1:
            return {"state": "conflict", "code": "multiple_valid_bases",
                    "permits": [a["permit"] for a in active], "active": active,
                    "history": histories, "at": when}

        # 没有有效依据：给出最贴切的原因（优先撤回 > 过期 > 未生效 > 从未准予）
        code = "no_permit"
        relevant = [h for h in histories if h["authorized_at"] is not None]
        if relevant:
            def rank(h):
                if h["revoked_at"] is not None and h["revoked_at"] <= when:
                    return 0
                if h["until"] is not None and when > h["until"]:
                    return 1
                if h["authorized_at"] > when:
                    return 2
                return 3
            h = sorted(relevant, key=rank)[0]
            if h["revoked_at"] is not None and h["revoked_at"] <= when:
                code = "permit_revoked"
            elif h["until"] is not None and when > h["until"]:
                code = "permit_expired"
            elif h["authorized_at"] > when:
                code = "permit_not_effective"
        # asked_to_amend / closed_without_permit 也能解释为什么没有依据
        line_outcome = self.vin_portrait(vin, when).get("line_outcome")
        if code == "no_permit" and line_outcome == "closed_without_permit":
            code = "line_closed_without_permit"
        elif code == "no_permit" and line_outcome == "amend_requested":
            code = "line_amend_requested"
        return {"state": "none", "code": code, "permit": None,
                "active": [], "history": histories, "at": when}

    def active_permit(self, vin: str, at: str | datetime | None = None) -> str | None:
        decision = self.basis_for(vin, at)
        return decision["permit"] if decision["state"] == "unique" else None

    # =====================================================================
    # 班车事实还原 / 物理状态
    # =====================================================================

    def fleet_timeline(self, vin: str) -> list[dict]:
        events = [r for r in self.fleet.values() if r["vin"] == vin]
        return sorted(events, key=lambda r: (r["at_dt"], r["event"]))

    def physical_state(self, vin: str, at: str | datetime | None = None,
                       *, use_uploaded: bool = False) -> dict:
        """按业务发生时间（默认）还原 VIN 在 ``at`` 时点的场站/班车状态。

        use_uploaded=True 时按资料上传时间还原“值班岗位当时能看到什么”。
        """
        when = self._resolve_at(at)
        events = self.fleet_timeline(vin)
        state = "in_yard"
        current_run = None
        staged = None
        departure = None
        history: list[dict] = []
        anomalies: list[dict] = []
        for ev in events:
            moment = ev["uploaded_dt"] if use_uploaded else ev["at_dt"]
            if moment is None or moment > when:
                continue
            action = ev["action"]
            history.append({"event": ev["event"], "action": action, "run": ev["run"],
                            "at": ev["at_dt"], "uploaded": ev.get("uploaded_dt"),
                            "permit": ev.get("permit")})
            if departure is not None:
                anomalies.append({"code": "event_after_departure", "event": ev["event"]})
                continue
            if action == "staged":
                state = "staged"
                current_run = ev["run"]
                staged = {"run": ev["run"], "at": ev["at_dt"], "event": ev["event"],
                          "permit_presented": ev.get("permit")}
            elif action == "unloaded":
                state = "in_yard"
                current_run = None
                staged = None
            elif action == "departed":
                state = "departed"
                current_run = ev["run"]
                departure = {"run": ev["run"], "at": ev["at_dt"], "event": ev["event"],
                             "permit_used": ev.get("permit"),
                             "uploaded_at": ev.get("uploaded_dt")}
                staged = None
        return {"state": state, "run": current_run, "staged": staged,
                "departure": departure, "history": history, "anomalies": anomalies}

    def current_run_of_vin(self, vin: str, at=None) -> str | None:
        phys = self.physical_state(vin, at)
        return phys["run"] if phys["state"] == "staged" else None

    # =====================================================================
    # 逐车核放 / 组班 / 临时替车
    # =====================================================================

    def verify_vehicle(self, vin: str, at: str | datetime | None = None,
                       *, for_run: str | None = None,
                       use_uploaded: bool = False) -> dict:
        """值班员逐辆核验：在库、未离场、无挂起、唯一有效依据、未占其他班车。

        ``use_uploaded=True`` 时物理状态按回执上传时间还原（“岗位当时能看到什么”），
        默认按业务发生时间还原事实状态。
        """
        when = self._resolve_at(at)
        checks: list[dict] = []
        errors: list[str] = []

        def add(check: str, ok: bool, error_code: str | None = None, **extra):
            item = {"check": check, "ok": ok}
            if not ok:
                item["code"] = error_code or check
                item["message"] = ISSUE_TEXT.get(item["code"], item["code"])
            item.update(extra)
            checks.append(item)
            if not ok:
                errors.append(error_code or check)

        row = self.current_invoice(vin)
        add("known_vin", row is not None, "unknown_vin")
        if row is None:
            return _jsonable({"vin": vin, "at": when, "ok": False, "checks": checks,
                              "errors": errors, "basis": None, "physical_state": "unknown"})

        yard = self.yard.get(vin)
        if yard is None:
            add("yard_presence", False, "yard_not_verified")
        elif yard["seen_dt"] > when:
            add("yard_presence", False, "yard_sighting_in_future",
                clerk=yard.get("clerk"), seen=yard.get("seen_dt"))
        else:
            add("yard_presence", True,
                clerk=yard.get("clerk"), seen=yard.get("seen_dt"))

        phys = self.physical_state(vin, when, use_uploaded=use_uploaded)
        add("not_departed", phys["state"] != "departed", "already_departed",
            departure=phys.get("departure"))

        hold = self.holds.get(vin)
        add("no_yard_hold", hold is None, "yard_hold", hold=hold)

        basis = self.basis_for(vin, when)
        if basis["state"] == "unique":
            add("unique_valid_basis", True, permit=basis["permit"],
                until=basis["basis"]["until"])
        elif basis["state"] == "conflict":
            add("unique_valid_basis", False, "multiple_valid_bases",
                permits=basis["permits"])
        else:
            add("unique_valid_basis", False, basis["code"])

        if for_run is not None:
            occupied_run = None
            if phys["state"] == "staged" and phys["run"] != for_run:
                occupied_run = phys["run"]
            else:
                # 值班台已确认加入另一辆班车（未确认的失败组班不占车）
                for rid, rec in self.runs.items():
                    if rid != for_run and rec.get("confirmed") and vin in rec.get("members", {}):
                        occupied_run = rid
                        break
            if occupied_run is not None:
                add("run_availability", False, "already_on_run", run=occupied_run)

        ok = not errors
        return _jsonable({"vin": vin, "at": when, "ok": ok, "checks": checks,
                          "errors": errors,
                          "basis": basis if basis["state"] != "none" else None,
                          "basis_decision": basis,
                          "physical_state": phys["state"], "physical": phys})

    def is_releasable(self, vin: str, at: str | datetime | None = None) -> bool:
        return self.verify_vehicle(vin, at)["ok"]

    def releasable_vins(self, at: str | datetime | None = None, *,
                        free_only: bool = False) -> list[str]:
        when = self._resolve_at(at)
        result = []
        for vin in self._all_vins():
            verdict = self.verify_vehicle(vin, when)
            if not verdict["ok"]:
                continue
            if free_only and verdict["physical"]["state"] == "staged":
                continue
            result.append(vin)
        return result

    def form_run(self, run: str, vins: list[str], at: str | datetime | None = None,
                 *, substitutes: list[str] | None = None, confirm: bool = True,
                 strict: bool = False) -> dict:
        """值班员用当前可核放车辆组成班车；确认车和临时替车都逐辆核验。

        默认返回每辆车的核验结果（``ok`` 为全车是否通过）；``strict=True`` 时
        只要有一辆不通过就抛 :class:`RuleViolation`。
        """
        when = self._resolve_at(at)
        substitutes = set(substitutes or [])
        members = list(dict.fromkeys(vins))
        rec = self.runs.setdefault(run, {"run": run, "source": "desk", "members": {},
                                         "events": [], "formed_at": when})
        results = []
        for vin in members:
            verdict = self.verify_vehicle(vin, when, for_run=run)
            rec["members"][vin] = {
                "vin": vin,
                "substitute": vin in substitutes,
                "added_at": when,
                "last_verification": _jsonable(verdict),
            }
            results.append(verdict)
        ok_all = all(v["ok"] for v in results)
        rec["last_formation"] = when
        if confirm:
            rec["confirmed_at"] = when if ok_all else None
            rec["confirmed"] = ok_all
        if strict and confirm and not ok_all:
            bad = [v["vin"] for v in results if not v["ok"]]
            raise RuleViolation("run_not_confirmed",
                                f"班车 {run} 有车辆未通过逐辆核验: {bad}",
                                details={"results": results})
        return _jsonable({"run": run, "at": when, "ok": ok_all, "results": results})

    def add_to_run(self, run: str, vin: str, at: str | datetime | None = None,
                   *, substitute: bool = True) -> dict:
        """加入临时替车（或补员），同样逐辆核验，不允许“先装车后补验”。"""
        when = self._resolve_at(at)
        rec = self.runs.setdefault(run, {"run": run, "source": "desk", "members": {},
                                         "events": []})
        verdict = self.verify_vehicle(vin, when, for_run=run)
        if not verdict["ok"]:
            raise RuleViolation(
                verdict["errors"][0] if verdict["errors"] else "line_not_submittable",
                details={"verdict": _jsonable(verdict)},
            )
        rec["members"][vin] = {"vin": vin, "substitute": substitute, "added_at": when,
                               "last_verification": _jsonable(verdict)}
        return verdict

    def confirm_run(self, run: str, at: str | datetime | None = None) -> dict:
        """发车前再次逐辆核验（排班可能拖过回执有效期或收到撤回）。"""
        when = self._resolve_at(at)
        rec = self.runs.get(run)
        if rec is None:
            raise RuleViolation("not_on_run", details={"run": run})
        results = []
        for vin in list(rec["members"]):
            verdict = self.verify_vehicle(vin, when, for_run=run)
            rec["members"][vin]["last_verification"] = _jsonable(verdict)
            results.append(verdict)
        ok_all = all(v["ok"] for v in results)
        rec["confirmed"] = ok_all
        rec["confirmed_at"] = when if ok_all else None
        return _jsonable({"run": run, "at": when, "ok": ok_all, "results": results})

    def remove_from_run(self, run: str, vin: str) -> None:
        rec = self.runs.get(run)
        if rec and vin in rec.get("members", {}):
            del rec["members"][vin]

    def form_releasable_run(self, run: str, at: str | datetime | None = None, *,
                            free_only: bool = True,
                            substitutes: list[str] | None = None,
                            strict: bool = False) -> dict:
        """值班员“用当前可核放的车重新组成班车”。

        默认只选未排在其他班车上的可核放车；选出后仍逐辆核验。
        """
        vins = self.releasable_vins(at, free_only=free_only)
        return self.form_run(run, vins, at, substitutes=substitutes, strict=strict)

    # ----- 场站“待卸 / 留场”处理 --------------------------------------

    def unsupported_staged(self, at: str | datetime | None = None,
                           run: str | None = None, *,
                           use_uploaded: bool = False) -> list[dict]:
        """已装车（staged）但在 ``at`` 没有本批有效依据的车。

        每辆车给出原因与可接续的处理：卸车(unload) 或 留场(hold_yard)。
        已 departed / unloaded 的车不在此列；旧装车清单不能作为出区依据。
        ``use_uploaded=True`` 按上传时间还原岗位当时可见的装车状态。
        """
        when = self._resolve_at(at)
        out = []
        for vin in self._all_vins():
            phys = self.physical_state(vin, when, use_uploaded=use_uploaded)
            if phys["state"] != "staged":
                continue
            if run is not None and phys["run"] != run:
                continue
            verdict = self.verify_vehicle(vin, when, for_run=phys["run"],
                                          use_uploaded=use_uploaded)
            if verdict["ok"]:
                continue
            codes = [c for c in verdict["errors"]
                     if c not in ("already_on_run",)]
            code = codes[0] if codes else "staged_without_basis"
            out.append({
                "vin": vin,
                "run": phys["run"],
                "staged_at": phys["staged"]["at"],
                "event": phys["staged"]["event"],
                "permit_presented": phys["staged"].get("permit_presented"),
                "code": "staged_without_basis",
                "basis_code": code,
                "reason": ISSUE_TEXT.get(code, code),
                "handling": ["unload", "hold_yard"],
                "errors": verdict["errors"],
            })
        return _jsonable(out)

    def mark_handling(self, vin: str, handling: str, *, clerk: str | None = None,
                      note: str | None = None, at: str | datetime | None = None) -> dict:
        """对无依据装车登记现场处理：``unload`` 生成卸车回执；``hold_yard`` 挂留场。"""
        when = self._resolve_at(at)
        if handling == "unload":
            phys = self.physical_state(vin, when)
            run_id = phys["run"] or "YARD"
            event_id = self._manual_event_id("U")
            row = {"event": event_id, "run": run_id, "vin": vin, "permit": "",
                   "action": "unloaded", "at": ts_text(when),
                   "uploaded": ts_text(when), "clerk": clerk, "note": note}
            self.ingest_fleet(row)
            return row
        if handling == "hold_yard":
            self.holds[vin] = {"vin": vin, "at": when, "clerk": clerk, "note": note}
            return self.holds[vin]
        raise RuleViolation("line_not_submittable", f"未知处理方式 {handling}")

    def release_hold(self, vin: str) -> None:
        self.holds.pop(vin, None)

    def _manual_event_id(self, prefix: str) -> str:
        self._manual_seq += 1
        return f"M-{prefix}{self._manual_seq:03d}"

    # ----- 发车命令（强校验；回执接入走 ingest_fleet 保留事实） ----------

    def depart(self, run: str, vin: str, permit: str | None = None, *,
               at: str | datetime | None = None, clerk: str | None = None) -> dict:
        """值班/卡口办理出场：必须在该班车上且持有当时唯一有效依据。"""
        when = self._resolve_at(at)
        verdict = self.verify_vehicle(vin, when, for_run=run)
        if not verdict["ok"]:
            raise RuleViolation(verdict["errors"][0],
                                details={"verdict": _jsonable(verdict)})
        phys = verdict["physical"]
        if phys["state"] != "staged" and run not in self.runs:
            raise RuleViolation("not_on_run", details={"vin": vin, "run": run})
        if phys["state"] == "staged" and phys["run"] != run:
            raise RuleViolation("already_on_run", details={"vin": vin, "run": phys["run"]})
        basis_permit = verdict["basis"]["permit"] if verdict.get("basis") else None
        used = permit or basis_permit
        if permit is not None and permit != basis_permit:
            raise RuleViolation("permit_mismatch",
                                details={"used": permit, "valid": basis_permit})
        event_id = self._manual_event_id("D")
        row = {"event": event_id, "run": run, "vin": vin, "permit": used,
               "action": "departed", "at": ts_text(when), "uploaded": ts_text(when),
               "clerk": clerk}
        self.ingest_fleet(row)
        return row

    def stage(self, run: str, vin: str, permit: str | None = None, *,
              at: str | datetime | None = None) -> dict:
        """登记装车/排班（允许先装后查，但无依据车会进入 unsupported_staged）。"""
        when = self._resolve_at(at)
        event_id = self._manual_event_id("K")
        row = {"event": event_id, "run": run, "vin": vin,
               "permit": permit or self.active_permit(vin, when) or "",
               "action": "staged", "at": ts_text(when), "uploaded": ts_text(when)}
        self.ingest_fleet(row)
        return row

    # =====================================================================
    # 出区回执核对：实际使用哪份 permit、异常与离场后处置
    # =====================================================================

    def departure_audit(self, vin: str, at: str | datetime | None = None) -> dict | None:
        """核对 VIN 实际出场使用的核放及其合法性、以及离场后的证据变化。

        只审计 ``at`` 时点（默认全部资料的最晚时点）之前已经发生的出场事实；
        更早离场、更晚才上传的回执在接入后按回执里的业务时间核对。
        """
        when = self._resolve_at(at)
        phys_all = self.physical_state(vin, when)
        dep = phys_all.get("departure")
        if dep is None:
            return None
        dep_at = dep["at"]
        used = dep.get("permit_used") or ""
        issues: list[dict] = []

        # 1) 过岗当时，所用 permit 是否有效且唯一
        decision_then = self.basis_for(vin, dep_at)
        valid_permit_then = decision_then["permit"] if decision_then["state"] == "unique" else None
        if not used:
            issues.append({"code": "departed_without_valid_permit", "at": dep_at})
        else:
            tl = self.permit_timeline(used, vin)
            if not tl["known"] or tl["authorized_at"] is None:
                issues.append({"code": "unknown_permit", "permit": used, "at": dep_at})
            elif tl["authorized_at"] > dep_at:
                issues.append({"code": "permit_not_effective", "permit": used, "at": dep_at})
            elif tl["revoked_at"] is not None and tl["revoked_at"] <= dep_at:
                issues.append({"code": "departed_on_revoked_permit", "permit": used,
                               "revoked_at": tl["revoked_at"], "at": dep_at})
            elif tl["until"] is not None and dep_at > tl["until"]:
                issues.append({"code": "departed_on_expired_permit", "permit": used,
                               "until": tl["until"], "at": dep_at})
            elif decision_then["state"] == "conflict":
                issues.append({"code": "multiple_valid_bases",
                               "permits": decision_then["permits"], "at": dep_at})
            elif valid_permit_then and used != valid_permit_then:
                issues.append({"code": "permit_mismatch", "used": used,
                               "valid_permit": valid_permit_then, "at": dep_at})

        # 2) 离场之后证据是否被撤回（哪怕过岗时完全合法）
        later_revokes = []
        if used:
            tl = self.permit_timeline(used, vin)
            if tl["revoked_at"] is not None and tl["revoked_at"] > dep_at:
                later_revokes.append({"permit": used, "revoked_at": tl["revoked_at"],
                                      "events": tl["revoke_events"]})
                issues.append({"code": "permit_revoked_after_exit", "permit": used,
                               "revoked_at": tl["revoked_at"], "departed_at": dep_at})
        # 也检查同 VIN 其他 permit 在离场后被撤回的情况（覆盖回告只列子集）
        for row in self.receipts.values():
            if (row["disposition"] == "revoked" and vin in row.get("vins", [])
                    and row["at_dt"] > dep_at
                    and not any(x["permit"] == row.get("permit") for x in later_revokes)):
                later_revokes.append({"permit": row.get("permit"),
                                      "revoked_at": row["at_dt"], "events": [row["event"]]})
                if not any(i["code"] == "permit_revoked_after_exit" for i in issues):
                    issues.append({"code": "permit_revoked_after_exit",
                                   "permit": row.get("permit"),
                                   "revoked_at": row["at_dt"], "departed_at": dep_at})

        # 3) 后续处置：只要涉及撤回/异常出区，交关务；物理状态永不回退
        if any(i["code"] == "permit_revoked_after_exit" for i in issues):
            follow_up = {
                "owner": "customs_affairs",
                "code": "customs_disposition_after_revocation",
                "instruction": "车辆确已过岗离场，撤回后的证据不得再用于发车；"
                               "本车交关务处置，不得改回在库可装。",
                "revocations": later_revokes,
            }
        elif issues:
            follow_up = {
                "owner": "customs_affairs",
                "code": "irregular_exit_review",
                "instruction": "出区时依据异常，交关务/卡口核查；车辆已离场，不回退在库。",
            }
        else:
            follow_up = {"owner": "fleet_archive", "code": "released_with_basis",
                         "instruction": "凭有效依据正常核放出区，归档。"}
        return {
            "vin": vin,
            "run": dep["run"],
            "departed_at": dep_at,
            "event": dep["event"],
            "uploaded_at": dep.get("uploaded_at"),
            "permit_used": used or None,
            "valid_basis_at_gate": valid_permit_then,
            "issues": issues,
            "follow_up": follow_up,
        }

    # =====================================================================
    # 申报行 / VIN 画像与单证岗档案
    # =====================================================================

    def _all_vins(self) -> list[str]:
        vins = {r["vin"] for r in self.invoice_rows.values()}
        vins.update(self.yard.keys())
        vins.update(r["vin"] for r in self.fleet.values())
        vins.update(v for line in self.line_index.values() for v in line["vins"])
        return sorted(vins)

    def vin_portrait(self, vin: str, at: str | datetime | None = None) -> dict:
        """汇总 VIN 所属申报行、口岸结果、物理状态与待办（结论限定在 ``at`` 前）。"""
        when = self._resolve_at(at)
        memberships = [key for key, line in self.line_index.items() if vin in line["vins"]]
        # 最近一次申报行（按建档时间）
        current_key = None
        if memberships:
            current_key = sorted(memberships,
                                 key=lambda k: self.line_index[k]["created_at"])[-1]
        line_outcome = None
        line_receipts: list[dict] = []
        if current_key:
            line = self.line_index[current_key]
            line_receipts = [self.receipts[e] for e in line["receipt_events"]
                             if e in self.receipts and self.receipts[e]["at_dt"] <= when]
            mine = [r for r in line_receipts if vin in r.get("vins", [])]
            dispositions = [r["disposition"] for r in mine]
            if "closed_without_permit" in dispositions:
                line_outcome = "closed_without_permit"
            elif any(d == "revoked" for d in dispositions):
                line_outcome = "revoked"
            elif "authorized" in dispositions:
                line_outcome = "authorized"
            elif "asked_to_amend" in dispositions:
                line_outcome = "amend_requested"
            elif line.get("submitted"):
                line_outcome = "submitted"
            else:
                line_outcome = "draft"
        return {"current_line": current_key, "line_outcome": line_outcome,
                "memberships": memberships}

    def vin_dossier(self, vin: str, at: str | datetime | None = None) -> dict:
        """单证岗单车对账：发票版本、复核、依据、实际 permit、问题待办、后续处置。"""
        when = self._resolve_at(at)
        inv = self.current_invoice(vin)
        conf_problem = self.confirmation_problem(vin) if vin in self.confirmations else (
            "materials_not_confirmed" if inv else "unknown_vin")
        basis = self.basis_for(vin, when) if inv else None
        phys = self.physical_state(vin, when)
        audit = self.departure_audit(vin, when)
        portrait = self.vin_portrait(vin, when)

        issues: list[dict] = []
        todos: list[dict] = []

        if inv is None:
            issues.append({"code": "unknown_vin"})
        elif conf_problem == "invoice_revision_unconfirmed":
            issues.append({"code": "invoice_revision_unconfirmed",
                           "current_revision": inv["revision"]})
            todos.append({"code": "reconfirm_invoice",
                          "text": "发票改版使既有确认失效，按最新 revision 重新复核受影响资料"})
        elif conf_problem == "agent_authorization_changed":
            issues.append({"code": "agent_authorization_changed"})
            todos.append({"code": "reconfirm_authorization",
                          "text": "代理授权已变化，重新确认授权资料"})
        elif conf_problem == "materials_not_confirmed":
            revisions = self.invoice_revisions(vin)
            prior_declaration = portrait["line_outcome"] not in (None, "draft")
            if len(revisions) > 1:
                # 只有交换资料、没有本地复核记录时，明确提示按最新版本复核
                issues.append({"code": "invoice_revision_needs_review",
                               "current_revision": inv["revision"],
                               "revisions": [r["revision"] for r in revisions]})
                todos.append({"code": "review_latest_invoice_revision",
                              "text": f"发票存在 {len(revisions)} 个 revision，"
                                      f"须按最新 revision {inv['revision']} 复核确认"})
            elif not prior_declaration and (basis is None or basis["state"] != "unique"):
                # 从未进入申报流程的车，提示初始复核；已有回告历史的车，
                # 当前动作由 basis/line_outcome 的续期、改单、重报待办给出。
                todos.append({"code": "confirm_materials",
                              "text": "复核申报资料并核对在库 VIN"})

        yard = self.yard.get(vin)
        if yard is None:
            issues.append({"code": "yard_not_verified"})
            todos.append({"code": "verify_yard_vin", "text": "场站核对在库 VIN 并签认"})

        if phys["state"] == "departed":
            # 已离场：不再列装车/核放待办，只保留后续处置
            audit_issues = audit["issues"] if audit else []
            return _jsonable({
                "vin": vin,
                "invoice": inv,
                "physical_state": "departed",
                "run": audit["run"] if audit else phys["run"],
                "permit_used": audit["permit_used"] if audit else None,
                "basis_at_query": basis,
                "line": portrait["current_line"],
                "line_outcome": portrait["line_outcome"],
                "issues": issues + audit_issues,
                "todos": [],
                "departure_audit": audit,
                "follow_up": audit["follow_up"] if audit else None,
                "releasable": False,
                "at": when,
            })

        # 未出区车辆：依据/申报/装车问题即错误或待办
        if basis is not None:
            if basis["state"] == "conflict":
                issues.append({"code": "multiple_valid_bases", "permits": basis["permits"]})
                todos.append({"code": "resolve_basis_conflict",
                              "text": "联系口岸确认旧依据已撤回/关闭后再放行，不得二选一"})
            elif basis["state"] == "none":
                code = basis["code"]
                issues.append({"code": code})
                if code == "permit_expired":
                    todos.append({"code": "renew_permit",
                                  "text": "排班错过回执有效期，重新申报/取得有效依据后再发车"})
                elif code == "permit_revoked":
                    todos.append({"code": "redeclare_after_revocation",
                                  "text": "证据已撤回，确需出区的重新申报取得新依据"})
                elif code in ("line_amend_requested",):
                    todos.append({"code": "amend_line",
                                  "text": "按口岸修订意见改单，可拆出被驳车辆与其他有效业务合报"})
                elif code == "line_closed_without_permit":
                    todos.append({"code": "redeclare_closed_line",
                                  "text": "该行已关闭且未准予，拆出车辆重新归并申报"})
                else:
                    todos.append({"code": "obtain_permit", "text": "完成复核申报并取得口岸准予回执"})

        if phys["state"] == "staged":
            unsupported = [u for u in self.unsupported_staged(when) if u["vin"] == vin]
            if unsupported:
                u = unsupported[0]
                issues.append({"code": "staged_without_basis", "basis_code": u["basis_code"]})
                todos.append({"code": "unload_or_hold",
                              "text": "已装上班车但无有效依据：安排卸车，或留场处理；"
                                      "不得沿用旧装车清单出区",
                              "options": ["unload", "hold_yard"]})
        if vin in self.holds:
            issues.append({"code": "yard_hold", "hold": self.holds[vin]})
            todos.append({"code": "close_yard_hold", "text": "留场事项处置完毕后解除挂起"})

        verdict = self.verify_vehicle(vin, when)
        return _jsonable({
            "vin": vin,
            "invoice": inv,
            "confirmed": self.is_confirmed(vin),
            "confirmation_problem": conf_problem,
            "yard": yard,
            "physical_state": phys["state"],
            "physical": phys,
            "run": phys["run"],
            "basis": basis,
            "line": portrait["current_line"],
            "line_outcome": portrait["line_outcome"],
            "issues": issues,
            "todos": todos,
            "departure_audit": audit,
            "follow_up": None,
            "releasable": verdict["ok"],
            "at": when,
        })

    def line_dossier(self, submission: str, line: str,
                     at: str | datetime | None = None) -> dict:
        """单证岗按申报行对账：组成车辆、每辆实际使用的核放、未出区错误待办、
        已离场车辆的后续处置。"""
        when = self._resolve_at(at)
        key = (submission, line)
        rec = self.line_index.get(key)
        if rec is None:
            raise KeyError(f"未知申报行 {submission}/{line}")
        vins = list(rec["vins"])
        vin_rows = [self.vin_dossier(v, when) for v in vins]

        # 行状态：逐车口岸结果聚合（一行准予/一行修订互不遮挡）
        outcomes = [self.vin_portrait(v, when)["line_outcome"] for v in vins]
        living = [v for v in vins if self.physical_state(v, when)["state"] != "departed"]
        if all(o == "authorized" for o in outcomes) and all(
                self.basis_for(v, when)["state"] == "unique" for v in living):
            status = "authorized"
        elif outcomes and all(o == "closed_without_permit" for o in outcomes):
            status = "closed_without_permit"
        elif "amend_requested" in outcomes:
            status = "amend_requested"
        elif any(o == "revoked" for o in outcomes):
            status = "partially_or_fully_revoked"
        else:
            status = "mixed_or_pending"

        open_issues, open_todos, customs = [], [], []
        for v in vin_rows:
            for issue in v.get("issues", []):
                open_issues.append({"vin": v["vin"], **issue})
            for todo in v.get("todos", []):
                open_todos.append({"vin": v["vin"], **todo})
            fu = v.get("follow_up")
            if fu and fu.get("owner") == "customs_affairs":
                customs.append({"vin": v["vin"], **fu})

        receipts = [self.receipts[e] for e in rec["receipt_events"]
                    if e in self.receipts and self.receipts[e]["at_dt"] <= when]
        amount_total = rec.get("amount_cents")
        declared = rec.get("declared_amount_cents")
        amount_matches = None if amount_total is None or declared is None else amount_total == declared

        vehicle_rows = []
        for v in vin_rows:
            # 申报快照与当前资料是否一致：回告只覆盖当时提交的资料
            snap = (rec.get("snapshot") or {}).get(v["vin"])
            snapshot_current = None
            extra_issues, extra_todos = [], []
            if snap and v.get("invoice"):
                current_fp = list(self._materials_fingerprint(v["vin"]))
                snapshot_current = list(snap.get("fingerprint", ())) == current_fp
                if not snapshot_current:
                    extra_issues.append({"code": "materials_changed_after_submission"})
                    extra_todos.append({
                        "code": "amend_changed_materials",
                        "text": "申报后发票或授权已变化，旧回告不覆盖新资料，"
                                "须改单或拆出重新申报并重新复核"})
                    open_issues.append({"vin": v["vin"], **extra_issues[-1]})
                    open_todos.append({"vin": v["vin"], **extra_todos[-1]})
            vehicle_rows.append({
                "vin": v["vin"],
                "invoice_key": (
                    f"{v['invoice']['invoice']}/{v['invoice']['line']}"
                    if v.get("invoice") else None
                ),
                "revision": v["invoice"]["revision"] if v.get("invoice") else None,
                "physical_state": v["physical_state"],
                "run": v.get("run"),
                "permit_used_on_departure": (
                    v.get("permit_used")
                    or (v.get("departure_audit") or {}).get("permit_used")
                    if v["physical_state"] == "departed" else None
                ),
                "valid_basis": (
                    v["basis"].get("permit")
                    if v.get("basis") and v["basis"].get("state") == "unique" else None
                ),
                "releasable": v.get("releasable", False),
                "snapshot_current": snapshot_current,
                "issues": v.get("issues", []) + extra_issues,
                "todos": v.get("todos", []) + extra_todos,
                "follow_up": v.get("follow_up"),
            })

        return _jsonable({
            "submission": submission,
            "line": line,
            "status": status,
            "external_record": rec.get("external", False),
            "agent": rec.get("agent") or (self.submissions.get(submission, {}) or {}).get("agent"),
            "grouping": rec.get("grouping"),
            "amounts": {
                "invoice_total_cents": amount_total,
                "declared_cents": declared,
                "matches": amount_matches,
            },
            "provenance": rec.get("provenance"),
            "receipts": [
                {"event": r["event"], "disposition": r["disposition"],
                 "permit": r.get("permit") or None, "vins": r["vins"], "at": r["at_dt"],
                 "until": r.get("until_dt")}
                for r in receipts
            ],
            "composition": vins,
            "vehicles": vehicle_rows,
            "open_issues": open_issues,
            "open_todos": open_todos,
            "customs_affairs": customs,
            "at": when,
        })

    def dossiers(self, at: str | datetime | None = None) -> list[dict]:
        when = self._resolve_at(at)
        return [self.line_dossier(s, l, when) for (s, l) in sorted(self.line_index)]

    # =====================================================================
    # 全场值班/单证总报告
    # =====================================================================

    def report(self, at: str | datetime | None = None) -> dict:
        when = self._resolve_at(at)
        vins = self._all_vins()
        vin_rows = [self.vin_dossier(v, when) for v in vins]
        conflicts = [
            {"vin": v, "permits": self.basis_for(v, when).get("permits", [])}
            for v in vins if self.basis_for(v, when)["state"] == "conflict"
        ]
        return _jsonable({
            "as_of": when,
            "vins": vin_rows,
            "releasable_vins": self.releasable_vins(when),
            "basis_conflicts": conflicts,
            "unsupported_staged": self.unsupported_staged(when),
            "runs": {rid: self.run_view(rid, when) for rid in sorted(self.runs)},
            "lines": self.dossiers(when),
            "customs_affairs": [
                {"vin": v["vin"], "follow_up": v["follow_up"]}
                for v in vin_rows
                if v.get("follow_up") and v["follow_up"].get("owner") == "customs_affairs"
            ],
            "todos": [
                {"vin": v["vin"], **todo}
                for v in vin_rows for todo in v.get("todos", [])
            ],
        })

    def run_view(self, run: str, at: str | datetime | None = None) -> dict:
        when = self._resolve_at(at)
        rec = self.runs[run]
        # 以班车事实事件还原的成员为准，合并值班员组班记录
        phys_members = {}
        for vin in self._all_vins():
            phys = self.physical_state(vin, when)
            if phys["run"] == run and phys["state"] in ("staged", "departed"):
                phys_members[vin] = phys
        desk_members = rec.get("members", {})
        members = sorted(set(phys_members) | set(desk_members))
        return _jsonable({
            "run": run,
            "source": rec.get("source"),
            "confirmed": rec.get("confirmed"),
            "confirmed_at": rec.get("confirmed_at"),
            "members": [
                {
                    "vin": vin,
                    "substitute": desk_members.get(vin, {}).get("substitute", False),
                    "physical_state": self.physical_state(vin, when)["state"],
                    "verification_ok": self.verify_vehicle(vin, when)["ok"]
                    if self.physical_state(vin, when)["state"] != "departed" else False,
                }
                for vin in members
            ],
            "unsupported": [u for u in self.unsupported_staged(when, run=run)],
        })

    # ----- 常见调用命名的等价别名 ----------------------------------------
    can_release = is_releasable
    check_vehicle = verify_vehicle
    eligible_vins = releasable_vins
    get_releasable_vins = releasable_vins
    releasable_vehicles = releasable_vins
    get_verdict = verify_vehicle
    vin_status = vin_dossier
    status = vin_dossier
    dossier = line_dossier
    get_dossier = line_dossier
    get_line_dossier = line_dossier
    load = load_directory
    load_feeds = load_directory
    ingest_dir = load_directory


def load_project(directory: str | Path = "samples", clock: str | datetime | None = None) -> Service:
    """载入一个交换资料目录，返回可直接查询/办理的 :class:`Service`。"""
    svc = Service(clock=clock)
    svc.load_directory(directory)
    return svc


def build_report(directory: str | Path = "samples", at: str | datetime | None = None) -> dict:
    """便捷函数：载入资料目录并返回全场报告（JSON 可序列化）。"""
    return load_project(directory).report(at)


# 函数式调用习惯的别名
evaluate = build_report
process = build_report


# 常见命名别名，方便不同调用习惯
YardService = Service
Ledger = Service
Engine = Service
Project = Service
App = Service
YardEngine = Service
ReleaseService = Service
DeclarationService = Service
CustomsYardService = Service
CustomsService = Service
YardFlow = Service
