import asyncio
import subprocess
import sys

import duckdb
import pytest

pytest.importorskip('mcp')
pytest.importorskip('sqlglot')

from metroflow.agent import mcp_server
from metroflow.agent.context import load_context
from metroflow.agent.query import run_sql


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'warehouse.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute('CREATE TABLE dim_zone AS SELECT range AS zone_key FROM range(100)')
    return path


@pytest.mark.parametrize('query', ['SELECT * FROM dim_zone ORDER BY zone_key', 'SELECT * FROM bronze.trips'])
def test_query(database, monkeypatch, query):
    monkeypatch.setattr(mcp_server, 'execute_sql', lambda sql: run_sql(sql, database))
    assert mcp_server.run_sql(query) == run_sql(query, database)


def test_context():
    assert mcp_server.describe_tables() == load_context()


def test_tools():
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {tool.name for tool in tools} == {'run_sql', 'describe_tables'}
    query_tool = next(tool for tool in tools if tool.name == 'run_sql')
    assert query_tool.input_schema['required'] == ['query']


def test_no_model_client():
    code = '''
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'openai' or name.startswith('openai.') or name == 'metroflow.agent.agent':
        raise AssertionError('MCP must not import the model client')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import metroflow.agent.mcp_server
assert 'openai' not in sys.modules
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
