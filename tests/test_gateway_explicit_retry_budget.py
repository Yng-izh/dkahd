from dwg_reader.recognition import model_gateway as M
from dwg_reader.recognition import ai_config as A


def test_explicit_zero_retries_survives_gateway_factory(monkeypatch):
    monkeypatch.setattr(A, 'enabled', lambda cfg: True)
    monkeypatch.setattr(A, 'credentials', lambda cfg: {
        'api_key': 'test', 'base_url': 'http://invalid', 'model': 'test',
        'vision_model': 'test', 'allow_remote_images': True})
    gateway = M.gateway_from_config({'provider': {'retries': 0}})
    assert gateway.retries == 0
    assert M.gateway_from_config({'provider': {}}).retries == 1
