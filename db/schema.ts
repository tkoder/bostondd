import { sql } from "drizzle-orm";
import {
  check, date, doublePrecision, index, integer, pgTable, primaryKey, serial, text, timestamp,
} from "drizzle-orm/pg-core";

export const products = pgTable("products", {
  id: serial().primaryKey(),
  sku: text().notNull().unique(),
  name: text().notNull(),
  category: text().notNull().default(""),
  unit: text().notNull().default("adet"),
  price: doublePrecision().notNull().default(0),
  minStock: doublePrecision("min_stock").notNull().default(0),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const movements = pgTable(
  "movements",
  {
    id: serial().primaryKey(),
    productId: integer("product_id").notNull().references(() => products.id, { onDelete: "cascade" }),
    type: text().notNull(),
    quantity: doublePrecision().notNull(),
    note: text().notNull().default(""),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    index("idx_movements_product").on(t.productId),
    check("movements_type_check", sql`${t.type} IN ('IN', 'OUT')`),
    check("movements_quantity_check", sql`${t.quantity} > 0`),
  ],
);

export const suppliers = pgTable("suppliers", {
  id: serial().primaryKey(),
  name: text().notNull().unique(),
});

export const fruits = pgTable("fruits", {
  id: serial().primaryKey(),
  name: text().notNull().unique(),
  sortOrder: integer("sort_order").notNull().default(100),
  unit: text().notNull().default("kg"),
});

export const fruitOrders = pgTable(
  "fruit_orders",
  {
    id: serial().primaryKey(),
    orderDate: date("order_date", { mode: "string" }).notNull(),
    supplierId: integer("supplier_id").references(() => suppliers.id),
    vatRate: doublePrecision("vat_rate").notNull().default(1),
    note: text().notNull().default(""),
    receipt: text(),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("idx_fruit_orders_date").on(t.orderDate)],
);

export const fruitOrderLines = pgTable(
  "fruit_order_lines",
  {
    id: serial().primaryKey(),
    orderId: integer("order_id").notNull().references(() => fruitOrders.id, { onDelete: "cascade" }),
    fruit: text().notNull(),
    kg: doublePrecision().notNull(),
    unitPrice: doublePrecision("unit_price").notNull().default(0),
  },
  (t) => [
    index("idx_fruit_order_lines_order").on(t.orderId),
    check("fruit_order_lines_kg_check", sql`${t.kg} > 0`),
  ],
);

// Tedarikçinin ay sonunda gönderdiği fatura; kendi kayıtlarımızla karşılaştırmak için.
export const supplierInvoices = pgTable(
  "supplier_invoices",
  {
    supplierId: integer("supplier_id").notNull().references(() => suppliers.id),
    month: text().notNull(),
    amount: doublePrecision().notNull(),
    invoiceNo: text("invoice_no").notNull().default(""),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [primaryKey({ columns: [t.supplierId, t.month] })],
);
