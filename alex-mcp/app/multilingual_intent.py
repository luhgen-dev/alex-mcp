"""Small deterministic permission vocabulary; the existing model interprets meaning.

These patterns neither translate user text nor fill tool arguments. Unknown
wording can discover reads; ambiguous/negative writes require clarification.
"""
import re

_TAMIL = re.compile(r"[\u0b80-\u0bff]")
_ACTION = re.compile(
    r"(?:சேர்க்கவும்|சேர்த்துவிடு|சேர்த்திடு|சேர்த்து|சேர்|பதிவு\s*செய்|சேமித்து|சேமிக்கவும்|சேமி(?![\u0b80-\u0bff])|"
    r"நினைவூட்டு|நினைவூட்ட|ஞாபகப்படுத்த|நியாபகப்படுத்த|ரிமைண்(?:ட்|டர்)\s*(?:பண்ணு|வை|போடு|செட்)|நினைவில்\s*(?:வை|வைத்துக்கொள்)|நீக்கவும்|நீக்கு|மாற்றவும்|மாற்று|"
    r"ரத்து\s*செய்|அணைக்கவும்|அணை|இயக்கு|செலவு\s*செய்தேன்|வாங்கினேன்|கட்டினேன்)"
    r"|\b(?:serthu|serthidu|serthukko|sethudu|semichu|semichuko|nyabaga\s*paduthu|"
    r"nyabaga\s*paduth(?:u|unga)|gnabaga\s*paduth(?:u|unga)|ninaivootu|ninaivuttu|padhivu\s*sei|pannidu|pannunga|podu|podunga|vaanginen|"
    r"tambah(?:kan)?|masukkan|simpan(?:kan)?|ingatkan|padam(?:kan)?|batalkan|"
    r"tukar(?:kan)?|ubah(?:kan)?|hidupkan|matikan|catat(?:kan)?|rekod(?:kan)?|"
    r"sudah\s+bayar|dah\s+bayar|sudah\s+beli|dah\s+beli)\b", re.I,
)
_NO_ACTION = re.compile(
    r"(?:வேண்டாம்|வேண்டா|கூடாது|செய்யாத|சேர்க்காத|சேமிக்காத|நீக்காத|மாற்றாத|"
    r"அணைக்காத|இயக்காத|நினைவூட்டாத|பண்ணாத|ஞாபகப்படுத்தாத|நியாபகப்படுத்தாத|செய்யாமல்|சொன்னால்|செய்தால்|எப்படி)"
    r"|\b(?:vendam|vendaam|venam|koodathu|seyyathe|pannathe|pannadha|pannadhe|"
    r"jangan|usah|tak\s+payah|tidak|bukan|kalau|sekiranya|bagaimana|macam\s+mana)\b", re.I,
)
_VOCAB = re.compile(
    r"\b(?:enakku|ennoda|naalai|nalai|naalaiku|innaiku|evlo|kaattu|kattu|"
    r"saya|susu|esok|berapa|tunjuk(?:kan)?|senarai|belanja|barang|tolong|peribadi|rahsia)\b", re.I,
)
_PRIVATE = re.compile(
    r"(?:தனிப்பட்ட|தனியாக|எனக்கு\s*மட்டும்|என்னுடையது\s*மட்டும்)"
    r"|\b(?:enakku\s+mattum|thanipatta|peribadi|rahsia|untuk\s+saya\s+sahaja)\b", re.I,
)


def is_multilingual(text: str) -> bool:
    return bool(_TAMIL.search(text or "") or _VOCAB.search(text or "") or _ACTION.search(text or "") or _NO_ACTION.search(text or ""))


def denies_action(text: str) -> bool:
    value = text or ""
    return bool(_NO_ACTION.search(value)) or bool(
        (_TAMIL.search(value) or _ACTION.search(value))
        and re.search(r"\b(?:don't|dont|do\s+not|not|never|without)\b", value, re.I)
    )


def requests_action(text: str) -> bool:
    return bool(_ACTION.search(text or "")) and not denies_action(text)


def requests_reminder(text: str) -> bool:
    return requests_action(text) and bool(re.search(
        r"நினைவூட்டு|நினைவூட்ட|ஞாபகப்படுத்த|நியாபகப்படுத்த|ரிமைண்(?:ட்|டர்)"
        r"|\b(?:ninaivootu|ninaivuttu|nyabaga\s*paduth(?:u|unga)|"
        r"gnabaga\s*paduth(?:u|unga)|ingatkan)\b", text or "", re.I))


def explicit_private(text: str) -> bool:
    return bool(_PRIVATE.search(text or ""))


def requests_memory(text: str) -> bool:
    return not denies_action(text) and bool(re.search(
        r"(?:நினைவில்\s*(?:வை|வைத்துக்கொள்)|சேமித்து|சேமிக்கவும்|சேமி)"
        r"|\b(?:semichu|semichuko|nyabagam\s+vechuko|simpan(?:kan)?|ingat\s+ini)\b", text or "", re.I))


