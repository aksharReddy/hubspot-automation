import os
import requests

HUBSPOT_TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}'}
BASE = 'https://api.hubapi.com'


def fetch_all_companies():
    results, after = [], None
    while True:
        params = {
            'limit': 100,
            'properties': 'name,lifecyclestage,hs_num_open_deals,domain',
        }
        if after:
            params['after'] = after
        r = requests.get(f'{BASE}/crm/v3/objects/companies', headers=HEADERS, params=params)
        data = r.json()
        results.extend(data.get('results', []))
        after = data.get('paging', {}).get('next', {}).get('after')
        if not after:
            break
    return results


companies = fetch_all_companies()

by_stage = {}
for c in companies:
    stage = c['properties'].get('lifecyclestage') or 'unknown'
    by_stage.setdefault(stage, []).append(c['properties'].get('name', '-'))

print(f'\nTotal companies in HubSpot: {len(companies)}\n')
print('=== Breakdown by lifecycle stage ===')
for stage, names in sorted(by_stage.items(), key=lambda x: -len(x[1])):
    print(f'\n{stage.upper()} ({len(names)} companies):')
    for name in sorted(names):
        print(f'  - {name}')

customers = by_stage.get('customer', [])
open_deal_cos = [
    c['properties'].get('name', '-') for c in companies
    if int(c['properties'].get('hs_num_open_deals') or 0) > 0
]

print(f'\n=== Summary ===')
print(f'Customers (lifecycle=customer): {len(customers)}')
print(f'Companies with open deals:      {len(open_deal_cos)}')
combined = set(customers) | set(open_deal_cos)
print(f'Combined unique (news targets): {len(combined)}')
