import re
import json
import requests
import smtplib
import os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from fpdf import FPDF, XPos, YPos
from google.oauth2 import service_account
from googleapiclient.discovery import build as gcal_build


def pdf_safe(text):
    t = str(text)
    char_map = {
        '–': '-', '—': '-',
        '‘': "'", '’': "'",
        '“': '"', '”': '"',
    }
    for src, dst in char_map.items():
        t = t.replace(src, dst)
    return t.encode('latin-1', errors='replace').decode('latin-1')


HUBSPOT_TOKEN      = os.environ['HUBSPOT_TOKEN']
GMAIL_ADDRESS      = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']
RECIPIENT_EMAIL    = os.environ['RECIPIENT_EMAIL']
APOLLO_API_KEY     = os.environ['APOLLO_API_KEY']
GOOGLE_SA_JSON     = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
CLICKUP_TOKEN      = os.environ['CLICKUP_TOKEN']
CLICKUP_LIST_ID    = '901615411023'
GROQ_API_KEY       = os.environ['GROQ_API_KEY']

HUBSPOT_HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}'}
APOLLO_BASE    = 'https://api.apollo.io/api/v1'
APOLLO_HEADERS = {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-cache',
    'X-Api-Key': APOLLO_API_KEY,
}
APOLLO_REPLY_CLASSES = [
    'willing_to_meet', 'follow_up_question', 'person_referral',
    'out_of_office', 'not_interested', 'unsubscribe',
]
BASE = 'https://api.hubapi.com'
NOW  = datetime.now(timezone.utc)
IST  = timezone(timedelta(hours=5, minutes=30))

_today_ist  = NOW.astimezone(IST)
_monday_ist = _today_ist - timedelta(days=_today_ist.weekday())
WEEK_START  = _monday_ist.replace(hour=0, minute=0, second=0, microsecond=0)
WEEK_END    = WEEK_START + timedelta(days=6)   # Sunday 00:00 IST — Saturday is last day shown
TODAY_LABEL = _today_ist.strftime('%A, %d %b')

EXCLUDED_TITLES = {'niro scrum call', 'weekend update and sprint planning'}


def days_since(date_str):
    if not date_str:
        return 9999
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return (NOW - dt).days
    except Exception:
        return 9999


def deal_silence(d):
    p = d['properties']
    return min(
        days_since(p.get('notes_last_updated')),
        days_since(p.get('notes_last_contacted')),
        days_since(p.get('hs_lastmodifieddate'))
    )


def _is_niroggyan(name):
    return 'niroggyan' in (name or '').lower()


def _is_excluded(title):
    return (title or '').lower().strip() in EXCLUDED_TITLES


def fetch_all(obj, props):
    results, after = [], None
    while True:
        params = {'limit': 100, 'properties': ','.join(props)}
        if after:
            params['after'] = after
        r = requests.get(f'{BASE}/crm/v3/objects/{obj}',
                         headers=HUBSPOT_HEADERS, params=params)
        data = r.json()
        results.extend(data.get('results', []))
        after = data.get('paging', {}).get('next', {}).get('after')
        if not after:
            break
    return results


def fetch_associations_batch(from_obj, to_obj, ids):
    result = {}
    for i in range(0, len(ids), 100):
        batch = ids[i:i+100]
        r = requests.post(
            f'{BASE}/crm/v3/associations/{from_obj}/{to_obj}/batch/read',
            headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
            json={'inputs': [{'id': id_} for id_ in batch]}
        )
        if r.status_code != 200:
            continue
        for item in r.json().get('results', []):
            from_id = str(item['from']['id'])
            result[from_id] = [str(a['id']) for a in item.get('to', [])]
    return result


def fetch_meetings(start_dt, end_dt):
    r = requests.post(
        f'{BASE}/crm/v3/objects/meetings/search',
        headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
        json={
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'hs_meeting_start_time',
                    'operator': 'BETWEEN',
                    'value': str(int(start_dt.timestamp() * 1000)),
                    'highValue': str(int(end_dt.timestamp() * 1000))
                }]
            }],
            'properties': ['hs_meeting_title', 'hs_meeting_start_time',
                           'hs_meeting_end_time', 'hs_meeting_body', 'hs_meeting_source'],
            'sorts': [{'propertyName': 'hs_meeting_start_time', 'direction': 'ASCENDING'}],
            'limit': 100
        }
    )
    return r.json().get('results', []) if r.status_code == 200 else []


def parse_agenda(body):
    if not body:
        return ''
    m = re.search(r'Additional notes:\s*\n([^\n]+)', body, re.IGNORECASE)
    if m:
        text = m.group(1).strip()
        if text and len(text) > 3:
            return text[:200]
    return ''


def format_lifecycle(stage):
    if not stage:
        return ''
    mapping = {'marketingqualifiedlead': 'MQL', 'salesqualifiedlead': 'SQL'}
    return mapping.get(stage.lower(), stage.capitalize())


def format_meeting_source(source_val):
    s = str(source_val or '').upper()
    if s == 'BIDIRECTIONAL_SYNC':
        return 'Inbound'
    if s == 'CRM_UI':
        return 'Manual'
    return ''


def parse_meeting_time(ts_val):
    if not ts_val:
        return '-'
    try:
        dt  = datetime.fromisoformat(str(ts_val).replace('Z', '+00:00'))
        ist = dt.astimezone(IST)
        return ist.strftime('%I:%M %p')
    except Exception:
        return '-'


def call_groq(prompt, max_tokens=400):
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
            },
            timeout=30
        )
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f'AI unavailable: {e}'


