"""业务规则与样本全流程测试。

运行（项目根目录）::

    python3 -m unittest discover -s tests -v

这些测试把《业务规则说明》和题目叙述中的关键场景固定下来：
拼报口径、金额核对、复核失效、在库核验、逐行回告、拆单重报、
唯一有效依据、撤回与离场后关务处置、组班替车逐验、有效期与单证档案。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"

from service import RuleViolation, Service  # noqa: E402

T_IN = "2026-11-27T02:05:00Z"
T_AFTER_EXPIRY = "2026-11-27T03:20:00Z"
UNTIL = "2026-11-27T05:00:00Z"


def read_jsonl(name: str):
    return [json.loads(line) for line in (SAMPLES / name).read_text("utf-8").splitlines() if line]


def loaded_service() -> Service:
    svc = Service()
    svc.load_directory(SAMPLES)
    return svc


def add_car(svc: Service, vin: str, **over) -> dict:
    row = {
        "invoice": f"INV-{vin}", "line": 1, "vin": vin, "revision": 1,
        "exporter": "E-1", "trade": "bonded", "commodity": "8703-A",
        "unit": "car", "USD_cents": 1_000_000, "agent": "A-1",
    }
    row.update(over)
    svc.ingest_invoice(row)
    svc.ingest_yard_presence(
        {"vin": vin, "zone": "K", "seen": "2026-11-27T00:00:00Z", "clerk": "Li"}
    )
    return row


def grant(svc: Service, event: str, submission: str, line: str, permit: str,
          vins: list[str], at: str = "2026-11-27T01:10:00Z", until: str = UNTIL) -> None:
    svc.ingest_receipt({
        "event": event, "submission": submission, "line": line, "vins": vins,
        "disposition": "authorized", "permit": permit, "at": at, "until": until,
    })


class FeedLoadingTest(unittest.TestCase):
    def test_loads_all_feeds_idempotently(self):
        svc = Service()
        counts = svc.load_directory(SAMPLES)
        self.assertEqual(counts, {
            "invoices": 5, "customs_receipts": 4, "fleet_reports": 3, "yard_presence": 4,
        })
        n_receipts = len(svc.receipts)
        svc.ingest_receipt(read_jsonl("customs_receipts.jsonl")[0])
        self.assertEqual(len(svc.receipts), n_receipts)

    def test_current_invoice_revision_is_max(self):
        svc = loaded_service()
        v312 = svc.current_invoice("V312")
        self.assertEqual(v312["revision"], 2)
        # 改版把商品编码从 8703-B 变成 8703-A
        self.assertEqual(v312["commodity"], "8703-A")
        self.assertEqual(svc.current_invoice("V310")["revision"], 1)


class GroupingRulesTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        add_car(self.svc, "C1", invoice="INV-A")
        add_car(self.svc, "C2", invoice="INV-B", USD_cents=2_000_000)

    def test_different_invoices_may_share_line(self):
        result = self.svc.check_grouping(["C1", "C2"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["grouping"]["commodity"], "8703-A")

    def test_group_requires_each_caliber_field_identical(self):
        for field, value in [
            ("exporter", "E-OTHER"),
            ("trade", "general"),
            ("commodity", "8703-Z"),
            ("unit", "set"),
        ]:
            svc = Service()
            add_car(svc, "C1")
            add_car(svc, "C3", **{field: value})
            result = svc.check_grouping(["C1", "C3"])
            self.assertFalse(result["ok"], field)
            self.assertEqual(result["errors"][0]["code"], "grouping_mismatch")
            self.assertEqual(result["errors"][0]["mismatches"][0]["field"], field)

    def test_similar_model_but_different_code_cannot_merge(self):
        # 样本里的关键对照：8703-B 与 8703-A 即使车型相近也不是同一申报口径
        svc = loaded_service()
        rev1_v312 = [r for r in svc.invoice_revisions("V312") if r["revision"] == 1][0]
        self.assertEqual(rev1_v312["commodity"], "8703-B")
        # 在 rev1 口径下构造归并检查
        svc.invoice_rows[("INV-2", 2, "V312", 2)]["commodity"] = "8703-B"
        try:
            result = svc.check_grouping(["V312", "V313"])
            self.assertFalse(result["ok"])
            fields = [m["field"] for e in result["errors"] for m in e["mismatches"]]
            self.assertIn("commodity", fields)
        finally:
            svc.invoice_rows[("INV-2", 2, "V312", 2)]["commodity"] = "8703-A"

    def test_amount_must_match_actual_invoice_lines_sum(self):
        preview = self.svc.prepare_line(["C1", "C2"], amount_cents=3_000_000,
                                        require_confirmed=False)
        self.assertTrue(preview["ok"])
        bad = self.svc.prepare_line(["C1", "C2"], amount_cents=2_999_999,
                                   require_confirmed=False)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["errors"][0]["code"], "amount_mismatch")
        self.assertEqual(bad["errors"][0]["invoice_total_cents"], 3_000_000)


class ReviewAndYardTest(unittest.TestCase):
    def test_unknown_vin_cannot_confirm(self):
        svc = Service()
        with self.assertRaises(RuleViolation) as ctx:
            svc.confirm_materials("GHOST")
        self.assertEqual(ctx.exception.code, "unknown_vin")

    def test_confirmation_requires_yard_presence(self):
        svc = Service()
        add_car(svc, "C1")
        svc.yard.clear()
        with self.assertRaises(RuleViolation) as ctx:
            svc.confirm_materials("C1", at="2026-11-27T01:00:00Z")
        self.assertEqual(ctx.exception.code, "yard_not_verified")

    def test_yard_sighting_must_precede_confirmation(self):
        svc = Service()
        add_car(svc, "C1")  # seen at 00:00
        with self.assertRaises(RuleViolation) as ctx:
            svc.confirm_materials("C1", at="2026-11-26T23:00:00Z")
        self.assertEqual(ctx.exception.code, "yard_sighting_in_future")
        svc.confirm_materials("C1", at="2026-11-27T00:30:00Z")
        self.assertTrue(svc.is_confirmed("C1"))

    def test_invoice_revision_invalidates_confirmation(self):
        svc = Service()
        add_car(svc, "C1")
        svc.confirm_materials("C1", reviewer="Wang", at="2026-11-27T00:30:00Z")
        self.assertTrue(svc.is_confirmed("C1"))
        svc.ingest_invoice({
            "invoice": "INV-C1", "line": 1, "vin": "C1", "revision": 2,
            "exporter": "E-1", "trade": "bonded", "commodity": "8703-A",
            "unit": "car", "USD_cents": 1_050_000, "agent": "A-1",
        })
        self.assertFalse(svc.is_confirmed("C1"))
        self.assertEqual(svc.confirmation_problem("C1"), "invoice_revision_unconfirmed")

    def test_agent_authorization_change_invalidates_confirmation(self):
        svc = Service()
        add_car(svc, "C1")
        svc.confirm_materials("C1", at="2026-11-27T00:30:00Z")
        svc.authorize_agent("C1", "A-9", at="2026-11-27T00:40:00Z")
        self.assertEqual(svc.confirmation_problem("C1"), "agent_authorization_changed")
        # 新授权下重新确认后恢复
        svc.confirm_materials("C1", at="2026-11-27T00:45:00Z")
        self.assertTrue(svc.is_confirmed("C1"))

    def test_submission_blocked_without_review_or_yard(self):
        svc = Service()
        add_car(svc, "C1")
        with self.assertRaises(RuleViolation) as ctx:
            svc.create_submission("S1", [("L1", ["C1"])], at="2026-11-27T01:00:00Z")
        self.assertEqual(ctx.exception.code, "line_not_submittable")
        svc.confirm_materials("C1", at="2026-11-27T00:30:00Z")
        out = svc.create_submission("S1", [("L1", ["C1"])], agent="A-1",
                                   at="2026-11-27T01:00:00Z")
        self.assertEqual(out["lines"], ["L1"])

    def test_submission_rejects_wrong_agent(self):
        svc = Service()
        add_car(svc, "C1")
        svc.confirm_materials("C1", at="2026-11-27T00:30:00Z")
        with self.assertRaises(RuleViolation):
            svc.create_submission("S1", [("L1", ["C1"])], agent="A-OTHER",
                                 at="2026-11-27T01:00:00Z")


class SampleStoryTest(unittest.TestCase):
    """samples 给出的完整故事：逐行回告、部分撤回、延迟上传、过期。"""

    def setUp(self):
        self.svc = loaded_service()

    def test_only_v311_releasable_while_permit_valid(self):
        self.assertEqual(self.svc.releasable_vins(T_IN), ["V311"])
        self.assertEqual(self.svc.active_permit("V311", T_IN), "P-18")

    def test_v310_departed_before_revocation_goes_to_customs_not_stock(self):
        audit = self.svc.departure_audit("V310", T_IN)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["permit_used"], "P-18")
        # 过岗当时 P-18 仍然有效且唯一
        self.assertEqual(audit["valid_basis_at_gate"], "P-18")
        codes = [i["code"] for i in audit["issues"]]
        self.assertIn("permit_revoked_after_exit", codes)
        self.assertEqual(audit["follow_up"]["owner"], "customs_affairs")

        report = self.svc.report(T_IN)
        customs_vins = [c["vin"] for c in report["customs_affairs"]]
        self.assertEqual(customs_vins, ["V310"])

        # 任何时点 V310 都不能回到在库可装/可核放
        for when in [T_IN, T_AFTER_EXPIRY, "2026-11-28T00:00:00Z"]:
            self.assertNotIn("V310", self.svc.releasable_vins(when))
            self.assertEqual(self.svc.physical_state("V310", when)["state"], "departed")

    def test_delayed_upload_uses_business_time_not_upload_time(self):
        # 只接入 K1/K3（K2 departed 回执尚未送达），模拟 01:55 值班视角
        svc = Service()
        for row in read_jsonl("invoices.jsonl"):
            svc.ingest_invoice(row)
        for row in read_jsonl("yard_presence.jsonl"):
            svc.ingest_yard_presence(row)
        for row in read_jsonl("customs_receipts.jsonl"):
            svc.ingest_receipt(row)
        for row in read_jsonl("fleet_reports.jsonl"):
            if row["event"] != "K2":
                svc.ingest_fleet(row)

        at = "2026-11-27T01:55:00Z"
        phys = svc.physical_state("V310", at)
        self.assertEqual(phys["state"], "staged")
        unsupported = svc.unsupported_staged(at)
        self.assertEqual([u["vin"] for u in unsupported], ["V310"])
        self.assertEqual(unsupported[0]["basis_code"], "permit_revoked")
        # 撤回之后不能再拿 P-18 发车
        with self.assertRaises(RuleViolation) as ctx:
            svc.depart("B16", "V310", at=at)
        self.assertEqual(ctx.exception.code, "permit_revoked")

        # 按上传时间还原的岗位视角同样只看得到 staged（K2 uploaded 02:01）
        desk = svc.physical_state("V310", at, use_uploaded=True)
        self.assertEqual(desk["state"], "staged")

        # 延迟的 K2 送达：实际 01:48 已凭当时有效的 P-18 离场，撤回发生在其后
        svc.ingest_fleet(next(r for r in read_jsonl("fleet_reports.jsonl") if r["event"] == "K2"))
        audit = svc.departure_audit("V310", T_IN)
        self.assertEqual(audit["departed_at"].isoformat(), "2026-11-27T01:48:00+00:00")
        self.assertEqual(audit["valid_basis_at_gate"], "P-18")
        self.assertIn("permit_revoked_after_exit", [i["code"] for i in audit["issues"]])
        self.assertEqual(audit["follow_up"]["owner"], "customs_affairs")

    def test_desk_visibility_window_before_delayed_upload(self):
        # 全量资料下按上传时间还原 01:55 岗位视角：K2(uploaded 02:01) 尚未送达，
        # V310 在岗位屏上仍是 staged，且 P-18 已在 01:52 被撤回 -> 待卸/留场名单。
        desk = self.svc.unsupported_staged(
            "2026-11-27T01:55:00Z", use_uploaded=True)
        self.assertEqual([u["vin"] for u in desk], ["V310"])
        self.assertEqual(desk[0]["basis_code"], "permit_revoked")
        # 按业务发生时间还原事实：K2 的 at=01:48 已过岗，V310 已 departed，
        # 因而不在“装车中无依据”名单，而走离场后关务处置。
        self.assertEqual(
            self.svc.unsupported_staged("2026-11-27T01:55:00Z"), [])

    def test_other_line_and_other_vin_keep_working(self):
        # L2 被要求修订并关闭，不遮挡 L1 的 V311：02:10 仍可正常核放出班
        svc = self.svc
        verdict = svc.verify_vehicle("V311", "2026-11-27T02:10:00Z", for_run="B16")
        self.assertTrue(verdict["ok"], verdict["errors"])
        row = svc.depart("B16", "V311", at="2026-11-27T02:10:00Z")
        self.assertEqual(row["permit"], "P-18")
        self.assertEqual(svc.physical_state("V311", "2026-11-27T02:11:00Z")["state"],
                         "departed")

    def test_expired_permit_makes_staged_car_unsupported_and_needs_renewal(self):
        svc = self.svc
        self.assertIsNone(svc.active_permit("V311", T_AFTER_EXPIRY))
        basis = svc.basis_for("V311", T_AFTER_EXPIRY)
        self.assertEqual(basis["state"], "none")
        self.assertEqual(basis["code"], "permit_expired")

        unsupported = svc.unsupported_staged(T_AFTER_EXPIRY)
        self.assertEqual([u["vin"] for u in unsupported], ["V311"])
        item = unsupported[0]
        self.assertEqual(item["run"], "B16")
        self.assertEqual(item["basis_code"], "permit_expired")
        self.assertIn("unload", item["handling"])
        self.assertIn("hold_yard", item["handling"])
        self.assertEqual(svc.releasable_vins(T_AFTER_EXPIRY), [])

        # 排班错过有效期后重新取得一份新依据，即可再次核放
        grant(svc, "R31", "A-710", "L1", "P-31", ["V311"],
              at="2026-11-27T03:40:00Z", until="2026-11-27T06:00:00Z")
        self.assertEqual(svc.active_permit("V311", "2026-11-27T03:45:00Z"), "P-31")
        self.assertIn("V311", svc.releasable_vins("2026-11-27T03:45:00Z"))

    def test_v312_closed_line_and_revision_todos(self):
        dossier = self.svc.vin_dossier("V312", T_IN)
        codes = [i["code"] for i in dossier["issues"]]
        self.assertIn("line_closed_without_permit", codes)
        self.assertIn("invoice_revision_needs_review", codes)
        todo_codes = [t["code"] for t in dossier["todos"]]
        self.assertIn("redeclare_closed_line", todo_codes)
        self.assertIn("review_latest_invoice_revision", todo_codes)
        self.assertFalse(dossier["releasable"])

    def test_v313_yard_car_without_declaration_has_todo(self):
        dossier = self.svc.vin_dossier("V313", T_IN)
        self.assertEqual(dossier["physical_state"], "in_yard")
        self.assertIn("obtain_permit", [t["code"] for t in dossier["todos"]])


class SplitAndRedeclareTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        for vin in ["V312", "V313"]:
            add_car(self.svc, vin)
        # 让 V312 与样本一样经历改版到 8703-A
        self.svc.ingest_invoice({
            "invoice": "INV-V312", "line": 1, "vin": "V312", "revision": 2,
            "exporter": "E-1", "trade": "bonded", "commodity": "8703-A",
            "unit": "car", "USD_cents": 1_000_000, "agent": "A-1",
        })
        self.svc.confirm_materials("V312", at="2026-11-27T00:30:00Z")
        self.svc.confirm_materials("V313", at="2026-11-27T00:30:00Z")

    def test_cannot_split_before_clear_port_result(self):
        self.svc.create_submission("A-700", [("L2", ["V312"])], agent="A-1",
                                   at="2026-11-27T01:00:00Z")
        with self.assertRaises(RuleViolation) as ctx:
            self.svc.split_and_redeclare("A-700", "L2", "A-701", "L1",
                                         at="2026-11-27T01:05:00Z")
        self.assertEqual(ctx.exception.code, "line_pending_port_result")

    def test_rejected_car_can_merge_with_other_valid_business(self):
        self.svc.record_submission("A-700", [("L2", ["V312"])], at="2026-11-27T01:00:00Z")
        self.svc.ingest_receipt({
            "event": "R12", "submission": "A-700", "line": "L2", "vins": ["V312"],
            "disposition": "asked_to_amend", "permit": "",
            "at": "2026-11-27T01:21:00Z", "until": "2026-11-27T03:10:00Z",
        })
        self.svc.ingest_receipt({
            "event": "R13", "submission": "A-700", "line": "L2", "vins": ["V312"],
            "disposition": "closed_without_permit", "permit": "",
            "at": "2026-11-27T01:43:00Z", "until": "2026-11-27T03:10:00Z",
        })
        out = self.svc.split_and_redeclare(
            "A-700", "L2", "A-701", "L1", merge_vins=["V313"],
            agent="A-1", at="2026-11-27T01:46:00Z")
        self.assertEqual(out["submission"], "A-701")
        line = self.svc.line_index[("A-701", "L1")]
        self.assertEqual(line["vins"], ["V312", "V313"])
        self.assertEqual(line["provenance"]["split_from"], "A-700/L2")
        self.assertEqual(line["amount_cents"], 2_000_000)

        # 新行取得准予后两车均可核放
        grant(self.svc, "R21", "A-701", "L1", "P-19", ["V312", "V313"],
              at="2026-11-27T01:50:00Z")
        self.assertEqual(self.svc.releasable_vins("2026-11-27T02:00:00Z"),
                         ["V312", "V313"])

    def test_split_refuses_if_old_basis_still_valid(self):
        self.svc.record_submission("A-700", [("L1", ["V312"])], at="2026-11-27T01:00:00Z")
        grant(self.svc, "R11", "A-700", "L1", "P-18", ["V312"],
              at="2026-11-27T01:20:00Z")
        with self.assertRaises(RuleViolation) as ctx:
            self.svc.split_and_redeclare(
                "A-700", "L1", "A-701", "L1", rejected_vins=["V312"],
                at="2026-11-27T02:00:00Z")
        self.assertEqual(ctx.exception.code, "multiple_valid_bases")


class UniqueBasisAndRevocationTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        add_car(self.svc, "C1")

    def test_two_overlapping_authorizations_are_conflict(self):
        grant(self.svc, "G1", "S1", "L1", "P1", ["C1"], at="2026-11-27T01:00:00Z")
        grant(self.svc, "G2", "S2", "L1", "P2", ["C1"], at="2026-11-27T02:00:00Z")
        decision = self.svc.basis_for("C1", "2026-11-27T02:30:00Z")
        self.assertEqual(decision["state"], "conflict")
        self.assertEqual(set(decision["permits"]), {"P1", "P2"})
        self.assertFalse(self.svc.is_releasable("C1", "2026-11-27T02:30:00Z"))
        report = self.svc.report("2026-11-27T02:30:00Z")
        self.assertEqual([c["vin"] for c in report["basis_conflicts"]], ["C1"])

        # 口岸明确撤回其中一张后，恢复唯一依据
        self.svc.ingest_receipt({
            "event": "X2", "submission": "S2", "line": "L1", "vins": ["C1"],
            "disposition": "revoked", "permit": "P2",
            "at": "2026-11-27T02:45:00Z", "until": UNTIL,
        })
        self.assertEqual(self.svc.active_permit("C1", "2026-11-27T03:00:00Z"), "P1")

    def test_revocation_only_covers_listed_vins(self):
        add_car(self.svc, "C2")
        grant(self.svc, "G1", "S1", "L1", "P18", ["C1", "C2"])
        self.svc.ingest_receipt({
            "event": "R14", "submission": "S1", "line": "L1", "vins": ["C1"],
            "disposition": "revoked", "permit": "P18",
            "at": "2026-11-27T01:52:00Z", "until": UNTIL,
        })
        self.assertIsNone(self.svc.active_permit("C1", "2026-11-27T02:00:00Z"))
        self.assertEqual(self.svc.active_permit("C2", "2026-11-27T02:00:00Z"), "P18")

    def test_revocation_without_permit_id_still_ends_listed_evidence(self):
        grant(self.svc, "G1", "S1", "L1", "P18", ["C1"])
        self.svc.ingest_receipt({
            "event": "X1", "submission": "S1", "line": "L1", "vins": ["C1"],
            "disposition": "revoked", "permit": "",
            "at": "2026-11-27T01:52:00Z", "until": UNTIL,
        })
        self.assertIsNone(self.svc.active_permit("C1", "2026-11-27T02:00:00Z"))

    def test_departure_after_revocation_flagged_irregular(self):
        grant(self.svc, "G1", "S1", "L1", "P1", ["C1"], at="2026-11-27T01:00:00Z")
        self.svc.ingest_receipt({
            "event": "X1", "submission": "S1", "line": "L1", "vins": ["C1"],
            "disposition": "revoked", "permit": "P1",
            "at": "2026-11-27T01:30:00Z", "until": UNTIL,
        })
        # 事实回执仍接入，但审计判定为凭撤回证据出区
        self.svc.ingest_fleet({
            "event": "K9", "run": "R9", "vin": "C1", "permit": "P1",
            "action": "departed", "at": "2026-11-27T02:00:00Z",
            "uploaded": "2026-11-27T02:01:00Z",
        })
        audit = self.svc.departure_audit("C1", "2026-11-27T02:05:00Z")
        self.assertIn("departed_on_revoked_permit", [i["code"] for i in audit["issues"]])
        self.assertEqual(audit["follow_up"]["owner"], "customs_affairs")
        # 物理上已离场，绝不回到在库
        self.assertEqual(self.svc.physical_state("C1")["state"], "departed")
        self.assertNotIn("C1", self.svc.releasable_vins("2026-11-27T02:05:00Z"))


class RunFormationTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        for vin in ["C1", "C2", "C3", "C4"]:
            add_car(self.svc, vin)
        grant(self.svc, "G1", "S1", "L1", "P1", ["C1", "C2", "C3"])

    def test_form_run_verifies_every_vehicle_including_substitute(self):
        result = self.svc.form_run(
            "B1", ["C1", "C2"], at="2026-11-27T02:00:00Z", substitutes=["C2"])
        self.assertTrue(result["ok"])
        run = self.svc.runs["B1"]
        self.assertTrue(run["confirmed"])
        self.assertTrue(run["members"]["C2"]["substitute"])

        # 临时替车 C3 逐辆核验通过
        verdict = self.svc.add_to_run("B1", "C3", at="2026-11-27T02:01:00Z")
        self.assertTrue(verdict["ok"])
        self.assertTrue(self.svc.runs["B1"]["members"]["C3"]["substitute"])

        # C4 没有依据，不能作为替车加入
        with self.assertRaises(RuleViolation) as ctx:
            self.svc.add_to_run("B1", "C4", at="2026-11-27T02:02:00Z")
        self.assertIn(ctx.exception.code, {"no_permit", "line_closed_without_permit"})

    def test_form_run_returns_per_car_failures_without_raising_by_default(self):
        result = self.svc.form_run("B2", ["C1", "C4"], at="2026-11-27T02:00:00Z")
        self.assertFalse(result["ok"])
        by_vin = {r["vin"]: r for r in result["results"]}
        self.assertTrue(by_vin["C1"]["ok"])
        self.assertFalse(by_vin["C4"]["ok"])
        self.assertFalse(self.svc.runs["B2"]["confirmed"])

        with self.assertRaises(RuleViolation):
            self.svc.form_run("B3", ["C4"], at="2026-11-27T02:00:00Z", strict=True)

    def test_vehicle_cannot_be_confirmed_on_two_runs(self):
        self.svc.form_run("B1", ["C1"], at="2026-11-27T02:00:00Z")
        verdict = self.svc.verify_vehicle("C1", "2026-11-27T02:00:00Z", for_run="B2")
        self.assertIn("already_on_run", verdict["errors"])
        bad = self.svc.form_run("B2", ["C1"], at="2026-11-27T02:00:00Z")
        self.assertFalse(bad["ok"])

    def test_reconfirm_run_catches_expiry(self):
        self.svc.form_run("B1", ["C1"], at="2026-11-27T02:00:00Z")
        # P1 until 默认 05:00；把时点推到过期后
        result = self.svc.confirm_run("B1", at="2026-11-27T06:00:00Z")
        self.assertFalse(result["ok"])
        self.assertIn("permit_expired", result["results"][0]["errors"])

    def test_depart_requires_matching_valid_permit(self):
        self.svc.ingest_fleet({
            "event": "K1", "run": "B1", "vin": "C1", "permit": "P1",
            "action": "staged", "at": "2026-11-27T02:10:00Z",
            "uploaded": "2026-11-27T02:10:01Z"})
        with self.assertRaises(RuleViolation) as ctx:
            self.svc.depart("B1", "C1", permit="WRONG", at="2026-11-27T02:20:00Z")
        self.assertEqual(ctx.exception.code, "permit_mismatch")
        row = self.svc.depart("B1", "C1", at="2026-11-27T02:20:00Z")
        self.assertEqual(row["action"], "departed")
        with self.assertRaises(RuleViolation) as ctx2:
            self.svc.depart("B1", "C1", at="2026-11-27T02:25:00Z")
        self.assertEqual(ctx2.exception.code, "already_departed")


class UnsupportedStagedHandlingTest(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        add_car(self.svc, "C1")  # 无 permit

    def test_identify_unfounded_loading_and_unload(self):
        self.svc.stage("B9", "C1", at="2026-11-27T02:00:00Z")
        unsupported = self.svc.unsupported_staged("2026-11-27T02:05:00Z")
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["vin"], "C1")
        self.assertEqual(unsupported[0]["code"], "staged_without_basis")

        self.svc.mark_handling("C1", "unload", at="2026-11-27T02:20:00Z")
        self.assertEqual(self.svc.physical_state("C1", "2026-11-27T02:21:00Z")["state"],
                         "in_yard")
        self.assertEqual(self.svc.unsupported_staged("2026-11-27T02:21:00Z"), [])

    def test_hold_yard_blocks_release_until_closed(self):
        self.svc.mark_handling("C1", "hold_yard", clerk="Du", note="待查",
                              at="2026-11-27T02:00:00Z")
        verdict = self.svc.verify_vehicle("C1", "2026-11-27T02:05:00Z")
        self.assertIn("yard_hold", verdict["errors"])
        self.svc.release_hold("C1")
        verdict2 = self.svc.verify_vehicle("C1", "2026-11-27T02:05:00Z")
        self.assertNotIn("yard_hold", verdict2["errors"])


class DossierTest(unittest.TestCase):
    def test_line_dossier_reconciles_every_requirement(self):
        svc = loaded_service()
        d = svc.line_dossier("A-700", "L1", T_IN)
        self.assertEqual(d["composition"], ["V310", "V311"])
        self.assertEqual({r["event"] for r in d["receipts"]}, {"R11", "R14"})

        by_vin = {v["vin"]: v for v in d["vehicles"]}
        self.assertEqual(by_vin["V310"]["permit_used_on_departure"], "P-18")
        self.assertFalse(by_vin["V310"]["releasable"])
        self.assertEqual(by_vin["V310"]["follow_up"]["owner"], "customs_affairs")
        self.assertEqual(by_vin["V311"]["valid_basis"], "P-18")
        self.assertTrue(by_vin["V311"]["releasable"])

        # 未出区车辆的错误/待办进入行级 open 列表
        d2 = svc.line_dossier("A-700", "L2", T_IN)
        open_vins = {x["vin"] for x in d2["open_issues"]}
        self.assertIn("V312", open_vins)
        self.assertTrue(any(t["code"] == "redeclare_closed_line" for t in d2["open_todos"]))

        # 整个报告必须可 JSON 序列化（交换/接口输出）
        json.dumps(svc.report(T_IN), ensure_ascii=False)

    def test_unknown_line_raises_keyerror(self):
        svc = loaded_service()
        with self.assertRaises(KeyError):
            svc.line_dossier("A-700", "NOPE")

    def test_snapshot_drift_flagged_after_invoice_change(self):
        svc = Service()
        add_car(svc, "C1", commodity="8703-B")
        svc.confirm_materials("C1", at="2026-11-27T00:30:00Z")
        svc.create_submission("S1", [("L1", ["C1"])], agent="A-1",
                              at="2026-11-27T01:00:00Z")
        # 申报后发票改版
        svc.ingest_invoice({
            "invoice": "INV-C1", "line": 1, "vin": "C1", "revision": 2,
            "exporter": "E-1", "trade": "bonded", "commodity": "8703-A",
            "unit": "car", "USD_cents": 1_000_000, "agent": "A-1"})
        d = svc.line_dossier("S1", "L1", "2026-11-27T01:30:00Z")
        row = d["vehicles"][0]
        self.assertFalse(row["snapshot_current"])
        self.assertIn("materials_changed_after_submission",
                      [i["code"] for i in row["issues"]])


class FacadeAndCliTest(unittest.TestCase):
    def test_root_service_module_facade(self):
        import service as root_service
        self.assertIs(root_service.Service, Service)
        svc = root_service.Service()
        self.assertIsInstance(svc, Service)

    def test_cli_json(self):
        proc = subprocess.run(
            [sys.executable, "-m", "yardflow", "--dir", str(SAMPLES),
             "--at", T_IN, "--json"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["releasable_vins"], ["V311"])
        self.assertEqual([c["vin"] for c in payload["customs_affairs"]], ["V310"])

    def test_cli_text_and_line_view(self):
        proc = subprocess.run(
            [sys.executable, "-m", "yardflow", "--dir", str(SAMPLES),
             "--at", T_IN, "--line", "A-700/L1"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("A-700/L1", proc.stdout)
        self.assertIn("关务", proc.stdout)


if __name__ == "__main__":
    unittest.main()
