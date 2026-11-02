"""Read-only utility portal adapters. Credentials stay in HA options and HTTP memory.

No mobile control, browser-session extraction, payments or account changes.
The HTML contracts were observed on the first-party portals on 2026-11-01.
A changed/ambiguous contract fails closed; never convert a missing balance to zero.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

TNB = 'https://myaccount.mytnb.com.my'
SAJ = 'https://www.ranhillsaj.com.my'
MAX_BODY = 12 * 1024 * 1024


class PortalUnavailable(ValueError):
    pass


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.parts, self.inputs, self.forms, self.buttons, self.scripts = [], {}, [], [], []
        self.account_ids = set(re.findall(r'<div[^>]*class=[\"\'][^\"\']*\bbill-account-id\b[^\"\']*[\"\'][^>]*>\s*([A-Za-z0-9-]+)\s*</div>', html, re.I))
        self.ignore = 0
        self.script = False
        self.feed(html)
        self.text = ' '.join(' '.join(self.parts).split())
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.ignore += 1
            self.script = tag == 'script'
        if tag == 'form': self.forms.append(a)
        if tag == 'input' and a.get('name') and a.get('type', '').lower() == 'hidden':
            self.inputs[a['name']] = a.get('value', '')
        if tag in ('button', 'input') and a.get('onclick'):
            self.buttons.append(a['onclick'])
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignore = max(0, self.ignore - 1)
            self.script = False
    def handle_data(self, data):
        if self.script: self.scripts.append(data)
        if not self.ignore: self.parts.append(data)


def money(value):
    if value is None or not str(value).strip(): return None
    try:
        amount = Decimal(str(value).replace(',', '').replace('RM', '').replace(' ', ''))
    except InvalidOperation:
        raise PortalUnavailable('The portal amount could not be verified.') from None
    if not amount.is_finite() or abs(amount) > 10000000:
        raise PortalUnavailable('The portal amount could not be verified.')
    return f'{amount:.2f}'


def date(value):
    if not value: return None
    for fmt in ('%d-%b-%Y', '%d %b %Y', '%d/%m/%Y', '%Y-%m-%d'):
        try: return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError: pass
    raise PortalUnavailable('The portal bill date could not be verified.')


def _one(text, pattern, required=True):
    hits = set(re.findall(pattern, text, re.I))
    if len(hits) == 1: return hits.pop()
    if not hits and not required: return None
    raise PortalUnavailable('The portal bill layout changed or contains ambiguous accounts. No balance was saved.')


def parse_tnb(html, account):
    p = Page(html)
    if not account or not re.search(r'(?<!\d)' + re.escape(account) + r'(?!\d)', p.text):
        raise PortalUnavailable('The configured TNB account was not verified on the dashboard.')
    # Deliberately support a single linked-account dashboard only until selection is verified.
    if not re.search(r'\b1 Accounts?\b', p.text, re.I):
        raise PortalUnavailable('TNB account selection is ambiguous. The adapter currently requires one linked account.')
    if re.search(r'logged out|session has been inactive|sign in to', p.text, re.I):
        raise PortalUnavailable('TNB login/session expired. Check the HA portal configuration.')
    section = _one(p.text, r'Billing Summary\s+(.*?)\s+Payment History')
    invoice = date(_one(section, r'Bill Date\s+(\d{2}-[A-Za-z]{3}-\d{4})'))
    current = money(_one(section, r'My Current Charges\s+RM\s*([\d,.]+)'))
    rounding = money(_one(section, r'Rounding Adjustment\s+(-?RM\s*[\d,.]+|RM\s*-?[\d,.]+)'))
    # The zero-balance wording is explicit; missing/changed labels are not a zero.
    due = money(_one(section, r"(?:I've cleared all bills|Total Outstanding|Total Amount Due|Amount Due|My Total Bill)\s+RM\s*([\d,.]+)"))
    last_amount = money(_one(p.text, r'LAST PAYMENT AMOUNT\s+RM\s*([\d,.]+)', False))
    last_date = date(_one(p.text, r'LAST PAYMENT DATE\s+(\d{2}-[A-Za-z]{3}-\d{4})', False))
    return {'invoice_date': invoice, 'invoice_amount': money(Decimal(current) + Decimal(rounding)),
            'current_charges': current, 'rounding': rounding, 'outstanding': due, 'due_date': None,
            'last_payment_amount': last_amount, 'last_payment_date': last_date,
            'currency': 'MYR', 'source': 'tnb_portal', 'pdf_supported': True}


def parse_saj(html, history_html, account):
    p, history = Page(html), Page(history_html)
    if not account or p.account_ids != {account} or history.account_ids != {account}:
        raise PortalUnavailable('The configured SAJ account was not verified on both portal pages.')
    # Do not mix amounts from another account card or an unauthenticated page.
    due = money(_one(p.text, r'Amount Due\s*\(RM\)\s*([\d,.]+)'))
    deadline = date(_one(p.text, r'Pay Before\s+(\d{2} [A-Za-z]{3} \d{4})'))
    # SAJ repeats column labels inside responsive table cells. Accept both
    # labelled cells and the compact desktop text while keeping each amount
    # bound to an explicit Bill row, never a payment or adjacent transaction.
    bills = re.findall(
        r'(\d{2}/\d{2}/\d{4})\s+(?:Type of Transaction\s+)?Bill\s+'
        r'(?:Bill No\.\s+)?([A-Za-z0-9-]+)\s+'
        r'(?:Reference No\.\s+)?-\s+(?:Amount\s+)?RM\s*([\d,.]+)',
        history.text,
    )
    if not bills: raise PortalUnavailable('SAJ bill history could not be verified; no monthly bill was saved.')
    bills = [{'invoice_date': date(d), 'bill_reference': ref, 'invoice_amount': money(a)} for d, ref, a in bills]
    bills.sort(key=lambda x:x['invoice_date'], reverse=True)
    payments = re.findall(r'Date\s+(\d{2}/\d{2}/\d{4})\s+Reference No\.\s+\S+\s+Amount\s+RM\s*([\d,.]+)', p.text)
    return {**bills[0], 'outstanding': due, 'due_date': deadline, 'currency':'MYR',
            'last_payment_amount': money(payments[0][1]) if payments else None,
            'last_payment_date': date(payments[0][0]) if payments else None,
            'history': bills, 'source':'saj_portal', 'pdf_supported': False}


class Portal:
    def __init__(self, provider):
        self.provider = provider
        self.hosts = {'www.mytnb.com.my','myaccount.mytnb.com.my'} if provider == 'tnb' else {'www.ranhillsaj.com.my'}
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Alex-Household/0.5 (read-only billing)'
    def close(self): self.session.close()
    def request(self, method, url, **kwargs):
        # Never follow a redirect before checking its origin. No URLs supplied by the model.
        for _ in range(5):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in self.hosts or parsed.username or parsed.password or parsed.port not in (None,443):
                raise PortalUnavailable('The portal returned an untrusted redirect; request stopped.')
            r = None
            try:
                r = self.session.request(method, url, timeout=(8,25), allow_redirects=False, stream=True, **kwargs)
                if r.status_code in (301,302,303,307,308):
                    target = urljoin(url, r.headers.get('Location',''))
                    r.close()
                    if target == url: raise PortalUnavailable('The portal returned an invalid redirect.')
                    if method != 'GET':
                        if r.status_code in (307,308): raise PortalUnavailable('Credential forwarding was refused.')
                        method, kwargs = 'GET', {}
                    url, kwargs = target, {}
                    continue
                if r.status_code != 200:
                    r.close()
                    raise PortalUnavailable('Portal access failed. Check login, connectivity or provider availability in HA; no balance was saved.')
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BODY: raise PortalUnavailable('Portal response exceeded the safe download size.')
                    chunks.append(chunk)
                content = b''.join(chunks)
                r.close()
                return content
            except requests.RequestException:
                raise PortalUnavailable('The portal could not be reached. Try again later; no balance was saved.') from None
            finally:
                if r is not None: r.close()
        raise PortalUnavailable('The portal login redirected too many times.')
    def html(self, method, url, **kwargs):
        return self.request(method,url,**kwargs).decode('utf-8',errors='replace')
    def login(self, email, password):
        if self.provider == 'tnb':
            self.html('GET','https://www.mytnb.com.my/')
            login = Page(self.html('POST','https://www.mytnb.com.my/api/sitecore/Account/Login',data={'Email':email,'Password':password},headers={'Origin':'https://www.mytnb.com.my','Referer':'https://www.mytnb.com.my/'}))
            if 'USERNAME' not in login.inputs:
                raise PortalUnavailable('TNB login needs attention. Check credentials or sign in directly if verification is required.')
            self.html('POST',TNB+'/SSO/SSOHandler',data=login.inputs)
        else:
            page = Page(self.html('GET',SAJ+'/customer/account/login/'))
            forms = [f for f in page.forms if '/customer/account/loginPost' in f.get('action','')]
            if len(forms)!=1 or not page.inputs.get('form_key'):
                raise PortalUnavailable('SAJ login layout changed or requires interactive verification.')
            self.html('POST',urljoin(SAJ,forms[0]['action']),data={'form_key':page.inputs['form_key'],'login[username]':email,'login[password]':password})
    def read(self, account):
        if self.provider == 'tnb':
            html = self.html('GET',TNB+'/AccountManagement/IndividualDashboard')
            return parse_tnb(html,account), html
        html = self.html('GET',SAJ+'/customer/account/')
        history = self.html('GET',SAJ+'/sales/order/history/')
        return parse_saj(html,history,account), html
    def pdf(self, html):
        if self.provider != 'tnb': raise PortalUnavailable('SAJ PDF download is not yet verified. Bill amounts and history remain available.')
        page = Page(html)
        tokens = set(m[0] for b in page.buttons if (m:=re.findall(r"^PrintPDF\('([A-Fa-f0-9]+)'\)$", b)))
        scripts = ' '.join(page.scripts)
        owned = _one(scripts, r'var IsOwnedAccount\s*=\s*"(True|False)"')
        if len(tokens)!=1: raise PortalUnavailable('The TNB bill download is ambiguous; no PDF sent.')
        raw = self.html('POST',TNB+'/AccountManagement/ViewAccountProfile/InitProtectID',data={'selectedAccountID':tokens.pop()+','+owned})
        token = raw.strip().strip('"')
        if not re.fullmatch(r'[A-Za-z0-9+/=_%-]{8,4096}',token):
            raise PortalUnavailable('TNB did not return a valid PDF download reference.')
        content = self.request('GET',TNB+'/AccountManagement/ViewAccountProfile/PrintPDF2',params={'billingNo':token})
        if not content.startswith(b'%PDF-') or len(content)<100:
            raise PortalUnavailable('TNB returned a login/error page instead of a PDF. No attachment sent.')
        return content
