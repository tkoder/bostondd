import csv
import io
import os
import re
import sqlite3
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
    send_from_directory, url_for,
)

import fis_okuyucu
import yerel_okuyucu

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "stok.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "stok-takip-yerel-anahtar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sku        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT '',
    unit       TEXT NOT NULL DEFAULT 'adet',
    price      REAL NOT NULL DEFAULT 0,
    min_stock  REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    type       TEXT NOT NULL CHECK (type IN ('IN', 'OUT')),
    quantity   REAL NOT NULL CHECK (quantity > 0),
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id);

CREATE TABLE IF NOT EXISTS suppliers (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS fruits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    unit       TEXT NOT NULL DEFAULT 'kg'
);
CREATE TABLE IF NOT EXISTS fruit_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date  TEXT NOT NULL,
    supplier_id INTEGER REFERENCES suppliers(id),
    vat_rate    REAL NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT '',
    receipt     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fruit_orders_date ON fruit_orders(order_date);
CREATE TABLE IF NOT EXISTS fruit_order_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES fruit_orders(id) ON DELETE CASCADE,
    fruit      TEXT NOT NULL,
    kg         REAL NOT NULL CHECK (kg > 0),
    unit_price REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fruit_order_lines_order ON fruit_order_lines(order_id);

-- Tedarikçinin ay sonunda gönderdiği fatura; kendi kayıtlarımızla karşılaştırmak için.
CREATE TABLE IF NOT EXISTS supplier_invoices (
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    month       TEXT NOT NULL,
    amount      REAL NOT NULL,
    invoice_no  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (supplier_id, month)
);
"""

# Excel'deki sütun sırası; yeni meyveler bunların arkasına eklenir.
DEFAULT_FRUITS = ["Çilek", "Muz", "Ananas", "Limon", "Portakal"]
DEFAULT_UNITS = {"Ananas": "adet"}
FRUIT_UNITS = ["kg", "adet", "kasa"]
# Bu birimlerdeki miktar tam sayı olmalı.
COUNT_UNITS = {"adet", "kasa"}
DEFAULT_SUPPLIERS = [
    "Çilek Tarım - İSMAİL ÇARBOĞA",
    "Yılmaz Kardeşler Tarım",
    "Taşdemirler Gıda - Recep Çatan Recoba",
]

# Stok miktarı hareketlerden hesaplanır; ayrı bir sütunda tutulmaz.
PRODUCTS_WITH_STOCK = """
SELECT p.*,
       COALESCE(SUM(CASE WHEN m.type = 'IN' THEN m.quantity ELSE -m.quantity END), 0) AS stock
FROM products p
LEFT JOIN movements m ON m.product_id = p.id
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        if db.execute("SELECT COUNT(*) FROM fruits").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO fruits (name, sort_order, unit) VALUES (?, ?, ?)",
                [(name, i, DEFAULT_UNITS.get(name, "kg")) for i, name in enumerate(DEFAULT_FRUITS)],
            )
        # Eski veritabanlarına birim sütununu ekle (meyve siparişleri ilk sürümde hep kg idi).
        if "unit" not in [r[1] for r in db.execute("PRAGMA table_info(fruits)")]:
            db.execute("ALTER TABLE fruits ADD COLUMN unit TEXT NOT NULL DEFAULT 'kg'")
            for name, unit in DEFAULT_UNITS.items():
                db.execute("UPDATE fruits SET unit = ? WHERE name = ?", (unit, name))
        if db.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
            db.executemany("INSERT INTO suppliers (name) VALUES (?)", [(n,) for n in DEFAULT_SUPPLIERS])


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_number(value, default=0.0):
    value = (value or "").strip().replace(",", ".")
    if not value:
        return default
    return float(value)


def fetch_product(product_id):
    return get_db().execute(
        PRODUCTS_WITH_STOCK + " WHERE p.id = ? GROUP BY p.id", (product_id,)
    ).fetchone()


@app.template_filter("num")
def format_number(value):
    """1234.5 -> '1.234,5' ; 10.0 -> '10'"""
    if value is None:
        return ""
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("tl")
def format_money(value):
    """13002 -> '13.002,00'"""
    if value is None:
        return ""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("input_num")
