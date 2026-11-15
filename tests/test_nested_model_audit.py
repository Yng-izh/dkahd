import base64
import json

from dwg_reader.recognition.v2 import _persist_model_audit


def test_nested_closeup_preserves_reply_and_image_without_payload_in_result(tmp_path):
    payload = base64.b64encode(b'png evidence').decode('ascii')
    rows = [{'family': 'fam:test', 'request': {'images': [{'b64': payload}]},
             'closeup': {'request': {'images': [{'b64': payload}]},
                         'raw_text': 'uncertain', 'raw': {'need_closeup': True}}}]
    public = _persist_model_audit(rows, audit_dir=tmp_path)
    assert 'b64' not in json.dumps(public)
    assert rows[0]['closeup']['request']['images'][0]['b64'] == payload
    index = json.loads((tmp_path / 'index.json').read_text())
    assert index['images'] == 2
    assert len(list(tmp_path.glob('*.png'))) == 2
    saved = json.loads((tmp_path / '01-fam-test.json').read_text())
    assert saved['closeup']['raw_text'] == 'uncertain'
    assert saved['closeup']['raw']['need_closeup'] is True
