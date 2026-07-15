import requests, os, json
from datetime import datetime, timezone, timedelta

TOKEN = os.environ['HUBSPOT_TOKEN']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
BASE = 'https://api.hubapi.com'
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)

def fetch_meetings(label, start_dt, end_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    r = requests.post(f'{BASE}/crm/v3/objects/meetings/search',
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json={
            'filterGroups': [{'filters': [{
                'propertyName': 'hs_meeting_start_time',
                'operator': 'BETWEEN',
                'value': str(start_ms),
                'highValue': str(end_ms)
            }]}],
            'properties': ['hs_meeting_title', 'hs_meeting_start_time',
                           'hs_meeting_end_time', 'hs_meeting_body',
                           'hs_meeting_outcome', 'hs_internal_meeting_notes',
                           'hubspot_owner_id'],
            'sorts': [{'propertyName': 'hs_meeting_start_time', 'direction': 'ASCENDING'}],
            'limit': 50
        })
    meetings = r.json().get('results', []) if r.status_code == 200 else []
    print(f"\n=== {label} ({start_dt.strftime('%d %b %Y')}) — {len(meetings)} meeting(s) ===")
    if not meetings:
        print("  None found")
        return meetings
    for m in meetings:
        p   = m['properties']
        ts  = p.get('hs_meeting_start_time')
        te  = p.get('hs_meeting_end_time')
        def fmt(t):
            if not t: return '-'
            try:
                return datetime.fromisoformat(str(t).replace('Z','+00:00')).astimezone(IST).strftime('%I:%M %p IST')
            except: return str(t)
        print(f"  id={m['id']}")
        print(f"  title={p.get('hs_meeting_title')!r}")
        print(f"  time={fmt(ts)} → {fmt(te)}")
        print(f"  outcome={p.get('hs_meeting_outcome')}")
        print(f"  body={( p.get('hs_meeting_body') or '' )[:200]!r}")
        print(f"  notes={( p.get('hs_internal_meeting_notes') or '' )[:200]!r}")
        # fetch associations
        ar = requests.get(f'{BASE}/crm/v3/objects/meetings/{m["id"]}/associations/contacts', headers=HEADERS)
        contacts = [a['id'] for a in ar.json().get('results', [])] if ar.status_code == 200 else []
        cr = requests.get(f'{BASE}/crm/v3/objects/meetings/{m["id"]}/associations/companies', headers=HEADERS)
        companies = [a['id'] for a in cr.json().get('results', [])] if cr.status_code == 200 else []
        print(f"  contact_ids={contacts}  company_ids={companies}")
        if companies:
            comp_r = requests.get(f'{BASE}/crm/v3/objects/companies/{companies[0]}',
                                  headers=HEADERS, params={'properties': 'name'})
            if comp_r.status_code == 200:
                print(f"  company_name={comp_r.json()['properties'].get('name')}")
        print()
    return meetings

today_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
today_end   = NOW.replace(hour=23, minute=59, second=59, microsecond=999999)
tmrw_start  = today_start + timedelta(days=1)
tmrw_end    = today_end   + timedelta(days=1)

fetch_meetings("Today", today_start, today_end)
fetch_meetings("Tomorrow", tmrw_start, tmrw_end)

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
