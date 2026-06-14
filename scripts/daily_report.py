import re
import requests
import smtplib
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from fpdf import FPDF, XPos, YPos


def pdf_safe(text):
    t = str(text)
    char_map = {
        '-': '-', '-': '-',
        ''': "'", ''': "'",
        '"': '"', '"': '"',
    }
    for src, dst in char_map.items():
        t = t.replace(src, dst)
    return t.encode('latin-1', errors='replace').decode('latin-1')


def strip_markdown(text):
    """Remove markdown symbols the AI might emit."""
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

HUBSPOT_HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}'}
BASE = 'https://api.hubapi.com'
NOW  = datetime.now(timezone.utc)


# ── HubSpot helpers ──────────────────────────────────────────────────────────

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


def days_since(date_str):
    if not date_str:
        return 9999
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return (NOW - dt).days
    except Exception:
        return 9999


def fetch_ticket_company_map(tickets):
    """Calls HubSpot associations API to link each ticket to its company.
    Returns dict: company_id (str) -> list of open ticket objects."""
    if not tickets:
        return {}

    ticket_by_id = {t['id']: t for t in tickets}
    company_to_tickets = {}

    ids = list(ticket_by_id.keys())
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        r = requests.post(
            f'{BASE}/crm/v3/associations/tickets/companies/batch/read',
            headers={**HUBSPOT_HEADERS, 'Content-Type': 'application/json'},
            json={'inputs': [{'id': tid} for tid in batch]}
        )
        if r.status_code != 200:
            continue
        for result in r.json().get('results', []):
            ticket_obj = ticket_by_id.get(result['from']['id'])
            if not ticket_obj:
                continue
            for assoc in result.get('to', []):
                cid = str(assoc['id'])
                company_to_tickets.setdefault(cid, []).append(ticket_obj)

    return company_to_tickets


# ── Fetch data ────────────────────────────────────────────────────────────────

def get_data():
    companies = fetch_all('companies', [
        'name', 'lifecyclestage', 'notes_last_updated', 'notes_last_contacted',
        'hs_last_logged_call_date', 'city', 'createdate'
    ])
    tickets = fetch_all('tickets', [
        'subject', 'hs_ticket_priority', 'hs_pipeline_stage', 'createdate'
    ])
    deals = fetch_all('deals', [
        'dealname', 'dealstage', 'createdate', 'hs_is_closed_won',
        'hs_closed_won_date', 'amount'
    ])
    ticket_company_map = fetch_ticket_company_map(tickets)
    return companies, tickets, deals, ticket_company_map


# ── Build report data (for HTML/PDF layout) ───────────────────────────────────

def build_report_data(companies, tickets, deals):
    def last_activity(c):
        p = c['properties']
        return min(days_since(p.get('notes_last_updated')),
                   days_since(p.get('notes_last_contacted')))

    customers     = [c for c in companies if c['properties'].get('lifecyclestage') == 'customer']
    opportunities = [c for c in companies if c['properties'].get('lifecyclestage') == 'opportunity']
    mqls          = [c for c in companies if c['properties'].get('lifecyclestage') == 'marketingqualifiedlead']

    c_healthy  = [c for c in customers if last_activity(c) < 14]
    c_at_risk  = [c for c in customers if 14 <= last_activity(c) < 30]
    c_critical = sorted([c for c in customers if last_activity(c) >= 30], key=last_activity, reverse=True)

    o_active = [c for c in opportunities if last_activity(c) < 14]
    o_follow = [c for c in opportunities if 14 <= last_activity(c) < 30]
    o_silent = sorted([c for c in opportunities if last_activity(c) >= 30], key=last_activity, reverse=True)

    new_leads  = [c for c in companies
                  if days_since(c['properties'].get('createdate')) <= 7
                  and c['properties'].get('lifecyclestage') == 'lead']
    new_deals  = [d for d in deals
                  if days_since(d['properties'].get('createdate')) <= 30
                  and not d['properties'].get('hs_is_closed_won')]
    closed_won = [d for d in deals
                  if d['properties'].get('hs_is_closed_won') == 'true'
                  and days_since(d['properties'].get('hs_closed_won_date')) <= 30]

    open_tickets = sorted(
        [t for t in tickets if t['properties'].get('hs_pipeline_stage') != '4'],
        key=lambda t: days_since(t['properties'].get('createdate')), reverse=True
    )
    high_open   = [t for t in open_tickets if t['properties'].get('hs_ticket_priority') == 'HIGH']
    medium_open = [t for t in open_tickets if t['properties'].get('hs_ticket_priority') == 'MEDIUM']

    return {
        'date': NOW.strftime('%d %B %Y'),
        'customers': {
            'total': len(customers), 'healthy': len(c_healthy),
            'at_risk': len(c_at_risk), 'critical': len(c_critical),
            'critical_list': c_critical[:6], 'last_activity': last_activity
        },
        'pipeline': {
            'total_opps': len(opportunities), 'active': len(o_active),
            'follow_up': len(o_follow), 'silent': len(o_silent),
            'hot_list': o_silent[:5], 'mqls': len(mqls),
            'new_leads': len(new_leads), 'new_deals': len(new_deals),
            'closed_won': len(closed_won), 'closed_won_list': closed_won,
            'last_activity': last_activity
        },
        'tickets': {
            'open': len(open_tickets), 'high': len(high_open),
            'medium': len(medium_open), 'list': open_tickets[:5]
        }
    }


