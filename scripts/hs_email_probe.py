import requests, os, json

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'

# Step 1: fetch a few companies to get their IDs
print("=== Fetching companies ===")
r = requests.get(f'{BASE}/crm/v3/objects/companies', headers=HEADERS,
                 params={'limit': 5, 'properties': 'name'})
companies = r.json().get('results', [])
for c in companies:
    print(f"  id={c['id']} name={c['properties'].get('name')}")

# Step 2: for each company, fetch associated engagements
print("\n=== Company-specific engagements ===")
all_email_types = set()
for company in companies[:3]:
    cid  = company['id']
    name = company['properties'].get('name')
    r2 = requests.get(
        f'{BASE}/engagements/v1/engagements/associated/company/{cid}/paged',
        headers=HEADERS, params={'limit': 50}
    )
    if r2.status_code != 200:
        print(f"  {name}: status={r2.status_code}")
        continue
    eng_list = r2.json().get('results', [])
    types = set(e.get('engagement', {}).get('type') for e in eng_list)
    all_email_types |= types
    print(f"\n  Company: {name}  total_engagements={len(eng_list)}  types={types}")
    emails = [e for e in eng_list if e.get('engagement', {}).get('type') in ('EMAIL', 'INCOMING_EMAIL', 'FORWARDED_EMAIL')]
    print(f"  Email engagements: {len(emails)}")
    for e in emails[:2]:
        eng  = e.get('engagement', {})
        meta = e.get('metadata', {})
        assoc = e.get('associations', {})
        print(f"    id={eng.get('id')} type={eng.get('type')} ts={eng.get('timestamp')}")
        print(f"    subject={meta.get('subject')}")
        print(f"    from={meta.get('from')}  to={meta.get('to')}")
        body = (meta.get('text') or meta.get('html') or '')[:300]
        print(f"    body={body!r}")
        print(f"    contactIds={assoc.get('contactIds')} companyIds={assoc.get('companyIds')}")

print(f"\nAll engagement types seen across companies: {all_email_types}")

# Step 3: paginate global engagements further to find EMAIL type
print("\n=== Global engagements paginated (looking for EMAIL type) ===")
offset, found_email = 0, False
for page in range(5):
    r3 = requests.get(f'{BASE}/engagements/v1/engagements/paged', headers=HEADERS,
                      params={'limit': 100, 'offset': offset})
    if r3.status_code != 200:
        break
    data = r3.json()
    results = data.get('results', [])
    types = set(e.get('engagement', {}).get('type') for e in results)
    print(f"  page {page}: offset={offset} count={len(results)} types={types}")
    email_results = [e for e in results if e.get('engagement', {}).get('type') in ('EMAIL', 'INCOMING_EMAIL')]
    if email_results:
        found_email = True
        e = email_results[0]
        eng = e.get('engagement', {})
        meta = e.get('metadata', {})
        print(f"  FOUND EMAIL: id={eng.get('id')} ts={eng.get('timestamp')} subject={meta.get('subject')}")
        print(f"  from={meta.get('from')} to={meta.get('to')}")
        print(f"  body={( meta.get('text') or '' )[:200]!r}")
        break
    if not data.get('hasMore'):
        break
    offset = data.get('offset', offset + 100)
if not found_email:
    print("  No EMAIL engagement type found in global results")