def build_ai_briefing(data, clickup_tickets, calendar_days):
    lines = [
        f"You are writing a daily morning briefing for the leadership team at NirogGyan, a B2B healthcare SaaS in India.",
        f"Today is {data['date']}. Here is a structured data dump from their internal systems.\n",
    ]

    # ClickUp
    if clickup_tickets:
        overdue = [t for t in clickup_tickets if t['overdue']]
        urgent_due_soon = [t for t in clickup_tickets if not t['overdue'] and t['days_left'] is not None and t['days_left'] <= 5 and t['priority'] == 'URGENT']
        lines.append(f"SUPPORT TICKETS ({len(clickup_tickets)} open):")
        if overdue:
            for t in overdue:
                lines.append(f"  - OVERDUE {abs(t['days_left'])}d: {t['name']} (assigned: {t['assignees']})")
        if urgent_due_soon:
            for t in urgent_due_soon:
                lines.append(f"  - URGENT due in {t['days_left']}d: {t['name']} (assigned: {t['assignees']})")
        no_due = [t for t in clickup_tickets if not t['due_str']]
        if no_due:
            lines.append(f"  - {len(no_due)} tickets have no due date set")
        lines.append('')

    # Calendar
    today_cal = next(((d, evts) for d, evts in calendar_days if d == TODAY_LABEL), None)
    if today_cal:
        _, evts = today_cal
        lines.append(f"TODAY'S CALENDAR ({len(evts)} meetings):")
        for ev in evts:
            attendee_str = ', '.join(ev['attendees']) if ev['attendees'] else 'internal'
            lines.append(f"  - {ev['time']}: {ev['title']} (with {attendee_str})")
        lines.append('')

    # HubSpot meetings today
    today_hs = next(((d, m) for d, m in data['hs_meetings_by_day'] if d == TODAY_LABEL), None)
    if today_hs:
        _, meetings = today_hs
        lines.append(f"HUBSPOT MEETINGS TODAY ({len(meetings)}):")
        for m, company, phone, *_ in meetings:
            title = m['properties'].get('hs_meeting_title', 'Meeting')
            lines.append(f"  - {title} with {company} (phone: {phone})")
        lines.append('')

    # Deals
    if data['active_deals']:
        lines.append(f"PIPELINE: {data['stats']['open_deals_count']} open deals total. Most recently active:")
        for d, silence, company in data['active_deals'][:4]:
            lines.append(f"  - {d['properties'].get('dealname','-')} ({company}, active {silence}d ago)")
        lines.append('')

    # Active companies
    lines.append(f"ACTIVE COMPANIES THIS MONTH: {data['stats']['active_count']} companies engaged recently.")

    lines += [
        "",
        "INSTRUCTIONS:",
        "Write 4-6 short bullet points (use •) for a morning briefing. Each bullet should be 1-2 sentences max.",
        "Focus on: what needs immediate action, risks, today's key meetings, patterns you see across the data.",
        "Do NOT just list what's in the data — interpret it. Flag what's worrying, what's good, what needs follow-up.",
        "Be direct and specific. Use names, numbers, dates. No filler words. No intro sentence.",
        "Plain text only, no markdown, no bold, no headers.",
    ]

    return call_groq('\n'.join(lines), max_tokens=450)


def fetch_calendar_week():
    try:
        creds  = service_account.Credentials.from_service_account_info(
            GOOGLE_SA_JSON, scopes=['https://www.googleapis.com/auth/calendar.readonly'])
        svc    = gcal_build('calendar', 'v3', credentials=creds, cache_discovery=False)
        result = svc.events().list(
            calendarId='Shweta@niroggyan.com',
            timeMin=WEEK_START.isoformat(),
            timeMax=WEEK_END.isoformat(),
            singleEvents=True,
            orderBy='startTime',
        ).execute()

        days      = {}
        day_order = []
        for e in result.get('items', []):
            title = e.get('summary', '')
            if _is_excluded(title):
                continue

            start_raw = e['start'].get('dateTime', e['start'].get('date'))
            if 'T' in start_raw:
                dt      = datetime.fromisoformat(start_raw).astimezone(IST)
                end_raw = e['end'].get('dateTime', e['end'].get('date'))
                end_dt  = datetime.fromisoformat(end_raw).astimezone(IST)
                time_str = f'{dt.strftime("%I:%M %p")} - {end_dt.strftime("%I:%M %p")} IST'
            else:
                dt       = datetime.fromisoformat(start_raw).replace(tzinfo=IST)
                time_str = 'All day'

            day_key = dt.strftime('%A, %d %b')
            if day_key not in days:
                days[day_key] = []
                day_order.append(day_key)

            attendees = [
                a['email'] for a in e.get('attendees', [])
                if not a.get('self') and 'niroggyan' not in a['email'].lower()
            ]
            days[day_key].append({
                'title':     title or '(No title)',
                'time':      time_str,
                'attendees': attendees,
                'meet':      e.get('hangoutLink', ''),
            })

        return [(day, days[day]) for day in day_order]
    except Exception as ex:
        print(f'Calendar fetch failed: {ex}')
        return []


def _week_label():
    saturday = WEEK_START + timedelta(days=5)
    return f'{WEEK_START.strftime("%d %b")} - {saturday.strftime("%d %b %Y")}'


def fetch_clickup_tickets():
    try:
        r = requests.get(
            f'https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task',
            headers={'Authorization': CLICKUP_TOKEN},
            timeout=15
        )
        if r.status_code != 200:
            return []
        tasks = r.json().get('tasks', [])
        result = []
        for t in tasks:
            due_ms    = t.get('due_date')
            due_str   = ''
            overdue   = False
            days_left = None
            if due_ms:
                due_dt    = datetime.fromtimestamp(int(due_ms) / 1000, tz=timezone.utc)
                days_left = (due_dt - NOW).days
                overdue   = days_left < 0
                due_str   = due_dt.strftime('%d %b %Y')
            assignees = ', '.join(a['username'] for a in t.get('assignees', [])) or 'Unassigned'
            priority  = (t.get('priority') or {}).get('priority', '') or ''
            result.append({
                'name':      t['name'],
                'status':    t['status']['status'],
                'priority':  priority.upper(),
                'due_str':   due_str,
                'days_left': days_left,
                'overdue':   overdue,
                'assignees': assignees,
            })
        # Sort: overdue first (oldest first), then by due date asc, then no-due-date
        def sort_key(t):
            if t['overdue']:
                return (0, t['days_left'])
            if t['days_left'] is not None:
                return (1, t['days_left'])
            return (2, 0)
        result.sort(key=sort_key)
        return result
    except Exception as ex:
        print(f'ClickUp fetch failed: {ex}')
        return []