# ── Build rich AI context with cross-object signals ───────────────────────────

def build_ai_context(companies, tickets, deals, ticket_company_map):
    def last_activity(c):
        p = c['properties']
        return min(days_since(p.get('notes_last_updated')),
                   days_since(p.get('notes_last_contacted')))

    customers  = sorted(
        [c for c in companies if c['properties'].get('lifecyclestage') == 'customer'],
        key=last_activity, reverse=True
    )
    silent_opps = sorted(
        [c for c in companies
         if c['properties'].get('lifecyclestage') == 'opportunity' and last_activity(c) >= 30],
        key=last_activity, reverse=True
    )[:20]

    lines = [
        'You are analyzing live CRM data for NirogGyan, a B2B healthcare SaaS company in India.',
        'NirogGyan sells diagnostic reporting software to hospitals, labs, and clinics.',
        '',
        '--- LIVE CUSTOMERS WITH ENGAGEMENT SIGNALS ---',
    ]

    for c in customers:
        p    = c['properties']
        name = p.get('name', '-')
        days = last_activity(c)
        call = (p.get('hs_last_logged_call_date') or '')[:10] or 'never called'
        cid  = str(c['id'])

        open_tix = [
            t for t in ticket_company_map.get(cid, [])
            if t['properties'].get('hs_pipeline_stage') != '4'
        ]
        ticket_str = ''
        if open_tix:
            parts = []
            for t in open_tix:
                tp  = t['properties']
                pri = tp.get('hs_ticket_priority', '?')
                age = days_since(tp.get('createdate'))
                subj = tp.get('subject', '?')
                parts.append(f'open ticket: "{subj}" [{pri}, {age}d old]')
            ticket_str = ' | ' + ' + '.join(parts)

        status = 'healthy' if days < 14 else ('at-risk' if days < 30 else 'CRITICAL')
        lines.append(f'- {name}: {days}d silent | last call: {call} | {status}{ticket_str}')

    lines += ['', '--- SALES OPPORTUNITIES SILENT 30d+ ---']
    for c in silent_opps:
        p    = c['properties']
        name = p.get('name', '-')
        days = last_activity(c)
        call = (p.get('hs_last_logged_call_date') or '')[:10] or 'never called'
        lines.append(f'- {name}: {days}d silent | last call: {call}')

    lines += [
        '',
        '--- WHAT I NEED FROM YOU ---',
        '',
        'IMPORTANT FORMATTING RULES:',
        '- Plain text only. No markdown. No **, no ##, no ###, no *, no backticks.',
        '- Each section max 4 lines. Be punchy and direct.',
        '- Use account names. No generic statements.',
        '',
        '1. CHURN RISK ANALYSIS',
        'Top 3 customers most likely to churn. One line each: account name, why they are at risk (combine all signals), what to do.',
        '',
        '2. CROSS-SIGNAL ALERTS',
        'Accounts where multiple bad signals hit at once (silent + open ticket + never called). One line per account. Say what makes it dangerous.',
        '',
        '3. ONE PATTERN',
        'One non-obvious thing you see across all accounts. Two sentences max.',
    ]

    return '\n'.join(lines)


