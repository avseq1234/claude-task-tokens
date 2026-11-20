import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import task_tokens as tt  # noqa: E402


def assistant(rid, ts, inp=0, out=0, cw=0, cr=0, model="claude-opus-5-5", tool_use=None):
    content = [{"type": "text", "text": "ok"}]
    if tool_use:
        content.append({"type": "tool_use", "id": tool_use, "name": "Agent", "input": {}})
    return {
        "type": "assistant", "requestId": rid, "timestamp": ts,
        "message": {"model": model, "content": content, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "cache_creation": {"ephemeral_5m_input_tokens": cw, "ephemeral_1h_input_tokens": 0},
        }},
    }


def user(text, ts="2026-11-01T00:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_agent(session_dir, aid, description, tool_use_id, records):
    base = os.path.join(session_dir, "subagents", f"agent-{aid}")
    write_jsonl(base + ".jsonl", records)
    with open(base + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"description": description, "toolUseId": tool_use_id}, f)


class ParseTagTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(tt.parse_tag("[checkout] 派工"), ("checkout", None, None))
        self.assertEqual(tt.parse_tag("[checkout/T3] impl"), ("checkout", "t3", None))
        self.assertEqual(tt.parse_tag("  [Checkout/T3/Review] x"), ("checkout", "t3", "review"))
        self.assertEqual(tt.parse_tag("［checkout/T3］ 全形"), ("checkout", "t3", None))

    def test_invalid(self):
        self.assertIsNone(tt.parse_tag("實作 [checkout/T3]"))
        self.assertIsNone(tt.parse_tag("[checkout T3]"))
        self.assertIsNone(tt.parse_tag("[結帳/T3]"))
        self.assertIsNone(tt.parse_tag("[docs](https://example.com) 更新"))
        self.assertIsNone(tt.parse_tag(None))

    def test_clean_prompt(self):
        raw = ('<command-name>/x</command-name><command-message>x</command-message>'
               '<command-args></command-args><local-command-stdout>hi</local-command-stdout>'
               '\n<pasted_content id="1">\n[checkout/T2] 做事\n</pasted_content id="1">')
        self.assertEqual(tt.parse_tag(tt.clean_prompt(raw)), ("checkout", "t2", None))


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        proj = os.path.join(root, "-proj-shop")

        # B: orchestrator session for spec "checkout".
        s1 = "11111111-aaaa"
        write_jsonl(os.path.join(proj, s1 + ".jsonl"), [
            user("[checkout] 依 spec.md 派工"),
            # same request written twice (one line per content block)
            assistant("r1", "2026-11-01T00:00:01Z", inp=100, out=10),
            assistant("r1", "2026-11-01T00:00:01Z", inp=100, out=10, tool_use="tu_impl"),
            assistant("r2", "2026-11-01T00:00:02Z", inp=50, out=5, tool_use="tu_review"),
        ])
        sdir = os.path.join(proj, s1)
        write_agent(sdir, "impl", "[checkout/T3] implement", "tu_impl", [
            user("go"),
            assistant("r3", "2026-11-01T00:01:00Z", inp=1000, out=200, cw=500, cr=2000,
                      tool_use="tu_nested"),
        ])
        write_agent(sdir, "nested", "helper without tag", "tu_nested", [
            user("look around"),
            assistant("r4", "2026-11-01T00:01:30Z", inp=300, out=30),
        ])
        write_agent(sdir, "review", "[checkout/T3/review] review T3", "tu_review", [
            user("review"),
            assistant("r5", "2026-11-01T00:02:00Z", inp=400, out=40),
        ])
        write_agent(sdir, "t4", "[checkout/T4] implement", "tu_x", [
            user("go"),
            assistant("r6", "2026-11-01T00:03:00Z", inp=700, out=70),
        ])

        # A: later session that continues T3 directly.
        write_jsonl(os.path.join(proj, "22222222-bbbb.jsonl"), [
            user("[checkout/T3/fix] 修 review 意見", ts="2026-11-02T00:00:00Z"),
            assistant("r7", "2026-11-02T00:00:01Z", inp=200, out=20),
        ])

        # Case 2: untagged single-prompt session, plus a resumed copy of it.
        write_jsonl(os.path.join(proj, "33333333-cccc.jsonl"), [
            user("把首頁標題改成 Hello", ts="2026-11-03T00:00:00Z"),
            {"type": "ai-title", "aiTitle": "Change homepage title"},
            assistant("r8", "2026-11-03T00:00:01Z", inp=80, out=8),
        ])
        write_jsonl(os.path.join(proj, "44444444-dddd.jsonl"), [
            user("把首頁標題改成 Hello", ts="2026-11-03T00:00:00Z"),
            assistant("r8", "2026-11-03T00:00:01Z", inp=80, out=8),  # repeated
            assistant("r9", "2026-11-04T00:00:01Z", inp=10, out=1),
        ])
        self.rows = tt.collect(root)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, spec, task, phase=None):
        return self.rows[(spec, task, phase)]

    def test_overhead_is_deduplicated(self):
        oh = self.get("checkout", None)
        self.assertEqual(oh.requests, 2)
        self.assertEqual(oh.input, 150)

    def test_nested_agent_inherits_parent_tag(self):
        t3 = self.get("checkout", "t3")
        self.assertEqual(t3.requests, 2)          # r3 + nested r4
        self.assertEqual(t3.input, 1300)
        self.assertEqual(t3.cache_write, 500)
        self.assertEqual(t3.cache_read, 2000)

    def test_phases_and_sessions_merge_into_task(self):
        rows = {(r.spec, r.task): r for r in tt.roll_up(self.rows, by_phase=False)}
        t3 = rows[("checkout", "t3")]
        self.assertEqual(t3.requests, 4)          # r3, r4, r5 (review), r7 (fix)
        self.assertEqual(len(t3.sessions), 2)

    def test_untagged_session_and_resume_dedupe(self):
        untagged = [r for k, r in self.rows.items() if k[0] == tt.UNTAGGED]
        self.assertEqual(sum(r.requests for r in untagged), 2)   # r8 once + r9
        labels = sorted(r.task for r in untagged)
        self.assertEqual(labels[0], "33333333 Change homepage title")

    def test_cost(self):
        r = tt.Request("x", "", "claude-opus-5-5", None, input=1_000_000, cache_5m=1_000_000,
                       cache_read=1_000_000, output=1_000_000)
        self.assertAlmostEqual(r.cost(), 4 + 5 + 0.2 + 20)
        r.speed = "fast"
        self.assertAlmostEqual(r.cost(), 2 * (4 + 5 + 0.2 + 20))
        self.assertIsNone(tt.Request("x", "", "claude-unknown", None).cost())
        self.assertEqual(tt.normalize_model("claude-opus-5-5[1m]"), "claude-opus-5-5")

    def test_allocate_overhead(self):
        rows = tt.allocate_overhead(tt.roll_up(self.rows, by_phase=False))
        checkout = [r for r in rows if r.spec == "checkout"]
        self.assertTrue(all(r.task is not None for r in checkout))
        self.assertEqual(sum(r.input for r in checkout), 150 + 1300 + 400 + 200 + 700)


if __name__ == "__main__":
    unittest.main()
