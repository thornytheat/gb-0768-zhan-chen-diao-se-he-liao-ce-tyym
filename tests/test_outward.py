import json
from copy import deepcopy
from hashlib import sha256

import pytest

from app.faults import LedgerError
from app.ledger.identity import encoded
from app.ledger.outward import material_no, seal, verify
from app.receipts.packet import read_sample


@pytest.fixture
def key(client):
    return client.post('/api/books', json={'sample': 'dirty'}).json['key']


@pytest.fixture
def outward(client, key):
    return client.get(f'/api/books/{key}/outward')


def _packet(order):
    files = dict(read_sample('dirty'))
    job = json.loads(files['job'])
    job['order'] = order
    files['job'] = json.dumps(job, ensure_ascii=False)
    return {'sheets': files}


def _body(**overrides):
    body = {'document': '回桶收据外发联', 'edition': 'PALETTE-1.0', 'key': 'k', 'order': 'o',
            'cutoff': 'c', 'issued_at': 'i',
            'tickets': [{'ticket': '1', 'occurred': 't', 'net_mg': 5,
                         'shares': [{'material': 'X-AAAAAAAAAA', 'mass_mg': 5}]}],
            'materials': [{'material': 'X-AAAAAAAAAA', 'returned_mg': 5, 'claimed_mg': 5,
                           'difference_mg': 0, 'claims': [{'receipt': 'R1', 'mass_mg': 5}], 'code': 'MATCH'}],
            'totals': {'tickets': 1, 'materials': 1, 'returned_mg': 5, 'claimed_mg': 5, 'difference_mg': 0}}
    body.update(overrides)
    return body


def test_issue_downloads_sealed_copy(outward, key):
    assert outward.headers['Content-Disposition'] == f'attachment; filename="outward-{key}.json"'
    doc = outward.json
    assert doc['document'] == '回桶收据外发联' and doc['key'] == key
    assert len(doc['seal']) == 64 and all(c in '0123456789abcdef' for c in doc['seal'])


def test_copy_hides_purchase_trace(outward):
    text = outward.get_data(as_text=True)
    for leaked in ('R-017', 'W-041', 'B-003', 'C-001', 'C-002', '胭脂', '石白', '复名',
                   '铁红', '钛白', '群青', '赭石', '炭黑', 'sheets', 'gross', 'tare', 'container',
                   'opening_mg', 'issued_mg', 'applied_mg', 'loss_mg', 'disputed_mg',
                   'lot_sources', 'pigment', '"lot"', '"pot"'):
        assert leaked not in text, leaked


def test_same_batch_shared_number_within_copy(outward):
    doc = outward.json
    appearances = {}
    for ticket in doc['tickets']:
        for share in ticket['shares']:
            appearances.setdefault(share['material'], set()).add(ticket['ticket'])
    shared = {m: ts for m, ts in appearances.items() if len(ts) > 1}
    assert len(shared) == 1 and len(next(iter(shared.values()))) == 11
    assert set(appearances) == {m['material'] for m in doc['materials']}


def test_offline_recompute(outward):
    doc = outward.json
    sums = {}
    for ticket in doc['tickets']:
        assert sum(s['mass_mg'] for s in ticket['shares']) == ticket['net_mg']
        for s in ticket['shares']:
            sums[s['material']] = sums.get(s['material'], 0) + s['mass_mg']
    for m in doc['materials']:
        assert m['returned_mg'] == sums[m['material']]
        if m['claimed_mg'] is not None:
            assert m['difference_mg'] == m['returned_mg'] - m['claimed_mg']
            assert all(c['mass_mg'] == m['claimed_mg'] for c in m['claims'])
    assert doc['totals'] == {
        'tickets': len(doc['tickets']), 'materials': len(doc['materials']),
        'returned_mg': sum(m['returned_mg'] for m in doc['materials']),
        'claimed_mg': sum(m['claimed_mg'] for m in doc['materials'] if m['claimed_mg'] is not None),
        'difference_mg': sum(m['difference_mg'] for m in doc['materials'] if m['difference_mg'] is not None)}


def test_other_order_cannot_link_materials(client):
    first = client.post('/api/books', json=_packet('RC-118')).json['key']
    second = client.post('/api/books', json=_packet('RC-999')).json['key']
    assert first != second
    doc_a = client.get(f'/api/books/{first}/outward').json
    doc_b = client.get(f'/api/books/{second}/outward').json
    numbers_a = {m['material'] for m in doc_a['materials']}
    numbers_b = {m['material'] for m in doc_b['materials']}
    assert numbers_a and numbers_b and not numbers_a & numbers_b
    lots_a = {client.get(f'/api/books/{first}/outward/{m}').json['lot']: m for m in numbers_a}
    lots_b = {client.get(f'/api/books/{second}/outward/{m}').json['lot']: m for m in numbers_b}
    assert set(lots_a) == set(lots_b)
    assert all(lots_a[lot] != lots_b[lot] for lot in lots_a)


