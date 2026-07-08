import re
import requests
import smtplib
import os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from fpdf import FPDF, XPos, YPos


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


def strip_markdown(text):
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text


HUBSPOT_TOKEN      = os.environ['HUBSPOT_TOKEN']
GROQ_API_KEY       = os.environ['GROQ_API_KEY']
GMAIL_ADDRESS      = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']
RECIPIENT_EMAIL    = os.environ['RECIPIENT_EMAIL']
APOLLO_API_KEY     = os.environ['APOLLO_API_KEY']

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
TODAY_START = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
TODAY_END   = NOW.replace(hour=23, minute=59, second=59, microsecond=999999)


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


def fetch_meetings_today():
    start_ms = int(TODAY_START.timestamp() * 1000)
    end_ms   = int(TODAY_END.timestamp() * 1000)
    r = requests.post(
        f'{BASE}/crm/v3/objects/meetings/search',
        headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
        json={
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'hs_meeting_start_time',
                    'operator': 'BETWEEN',
                    'value': str(start_ms),
                    'highValue': str(end_ms)
                }]
            }],
            'properties': ['hs_meeting_title', 'hs_meeting_start_time', 'hs_meeting_end_time', 'hs_meeting_body'],
            'limit': 50
        }
    )
    return r.json().get('results', []) if r.status_code == 200 else []


def parse_meeting_time(ts_val):
    if not ts_val:
        return '-'
    try:
        dt = datetime.fromisoformat(str(ts_val).replace('Z', '+00:00'))
        ist = dt + timedelta(hours=5, minutes=30)
        return ist.strftime('%I:%M %p')
    except Exception:
        return '-'


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
    tickets = fetch_all('tickets', [
        'subject', 'hs_ticket_priority', 'hs_pipeline_stage', 'createdate'
    ])
    meetings_today = fetch_meetings_today()
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

    ticket_company_map  = build_name_map('tickets',  'companies', [t['id'] for t in tickets])
    deal_company_map    = build_name_map('deals',    'companies', [d['id'] for d in deals])
    meeting_company_map = build_name_map('meetings', 'companies', [m['id'] for m in meetings_today])

    # Fetch contact phone numbers for meetings
    meeting_phone_map = {}
    if meetings_today:
        mid_list = [m['id'] for m in meetings_today]
        contact_assoc = fetch_associations_batch('meetings', 'contacts', mid_list)
        contact_ids = list({cid for cids in contact_assoc.values() for cid in cids})
        if contact_ids:
            r = requests.post(
                f'{BASE}/crm/v3/objects/contacts/batch/read',
                headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
                json={
                    'inputs': [{'id': cid} for cid in contact_ids],
                    'properties': ['firstname', 'lastname', 'phone', 'mobilephone']
                }
            )
            if r.status_code == 200:
                contact_info = {}
                for c in r.json().get('results', []):
                    p = c['properties']
                    fname = p.get('firstname') or ''
                    lname = p.get('lastname') or ''
                    name  = f'{fname} {lname}'.strip()
                    phone = p.get('mobilephone') or p.get('phone') or ''
                    contact_info[str(c['id'])] = {'name': name, 'phone': phone}
                for mid, cids in contact_assoc.items():
                    for cid in cids:
                        info  = contact_info.get(str(cid), {})
                        phone = info.get('phone') or info.get('name') or 'Not on record'
                        meeting_phone_map[mid] = phone
                        break

    # Fallback: parse phone_number from meeting body if not found via contact
    for m in meetings_today:
        mid = m['id']
        if meeting_phone_map.get(mid, 'Not on record') == 'Not on record':
            body = m['properties'].get('hs_meeting_body') or ''
            match = re.search(r'phone_number[:\s]+([+\d][\d\s\-]{6,})', body)
            if match:
                meeting_phone_map[mid] = match.group(1).strip()

    return companies, deals, tickets, meetings_today, ticket_company_map, deal_company_map, meeting_company_map, meeting_phone_map


