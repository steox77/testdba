#!/usr/bin/env python3
"""Test offline di pg_report_analyzer.py: client Claude finto, nessuna chiamata API."""

import json
import unittest
from types import SimpleNamespace as NS

import pg_report_analyzer as a

ANALYSIS = {
    "stato_generale": "attenzione",
    "sintesi": "Cache hit basso e shared_buffers al default.",
    "raccomandazioni": [{
        "priorita": "alta", "area": "configurazione", "titolo": "Aumentare shared_buffers",
        "problema": "Cache hit al 93%.", "evidenza": "shared_buffers = 128MB, cache hit 93.57%.",
        "azione": "Portarlo al 25% della RAM.", "comandi": ["ALTER SYSTEM SET shared_buffers = '4GB';"],
        "rischio": "Serve un riavvio.", "richiede_riavvio": True,
    }],
    "punti_positivi": ["Nessun lock."],
    "dati_mancanti": ["RAM del server."],
}


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    """Restituisce in sequenza le risposte preparate e registra le richieste."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **params):
        self.calls.append(params)
        return FakeStream(self.responses.pop(0))


class FakeRunner:
    def __init__(self):
        self.queries = []

    def run(self, sql):
        self.queries.append(sql)
        return '{"righe": [{"setting": "128MB"}]}', False


def text_msg(data, stop="end_turn"):
    return NS(stop_reason=stop, stop_details=None, content=[NS(type="text", text=json.dumps(data))])


class AnalyzerTest(unittest.TestCase):
    def test_check_sql(self):
        self.assertIsNone(a.check_sql("SELECT 1;"))
        self.assertIsNone(a.check_sql("explain select * from t"))
        self.assertIsNotNone(a.check_sql("DELETE FROM t"))
        self.assertIsNotNone(a.check_sql("select 1; drop table t"))
        self.assertIsNotNone(a.check_sql("EXPLAIN (ANALYZE, BUFFERS) select 1"))

    def test_report_only(self):
        client = FakeClient([text_msg(ANALYSIS)])
        out = a.analyze({"sections": []}, client=client, log=lambda *_: None)
        self.assertEqual(out, ANALYSIS)
        self.assertNotIn("tools", client.calls[0])
        self.assertEqual(client.calls[0]["fallbacks"], "default")

    def test_tool_loop(self):
        tool_call = NS(stop_reason="tool_use", stop_details=None, content=[
            NS(type="tool_use", id="tu_1", name="run_sql",
               input={"sql": "SHOW shared_buffers", "motivo": "verifica"})])
        client = FakeClient([tool_call, text_msg(ANALYSIS)])
        runner = FakeRunner()
        out = a.analyze({"sections": []}, client=client, sql_runner=runner, log=lambda *_: None)
        self.assertEqual(out, ANALYSIS)
        self.assertEqual(runner.queries, ["SHOW shared_buffers"])
        # la lista messages è condivisa tra le chiamate: il penultimo turno sono i tool_result
        result = client.calls[1]["messages"][-2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "tu_1"))

    def test_refusal(self):
        client = FakeClient([NS(stop_reason="refusal", stop_details="cyber", content=[])])
        with self.assertRaises(RuntimeError):
            a.analyze({"sections": []}, client=client, log=lambda *_: None)

    def test_render(self):
        md = a.render_markdown(ANALYSIS, {"server_version_num": 180006})
        self.assertIn("### 1. Aumentare shared_buffers", md)
        self.assertIn("ALTER SYSTEM SET shared_buffers", md)
        self.assertIn("richiede riavvio", a.render_html(ANALYSIS, {}))


if __name__ == "__main__":
    unittest.main()
