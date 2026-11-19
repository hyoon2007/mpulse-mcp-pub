"""Exercise JSON-only metadata changes through discovery, validation and results."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mpulse_mcp import catalog, query_types
from mpulse_mcp.errors import ConfigError


def _isolated_package(tmp_path: Path) -> Path:
    target = tmp_path / "mpulse_mcp"
    shutil.copytree(Path(query_types.__file__).parent, target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return target


def _run(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          text=True, capture_output=True, timeout=20)


def test_json_only_edit_reaches_discovery_validation_and_aggregation(tmp_path) -> None:
    pkg = _isolated_package(tmp_path)
    path = pkg / "catalog.json"
    data = json.loads(path.read_text())
    endpoint = data["endpoint_params"]["timers-metrics"]
    endpoint.update(summary="Edited in JSON only", aggregation="reviewed period aggregate")
    endpoint["value_params"]["review-field"] = "single"
    path.write_text(json.dumps(data))
    result = _run(tmp_path, '''
import asyncio, json, os
import httpx
from mpulse_mcp import server
from mpulse_mcp.client import MpulseClient
from mpulse_mcp.config import AppConfig, Registry
from mpulse_mcp.errors import ValidationError
os.environ['METADATA_TEST_TOKEN']='fixture-only'
async def main():
    calls=[]
    def respond(request):
        calls.append(request)
        if request.method=='PUT':
            return httpx.Response(201,json={'token':'fixture'})
        return httpx.Response(200,json={'values':[{'id':'PageLoad','latest':42,'history':[42]}]})
    reg=Registry(default_app='demo',apps={'demo':AppConfig(name='demo',api_key='KEY',tenant='tenant',api_token_env='METADATA_TEST_TOKEN')})
    client=MpulseClient(reg,http=httpx.AsyncClient(base_url='https://mpulse.soasta.com',transport=httpx.MockTransport(respond)))
    server._client=client
    server._registry=reg
    try:
        for verbose in [False,True]:
            meta=server.describe_query('timers-metrics',verbose=verbose)
            assert meta['summary']=='Edited in JSON only'
            assert meta['aggregation']=='reviewed period aggregate'
            assert 'review-field' in meta['parameters']
            assert 'review-field' in meta['value_parameters']
        try:
            await server.query('timers-metrics',params={'review-field':['not a scalar']})
        except ValidationError:
            pass
        else:
            raise AssertionError('JSON cardinality was ignored')
        assert not calls
        out=await server.get_metrics(timer='PageLoad',date='2026-11-08',history_mode='none')
        assert out['aggregation']=='reviewed period aggregate'
        assert out['body']['series'][0]['latest']==42
        assert len(calls)==2
        print(json.dumps({'ok':True}))
    finally:
        await client.aclose()
asyncio.run(main())
''')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("mode", ["missing", "invalid_json", "missing_metadata"])
def test_bad_packaged_catalog_fails_with_clear_configuration_error(tmp_path, mode) -> None:
    pkg = _isolated_package(tmp_path)
    path = pkg / "catalog.json"
    if mode == "missing":
        path.unlink()
    elif mode == "invalid_json":
        path.write_text("not json")
    else:
        data = json.loads(path.read_text())
        del data["endpoint_params"]["summary"]["aggregation"]
        path.write_text(json.dumps(data))
    result = _run(tmp_path, "from mpulse_mcp import server")
    assert result.returncode != 0
    assert "ConfigError" in result.stderr
    assert "catalog.json" in result.stderr
    assert "Restore the packaged catalog.json" in result.stderr


@pytest.mark.parametrize("field,value", [("supports_time", "yes"), ("filters", None),
                                        ("value_params", []), ("response_shape", 4)])
def test_invalid_metadata_cannot_silently_weaken_checks(field, value) -> None:
    data = copy.deepcopy(catalog.load())
    data["endpoint_params"]["summary"][field] = value
    with pytest.raises(ConfigError, match="catalog.json"):
        query_types._load_metadata(data)