def build_report_data(companies, deals, tickets, meetings_today,
                      ticket_company_map, deal_company_map, meeting_company_map, meeting_phone_map):
    open_deals    = [d for d in deals
                     if d['properties'].get('hs_is_closed') != 'true'
                     and d['properties'].get('hs_is_closed_won') != 'true']
    silent_deals  = sorted(open_deals, key=deal_silence, reverse=True)[:5]
    active_deals  = sorted(open_deals, key=deal_silence)[:5]

    open_tickets  = [t for t in tickets if t['properties'].get('hs_pipeline_stage') != '4']
    top_tickets   = sorted(open_tickets,
                           key=lambda t: days_since(t['properties'].get('createdate')),
                           reverse=True)[:10]

    active_cos    = [c for c in companies
                     if days_since(c['properties'].get('notes_last_contacted')
                                   or c['properties'].get('notes_last_updated')) <= 30]
    active_cos    = sorted(active_cos,
                           key=lambda c: days_since(c['properties'].get('notes_last_contacted')
                                                    or c['properties'].get('notes_last_updated')))[:10]

    def meeting_ts(m):
        try:
            return int(m['properties'].get('hs_meeting_start_time') or 0)
        except Exception:
            return 0

    meetings_sorted = sorted(meetings_today, key=meeting_ts)

    return {
        'date':            NOW.strftime('%d %B %Y'),
        'silent_deals':    [(d, deal_silence(d), deal_company_map.get(d['id'], '-'))
                            for d in silent_deals],
        'active_deals':    [(d, deal_silence(d), deal_company_map.get(d['id'], '-'))
                            for d in active_deals],
        'open_tickets':    [(t, ticket_company_map.get(t['id'], '-')) for t in top_tickets],
        'meetings':        [(m, meeting_company_map.get(m['id'], '-'), meeting_phone_map.get(m['id'], '-')) for m in meetings_sorted],
        'active_companies': active_cos,
        'stats': {
            'open_deals_count':   len(open_deals),
            'open_tickets_count': len(open_tickets),
            'meetings_count':     len(meetings_today),
            'active_count':       len(active_cos),
        }
    }


def build_ai_context(report_data, ticket_company_map, deal_company_map, meeting_company_map):
    report_companies = {}

    def ensure(name):
        if name and name != '-':
            report_companies.setdefault(name, {'deals': [], 'tickets': [], 'meeting': False, 'last_contact': None, 'last_call': None})

    for d, silence, company in report_data['silent_deals']:
        ensure(company)
        if company in report_companies:
            p = d['properties']
            report_companies[company]['deals'].append({
                'name': p.get('dealname', '-'),
                'stage': p.get('dealstage', '-'),
                'silence': silence
            })

    for t, company in report_data['open_tickets']:
        ensure(company)
        if company in report_companies:
            p = t['properties']
            report_companies[company]['tickets'].append({
                'subject': p.get('subject', '-'),
                'priority': p.get('hs_ticket_priority', '?'),
                'age': days_since(p.get('createdate'))
            })

    for m, company, _phone in report_data['meetings']:
        ensure(company)
        if company in report_companies:
            report_companies[company]['meeting'] = True

    for c in report_data['active_companies']:
        p    = c['properties']
        name = p.get('name', '-')
        ensure(name)
        if name in report_companies:
            report_companies[name]['last_contact'] = days_since(
                p.get('notes_last_contacted') or p.get('notes_last_updated'))
            report_companies[name]['last_call'] = (p.get('hs_last_logged_call_date') or '')[:10] or 'never'

    lines = [
        "You are analyzing CRM data for NirogGyan, a B2B healthcare SaaS company in India.",
        "Below is data ONLY for companies that appear in today's report across deals, tickets, meetings, and active conversations.",
        "Give practical insights. Plain text only, no markdown, no ** or ##.",
        '',
        '--- COMPANY DATA ---',
    ]

    for name, s in report_companies.items():
        parts = [f'Company: {name}']
        if s['last_contact'] is not None:
            parts.append(f'last contact: {s["last_contact"]}d ago')
        if s['last_call']:
            parts.append(f'last call: {s["last_call"]}')
        if s['meeting']:
            parts.append('has meeting TODAY')
        if s['deals']:
            deal_strs = [f'{d["name"]} ({d["stage"]}, {d["silence"]}d silent)' for d in s['deals']]
            parts.append(f'open deals: {", ".join(deal_strs)}')
        if s['tickets']:
            tix_strs = [f'{t["subject"]} [{t["priority"]}, {t["age"]}d old]' for t in s['tickets']]
            parts.append(f'open tickets: {", ".join(tix_strs)}')
        lines.append('- ' + ' | '.join(parts))

    lines += [
        '',
        '--- INSTRUCTIONS ---',
        'Analyze the above CRM data and give insights in exactly 3 sections.',
        'You decide what the 3 sections are based on what is most useful in the data.',
        'Each section must start with a title line prefixed by ##, like: ## Recent Engagements',
        'Then write 2-4 lines of practical insight below the title.',
        'Use company names. Be specific. No bold (**), no bullet points, just plain text under each ## title.',
    ]

    return '\n'.join(lines)

