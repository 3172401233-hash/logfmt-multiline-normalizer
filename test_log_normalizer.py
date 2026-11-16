import unittest

from log_normalizer import (
    StreamNormalizer,
    normalize_level,
    parse_logfmt,
)


def run_chunks(chunks, **kwargs):
    norm = StreamNormalizer(**kwargs)
    records = []
    for chunk in chunks:
        records.extend(norm.feed(chunk))
    records.extend(norm.close())
    return records


class TestLogfmtParsing(unittest.TestCase):
    def test_simple_line(self):
        records = run_chunks([
            '2026-11-16T10:00:00Z level=info trace_id=abc123 msg="hello world" svc=gw\n'
        ])
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.timestamp, "2026-11-16T10:00:00Z")
        self.assertEqual(rec.level, "INFO")
        self.assertEqual(rec.trace_id, "abc123")
        self.assertEqual(rec.msg, "hello world")
        self.assertEqual(rec.fields, {"svc": "gw"})
        self.assertEqual(rec.origin, "record")

    def test_out_of_order_fields(self):
        records = run_chunks([
            '2026-11-16T10:00:00.123+08:00 msg="hi" svc=gw level=warn trace_id=t-1 extra=yes\n'
        ])
        rec = records[0]
        self.assertEqual(rec.timestamp, "2026-11-16T10:00:00.123+08:00")
        self.assertEqual(rec.level, "WARN")
        self.assertEqual(rec.trace_id, "t-1")
        self.assertEqual(rec.msg, "hi")
        self.assertEqual(rec.fields, {"svc": "gw", "extra": "yes"})

    def test_escaped_quotes_and_backslash(self):
        fields = parse_logfmt(r'msg="he said \"hi\"\nnext" path="C:\\tmp"')
        self.assertEqual(fields["msg"], 'he said "hi"\nnext')
        self.assertEqual(fields["path"], "C:\\tmp")

    def test_duplicate_keys_become_list(self):
        fields = parse_logfmt("tag=a tag=b tag=c once=1")
        self.assertEqual(fields["tag"], ["a", "b", "c"])
        self.assertEqual(fields["once"], "1")

    def test_unknown_level_preserved(self):
        records = run_chunks(["2026-11-16 10:00:00 level=verbose msg=x\n"])
        rec = records[0]
        self.assertEqual(rec.level, "UNKNOWN")
        self.assertEqual(rec.fields["level_raw"], "verbose")

    def test_level_aliases(self):
        self.assertEqual(normalize_level("warning"), "WARN")
        self.assertEqual(normalize_level("ERR"), "ERROR")
        self.assertEqual(normalize_level("critical"), "CRITICAL")
        self.assertEqual(normalize_level("nonsense"), "UNKNOWN")
        self.assertEqual(normalize_level(None), "UNKNOWN")

    def test_missing_timestamp(self):
        records = run_chunks(['level=error trace_id=t-9 msg="no clock here"\n'])
        rec = records[0]
        self.assertIsNone(rec.timestamp)
        self.assertEqual(rec.level, "ERROR")
        self.assertEqual(rec.trace_id, "t-9")
        self.assertEqual(rec.msg, "no clock here")


class TestChunkBoundaries(unittest.TestCase):
    def test_record_split_across_chunks(self):
        line = '2026-11-16T10:00:00Z level=info msg="quoted \\"value\\" ok" trace_id=t7\n'
        chunks = [line[:17], line[17:40], line[40:]]
        records = run_chunks(chunks)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].msg, 'quoted "value" ok')
        self.assertEqual(records[0].trace_id, "t7")
        self.assertFalse(records[0].truncated)

    def test_byte_chunks(self):
        data = "2026-11-16T10:00:00Z level=info msg=café\n".encode("utf-8")
        chunks = [data[:25], data[25:]]
        records = run_chunks(chunks)
        self.assertEqual(records[0].msg, "café")

    def test_truncated_tail_flushed_on_close(self):
        norm = StreamNormalizer()
        self.assertEqual(norm.feed("2026-11-16T10:00:00Z level=info msg=part1\n"), [])
        self.assertEqual(norm.feed("2026-11-16T10:00:01Z level=warn msg=cut-off"), [])
        records = norm.close()
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0].truncated)
        self.assertEqual(records[1].msg, "cut-off")
        self.assertTrue(records[1].truncated)