def format_input_number(value):
    """Form alanı için binlik ayırıcısız sayı: 18.1 -> '18,1'. Metin olduğu gibi kalır."""
    if not isinstance(value, (int, float)):
        return value or ""
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


GUNLER = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


@app.template_filter("tarih")
def format_date(value):
    """'2026-10-23' -> '23.10.2026 Cum'"""
    d = datetime.strptime(value, "%Y-%m-%d")
    return f"{d:%d.%m.%Y} {GUNLER[d.weekday()]}"


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    only_critical = request.args.get("kritik") == "1"

    sql = PRODUCTS_WITH_STOCK
    params = []
    if q:
        sql += " WHERE p.name LIKE ? OR p.sku LIKE ? OR p.category LIKE ?"
        params = [f"%{q}%"] * 3
    sql += " GROUP BY p.id"
    if only_critical:
        sql += " HAVING stock <= p.min_stock"
    sql += " ORDER BY p.name COLLATE NOCASE"

    products = get_db().execute(sql, params).fetchall()
    all_products = get_db().execute(PRODUCTS_WITH_STOCK + " GROUP BY p.id").fetchall()
    stats = {
        "count": len(all_products),
        "critical": sum(1 for p in all_products if p["stock"] <= p["min_stock"]),
        "value": sum(max(p["stock"], 0) * p["price"] for p in all_products),
    }
    return render_template(
        "index.html", products=products, q=q, only_critical=only_critical, stats=stats
    )


def product_from_form():
    form = request.form
    data = {
        "sku": form.get("sku", "").strip(),
        "name": form.get("name", "").strip(),
        "category": form.get("category", "").strip(),
        "unit": form.get("unit", "").strip() or "adet",
    }
    errors = []
    if not data["sku"]:
        errors.append("Ürün kodu zorunludur.")
    if not data["name"]:
        errors.append("Ürün adı zorunludur.")
    try:
        data["price"] = parse_number(form.get("price"))
        data["min_stock"] = parse_number(form.get("min_stock"))
        if data["price"] < 0 or data["min_stock"] < 0:
            errors.append("Fiyat ve minimum stok negatif olamaz.")
    except ValueError:
        errors.append("Fiyat ve minimum stok sayı olmalıdır.")
    return data, errors


