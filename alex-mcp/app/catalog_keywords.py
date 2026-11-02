"""Small catalogue spelling/plural normalization; never changes pack sizes."""
import re

ALIASES = {'yogurts': 'yogurt', 'yoghurts': 'yogurt', 'yoghurt': 'yogurt',
           'oils': 'oil', 'detergents': 'detergent', 'sardines': 'sardine',
           'diapers': 'diaper', 'wipes': 'wipe', 'tissues': 'tissue'}


def normalize(query):
    return re.sub(r'\b[a-z]+\b', lambda m: ALIASES.get(m[0], m[0]), query.casefold()).strip()


def relevant(query, title):
    """Do not substitute scented toiletries/containers for a food/diaper query."""
    q, t = normalize(query), normalize(title)
    accessory = r'\b(?:shampoo|wash|soap|lotion|bath|cream|bag|case|container|bottle|wipe)\b|\bhead to toe\b'
    if not re.search(accessory, q):
        if re.search(r'\b(?:milk|yogurt|rice)\b', q) and re.search(accessory, t):
            # Full cream milk is food; cream alone is not enough to reject it.
            if re.search(r'\b(?:shampoo|wash|soap|lotion|bath|bag|case|container|bottle|wipe)\b|\bhead to toe\b', t):
                return False
        if re.search(r'\bdiaper\b', q) and re.search(r'\b(?:bag|case|container)\b', t):
            return False
    return True
