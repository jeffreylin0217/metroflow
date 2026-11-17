from pathlib import Path

import pytest
import yaml

pytest.importorskip('openai')
pytest.importorskip('sqlglot')

from sqlglot.errors import SqlglotError

from metroflow.agent.context import TABLES, load_context, schema_prompt
from metroflow.agent.guardrails import validate_sql


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