class TestMultiline(unittest.TestCase):
    STACK = (
        "2026-11-16T10:00:00Z level=error trace_id=t1 msg=\"boom\" svc=api\n"
        "Traceback (most recent call last):\n"
        "  File \"app.py\", line 10, in <module>\n"
        "    main()\n"
        "ValueError: invalid literal\n"
        "2026-11-16T10:00:01Z level=info msg=recovered\n"
    )

    def test_stack_merged_into_record(self):
        records = run_chunks([self.STACK])
        self.assertEqual(len(records), 2)
        rec = records[0]
        self.assertEqual(rec.level, "ERROR")
        self.assertEqual(len(rec.stack), 4)
        self.assertIn("ValueError", rec.stack[-1])
        self.assertEqual(records[1].msg, "recovered")
        self.assertEqual(records[1].stack, [])

    def test_stack_split_across_chunks(self):
        chunks = [self.STACK[:50], self.STACK[50:90], self.STACK[90:]]
        records = run_chunks(chunks)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(records[0].stack), 4)

    def test_very_long_stack(self):
        stack_lines = ["    frame %d" % i for i in range(5000)]
        text = "2026-11-16T10:00:00Z level=error msg=boom\n" + "\n".join(stack_lines) + "\n"
        records = run_chunks([text])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0].stack), 5000)
        self.assertFalse(records[0].stack_truncated)

    def test_max_stack_lines_cap(self):
        text = "level=error msg=boom\n" + "\n".join("  f%d" % i for i in range(100)) + "\n"
        records = run_chunks([text], max_stack_lines=10)
        self.assertEqual(len(records[0].stack), 10)
        self.assertTrue(records[0].stack_truncated)

    def test_orphan_continuation_goes_pending(self):
        records = run_chunks([
            "  indented junk with no owner\n",
            "another stray line\n",
            "2026-11-16T10:00:00Z level=info msg=real\n",
        ])
        self.assertEqual(len(records), 2)
        pending, real = records
        self.assertEqual(pending.origin, "pending")
        self.assertEqual(pending.level, "UNKNOWN")
        self.assertEqual(pending.msg, "  indented junk with no owner")
        self.assertEqual(pending.stack, ["another stray line"])
        self.assertEqual(real.origin, "record")
        self.assertEqual(real.msg, "real")

    def test_pending_released_on_close(self):
        records = run_chunks(["stray one\nstray two\n"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].origin, "pending")
        self.assertEqual(records[0].msg, "stray one")
        self.assertEqual(records[0].stack, ["stray two"])


class TestSerialization(unittest.TestCase):
    def test_round_trip(self):
        line = '2026-11-16T10:00:00Z level=warn trace_id=t1 msg="a \\"b\\" c" k=v\n'
        (rec,) = run_chunks([line])
        out = rec.to_logfmt()
        (rec2,) = run_chunks([out + "\n"])
        self.assertEqual(rec2.timestamp, rec.timestamp)
        self.assertEqual(rec2.level, rec.level)
        self.assertEqual(rec2.trace_id, rec.trace_id)
        self.assertEqual(rec2.msg, rec.msg)
        self.assertEqual(rec2.fields, rec.fields)

    def test_to_dict_shape(self):
        (rec,) = run_chunks(["level=info msg=x\n"])
        d = rec.to_dict()
        self.assertEqual(
            set(d),
            {"timestamp", "level", "trace_id", "msg", "fields",
             "stack", "origin", "truncated", "stack_truncated"},
        )


if __name__ == "__main__":
    unittest.main()