def file_delivery_requested(text: str) -> bool:
    """Explicit current-language viewing/sending intent, never a destination/ACL.

    This only permits an already scoped retrieval tool's result to be attached.
    It cannot choose a record, change scope, or execute retrieved audio.
    """
    value = str(text or "")
    return not denies_action(value) and bool(re.search(
        r"(?:அனுப்பு|அனுப்புங்கள்|அனுப்பவும்|காட்டு|காமி|காண்பி|திறந்து|படத்தை|புகைப்படத்தை|ரசீதை)"
        r"|\b(?:anuppu(?:nga)?|anupunga|kaattu|kattu|hantar(?:kan)?|kirim(?:kan)?|"
        r"tunjuk(?:kan)?|papar(?:kan)?|buka|mainkan|dengar)\b",
        value, re.I))


def file_delivery_denied(text: str) -> bool:
    value = str(text or "")
    return denies_action(value) or bool(re.search(
        r"(?:அனுப்பாத|காட்டாத|காமிக்காத|திறக்காத)"
        r"|\b(?:don't|do not|dont|never|no need to)\s+(?:\w+\s+){0,2}"
        r"(?:send|share|attach|show|open|download|export)\b"
        r"|\b(?:no|without)\s+(?:the\s+)?(?:file|image|photo|attachment|pdf|csv|json)\b",
        value, re.I))


def export_format_requested(text: str) -> bool:
    """File-format words may carry Tamil suffixes (for example PDFல)."""
    return bool(re.search(
        r"(?<![a-z])(?:pdf|csv|json)(?![a-z])|பி\.?\s*டி\.?\s*எஃப்|பிடிஎஃப்",
        str(text or ""), re.I))


def time_evidence(text: str) -> str:
    """Normalize explicit time tokens only, preserving quantities/ambiguities.

    Used by the existing time validator, never as intent, privacy or model input.
    Unrecognized wording still asks a question. Source spans are checked raw.
    """
    value = str(text or "").casefold()
    replacements = {
        "நாளை": "tomorrow", "நாளைக்கு": "tomorrow", "நாளைக்கி": "tomorrow",
        "இன்று": "today", "இன்னைக்கு": "today", "காலை": "morning", "காலையில்": "morning",
        "மதியம்": "afternoon", "மாலை": "evening", "இரவு": "night",
        "ஒன்று": "1", "இரண்டு": "2", "ரெண்டு": "2", "மூன்று": "3", "நான்கு": "4",
        "ஐந்து": "5", "அஞ்சு": "5", "ஒரு அஞ்சு": "5", "ஒரு ஐந்து": "5",
        "ஆறு": "6", "ஏழு": "7", "எட்டு": "8",
        "ஒன்பது": "9", "பத்து": "10", "பதினொன்று": "11", "பன்னிரண்டு": "12",
        "இருபது": "20", "முப்பது": "30", "நாற்பது": "40",
        "ஐம்பது": "50", "அறுபது": "60",
        "நிமிடங்களில்": "minutes later", "நிமிடத்திற்கு": "minutes",
        "நிமிஷத்துல": "minutes later", "நிமிடத்தில்": "minutes later",
        "நிமிஷம்": "minutes", "நிமிஷங்கள்": "minutes",
        "நிமிடங்கள்": "minutes", "நிமிடம்": "minutes",
        "மணி நேரத்தில்": "hours later", "மணி நேரம்": "hours",
        "கழித்து": "later", "பிறகு": "later", "அல்லது": "or", "சுமார்": "around",
        "naalai": "tomorrow", "naalaiku": "tomorrow", "nalai": "tomorrow",
        "innaiku": "today", "kaalai": "morning", "kaalaila": "morning",
        "maalai": "evening", "rathiri": "night", "nimisham": "minutes",
        "apram": "later", "kazhichu": "later",
        "esok": "tomorrow", "hari ini": "today", "pagi": "morning",
        "petang": "evening", "malam": "night", "tengah hari": "noon",
        "pukul": "at", "jam": "hours", "minit": "minutes", "lagi": "later",
        "selepas": "after", "sebelum": "before", "atau": "or", "antara": "between",
        "satu": "1", "dua": "2", "tiga": "3", "empat": "4", "lima": "5",
        "enam": "6", "tujuh": "7", "lapan": "8", "sembilan": "9", "sepuluh": "10",
        "sebelas": "11", "dua belas": "12", "dua puluh": "20", "tiga puluh": "30",
    }
    # Tamil combining marks must count as word characters, unlike regex \b.
    boundary = r"[\w\u0b80-\u0bff]"
    pattern = "|".join(re.escape(x) for x in sorted(replacements, key=len, reverse=True))
    value = re.sub(r"(?<!" + boundary + r")(" + pattern + r")(?!" + boundary + r")",
                   lambda m: replacements[m[0]], value)
    value = re.sub(r"(?<!\d)(\d{1,2}(?::[0-5]\d)?)\s*(?:மணிக்கு|manikku)(?![\w\u0b80-\u0bff])", r"at \1", value)
    return value
