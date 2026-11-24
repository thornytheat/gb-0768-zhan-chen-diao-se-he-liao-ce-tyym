"""外发联：签发展方联，隐去采购批号与称量原件。

外发料号由留档键与规范批号经键控摘要派生：同一份留档内同一批料号码一致，
不同工单的留档之间无法靠料号串联。签发是留档的纯函数，重试不改号码，
旧留档无需补录任何字段。重量与对账结果由键控签章保护，
改动后即使重算明文摘要也无法通过核验。
"""
import hmac
import re
from hashlib import sha256

from app.faults import LedgerError
from app.ledger.identity import encoded
from app.ledger.signoff import ensure_sound
from app.settings import EDITION, OUTWARD_SECRET

FORMAT = re.compile(r'X-[0-9A-F]{10}')
CODES = {'MATCH', 'MISMATCH', 'NO_CLAIM', 'CLAIM_CONFLICT'}


def material_no(key, lot):
    digest = hmac.new(OUTWARD_SECRET, f'outward-lot|{key}|{lot}'.encode(), sha256).hexdigest()
    return 'X-' + digest[:10].upper()


def seal(body):
    return hmac.new(OUTWARD_SECRET, b'outward-seal|' + encoded(body), sha256).hexdigest()


def totals(tickets, materials):
    return {'tickets': len(tickets), 'materials': len(materials),
            'returned_mg': sum(line['returned_mg'] for line in materials),
            'claimed_mg': sum(line['claimed_mg'] for line in materials if line['claimed_mg'] is not None),
            'difference_mg': sum(line['difference_mg'] for line in materials if line['difference_mg'] is not None)}


def issue(record):
    ensure_sound(record)
    manifest = record['manifest']
    key = manifest['key']
    tickets = []
    for row in record['snapshot']['recovery']['rows']:
        if row['data']['action'] != 'RETURN':
            continue
        shares = [{'material': material_no(key, lot), 'mass_mg': mass} for lot, mass in row['parts_mg'].items()]
        shares.sort(key=lambda share: share['material'])
        tickets.append({'ticket': row['data']['ticket'], 'occurred': row['at'],
                        'net_mg': row['net_mg'], 'shares': shares})
    materials = []
    for line in record['snapshot']['balance']['lines']:
        if not line['returned_mg'] and line['claimed_mg'] is None:
            continue
        claims = sorted(({'receipt': ev['data']['receipt'], 'mass_mg': ev['declared_mg']}
                         for ev in line['receipts']), key=lambda claim: (claim['receipt'], claim['mass_mg']))
        materials.append({'material': material_no(key, line['lot']), 'returned_mg': line['returned_mg'],
                          'claimed_mg': line['claimed_mg'], 'difference_mg': line['difference_mg'],
                          'claims': claims, 'code': line['code']})
    materials.sort(key=lambda line: line['material'])
    body = {'document': '回桶收据外发联', 'edition': EDITION, 'key': key, 'order': manifest['order'],
            'cutoff': manifest['cutoff'], 'issued_at': manifest['issued_at'],
            'tickets': tickets, 'materials': materials, 'totals': totals(tickets, materials)}
    return body | {'seal': seal(body)}


def verify(document):
    if not isinstance(document, dict) or not isinstance(document.get('seal'), str):
        raise LedgerError('OUTWARD_INVALID', '外发联缺签章或不是有效单据')
    body = {name: value for name, value in document.items() if name != 'seal'}
    if not hmac.compare_digest(seal(body), document['seal']):
        raise LedgerError('SEAL_DISAGREEMENT', '外发联签章不符，内容已被改动', 409)
    recount(body)
    return body


def recount(body):
    try:
        tickets, materials, sums = body['tickets'], body['materials'], body['totals']
        head = body['document'] == '回桶收据外发联' and all(
            isinstance(body[name], str) for name in ('edition', 'key', 'order', 'cutoff', 'issued_at'))
        if not head or not isinstance(tickets, list) or not isinstance(materials, list) or not isinstance(sums, dict):
            raise TypeError
    except (KeyError, TypeError):
        raise LedgerError('OUTWARD_INVALID', '外发联结构无效') from None
    shares = {}
    for ticket in tickets:
        try:
            parts = ticket['shares']
            valid = (isinstance(ticket['ticket'], str) and isinstance(ticket['occurred'], str)
                     and _mass(ticket['net_mg']) and isinstance(parts, list)
                     and all(_mass(part['mass_mg']) and isinstance(part['material'], str)
                             and FORMAT.fullmatch(part['material']) for part in parts))
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise LedgerError('OUTWARD_INVALID', '外发联结构无效')
        if sum(part['mass_mg'] for part in parts) != ticket['net_mg']:
            raise LedgerError('OUTWARD_UNBALANCED', '回桶票分摊与净重不符', 409)
        for part in parts:
            shares[part['material']] = shares.get(part['material'], 0) + part['mass_mg']
    seen = set()
    for line in materials:
        try:
            claims = line['claims']
            valid = (isinstance(line['material'], str) and FORMAT.fullmatch(line['material'])
                     and _mass(line['returned_mg']) and line['code'] in CODES and isinstance(claims, list)
                     and all(isinstance(claim['receipt'], str) and _mass(claim['mass_mg']) for claim in claims)
                     and (line['claimed_mg'] is None or _mass(line['claimed_mg']))
                     and (line['difference_mg'] is None or _integer(line['difference_mg'])))
        except (KeyError, TypeError):
            valid = False
        if not valid or line['material'] in seen:
            raise LedgerError('OUTWARD_INVALID', '外发联结构无效')
        seen.add(line['material'])
        declared = {claim['mass_mg'] for claim in claims}
        claimed = next(iter(declared)) if len(declared) == 1 else None
        difference = None if claimed is None else line['returned_mg'] - claimed
        code = ('CLAIM_CONFLICT' if len(declared) > 1 else 'NO_CLAIM' if claimed is None
                else 'MATCH' if difference == 0 else 'MISMATCH')
        expect = {'returned_mg': shares.get(line['material'], 0),
                  'claimed_mg': claimed, 'difference_mg': difference, 'code': code}
        got = {name: line[name] for name in expect}
        if got != expect:
            raise LedgerError('OUTWARD_UNBALANCED', '外发联复算与记载不符', 409)
    if set(shares) != {line['material'] for line in materials if line['returned_mg']}:
        raise LedgerError('OUTWARD_UNBALANCED', '外发联复算与记载不符', 409)
    if sums != totals(tickets, materials):
        raise LedgerError('OUTWARD_UNBALANCED', '外发联合计与明细不符', 409)
    return body


def reveal(record, material):
    if isinstance(material, str) and FORMAT.fullmatch(material):
        key = record['manifest']['key']
        for line in record['snapshot']['balance']['lines']:
            if material_no(key, line['lot']) == material:
                return line['lot']
    raise LedgerError('MATERIAL_UNKNOWN', '外发料号对不上留档批号', 404)


def _mass(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)
