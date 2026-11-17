"""Gold table meanings and join keys, drafted from dbt/models/schema.yml."""
from pathlib import Path

import yaml

TABLES = {'main.' + name for name in ['fct_trips', 'dim_zone', 'dim_date', 'mart_zone_daily',
                                    'mart_borough_daily', 'mart_weather_demand', 'mart_quality']}


def load_context():
    path = Path(__file__).with_name('context.yml')
    context = yaml.safe_load(path.read_text())
    if set(context['tables']) != TABLES:
        raise ValueError('semantic context must describe exactly the allowed Gold tables')
    return context


def schema_prompt():
    return yaml.safe_dump(load_context(), sort_keys=False)
