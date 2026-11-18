from datetime import datetime
import json
import os

import duckdb
import pytest

pytest.importorskip('openai')
pytest.importorskip('sqlglot')
pytest.importorskip('mcp')

from metroflow.agent import agent, mcp_server, query_log
from metroflow.agent.query import run_sql
from tests.test_agent_guardrails import FakeClient, FakeMessage


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'warehouse.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute('CREATE TABLE dim_zone AS SELECT range AS zone_key FROM range(100)')
    return path


def entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_append(query_log_path):
    query_log.append_query_log(question='first\nquestion', sql='SELECT 1', model='local', row_count=1)
    query_log.append_query_log(sql='SELECT 2', error='example failure')
    rows = entries(query_log_path)
    assert len(rows) == 2 and rows[0]['question'] == 'first\nquestion'
    assert datetime.fromisoformat(rows[0]['timestamp']).utcoffset().total_seconds() == 0
    assert rows[0]['model'] == 'local' and rows[0]['row_count'] == 1
    assert rows[1]['question'] is None and rows[1]['error'] == 'example failure'
    assert query_log_path.stat().st_mode & 0o777 == 0o600


def test_retry_log(database, query_log_path):
    client = FakeClient([FakeMessage('SELECT missing FROM dim_zone'),
                         FakeMessage('SELECT count(*) FROM dim_zone'), FakeMessage(content='100 zones')])
    result = agent.ask('How many zones?', database, client)
    rows = entries(query_log_path)
    assert len(rows) == 1
    assert rows[0]['question'] == 'How many zones?' and rows[0]['sql'] == result['sql']
    assert rows[0]['model'] == os.getenv('OPENAI_MODEL', 'qwen3.5:4b')
    assert rows[0]['attempts'] == 2 and rows[0]['row_count'] == 1 and rows[0]['error'] is None


def test_failed_log(database, query_log_path):
    client = FakeClient([FakeMessage(content='guess') for _ in range(3)])
    result = agent.ask('How many zones?', database, client)
    row = entries(query_log_path)[0]
    assert row['attempts'] == 3 and row['sql'] is None and row['row_count'] is None
    assert row['error'] == result['error']


def test_client_error(database, monkeypatch, query_log_path):
    def broken(**kwargs):
        raise RuntimeError('client initialization failed')
    monkeypatch.setattr(agent, 'OpenAI', broken)
    with pytest.raises(RuntimeError, match='client initialization'):
        agent.ask('How many zones?', database)
    row = entries(query_log_path)[0]
    assert row['attempts'] == 0 and row['error'] == 'client initialization failed'


@pytest.mark.parametrize('query, count', [('SELECT * FROM dim_zone', 50), ('SELECT * FROM bronze.trips', None)])
def test_mcp_log(database, monkeypatch, query_log_path, query, count):
    monkeypatch.setattr(mcp_server, 'execute_sql', lambda sql: run_sql(sql, database))
    result = mcp_server.run_sql(query)
    row = entries(query_log_path)[0]
    assert row['question'] is None and row['model'] is None and row['attempts'] == 1
    assert row['sql'] == result.get('sql', query) and row['row_count'] == count
    assert row['error'] == result.get('error')


def test_log_error(database, monkeypatch, tmp_path):
    monkeypatch.setenv('METROFLOW_QUERY_LOG', str(tmp_path))
    client = FakeClient([FakeMessage('SELECT count(*) FROM dim_zone'), FakeMessage(content='100 zones')])
    assert agent.ask('How many zones?', database, client)['error'] is None
    monkeypatch.setattr(mcp_server, 'execute_sql', lambda sql: run_sql(sql, database))
    assert mcp_server.run_sql('SELECT count(*) FROM dim_zone')['rows'] == [[100]]


def test_json_error(query_log_path):
    query_log.append_query_log(question=object())
    assert not query_log_path.exists()
