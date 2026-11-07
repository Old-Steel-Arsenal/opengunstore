from __future__ import annotations

from unittest.mock import MagicMock, patch
from unittest.mock import patch as _p

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import reconcile
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig, FastBoundError


def _client():
    return FastBoundClient(FastBoundConfig(account_num="A", api_key="k",
        base_url="https://cloud.fastbound.com", api_version="1.0",
        audit_user="t@t", timeout=30))


def _firearm_item(code, ftype):
    if not frappe.db.exists("Item Group", "Used Firearm"):
        frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
            "parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
    if not frappe.db.exists("Item", code):
        frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
            "item_group": "Used Firearm", "is_firearm": 1, "is_stock_item": 1,
            "has_serial_no": 1, "firearm_type": ftype}).insert(ignore_permissions=True)
    return code


class FirearmItemFieldsType(FrappeTestCase):
    def test_prefers_serial_firearm_type(self):
        code = _firearm_item("FB-MAP-RIFLE", "Rifle")
        frappe.get_doc({"doctype": "Serial No", "serial_no": "FB-MAP-1",
            "item_code": code, "firearm_type": "Pistol"}).insert(ignore_permissions=True)
        fields = FastBoundClient._firearm_item_fields(code, "FB-MAP-1")
        self.assertEqual(fields["type"], "Pistol")  # serial wins over Item's "Rifle"

    def test_importer_from_serial_only_never_item(self):
        # 2026-08-20: Item.importer is not a per-gun default. The push takes an
        # importer only from the Serial No; an Item-level value is ignored — a
        # generic surplus bucket's Item importer must never enter the bound book
        # on a gun that doesn't actually carry that marking.
        code = _firearm_item("FB-MAP-IMP", "Rifle")
        frappe.db.set_value("Item", code, "importer", "Bucket Importer Inc")
        frappe.get_doc({"doctype": "Serial No", "serial_no": "FB-MAP-IMP-1",
            "item_code": code}).insert(ignore_permissions=True)
        fields = FastBoundClient._firearm_item_fields(code, "FB-MAP-IMP-1")
        self.assertNotIn("importer", fields)

    def test_importer_from_serial_used(self):
        # A per-serial importer (the marking on THIS gun) still reaches the push.
        code = _firearm_item("FB-MAP-IMP2", "Rifle")
        frappe.get_doc({"doctype": "Serial No", "serial_no": "FB-MAP-IMP2-1",
            "item_code": code, "importer": "Per Unit Imports"}).insert(ignore_permissions=True)
        fields = FastBoundClient._firearm_item_fields(code, "FB-MAP-IMP2-1")
        self.assertEqual(fields["importer"], "Per Unit Imports")


class PosEffectiveImporter(FrappeTestCase):
    def test_pos_effective_importer_never_falls_back_to_item(self):
        # The diff must judge the same value the push sends (serial-only,
        # 2026-08-20): with an Item-level importer and a blank serial, the POS
        # effective importer is "" — the diff must not report a value the push
        # would never send.
        code = _firearm_item("RC-POSEFF", "Pistol")
        frappe.db.set_value("Item", code, "importer", "Bucket Importer Inc")
        frappe.get_doc({"doctype": "Serial No", "serial_no": "RC-POSEFF-1",
            "item_code": code}).insert(ignore_permissions=True)
        self.assertEqual(reconcile._pos_effective("RC-POSEFF-1")["importer"], "")

    def test_fb_importer_vs_blank_serial_is_a_real_diff(self):
        # Same setup at the diff level: FastBound carries an importer, the
        # serial none — the Item's value must not mask the difference.
        code = _firearm_item("RC-POSEFF2", "Pistol")
        frappe.db.set_value("Item", code, "importer", "IO INC")
        _serial(code, "RC-POSEFF2-1", fb_id="fbid", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="Pistol")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                importer="IO INC", model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.compute_serial_fb_diff("RC-POSEFF2-1")
        self.assertEqual(res["status"], "ok")
        imp = [d for d in res["diffs"] if d["field"] == "importer"]
        self.assertEqual(len(imp), 1)
        self.assertEqual(imp[0]["pos_value"], "")
        self.assertEqual(imp[0]["fb_value"], "IO INC")


class GetItem(FrappeTestCase):
    def test_get_item_returns_dict(self):
        c = _client()
        with patch.object(c, "_request", return_value={"id": "x", "serial": "S1"}):
            self.assertEqual(c.get_item("x")["serial"], "S1")

    def test_get_item_404_returns_none(self):
        c = _client()
        with patch.object(c, "_request", side_effect=FastBoundError("nope", status_code=404)):
            self.assertIsNone(c.get_item("missing"))


def _ensure_link_masters(manufacturer=None, caliber=None):
    """Serial No.manufacturer is a custom Link field LABELLED "Manufacturer" but
    pointing at ERPNext's Brand doctype (ffl_core custom_field "Serial No-
    manufacturer", options=Brand), and .caliber a Link to ffl_core's Caliber. A
    fresh site has neither Brand "Colt" nor Caliber "45ACP" — nothing in setup
    seeds them, so the inserts below would raise LinkValidationError ("Could not
    find Manufacturer: Colt"). Auto-vivify whatever value the fixture uses,
    mirroring how production auto-vivifies what it sees (importer._ensure_brand /
    _ensure_caliber) and how _firearm_item above self-seeds its Item Group. This
    keeps these tests from silently depending on a warm dev site that happened to
    accumulate a Brand "Colt" from real intake (the root cause of the 15 fresh-
    site reconcile failures). Inlined (not reconcile._ensure_brand) so the
    fixture stays independent of the code-under-test some tests mock out."""
    if manufacturer and not frappe.db.exists("Brand", manufacturer):
        frappe.get_doc({"doctype": "Brand", "brand": manufacturer}).insert(ignore_permissions=True)
    if caliber and not frappe.db.exists("Caliber", caliber):
        frappe.get_doc({"doctype": "Caliber", "caliber_name": caliber}).insert(ignore_permissions=True)


def _serial(code, serial, **over):
    _ensure_link_masters(over.get("manufacturer"), over.get("caliber"))
    frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": code,
        "fastbound_item_id": over.pop("fb_id", None), **over}).insert(ignore_permissions=True)
    return serial


def _fb(serial="S", **over):
    base = {"id": "fbid", "serial": serial, "manufacturer": "Colt", "importer": "",
        "model": "1911", "caliber": "45ACP", "type": "Pistol"}
    base.update(over)
    return base


