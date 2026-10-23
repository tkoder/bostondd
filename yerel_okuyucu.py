"""Sipariş fişini macOS'un yerleşik metin tanıma motoruyla (Apple Vision) okur.

İnternet ve ücret gerektirmez. Rakamları iyi, el yazısı meyve adlarını zayıf okur;
bu yüzden adlar bilinen meyve listesiyle benzerlik üzerinden eşleştirilir,
eşleşmeyenler kullanıcıya bırakılır. Çıktı fis_okuyucu.read_receipt ile aynı biçimdedir.
"""

import io
import math
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from PIL import Image, ImageOps

try:
    import Vision
    from Foundation import NSData

    AVAILABLE = True
except ImportError:  # macOS dışı sistemler
    AVAILABLE = False


class YerelOkumaHatasi(Exception):
    """Kullanıcıya gösterilebilir hata mesajı taşır."""


# Fiş tablosunun sütun başlıkları (Türkçe karakterler sadeleştirilmiş, büyük harf).
HEADERS = {"kg": "MIKTAR", "fruit": "CINS", "price": "FIYAT", "total": "TUTAR"}
TR_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
# El yazısında rakamlarla sık karışan harfler.
DIGIT_LOOKALIKES = str.maketrans({"o": "0", "O": "0", "C": "0", "c": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5", "s": "5", "B": "8", "g": "9", "Z": "2"})
FRUIT_MATCH_THRESHOLD = 0.6
MAX_DATE_AGE_DAYS = 31
MIN_LONG_SIDE = 2400
MIN_SKEW_DEGREES = 0.7


def plain(text):
    return text.translate(TR_ASCII).upper()


# ---------------------------------------------------------------------------
# Metin tanıma
# ---------------------------------------------------------------------------

def recognize_words(img):
    """Görseldeki kelimeleri (metin, x, y_merkez) olarak döndürür; koordinatlar 0-1, y yukarıdan aşağı."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    data = buf.getvalue()

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        NSData.dataWithBytes_length_(data, len(data)), None
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["tr-TR"])
    # Dil düzeltmesi rakamları kelimeye çevirmeye çalışıyor; kapalı daha iyi.
    request.setUsesLanguageCorrection_(False)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise YerelOkumaHatasi(f"Metin tanıma başarısız: {error}")

    words = []
    for obs in request.results() or []:
        candidate = obs.topCandidates_(1)[0]
        text = candidate.string()
        # Bir gözlem birden çok kelime içerebilir ("181 Muz"); her kelimenin kendi kutusunu al.
        for m in re.finditer(r"\S+", text):
            box, _err = candidate.boundingBoxForRange_error_((m.start(), m.end() - m.start()), None)
            rect = box.boundingBox() if box else obs.boundingBox()
            x = rect.origin.x
            y = 1 - (rect.origin.y + rect.size.height / 2)
            words.append({"text": m.group(), "x": x, "y": y})
    return words


# ---------------------------------------------------------------------------
# Ayrıştırma
# ---------------------------------------------------------------------------

def parse_amount(text):
    """'5,9' -> 5.9, '1.810' -> 1810, '181' -> 181. Sayı değilse None."""
    raw = text.strip().rstrip(".,-").replace("₺", "").replace("TL", "")
    if sum(ch.isdigit() for ch in raw) * 2 < len(raw):
        return None
    raw = raw.translate(DIGIT_LOOKALIKES)
    if not re.fullmatch(r"\d+([.,]\d+)*", raw):
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")  # 1.810 binlik ayırıcı
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    return float(raw)


def decimal_variants(value, raw_text):
    """El yazısında virgül çoğu zaman kaçar: '181' için 181, 18.1, 1.81 adaylarını üretir."""
    if any(sep in raw_text for sep in ",."):
        return [value]
    digits = str(int(value))
    return [float(digits[:i] + "." + digits[i:]) for i in range(len(digits), 0, -1)]


def find_date(words, today):
    """Fiş tarihini bulur. Döndürür: (YYYY-MM-DD ya da None, uyarı ya da None)."""
    for w in words:
        m = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", w["text"])
        if not m:
            continue
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            if year and len(year) == 4:
                found = date(int(year), month, day)
            else:
                # Yıl yazılmamış ya da matbu "20" önekinden ibaret: bugüne en yakın geçmiş tarih.
                found = date(today.year, month, day)
                if found > today:
                    found = date(today.year - 1, month, day)
        except ValueError:
            continue
        # El yazısı rakamlar karışabiliyor (23 → 28); siparişler genelde birkaç gün içinde girildiği
        # için uzak bir tarih büyük olasılıkla yanlış okumadır.
        if timedelta(0) <= today - found <= timedelta(days=MAX_DATE_AGE_DAYS):
            return found.isoformat(), None
        return None, f"Fiş tarihi net okunamadı (“{m.group(0)}”); tarihi kontrol edin."
    return None, None


def find_headers(words):
    """Tablo başlık kelimelerini bulur; dördü de soldan sağa doğru sıradaysa döndürür."""
    found = {}
    for w in words:
        text = plain(w["text"])
        for key, label in HEADERS.items():
            if label in text and key not in found:
                found[key] = w
    order = ["kg", "fruit", "price", "total"]
    if len(found) < 4 or [found[k]["x"] for k in order] != sorted(found[k]["x"] for k in order):
        return None
    return found


def column_bounds(words):
    """Başlık konumlarından sütun sınırlarını çıkarır."""
    found = find_headers(words)
    if found is None:
        return None
    order = ["kg", "fruit", "price", "total"]
    header_y = max(found[k]["y"] for k in found)
    # Sütunlar arası sınır: komşu başlıkların ortası.
    cuts = [(found[a]["x"] + found[b]["x"]) / 2 for a, b in zip(order, order[1:])]
    return header_y, cuts


def skew_angle(words, size):
    """Matbu başlık satırının (MİKTARI … TUTARI) eğiminden fotoğrafın eğim açısını derece olarak verir."""
    found = find_headers(words)
    if found is None:
        return 0.0
    left, right = found["kg"], found["total"]
    dx = (right["x"] - left["x"]) * size[0]
    dy = (right["y"] - left["y"]) * size[1]
    return math.degrees(math.atan2(dy, dx))


def column_of(x, cuts):
    for name, cut in zip(["kg", "fruit", "price"], cuts):
        if x < cut:
            return name
    return "total"


def match_fruit(raw, fruits):
    if not raw:
        return None
    key = plain(raw).replace(" ", "")
    best, score = None, 0.0
    for fruit in fruits:
        s = SequenceMatcher(None, key, plain(fruit)).ratio()
        if s > score:
            best, score = fruit, s
    return best if score >= FRUIT_MATCH_THRESHOLD else None


def plausible_kg(variants):
    """Kontrol imkânı yoksa makul bir sipariş miktarı seç (en fazla 2 tam basamak)."""
    return next((v for v in variants if v < 100), variants[-1])


def resolve_row(kg_word, price, total):
    """Satırın kg ve fiyatını tutarla tutarlı olacak şekilde belirler.

    Döndürür: (kg, fiyat, not) — not, tahmin yapıldıysa kullanıcıya açıklamadır.
    """
    if kg_word is None:
        kg = round(total / price, 2)
        return kg, price, "miktar okunamadı; tutar ÷ fiyat ile hesaplandı"

    value = parse_amount(kg_word["text"])
    variants = decimal_variants(value, kg_word["text"])
    if price:
        # Virgülü kaçmış miktarı, fiyat × miktar ≈ tutar olacak şekilde seç.
        kg = min(variants, key=lambda v: abs(v * price - total))
        if abs(kg * price - total) <= 0.15 * total:
            return kg, price, None
        # Hiçbir okuma tutmuyor: fiyat hücresi büyük olasılıkla başka bir rakam (ör. ara toplam).
        kg = plausible_kg(variants)
        return kg, round(total / kg, 2), "fiyat tutarla uyuşmadı; tutar ÷ kg ile hesaplandı"

    kg = plausible_kg(variants)
    return kg, round(total / kg, 2), "fiyat okunamadı; tutar ÷ kg ile hesaplandı"


def parse_words(words, fruits, today):
    bounds = column_bounds(words)
    if bounds is None:
        raise YerelOkumaHatasi(
            "Fiş tablosu tanınamadı (MİKTARI / CİNSİ / FİYATI / TUTARI başlıkları bulunamadı)."
        )
    header_y, cuts = bounds

    cells = []
    for w in words:
        if w["y"] <= header_y + 0.005:
            continue
        col = column_of(w["x"], cuts)
        value = parse_amount(w["text"]) if col != "fruit" else None
        if col != "fruit" and value is None:
            continue
        cells.append({**w, "col": col, "value": value})

    # Her satırı tutar sütunundaki bir rakam başlatır; el yazısı satırları eğik olduğundan
    # diğer hücreler y'si en yakın tutara bağlanır.
    anchors = sorted((c for c in cells if c["col"] == "total"), key=lambda c: c["y"])
    if not anchors:
        raise YerelOkumaHatasi("Fişte tutar sütununda rakam bulunamadı.")
    rows = [{"total": a, "kg": None, "price": None, "fruit": []} for a in anchors]
    orphan_kgs = []
    for c in cells:
        if c["col"] == "total":
            continue
        row = min(rows, key=lambda r: abs(r["total"]["y"] - c["y"]))
        distance = abs(row["total"]["y"] - c["y"])
        if distance > 0.04:
            if c["col"] == "kg":
                orphan_kgs.append(c)
            continue
        if c["col"] == "fruit":
            row["fruit"].append(c)
        elif row[c["col"]] is None or distance < abs(row[c["col"]]["y"] - row["total"]["y"]):
            row[c["col"]] = c

    lines, line_rows, notes = [], [], []
    for row in rows:
        total = row["total"]["value"]
        price = row["price"]["value"] if row["price"] else None
        if row["kg"] is None and not price:
            continue  # miktarı ve fiyatı olmayan satırlar ara toplam ya da genel toplamdır
        n = len(lines) + 1
        kg, price, note = resolve_row(row["kg"], price, total)
        if note:
            notes.append(f"{n}. satırda {note}.")
        raw_name = " ".join(c["text"] for c in sorted(row["fruit"], key=lambda c: c["x"]))
        fruit = match_fruit(raw_name, fruits)
        if not fruit:
            seen = f"“{raw_name}” okundu" if raw_name else "ad okunamadı"
            notes.append(f"{n}. satırda meyve adı tanınamadı ({seen}); listeden seçin.")
        lines.append({"fruit": fruit or "", "kg": kg, "unit_price": price, "line_total": total})
        line_rows.append(row)
    if orphan_kgs:
        notes.append("Tutarı okunamayan satır(lar) olabilir; fişle karşılaştırıp eksik satırı ekleyin.")

    if not lines:
        raise YerelOkumaHatasi("Fişte meyve satırı bulunamadı.")
    # Genel toplam: son meyve satırının altındaki en alttaki rakam. Toplamlar sola kayarak
    # yazılabildiği için fiyat sütununa düşenler de sayılır.
    last_line_y = max(r["total"]["y"] for r in line_rows)
    below = [c for c in cells if c["col"] in ("price", "total") and c["y"] > last_line_y + 0.01]
    grand_total = max(below, key=lambda c: c["y"])["value"] if below else None

    found_date, date_note = find_date(words, today)
    if date_note:
        notes.insert(0, date_note)
    return {
        "date": found_date,
        "supplier": None,  # el yazısı fişlerde firma bilgisi yok; kullanıcı seçer
        "lines": lines,
        "total": grand_total,
        "notes": "\n".join(notes),
    }


def upscaled(img):
    """Küçük görselleri büyütür; Apple Vision düşük çözünürlüklü el yazısında rakam kaçırıyor."""
    scale = MIN_LONG_SIDE / max(img.size)
    if scale <= 1.05:
        return None
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)


def consistency(result):
    """Okumanın kendi içinde ne kadar tutarlı olduğu: kg × fiyat ≈ tutar, satırlar ≈ genel toplam."""
    score = 0
    for line in result["lines"]:
        if abs(line["kg"] * line["unit_price"] - line["line_total"]) <= 0.01 * line["line_total"]:
            score += 2
        if line["fruit"]:
            score += 1
    if result["total"] and abs(sum(l["line_total"] for l in result["lines"]) - result["total"]) <= 0.01 * result["total"]:
        score += 3
    if result["date"]:
        score += 1
    return score


def read_receipt(image_bytes, fruits, today=None):
    if not AVAILABLE:
        raise YerelOkumaHatasi("Yerel okuma yalnızca macOS'ta çalışır.")
    today = today or datetime.now().date()
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    except Exception as exc:
        raise YerelOkumaHatasi("Görsel açılamadı. JPG, PNG veya WEBP yükleyin.") from exc

    # Aynı fiş farklı ölçeklerde farklı okunabiliyor; hepsini dene, en tutarlı okumayı seç.
    # Yan ya da ters çekilmiş fotoğraflarda tablo başlıkları ancak döndürünce bulunur.
    last_error = None
    for angle in (0, 90, 270, 180):
        base = img.rotate(angle, expand=True) if angle else img
        results = []
        for candidate in (base, upscaled(base)):
            if candidate is None:
                continue
            readings = [recognize_words(candidate)]
            # Eğik çekilmişse satırlar karışabilir; başlık çizgisine göre düzeltilmiş hâli de oku.
            angle = skew_angle(readings[0], candidate.size)
            if abs(angle) > MIN_SKEW_DEGREES:
                readings.append(recognize_words(
                    candidate.rotate(angle, expand=True, resample=Image.BICUBIC, fillcolor="white")
                ))
            for words in readings:
                try:
                    results.append(parse_words(words, fruits, today))
                except YerelOkumaHatasi as exc:
                    last_error = exc
        if results:
            return max(results, key=consistency)
    raise last_error
