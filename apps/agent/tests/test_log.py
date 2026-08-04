import json
from logging.handlers import RotatingFileHandler

from moko import log


def test_log_exchange_noop_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    # Not configured (the default) → nothing is written.
    log.log_exchange({"kind": "reply", "reply": "hi"})
    assert not (tmp_path / "logs" / "llm.jsonl").exists()


def test_log_exchange_writes_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    # Flip the gate directly (monkeypatch auto-reverts) so we exercise the
    # JSONL writer without configuring the global logging handlers.
    monkeypatch.setattr(log, "_enabled", True)
    monkeypatch.setattr(log, "_jsonl_handler", None)  # fresh handler for this tmp home
    log.log_exchange({"kind": "reply", "reply": "hi", "model": "fake"})

    path = tmp_path / "logs" / "llm.jsonl"
    assert path.exists()
    record = json.loads(path.read_text().strip())
    assert record["kind"] == "reply"
    assert record["reply"] == "hi"
    assert "at" in record  # timestamp stamped


def test_jsonl_handler_is_bounded_rotating(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_jsonl_handler", None)
    handler = log._get_jsonl_handler()
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes > 0  # the file is capped, not unbounded
    assert handler.backupCount > 0  # and old data is rolled off, not kept forever


def test_llm_jsonl_rotates_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    monkeypatch.setattr(log, "_enabled", True)
    monkeypatch.setattr(log, "_jsonl_handler", None)
    # Tiny cap so a handful of records trip rotation without writing megabytes.
    handler = log._get_jsonl_handler()
    handler.maxBytes = 500

    logs = tmp_path / "logs"
    for i in range(50):
        log.log_exchange({"kind": "reply", "reply": "x" * 100, "n": i})

    live = logs / "llm.jsonl"
    assert live.exists()
    assert live.stat().st_size <= 500 + 500  # bounded near the cap, never unbounded growth
    assert (logs / "llm.jsonl.1").exists()  # rotation happened → a backup exists
    # Every surviving line is still exactly one valid JSON record.
    for ln in live.read_text().splitlines():
        if ln.strip():
            json.loads(ln)