class ComputeDiff(FrappeTestCase):
    def test_caliber_spelling_variant_is_not_a_diff(self):
        code = _firearm_item("RC-CAL", "Pistol")
        _serial(code, "RC-CAL-1", fb_id="fbid", manufacturer="Colt", model_name="1911",
            caliber=None, firearm_type="Pistol")
        # Item caliber + serial fallback; FB says "45 AUTO" which canon-equals "45ACP".
        frappe.db.set_value("Item", code, "caliber", "45ACP")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(caliber="45 AUTO", manufacturer="Colt", model="1911", type="Pistol")
            res = reconcile.compute_serial_fb_diff("RC-CAL-1")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["in_sync"])

    def test_manufacturer_real_diff_reported(self):
        code = _firearm_item("RC-MFG", "Pistol")
        _serial(code, "RC-MFG-1", fb_id="fbid", manufacturer="Colt", model_name="1911",
            caliber="45ACP", firearm_type="Pistol")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.compute_serial_fb_diff("RC-MFG-1")
        fields = [d["field"] for d in res["diffs"]]
        self.assertIn("manufacturer", fields)
        self.assertFalse(res["in_sync"])

    def test_not_in_fastbound(self):
        code = _firearm_item("RC-NONE", "Pistol")
        _serial(code, "RC-NONE-1", firearm_type="Pistol")  # no fb_id
        self.assertEqual(reconcile.compute_serial_fb_diff("RC-NONE-1")["status"], "not_in_fastbound")

    def test_fb_disposed(self):
        code = _firearm_item("RC-DISP", "Pistol")
        _serial(code, "RC-DISP-1", fb_id="fbid", firearm_type="Pistol")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(disposed=True)
            self.assertEqual(reconcile.compute_serial_fb_diff("RC-DISP-1")["status"], "fb_disposed")

    def test_blank_pos_type_vs_fb_other_firearm_is_not_a_diff(self):
        # POS effective firearm_type is blank (both Serial No and its Item empty).
        # The acquisition push maps a blank firearm_type -> "Other Firearm", so FB
        # stores "Other Firearm". _fb_item_type canons both sides to "Other Firearm",
        # so no spurious "Firearm Type" diff should be reported.
        code = _firearm_item("RC-TYPE-BLANK", "")
        frappe.db.set_value("Item", code, "firearm_type", "")
        _serial(code, "RC-TYPE-BLANK-1", fb_id="fbid", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Other Firearm")
            res = reconcile.compute_serial_fb_diff("RC-TYPE-BLANK-1")
        self.assertEqual(res["status"], "ok")
        fields = [d["field"] for d in res["diffs"]]
        self.assertNotIn("type", fields)
        self.assertTrue(res["in_sync"])

    def test_type_real_diff_reported(self):
        code = _firearm_item("RC-TYPE", "Pistol")
        _serial(code, "RC-TYPE-1", fb_id="fbid", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="Pistol")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Rifle")
            res = reconcile.compute_serial_fb_diff("RC-TYPE-1")
        fields = [d["field"] for d in res["diffs"]]
        self.assertIn("type", fields)
        self.assertFalse(res["in_sync"])

    def test_fb_unknown_placeholder_not_a_diff(self):
        # client._firearm_item_fields injects "Unknown" for a blank required field,
        # so a POS-blank manufacturer reads as FB "Unknown". That placeholder is NOT
        # a real value: it folds to blank on both sides, so there is no diff.
        code = _firearm_item("RC-UNK", "Pistol")  # Item manufacturer left blank
        _serial(code, "RC-UNK-1", fb_id="fbid", model_name="1911",
            caliber="45ACP", firearm_type="Pistol")  # Serial No manufacturer blank
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Unknown",
                model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.compute_serial_fb_diff("RC-UNK-1")
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("manufacturer", [d["field"] for d in res["diffs"]])
        self.assertTrue(res["in_sync"])

    def test_fb_frame_type_maps_to_receiver_no_diff(self):
        # POS firearm_type "Receiver" was set at import from FB "Frame" via
        # importer._map_firearm_type. Comparing through that SAME mapper, FB "Frame"
        # -> "Receiver" == POS "Receiver": no perpetual "Firearm Type" diff.
        code = _firearm_item("RC-FRAME", "Receiver")
        _serial(code, "RC-FRAME-1", fb_id="fbid", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="Receiver")
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Frame")
            res = reconcile.compute_serial_fb_diff("RC-FRAME-1")
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("type", [d["field"] for d in res["diffs"]])
        self.assertTrue(res["in_sync"])


class ApplyResolution(FrappeTestCase):
    def _setup(self, code_serial):
        code, serial = code_serial
        _firearm_item(code, "Pistol")
        _serial(code, serial, fb_id="fbid", manufacturer="Colt", model_name="1911",
            caliber="45ACP", importer="", firearm_type="Pistol")
        return serial

    def test_fb_wins_writes_serial_no(self):
        serial = self._setup(("AP-FB", "AP-FB-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-FB-1",
                {"manufacturer": {"choice": "fb", "fb_value": "Glock"}})
        self.assertTrue(res["ok"])
        self.assertIn("manufacturer", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-FB-1", "manufacturer"), "Glock")
        self.assertTrue(frappe.db.exists("Brand", "Glock"))

    def test_pos_wins_pushes_once(self):
        serial = self._setup(("AP-POS", "AP-POS-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound", return_value={"ok": True}) as push:
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-POS-1",
                {"manufacturer": {"choice": "pos", "fb_value": "Glock"}})
        push.assert_called_once_with("AP-POS-1")
        self.assertTrue(res["fastbound_updated"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-POS-1", "manufacturer"), "Colt")

    def test_pos_win_with_blank_importer_is_skipped_not_pushed(self):
        # "POS wins" on importer with a blank POS value is a no-op the push
        # cannot perform: it never sends a clearing empty, so the
        # read-merge-write PUT would keep FastBound's value and the same diff
        # would resurface on the next scan. It must land in skipped with a
        # reason — never report success for an apply that didn't happen.
        serial = self._setup(("AP-BLANKIMP", "AP-BLANKIMP-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Pistol", importer="IO INC")
            res = reconcile.apply_serial_fb_resolution("AP-BLANKIMP-1",
                {"importer": {"choice": "pos", "fb_value": "IO INC"}})
        push.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertNotIn("importer", res["applied"]["pos"])
        self.assertIn({"field": "importer", "reason": "pos_blank_importer"}, res["skipped"])
        self.assertFalse(res["fastbound_updated"])

    def test_all_fb_does_not_push(self):
        serial = self._setup(("AP-NOPUSH", "AP-NOPUSH-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push:
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-NOPUSH-1",
                {"manufacturer": {"choice": "fb", "fb_value": "Glock"}})
        push.assert_not_called()
        self.assertFalse(res["fastbound_updated"])

    def test_stale_field_skipped(self):
        serial = self._setup(("AP-STALE", "AP-STALE-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            # Operator was shown "Sig", but FB now says "Glock" → skipped as changed.
            res = reconcile.apply_serial_fb_resolution("AP-STALE-1",
                {"manufacturer": {"choice": "fb", "fb_value": "Sig"}})
        self.assertEqual(frappe.db.get_value("Serial No", "AP-STALE-1", "manufacturer"), "Colt")
        self.assertTrue(any(s["reason"] == "changed" for s in res["skipped"]))

    def test_apply_fb_type_uses_map_firearm_type(self):
        # FB type "Frame" is authoritative: importer._map_firearm_type("Frame")
        # -> "Receiver", so accepting FB stamps "Receiver" onto the Serial No
        # (NOT the raw "Frame" string, and NOT skipped).
        serial = self._setup(("AP-FTYPE", "AP-FTYPE-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Frame")
            res = reconcile.apply_serial_fb_resolution("AP-FTYPE-1",
                {"type": {"choice": "fb", "fb_value": "Frame"}})
        self.assertTrue(res["ok"])
        self.assertIn("type", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-FTYPE-1", "firearm_type"), "Receiver")
        push.assert_not_called()

    def test_blank_fb_type_skipped_not_applied(self):
        # A BLANK FB type maps to None (importer._map_firearm_type) — there is
        # nothing to stamp, so the FB->POS stage skips it (fb_type_blank) and the
        # Serial No keeps its existing firearm_type.
        serial = self._setup(("AP-BTYPE", "AP-BTYPE-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push:
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="")
            res = reconcile.apply_serial_fb_resolution("AP-BTYPE-1",
                {"type": {"choice": "fb", "fb_value": ""}})
        self.assertTrue(res["ok"])
        self.assertTrue(any(s["field"] == "type" and s["reason"] == "fb_type_blank"
            for s in res["skipped"]))
        self.assertNotIn("type", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-BTYPE-1", "firearm_type"), "Pistol")
        push.assert_not_called()

    def test_fb_unknown_not_written_on_apply(self):
        # POS "Colt" vs FB "Unknown" IS a real diff (the POS-wins case). If the
        # operator accepts FB anyway, the "Unknown" placeholder must NOT be stamped,
        # and no junk Brand "Unknown" is minted (_ensure_brand is never reached).
        serial = self._setup(("AP-UNK", "AP-UNK-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push, \
             _p.object(reconcile, "_ensure_brand") as ensure_brand:
            fs.return_value.get_item.return_value = _fb(manufacturer="Unknown",
                model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-UNK-1",
                {"manufacturer": {"choice": "fb", "fb_value": "Unknown"}})
        self.assertTrue(res["ok"])
        self.assertTrue(any(s["field"] == "manufacturer" and s["reason"] == "fb_placeholder"
            for s in res["skipped"]))
        self.assertNotIn("manufacturer", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-UNK-1", "manufacturer"), "Colt")
        ensure_brand.assert_not_called()  # no junk Brand "Unknown" created
        push.assert_not_called()

    def test_already_resolved_field_skipped(self):
        serial = self._setup(("AP-RESOLVED", "AP-RESOLVED-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound") as push:
            # FB matches POS exactly -> no diffs; a resolution for an in-sync
            # field is skipped as already_resolved, with nothing written/pushed.
            fs.return_value.get_item.return_value = _fb(manufacturer="Colt",
                model="1911", caliber="45ACP", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-RESOLVED-1",
                {"manufacturer": {"choice": "fb", "fb_value": "Colt"}})
        self.assertTrue(res["ok"])
        self.assertTrue(any(s["field"] == "manufacturer" and s["reason"] == "already_resolved"
            for s in res["skipped"]))
        self.assertNotIn("manufacturer", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-RESOLVED-1", "manufacturer"), "Colt")
        push.assert_not_called()
        self.assertFalse(res["fastbound_updated"])

    def test_mixed_fb_and_pos(self):
        serial = self._setup(("AP-MIX", "AP-MIX-1"))
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile, "push_serial_to_fastbound", return_value={"ok": True}) as push:
            # manufacturer differs (FB Glock) and caliber differs (FB 9mm).
            fs.return_value.get_item.return_value = _fb(manufacturer="Glock",
                model="1911", caliber="9mm", type="Pistol")
            res = reconcile.apply_serial_fb_resolution("AP-MIX-1", {
                "manufacturer": {"choice": "fb", "fb_value": "Glock"},
                "caliber": {"choice": "pos", "fb_value": "9mm"},
            })
        self.assertTrue(res["ok"])
        # FB-wins field is written locally to the Serial No (readback proves the
        # set_value landed before the single push fired).
        self.assertIn("manufacturer", res["applied"]["fb"])
        self.assertEqual(frappe.db.get_value("Serial No", "AP-MIX-1", "manufacturer"), "Glock")
        # POS-wins field recorded and exactly one push, ordered after the write.
        self.assertIn("caliber", res["applied"]["pos"])
        push.assert_called_once_with("AP-MIX-1")
        self.assertTrue(res["fastbound_updated"])


class MatchAndScan(FrappeTestCase):
    def test_match_stamps_fastbound_item_id(self):
        code = _firearm_item("MT-1", "Pistol")
        _serial(code, "MT-1-1", firearm_type="Pistol")  # no fb_id
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs:
            fs.return_value.find_item_by_serial.return_value = {"id": "found-id", "serial": "MT-1-1"}
            res = reconcile.match_serial_to_fastbound("MT-1-1")
        self.assertTrue(res["ok"])
        self.assertEqual(frappe.db.get_value("Serial No", "MT-1-1", "fastbound_item_id"), "found-id")

    def test_scan_enqueues(self):
        with _p.object(reconcile.frappe, "enqueue") as eq:
            res = reconcile.scan_in_stock_fb_diffs()
        self.assertTrue(res["started"])
        eq.assert_called_once()

    def test_run_scan_collects_divergent(self):
        # Two in-stock guns with a FastBound id: one diverges (FB maker Glock vs
        # POS Colt), one matches. The scan publishes exactly one realtime event
        # whose `divergent` list names the divergent serial and omits the matched.
        code = _firearm_item("RC-SCAN", "Pistol")
        _serial(code, "RC-SCAN-DIV", fb_id="fb-div", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="Pistol")
        _serial(code, "RC-SCAN-OK", fb_id="fb-match", manufacturer="Colt",
            model_name="1911", caliber="45ACP", firearm_type="Pistol")
        # Force Active so the scan's status filter picks both up (a bare Serial No
        # insert may not land Active without a stock movement).
        for s in ("RC-SCAN-DIV", "RC-SCAN-OK"):
            frappe.db.set_value("Serial No", s, "status", "Active", update_modified=False)

        def fake_get_item(item_id):
            if item_id == "fb-div":
                return _fb(manufacturer="Glock", model="1911", caliber="45ACP", type="Pistol")
            if item_id == "fb-match":
                return _fb(manufacturer="Colt", model="1911", caliber="45ACP", type="Pistol")
            return None  # any other (pre-existing) serial: fb_missing, never divergent

        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
             _p.object(reconcile.frappe, "publish_realtime") as pub:
            fs.return_value.get_item.side_effect = fake_get_item
            reconcile._run_scan_in_stock_fb_diffs(user="Administrator")
        # The Notification Log insert fires its own realtime events, so target the
        # scan's own event: it must be published exactly once with the right payload.
        scan_calls = [c for c in pub.call_args_list if c.args and c.args[0] == "fb_diff_scan_done"]
        self.assertEqual(len(scan_calls), 1)
        payload = scan_calls[0].args[1]
        serials = [d["serial"] for d in payload["divergent"]]
        self.assertIn("RC-SCAN-DIV", serials)
        self.assertNotIn("RC-SCAN-OK", serials)


class FastBoundErrorTriage(FrappeTestCase):
    """Terminal failures are flagged AT THE SOURCE (FastBoundError.terminal ->
    _TERMINAL_PREFIX marker) and skipped by reconcile; recoverable global/config
    and transient failures carry NO marker and stay eligible for auto-retry."""

    def test_only_marker_prefixed_error_is_terminal(self):
        from ffl_integrations.fastbound.tasks import _fb_error_is_terminal, _TERMINAL_PREFIX
        self.assertTrue(_fb_error_is_terminal(
            _TERMINAL_PREFIX + "Disposition D-1: already disposed in FastBound"))
        # Recoverable (global/config) + transient + unparseable must NOT be terminal,
        # so the daily reconcile keeps retrying them once the cause clears.
        for err in (
            "POST Dispositions → 400: Invalid Audit User",   # global config — fixable
            "POST x → 401: unauthorized",                    # expired/rotated key
            "POST x → 408: request timeout",                 # transient
            "POST x → 429: slow down",
            "POST x → 503: down",
            "connection reset by peer",
            "", None,
        ):
            self.assertFalse(_fb_error_is_terminal(err), msg=repr(err))

    def test_reconcile_skips_terminal_keeps_recoverable(self):
        from ffl_integrations.fastbound import tasks
        disp = [
            frappe._dict(name="D-TERM",
                         fastbound_sync_error=tasks._TERMINAL_PREFIX + "already disposed"),
            frappe._dict(name="D-CONFIG",
                         fastbound_sync_error="POST → 400: Invalid Audit User"),  # recoverable
        ]
        enq: list = []

        def fake_get_all(dt, **kw):
            return [] if dt == "FFL Acquisition" else disp

        with patch.object(tasks.frappe, "get_all", side_effect=fake_get_all), \
             patch.object(tasks.frappe, "enqueue",
                          side_effect=lambda *a, **k: enq.append(k.get("disposition_name"))):
            tasks.reconcile_failed_pushes()
        self.assertIn("D-CONFIG", enq)    # recoverable config fault -> re-enqueued
        self.assertNotIn("D-TERM", enq)   # source-flagged terminal -> skipped

    def test_push_disposition_terminal_stamps_marker_and_fast_fails(self):
        from ffl_integrations.fastbound import tasks
        doc = MagicMock(docstatus=1, fastbound_id=None, disposition_type="FFL Transfer")
        captured = {}

        def fake_record(dt, name, err):
            captured["err"] = err
            return True

        client = MagicMock()
        client.post_disposition.side_effect = FastBoundError(
            "already disposed", status_code=400, terminal=True)
        with patch.object(tasks.frappe, "get_doc", return_value=doc), \
             patch.object(tasks.FastBoundClient, "from_settings", return_value=client), \
             patch.object(tasks, "_record_push_error", side_effect=fake_record), \
             patch.object(tasks.frappe.db, "commit"), \
             patch.object(tasks.time, "sleep"), \
             patch.object(tasks, "_notify_terminal") as bell:
            # (the bell is patched: under the get_doc mock a real Notification Log
            # insert would cache a MagicMock as its new-doc template for the session)
            tasks.push_disposition_to_fastbound("DISP-X", _retries=3, _commit=False)
        self.assertTrue(captured["err"].startswith(tasks._TERMINAL_PREFIX))
        self.assertEqual(client.post_disposition.call_count, 1)  # fast-fail, no retry
        bell.assert_called_once_with("FFL Disposition", "DISP-X", "already disposed")

    def test_push_disposition_recoverable_no_marker_and_retries(self):
        from ffl_integrations.fastbound import tasks
        doc = MagicMock(docstatus=1, fastbound_id=None, disposition_type="FFL Transfer")
        captured = {}

        def fake_record(dt, name, err):
            captured["err"] = err
            return True

        client = MagicMock()
        client.post_disposition.side_effect = FastBoundError(
            "POST Dispositions → 400: Invalid Audit User", status_code=400)  # terminal defaults False
        with patch.object(tasks.frappe, "get_doc", return_value=doc), \
             patch.object(tasks.FastBoundClient, "from_settings", return_value=client), \
             patch.object(tasks, "_record_push_error", side_effect=fake_record), \
             patch.object(tasks.frappe.db, "commit"), \
             patch.object(tasks.time, "sleep"):
            tasks.push_disposition_to_fastbound("DISP-Y", _retries=2, _commit=False)
        self.assertFalse(captured["err"].startswith(tasks._TERMINAL_PREFIX))
        self.assertEqual(client.post_disposition.call_count, 3)  # 1 + _retries=2


def _warehouse(name: str, *, consignment_dealer: str | None = None) -> str:
    """A leaf warehouse, optionally stamped as a per-dealer consignment holding
    location. in_stock_targets keys off Warehouse.consignment_dealer being set,
    so stamp the field directly rather than standing up the whole dealer
    onboarding path — the SQL is what's under test."""
    company = (frappe.db.get_single_value("Global Defaults", "default_company")
               or frappe.get_all("Company", pluck="name")[0])
    abbr = frappe.db.get_value("Company", company, "abbr")
    full = f"{name} - {abbr}"
    if not frappe.db.exists("Warehouse", full):
        frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name,
            "company": company, "is_group": 0}).insert(ignore_permissions=True)
    frappe.db.set_value("Warehouse", full, "consignment_dealer", consignment_dealer or "")
    return full


def _active_serial(serial: str, item_code: str, warehouse: str, fb_id: str,
                   *, consigned_out: int = 0) -> None:
    if not frappe.db.exists("Serial No", serial):
        frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
            "item_code": item_code}).insert(ignore_permissions=True)
    frappe.db.set_value("Serial No", serial, {
        "status": "Active", "warehouse": warehouse, "fastbound_item_id": fb_id,
        "consigned_out": consigned_out})


class InStockTargetsConsignmentGuard(FrappeTestCase):
    """Incident CONO-2026-01544 (2026-08-08).

    A gun at a consignment dealer is SUPPOSED to be disposed in FastBound and
    Active here at once: the A&D book records that FFL responsibility moved to
    the dealer, while the asset stays ours until the dealer sells it. Both
    consumers of in_stock_targets do exactly one thing with a returned row —
    Material-Issue it out — so returning consignment stock makes the asset
    vanish and skips the settlement invoice that bills the sale.

    The webhook caller did that to 39 guns ($22,323.85);
    sync_in_stock_from_boundbook with apply=true would have done it to the same
    39 from the other side. Excluding it here closes both at the source.

    The predicate is the repo's canonical TWO keys — Serial No.consigned_out OR
    a consignment warehouse — per firearm._pos_picker_excluded_serials rule 3 /
    ffl_core.custody: a flag and a location drift apart, so either alone must
    disqualify. Each key gets its own test below. The flag half is also what
    makes this race-free: ship_consignment_out stamps consigned_out as its
    FIRST write, before the Material Transfer and long before the disposition
    is pushed, so no echo can arrive early enough to miss it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.item = _firearm_item("FB-CG-GUN", "Rifle")
        cls.plain_wh = _warehouse("FB CG Plain")
        cls.cono_wh = _warehouse("FB CG Dealer", consignment_dealer="_Test FB CG Dealer")
        _active_serial("FB-CG-PLAIN", cls.item, cls.plain_wh, "FB-CG-ID-1")
        # key 2 only: sitting in a dealer warehouse, flag never stamped
        _active_serial("FB-CG-CONSIGNED", cls.item, cls.cono_wh, "FB-CG-ID-2")
        # key 1 only: flagged mid-ship, stock not moved yet (the echo-race shape)
        _active_serial("FB-CG-FLAGGED", cls.item, cls.plain_wh, "FB-CG-ID-3", consigned_out=1)
        cls.disposed = {"FB-CG-ID-1": "FB-CG-PLAIN", "FB-CG-ID-2": "FB-CG-CONSIGNED",
                        "FB-CG-ID-3": "FB-CG-FLAGGED"}

    def test_consignment_stock_is_not_a_removal_target(self):
        from ffl_integrations.fastbound.inventory_sync import in_stock_targets
        serials = {t["serial"] for t in in_stock_targets(self.disposed)}
        self.assertEqual(serials, {"FB-CG-PLAIN"})

    def test_the_warehouse_key_alone_excludes(self):
        from ffl_integrations.fastbound.inventory_sync import in_stock_targets
        self.assertNotIn("FB-CG-CONSIGNED", {t["serial"] for t in in_stock_targets(self.disposed)})

    def test_the_consigned_out_flag_alone_excludes(self):
        """The race-proof half. A gun flagged by ship_consignment_out step 6a but
        whose Material Transfer hasn't landed yet is still in a NORMAL warehouse —
        the warehouse key can't see it, and this is exactly the window an early
        FastBound echo would hit."""
        from ffl_integrations.fastbound.inventory_sync import in_stock_targets
        self.assertNotIn("FB-CG-FLAGGED", {t["serial"] for t in in_stock_targets(self.disposed)})

    def test_a_genuinely_stranded_gun_is_still_a_target(self):
        """The counter-example that keeps the exclusion honest: a disposed gun
        still Active in a NORMAL warehouse is a real discrepancy and must keep
        showing up — otherwise the guard has quietly broken the reconcile."""
        from ffl_integrations.fastbound.inventory_sync import in_stock_targets
        self.assertIn("FB-CG-PLAIN", {t["serial"] for t in in_stock_targets(self.disposed)})

    def test_held_stock_is_reachable_only_through_the_reporting_function(self):
        """consignment_held_targets is the ONE way to see this stock, and it
        returns exactly what in_stock_targets withheld — no overlap, nothing
        dropped. A stock-out path calling in_stock_targets cannot opt in."""
        from ffl_integrations.fastbound.inventory_sync import (
            consignment_held_targets, in_stock_targets)
        safe = {t["serial"] for t in in_stock_targets(self.disposed)}
        held = {t["serial"] for t in consignment_held_targets(self.disposed)}
        self.assertEqual(held, {"FB-CG-CONSIGNED", "FB-CG-FLAGGED"})
        self.assertEqual(safe & held, set())
        self.assertEqual(safe | held, {"FB-CG-PLAIN", "FB-CG-CONSIGNED", "FB-CG-FLAGGED"})

    def test_the_dry_run_reports_held_consignment_stock_separately(self):
        from ffl_integrations.fastbound import inventory_sync
        with patch.object(inventory_sync, "_fb_disposed_items", return_value=self.disposed):
            out = inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
        self.assertEqual([t["serial"] for t in out["would_remove"]], ["FB-CG-PLAIN"])
        self.assertEqual(out["would_remove_count"], 1)
        self.assertEqual({t["serial"] for t in out["consignment_held"]},
                         {"FB-CG-CONSIGNED", "FB-CG-FLAGGED"})
        self.assertEqual(out["consignment_held_count"], 2)


class WebhookEchoStopsAtTheGuardedQuery(FrappeTestCase):
    """The incident's own path, end to end, with NOTHING mocked out of the way.

    Every pre-existing webhook test patches in_stock_targets to a literal, so
    all of them would stay green if someone re-pointed the webhook at its own
    query or added an opt-out — and webhook.py's NOTE about this is a comment,
    not an assertion. This pins the contract that actually stopped the bleeding:
    _sync_fb_direct_disposal_stock may only take rows from in_stock_targets, and
    therefore can never see a consigned gun.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        item = _firearm_item("FB-WH-GUN", "Rifle")
        cls.plain_wh = _warehouse("FB WH Plain")
        cls.cono_wh = _warehouse("FB WH Dealer", consignment_dealer="_Test FB WH Dealer")
        _active_serial("FB-WH-CONSIGNED", item, cls.cono_wh, "FB-WH-ID-1")
        _active_serial("FB-WH-FLAGGED", item, cls.plain_wh, "FB-WH-ID-2", consigned_out=1)
        _active_serial("FB-WH-PLAIN", item, cls.plain_wh, "FB-WH-ID-3")

    def _fire(self, fb_id, serial):
        """Drive the real webhook entry point for one echoed disposition item."""
        from ffl_integrations.fastbound import webhook as fb
        with patch("ffl_core.firearm.issue_serial") as mock_issue, \
             patch.object(fb, "_serial_on_open_pos_invoice", return_value=False):
            fb._sync_fb_direct_disposal_stock(
                "FB-DISP-ECHO", {"items": [{"id": fb_id, "serial": serial}]})
        return mock_issue

    def test_the_echo_of_a_consigned_gun_issues_nothing(self):
        self.assertFalse(self._fire("FB-WH-ID-1", "FB-WH-CONSIGNED").called)

    def test_the_echo_of_a_gun_flagged_mid_ship_issues_nothing(self):
        """The race window: flagged by ship step 6a, stock not moved yet."""
        self.assertFalse(self._fire("FB-WH-ID-2", "FB-WH-FLAGGED").called)

    def test_a_real_direct_disposal_still_issues(self):
        """The counter-example — otherwise this suite would pass on a webhook
        that does nothing at all."""
        mock_issue = self._fire("FB-WH-ID-3", "FB-WH-PLAIN")
        mock_issue.assert_called_once()
        self.assertEqual(mock_issue.call_args.args[1], "FB-WH-PLAIN")


class ReconcileRecordToFastbound(FrappeTestCase):
    """The whitelisted reconcile endpoint: link an already-booked POS record to
    an EXISTING FastBound record instead of re-pushing (which double-books —
    the 2026-08-21 JV-return incident had to do this via bench console because
    fastbound_id is locked after submit and no sanctioned entrance existed)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ffl_core.setup import test_bootstrap
        test_bootstrap.run()
        cls.company = frappe.db.get_value(
            "Company", {"company_name": "FFL Test"}, "name")

    def _acq(self, code, serial):
        from ffl_core.acquisition import record_firearm_acquisition
        _firearm_item(code, "Rifle")
        _serial(code, serial)
        return record_firearm_acquisition(
            item_code=code, serial=serial, source_type="FFL Dealer",
            source_name="EXAMPLE SUPPLY LLC", source_ffl="9-99-999-99-9X-99998",
            cost=100, company=self.company)

    def _disp(self, code, serial):
        _firearm_item(code, "Rifle")
        _serial(code, serial)
        dest = frappe.db.get_value("ATF FFL Record", {}, "name") or "TEST-FFL-RECORD"
        if not frappe.db.exists("ATF FFL Record", dest):
            frappe.get_doc({"doctype": "ATF FFL Record", "ffl_number": dest,
                "loaded_from": "test-fixture"}).insert(ignore_permissions=True)
        # Serial No has no warehouse/Active status → issue_stock_on_submit skips.
        disp = frappe.get_doc({
            "doctype": "FFL Disposition", "disposition_date": frappe.utils.now_datetime(),
            "disposition_type": "Dealer Transfer", "destination_ffl": dest,
            "company": self.company, "item": code, "serial": serial,
        })
        disp.insert(ignore_permissions=True)
        disp.submit()
        return disp.name

    def _client_mock(self, item):
        fs = MagicMock()
        client = fs.return_value
        client.get_item.return_value = item
        client.find_acquisition_id_by_item.return_value = None
        return fs, client

    def test_acquisition_reconciles_by_guid_and_repoints_serial(self):
        code, serial = "RCE-ACQ", "RCE-ACQ-1"
        a = self._acq(code, serial)
        frappe.db.set_value("FFL Acquisition", a,
            "fastbound_sync_error", "External ID boom", update_modified=False)
        item = {"id": "itm-guid-1", "itemNumber": "01171", "serial": serial,
            "disposed": True, "status": {"id": 3, "name": "Disposed"},
            "dispositionId": "disp-guid-1", "deleted": False}
        fs, client = self._client_mock(item)
        client.find_acquisition_id_by_item.return_value = "acq-guid-1"
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            res = reconcile.reconcile_record_to_fastbound(
                "FFL Acquisition", a, "itm-guid-1")
        self.assertTrue(res["ok"])
        row = frappe.db.get_value("FFL Acquisition", a,
            ["fastbound_id", "fastbound_sync_error", "fastbound_synced_at"], as_dict=True)
        self.assertEqual(row.fastbound_id, "acq-guid-1")
        self.assertFalse(row.fastbound_sync_error)
        self.assertTrue(row.fastbound_synced_at)
        self.assertEqual(
            frappe.db.get_value("Serial No", serial, "fastbound_item_id"), "itm-guid-1")
        # The audit Comment: who linked what, on the doc.
        self.assertTrue(frappe.db.exists("Comment", {
            "reference_doctype": "FFL Acquisition", "reference_name": a,
            "comment_type": "Comment"}))

    def test_acquisition_falls_back_to_item_guid(self):
        # No FastBound acquisition id resolvable → the item GUID stands in
        # (the _adopt_existing_item convention, client.py).
        code, serial = "RCE-ACQ2", "RCE-ACQ2-1"
        a = self._acq(code, serial)
        item = {"id": "itm-guid-2", "itemNumber": "01172", "serial": serial,
            "disposed": False, "status": {"id": 1, "name": "Available"},
            "dispositionId": None, "deleted": False}
        fs, client = self._client_mock(item)
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            res = reconcile.reconcile_record_to_fastbound("FFL Acquisition", a, "itm-guid-2")
        self.assertEqual(res["fastbound_id"], "itm-guid-2")

    def test_disposition_reconciles_by_item_number(self):
        code, serial = "RCE-DISP", "RCE-DISP-1"
        d = self._disp(code, serial)
        item = {"id": "itm-guid-3", "itemNumber": "01173", "serial": serial,
            "disposed": True, "status": {"id": 3, "name": "Disposed"},
            "dispositionId": "disp-guid-3", "deleted": False}
        fs, client = self._client_mock(item)
        client.find_item_by_number.return_value = item
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            res = reconcile.reconcile_record_to_fastbound("FFL Disposition", d, "01173")
        self.assertTrue(res["ok"])
        self.assertEqual(
            frappe.db.get_value("FFL Disposition", d, "fastbound_id"), "disp-guid-3")
        client.find_item_by_number.assert_called_once_with("01173")

    def test_disposition_requires_disposed_item(self):
        # Linking a DISP to an item FastBound still shows in stock would assert
        # a transfer the book denies — fail closed, write nothing.
        code, serial = "RCE-DISP2", "RCE-DISP2-1"
        d = self._disp(code, serial)
        item = {"id": "itm-guid-4", "itemNumber": "01174", "serial": serial,
            "disposed": False, "status": {"id": 1, "name": "Available"},
            "dispositionId": None, "deleted": False}
        fs, client = self._client_mock(item)
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            with self.assertRaises(frappe.ValidationError):
                reconcile.reconcile_record_to_fastbound("FFL Disposition", d, "itm-guid-4")
        self.assertFalse(frappe.db.get_value("FFL Disposition", d, "fastbound_id"))

    def test_serial_mismatch_fails_closed(self):
        code, serial = "RCE-MM", "RCE-MM-1"
        a = self._acq(code, serial)
        item = {"id": "itm-guid-5", "itemNumber": "01175", "serial": "OTHER-SN",
            "disposed": True, "status": {"id": 3, "name": "Disposed"},
            "dispositionId": "x", "deleted": False}
        fs, client = self._client_mock(item)
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            with self.assertRaises(frappe.ValidationError):
                reconcile.reconcile_record_to_fastbound("FFL Acquisition", a, "itm-guid-5")
        self.assertFalse(frappe.db.get_value("FFL Acquisition", a, "fastbound_id"))
        self.assertFalse(frappe.db.get_value("Serial No", serial, "fastbound_item_id"))

    def test_already_linked_draft_and_wrong_doctype_are_refused(self):
        code, serial = "RCE-LNK", "RCE-LNK-1"
        a = self._acq(code, serial)
        frappe.db.set_value("FFL Acquisition", a, "fastbound_id", "existing-guid",
            update_modified=False)
        with self.assertRaises(frappe.ValidationError):
            reconcile.reconcile_record_to_fastbound("FFL Acquisition", a, "whatever")
        # A DRAFT record (the deferred trade-in booking path) is not in the
        # bound book's scope yet — linking it would fake a booking that never
        # happened.
        from ffl_core.acquisition import record_firearm_acquisition
        code2, serial2 = "RCE-DRAFT", "RCE-DRAFT-1"
        _firearm_item(code2, "Rifle")
        _serial(code2, serial2)
        draft = record_firearm_acquisition(
            item_code=code2, serial=serial2, source_type="FFL Dealer",
            source_name="EXAMPLE SUPPLY LLC", source_ffl="9-99-999-99-9X-99998",
            cost=100, company=self.company, submit_now=False)
        self.assertEqual(
            frappe.db.get_value("FFL Acquisition", draft, "docstatus"), 0)
        with self.assertRaises(frappe.ValidationError):
            reconcile.reconcile_record_to_fastbound("FFL Acquisition", draft, "01171")
        self.assertFalse(frappe.db.get_value("FFL Acquisition", draft, "fastbound_id"))
        # And a random doctype is refused outright.
        with self.assertRaises(frappe.ValidationError):
            reconcile.reconcile_record_to_fastbound("Item", code, "whatever")

    def test_role_gate_rejects_guest(self):
        # frappe.only_for(STOCK_ROLES): the whole suite runs as Administrator,
        # which only_for waves through — so the gate line never executes unless
        # someone actually knocks without the role.
        code, serial = "RCE-GATE", "RCE-GATE-1"
        a = self._acq(code, serial)
        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                reconcile.reconcile_record_to_fastbound("FFL Acquisition", a, "01171")
        finally:
            frappe.set_user("Administrator")
        self.assertFalse(frappe.db.get_value("FFL Acquisition", a, "fastbound_id"))


class RetryDispositionPush(FrappeTestCase):
    """`push_disposition_to_fastbound` was a @frappe.task and nothing else: a
    failed push had no way back but the daily `reconcile_failed_pushes`, i.e. up
    to 24 h with a firearm out of the building and no bound-book entry. That is
    where DISP-2026-02000 sat after its obstacle had already been cleared.
    """

    ROW = {"docstatus": 1, "fastbound_id": "", "disposition_type": "FFL Transfer"}

    def _call(self, row=None, *, after=None):
        before = frappe._dict({**self.ROW, **(row or {})})
        after = frappe._dict(after if after is not None else {
            "fastbound_id": "fb-disp-1", "fastbound_sync_error": ""})

        def get_value(_dt, _name, fields, **kw):
            return before if "docstatus" in fields else after

        with patch("frappe.db.exists", return_value=True), \
             patch("frappe.db.get_value", side_effect=get_value), \
             patch("ffl_integrations.fastbound.tasks.push_disposition_to_fastbound") as push:
            return reconcile.retry_disposition_push("DISP-R-1"), push

    def test_a_successful_push_reports_the_new_bound_book_id(self):
        out, push = self._call()
        self.assertTrue(out["ok"])
        self.assertEqual(out["fastbound_id"], "fb-disp-1")
        push.assert_called_once_with("DISP-R-1", _retries=0)

    def test_the_retry_ladder_is_off_because_an_operator_is_waiting(self):
        """The task's default is four attempts with exponential back-off on top of
        a 30s-per-request timeout — up to ~80 seconds of a frozen desk, since this
        wrapper runs the push INLINE so the operator can see the outcome. One
        attempt, then show the error: the button is right there, and
        `reconcile_failed_pushes` retries on its own schedule anyway."""
        _out, push = self._call()
        self.assertEqual(push.call_args.kwargs["_retries"], 0)

    def test_a_switched_off_fastbound_is_reported_not_silently_no_opped(self):
        """The task returns silently when FastBound is disabled — right for a cron,
        useless behind a button: the wrapper would report ok:False carrying
        whatever stale `fastbound_sync_error` the row already held, which the form
        renders as "FastBound refused it again: ?"."""
        with patch("ffl_integrations.integration_gate.fastbound_enabled",
                   return_value=False), \
             patch("ffl_integrations.fastbound.tasks"
                   ".push_disposition_to_fastbound") as push:
            with self.assertRaises(frappe.ValidationError) as ctx:
                reconcile.retry_disposition_push("DISP-R-1")
        self.assertIn("switched off", str(ctx.exception))
        push.assert_not_called()

    def test_a_still_failing_push_reports_the_reason_rather_than_claiming_success(self):
        out, _ = self._call(after={
            "fastbound_id": "", "fastbound_sync_error": "400: item is held"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["fastbound_sync_error"], "400: item is held")

    def test_a_retail_sale_is_refused(self):
        """THE compliance guard. Retail Sale is a 4473 disposition FastBound's own
        wizard owns; pushing one from here would record a transfer to an
        individual as a plain bound-book entry. The task itself no-ops silently on
        it, so a wrapper that forgot this check would look like it had worked."""
        with self.assertRaises(frappe.ValidationError):
            self._call({"disposition_type": "Retail Sale"})

    def test_an_already_linked_record_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._call({"fastbound_id": "fb-existing"})

    def test_a_draft_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._call({"docstatus": 0})

    def test_the_push_is_not_even_attempted_when_a_guard_fires(self):
        for row in ({"disposition_type": "Retail Sale"}, {"fastbound_id": "fb-x"},
                    {"docstatus": 0}):
            with patch("ffl_integrations.fastbound.tasks"
                       ".push_disposition_to_fastbound") as push:
                with self.assertRaises(frappe.ValidationError):
                    self._call(row)
            push.assert_not_called()

    def test_role_gate_rejects_guest(self):
        # Same note as reconcile_record_to_fastbound's gate above: the suite runs
        # as Administrator, whom only_for waves through.
        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                reconcile.retry_disposition_push("DISP-R-1")
        finally:
            frappe.set_user("Administrator")

    def test_the_form_button_and_the_server_agree_on_what_is_pushable(self):
        """The JS decides whether the button is DRAWN. A drifted copy of the list
        draws it on a Retail Sale, which is an invitation to a compliance
        incident even though the server refuses it."""
        import re
        from pathlib import Path

        from ffl_integrations.fastbound.client import PUSHABLE_DISPOSITION_TYPES

        js = Path(frappe.get_app_path("ffl_core", "ffl_core", "doctype",
            "ffl_disposition", "ffl_disposition.js")).read_text()
        block = re.search(
            r"FFL_DISPOSITION_PUSHABLE_TYPES = new Set\(\[(.*?)\]\)", js, re.S)
        self.assertIsNotNone(block, "the button's type list is gone or was renamed")
        self.assertEqual(
            set(re.findall(r"'([^']+)'", block.group(1))),
            set(PUSHABLE_DISPOSITION_TYPES))


class NotInBoundBook(FrappeTestCase):
    """mark_not_in_bound_book: a record a person has checked off the bound book (an antique
    booked by mistake, DISP-2026-02131) leaves the retry, the stuck digest and the Dashboard."""

    def _disposition(self, name, **fields):
        from ffl_integrations.fastbound.client import TRANSFER_DISPOSITION_TYPES
        doc = frappe.new_doc("FFL Disposition")
        doc.update({"name": name, "docstatus": 1, "disposition_type": sorted(TRANSFER_DISPOSITION_TYPES)[0],
            "fastbound_sync_error": "[TERMINAL] serial not found in FastBound", **fields})
        doc.db_insert()
        return name

    def test_a_marked_record_leaves_every_waiting_list(self):
        from ffl_integrations.fastbound import tasks
        name = self._disposition("NIB-DISP-1")
        self.assertIn(name, [r["name"] for r in tasks.unsynced_records()])

        out = reconcile.mark_not_in_bound_book("FFL Disposition", name, "Antique, not a GCA firearm")
        self.assertTrue(out["ok"])
        self.assertEqual(frappe.db.get_value("FFL Disposition", name, "fastbound_sync_error"),
            "[NOT A&D] Antique, not a GCA firearm")
        self.assertNotIn(name, [r["name"] for r in tasks.unsynced_records()])
        with _p.object(tasks, "fastbound_enabled", return_value=True), \
                _p.object(tasks, "notify_admins") as digest:
            tasks.alert_stuck_fastbound()
        self.assertFalse(any(name in str(c) for c in digest.call_args_list))
        comment = frappe.get_all("Comment", filters={"reference_doctype": "FFL Disposition",
            "reference_name": name, "comment_type": "Info"}, pluck="content")
        self.assertTrue(comment and "serial not found in FastBound" in comment[0])

    def test_refuses_without_a_reason_or_for_a_record_already_in_the_book(self):
        name = self._disposition("NIB-DISP-2")
        with self.assertRaises(frappe.ValidationError):
            reconcile.mark_not_in_bound_book("FFL Disposition", name, "  ")
        linked = self._disposition("NIB-DISP-3", fastbound_id="fb-linked")
        with self.assertRaises(frappe.ValidationError):
            reconcile.mark_not_in_bound_book("FFL Disposition", linked, "antique")
        with self.assertRaises(frappe.ValidationError):
            reconcile.mark_not_in_bound_book("Sales Invoice", name, "antique")

    def test_a_marked_record_is_never_pushed_and_keeps_its_marker(self):
        from ffl_integrations.fastbound import tasks
        name = self._disposition("NIB-DISP-4", fastbound_sync_error="[NOT A&D] antique")
        with _p.object(tasks, "fastbound_enabled", return_value=True), \
                _p.object(tasks.FastBoundClient, "from_settings") as fs:
            tasks.push_disposition_to_fastbound(name, _retries=0, _commit=False)
        fs.assert_not_called()
        self.assertEqual(frappe.db.get_value("FFL Disposition", name, "fastbound_sync_error"),
            "[NOT A&D] antique")

    def test_a_push_failing_while_the_record_is_marked_leaves_the_marker(self):
        from ffl_integrations.fastbound import tasks
        name = self._disposition("NIB-DISP-5")

        def mark_then_fail(_doc):
            reconcile.mark_not_in_bound_book("FFL Disposition", name, "antique")
            raise FastBoundError("serial not found", status_code=400, terminal=True)

        client = MagicMock()
        client.post_disposition.side_effect = mark_then_fail
        with _p.object(tasks, "fastbound_enabled", return_value=True), \
                _p.object(tasks.FastBoundClient, "from_settings", return_value=client):
            tasks.push_disposition_to_fastbound(name, _retries=0, _commit=False)
        self.assertEqual(frappe.db.get_value("FFL Disposition", name, "fastbound_sync_error"),
            "[NOT A&D] antique")

    def test_the_error_write_never_overwrites_a_marker(self):
        from ffl_integrations.fastbound import tasks
        name = self._disposition("NIB-DISP-6", fastbound_sync_error="[NOT A&D] antique")
        self.assertFalse(tasks._record_push_error("FFL Disposition", name, "[TERMINAL] refused"))
        self.assertEqual(frappe.db.get_value("FFL Disposition", name, "fastbound_sync_error"),
            "[NOT A&D] antique")

    def test_a_cleared_mark_puts_the_record_back_and_retry_refuses_a_marked_one(self):
        from ffl_integrations.fastbound import tasks
        name = self._disposition("NIB-DISP-7")
        reconcile.mark_not_in_bound_book("FFL Disposition", name, "antique")
        with _p.object(reconcile, "fastbound_enabled", create=True, return_value=True), \
                _p("ffl_integrations.integration_gate.fastbound_enabled", return_value=True), \
                self.assertRaises(frappe.ValidationError) as refused:
            reconcile.retry_disposition_push(name)
        self.assertIn("not belonging in the bound book", str(refused.exception))
        reconcile.clear_not_in_bound_book("FFL Disposition", name)
        self.assertIn(name, [r["name"] for r in tasks.unsynced_records()])
        with self.assertRaises(frappe.ValidationError):
            reconcile.clear_not_in_bound_book("FFL Disposition", name)  # nothing to clear now


class OffBookAntiqueStaysOffBook(FrappeTestCase):
    """Once a [NOT A&D] record's gun is antique (its Item flagged, or its serial moved to an
    antique Item), clearing the mark or linking it to FastBound would put an antique back on
    the books, with no 4473 or disposition ever to close it. Both are refused until the gun
    is regulated again."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ffl_core.setup import test_bootstrap
        test_bootstrap.run()
        cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

    def _marked_acq(self, code, serial):
        a = ReconcileRecordToFastbound._acq(self, code, serial)
        reconcile.mark_not_in_bound_book("FFL Acquisition", a, "antique booked by mistake")
        return a

    def _set_antique(self, code, value):
        frappe.db.set_value("Item", code, "is_antique", value, update_modified=False)
        frappe.db.value_cache.clear()  # is_antique_item reads with cache=True

    def test_clear_refused_while_item_antique_allowed_once_regulated(self):
        code, serial = "OBA-ITEM", "OBA-ITEM-1"
        a = self._marked_acq(code, serial)
        self._set_antique(code, 1)
        with self.assertRaisesRegex(frappe.ValidationError, "Antique"):
            reconcile.clear_not_in_bound_book("FFL Acquisition", a)
        self.assertTrue(frappe.db.get_value("FFL Acquisition", a, "fastbound_sync_error")
            .startswith("[NOT A&D] "))
        self._set_antique(code, 0)
        self.assertTrue(reconcile.clear_not_in_bound_book("FFL Acquisition", a)["ok"])

    def test_clear_refused_when_serial_moved_to_an_antique_item(self):
        code, serial = "OBA-REG", "OBA-REG-1"
        a = self._marked_acq(code, serial)
        _firearm_item("OBA-ANT", "Rifle")
        self._set_antique("OBA-ANT", 1)
        frappe.db.set_value("Serial No", serial, "item_code", "OBA-ANT", update_modified=False)
        with self.assertRaisesRegex(frappe.ValidationError, "Antique"):
            reconcile.clear_not_in_bound_book("FFL Acquisition", a)

    def test_link_refused_while_item_antique(self):
        code, serial = "OBA-LINK", "OBA-LINK-1"
        a = self._marked_acq(code, serial)
        self._set_antique(code, 1)
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
                self.assertRaisesRegex(frappe.ValidationError, "Antique"):
            reconcile.reconcile_record_to_fastbound("FFL Acquisition", a, "itm-guid-oba")
        fs.assert_not_called()
        self.assertFalse(frappe.db.get_value("FFL Acquisition", a, "fastbound_id"))
        # Regulated again: the link goes through.
        self._set_antique(code, 0)
        item = {"id": "itm-guid-oba", "itemNumber": "01199", "serial": serial,
            "disposed": False, "dispositionId": None, "deleted": False}
        fs, _client_ = ReconcileRecordToFastbound._client_mock(self, item)
        with _p.object(reconcile.FastBoundClient, "from_settings", fs):
            self.assertTrue(reconcile.reconcile_record_to_fastbound(
                "FFL Acquisition", a, "itm-guid-oba")["ok"])

    def test_disposition_of_an_antique_cannot_be_cleared_or_linked(self):
        code, serial = "OBA-DISP", "OBA-DISP-1"
        d = ReconcileRecordToFastbound._disp(self, code, serial)
        reconcile.mark_not_in_bound_book("FFL Disposition", d, "antique")
        self._set_antique(code, 1)
        with self.assertRaisesRegex(frappe.ValidationError, "Antique"):
            reconcile.clear_not_in_bound_book("FFL Disposition", d)
        with _p.object(reconcile.FastBoundClient, "from_settings") as fs, \
                self.assertRaisesRegex(frappe.ValidationError, "Antique"):
            reconcile.reconcile_record_to_fastbound("FFL Disposition", d, "01173")
        fs.assert_not_called()