def test_retry_keeps_material_numbers(client, key, outward):
    assert client.get(f'/api/books/{key}/outward').get_data() == outward.get_data()
    assert client.post('/api/books', json={'sample': 'dirty'}).json['reused'] is True
    assert client.get(f'/api/books/{key}/outward').get_data() == outward.get_data()


def test_legacy_archive_issues_without_stored_numbers(client, key, tmp_path):
    raw = (tmp_path / f'{key}.receipt').read_text()
    assert 'seal' not in raw and 'material' not in raw
    response = client.get(f'/api/books/{key}/outward')
    assert response.status_code == 200 and response.json['materials']


def test_verify_accepts_issued_copy(client, outward):
    doc = outward.json
    response = client.post('/api/outward/verify', json=doc)
    assert response.status_code == 200
    assert response.json['valid'] is True
    assert response.json['key'] == doc['key'] and response.json['order'] == doc['order']
    assert response.json['totals'] == doc['totals']


def test_tampered_weight_fails_verification(client, outward):
    doc = deepcopy(outward.json)
    doc['tickets'][0]['shares'][0]['mass_mg'] += 1
    response = client.post('/api/outward/verify', json=doc)
    assert (response.status_code, response.json['code']) == (409, 'SEAL_DISAGREEMENT')


def test_recomputed_plain_digest_still_fails(client, outward):
    doc = deepcopy(outward.json)
    doc['materials'][0]['returned_mg'] += 500
    body = {k: v for k, v in doc.items() if k != 'seal'}
    doc['seal'] = sha256(encoded(body)).hexdigest()
    response = client.post('/api/outward/verify', json=doc)
    assert (response.status_code, response.json['code']) == (409, 'SEAL_DISAGREEMENT')


def test_verify_rejects_shapeless_document(client):
    assert client.post('/api/outward/verify', json={}).json['code'] == 'OUTWARD_INVALID'
    assert client.post('/api/outward/verify', json={'seal': 1}).json['code'] == 'OUTWARD_INVALID'
    response = client.post('/api/outward/verify', data='not json', content_type='application/json')
    assert response.json['code'] == 'OUTWARD_INVALID'


def test_verify_recomputes_signed_copy():
    assert verify(_body() | {'seal': seal(_body())})['order'] == 'o'


def test_verify_catches_unbalanced_split():
    doc = _body(tickets=[{'ticket': '1', 'occurred': 't', 'net_mg': 6,
                          'shares': [{'material': 'X-AAAAAAAAAA', 'mass_mg': 5}]}])
    doc['seal'] = seal(doc)
    with pytest.raises(LedgerError) as err:
        verify(doc)
    assert err.value.code == 'OUTWARD_UNBALANCED'


def test_verify_catches_unbalanced_material():
    doc = _body(materials=[{'material': 'X-AAAAAAAAAA', 'returned_mg': 4, 'claimed_mg': 4,
                            'difference_mg': 0, 'claims': [{'receipt': 'R1', 'mass_mg': 4}], 'code': 'MATCH'}],
                totals={'tickets': 1, 'materials': 1, 'returned_mg': 4, 'claimed_mg': 4, 'difference_mg': 0})
    doc['seal'] = seal(doc)
    with pytest.raises(LedgerError) as err:
        verify(doc)
    assert err.value.code == 'OUTWARD_UNBALANCED'


def test_audit_traces_material_to_original(client, key, outward):
    doc = outward.json
    by_claim = {m['claimed_mg']: m['material'] for m in doc['materials']}
    response = client.get(f"/api/books/{key}/outward/{by_claim[39010]}")
    assert response.json['lot'] == 'R-017'
    for m in doc['materials']:
        found = client.get(f"/api/books/{key}/outward/{m['material']}")
        assert found.status_code == 200 and found.json['lot'] in {'R-017', 'W-041', 'B-003'}


def test_audit_rejects_foreign_material(client, key, outward):
    alien = next(m for m in ('X-FFFFFFFFFF', 'X-EEEEEEEEEE')
                 if m not in {line['material'] for line in outward.json['materials']})
    response = client.get(f'/api/books/{key}/outward/{alien}')
    assert (response.status_code, response.json['code']) == (404, 'MATERIAL_UNKNOWN')
    assert client.get(f'/api/books/{key}/outward/not-a-number').json['code'] == 'MATERIAL_UNKNOWN'


def test_outward_requires_archive(client):
    assert client.get(f"/api/books/{'0' * 64}/outward").json['code'] == 'BOOK_UNKNOWN'


def test_receipt_page_links_outward(client, key):
    page = client.get('/studio/receipt?key=' + key).get_data(as_text=True)
    assert f'href="/api/books/{key}/outward"' in page
    doc = client.get(f'/api/books/{key}/outward').json
    section = page.split('外发联预览')[1].split('签发成分清单')[0]
    for m in doc['materials']:
        assert m['material'] in section
    assert 'R-017' not in section


def test_material_number_derivation():
    assert material_no('k', 'R-017') == material_no('k', 'R-017')
    assert material_no('k', 'R-017') != material_no('k', 'W-041')
    assert material_no('k', 'R-017') != material_no('other', 'R-017')
