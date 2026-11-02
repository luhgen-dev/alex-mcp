"""Small deterministic permission vocabulary; the existing model interprets meaning.

These patterns neither translate user text nor fill tool arguments. Unknown
wording can discover reads; ambiguous/negative writes require clarification.
"""
import re

_TAMIL = re.compile(r"[\u0b80-\u0bff]")
_ACTION = re.compile(
    r"(?:சேர்க்கவும்|சேர்த்துவிடு|சேர்த்திடு|சேர்த்து|சேர்|பதிவு\s*செய்|சேமித்து|சேமிக்கவும்|சேமி|"
    r"நினைவூட்டு|நினைவூட்ட|நினைவில்\s*(?:வை|வைத்துக்கொள்)|நீக்கவும்|நீக்கு|மாற்றவும்|மாற்று|"
    r"ரத்து\s*செய்|அணைக்கவும்|அணை|இயக்கு|செலவு\s*செய்தேன்|வாங்கினேன்|கட்டினேன்)"
    r"|\b(?:serthu|serthidu|serthukko|sethudu|semichu|semichuko|nyabaga\s*paduthu|"
    r"ninaivootu|ninaivuttu|padhivu\s*sei|pannidu|pannunga|podu|podunga|vaanginen|"
    r"tambah(?:kan)?|masukkan|simpan(?:kan)?|ingatkan|padam(?:kan)?|batalkan|"
    r"tukar(?:kan)?|ubah(?:kan)?|hidupkan|matikan|catat(?:kan)?|rekod(?:kan)?|"
    r"sudah\s+bayar|dah\s+bayar|sudah\s+beli|dah\s+beli)\b", re.I,
)
_NO_ACTION = re.compile(
    r"(?:வேண்டாம்|வேண்டா|கூடாது|செய்யாத|சேர்க்காத|சேமிக்காத|நீக்காத|மாற்றாத|"
    r"அணைக்காத|இயக்காத|நினைவூட்டாத|செய்யாமல்|சொன்னால்|செய்தால்|எப்படி)"
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


def explicit_private(text: str) -> bool:
    return bool(_PRIVATE.search(text or ""))


def requests_memory(text: str) -> bool:
    return not denies_action(text) and bool(re.search(
        r"(?:நினைவில்\s*(?:வை|வைத்துக்கொள்)|சேமித்து|சேமிக்கவும்|சேமி)"
        r"|\b(?:semichu|semichuko|nyabagam\s+vechuko|simpan(?:kan)?|ingat\s+ini)\b", text or "", re.I))


def time_evidence(text: str) -> str:
    """Normalize explicit time tokens only, preserving quantities/ambiguities.

    Used by the existing time validator, never as intent, privacy or model input.
    Unrecognized wording still asks a question. Source spans are checked raw.
    """
    value = str(text or "").casefold()
    replacements = {
        "நாளை": "tomorrow", "இன்று": "today", "காலை": "morning",
        "மதியம்": "afternoon", "மாலை": "evening", "இரவு": "night",
        "ஒன்று": "1", "இரண்டு": "2", "மூன்று": "3", "நான்கு": "4",
        "ஐந்து": "5", "ஆறு": "6", "ஏழு": "7", "எட்டு": "8",
        "ஒன்பது": "9", "பத்து": "10", "பதினொன்று": "11", "பன்னிரண்டு": "12",
        "இருபது": "20", "முப்பது": "30", "நாற்பது": "40",
        "ஐம்பது": "50", "அறுபது": "60",
        "நிமிடங்களில்": "minutes later", "நிமிடத்திற்கு": "minutes",
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
