import requests, os, json
from datetime import datetime, timezone, timedelta

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'

# 1. Legacy engagements API - uses basic contacts scope, no sales-email-read needed
print("=== Legacy Engagements API ===")
r = requests.get(f'{BASE}/engagements/v1/engagements/paged', headers=HEADERS,
                 params={'limit': 20, 'offset': 0})
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    results = data.get('results', [])
    all_types = set(e.get('engagement', {}).get('type') for e in results)
    print(f"Total in page: {len(results)}  Types seen: {all_types}")
    emails = [e for e in results if e.get('engagement', {}).get('type') in ('EMAIL', 'INCOMING_EMAIL')]
    print(f"Email engagements: {len(emails)}")
    for e in emails[:3]:
        eng  = e.get('engagement', {})
        meta = e.get('metadata', {})
        assoc = e.get('associations', {})
        print(f"\n  id={eng.get('id')} type={eng.get('type')} ts={eng.get('timestamp')}")
        print(f"  subject={meta.get('subject')}")
        print(f"  from={meta.get('from')}")
        print(f"  to={meta.get('to')}")
        print(f"  contactIds={assoc.get('contactIds')}  companyIds={assoc.get('companyIds')}")
        body = (meta.get('text') or meta.get('html') or '')[:300]
        print(f"  body={body!r}")
else:
    print(r.text[:500])

# 2. CRM v3 emails (needs sales-email-read)
print("\n=== CRM v3 /emails ===")
r2 = requests.get(f'{BASE}/crm/v3/objects/emails', headers=HEADERS,
                  params={'limit': 3, 'properties': 'hs_email_subject,hs_timestamp'})
print(f"Status: {r2.status_code}  body: {r2.text[:300]}")

# 3. Activities via timeline
print("\n=== CRM v3 /objects/0-14 (email activity) ===")
r3 = requests.get(f'{BASE}/crm/v3/objects/0-14', headers=HEADERS, params={'limit': 3})
print(f"Status: {r3.status_code}  body: {r3.text[:300]}")

# 4. Check token scopes
print("\n=== Token scopes ===")
r4 = requests.get(f'{BASE}/oauth/v1/access-tokens/{TOKEN}')
print(f"Status: {r4.status_code}")
if r4.status_code == 200:
    info = r4.json()
    print(f"Scopes: {info.get('scopes')}")
else:
    r5 = requests.get(f'https://api.hubapi.com/crm/v3/objects/contacts?limit=1', headers=HEADERS)
    print(f"contacts check: {r5.status_code}")
