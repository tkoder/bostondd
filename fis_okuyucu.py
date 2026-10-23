"""İrsaliye / sipariş fişi fotoğrafından sipariş bilgilerini Claude ile okur."""

import base64
import io
import json
import os

import anthropic
from PIL import Image, ImageOps

MODEL = "claude-opus-5-5"

# Claude'a gönderilecek görselin uzun kenarı; el yazısını okumaya yeterli, istek boyutu küçük kalır.
MAX_IMAGE_SIDE = 2000

SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Fiş tarihi, YYYY-MM-DD biçiminde. Okunamıyorsa null.",
        },
        "supplier": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Fişi kesen tedarikçi firma. Belirlenemiyorsa null.",
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fruit": {"type": "string"},
                    "kg": {"type": "number"},
                    "unit_price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "line_total": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["fruit", "kg", "unit_price", "line_total"],
                "additionalProperties": False,
            },
        },
        "total": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "Fişin en altındaki genel toplam.",
        },
        "notes": {
            "type": "string",
            "description": "Okumada emin olunamayan yerler, kısa ve Türkçe. Yoksa boş.",
        },
    },
    "required": ["date", "supplier", "lines", "total", "notes"],
    "additionalProperties": False,
}

PROMPT = """Bu, bir meyve tedarikçisinden gelen el yazısı sipariş fişi / irsaliyenin fotoğrafı.
Fişteki bilgileri çıkar.

- Her satır için meyve adını, miktarı, birim fiyatı ve satır tutarını fişte yazdığı gibi oku.
  Miktar meyvenin biriminde yazılır (aşağıdaki listede parantez içinde): kg ya da adet gibi.
  Hesaplayıp düzeltme yapma; tutarsızlığı uygulama kendisi kontrol ediyor.
- Ara toplamlar satır değildir; yalnızca meyve satırlarını listele.
- Meyve adını şu listeden en yakın olanla eşleştir ve parantezdeki birim olmadan aynı yazımla döndür: {fruits}.
  Listede karşılığı yoksa düzgün Türkçe yazımıyla döndür (ör. "Kivi").
- Ondalık ayırıcı virgül veya nokta olabilir (18,1 = 18.1).
- Fişteki "Sayın" alanı alıcıdır (bizim işletmemiz), tedarikçi değildir.
  Tedarikçi fişin başlığında / kaşesinde yazıyorsa şu listeden eşleştir: {suppliers}.
  Fişte tedarikçi bilgisi yoksa null döndür.
- Tarih için bugünün tarihi {today}. Yıl eksik ya da yarım yazılmışsa (ör. "23/10/20")
  bugüne en yakın geçmiş tarihi kabul et.
- Okuyamadığın veya emin olmadığın yerleri "notes" alanında kısaca belirt."""


class FisOkumaHatasi(Exception):
    """Kullanıcıya gösterilebilir hata mesajı taşır."""


def is_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _image_block(data):
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # telefon fotoğraflarının yönünü düzelt
    except Exception as exc:
        raise FisOkumaHatasi("Görsel açılamadı. JPG, PNG veya WEBP yükleyin.") from exc
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(buf.getvalue()).decode("ascii"),
        },
    }


def _pdf_block(data):
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def read_receipt(data, is_pdf, fruits, suppliers, today, units=None):
    """Fişi okur; SCHEMA biçiminde sözlük döndürür."""
    if not is_configured():
        raise FisOkumaHatasi(
            "Fiş okuma için ANTHROPIC_API_KEY ayarlanmamış. "
            "ayarlar.env dosyasına anahtarı ekleyip uygulamayı yeniden başlatın."
        )

    source = _pdf_block(data) if is_pdf else _image_block(data)
    prompt = PROMPT.format(
        fruits=", ".join(f"{f} ({(units or {}).get(f, 'kg')})" for f in fruits) or "-",
        suppliers=", ".join(suppliers) or "-",
        today=today,
    )

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            # El yazısı rakamlarda dikkat önemli; okuma başına maliyet düşük.
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": [source, {"type": "text", "text": prompt}]}],
        )
    except anthropic.AuthenticationError as exc:
        raise FisOkumaHatasi("API anahtarı geçersiz. ayarlar.env dosyasını kontrol edin.") from exc
    except anthropic.RateLimitError as exc:
        raise FisOkumaHatasi("Çok fazla istek gönderildi; biraz bekleyip tekrar deneyin.") from exc
    except anthropic.BadRequestError as exc:
        raise FisOkumaHatasi(f"Fiş işlenemedi: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise FisOkumaHatasi(f"Claude API hatası ({exc.status_code}); tekrar deneyin.") from exc
    except anthropic.APIConnectionError as exc:
        raise FisOkumaHatasi("Claude API'ye bağlanılamadı; internet bağlantınızı kontrol edin.") from exc

    if response.stop_reason == "refusal":
        raise FisOkumaHatasi("Fiş okunamadı (istek reddedildi). Bilgileri elle girin.")
    if response.stop_reason == "max_tokens":
        raise FisOkumaHatasi("Fiş okuması yarıda kaldı; tekrar deneyin.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise FisOkumaHatasi("Fişten bilgi çıkarılamadı. Bilgileri elle girin.")
    return json.loads(text)
