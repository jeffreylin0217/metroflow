import pytest


@pytest.fixture(autouse=True)
def query_log_path(tmp_path, monkeypatch):
    path = tmp_path/'queries.jsonl'
    monkeypatch.setenv('METROFLOW_QUERY_LOG', str(path))
    return path
