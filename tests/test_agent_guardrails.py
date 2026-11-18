from pathlib import Path
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
import json
import subprocess
import sys

import duckdb
import pytest
import yaml

pytest.importorskip('openai')
pytest.importorskip('sqlglot')

from sqlglot.errors import SqlglotError

from metroflow.agent.context import TABLES, load_context, schema_prompt
from metroflow.agent.guardrails import validate_sql
from metroflow.agent.agent import ask
from metroflow.agent.query import json_safe, run_sql


@pytest.mark.parametrize('query', [
    'SELECT * FROM dim_zone',
    'SELECT count(*) FROM main.fct_trips',
    'WITH x AS (SELECT * FROM main.dim_zone) SELECT * FROM x',
    'SELECT * FROM (SELECT * FROM main.dim_zone) z',
    'SELECT * FROM (WITH private AS (SELECT * FROM main.dim_zone) SELECT * FROM private) x',
    'SELECT (SELECT max(trip_count) FROM main.mart_quality)',
    'WITH x AS (SELECT 1 AS a), y AS (SELECT * FROM x) SELECT * FROM y',
    'SELECT z.zone, sum(m.trip_count) FROM main.mart_zone_daily m '
    'JOIN main.dim_zone z ON m.zone_key=z.zone_key GROUP BY z.zone',
    "SELECT count(*) FILTER (WHERE quality_status='INVALID') FROM mart_quality",
    "SELECT * FROM dim_zone WHERE zone_key > 0 AND (borough='Manhattan' OR NOT borough IS NULL)",
    "SELECT * FROM mart_weather_demand WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-01-31'",
])
def test_allowed(query):
    assert validate_sql(query).endswith('LIMIT 200')


@pytest.mark.parametrize('query', [
    '', 'SELECT 1; SELECT 2', 'SELECT 1;;', 'DELETE FROM main.fct_trips',
    'CREATE TABLE x AS SELECT 1', 'UPDATE main.dim_zone SET borough=\'x\'',
    'PRAGMA version', "ATTACH 'other.duckdb'", "COPY main.dim_zone TO '/tmp/z.csv'",
    'SELECT * INTO x FROM main.dim_zone', 'SELECT * FROM bronze.trips',
    'SELECT * FROM stg_taxi_trips', 'SELECT * FROM information_schema.tables',
    'SELECT * FROM other.main.dim_zone', 'SELECT * FROM (SELECT * FROM bronze.trips) x',
    'WITH x AS (SELECT * FROM stg_zones) SELECT * FROM x',
    'WITH x AS (SELECT * FROM bronze.trips) SELECT * FROM main.dim_zone',
    'WITH x AS (SELECT 1) SELECT * FROM (SELECT * FROM x) a, x_missing',
    'WITH x AS (SELECT 1) SELECT * FROM x UNION SELECT * FROM bronze.trips',
    'SELECT (SELECT count(*) FROM stg_weather)',
    "SELECT * FROM read_parquet('/tmp/x.parquet')",
    "SELECT * FROM read_csv('/tmp/x.csv')",
    "SELECT * FROM '/tmp/x.parquet'", "SELECT read_blob('/tmp/x')",
    "SELECT * FROM query('SELECT * FROM bronze.trips')",
    "SELECT getenv('HOME')", 'SELECT current_setting(\'search_path\')',
    'SELECT * FROM range(100)', 'SELECT main.my_macro(1)',
    'SELECT * FROM dim_zone LIMIT (SELECT 1)',
])
def test_rejected(query):
    with pytest.raises((ValueError, SqlglotError)):
        validate_sql(query)


def test_limits():
    assert validate_sql('SELECT * FROM MAIN.DIM_ZONE LIMIT 3').endswith('LIMIT 3')
    assert validate_sql('SELECT * FROM dim_zone LIMIT 10000').endswith('LIMIT 200')
    assert 'main.dim_zone' in validate_sql('SELECT * FROM dim_zone')
    assert validate_sql("SELECT 'DELETE FROM bronze.trips' AS label").endswith('LIMIT 200')


def test_local_cte():
    query = 'SELECT * FROM (WITH private AS (SELECT 1) SELECT * FROM private) x, private'
    with pytest.raises(ValueError, match='table is not allowed'):
        validate_sql(query)
    with pytest.raises(ValueError, match='table is not allowed'):
        validate_sql('WITH dim_zone AS (SELECT 1) SELECT * FROM bronze.dim_zone')


@pytest.mark.parametrize('hidden_table', ['bronze.trips', 'stg_zones'])
def test_cte_tables(hidden_table):
    with pytest.raises(ValueError, match='table is not allowed'):
        validate_sql(f'WITH dim_zone AS (SELECT * FROM {hidden_table}) SELECT * FROM dim_zone')


def test_cte_staging():
    with pytest.raises(ValueError, match='table is not allowed'):
        validate_sql('WITH stg_zones AS (SELECT * FROM stg_zones) SELECT * FROM stg_zones')


def test_context():
    context = load_context()
    models = yaml.safe_load(Path('dbt/models/schema.yml').read_text())['models']
    assert set(context['tables']) == TABLES
    assert {name.split('.')[1] for name in TABLES} <= {model['name'] for model in models}
    for table in context['tables'].values():
        assert table['description'] and table['grain'] and table['columns']
        assert isinstance(table['joins'], list)
    assert 'zone_key only' in context['tables']['main.mart_zone_daily']['description']
    assert 'INVALID rows are excluded' in schema_prompt()
    assert 'precipitation_mm > 0' in schema_prompt()
    assert '24 million' in schema_prompt()


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'warehouse.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute('CREATE TABLE dim_zone AS SELECT range AS zone_key FROM range(100)')
    return path


