import requests, os, json
from datetime import datetime, timezone

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'

# Step 1: search for Diagnofirm Medical Laboratories
print("=== Searching for Diagnofirm ===")
r = requests.post(f'{BASE}/crm/v3/objects/companies/search',
    headers={**HEADERS, 'Content-Type': 'application/json'},
    json={
        'filterGroups': [{'filters': [
            {'propertyName': 'name', 'operator': 'CONTAINS_TOKEN', 'value': 'diagnofirm'}
        ]}],
        'properties': ['name'],
        'limit': 5
    })
print(f"Status: {r.status_code}")
companies = r.json().get('results', [])
for c in companies:
    print(f"  id={c['id']} name={c['properties'].get('name')}")

if not companies:
    print("Not found by name, trying partial...")
    r2 = requests.post(f'{BASE}/crm/v3/objects/companies/search',
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json={
            'filterGroups': [{'filters': [
                {'propertyName': 'name', 'operator': 'CONTAINS_TOKEN', 'value': 'diagno'}
            ]}],
            'properties': ['name'],
            'limit': 5
        })
    companies = r2.json().get('results', [])
    for c in companies:
        print(f"  id={c['id']} name={c['properties'].get('name')}")

# Step 2: fetch all engagements for this company
if companies:
    cid  = companies[0]['id']
    name = companies[0]['properties'].get('name')
    print(f"\n=== Engagements for {name} (id={cid}) ===")

    eng_r = requests.get(
        f'{BASE}/engagements/v1/engagements/associated/company/{cid}/paged',
        headers=HEADERS, params={'limit': 100}
    )
    print(f"Status: {eng_r.status_code}")
    all_eng = eng_r.json().get('results', [])
    types = set(e.get('engagement', {}).get('type') for e in all_eng)
    print(f"Total engagements: {len(all_eng)}  Types: {types}")

    emails = [e for e in all_eng
              if e.get('engagement', {}).get('type') in ('EMAIL', 'INCOMING_EMAIL', 'FORWARDED_EMAIL')]
    print(f"Email engagements: {len(emails)}")

    for e in emails:
        eng  = e.get('engagement', {})
        meta = e.get('metadata', {})
        assoc = e.get('associations', {})
        ts_ms = eng.get('timestamp')
        ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M') if ts_ms else 'unknown'
        print(f"\n  --- Email id={eng.get('id')} ---")
        print(f"  type={eng.get('type')}  date={ts_str}")
        print(f"  subject={meta.get('subject')!r}")
        print(f"  from={meta.get('from')}")
        print(f"  to={meta.get('to')}")
        print(f"  cc={meta.get('cc')}")
        body = meta.get('text') or meta.get('html') or ''
        print(f"  body_len={len(body)}  body_preview={body[:500]!r}")
        print(f"  contactIds={assoc.get('contactIds')}  companyIds={assoc.get('companyIds')}")
        # print all metadata keys
        print(f"  meta keys={list(meta.keys())}")
