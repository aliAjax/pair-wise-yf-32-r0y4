import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class TransferTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2"):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": 5, "wait_days": 500, "willing": True, "clinical_match": 9})

    def accepted_allocation(self):
        donor, candidate = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.svc.accept(allocation["id"], "nurse-h2", "hospital", "H2", {"expected_revision": 1})
        return allocation

    def events(self, allocation_id):
        return self.svc.timeline(allocation_id, "auditor", "")["timeline"]

    def test_timeline_attributes_actor_role_and_hospital(self):
        allocation = self.accepted_allocation()
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        self.svc.initiate_handoff(allocation["id"], "dr-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.5})
        self.svc.accept_handoff(allocation["id"], "nurse-h2", "hospital", "H2", {})
        timeline = self.events(allocation["id"])
        seen = [(e["action"], e["actor"], e["role"], e["hospital"]) for e in timeline]
        self.assertIn(("proposed", "allocator", "allocation_officer", None), seen)
        self.assertIn(("accepted", "nurse-h2", "hospital", "H2"), seen)
        self.assertIn(("transit_started", "allocator", "allocation_officer", None), seen)
        self.assertIn(("handoff_initiated", "dr-h1", "hospital", "H1"), seen)
        self.assertIn(("handoff_accepted", "nurse-h2", "hospital", "H2"), seen)
        self.assertTrue(all(e["outcome"] == "applied" for e in timeline))

    def test_timeline_hospital_scoping(self):
        allocation = self.accepted_allocation()
        self.assertEqual(self.svc.timeline(allocation["id"], "hospital", "H1")["allocation_id"], allocation["id"])
        self.assertEqual(self.svc.timeline(allocation["id"], "hospital", "H2")["allocation_id"], allocation["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.timeline(allocation["id"], "hospital", "H3")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.timeline(allocation["id"], "viewer", "")
        self.assertEqual(ctx.exception.status, 403)

    def test_duplicate_event_recorded_once(self):
        allocation = self.accepted_allocation()
        first = self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "transit_started", "submitted_at": iso(self.now), "detail": {"cold_chain_temp": 4.0}})
        self.assertEqual(first["result"], "applied")
        self.assertEqual(first["event"]["hospital"], None)
        self.assertEqual(first["allocation"]["status"], "in_transit")
        again = self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "transit_started", "submitted_at": iso(self.now + timedelta(minutes=30)), "detail": {"cold_chain_temp": 4.0}})
        self.assertEqual(again["result"], "duplicate")
        transit_events = [e for e in self.events(allocation["id"]) if e["action"] == "transit_started"]
        self.assertEqual(len(transit_events), 1)
        self.assertEqual(again["allocation"]["status"], "in_transit")

    def test_late_event_backfills_without_regression(self):
        allocation = self.accepted_allocation()
        done = self.svc.submit_event(allocation["id"], "nurse-h2", "hospital", "H2", {"action": "handoff_accepted", "submitted_at": iso(self.now)})
        self.assertEqual(done["result"], "applied")
        self.assertEqual(done["allocation"]["status"], "handed_off")
        late = self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "transit_started", "submitted_at": iso(self.now - timedelta(hours=1))})
        self.assertEqual(late["result"], "applied")
        self.assertEqual(late["allocation"]["status"], "handed_off")
        timeline = self.events(allocation["id"])
        self.assertIn("transit_started", [e["action"] for e in timeline])
        self.assertEqual(self.svc.get_allocation(allocation["id"], "auditor", "")["status"], "handed_off")

    def test_conflicting_completion_and_withdrawal_resolved_by_submission_time(self):
        allocation = self.accepted_allocation()
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        withdrawn = self.svc.submit_event(allocation["id"], "nurse-h2", "hospital", "H2", {"action": "withdrawn", "submitted_at": iso(self.now + timedelta(minutes=10)), "detail": {"reason": "患者发热"}})
        self.assertEqual(withdrawn["result"], "applied")
        self.assertEqual(withdrawn["allocation"]["status"], "withdrawn")
        self.assertEqual(withdrawn["allocation"]["donor_status"], "available")
        timeline = {e["action"]: e["outcome"] for e in self.events(allocation["id"])}
        self.assertEqual(timeline["transit_started"], "conflict")
        completed = self.svc.submit_event(allocation["id"], "dr-h1", "hospital", "H1", {"action": "handoff_accepted", "submitted_at": iso(self.now + timedelta(minutes=20))})
        self.assertEqual(completed["result"], "applied")
        self.assertEqual(completed["allocation"]["status"], "handed_off")
        timeline = {e["action"]: e["outcome"] for e in self.events(allocation["id"])}
        self.assertEqual(timeline["withdrawn"], "conflict")
        self.assertEqual(timeline["handoff_accepted"], "applied")
        stale = self.svc.submit_event(allocation["id"], "nurse-h2", "hospital", "H2", {"action": "withdrawn", "submitted_at": iso(self.now + timedelta(minutes=5)), "detail": {"reason": "重复上报"}})
        self.assertEqual(stale["result"], "conflict")
        self.assertEqual(stale["allocation"]["status"], "handed_off")

    def test_conflict_resolution_depends_on_submission_not_arrival(self):
        first = self.accepted_allocation()
        self.svc.submit_event(first["id"], "nurse-h2", "hospital", "H2", {"action": "withdrawn", "submitted_at": iso(self.now + timedelta(minutes=20)), "detail": {"reason": "先登记的撤回"}})
        late_completion = self.svc.submit_event(first["id"], "dr-h1", "hospital", "H1", {"action": "handoff_accepted", "submitted_at": iso(self.now + timedelta(minutes=10))})
        self.assertEqual(late_completion["result"], "conflict")
        self.assertEqual(late_completion["allocation"]["status"], "withdrawn")

        second = self.accepted_allocation()
        self.svc.submit_event(second["id"], "dr-h1", "hospital", "H1", {"action": "handoff_accepted", "submitted_at": iso(self.now + timedelta(minutes=10))})
        later_withdrawal = self.svc.submit_event(second["id"], "nurse-h2", "hospital", "H2", {"action": "withdrawn", "submitted_at": iso(self.now + timedelta(minutes=20)), "detail": {"reason": "后到的撤回"}})
        self.assertEqual(later_withdrawal["result"], "applied")
        self.assertEqual(later_withdrawal["allocation"]["status"], "withdrawn")
        for allocation_id in (first["id"], second["id"]):
            timeline = {e["action"]: e["outcome"] for e in self.events(allocation_id)}
            self.assertEqual(timeline["handoff_accepted"], "conflict")
            self.assertEqual(timeline["withdrawn"], "applied")

    def test_backfill_initial_record_for_historical_allocation(self):
        allocation = self.accepted_allocation()
        self.svc.repo.conn.execute("DELETE FROM transfer_events WHERE allocation_id=?", (allocation["id"],))
        timeline = self.events(allocation["id"])
        self.assertEqual(len(timeline), 1)
        initial = timeline[0]
        self.assertEqual((initial["action"], initial["actor"], initial["role"], initial["outcome"]), ("accepted", "system", "system", "applied"))
        self.assertTrue(initial["detail"]["backfilled"])

        self.svc.repo.conn.execute("DELETE FROM transfer_events WHERE allocation_id=?", (allocation["id"],))
        withdrawn = self.svc.submit_event(allocation["id"], "nurse-h2", "hospital", "H2", {"action": "withdrawn", "submitted_at": iso(self.now + timedelta(hours=1)), "detail": {"reason": "患者拒绝"}})
        self.assertEqual(withdrawn["result"], "applied")
        self.assertEqual(withdrawn["allocation"]["status"], "withdrawn")
        self.assertEqual([e["action"] for e in self.events(allocation["id"])], ["accepted", "withdrawn"])

    def test_expired_allocation_marks_new_events_conflict(self):
        allocation = self.accepted_allocation()
        self.svc.repo.conn.execute("UPDATE donors SET expires_at=? WHERE id=(SELECT donor_id FROM allocations WHERE id=?)", (iso(self.now - timedelta(hours=1)), allocation["id"]))
        late = self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "transit_started", "submitted_at": iso(self.now)})
        self.assertEqual(late["result"], "conflict")
        self.assertEqual(late["allocation"]["status"], "expired")
        timeline = {e["action"]: e["outcome"] for e in self.events(allocation["id"])}
        self.assertEqual(timeline["expired"], "applied")
        self.assertEqual(timeline["transit_started"], "conflict")

    def test_submit_event_validation_and_scoping(self):
        allocation = self.accepted_allocation()
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_event(allocation["id"], "outsider", "hospital", "H3", {"action": "accepted"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_event(allocation["id"], "aud", "auditor", "", {"action": "accepted"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "teleport"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_event(allocation["id"], "dispatcher", "coordinator", "", {"action": "transit_started", "detail": {"cold_chain_temp": 12}})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")


if __name__ == "__main__": unittest.main()