def get_ai_insight(context):
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}',
                     'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [{'role': 'user', 'content': context}],
                'max_tokens': 500
            }
        )
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f'AI analysis unavailable: {e}'


# ── HTML ──────────────────────────────────────────────────────────────────────

def _apollo_seq_detail(seq_id):
    r = requests.get(f'{APOLLO_BASE}/emailer_campaigns/{seq_id}', headers=APOLLO_HEADERS)
    return r.json().get('emailer_campaign', {}) if r.status_code == 200 else {}


def _fetch_all_messages(seq_id):
    """Paginate through all messages for a sequence."""
    msgs, page = [], 1
    while page <= 50:
        params = [('emailer_campaign_ids[]', seq_id), ('per_page', 100), ('page', page)]
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
        # Fetch ALL non-archived sequences
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

            # Pre-populate all steps with zero counts so every step shows up
            step_stats = {
                s.get('position', i + 1): {
                    'sent': 0, 'opened': 0, 'bounced': 0,
                    'replied': 0, 'repliers': set(),
                }
                for i, s in enumerate(steps_meta)
            }

            # Fetch all messages and parse status directly
            # status field: 'scheduled'|'drafted'|'not_opened'|'opened'|'clicked'|
            #               'bounced'|'unsubscribed'|'spam_blocked'|'failed_other'
            # campaign_position = step number (1-based)
            # replied = bool, reply_class = str, to_name = contact name
            for msg in _fetch_all_messages(sid):
                pos    = msg.get('campaign_position')
                status = str(msg.get('status') or '').lower()

                if pos not in step_stats:
                    step_stats[pos] = {'sent': 0, 'opened': 0, 'bounced': 0,
                                       'replied': 0, 'repliers': set()}

                is_replied = bool(msg.get('replied') or msg.get('reply_class'))
                # delivered = actually reached inbox (excludes bounced/spam/failed)
                if status in ('delivered', 'not_opened', 'opened', 'clicked', 'unsubscribed') or is_replied:
                    step_stats[pos]['sent'] += 1
                if status in ('opened', 'clicked') or is_replied:
                    step_stats[pos]['opened'] += 1
                if status == 'bounced' or msg.get('bounce'):
                    step_stats[pos]['bounced'] += 1
                if is_replied:
                    name = msg.get('to_name') or msg.get('to_email', 'Unknown')
                    step_stats[pos]['repliers'].add(name)
                    step_stats[pos]['replied'] += 1

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
            open_pct    = pct(s['opened'],  s['sent'])
            reply_pct   = pct(s['replied'], s['sent'])
            bounce_pct  = pct(s['bounced'], s['sent'])
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
                f'</tr>'
                f'{replier_row}'
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


