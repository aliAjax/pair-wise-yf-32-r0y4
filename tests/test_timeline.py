import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class TimelineReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db")
        self.now = utcnow()

    def tearDown(self):
        self.tmp.cleanup()

    def donor(self, hospital="H1"):
        return self.svc.register_donor("coord", "coordinator", {
            "blood_type": "O", "organ": "kidney", "hospital": hospital, "region": "East",
            "available_at": iso(self.now - timedelta(days=3)),
            "expires_at": iso(self.now + timedelta(days=2)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2"):
        return self.svc.register_candidate("coord", "coordinator", {
            "patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital,
            "region": "East", "urgency": 5, "wait_days": 500, "willing": True, "clinical_match": 9})

    def allocate(self):
        donor, cand = self.donor(), self.candidate()
        return self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": cand["id"]})

    def events_of(self, allocation_id, role="auditor", hospital=""):
        return self.svc.timeline(allocation_id, role, hospital)["events"]

    def test_normal_flow_timeline_shows_actor_and_institution(self):
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(aid, "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.svc.initiate_handoff(aid, "hospital-h1", "hospital", "H1", {"expected_revision": 3, "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.svc.accept_handoff(aid, "hospital-h2", "hospital", "H2", {})
        self.svc.implant(aid, "allocator", "allocation_officer", {})
        events = self.events_of(aid)
        self.assertEqual([e["action"] for e in events],
                         ["allocation_proposed", "allocation_accepted", "transfer_started",
                          "handoff_initiated", "handoff_accepted", "organ_implanted"])
        # 每个动作都能看出由谁在哪个机构提交
        by_action = {e["action"]: e for e in events}
        self.assertEqual(by_action["allocation_accepted"]["actor"], "hospital-h2")
        self.assertEqual(by_action["allocation_accepted"]["hospital"], "H2")
        self.assertEqual(by_action["handoff_initiated"]["actor"], "hospital-h1")
        self.assertEqual(by_action["handoff_initiated"]["hospital"], "H1")
        self.assertEqual(by_action["organ_implanted"]["actor"], "allocator")
        for e in events:
            self.assertEqual(e["status"], "effective")

    def test_duplicate_or_late_submission_adds_single_record(self):
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        # 接收医院重复/晚到提交同一个确认动作
        again = self.svc.submit_event(aid, "hospital-h2", "hospital", "H2", {"action": "allocation_accepted"})
        events = self.events_of(aid)
        self.assertEqual(len([e for e in events if e["action"] == "allocation_accepted"]), 1)
        self.assertEqual(again["action"], "allocation_accepted")
        # 显式 event_id 幂等
        eid = "evt-abc-123"
        first = self.svc.submit_event(aid, "allocator", "allocation_officer", "", {"action": "transfer_started", "event_id": eid})
        second = self.svc.submit_event(aid, "allocator", "allocation_officer", "", {"action": "transfer_started", "event_id": eid})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len([e for e in self.events_of(aid) if e["action"] == "transfer_started"]), 1)

    def test_race_completion_vs_withdraw_last_submitted_wins(self):
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(aid, "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        # 来源医院提交撤回、接收医院提交完成，时刻不同
        t1 = iso(self.now + timedelta(minutes=10))
        t2 = iso(self.now + timedelta(minutes=20))
        # 情况一：撤回先到、完成后到 -> 完成生效，撤回被压成冲突
        self.svc.submit_event(aid, "hospital-h1", "hospital", "H1", {"action": "allocation_withdrawn", "submitted_at": t1, "detail": {"reason": "cancel"}})
        self.svc.submit_event(aid, "hospital-h2", "hospital", "H2", {"action": "handoff_accepted", "submitted_at": t2})
        events = self.events_of(aid)
        withdraw = [e for e in events if e["action"] == "allocation_withdrawn"][0]
        complete = [e for e in events if e["action"] == "handoff_accepted"][0]
        self.assertEqual(complete["status"], "effective")
        self.assertEqual(withdraw["status"], "conflict")

    def test_race_withdraw_later_wins(self):
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(aid, "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        t1 = iso(self.now + timedelta(minutes=10))
        t2 = iso(self.now + timedelta(minutes=20))
        # 情况二：完成先到、撤回后到 -> 撤回生效，完成被压成冲突
        self.svc.submit_event(aid, "hospital-h2", "hospital", "H2", {"action": "handoff_accepted", "submitted_at": t1})
        self.svc.submit_event(aid, "hospital-h1", "hospital", "H1", {"action": "allocation_withdrawn", "submitted_at": t2, "detail": {"reason": "cancel"}})
        events = self.events_of(aid)
        withdraw = [e for e in events if e["action"] == "allocation_withdrawn"][0]
        complete = [e for e in events if e["action"] == "handoff_accepted"][0]
        self.assertEqual(withdraw["status"], "effective")
        self.assertEqual(complete["status"], "conflict")

    def test_late_withdraw_after_completed_handoff_does_not_rollback(self):
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        transit = self.svc.mark_transit(aid, "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        self.svc.initiate_handoff(aid, "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.svc.accept_handoff(aid, "hospital-h2", "hospital", "H2", {})
        # 交接已完成后，来源医院晚到的撤回不能退回已完成步骤
        late = iso(self.now + timedelta(hours=2))
        self.svc.submit_event(aid, "hospital-h1", "hospital", "H1", {"action": "allocation_withdrawn", "submitted_at": late, "detail": {"reason": "late cancel"}})
        events = self.events_of(aid)
        withdraw = [e for e in events if e["action"] == "allocation_withdrawn"][0]
        self.assertEqual(withdraw["status"], "conflict")
        # 正式状态仍为 handed_off，未被退回
        current = self.svc.get_allocation(aid, "auditor", "")
        self.assertEqual(current["status"], "handed_off")

    def test_hospital_only_sees_own_institution_allocation(self):
        alloc = self.allocate()
        aid = alloc["id"]
        # H2（接收医院）可以看本机构分配的时间线
        self.assertEqual(len(self.events_of(aid, "hospital", "H2")), 1)
        # H3（无关医院）不能查看
        with self.assertRaises(ApiError) as ctx:
            self.events_of(aid, "hospital", "H3")
        self.assertEqual(ctx.exception.status, 403)
        # 医院也不能提交无关分配的事件
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_event(aid, "hospital-h3", "hospital", "H3", {"action": "allocation_accepted"})
        self.assertEqual(ctx.exception.status, 403)

    def test_historical_allocation_gets_initial_record(self):
        # 直接构造一个“没有时间线”的历史分配（走正式流程后清空时间线模拟）
        alloc = self.allocate()
        aid = alloc["id"]
        self.svc.accept(aid, "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(aid, "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        # 清空时间线，模拟历史分配无时间线
        self.svc.repo.conn.execute("DELETE FROM timeline_events WHERE allocation_id=?", (aid,))
        events = self.events_of(aid)
        self.assertEqual(len(events), 1)
        initial = events[0]
        self.assertTrue(initial["detail_json"])
        self.assertEqual(initial["action"], "transfer_started")
        self.assertEqual(initial["actor"], "system")
        # 补录后再提交同动作不应重复
        again = self.svc.submit_event(aid, "allocator", "allocation_officer", "", {"action": "transfer_started"})
        self.assertEqual(again["id"], initial["id"])

    def test_coordinator_submits_event(self):
        alloc = self.allocate()
        aid = alloc["id"]
        ev = self.svc.submit_event(aid, "coord", "coordinator", "", {"action": "allocation_accepted"})
        self.assertEqual(ev["role"], "coordinator")
        self.assertEqual(ev["hospital"], "")
        self.assertEqual(ev["status"], "effective")


if __name__ == "__main__":
    unittest.main()