@app.route("/urun/yeni", methods=["GET", "POST"])
def product_new():
    if request.method == "POST":
        data, errors = product_from_form()
        initial = 0.0
        try:
            initial = parse_number(request.form.get("initial_stock"))
            if initial < 0:
                errors.append("Açılış stoğu negatif olamaz.")
        except ValueError:
            errors.append("Açılış stoğu sayı olmalıdır.")

        if not errors:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO products (sku, name, category, unit, price, min_stock, created_at)"
                    " VALUES (:sku, :name, :category, :unit, :price, :min_stock, :created_at)",
                    {**data, "created_at": now()},
                )
                if initial > 0:
                    db.execute(
                        "INSERT INTO movements (product_id, type, quantity, note, created_at)"
                        " VALUES (?, 'IN', ?, 'Açılış stoğu', ?)",
                        (cur.lastrowid, initial, now()),
                    )
                db.commit()
                flash(f"“{data['name']}” eklendi.", "success")
                return redirect(url_for("product_detail", product_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                errors.append(f"“{data['sku']}” kodlu bir ürün zaten var.")

        for e in errors:
            flash(e, "error")
        return render_template("product_form.html", product=request.form, is_new=True)

    return render_template("product_form.html", product={}, is_new=True)


@app.route("/urun/<int:product_id>/duzenle", methods=["GET", "POST"])
def product_edit(product_id):
    product = fetch_product(product_id)
    if product is None:
        flash("Ürün bulunamadı.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        data, errors = product_from_form()
        if not errors:
            db = get_db()
            try:
                db.execute(
                    "UPDATE products SET sku=:sku, name=:name, category=:category, unit=:unit,"
                    " price=:price, min_stock=:min_stock WHERE id=:id",
                    {**data, "id": product_id},
                )
                db.commit()
                flash("Ürün güncellendi.", "success")
                return redirect(url_for("product_detail", product_id=product_id))
            except sqlite3.IntegrityError:
                errors.append(f"“{data['sku']}” kodlu bir ürün zaten var.")
        for e in errors:
            flash(e, "error")
        return render_template(
            "product_form.html", product={**request.form, "id": product_id}, is_new=False
        )

    return render_template("product_form.html", product=product, is_new=False)


@app.route("/urun/<int:product_id>/sil", methods=["POST"])
def product_delete(product_id):
    product = fetch_product(product_id)
    if product is not None:
        db = get_db()
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        db.commit()
        flash(f"“{product['name']}” ve tüm hareketleri silindi.", "success")
    return redirect(url_for("index"))


@app.route("/urun/<int:product_id>")
def product_detail(product_id):
    product = fetch_product(product_id)
    if product is None:
        flash("Ürün bulunamadı.", "error")
        return redirect(url_for("index"))
    movements = get_db().execute(
        "SELECT * FROM movements WHERE product_id = ? ORDER BY created_at DESC, id DESC",
        (product_id,),
    ).fetchall()
    return render_template("product_detail.html", product=product, movements=movements)


@app.route("/urun/<int:product_id>/hareket", methods=["POST"])
def movement_add(product_id):
    product = fetch_product(product_id)
    if product is None:
        flash("Ürün bulunamadı.", "error")
        return redirect(url_for("index"))

    mtype = request.form.get("type")
    note = request.form.get("note", "").strip()
    try:
        quantity = parse_number(request.form.get("quantity"))
    except ValueError:
        quantity = 0

    if mtype not in ("IN", "OUT"):
        flash("Geçersiz hareket türü.", "error")
    elif quantity <= 0:
        flash("Miktar sıfırdan büyük bir sayı olmalıdır.", "error")
    elif mtype == "OUT" and quantity > product["stock"]:
        flash(
            f"Yetersiz stok: mevcut {format_number(product['stock'])} {product['unit']}, "
            f"çıkış {format_number(quantity)} {product['unit']}.",
            "error",
        )
    else:
        db = get_db()
        db.execute(
            "INSERT INTO movements (product_id, type, quantity, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (product_id, mtype, quantity, note, now()),
        )
        db.commit()
        label = "Giriş" if mtype == "IN" else "Çıkış"
        flash(f"{label} kaydedildi: {format_number(quantity)} {product['unit']}.", "success")
        updated = fetch_product(product_id)
        if updated["stock"] <= updated["min_stock"]:
            flash(
                f"Dikkat: stok kritik seviyede ({format_number(updated['stock'])} {updated['unit']}).",
                "warning",
            )
    return redirect(url_for("product_detail", product_id=product_id))


@app.route("/hareket/<int:movement_id>/sil", methods=["POST"])
def movement_delete(movement_id):
    db = get_db()
    movement = db.execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()
    if movement is None:
        return redirect(url_for("index"))
    product = fetch_product(movement["product_id"])
    # Bir girişi silmek stoğu eksiye düşürecekse engelle.
    if movement["type"] == "IN" and product["stock"] - movement["quantity"] < 0:
        flash("Bu giriş silinirse stok eksiye düşer; önce ilgili çıkışları silin.", "error")
    else:
        db.execute("DELETE FROM movements WHERE id = ?", (movement_id,))
        db.commit()
        flash("Hareket silindi.", "success")
    return redirect(url_for("product_detail", product_id=movement["product_id"]))


@app.route("/hareketler")
def movements():
    rows = get_db().execute(
        "SELECT m.*, p.name, p.sku, p.unit FROM movements m"
        " JOIN products p ON p.id = m.product_id"
        " ORDER BY m.created_at DESC, m.id DESC LIMIT 500"
    ).fetchall()
    return render_template("movements.html", movements=rows)


def csv_response(filename, header, rows):
    buf = io.StringIO()
    # Türkçe Excel ';' ayırıcı ve UTF-8 BOM ile dosyayı doğru açar.
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    data = "﻿" + buf.getvalue()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}-{stamp}.csv"},
    )


@app.route("/disa-aktar/stok.csv")
def export_stock():
    products = get_db().execute(
        PRODUCTS_WITH_STOCK + " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    ).fetchall()
    rows = [
        [
            p["sku"], p["name"], p["category"], p["unit"],
            format_number(p["stock"]), format_number(p["min_stock"]),
            format_number(p["price"]), format_number(max(p["stock"], 0) * p["price"]),
            "KRİTİK" if p["stock"] <= p["min_stock"] else "",
        ]
        for p in products
    ]
    header = ["Kod", "Ürün", "Kategori", "Birim", "Stok", "Min. Stok", "Birim Fiyat", "Stok Değeri", "Durum"]
    return csv_response("stok-listesi", header, rows)


@app.route("/disa-aktar/hareketler.csv")
def export_movements():
    movements = get_db().execute(
        "SELECT m.*, p.name, p.sku, p.unit FROM movements m"
        " JOIN products p ON p.id = m.product_id ORDER BY m.created_at, m.id"
    ).fetchall()
    rows = [
        [
            m["created_at"], m["sku"], m["name"],
            "Giriş" if m["type"] == "IN" else "Çıkış",
            format_number(m["quantity"]), m["unit"], m["note"],
        ]
        for m in movements
    ]
    header = ["Tarih", "Kod", "Ürün", "Tür", "Miktar", "Birim", "Açıklama"]
    return csv_response("stok-hareketleri", header, rows)



# ---------------------------------------------------------------------------
# Meyve siparişleri
# ---------------------------------------------------------------------------

RECEIPT_DIR = os.path.join(BASE_DIR, "fisler")
RECEIPT_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
MAX_RECEIPT_BYTES = 20 * 1024 * 1024


TR_ALFABE = "abcçdefgğhıijklmnoöprsştuüvyz"


def tr_key(text):
    """Türkçe alfabetik sıralama anahtarı (SQLite NOCASE Ç, Ş, İ gibi harfleri sona atar)."""
    lowered = text.replace("I", "ı").replace("İ", "i").lower()
    return [TR_ALFABE.find(ch) if ch in TR_ALFABE else 100 + ord(ch) for ch in lowered]


def fruit_names():
    rows = get_db().execute("SELECT name FROM fruits ORDER BY sort_order, name COLLATE NOCASE")
    return [r["name"] for r in rows]


def fruit_units():
    return {r["name"]: r["unit"] for r in get_db().execute("SELECT name, unit FROM fruits")}


def supplier_names():
    rows = get_db().execute("SELECT name FROM suppliers")
    return sorted((r["name"] for r in rows), key=tr_key)


def month_bounds(month):
    """'2026-10' -> ('2026-10-01', '2026-11-01')"""
    start = datetime.strptime(month, "%Y-%m")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"


def shift_month(month, delta):
    d = datetime.strptime(month, "%Y-%m")
    index = d.year * 12 + d.month - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def kurus(value):
    """Kuruşa yuvarlar; Excel ve faturalar gibi yarım kuruşu yukarı yuvarlar (round() çifte yuvarlar)."""
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def load_orders(where="", params=()):
    """Siparişleri satırları ve hesaplanmış toplamlarıyla birlikte döndürür."""
    db = get_db()
    orders = [
        dict(r)
        for r in db.execute(
            "SELECT o.*, s.name AS supplier FROM fruit_orders o"
            " LEFT JOIN suppliers s ON s.id = o.supplier_id"
            f" {where} ORDER BY o.order_date, o.id",
            params,
        )
    ]
    if not orders:
        return []
    by_id = {o["id"]: o for o in orders}
    for o in orders:
        o["lines"], o["by_fruit"], o["subtotal"] = [], {}, 0.0
    marks = ",".join("?" * len(by_id))
    for line in db.execute(
        f"SELECT * FROM fruit_order_lines WHERE order_id IN ({marks}) ORDER BY id", list(by_id)
    ):
        o = by_id[line["order_id"]]
        total = line["kg"] * line["unit_price"]
        o["lines"].append({**dict(line), "total": total})
        o["subtotal"] += total
        agg = o["by_fruit"].setdefault(line["fruit"], {"kg": 0.0, "total": 0.0})
        agg["kg"] += line["kg"]
        agg["total"] += total
    for o in orders:
        # Fatura ve Excel'deki gibi her siparişin tutarları kuruşa yuvarlanır; aylık toplamlar
        # bu yuvarlanmış satırların toplamıdır.
        o["subtotal"] = kurus(o["subtotal"])
        o["vat"] = kurus(o["subtotal"] * o["vat_rate"] / 100)
        o["grand_total"] = kurus(o["subtotal"] + o["vat"])
    return orders


def summarize(orders):
    """Meyve bazında ay özeti."""
    fruits = {}
    for o in orders:
        for name, agg in o["by_fruit"].items():
            f = fruits.setdefault(name, {"kg": 0.0, "total": 0.0})
            f["kg"] += agg["kg"]
            f["total"] += agg["total"]
    order = {name: i for i, name in enumerate(fruit_names())}
    units = fruit_units()
    fruit_cols = sorted(fruits, key=lambda n: (order.get(n, len(order)), n))
    return {
        "fruit_cols": fruit_cols,
        "units": {name: units.get(name, "kg") for name in fruit_cols},
        "fruits": fruits,
        "subtotal": sum(o["subtotal"] for o in orders),
        "vat": sum(o["vat"] for o in orders),
        "grand_total": sum(o["grand_total"] for o in orders),
    }


def month_orders():
    month = request.args.get("ay") or datetime.now().strftime("%Y-%m")
    try:
        start, end = month_bounds(month)
    except ValueError:
        abort(404)
    supplier_id = request.args.get("firma", type=int)
    where, params = "WHERE o.order_date >= ? AND o.order_date < ?", [start, end]
    if supplier_id:
        where += " AND o.supplier_id = ?"
        params.append(supplier_id)
    return month, supplier_id, load_orders(where, params)


def parse_money(text):
    """Türkçe yazılmış tutar: '27.231,62' / '27231,62' / '27.231' / '27231.62' -> float."""
    raw = text.replace("₺", "").replace("TL", "").replace(" ", "").strip()
    if not re.fullmatch(r"\d[\d.,]*", raw):
        raise ValueError(text)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1 or re.search(r"\.\d{3}$", raw):
        raw = raw.replace(".", "")  # yalnızca binlik ayırıcı
    return float(raw)


# Fatura ile kendi hesabımız arasında kabul edilen kuruş farkı (KDV yuvarlaması).
INVOICE_TOLERANCE = 1.0


def supplier_statements(month):
    """Ay içindeki alımları firma bazında toplar ve girilmiş faturalarla karşılaştırır."""
    db = get_db()
    start, end = month_bounds(month)
    statements = {}

    def statement(supplier_id, name):
        return statements.setdefault(supplier_id, {
            "supplier_id": supplier_id, "name": name, "count": 0,
            "subtotal": 0.0, "vat": 0.0, "grand_total": 0.0, "invoice": None, "diff": None,
        })

    for o in load_orders("WHERE o.order_date >= ? AND o.order_date < ?", (start, end)):
        st = statement(o["supplier_id"], o["supplier"])
        st["count"] += 1
        st["subtotal"] += o["subtotal"]
        st["vat"] += o["vat"]
        st["grand_total"] += o["grand_total"]

    for inv in db.execute(
        "SELECT i.*, s.name FROM supplier_invoices i JOIN suppliers s ON s.id = i.supplier_id WHERE i.month = ?",
        (month,),
    ):
        st = statement(inv["supplier_id"], inv["name"])
        st["invoice"] = inv
        # Ekranda görünen kuruşa yuvarlanmış tutarla karşılaştır; aksi hâlde 200,00 fark 200,01 görünebilir.
        st["diff"] = kurus(inv["amount"] - kurus(st["grand_total"]))

    for st in statements.values():
        if st["diff"] is None:
            st["status"] = "bekliyor"
        elif abs(st["diff"]) <= INVOICE_TOLERANCE:
            st["status"] = "tutuyor"
        else:
            st["status"] = "fazla" if st["diff"] > 0 else "eksik"
    # Firması seçilmemiş siparişler en sonda.
    return sorted(statements.values(), key=lambda st: (st["supplier_id"] is None, -st["grand_total"]))


@app.route("/siparisler/fatura", methods=["POST"])
def invoice_save():
    month = request.form.get("ay", "")
    supplier_id = request.form.get("firma", type=int)
    try:
        month_bounds(month)
    except ValueError:
        abort(400)
    db = get_db()
    if not supplier_id or not db.execute("SELECT 1 FROM suppliers WHERE id = ?", (supplier_id,)).fetchone():
        abort(400)
    raw = request.form.get("amount", "").strip()
    if not raw:
        db.execute("DELETE FROM supplier_invoices WHERE supplier_id = ? AND month = ?", (supplier_id, month))
        db.commit()
        flash("Fatura tutarı silindi.", "success")
    else:
        try:
            amount = parse_money(raw)
        except ValueError:
            flash("Fatura tutarı geçerli bir sayı olmalıdır (ör. 27.231,62).", "error")
            return redirect(url_for("orders", ay=month, _anchor=f"firma-{supplier_id}"))
        db.execute(
            "INSERT INTO supplier_invoices (supplier_id, month, amount, invoice_no, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (supplier_id, month) DO UPDATE SET amount = excluded.amount,"
            " invoice_no = excluded.invoice_no, updated_at = excluded.updated_at",
            (supplier_id, month, amount, request.form.get("invoice_no", "").strip(), now()),
        )
        db.commit()
        flash("Fatura tutarı kaydedildi.", "success")
    return redirect(url_for("orders", ay=month, _anchor=f"firma-{supplier_id}"))


@app.route("/siparisler")
def orders():
    month, supplier_id, rows = month_orders()
    d = datetime.strptime(month, "%Y-%m")
    statements = supplier_statements(month)
    return render_template(
        "orders.html",
        orders=rows,
        summary=summarize(rows),
        statements=statements,
        month_total=sum(st["grand_total"] for st in statements),
        orders_all_count=sum(st["count"] for st in statements),
        invoice_total=sum(st["invoice"]["amount"] for st in statements if st["invoice"]),
        month=month,
        month_label=f"{AYLAR[d.month - 1]} {d.year}",
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1),
        supplier_id=supplier_id,
        suppliers=sorted(get_db().execute("SELECT * FROM suppliers").fetchall(), key=lambda r: tr_key(r["name"])),
    )


def slug(text):
    text = text.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


@app.route("/siparisler/disa-aktar.csv")
def export_orders():
    month, supplier_id, rows = month_orders()
    summary = summarize(rows)
    cols = summary["fruit_cols"]
    header = ["Tarih"]
    for name in cols:
        header += [f"{name} {summary['units'][name].upper()}", f"{name} Br. F.", f"{name} Toplam"]
    header += ["Toplam Fiyat", "KDV %", "KDV Dahil", "Firma", "Not"]

    def fruit_cells(agg):
        if not agg:
            return ["", "", ""]
        return [format_number(agg["kg"]), format_money(agg["total"] / agg["kg"]), format_money(agg["total"])]

    out = []
    for o in rows:
        d = datetime.strptime(o["order_date"], "%Y-%m-%d")
        row = [f"{d:%d.%m.%Y}"]
        for name in cols:
            row += fruit_cells(o["by_fruit"].get(name))
        row += [format_money(o["subtotal"]), format_number(o["vat_rate"]),
                format_money(o["grand_total"]), o["supplier"] or "", o["note"]]
        out.append(row)
    if rows:
        total = ["TOPLAM"]
        for name in cols:
            total += fruit_cells(summary["fruits"][name])
        total += [format_money(summary["subtotal"]), "", format_money(summary["grand_total"]), "", ""]
        out.append(total)

    filename = f"meyve-siparisleri-{month}"
    if supplier_id and rows and rows[0]["supplier"]:
        filename += "-" + slug(rows[0]["supplier"])
    return csv_response(filename, header, out)


def get_or_create(table, name):
    db = get_db()
    row = db.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid


def valid_receipt(name):
    """Formdan gelen fiş adının bizim kaydettiğimiz bir dosya olduğunu doğrular."""
    if not name or os.path.basename(name) != name:
        return None
    return name if os.path.isfile(os.path.join(RECEIPT_DIR, name)) else None


def order_from_form():
    form = request.form
    data = {
        "order_date": form.get("order_date", "").strip(),
        "supplier": form.get("supplier", "").strip(),
        "note": form.get("note", "").strip(),
        "receipt": valid_receipt(form.get("receipt", "").strip()),
    }
    errors = []
    try:
        datetime.strptime(data["order_date"], "%Y-%m-%d")
    except ValueError:
        errors.append("Geçerli bir tarih girin.")
    if not data["supplier"]:
        errors.append("Firma seçin; firması olmayan sipariş fatura kontrolüne girmez.")
    units = fruit_units()
    try:
        data["vat_rate"] = parse_number(form.get("vat_rate"), 0.0)
    except ValueError:
        errors.append("KDV oranı sayı olmalıdır.")

    lines = []
    for i, (fruit, kg, price) in enumerate(
        zip(form.getlist("fruit"), form.getlist("kg"), form.getlist("unit_price")), start=1
    ):
        fruit, kg, price = fruit.strip(), kg.strip(), price.strip()
        if not (fruit or kg or price):
            continue
        fruit = fruit[:1].upper() + fruit[1:]
        try:
            kg_val, price_val = parse_number(kg), parse_number(price)
        except ValueError:
            errors.append(f"{i}. satır: miktar ve fiyat sayı olmalıdır.")
            continue
        unit = units.get(fruit, "kg")
        if not fruit:
            errors.append(f"{i}. satır: meyve adı boş.")
        elif kg_val <= 0:
            errors.append(f"{i}. satır ({fruit}): miktar sıfırdan büyük olmalıdır.")
        elif unit in COUNT_UNITS and kg_val != int(kg_val):
            errors.append(f"{i}. satır ({fruit}): {unit} olarak tam sayı girin ({format_number(kg_val)} değil).")
        elif price_val < 0:
            errors.append(f"{i}. satır ({fruit}): fiyat negatif olamaz.")
        else:
            lines.append({"fruit": fruit, "kg": kg_val, "unit_price": price_val})
    if not lines and not errors:
        errors.append("En az bir meyve satırı girin.")
    data["lines"] = lines
    return data, errors


def save_order(data, order_id=None):
    db = get_db()
    supplier_id = get_or_create("suppliers", data["supplier"]) if data["supplier"] else None
    values = (data["order_date"], supplier_id, data["vat_rate"], data["note"], data["receipt"])
    if order_id is None:
        order_id = db.execute(
            "INSERT INTO fruit_orders (order_date, supplier_id, vat_rate, note, receipt, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            values + (now(),),
        ).lastrowid
    else:
        db.execute(
            "UPDATE fruit_orders SET order_date=?, supplier_id=?, vat_rate=?, note=?, receipt=? WHERE id=?",
            values + (order_id,),
        )
        db.execute("DELETE FROM fruit_order_lines WHERE order_id = ?", (order_id,))
    for line in data["lines"]:
        get_or_create("fruits", line["fruit"])
        db.execute(
            "INSERT INTO fruit_order_lines (order_id, fruit, kg, unit_price) VALUES (?, ?, ?, ?)",
            (order_id, line["fruit"], line["kg"], line["unit_price"]),
        )
    db.commit()
    return order_id


def render_order_form(order, is_new):
    return render_template(
        "order_form.html",
        order=order,
        is_new=is_new,
        fruits=fruit_names(),
        units=fruit_units(),
        suppliers=supplier_names(),
        local_ready=yerel_okuyucu.AVAILABLE,
        claude_ready=fis_okuyucu.is_configured(),
    )


def form_state(data):
    """Hatalı gönderimde formu kullanıcının girdikleriyle yeniden doldurmak için."""
    form = request.form
    return {
        **data,
        "vat_rate": form.get("vat_rate", ""),
        "lines": [
            {"fruit": f, "kg": k, "unit_price": p}
            for f, k, p in zip(form.getlist("fruit"), form.getlist("kg"), form.getlist("unit_price"))
        ],
    }


@app.route("/meyveler", methods=["GET", "POST"])
def fruit_settings():
    db = get_db()
    if request.method == "POST":
        for row in db.execute("SELECT id FROM fruits").fetchall():
            unit = request.form.get(f"unit-{row['id']}")
            if unit in FRUIT_UNITS:
                db.execute("UPDATE fruits SET unit = ? WHERE id = ?", (unit, row["id"]))
        db.commit()
        flash("Meyve birimleri kaydedildi.", "success")
        target = request.form.get("next", "")
        # Yalnızca uygulama içi adreslere geri dön.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("fruit_settings")
        return redirect(target)
    fruits = db.execute("SELECT * FROM fruits ORDER BY sort_order, name COLLATE NOCASE").fetchall()
    return render_template("fruits.html", fruits=fruits, choices=FRUIT_UNITS, next=request.args.get("next", ""))


@app.route("/siparis/yeni", methods=["GET", "POST"])
def order_new():
    if request.method == "POST":
        data, errors = order_from_form()
        if not errors:
            save_order(data)
            flash(f"{format_date(data['order_date'])} siparişi kaydedildi.", "success")
            return redirect(url_for("orders", ay=data["order_date"][:7]))
        for e in errors:
            flash(e, "error")
        return render_order_form(form_state(data), is_new=True)

    order = {
        "order_date": datetime.now().strftime("%Y-%m-%d"),
        "supplier": "",
        "vat_rate": 1,
        "note": "",
        "receipt": None,
        "lines": [],
    }
    return render_order_form(order, is_new=True)


@app.route("/siparis/<int:order_id>", methods=["GET", "POST"])
def order_edit(order_id):
    found = load_orders("WHERE o.id = ?", (order_id,))
    if not found:
        flash("Sipariş bulunamadı.", "error")
        return redirect(url_for("orders"))
    order = found[0]

    if request.method == "POST":
        data, errors = order_from_form()
        if not errors:
            save_order(data, order_id)
            flash("Sipariş güncellendi.", "success")
            return redirect(url_for("orders", ay=data["order_date"][:7]))
        for e in errors:
            flash(e, "error")
        return render_order_form({**form_state(data), "id": order_id}, is_new=False)

    return render_order_form(order, is_new=False)


@app.route("/siparis/<int:order_id>/sil", methods=["POST"])
def order_delete(order_id):
    db = get_db()
    row = db.execute("SELECT order_date FROM fruit_orders WHERE id = ?", (order_id,)).fetchone()
    if row:
        db.execute("DELETE FROM fruit_orders WHERE id = ?", (order_id,))
        db.commit()
        flash("Sipariş silindi.", "success")
        return redirect(url_for("orders", ay=row["order_date"][:7]))
    return redirect(url_for("orders"))


@app.route("/siparis/fis-yukle", methods=["POST"])
def receipt_upload():
    """Fişi kaydeder ve ücretsiz yerel okuma (Apple Vision) ile okur.

    Okuma başarısız olsa da fiş siparişe eklenebilir; kullanıcı Claude ile yeniden okutabilir.
    """
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(error="Dosya seçilmedi."), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in RECEIPT_EXTS:
        return jsonify(error="Desteklenen biçimler: JPG, PNG, WEBP, GIF, PDF."), 400
    data = file.read()
    if len(data) > MAX_RECEIPT_BYTES:
        return jsonify(error="Dosya 20 MB'tan büyük olamaz."), 400

    os.makedirs(RECEIPT_DIR, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{os.urandom(4).hex()}{ext}"
    with open(os.path.join(RECEIPT_DIR, name), "wb") as f:
        f.write(data)

    result = {"receipt": name, "receipt_url": url_for("receipt_file", name=name)}
    if ext == ".pdf":
        result["error"] = "PDF fişler yerel olarak okunamıyor."
    elif not yerel_okuyucu.AVAILABLE:
        result["error"] = "Yerel okuma yalnızca macOS'ta çalışır."
    else:
        try:
            result["data"] = yerel_okuyucu.read_receipt(data, fruit_names())
        except yerel_okuyucu.YerelOkumaHatasi as exc:
            result["error"] = str(exc)
        except Exception:
            app.logger.exception("Fiş yerel olarak okunamadı")
            result["error"] = "Fiş okunurken beklenmeyen bir hata oluştu."
    return jsonify(result)


@app.route("/siparis/fis-claude", methods=["POST"])
def receipt_read_claude():
    """Daha önce yüklenmiş bir fişi Claude ile okur."""
    name = valid_receipt((request.get_json(silent=True) or {}).get("receipt", ""))
    if not name:
        return jsonify(error="Fiş bulunamadı; fişi yeniden yükleyin."), 400
    with open(os.path.join(RECEIPT_DIR, name), "rb") as f:
        data = f.read()
    try:
        return jsonify(data=fis_okuyucu.read_receipt(
            data,
            is_pdf=name.endswith(".pdf"),
            fruits=fruit_names(),
            units=fruit_units(),
            suppliers=supplier_names(),
            today=datetime.now().strftime("%Y-%m-%d"),
        ))
    except fis_okuyucu.FisOkumaHatasi as exc:
        return jsonify(error=str(exc))
    except Exception:
        app.logger.exception("Fiş Claude ile okunamadı")
        return jsonify(error="Fiş okunurken beklenmeyen bir hata oluştu. Bilgileri elle girin.")


@app.route("/fis/<path:name>")
def receipt_file(name):
    return send_from_directory(RECEIPT_DIR, name)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