# ── AI analysis via Groq ──────────────────────────────────────────────────────

def get_ai_insight(context):
    try:
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}',
                     'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [{'role': 'user', 'content': context}],
                'max_tokens': 450
            }
        )
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f'AI analysis unavailable: {e}'


# ── HTML email ────────────────────────────────────────────────────────────────

def format_html(data, ai_insight):
    cus = data['customers']
    pip = data['pipeline']
    tix = data['tickets']
    la  = cus['last_activity']

    def badge(text, color):
        colors = {
            'red':    ('fee2e2', 'b91c1c'),
            'yellow': ('fef3c7', '92400e'),
            'green':  ('dcfce7', '166534'),
        }
        bg, fg = colors.get(color, ('f1f5f9', '475569'))
        return (f'<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
                f'background:#{bg};color:#{fg};font-size:11px;font-weight:700;">{text}</span>')

    critical_rows = ''
    for c in cus['critical_list']:
        p    = c['properties']
        days = la(c)
        call = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never called'
        col  = 'red' if days >= 60 else 'yellow'
        critical_rows += (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #fef2f2;font-size:13px;color:#1e293b;font-weight:500;">{p.get("name","-")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #fef2f2;text-align:center;">{badge(f"{days}d silent", col)}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #fef2f2;font-size:12px;color:#64748b;">{call}</td>'
            f'</tr>'
        )

    la_pip   = pip['last_activity']
    hot_rows = ''
    for c in pip['hot_list']:
        p    = c['properties']
        days = la_pip(c)
        call = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never called'
        hot_rows += (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:13px;color:#1e293b;font-weight:500;">{p.get("name","-")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;text-align:center;">{badge(f"{days}d silent", "yellow")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f0f9ff;font-size:12px;color:#64748b;">{call}</td>'
            f'</tr>'
        )

    ticket_rows = ''
    for t in tix['list']:
        p   = t['properties']
        age = days_since(p.get('createdate'))
        pri = p.get('hs_ticket_priority', '?')
        col = 'red' if pri == 'HIGH' else 'yellow'
        ticket_rows += (
            f'<tr>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:13px;color:#1e293b;">{p.get("subject","-")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;text-align:center;">{badge(pri, col)}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #f8fafc;font-size:12px;color:#64748b;text-align:center;">{age}d</td>'
            f'</tr>'
        )

    closed_html = ''
    for d in pip['closed_won_list']:
        closed_html += f'<li style="margin:4px 0;font-size:13px;color:#166534;">&#10003; {d["properties"].get("dealname","-")}</li>'
    if closed_html:
        closed_html = f'<ul style="margin:10px 0 0;padding-left:20px;">{closed_html}</ul>'

    def stat_row(label, val):
        return (
            f'<div style="display:flex;justify-content:space-between;padding:7px 0;'
            f'border-bottom:1px solid #f1f5f9;font-size:13px;">'
            f'<span style="color:#475569;">{label}</span>'
            f'<span style="font-weight:700;color:#0f2744;">{val}</span></div>'
        )

    pip_left  = ''.join([stat_row(*x) for x in [
        ('Active (last 14d)', pip['active']),
        ('Follow up (14-30d)', pip['follow_up']),
        ('Silent 30d+', pip['silent']),
    ]])
    pip_right = ''.join([stat_row(*x) for x in [
        ('New leads (7d)', pip['new_leads']),
        ('New deals (30d)', pip['new_deals']),
        ('Closed won (30d)', pip['closed_won']),
    ]])

    # Strip markdown then render AI response
    ai_clean = strip_markdown(ai_insight)
    ai_html  = ''
    for line in ai_clean.strip().split('\n'):
        s = line.strip()
        if not s:
            ai_html += '<div style="height:8px;"></div>'
        elif s[:2] in ('1.', '2.', '3.'):
            ai_html += (f'<p style="margin:14px 0 5px;font-size:12px;font-weight:700;'
                        f'color:#0f2744;text-transform:uppercase;letter-spacing:0.5px;">{s}</p>')
        elif s.startswith('-') or s.startswith('-'):
            ai_html += (f'<p style="margin:3px 0 3px 12px;font-size:13px;color:#1e3a5f;line-height:1.6;">'
                        f'{s}</p>')
        else:
            ai_html += f'<p style="margin:3px 0;font-size:13px;color:#1e3a5f;line-height:1.6;">{s}</p>'

    critical_block = ''
    if cus['critical_list']:
        critical_block = (
            f'<div style="margin-top:18px;">'
            f'<div style="font-size:12px;color:#64748b;margin-bottom:8px;font-weight:600;">ACCOUNTS NEEDING A CALL</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #fecaca;border-radius:8px;overflow:hidden;">'
            f'<tr style="background:#fef2f2;">'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Account</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Silence</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Last Call</th>'
            f'</tr>{critical_rows}</table></div>'
        )

    hot_block = ''
    if pip['hot_list']:
        hot_block = (
            f'<div style="margin-top:18px;">'
            f'<div style="font-size:12px;color:#64748b;margin-bottom:8px;font-weight:600;">OPPORTUNITIES TO CHASE TODAY</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #bfdbfe;border-radius:8px;overflow:hidden;">'
            f'<tr style="background:#eff6ff;">'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Company</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Silence</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Last Call</th>'
            f'</tr>{hot_rows}</table></div>'
        )

    ticket_table = ''
    if tix['list']:
        ticket_table = (
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">'
            f'<tr style="background:#f8fafc;">'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:left;font-weight:600;">Subject</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Priority</th>'
            f'<th style="padding:8px 14px;font-size:11px;color:#64748b;text-align:center;font-weight:600;">Age</th>'
            f'</tr>{ticket_rows}</table>'
        )

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;">

  <tr><td style="background:linear-gradient(135deg,#0f2744,#1a56a0);border-radius:12px 12px 0 0;padding:28px 32px;">
    <div style="color:white;font-size:22px;font-weight:700;letter-spacing:-0.5px;">NirogGyan Daily Pulse</div>
    <div style="color:rgba(255,255,255,0.65);font-size:13px;margin-top:4px;">{data['date']} &nbsp;|&nbsp; Auto-generated report</div>
  </td></tr>

  <tr><td style="background:#fff;padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="33%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:30px;font-weight:700;color:#0f2744;">{cus['total']}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Live Clients</div>
      </td>
      <td width="33%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:30px;font-weight:700;color:#0f2744;">{pip['total_opps']}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Opportunities</div>
      </td>
      <td width="33%" style="padding:20px;text-align:center;">
        <div style="font-size:30px;font-weight:700;color:#0f2744;">{tix['open']}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Open Tickets</div>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Client Health</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:8px 14px;background:#f0fdf4;border-radius:8px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-size:13px;color:#15803d;">&#11044; Healthy &lt;14 days</td>
          <td style="text-align:right;font-size:18px;font-weight:700;color:#15803d;">{cus['healthy']}</td>
        </tr></table>
      </td></tr>
      <tr><td style="height:6px;"></td></tr>
      <tr><td style="padding:8px 14px;background:#fffbeb;border-radius:8px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-size:13px;color:#b45309;">&#11044; At Risk 14-30 days</td>
          <td style="text-align:right;font-size:18px;font-weight:700;color:#b45309;">{cus['at_risk']}</td>
        </tr></table>
      </td></tr>
      <tr><td style="height:6px;"></td></tr>
      <tr><td style="padding:8px 14px;background:#fef2f2;border-radius:8px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-size:13px;color:#dc2626;">&#11044; Critical 30+ days</td>
          <td style="text-align:right;font-size:18px;font-weight:700;color:#dc2626;">{cus['critical']}</td>
        </tr></table>
      </td></tr>
    </table>
    {critical_block}
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Sales Pipeline</div>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="width:50%;padding-right:10px;vertical-align:top;">{pip_left}</td>
      <td style="width:50%;padding-left:10px;vertical-align:top;">{pip_right}</td>
    </tr></table>
    {closed_html}{hot_block}
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">&#9632; Open Support Tickets ({tix['open']})</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;"><tr>
      <td style="padding:10px 14px;background:#fef2f2;border-radius:6px;text-align:center;width:50%;">
        <span style="font-size:20px;font-weight:700;color:#dc2626;">{tix['high']}</span>
        <span style="font-size:11px;color:#64748b;margin-left:6px;">HIGH</span>
      </td>
      <td style="width:12px;"></td>
      <td style="padding:10px 14px;background:#fffbeb;border-radius:6px;text-align:center;width:50%;">
        <span style="font-size:20px;font-weight:700;color:#b45309;">{tix['medium']}</span>
        <span style="font-size:11px;color:#64748b;margin-left:6px;">MEDIUM</span>
      </td>
    </tr></table>
    {ticket_table}
  </td></tr>

  <tr><td style="background:#fff;padding:8px 32px 24px;">
    <div style="background:#f8faff;border:1px solid #c7d9f5;border-left:4px solid #1a56a0;border-radius:0 10px 10px 0;padding:20px 22px;">
      <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;">&#9733; AI Analysis</div>
      {ai_html}
    </div>
  </td></tr>

  <tr><td style="background:#0f2744;border-radius:0 0 12px 12px;padding:16px 32px;text-align:center;">
    <div style="color:rgba(255,255,255,0.5);font-size:11px;">NirogGyan &nbsp;|&nbsp; Automated daily report &nbsp;|&nbsp; {data['date']}</div>
  </td></tr>