def get_data():
    companies = fetch_all('companies', [
        'name', 'lifecyclestage', 'notes_last_updated', 'notes_last_contacted',
        'hs_last_logged_call_date', 'createdate',
        'hs_num_open_deals', 'hs_last_sales_activity_date'
    ])
    deals = fetch_all('deals', [
        'dealname', 'dealstage', 'amount', 'closedate',
        'notes_last_updated', 'notes_last_contacted', 'hs_lastmodifieddate',
        'hs_is_closed', 'hs_is_closed_won'
    ])
    meetings_week = fetch_meetings(
        WEEK_START.astimezone(timezone.utc),
        WEEK_END.astimezone(timezone.utc)
    )
    company_by_id = {c['id']: c for c in companies}

    def build_name_map(from_obj, to_obj, ids):
        name_map = {}
        if not ids:
            return name_map
        assoc = fetch_associations_batch(from_obj, to_obj, ids)
        for obj_id, cids in assoc.items():
            for cid in cids:
                c = company_by_id.get(cid)
                if c:
                    name_map[obj_id] = c['properties'].get('name', '-')
                    break
        return name_map

    deal_company_map    = build_name_map('deals',    'companies', [d['id'] for d in deals])
    meeting_company_map = build_name_map('meetings', 'companies', [m['id'] for m in meetings_week])

    meeting_phone_map = {}
    meeting_stage_map = {}
    if meetings_week:
        mid_list      = [m['id'] for m in meetings_week]
        contact_assoc = fetch_associations_batch('meetings', 'contacts', mid_list)
        contact_ids   = list({cid for cids in contact_assoc.values() for cid in cids})
        if contact_ids:
            r = requests.post(
                f'{BASE}/crm/v3/objects/contacts/batch/read',
                headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
                json={
                    'inputs': [{'id': cid} for cid in contact_ids],
                    'properties': ['firstname', 'lastname', 'phone', 'mobilephone', 'lifecyclestage']
                }
            )
            if r.status_code == 200:
                contact_info = {}
                for c in r.json().get('results', []):
                    p     = c['properties']
                    fname = p.get('firstname') or ''
                    lname = p.get('lastname') or ''
                    name  = f'{fname} {lname}'.strip()
                    phone = p.get('mobilephone') or p.get('phone') or ''
                    contact_info[str(c['id'])] = {
                        'name': name, 'phone': phone,
                        'stage': p.get('lifecyclestage') or '',
                    }
                for mid, cids in contact_assoc.items():
                    for cid in cids:
                        info  = contact_info.get(str(cid), {})
                        phone = info.get('phone') or info.get('name') or 'Not on record'
                        meeting_phone_map[mid] = phone
                        if info.get('stage'):
                            meeting_stage_map[mid] = info['stage']
                        break

    for m in meetings_week:
        mid = m['id']
        if meeting_phone_map.get(mid, 'Not on record') == 'Not on record':
            body  = m['properties'].get('hs_meeting_body') or ''
            match = re.search(r'phone_number[:\s]+([+\d][\d\s\-]{6,})', body)
            if match:
                meeting_phone_map[mid] = match.group(1).strip()

    return (companies, deals, meetings_week,
            deal_company_map, meeting_company_map, meeting_phone_map, meeting_stage_map)


def build_report_data(companies, deals, meetings_week,
                      deal_company_map, meeting_company_map, meeting_phone_map,
                      meeting_stage_map, calendar_days):
    open_deals   = [d for d in deals
                    if d['properties'].get('hs_is_closed') != 'true'
                    and d['properties'].get('hs_is_closed_won') != 'true']
    active_deals = sorted(open_deals, key=deal_silence)[:10]

    active_cos = [c for c in companies
                  if not _is_niroggyan(c['properties'].get('name'))
                  and days_since(c['properties'].get('notes_last_contacted')
                                 or c['properties'].get('notes_last_updated')) <= 30]
    active_cos = sorted(active_cos,
                        key=lambda c: days_since(c['properties'].get('notes_last_contacted')
                                                 or c['properties'].get('notes_last_updated')))[:10]

    _stage_map = meeting_stage_map or {}

    # Group HubSpot meetings by IST day, filter excluded titles
    hs_days      = {}
    hs_day_order = []
    for m in meetings_week:
        title = m['properties'].get('hs_meeting_title', '')
        if _is_excluded(title):
            continue
        ts = m['properties'].get('hs_meeting_start_time')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(IST)
        except Exception:
            continue
        day_key = dt.strftime('%A, %d %b')
        if day_key not in hs_days:
            hs_days[day_key] = []
            hs_day_order.append(day_key)
        mid     = m['id']
        company = meeting_company_map.get(mid, '-')
        if _is_niroggyan(company):
            company = '-'
        phone   = meeting_phone_map.get(mid, '-')
        agenda  = parse_agenda(m['properties'].get('hs_meeting_body', ''))
        stage   = format_lifecycle(_stage_map.get(mid, ''))
        source  = format_meeting_source(m['properties'].get('hs_meeting_source'))
        hs_days[day_key].append((m, company, phone, agenda, stage, source))

    hs_meetings_by_day = [(day, hs_days[day]) for day in hs_day_order]

    today_hs       = next((evts for day, evts in hs_meetings_by_day if day == TODAY_LABEL), [])
    meetings_count = len(today_hs)
    week_meetings  = sum(len(evts) for _, evts in calendar_days)

    return {
        'date':              _today_ist.strftime('%d %B %Y'),
        'active_deals':      [(d, deal_silence(d), deal_company_map.get(d['id'], '-')) for d in active_deals],
        'hs_meetings_by_day': hs_meetings_by_day,
        'active_companies':  active_cos,
        'stats': {
            'open_deals_count':    len(open_deals),
            'week_meetings_count': week_meetings,
            'meetings_count':      meetings_count,
            'active_count':        len(active_cos),
        }
    }


# ── Apollo ────────────────────────────────────────────────────────────────────

def _apollo_seq_detail(seq_id):
    r = requests.get(f'{APOLLO_BASE}/emailer_campaigns/{seq_id}', headers=APOLLO_HEADERS)
    return r.json().get('emailer_campaign', {}) if r.status_code == 200 else {}


def _fetch_messages(seq_id, stat_filter=None):
    msgs, page = [], 1
    while page <= 50:
        params = [('emailer_campaign_ids[]', seq_id), ('per_page', 100), ('page', page)]
        if stat_filter:
            params.append(('emailer_message_stats[]', stat_filter))
        r = requests.get(f'{APOLLO_BASE}/emailer_messages/search',
                         headers=APOLLO_HEADERS, params=params)
        if r.status_code != 200:
            break
        batch = r.json().get('emailer_messages', [])
        if not batch:
            break
        msgs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return msgs


