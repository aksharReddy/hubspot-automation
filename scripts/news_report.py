import os
import re
import time
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from fpdf import FPDF, XPos, YPos

HUBSPOT_TOKEN   = os.environ['HUBSPOT_TOKEN']
GROQ_API_KEY    = os.environ['GROQ_API_KEY']
DISCORD_WEBHOOK = os.environ['DISCORD_WEBHOOK']

BASE       = 'https://api.hubapi.com'
HS_HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}'}
NOW           = datetime.now(timezone.utc)
TWO_WEEKS_AGO = NOW - timedelta(days=14)


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
            try:
                pub_dt  = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
                if pub_dt and pub_dt < TWO_WEEKS_AGO:
                    continue
                pub_str = pub_dt.strftime('%d %b %Y') if pub_dt else ''
            except Exception:
                pub_str = ''
            if title:
                items.append({'title': title, 'link': link, 'date': pub_str})
        return items
    except Exception:
        return []


def call_groq(prompt, max_tokens=200):
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


IRRELEVANT_PHRASES = [
    'no connection', 'not directly', 'no relevance', 'no immediate relevance',
    'unaffected', 'lack of relevance', 'does not directly', 'does not intersect',
    'no immediate implication', 'not related', 'no direct', 'unrelated',
]


def analyze_company_news(company_name, news_items):
    headlines = '\n'.join(f'- {n["title"]} ({n["date"]})' for n in news_items)
    prompt = f"""You are a CRM analyst for NirogGyan, a B2B healthcare SaaS company in India.

Company: {company_name}
News headlines:
{headlines}

Rules:
1. If none of these headlines are actually about {company_name} in healthcare, diagnostics, hospitals, or health tech, reply with just: IRRELEVANT
2. Otherwise, write 1-2 sharp sentences on what this means for NirogGyan's relationship with {company_name}. Be direct — no preamble, no explaining what you checked. Just the insight.
3. End with exactly one of: SIGNAL: RISK | SIGNAL: OPPORTUNITY | SIGNAL: NEUTRAL"""

    response = call_groq(prompt)

    if 'IRRELEVANT' in response.upper():
        return None

    signal = 'NEUTRAL'
    for line in response.split('\n'):
        m = re.search(r'SIGNAL:\s*(RISK|OPPORTUNITY|NEUTRAL)', line, re.IGNORECASE)
        if m:
            signal = m.group(1).upper()
            break
    summary = re.sub(r'\n?SIGNAL:.*', '', response, flags=re.IGNORECASE).strip()

    if signal == 'NEUTRAL' and any(p in summary.lower() for p in IRRELEVANT_PHRASES):
        return None

    return {'signal': signal, 'summary': summary}


def get_global_summary(companies_with_news):
    if not companies_with_news:
        return 'No relevant news found for any client companies in the last two weeks.'
    lines = [f'- {c["name"]} [{c["signal"]}]: {c["summary"][:120]}' for c in companies_with_news]
    prompt = f"""NirogGyan is a B2B healthcare SaaS company in India. Recent news about their clients:

{chr(10).join(lines)}

Write 2-3 sentences of the most important cross-company insights — patterns, risks, or opportunities NirogGyan should be aware of. Plain text only."""
    return call_groq(prompt, max_tokens=200)


def pdf_safe(text):
    t = str(text)
    char_map = {'–': '-', '—': '-', '’': "'", '‘': "'", '“': '"', '”': '"'}
    for src, dst in char_map.items():
        t = t.replace(src, dst)
    return t.encode('latin-1', errors='replace').decode('latin-1')