def test_json_values():
    values = [Decimal('12345678901234567890.12'), 2**100, date(2025, 1, 1),
              datetime(2025, 1, 1, 12, 30), None, float('inf'), float('nan')]
    result = json_safe({'rows': [tuple(values)]})
    assert result['rows'][0] == ['12345678901234567890.12', 2**100, '2025-01-01',
                                 '2025-01-01T12:30:00', None, None, None]
    json.dumps(result, allow_nan=False)


def test_query_results(database):
    result = run_sql('SELECT * FROM main.dim_zone ORDER BY zone_key', database)
    assert len(result['rows']) == 50 and result['truncated']
    assert result['columns'] == ['zone_key'] and result['rows'][0] == [0]
    result = run_sql("SELECT 12.34::DECIMAL(18,2), 123::HUGEINT, DATE '2025-01-01', "
                     "TIMESTAMP '2025-01-01 12:30:00'", database)
    assert result['rows'] == [['12.34', 123, '2025-01-01', '2025-01-01T12:30:00']]
    assert 'does_not_exist' in run_sql('SELECT does_not_exist FROM dim_zone', database)['error']
    assert 'table is not allowed' in run_sql('SELECT * FROM bronze.trips', database)['error']
    assert 'error' in run_sql('SELECT FROM', database)


def test_timeout(database):
    result = run_sql('SELECT sum(a.zone_key*b.zone_key*c.zone_key*d.zone_key) '
                     'FROM dim_zone a, dim_zone b, dim_zone c, dim_zone d', database, timeout=0.01)
    assert 'exceeded 0.01 seconds' in result['error']
    assert run_sql('SELECT count(*) FROM dim_zone', database)['rows'] == [[100]]


def test_locked(database):
    code = ('import duckdb, sys; c=duckdb.connect(sys.argv[1]); '
            'print("ready", flush=True); sys.stdin.readline()')
    child = subprocess.Popen([sys.executable, '-c', code, str(database)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        assert 'Warehouse is locked' in run_sql('SELECT * FROM dim_zone', database)['error']
    finally:
        child.communicate('\n', timeout=10)


class FakeMessage:
    def __init__(self, query=None, content=None):
        self.content = content
        self.tool_calls = [] if query is None else [SimpleNamespace(
            id='call_1', function=SimpleNamespace(name='run_sql', arguments=json.dumps({'query': query})))]

    def model_dump(self, **kwargs):
        result = {'role': 'assistant', 'content': self.content or ''}
        if self.tool_calls:
            call = self.tool_calls[0]
            result['tool_calls'] = [{'id': call.id, 'type': 'function',
                'function': {'name': call.function.name, 'arguments': call.function.arguments}}]
        return result


class FakeClient:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.responses))])


def test_retry(database):
    client = FakeClient([FakeMessage('SELECT missing FROM dim_zone'),
                         FakeMessage('SELECT count(*) FROM dim_zone'), FakeMessage(content='100 zones')])
    result = ask('How many zones?', database, client)
    assert result['attempts'] == 2 and result['error'] is None
    assert result['answer'] == '100 zones' and 'LIMIT 200' in result['sql']
    feedback = next(message for message in client.requests[1]['messages'] if message['role'] == 'tool')
    assert feedback['role'] == 'tool' and 'missing' in feedback['content']
    assert 'missing' in client.requests[1]['messages'][-1]['content']
    assert client.requests[-1]['tool_choice'] == 'none'


def test_missing_tool(database):
    client = FakeClient([FakeMessage(content='100 zones') for _ in range(3)])
    result = ask('How many zones?', database, client)
    assert result['attempts'] == 3 and result['sql'] is None
    assert result['error'] and result['answer'].startswith('Unable to answer')
    assert len(client.requests) == 3


def test_retry_limit(database):
    client = FakeClient([FakeMessage('SELECT missing FROM dim_zone') for _ in range(3)])
    result = ask('How many zones?', database, client)
    assert result['attempts'] == 3 and len(result['history']) == 3
    assert 'missing' in result['error']


def test_tool_retry(database):
    client = FakeClient([FakeMessage(content='guess'), FakeMessage('SELECT count(*) FROM dim_zone'),
                         FakeMessage(content='100 zones')])
    result = ask('How many zones?', database, client)
    assert result['attempts'] == 2 and result['answer'] == '100 zones'


def test_eval_results():
    from scripts.eval_agent import CASES, compare
    assert len(CASES) == 15
    assert compare([(1, 'a'), (2, 'b')], [('b', 2.005, 99), ('a', 1, 98)], 2, 3)[0]
    assert compare([(1,), (1,)], [(1.009,), (1.001,)], 1, 1)[0]
    assert not compare([(1,), (1,)], [(1,), (2,)], 1, 1)[0]
    assert not compare([(1, 'a'), (2, 'b')], [(1, 'b'), (2, 'a')], 2, 2)[0]
    assert not compare([(1,)], [(1.011,)], 1, 1)[0]
    assert not compare([(None,)], [(0,)], 1, 1)[0]
    assert not compare([(1,)], [(1,), (1,)], 1, 1)[0]
    assert compare([], [], 1, 2)[0]
    assert compare([(1,)], [(True, 1)], 1, 2)[0]
    assert not compare([(1,)], [(float('nan'),)], 1, 1)[0]




def test_summary_retry(database):
    client = FakeClient([FakeMessage('SELECT count(*) FROM dim_zone'), FakeMessage(content=''),
                         FakeMessage('SELECT count(*) FROM dim_zone'), FakeMessage(content='100 zones')])
    result = ask('How many zones?', database, client)
    assert result['attempts'] == len(result['history']) == 2
    assert 'final answer' in result['history'][0]['error']