def get_apollo_data():
    try:
        all_seqs, page = [], 1
        while True:
            r = requests.post(f'{APOLLO_BASE}/emailer_campaigns/search',
                              headers=APOLLO_HEADERS,
                              json={'per_page': 50, 'page': page})
            if r.status_code != 200:
                break
            d     = r.json()
            batch = d.get('emailer_campaigns', [])
            if not batch:
                break
            all_seqs.extend(s for s in batch if not s.get('archived', False))
            if len(batch) < 50:
                break
            page += 1

        seqs    = sorted(all_seqs, key=lambda s: s.get('name', '').lower())
        results = []

        for seq in seqs:
            sid        = str(seq['id'])
            detail     = _apollo_seq_detail(sid)
            steps_meta = detail.get('emailer_steps', [])
            num_steps  = len(steps_meta) or seq.get('num_steps', 0)

            uniq_delivered = detail.get('unique_delivered') or 0
            uniq_opened    = detail.get('unique_opened') or 0
            uniq_bounced   = detail.get('unique_bounced') or 0

            step_stats = {
                sm.get('position', i + 1): {
                    'sent': 0, 'opened': 0, 'bounced': 0,
                    'replied': 0, 'repliers': set(),
                }
                for i, sm in enumerate(steps_meta)
            }

            all_msgs = _fetch_messages(sid)

            msg_compl_per_step = {}
            for msg in all_msgs:
                pos    = msg.get('campaign_position')
                status = str(msg.get('status') or '').lower()
                if pos not in step_stats:
                    step_stats[pos] = {'sent': 0, 'opened': 0, 'bounced': 0,
                                       'replied': 0, 'repliers': set()}
                if status == 'completed' and not msg.get('spam_blocked'):
                    msg_compl_per_step[pos] = msg_compl_per_step.get(pos, 0) + 1
                if msg.get('replied') or msg.get('reply_class'):
                    name = msg.get('to_name') or msg.get('to_email', 'Unknown')
                    step_stats[pos]['repliers'].add(name)
                    step_stats[pos]['replied'] += 1
                    step_stats[pos]['opened']  += 1

            total_compl = sum(msg_compl_per_step.values())
            for pos, cnt in msg_compl_per_step.items():
                ratio = cnt / total_compl if total_compl > 0 else 0
                step_stats[pos]['sent']    = round(uniq_delivered * ratio)
                step_stats[pos]['bounced'] = round(uniq_bounced   * ratio)

            for pos, cnt in msg_compl_per_step.items():
                ratio = cnt / total_compl if total_compl > 0 else 0
                step_stats[pos]['opened'] = round(uniq_opened * ratio)
            for pos, st in step_stats.items():
                if st['replied'] > st['opened']:
                    step_stats[pos]['opened'] = st['replied']

            sorted_steps = sorted(
                step_stats.items(),
                key=lambda x: x[0] if isinstance(x[0], (int, float)) else 999
            )
            current_step = None
            for pos, st in sorted_steps:
                if st['sent'] > 0:
                    current_step = pos

            results.append({
                'name':          seq.get('name', 'Unknown'),
                'active':        seq.get('active', False),
                'num_steps':     num_steps,
                'current_step':  current_step,
                'steps':         [{
                    'step':     pos,
                    'sent':     st['sent'],
                    'opened':   st['opened'],
                    'bounced':  st['bounced'],
                    'replied':  st['replied'],
                    'repliers': sorted(st['repliers']),
                } for pos, st in sorted_steps],
                'total_replied': sum(st['replied'] for _, st in sorted_steps),
            })

        return results
    except Exception as e:
        return [{'name': 'Apollo fetch failed', 'error': str(e),
                 'active': False, 'num_steps': 0, 'current_step': None,
                 'steps': [], 'total_replied': 0}]


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _ai_briefing_html(briefing):
    if not briefing or briefing.startswith('AI unavailable'):
        return ''
    bullet_html = ''
    for line in briefing.strip().split('\n'):
        line = line.strip().lstrip('•-').strip()
        if not line:
            continue
        bullet_html += (
            f'<div style="display:flex;align-items:flex-start;margin-bottom:10px;">'
            f'<span style="color:#f59e0b;font-size:16px;line-height:1;margin-right:10px;margin-top:1px;">&#9679;</span>'
            f'<span style="font-size:13px;color:#1e293b;line-height:1.6;">{line}</span>'
            f'</div>'
        )
    return f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:20px 32px;">
    <div style="background:#ffffff;border:1px solid #e2e8f0;border-left:4px solid #f59e0b;border-radius:0 8px 8px 0;padding:18px 22px;">
      <div style="font-size:11px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:2px;margin-bottom:14px;">&#9733; AI Morning Briefing</div>
      {bullet_html}
    </div>
  </td></tr>'''


def _day_header_style(day_label, is_today):
    if is_today:
        return ('background:#1a56a0;color:#ffffff;',
                ' <span style="display:inline-block;padding:1px 7px;border-radius:8px;'
                'background:#fbbf24;color:#1a1a1a;font-size:10px;font-weight:700;margin-left:6px;">TODAY</span>')
    return 'background:#f8fafc;color:#0f2744;', ''


def _calendar_html(calendar_days):
    if not calendar_days:
        return ''
    wl    = _week_label()
    total = sum(len(evts) for _, evts in calendar_days)

    rows = ''
    for day_label, events in calendar_days:
        is_today  = (day_label == TODAY_LABEL)
        hdr_style, today_tag = _day_header_style(day_label, is_today)
        rows += (
            f'<tr><td colspan="3" style="padding:7px 14px 5px;font-size:11px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:0.5px;border-top:2px solid #e2e8f0;{hdr_style}">'
            f'{day_label}{today_tag}</td></tr>'
        )
        for ev in events:
            attendee_str = ', '.join(ev['attendees']) if ev['attendees'] else '—'
            meet_badge   = ''
            if ev['meet']:
                meet_badge = (' <span style="display:inline-block;padding:1px 5px;border-radius:6px;'
                              'background:#dbeafe;color:#1d4ed8;font-size:10px;font-weight:700;">Meet</span>')
            bg = '#f0f6ff' if is_today else '#ffffff'
            rows += (
                f'<tr style="background:{bg};">'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#1e293b;">{ev["title"]}{meet_badge}</td>'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:11px;color:#64748b;white-space:nowrap;">{ev["time"]}</td>'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:11px;color:#64748b;">{attendee_str}</td>'
                f'</tr>'
            )

    return f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#128197; Shweta\'s Calendar &mdash; {wl} ({total} meetings)</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f8fafc;">
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Meeting</th>
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Time (IST)</th>
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">External Attendees</th>
      </tr>{rows}
    </table>
  </td></tr>'''


