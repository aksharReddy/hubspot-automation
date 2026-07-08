import requests, os, json

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'

# 1. Fetch recent emails
print("=== Recent emails ===")
r = requests.get(f'{BASE}/crm/v3/objects/emails', headers=HEADERS, params={
    'limit': 5,
    'properties': 'hs_email_subject,hs_email_direction,hs_timestamp,hs_email_from_email,hs_email_to_email,hs_email_status,hs_email_body',
    'sort': '-hs_timestamp'
})
print(f"Status: {r.status_code}")
data = r.json()
emails = data.get('results', [])
print(f"Count: {len(emails)}")
for e in emails:
    p = e['properties']
    print(f"\n  id={e['id']}")
    print(f"  subject={p.get('hs_email_subject')}")
    print(f"  direction={p.get('hs_email_direction')}")
    print(f"  timestamp={p.get('hs_timestamp')}")
    print(f"  from={p.get('hs_email_from_email')}")
    print(f"  to={p.get('hs_email_to_email')}")
    print(f"  status={p.get('hs_email_status')}")
    body = (p.get('hs_email_body') or '')[:200]
    print(f"  body_preview={body!r}")

# 2. Try associations on first email
if emails:
    eid = emails[0]['id']
    print(f"\n=== Associations for email {eid} ===")
    for obj in ['contacts', 'companies']:
        r2 = requests.get(f'{BASE}/crm/v3/objects/emails/{eid}/associations/{obj}', headers=HEADERS)
        print(f"  {obj}: status={r2.status_code} data={r2.json()}")

# 3. Try search with direction filter
print("\n=== Search OUTBOUND emails last 7 days ===")
from datetime import datetime, timezone, timedelta
cutoff = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
r3 = requests.post(f'{BASE}/crm/v3/objects/emails/search', headers={**HEADERS, 'Content-Type': 'application/json'},
    json={
        'filterGroups': [{'filters': [
            {'propertyName': 'hs_email_direction', 'operator': 'EQ', 'value': 'EMAIL'},
            {'propertyName': 'hs_timestamp', 'operator': 'GTE', 'value': str(cutoff)}
        ]}],
        'properties': ['hs_email_subject', 'hs_email_direction', 'hs_timestamp', 'hs_email_from_email', 'hs_email_to_email'],
        'limit': 5,
        'sorts': [{'propertyName': 'hs_timestamp', 'direction': 'DESCENDING'}]
    })
print(f"Status: {r3.status_code}")
print(json.dumps(r3.json(), indent=2)[:1000])