def build_pdf(companies_with_news, companies_without_news, global_summary, date_range):
    pdf = FPDF()
    pdf.set_margins(14, 14, 14)
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # Header bar
    pdf.set_fill_color(15, 39, 68)
    pdf.rect(0, 0, 210, 24, 'F')
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(14, 7)
    pdf.cell(0, 10, 'NirogGyan  |  Client News Brief', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(180, 200, 220)
    pdf.set_xy(14, 17)
    pdf.cell(0, 6, pdf_safe(date_range), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)

    # Stats row
    risk_count = sum(1 for c in companies_with_news if c['signal'] == 'RISK')
    opp_count  = sum(1 for c in companies_with_news if c['signal'] == 'OPPORTUNITY')
    neu_count  = sum(1 for c in companies_with_news if c['signal'] == 'NEUTRAL')
    stats = [
        (str(risk_count),                  'Risk Signals',  (185, 28,  28)),
        (str(opp_count),                   'Opportunities', (22,  101, 52)),
        (str(neu_count),                   'Neutral',       (71,  85,  105)),
        (str(len(companies_without_news)), 'No News',       (15,  39,  68)),
    ]
    col_w  = 45
    row_y  = pdf.get_y()
    for i, (val, label, color) in enumerate(stats):
        x = 14 + i * col_w
        pdf.set_xy(x, row_y)
        pdf.set_font('Helvetica', 'B', 18)
        pdf.set_text_color(*color)
        pdf.cell(col_w, 10, val, align='C')
    pdf.ln(10)
    label_y = pdf.get_y()
    for i, (_, label, _) in enumerate(stats):
        x = 14 + i * col_w
        pdf.set_xy(x, label_y)
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(col_w, 6, label, align='C')
    pdf.ln(12)
    pdf.set_x(14)

    # AI Summary
    W = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_fill_color(248, 250, 255)
    pdf.set_draw_color(199, 217, 245)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(15, 39, 68)
    pdf.set_x(pdf.l_margin)
    pdf.cell(W, 7, '  AI SUMMARY', fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(30, 58, 138)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(W, 6, pdf_safe(f'  {global_summary}'), fill=True)
    pdf.ln(6)

    # Company sections
    signal_labels = {'RISK': '[RISK]', 'OPPORTUNITY': '[OPPORTUNITY]', 'NEUTRAL': '[NEUTRAL]'}
    signal_colors = {
        'RISK':        (185, 28,  28),
        'OPPORTUNITY': (22,  101, 52),
        'NEUTRAL':     (71,  85,  105),
    }

    for c in companies_with_news:
        if pdf.h - pdf.b_margin - pdf.get_y() < 40:
            pdf.add_page()

        color = signal_colors.get(c['signal'], (71, 85, 105))
        label = signal_labels.get(c['signal'], '')

        # Company name + signal badge
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_text_color(15, 39, 68)
        pdf.cell(0, 8, pdf_safe(c['name']), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(*color)
        pdf.cell(0, 5, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Summary
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(30, 41, 59)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(W, 6, pdf_safe(c['summary']))
        pdf.ln(2)

        # News articles
        for n in c['news']:
            pdf.set_font('Helvetica', 'I', 8)
            pdf.set_text_color(26, 86, 160)
            line = pdf_safe(f'  * {n["title"]}')
            if n['date']:
                line += pdf_safe(f'  ({n["date"]})')
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(W, 5, line)

        pdf.ln(6)
        pdf.set_draw_color(241, 245, 249)
        pdf.line(14, pdf.get_y(), 196, pdf.get_y())
        pdf.ln(4)

    # No news section
    if companies_without_news:
        if pdf.h - pdf.b_margin - pdf.get_y() < 30:
            pdf.add_page()
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_fill_color(248, 250, 252)
        pdf.set_text_color(15, 39, 68)
        pdf.set_x(pdf.l_margin)
        pdf.cell(W, 7, '  NO NEWS THIS PERIOD', fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(100, 116, 139)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(W, 6, pdf_safe('  ' + ', '.join(sorted(companies_without_news))))

    # Footer
    pdf.set_y(-14)
    pdf.set_font('Helvetica', 'I', 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 8, pdf_safe(f'NirogGyan Client News Brief  |  {date_range}'), align='C')

    return bytes(pdf.output())


def send_to_discord(webhook_url, pdf_bytes, date_str):
    filename = f'NirogGyan_News_Brief_{date_str.replace(" ", "_")}.pdf'
    message  = f'Here is the CRM News Brief for {date_str}'
    r = requests.post(
        webhook_url,
        data={'payload_json': json.dumps({'content': message})},
        files={'file': (filename, pdf_bytes, 'application/pdf')}
    )
    return r.status_code


if __name__ == '__main__':
    print('Fetching customer companies from HubSpot...')
    companies = fetch_customer_companies()
    print(f'Found {len(companies)} customer companies\n')

    companies_with_news    = []
    companies_without_news = []

    for i, name in enumerate(companies):
        print(f'[{i+1}/{len(companies)}] {name}', end=' ... ', flush=True)
        news = fetch_news(name)
        if news:
            analysis = analyze_company_news(name, news)
            if analysis is None:
                print('irrelevant, skipped')
                companies_without_news.append(name)
            else:
                companies_with_news.append({'name': name, 'news': news, **analysis})
                print(analysis['signal'])
        else:
            print('no news')
            companies_without_news.append(name)
        time.sleep(0.5)

    signal_order = {'RISK': 0, 'OPPORTUNITY': 1, 'NEUTRAL': 2}
    companies_with_news.sort(key=lambda x: signal_order.get(x['signal'], 3))

    print('\nGenerating global AI summary...')
    global_summary = get_global_summary(companies_with_news)

    date_range = f'{TWO_WEEKS_AGO.strftime("%d %b")} - {NOW.strftime("%d %b %Y")}'
    date_str   = NOW.strftime('%d %b %Y')

    print('Building PDF...')
    pdf_bytes = build_pdf(companies_with_news, companies_without_news, global_summary, date_range)

    print('Sending to Discord...')
    status = send_to_discord(DISCORD_WEBHOOK, pdf_bytes, date_str)
    print(f'Done. Discord status: {status}')