def _hs_meetings_html(hs_meetings_by_day, date_str):
    if not hs_meetings_by_day:
        return ''
    wl    = _week_label()
    total = sum(len(evts) for _, evts in hs_meetings_by_day)

    meeting_th = (
        '<th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Title</th>'
        '<th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>'
        '<th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Time</th>'
        '<th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Phone</th>'
    )

    rows = ''
    for day_label, meetings in hs_meetings_by_day:
        is_today  = (day_label == TODAY_LABEL)
        hdr_style, today_tag = _day_header_style(day_label, is_today)
        rows += (
            f'<tr><td colspan="4" style="padding:7px 14px 5px;font-size:11px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:0.5px;border-top:2px solid #e2e8f0;{hdr_style}">'
            f'{day_label}{today_tag}</td></tr>'
        )
        for m, company, phone, agenda, stage, source in meetings:
            p        = m['properties']
            title    = p.get('hs_meeting_title', 'Meeting')
            time_str = parse_meeting_time(p.get('hs_meeting_start_time'))
            badges   = ''
            if stage:
                badges += (f'<span style="display:inline-block;padding:1px 6px;border-radius:8px;'
                           f'background:#ede9fe;color:#5b21b6;font-size:10px;font-weight:700;'
                           f'margin-left:5px;">{stage}</span>')
            if source:
                clr = ('#dcfce7', '#166534') if source == 'Inbound' else ('#f1f5f9', '#475569')
                badges += (f'<span style="display:inline-block;padding:1px 6px;border-radius:8px;'
                           f'background:{clr[0]};color:{clr[1]};font-size:10px;font-weight:700;'
                           f'margin-left:4px;">{source}</span>')
            bg = '#f0f6ff' if is_today else '#ffffff'
            rows += (
                f'<tr style="background:{bg};">'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#1e293b;">{title}{badges}</td>'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:11px;color:#64748b;">{company}</td>'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:11px;color:#64748b;white-space:nowrap;">{time_str} IST</td>'
                f'<td style="padding:5px 14px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#1e293b;font-weight:500;">{phone}</td>'
                f'</tr>'
            )
            if agenda:
                rows += (
                    f'<tr style="background:{bg};"><td colspan="4" style="padding:2px 14px 6px 20px;'
                    f'border-bottom:1px solid #f1f5f9;font-size:11px;color:#475569;font-style:italic;">'
                    f'Agenda: {agenda}</td></tr>'
                )

    return f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; HubSpot Meetings &mdash; {wl} ({total} meetings)</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #bfdbfe;border-radius:8px;overflow:hidden;">
      <tr style="background:#eff6ff;">{meeting_th}</tr>{rows}
    </table>
  </td></tr>'''


def _clickup_html(tickets):
    if not tickets:
        return ''
    overdue_count = sum(1 for t in tickets if t['overdue'])
    rows = ''
    for t in tickets:
        # Status badge
        if t['status'].lower() == 'in progress':
            st_bg, st_fg = '#dbeafe', '#1d4ed8'
        else:
            st_bg, st_fg = '#f1f5f9', '#475569'
        st_badge = (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                    f'background:{st_bg};color:{st_fg};font-size:10px;font-weight:700;">'
                    f'{t["status"].upper()}</span>')

        # Priority badge
        pri_badge = ''
        if t['priority'] == 'URGENT':
            pri_badge = ('<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                         'background:#fee2e2;color:#b91c1c;font-size:10px;font-weight:700;margin-left:4px;">URGENT</span>')
        elif t['priority'] == 'HIGH':
            pri_badge = ('<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                         'background:#fef3c7;color:#92400e;font-size:10px;font-weight:700;margin-left:4px;">HIGH</span>')
        elif t['priority'] == 'NORMAL':
            pri_badge = ('<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                         'background:#f1f5f9;color:#475569;font-size:10px;font-weight:700;margin-left:4px;">NORMAL</span>')

        # Due date
        if not t['due_str']:
            due_html = '<span style="color:#94a3b8;font-size:11px;">No due date</span>'
        elif t['overdue']:
            due_html = (f'<span style="color:#b91c1c;font-weight:700;font-size:11px;">'
                        f'{t["due_str"]} &nbsp;&#9888; {abs(t["days_left"])}d overdue</span>')
        elif t['days_left'] == 0:
            due_html = f'<span style="color:#d97706;font-weight:700;font-size:11px;">{t["due_str"]} &nbsp;&#9888; Today</span>'
        elif t['days_left'] <= 3:
            due_html = f'<span style="color:#d97706;font-size:11px;">{t["due_str"]} ({t["days_left"]}d)</span>'
        else:
            due_html = f'<span style="color:#64748b;font-size:11px;">{t["due_str"]} ({t["days_left"]}d)</span>'

        row_bg = '#fff5f5' if t['overdue'] else '#ffffff'
        rows += (
            f'<tr style="background:{row_bg};">'
            f'<td style="padding:8px 14px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#1e293b;">'
            f'{t["name"]}</td>'
            f'<td style="padding:8px 14px;border-bottom:1px solid #f1f5f9;white-space:nowrap;">'
            f'{st_badge}{pri_badge}</td>'
            f'<td style="padding:8px 14px;border-bottom:1px solid #f1f5f9;">{due_html}</td>'
            f'<td style="padding:8px 14px;border-bottom:1px solid #f1f5f9;font-size:11px;color:#64748b;">'
            f'{t["assignees"]}</td>'
            f'</tr>'
        )

    overdue_note = ''
    if overdue_count:
        overdue_note = (f' &nbsp;<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
                        f'background:#fee2e2;color:#b91c1c;font-size:11px;font-weight:700;">'
                        f'{overdue_count} overdue</span>')

    return f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">
      &#9632; Customer Support — Active Tickets ({len(tickets)}){overdue_note}
    </div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #fecaca;border-radius:8px;overflow:hidden;">
      <tr style="background:#fef2f2;">
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Task</th>
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Status / Priority</th>
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Due Date</th>
        <th style="padding:7px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Assignee(s)</th>
      </tr>{rows}
    </table>
  </td></tr>'''