</table>
</body>
</html>'''


# ── PDF report ────────────────────────────────────────────────────────────────

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
        self.cell(0, 8, f'NirogGyan Daily Pulse - {NOW.strftime("%d %B %Y")}', align='C')

    def section_title(self, title):
        self.ln(4)
        self.set_fill_color(240, 244, 248)
        self.set_font('Helvetica', 'B', 9)
        self.set_text_color(15, 39, 68)
        self.cell(0, 8, pdf_safe(f'  {title}'), new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
        self.ln(2)

    def kv_row(self, label, value, highlight=False):
        fill = highlight
        if highlight:
            self.set_fill_color(254, 242, 242)
        self.set_font('Helvetica', '', 10)
        self.set_text_color(71, 85, 105)
        self.cell(110, 7, pdf_safe(f'  {label}'), fill=fill)
        self.set_font('Helvetica', 'B', 10)
        self.set_text_color(15, 39, 68)
        self.cell(0, 7, pdf_safe(str(value)), new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=fill)

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
            self.cell(w, 7, pdf_safe(str(text)[:55]), fill=shade)
        self.ln()


def format_pdf(data, ai_insight):
    cus    = data['customers']
    pip    = data['pipeline']
    tix    = data['tickets']
    la     = cus['last_activity']
    la_pip = pip['last_activity']

    pdf = PulsePDF()
    pdf.set_margins(12, 28, 12)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(0, 7, pdf_safe(data['date']), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_fill_color(15, 39, 68)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(255, 255, 255)
    for label, val in [('Live Clients', cus['total']),
                       ('Opportunities', pip['total_opps']),
                       ('Open Tickets', tix['open'])]:
        pdf.cell(62, 12, pdf_safe(f'{val}  {label}'), fill=True, align='C')
    pdf.ln(14)

    pdf.section_title('CLIENT HEALTH')
    pdf.kv_row('Healthy  (< 14 days)', cus['healthy'])
    pdf.kv_row('At Risk  (14-30 days)', cus['at_risk'])
    pdf.kv_row('Critical (30+ days)', cus['critical'], highlight=(cus['critical'] > 0))

    def page_guard(min_mm=45):
        if pdf.h - pdf.b_margin - pdf.get_y() < min_mm:
            pdf.add_page()

    if cus['critical_list']:
        pdf.ln(4)
        page_guard(50)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 6, '  ACCOUNTS NEEDING A CALL', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.tbl_header([('Account', 90), ('Days Silent', 40), ('Last Call', 56)])
        for i, c in enumerate(cus['critical_list']):
            p    = c['properties']
            days = la(c)
            call = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never called'
            pdf.tbl_row([(p.get('name', '-'), 90), (f'{days}d', 40), (call, 56)], shade=(i % 2 == 1))

    pdf.ln(4)
    pdf.section_title('SALES PIPELINE')
    for label, val in [('Active (last 14d)', pip['active']),
                       ('Follow up (14-30d)', pip['follow_up']),
                       ('Silent 30d+', pip['silent']),
                       ('New leads (7d)', pip['new_leads']),
                       ('New deals (30d)', pip['new_deals']),
                       ('Closed won (30d)', pip['closed_won'])]:
        pdf.kv_row(label, val)

    if pip['hot_list']:
        pdf.ln(4)
        page_guard(50)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(0, 6, '  OPPORTUNITIES TO CHASE TODAY', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.tbl_header([('Company', 90), ('Days Silent', 40), ('Last Call', 56)])
        for i, c in enumerate(pip['hot_list']):
            p    = c['properties']
            days = la_pip(c)
            call = (p.get('hs_last_logged_call_date') or '')[:10] or 'Never called'
            pdf.tbl_row([(p.get('name', '-'), 90), (f'{days}d', 40), (call, 56)], shade=(i % 2 == 1))

    pdf.ln(4)
    pdf.section_title(f'OPEN SUPPORT TICKETS ({tix["open"]})')
    pdf.kv_row('HIGH priority', tix['high'], highlight=(tix['high'] > 0))
    pdf.kv_row('MEDIUM priority', tix['medium'])

    if tix['list']:
        pdf.ln(4)
        page_guard(50)
        pdf.tbl_header([('Subject', 110), ('Priority', 35), ('Age', 41)])
        for i, t in enumerate(tix['list']):
            p   = t['properties']
            age = days_since(p.get('createdate'))
            pdf.tbl_row([(p.get('subject', '-'), 110),
                         (p.get('hs_ticket_priority', '?'), 35),
                         (f'{age}d', 41)], shade=(i % 2 == 1))

    pdf.ln(4)
    page_guard(60)
    pdf.section_title('AI ANALYSIS')
    pdf.set_fill_color(248, 250, 255)
    ai_clean = strip_markdown(ai_insight)
    for line in ai_clean.strip().split('\n'):
        s = line.strip()
        if not s:
            pdf.ln(3)
            continue
        is_heading = s[:2] in ('1.', '2.', '3.')
        if is_heading:
            pdf.ln(4)
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(15, 39, 68)
        elif s.startswith('-') or s.startswith('-'):
            pdf.set_font('Helvetica', '', 9)
            pdf.set_text_color(30, 58, 138)
        else:
            pdf.set_font('Helvetica', '', 9)
            pdf.set_text_color(30, 58, 138)
        pdf.multi_cell(0, 6, pdf_safe(f'  {s}'), fill=True)
        pdf.ln(1)

    return bytes(pdf.output())


# ── Send email with HTML body + PDF attachment ────────────────────────────────

def send_email(subject, html_body, pdf_bytes, date_str):
    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = RECIPIENT_EMAIL

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
    print('Fetching HubSpot data + ticket-company associations...')
    companies, tickets, deals, ticket_company_map = get_data()

    print('Building report data...')
    data = build_report_data(companies, tickets, deals)

    print('Building AI context (per-account with cross-object signals)...')
    ai_context = build_ai_context(companies, tickets, deals, ticket_company_map)

    print('Getting AI analysis...')
    insight = get_ai_insight(ai_context)

    print('Generating HTML and PDF...')
    html = format_html(data, insight)
    pdf  = format_pdf(data, insight)

    print('Sending email...')
    send_email(f"NirogGyan Daily Pulse - {data['date']}", html, pdf, data['date'])
    print('Done. Email sent with PDF attachment.')
