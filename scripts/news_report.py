import os
import re
import time
import requests
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote_plus

HUBSPOT_TOKEN      = os.environ['HUBSPOT_TOKEN']
GROQ_API_KEY       = os.environ['GROQ_API_KEY']
GMAIL_ADDRESS      = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']
RECIPIENT_EMAIL    = os.environ['RECIPIENT_EMAIL']

BASE       = 'https://api.hubapi.com'
HS_HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}'}
NOW            = datetime.now(timezone.utc)
TWO_WEEKS_AGO  = NOW - timedelta(days=14)


def fetch_customer_companies():
    companies, after = [], None
    while True:
        body = {
            'filterGroups': [{'filters': [{'propertyName': 'lifecyclestage', 'operator': 'EQ', 'value': 'customer'}]}],
            'properties': ['name'],
            'limit': 100,
        }
        if after:
            body['after'] = after
        r = requests.post(
            f'{BASE}/crm/v3/objects/companies/search',
            headers={**HS_HEADERS, 'Content-Type': 'application/json'},
            json=body
        )
        data = r.json()
        companies.extend(data.get('results', []))
        after = data.get('paging', {}).get('next', {}).get('after')
        if not after:
            break
    return [c['properties']['name'] for c in companies if c['properties'].get('name')]


def fetch_news(company_name):
    try:
        q   = quote_plus(f'"{company_name}"')
        url = f'https://news.google.com/rss/search?q={q}&hl=en&gl=IN&ceid=IN:en'
        r   = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return []
        root  = ET.fromstring(r.content)
        items = []
        for item in root.findall('.//item')[:5]:
            title = item.findtext('title') or ''
            link  = item.findtext('link') or ''
            pub   = item.findtext('pubDate') or ''
            desc  = re.sub(r'<[^>]+>', '', item.findtext('description') or '').strip()
            try:
                pub_dt  = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
                if pub_dt and pub_dt < TWO_WEEKS_AGO:
                    continue
                pub_str = pub_dt.strftime('%d %b %Y') if pub_dt else ''
            except Exception:
                pub_str = ''
            if title:
                items.append({'title': title, 'link': link, 'date': pub_str, 'snippet': desc[:200]})
        return items
    except Exception:
        return []


def call_groq(prompt, max_tokens=350):
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


def analyze_company_news(company_name, news_items):
    headlines = '\n'.join(f'- {n["title"]} ({n["date"]})' for n in news_items)
    prompt = f"""You are analyzing news about one of NirogGyan's clients. NirogGyan is a B2B healthcare SaaS company in India.

Company: {company_name}
Recent news (last 2 weeks):
{headlines}

In 2-3 sentences, summarize what this news means for NirogGyan's relationship with this company. Be specific and practical.
Then on a new line write exactly one of: SIGNAL: RISK  or  SIGNAL: OPPORTUNITY  or  SIGNAL: NEUTRAL

Plain text only, no bullet points, no markdown."""

    response = call_groq(prompt)
    signal   = 'NEUTRAL'
    for line in response.split('\n'):
        m = re.search(r'SIGNAL:\s*(RISK|OPPORTUNITY|NEUTRAL)', line, re.IGNORECASE)
        if m:
            signal = m.group(1).upper()
            break
    summary = re.sub(r'\n?SIGNAL:.*', '', response, flags=re.IGNORECASE).strip()
    return {'signal': signal, 'summary': summary}


def get_global_summary(companies_with_news):
    if not companies_with_news:
        return 'No news found for any client companies in the last two weeks.'
    lines = [f'- {c["name"]} [{c["signal"]}]: {c["summary"][:120]}' for c in companies_with_news]
    prompt = f"""NirogGyan is a B2B healthcare SaaS company in India. Here is a summary of recent news about their clients:

{chr(10).join(lines)}

Write 2-3 sentences of the most important cross-company insights — patterns, risks, or opportunities NirogGyan should be aware of across their client base. Plain text only."""
    return call_groq(prompt, max_tokens=200)