def format_html(data, ai_insight, apollo_data=None):
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

    def deal_row(d, silence, company, is_silent):
        p         = d['properties']
        name      = p.get('dealname') or '-'
        close_raw = p.get('closedate') or ''
        try:
            close_dt  = datetime.fromisoformat(close_raw.replace('Z', '+00:00'))
            close_str = close_dt.strftime('%d %b %Y')
        except Exception:
            close_str = '-'
        col = ('red' if silence >= 30 else 'yellow') if is_silent else ('green' if silence <= 7 else 'blue')
        label = f'{silence}d silent' if is_silent else f'active {silence}d ago'
        row = (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:13px;color:#1e293b;font-weight:500;">{name}</td>'
        )
        if company and company != '-':
            row += f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;">{company}</td>'
        else:
            row += f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#94a3b8;">-</td>'
        row += (
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;text-align:center;">{badge(label, col)}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;">{close_str}</td>'
            f'</tr>'
        )
        return row

    silent_rows = ''.join(deal_row(d, s, c, True)  for d, s, c in data['silent_deals'])
    active_rows = ''.join(deal_row(d, s, c, False) for d, s, c in data['active_deals'])

    deal_th = (
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Deal</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Activity</th>'
        '<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Close Date</th>'
    )

    ticket_rows = ''
    for t, company in data['open_tickets']:
        p   = t['properties']
        age = days_since(p.get('createdate'))
        pri = p.get('hs_ticket_priority', '?')
        col = 'red' if pri == 'HIGH' else 'yellow'
        ticket_rows += (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:13px;color:#1e293b;">{p.get("subject","-")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;">{company}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;text-align:center;">{badge(pri, col)}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;text-align:center;">{age}d</td>'
            f'</tr>'
        )

    meeting_rows = ''
    for m, company, phone in data['meetings']:
        p        = m['properties']
        title    = p.get('hs_meeting_title', 'Meeting')
        time_str = parse_meeting_time(p.get('hs_meeting_start_time'))
        meeting_rows += (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:13px;color:#1e293b;">{title}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:12px;color:#64748b;">{company}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:12px;color:#64748b;">{time_str} IST</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:12px;color:#1e293b;font-weight:500;">{phone}</td>'
            f'</tr>'
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
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0fdf4;font-size:13px;color:#1e293b;font-weight:500;">{p.get("name","-")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0fdf4;text-align:center;">{badge(f"{last_contact}d ago", col)}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0fdf4;font-size:12px;color:#64748b;">{last_call}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0fdf4;font-size:12px;color:#64748b;">{stage}</td>'
            f'</tr>'
        )

    ai_html = ''
    for line in ai_insight.strip().split('\n'):
        s = line.strip()
        if not s:
            ai_html += '<div style="height:10px;"></div>'
        elif s.startswith('##'):
            title = s.lstrip('#').strip()
            ai_html += (f'<p style="margin:16px 0 6px;font-size:12px;font-weight:700;'
                        f'color:#0f2744;text-transform:uppercase;letter-spacing:1px;'
                        f'border-bottom:1px solid #e2e8f0;padding-bottom:4px;">{title}</p>')
        else:
            s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            ai_html += f'<p style="margin:4px 0;font-size:13px;color:#1e3a5f;line-height:1.7;">{s}</p>'

    if data['meetings']:
        meetings_section = f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Meetings Today ({stats["meetings_count"]})</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #bfdbfe;border-radius:8px;overflow:hidden;">
      <tr style="background:#eff6ff;">
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Title</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Time (IST)</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Phone</th>
      </tr>{meeting_rows}
    </table>
  </td></tr>'''
    else:
        meetings_section = '''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Meetings Today</div>
    <p style="font-size:13px;color:#94a3b8;margin:0;">No meetings scheduled for today.</p>
  </td></tr>'''

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
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{stats["open_tickets_count"]}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Open Tickets</div>
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

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:16px 32px;">
    <div style="background:#f8faff;border:1px solid #c7d9f5;border-left:4px solid #1a56a0;border-radius:0 10px 10px 0;padding:18px 22px;">
      <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px;">&#9733; AI Analysis</div>
      {ai_html}
    </div>
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px 12px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; 5 Most Silent Deals — Needs Follow-Up</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #fecaca;border-radius:8px;overflow:hidden;">
      <tr style="background:#fef2f2;">{deal_th}</tr>{silent_rows}
    </table>
  </td></tr>

  <tr><td style="background:#fff;padding:12px 32px 24px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; 5 Most Active Deals — Recently Engaged</div>
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

  {meetings_section}

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Top 10 Open Tickets</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f8fafc;">
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Subject</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Priority</th>
        <th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Age</th>
      </tr>{ticket_rows}
    </table>
  </td></tr>

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
        self.cell(0, 8, pdf_safe(f'NirogGyan Daily Pulse - {NOW.strftime("%d %B %Y")}'), align='C')

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
            self.cell(w, 7, pdf_safe(str(text)[:50]), fill=shade)
        self.ln()


def format_pdf(data, ai_insight, apollo_data=None):
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
                       ('Open Tickets', stats['open_tickets_count']),
                       ('Meetings Today', stats['meetings_count']),
                       ('Active This Month', stats['active_count'])]:
        pdf.cell(46, 12, pdf_safe(f'{val}  {label}'), fill=True, align='C')
    pdf.ln(16)

    def page_guard(min_mm=45):
        if pdf.h - pdf.b_margin - pdf.get_y() < min_mm:
            pdf.add_page()

    def pdf_close(d):
        raw = d['properties'].get('closedate') or ''
        try:
            return datetime.fromisoformat(raw.replace('Z','+00:00')).strftime('%d %b %Y')
        except Exception:
            return '-'

    pdf.section_title('5 MOST SILENT DEALS - NEEDS FOLLOW-UP')
    pdf.tbl_header([('Deal', 70), ('Company', 55), ('Activity', 35), ('Close Date', 26)])
    for i, (d, silence, company) in enumerate(data['silent_deals']):
        p = d['properties']
        pdf.tbl_row([
            (p.get('dealname', '-'), 70),
            (company if company != '-' else '-', 55),
            (f'{silence}d silent', 35),
            (pdf_close(d), 26),
        ], shade=(i % 2 == 1))

    pdf.ln(4)
    page_guard()
    pdf.section_title('5 MOST ACTIVE DEALS - RECENTLY ENGAGED')
    pdf.tbl_header([('Deal', 70), ('Company', 55), ('Activity', 35), ('Close Date', 26)])
    for i, (d, silence, company) in enumerate(data['active_deals']):
        p = d['properties']
        pdf.tbl_row([
            (p.get('dealname', '-'), 70),
            (company if company != '-' else '-', 55),
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

    if data['meetings']:
        pdf.ln(4)
        page_guard()
        pdf.section_title(f'MEETINGS TODAY ({stats["meetings_count"]})')
        pdf.tbl_header([('Title', 72), ('Company', 50), ('Time IST', 30), ('Phone', 34)])
        for i, (m, company, phone) in enumerate(data['meetings']):
            p        = m['properties']
            time_str = parse_meeting_time(p.get('hs_meeting_start_time'))
            pdf.tbl_row([
                (p.get('hs_meeting_title', 'Meeting'), 72),
                (company, 50),
                (time_str, 30),
                (phone, 34),
            ], shade=(i % 2 == 1))

    pdf.ln(4)
    page_guard()
    pdf.section_title(f'TOP 10 OPEN TICKETS ({stats["open_tickets_count"]} total)')
    pdf.tbl_header([('Subject', 90), ('Company', 50), ('Priority', 26), ('Age', 20)])
    for i, (t, company) in enumerate(data['open_tickets']):
        p   = t['properties']
        age = days_since(p.get('createdate'))
        pdf.tbl_row([
            (p.get('subject', '-'), 90),
            (company, 50),
            (p.get('hs_ticket_priority', '?'), 26),
            (f'{age}d', 20),
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

    pdf.ln(4)
    page_guard(60)
    pdf.section_title('AI ANALYSIS — WARNING SIGNALS')
    pdf.set_fill_color(248, 250, 255)
    for line in ai_insight.strip().split('\n'):
        s = line.strip()
        if not s:
            pdf.ln(3)
            continue
        if s.startswith('##'):
            title = s.lstrip('#').strip()
            pdf.ln(4)
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(15, 39, 68)
            pdf.multi_cell(0, 6, pdf_safe(f'  {title.upper()}'), fill=True)
        else:
            s_clean = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
            pdf.set_font('Helvetica', '', 9)
            pdf.set_text_color(30, 58, 138)
            pdf.multi_cell(0, 6, pdf_safe(f'  {s_clean}'), fill=True)
        pdf.ln(1)

    return bytes(pdf.output())


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject, html_body, pdf_bytes, date_str):
    msg             = MIMEMultipart('mixed')
    msg['Subject']  = subject
    msg['From']     = GMAIL_ADDRESS
    msg['To']       = RECIPIENT_EMAIL

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
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Fetching HubSpot data...')
    companies, deals, tickets, meetings_today, ticket_company_map, deal_company_map, meeting_company_map, meeting_phone_map = get_data()

    print('Building report data...')
    data = build_report_data(companies, deals, tickets, meetings_today,
                             ticket_company_map, deal_company_map, meeting_company_map, meeting_phone_map)

    print('Building AI context...')
    ai_context = build_ai_context(data, ticket_company_map, deal_company_map, meeting_company_map)

    print('Getting AI analysis...')
    insight = get_ai_insight(ai_context)

    print('Fetching Apollo sequence analytics...')
    apollo_data = get_apollo_data()

    print('Generating HTML and PDF...')
    html = format_html(data, insight, apollo_data)
    pdf  = format_pdf(data, insight, apollo_data)

    print('Sending email...')
    send_email(f"NirogGyan Daily Pulse - {data['date']}", html, pdf, data['date'])
    print('Done. Email sent.')