def _apollo_html(apollo_data):
    if not apollo_data:
        return ''
    cards = ''
    for seq in apollo_data:
        if 'error' in seq:
            cards += (f'<div style="color:#dc2626;font-size:13px;padding:6px 0;">'
                      f'Error: {seq["error"]}</div>')
            continue
        status_badge = (
            '<span style="display:inline-block;padding:2px 9px;border-radius:10px;'
            'background:#dcfce7;color:#166534;font-size:11px;font-weight:700;">Active</span>'
            if seq['active'] else
            '<span style="display:inline-block;padding:2px 9px;border-radius:10px;'
            'background:#f1f5f9;color:#475569;font-size:11px;font-weight:700;">Paused</span>'
        )
        cur        = seq['current_step']
        step_label = f'Step {cur} of {seq["num_steps"]}' if cur else f'{seq["num_steps"]} steps'

        step_rows = ''
        for s in seq['steps']:
            pct = lambda n, d: f'{round(n / d * 100)}%' if d else '-'
            open_pct   = pct(s['opened'],  s['sent'])
            reply_pct  = pct(s['replied'], s['sent'])
            bounce_pct = pct(s['bounced'], s['sent'])
            replier_row = ''
            if s['repliers']:
                names = ', '.join(s['repliers'])
                replier_row = (
                    f'<tr><td colspan="7" style="padding:4px 16px 8px;font-size:11px;'
                    f'color:#166534;background:#f0fdf4;font-style:italic;">'
                    f'&#8627; Replied: {names}</td></tr>'
                )
            step_rows += (
                f'<tr style="border-top:1px solid #f1f5f9;">'
                f'<td style="padding:7px 12px;font-size:13px;color:#334155;">Step {s["step"]}</td>'
                f'<td style="padding:7px 12px;font-size:13px;color:#0f2744;font-weight:600;text-align:center;">{s["sent"]}</td>'
                f'<td style="padding:7px 12px;font-size:13px;color:#0f2744;font-weight:600;text-align:center;">{s["opened"]}</td>'
                f'<td style="padding:7px 12px;font-size:12px;color:#64748b;text-align:center;">{open_pct}</td>'
                f'<td style="padding:7px 12px;font-size:13px;color:#166534;font-weight:600;text-align:center;">{s["replied"]}</td>'
                f'<td style="padding:7px 12px;font-size:12px;color:#64748b;text-align:center;">{reply_pct}</td>'
                f'<td style="padding:7px 12px;font-size:12px;color:#b45309;text-align:center;">{s["bounced"]} ({bounce_pct})</td>'
                f'</tr>{replier_row}'
            )

        cards += f'''
<div style="border:1px solid #e2e8f0;border-radius:8px;margin-bottom:14px;overflow:hidden;">
  <div style="background:#f8fafc;padding:11px 14px;display:flex;justify-content:space-between;align-items:center;">
    <span style="font-size:13px;font-weight:700;color:#0f2744;">{seq["name"]}</span>
    <span>{status_badge}&nbsp;&nbsp;<span style="font-size:12px;color:#64748b;">Currently on <strong style="color:#0f2744;">{step_label}</strong></span></span>
  </div>
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr style="background:#f8fafc;">
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:left;font-weight:600;">STEP</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">DELIVERED</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">OPENED</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">OPEN %</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">REPLIED</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">REPLY %</th>
      <th style="padding:6px 12px;font-size:11px;color:#94a3b8;text-align:center;font-weight:600;">BOUNCED</th>
    </tr>
    {step_rows}
  </table>
</div>'''
    return f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:16px;">&#9632; Email Sequences (Apollo)</div>
    {cards}
  </td></tr>'''


def format_html(data, calendar_days, ai_briefing='', clickup_tickets=None, apollo_data=None):
    stats = data['stats']

    def badge(text, color):
        colors = {
            'red':    ('fee2e2', 'b91c1c'),
            'yellow': ('fef3c7', '92400e'),
            'green':  ('dcfce7', '166534'),
            'blue':   ('eff6ff', '1d4ed8'),
        }
        bg, fg = colors.get(color, ('f1f5f9', '475569'))
        return (f'<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
                f'background:#{bg};color:#{fg};font-size:11px;font-weight:700;">{text}</span>')

    def deal_row(d, silence, company):
        p         = d['properties']
        name      = p.get('dealname') or '-'
        close_raw = p.get('closedate') or ''
        try:
            close_dt  = datetime.fromisoformat(close_raw.replace('Z', '+00:00'))
            close_str = close_dt.strftime('%d %b %Y')
        except Exception:
            close_str = '-'
        col = 'green' if silence <= 7 else 'blue'
        co  = '-' if _is_niroggyan(company) else (company or '-')
        return (
            f'<tr>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f8fafc;font-size:13px;color:#1e293b;font-weight:500;">{name}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;">{co}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f8fafc;text-align:center;">{badge(f"active {silence}d ago", col)}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;">{close_str}</td>'
            f'</tr>'
        )

    active_rows = ''.join(deal_row(d, s, c) for d, s, c in data['active_deals'])
    deal_th = (
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Deal</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Activity</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Close Date</th>'
    )

    company_rows = ''
    for c in data['active_companies']:
        p            = c['properties']
        last_contact = days_since(p.get('notes_last_contacted') or p.get('notes_last_updated'))
        last_call    = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never'
        stage        = (p.get('lifecyclestage') or '-').replace('marketingqualifiedlead', 'MQL')
        col          = 'green' if last_contact <= 7 else 'yellow'
        company_rows += (
            f'<tr>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f0fdf4;font-size:13px;color:#1e293b;font-weight:500;">{p.get("name","-")}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f0fdf4;text-align:center;">{badge(f"{last_contact}d ago", col)}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f0fdf4;font-size:12px;color:#64748b;">{last_call}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #f0fdf4;font-size:12px;color:#64748b;">{stage}</td>'
            f'</tr>'
        )

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

  <tr><td style="background:linear-gradient(135deg,#0f2744,#1a56a0);border-radius:12px 12px 0 0;padding:28px 32px;">
    <div style="color:white;font-size:22px;font-weight:700;letter-spacing:-0.5px;">NirogGyan Daily Pulse</div>
    <div style="color:rgba(255,255,255,0.65);font-size:13px;margin-top:4px;">{data["date"]} &nbsp;|&nbsp; Auto-generated report</div>
  </td></tr>

  <tr><td style="background:#fff;padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{stats["open_deals_count"]}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Open Deals</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{stats["week_meetings_count"]}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Week Meetings</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{stats["meetings_count"]}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Meetings Today</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{stats["active_count"]}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Active This Month</div>
      </td>
    </tr></table>
  </td></tr>

  {_ai_briefing_html(ai_briefing)}

  {_calendar_html(calendar_days)}

  {_hs_meetings_html(data["hs_meetings_by_day"], data["date"])}

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:12px 32px 24px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; 10 Most Active Deals &mdash; Recently Engaged</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #bbf7d0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f0fdf4;">{deal_th}</tr>{active_rows}
    </table>
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Active Client Conversations This Month</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #bbf7d0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f0fdf4;">
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Last Contact</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Last Call</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Stage</th>
      </tr>{company_rows}
    </table>
  </td></tr>

  {_clickup_html(clickup_tickets or [])}

  {_apollo_html(apollo_data)}

  <tr><td style="background:#0f2744;border-radius:0 0 12px 12px;padding:16px 32px;text-align:center;">
    <div style="color:rgba(255,255,255,0.5);font-size:11px;">NirogGyan &nbsp;|&nbsp; Automated daily report &nbsp;|&nbsp; {data["date"]}</div>
  </td></tr>

</table>
</body>
</html>'''