def format_html(companies_with_news, companies_without_news, global_summary, date_range):
    signal_style = {
        'RISK':        ('#fef2f2', '#fca5a5', '#b91c1c', '#fee2e2'),
        'OPPORTUNITY': ('#f0fdf4', '#86efac', '#166534', '#dcfce7'),
        'NEUTRAL':     ('#f8fafc', '#cbd5e1', '#475569', '#f1f5f9'),
    }

    company_cards = ''
    for c in companies_with_news:
        bg, border_color, text_color, badge_bg = signal_style.get(c['signal'], signal_style['NEUTRAL'])
        news_items_html = ''
        for n in c['news']:
            news_items_html += (
                f'<div style="padding:7px 0;border-bottom:1px solid #f1f5f9;">'
                f'<a href="{n["link"]}" style="font-size:13px;color:#1a56a0;text-decoration:none;font-weight:500;">{n["title"]}</a>'
                f'<span style="font-size:11px;color:#94a3b8;margin-left:8px;">{n["date"]}</span>'
                f'</div>'
            )
        company_cards += f'''
<div style="border:1px solid {border_color};border-left:4px solid {text_color};border-radius:8px;margin-bottom:14px;background:{bg};">
  <div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center;">
    <span style="font-size:14px;font-weight:700;color:#0f2744;">{c["name"]}</span>
    <span style="display:inline-block;padding:3px 12px;border-radius:12px;background:{badge_bg};color:{text_color};font-size:11px;font-weight:700;letter-spacing:0.5px;">{c["signal"]}</span>
  </div>
  <div style="padding:0 16px 12px;">
    <p style="margin:0 0 10px;font-size:13px;color:#1e3a5f;line-height:1.8;">{c["summary"]}</p>
    <div style="background:white;border-radius:6px;padding:4px 12px;">{news_items_html}</div>
  </div>
</div>'''

    no_news_section = ''
    if companies_without_news:
        names = ', '.join(sorted(companies_without_news))
        no_news_section = f'''
  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>
  <tr><td style="background:#fff;padding:20px 32px 24px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px;">&#9632; No News This Period</div>
    <p style="font-size:13px;color:#94a3b8;margin:0;line-height:1.9;">{names}</p>
  </td></tr>'''

    risk_count = sum(1 for c in companies_with_news if c['signal'] == 'RISK')
    opp_count  = sum(1 for c in companies_with_news if c['signal'] == 'OPPORTUNITY')
    neu_count  = sum(1 for c in companies_with_news if c['signal'] == 'NEUTRAL')

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

  <tr><td style="background:linear-gradient(135deg,#0f2744,#1a56a0);border-radius:12px 12px 0 0;padding:28px 32px;">
    <div style="color:white;font-size:22px;font-weight:700;letter-spacing:-0.5px;">NirogGyan Client News Brief</div>
    <div style="color:rgba(255,255,255,0.65);font-size:13px;margin-top:4px;">{date_range} &nbsp;|&nbsp; {len(companies_with_news)} of {len(companies_with_news) + len(companies_without_news)} clients in the news</div>
  </td></tr>

  <tr><td style="background:#fff;padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#b91c1c;">{risk_count}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Risk Signals</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#166534;">{opp_count}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Opportunities</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;border-right:1px solid #f1f5f9;">
        <div style="font-size:28px;font-weight:700;color:#475569;">{neu_count}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Neutral</div>
      </td>
      <td width="25%" style="padding:20px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#0f2744;">{len(companies_without_news)}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">No News</div>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:16px 32px;">
    <div style="background:#f8faff;border:1px solid #c7d9f5;border-left:4px solid #1a56a0;border-radius:0 10px 10px 0;padding:18px 22px;">
      <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px;">&#9733; AI Summary</div>
      <p style="margin:0;font-size:13px;color:#1e3a5f;line-height:1.8;">{global_summary}</p>
    </div>
  </td></tr>

  <tr><td style="background:#fff;padding:0 32px;"><hr style="border:none;border-top:1px solid #f1f5f9;margin:0;"></td></tr>

  <tr><td style="background:#fff;padding:24px 32px;">
    <div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:16px;">&#9632; Client News — Risks First</div>
    {company_cards if company_cards else '<p style="font-size:13px;color:#94a3b8;">No client news found in the last two weeks.</p>'}
  </td></tr>

  {no_news_section}

  <tr><td style="background:#0f2744;border-radius:0 0 12px 12px;padding:16px 32px;text-align:center;">
    <div style="color:rgba(255,255,255,0.5);font-size:11px;">NirogGyan &nbsp;|&nbsp; Bi-weekly client news brief &nbsp;|&nbsp; {date_range}</div>
  </td></tr>

</table>
</body>
</html>'''


def send_email(subject, html_body):
    msg            = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())


if __name__ == '__main__':
    print('Fetching customer companies from HubSpot...')
    companies = fetch_customer_companies()
    print(f'Found {len(companies)} customer companies\n')

    companies_with_news    = []
    companies_without_news = []

    for i, name in enumerate(companies):
        print(f'[{i+1}/{len(companies)}] {name}')
        news = fetch_news(name)
        if news:
            print(f'  {len(news)} articles — analyzing...')
            analysis = analyze_company_news(name, news)
            companies_with_news.append({'name': name, 'news': news, **analysis})
            print(f'  Signal: {analysis["signal"]}')
        else:
            print(f'  No news')
            companies_without_news.append(name)
        time.sleep(0.5)

    signal_order = {'RISK': 0, 'OPPORTUNITY': 1, 'NEUTRAL': 2}
    companies_with_news.sort(key=lambda x: signal_order.get(x['signal'], 3))

    print('\nGenerating global AI summary...')
    global_summary = get_global_summary(companies_with_news)

    date_range = f'{TWO_WEEKS_AGO.strftime("%d %b")} – {NOW.strftime("%d %b %Y")}'
    subject    = f'NirogGyan Client News Brief — {NOW.strftime("%d %b %Y")}'

    print('Building HTML and sending email...')
    html = format_html(companies_with_news, companies_without_news, global_summary, date_range)
    send_email(subject, html)
    print('Done. Email sent.')
