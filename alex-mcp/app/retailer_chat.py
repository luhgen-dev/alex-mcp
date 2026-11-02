"""Bounded public-catalogue conversation context and evidence-only replies.

History chooses a retailer, never a price, identity or write permission. The
caller supplies recent same-actor/chat turns and retrieves fresh catalogue data.
"""
from __future__ import annotations

import html
import re

TOOLS = {
    'search_mydin_prices', 'get_mydin_product_price',
    'search_manjaku_prices', 'get_manjaku_product_price',
}
UNRELATED_READS = {'query_finances', 'search_saved_items', 'get_saved_item',
                   'list_shopping_items', 'list_reminders', 'get_agenda_range'}
_OTHER = re.compile(
    r"\b(?:spent|spend|spending|paid|payment|bought|purchased|expense|expenses|receipt|receipts|"
    r"budget|salary|income|monthly|month|diary|agenda|remind|reminder|reminders|task|tasks|"
    r"goals?|roster|shift|work|weather|tomorrow|yesterday|bill|bills|tnb|saj|digi|"
    r"bitcoin|shares|stocks|forex|gold|petrol|fuel|flight|hotel|recipe|calories|nutrition|"
    r"add|put|remove|delete|save|record|log|mark|list)\b|\bat home\b|\bin (?:the )?fridge\b",
    re.I,
)
_PRICE = re.compile(r'\b(?:prices?|harga)\b|விலை', re.I)
_FOLLOW = re.compile(r'^(?:what about|how about|and|also|do (?:we|they|you) have|any|is there)\b', re.I)


def _direct(text: str) -> set[str]:
    if _OTHER.search(text):
        return set()
    stores = set(re.findall(r'\b(?:mydin|manjaku)\b', text.casefold()))
    if stores and re.search(r'price|harga|cost|how much|search|find|check|sell|stock|available|விலை', text, re.I):
        return stores
    return {'mydin'} if _PRICE.search(text) else set()


def context(user_text: str, prior_turns: list[dict] = ()) -> set[str]:
    """Carry a catalogue topic through short follow-ups; stop at topic changes."""
    current = str(user_text or '').strip()
    if _OTHER.search(current):
        return set()
    named = set(re.findall(r'\b(?:mydin|manjaku)\b', current.casefold()))
    direct = _direct(current)
    if named and direct:
        return named
    followup = len(current) <= 180 and bool(_FOLLOW.search(current) or _PRICE.search(current) or named)
    if followup:
        inherited = set(named)
        for turn in reversed(prior_turns):
            if turn.get('role') != 'user':
                continue
            previous = str(turn.get('content') or '').strip()
            if _OTHER.search(previous):
                break
            origin = _direct(previous)
            if origin:
                return inherited or origin
            if not inherited:
                inherited = set(re.findall(r'\b(?:mydin|manjaku)\b', previous.casefold()))
            if not _FOLLOW.search(previous) and not re.fullmatch(r'(?i)(?:mydin|manjaku)[?!.]*', previous):
                break
    return direct


def tool_names(stores: set[str]) -> set[str]:
    return {name for name in TOOLS if any(f'_{store}_' in name for store in stores)}


def render(evidence: list[dict]) -> str:
    """Render only verified product data, retaining variant/stock uncertainty."""
    sections = []
    for item in evidence:
        name = item.get('_tool_name')
        if name not in TOOLS:
            continue
        source = 'Manjaku Online' if 'manjaku' in name else 'MYDIN Online'
        if item.get('error') or item.get('ok') is not True:
            sections.append(f'{source}: current prices are unavailable. I have no verified price to quote.')
            continue
        lines = [source + (f" — checked {item['checked_at_local']}" if item.get('checked_at_local') else '')]
        rows = item.get('products') or []
        if not rows:
            lines.append('No matching products were found in these search results. This does not prove they are absent from the full catalogue; a brand or pack size may help.')
        for index, row in enumerate(rows[:10], 1):
            title = html.unescape(str(row.get('title') or 'Product'))
            selected = row.get('selected_variant')
            variant = selected or (row.get('variants') or [{}])[0]
            price = row.get('unit_price_for_quantity') or row.get('selling_price')
            if row.get('requires_variant_selection'):
                low = row.get('price_min') or row.get('selling_price')
                high = row.get('price_max') or row.get('maximum_price')
                detail = f'RM {low}' + (f'–{high}' if high and high != low else '') + ' — choose a variant/size'
            else:
                price = price or variant.get('selling_price')
                detail = f'RM {price}' if price is not None else 'price unavailable'
                if selected and selected.get('option'):
                    title += ' — ' + html.unescape(str(selected['option']))
            stock = row.get('in_stock') if 'in_stock' in row else variant.get('available_online')
            if not row.get('requires_variant_selection'):
                if stock is False:
                    detail += ' — sold out online'
                elif stock is None:
                    detail += ' — availability unconfirmed'
            if row.get('sufficient_stock') is False:
                detail += ' — requested quantity unavailable'
            lines.append(f'{index}. {title}: {detail}')
            if row.get('total_for_quantity') is not None:
                lines.append(f"   {row.get('requested_quantity')} packs: RM {row['total_for_quantity']} (items only)")
            elif row.get('listed_items_subtotal') is not None:
                lines.append(f"   {row.get('requested_quantity')} units: RM {row['listed_items_subtotal']} (listed prices only)")
            lines.append(str((selected or {}).get('url') or row.get('url') or ''))
        if item.get('more_results') or item.get('may_have_more_results'):
            lines.append('These are some matches; more results may be available.')
        if item.get('unavailable_products'):
            lines.append('Some product prices could not be verified and have been omitted.')
        if source == 'Manjaku Online':
            lines.append('Online listed prices; store prices, promotions and delivery may differ.')
        else:
            lines.append('Online selling prices used as your shopping reference.')
        sections.append('\n'.join(filter(None, lines)))
    return '\n\n'.join(sections)