# ── PDF ───────────────────────────────────────────────────────────────────────

class PulsePDF(FPDF):
    def header(self):
        self.set_fill_color(15, 39, 68)
        self.rect(0, 0, 210, 22, 'F')
        self.set_font('Helvetica', 'B', 14)
        self.set_text_color(255, 255, 255)
        self.set_xy(12, 6)
        self.cell(0, 10, 'NirogGyan Daily Pulse', new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font('Helvetica', '', 9)
        self.set_text_color(180, 200, 220)
        self.set_xy(12, 14)
        self.cell(0, 6, 'Auto-generated report', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def footer(self):
        self.set_y(-12)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, pdf_safe(f'NirogGyan Daily Pulse - {_today_ist.strftime("%d %B %Y")}'), align='C')

    def section_title(self, title):
        self.ln(4)
        self.set_fill_color(240, 244, 248)
        self.set_font('Helvetica', 'B', 9)
        self.set_text_color(15, 39, 68)
        self.cell(0, 8, pdf_safe(f'  {title}'), new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
        self.ln(2)

    def tbl_header(self, cols):
        self.set_fill_color(248, 250, 252)
        self.set_font('Helvetica', 'B', 8)
        self.set_text_color(100, 116, 139)
        for col, w in cols:
            self.cell(w, 7, pdf_safe(col), fill=True, border='B')
        self.ln()

    def tbl_row(self, cells, shade=False):
        if shade:
            self.set_fill_color(252, 252, 253)
        self.set_font('Helvetica', '', 9)
        self.set_text_color(30, 41, 59)
        for text, w in cells:
            self.cell(w, 6, pdf_safe(str(text)[:52]), fill=shade)
        self.ln()

    def day_header(self, label, is_today):
        self.ln(2)
        if is_today:
            self.set_fill_color(26, 86, 160)
            self.set_text_color(255, 255, 255)
        else:
            self.set_fill_color(240, 244, 248)
            self.set_text_color(15, 39, 68)
        self.set_font('Helvetica', 'B', 8)
        tag = '  [TODAY]' if is_today else ''
        self.cell(0, 6, pdf_safe(label.upper() + tag),
                  fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def format_pdf(data, calendar_days, ai_briefing='', clickup_tickets=None, apollo_data=None):
    stats = data['stats']

    pdf = PulsePDF()
    pdf.set_margins(12, 28, 12)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(0, 7, pdf_safe(data['date']), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_fill_color(15, 39, 68)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(255, 255, 255)
    for label, val in [('Open Deals', stats['open_deals_count']),
                       ('Week Meetings', stats['week_meetings_count']),
                       ('Meetings Today', stats['meetings_count']),
                       ('Active This Month', stats['active_count'])]:
        pdf.cell(46, 12, pdf_safe(f'{val}  {label}'), fill=True, align='C')
    pdf.ln(16)

    # AI briefing box
    if ai_briefing and not ai_briefing.startswith('AI unavailable'):
        W = pdf.w - pdf.l_margin - pdf.r_margin
        pdf.set_fill_color(15, 39, 68)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(245, 158, 11)
        pdf.set_x(pdf.l_margin)
        pdf.cell(W, 7, '  * AI MORNING BRIEFING', fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_fill_color(26, 58, 107)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(220, 235, 255)
        for line in ai_briefing.strip().split('\n'):
            line = line.strip().lstrip('•-').strip()
            if not line:
                continue
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(W, 6, pdf_safe(f'  • {line}'), fill=True)
        pdf.ln(6)

    def page_guard(min_mm=40):
        if pdf.h - pdf.b_margin - pdf.get_y() < min_mm:
            pdf.add_page()

    def pdf_close(d):
        raw = d['properties'].get('closedate') or ''
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).strftime('%d %b %Y')
        except Exception:
            return '-'

    # Google Calendar section
    if calendar_days:
        wl = _week_label()
        pdf.section_title(f"SHWETA'S CALENDAR — {wl}")
        for day_label, events in calendar_days:
            is_today = (day_label == TODAY_LABEL)
            page_guard(20)
            pdf.day_header(day_label, is_today)
            pdf.tbl_header([('Meeting', 95), ('Time', 50), ('External Attendees', 41)])
            for i, ev in enumerate(events):
                attendees = ', '.join(ev['attendees']) if ev['attendees'] else '-'
                title     = ev['title'] + (' [Meet]' if ev['meet'] else '')
                if is_today:
                    pdf.set_fill_color(240, 246, 255)
                pdf.tbl_row([
                    (title, 95),
                    (ev['time'].replace(' IST', ''), 50),
                    (attendees, 41),
                ], shade=(i % 2 == 1))

    # HubSpot meetings section
    hs_by_day = data['hs_meetings_by_day']
    if hs_by_day:
        wl = _week_label()
        pdf.ln(4)
        page_guard()
        pdf.section_title(f'HUBSPOT MEETINGS — {wl}')
        for day_label, meetings in hs_by_day:
            is_today = (day_label == TODAY_LABEL)
            page_guard(20)
            pdf.day_header(day_label, is_today)
            pdf.tbl_header([('Title', 58), ('Source', 28), ('Company', 40), ('Time IST', 24), ('Phone', 36)])
            for i, (m, company, phone, agenda, stage, source) in enumerate(meetings):
                p        = m['properties']
                time_str = parse_meeting_time(p.get('hs_meeting_start_time'))
                lead_src = ' / '.join(filter(None, [stage, source])) or '-'
                if is_today:
                    pdf.set_fill_color(240, 246, 255)
                pdf.tbl_row([
                    (p.get('hs_meeting_title', 'Meeting'), 58),
                    (lead_src, 28),
                    (company, 40),
                    (time_str, 24),
                    (phone, 36),
                ], shade=(i % 2 == 1))
                if agenda:
                    pdf.set_font('Helvetica', 'I', 8)
                    pdf.set_text_color(71, 85, 105)
                    pdf.multi_cell(0, 5, pdf_safe(f'    Agenda: {agenda}'))

    # Active deals
    pdf.ln(4)
    page_guard()
    pdf.section_title('10 MOST ACTIVE DEALS — RECENTLY ENGAGED')
    pdf.tbl_header([('Deal', 70), ('Company', 55), ('Activity', 35), ('Close Date', 26)])
    for i, (d, silence, company) in enumerate(data['active_deals']):
        p  = d['properties']
        co = '-' if _is_niroggyan(company) else (company or '-')
        pdf.tbl_row([
            (p.get('dealname', '-'), 70),
            (co, 55),
            (f'active {silence}d ago', 35),
            (pdf_close(d), 26),
        ], shade=(i % 2 == 1))

    pdf.ln(4)
    page_guard()
    pdf.section_title('ACTIVE CLIENT CONVERSATIONS THIS MONTH')
    pdf.tbl_header([('Company', 75), ('Last Contact', 38), ('Last Call', 45), ('Stage', 28)])
    for i, c in enumerate(data['active_companies']):
        p            = c['properties']
        last_contact = days_since(p.get('notes_last_contacted') or p.get('notes_last_updated'))
        last_call    = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never'
        stage        = (p.get('lifecyclestage') or '-').replace('marketingqualifiedlead', 'MQL')
        pdf.tbl_row([
            (p.get('name', '-'), 75),
            (f'{last_contact}d ago', 38),
            (last_call, 45),
            (stage, 28),
        ], shade=(i % 2 == 1))

    if clickup_tickets:
        pdf.ln(4)
        page_guard()
        overdue_count = sum(1 for t in clickup_tickets if t['overdue'])
        title = f'CUSTOMER SUPPORT — ACTIVE TICKETS ({len(clickup_tickets)})'
        if overdue_count:
            title += f'  [{overdue_count} OVERDUE]'
        pdf.section_title(title)
        pdf.tbl_header([('Task', 90), ('Status', 24), ('Priority', 22), ('Due Date', 28), ('Assignee(s)', 22)])
        for i, t in enumerate(clickup_tickets):
            if not t['due_str']:
                due_label = 'No date'
            elif t['overdue']:
                due_label = f'{t["due_str"]} OVR'
            else:
                due_label = t['due_str']
            pdf.tbl_row([
                (t['name'], 90),
                (t['status'].upper(), 24),
                (t['priority'] or '-', 22),
                (due_label, 28),
                (t['assignees'], 22),
            ], shade=(i % 2 == 1))

    if apollo_data:
        pdf.ln(4)
        page_guard(40)
        pdf.section_title('EMAIL SEQUENCES (APOLLO)')
        for seq in apollo_data:
            if 'error' in seq:
                pdf.set_font('Helvetica', '', 9)
                pdf.set_text_color(220, 38, 38)
                pdf.cell(0, 7, pdf_safe(f'  Error: {seq["error"]}'),
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                continue
            page_guard(50)
            status     = 'Active' if seq['active'] else 'Paused'
            cur        = seq['current_step']
            step_label = f'Step {cur} of {seq["num_steps"]}' if cur else f'{seq["num_steps"]} steps'
            pdf.ln(3)
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(15, 39, 68)
            pdf.cell(0, 7, pdf_safe(f'  {seq["name"]}  [{status}]  --  Currently on {step_label}'),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if seq['steps']:
                pdf.tbl_header([
                    ('Step', 25), ('Delivered', 28), ('Opened', 28), ('Open%', 22),
                    ('Replied', 28), ('Reply%', 22), ('Bounced', 33),
                ])
                for i, s in enumerate(seq['steps']):
                    pct = lambda n, d: f'{round(n / d * 100)}%' if d else '-'
                    pdf.tbl_row([
                        (f'Step {s["step"]}', 25),
                        (str(s['sent']),    28),
                        (str(s['opened']),  28),
                        (pct(s['opened'],  s['sent']), 22),
                        (str(s['replied']), 28),
                        (pct(s['replied'], s['sent']), 22),
                        (str(s['bounced']), 33),
                    ], shade=(i % 2 == 1))
                    if s['repliers']:
                        names = ', '.join(s['repliers'])
                        pdf.set_font('Helvetica', 'I', 8)
                        pdf.set_text_color(22, 101, 52)
                        pdf.multi_cell(0, 5, pdf_safe(f'    Replied: {names}'))

    return bytes(pdf.output())


# ── Email ─────────────────────────────────────────────────────────────────────

RECIPIENTS = [RECIPIENT_EMAIL, 'joyneel@niroggyan.com']


def send_email(subject, html_body, pdf_bytes, date_str):
    msg            = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = ', '.join(RECIPIENTS)

    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(html_body, 'html'))
    msg.attach(alt)

    pdf_part = MIMEBase('application', 'pdf')
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    filename = f'NirogGyan_Pulse_{date_str.replace(" ", "_")}.pdf'
    pdf_part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
    msg.attach(pdf_part)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENTS, msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Fetching Google Calendar (week)...')
    calendar_days = fetch_calendar_week()
    print(f'Got {sum(len(e) for _, e in calendar_days)} calendar events across {len(calendar_days)} days\n')

    print('Fetching HubSpot data...')
    (companies, deals, meetings_week,
     deal_company_map, meeting_company_map,
     meeting_phone_map, meeting_stage_map) = get_data()

    print('Building report data...')
    data = build_report_data(
        companies, deals, meetings_week,
        deal_company_map, meeting_company_map,
        meeting_phone_map, meeting_stage_map,
        calendar_days
    )

    print('Fetching ClickUp active tickets...')
    clickup_tickets = fetch_clickup_tickets()
    print(f'Got {len(clickup_tickets)} tickets\n')

    print('Fetching Apollo sequence analytics...')
    apollo_data = get_apollo_data()

    print('Generating AI briefing...')
    ai_briefing = build_ai_briefing(data, clickup_tickets, calendar_days)
    print(f'AI briefing: {ai_briefing[:80]}...\n')

    print('Generating HTML and PDF...')
    date_str = data['date']
    html = format_html(data, calendar_days, ai_briefing=ai_briefing, clickup_tickets=clickup_tickets, apollo_data=apollo_data)
    pdf  = format_pdf(data, calendar_days, ai_briefing=ai_briefing, clickup_tickets=clickup_tickets, apollo_data=apollo_data)

    print('Sending email...')
    send_email(f"NirogGyan Daily Pulse - {date_str}", html, pdf, date_str)
    print('Done. Email sent.')
