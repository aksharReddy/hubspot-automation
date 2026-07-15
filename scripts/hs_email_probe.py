import requests, os, json
from datetime import datetime, timezone, timedelta

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)

today_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
today_end   = NOW.replace(hour=23, minute=59, second=59, microsecond=999999)

# Fetch today's meetings with all possible properties
print("=== Today's meetings — full property dump ===")
r = requests.post(f'{BASE}/crm/v3/objects/meetings/search',
    headers={**HEADERS, 'Content-Type': 'application/json'},
    json={
        'filterGroups': [{'filters': [{
            'propertyName': 'hs_meeting_start_time',
            'operator': 'BETWEEN',
            'value': str(int(today_start.timestamp() * 1000)),
            'highValue': str(int(today_end.timestamp() * 1000))
        }]}],
        'properties': [
            'hs_meeting_title', 'hs_meeting_start_time', 'hs_meeting_end_time',
            'hs_meeting_body', 'hs_meeting_outcome', 'hs_internal_meeting_notes',
            'hs_meeting_location', 'hs_meeting_source', 'hs_meeting_source_id',
            'hubspot_owner_id', 'hs_activity_type', 'hs_meeting_type',
        ],
        'limit': 10
    })
print(f"Status: {r.status_code}")
meetings = r.json().get('results', []) if r.status_code == 200 else []
print(f"Count: {len(meetings)}")

for m in meetings:
    p = m['properties']
    print(f"\n--- Meeting id={m['id']} ---")
    print(f"All properties: {json.dumps(p, indent=2)}")

    # Fetch associated contacts with ALL useful properties
    ar = requests.get(f'{BASE}/crm/v3/objects/meetings/{m["id"]}/associations/contacts', headers=HEADERS)
    contact_ids = [a['id'] for a in ar.json().get('results', [])] if ar.status_code == 200 else []
    print(f"Contact IDs: {contact_ids}")

    if contact_ids:
        cr = requests.post(f'{BASE}/crm/v3/objects/contacts/batch/read',
            headers={**HEADERS, 'Content-Type': 'application/json'},
            json={
                'inputs': [{'id': cid} for cid in contact_ids],
                'properties': [
                    'firstname', 'lastname', 'email', 'phone', 'mobilephone',
                    'lifecyclestage', 'hs_lead_status',
                    'hs_analytics_source', 'hs_analytics_source_data_1',
                    'hs_latest_source', 'hs_latest_source_data_1',
                    'lead_type', 'lead_source',
                ]
            })
        if cr.status_code == 200:
            for c in cr.json().get('results', []):
                cp = c['properties']
                print(f"  Contact: {cp.get('firstname')} {cp.get('lastname')} <{cp.get('email')}>")
                print(f"  lifecyclestage={cp.get('lifecyclestage')}")
                print(f"  hs_lead_status={cp.get('hs_lead_status')}")
                print(f"  hs_analytics_source={cp.get('hs_analytics_source')}")
                print(f"  hs_analytics_source_data_1={cp.get('hs_analytics_source_data_1')}")
                print(f"  hs_latest_source={cp.get('hs_latest_source')}")
                print(f"  hs_latest_source_data_1={cp.get('hs_latest_source_data_1')}")
                print(f"  lead_type={cp.get('lead_type')}")
                print(f"  lead_source={cp.get('lead_source')}")
