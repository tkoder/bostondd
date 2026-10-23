CREATE TABLE "fruit_order_lines" (
	"id" serial PRIMARY KEY,
	"order_id" integer NOT NULL,
	"fruit" text NOT NULL,
	"kg" double precision NOT NULL,
	"unit_price" double precision DEFAULT 0 NOT NULL,
	CONSTRAINT "fruit_order_lines_kg_check" CHECK ("kg" > 0)
);
--> statement-breakpoint
CREATE TABLE "fruit_orders" (
	"id" serial PRIMARY KEY,
	"order_date" date NOT NULL,
	"supplier_id" integer,
	"vat_rate" double precision DEFAULT 1 NOT NULL,
	"note" text DEFAULT '' NOT NULL,
	"receipt" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "fruits" (
	"id" serial PRIMARY KEY,
	"name" text NOT NULL UNIQUE,
	"sort_order" integer DEFAULT 100 NOT NULL,
	"unit" text DEFAULT 'kg' NOT NULL
);
--> statement-breakpoint
CREATE TABLE "movements" (
	"id" serial PRIMARY KEY,
	"product_id" integer NOT NULL,
	"type" text NOT NULL,
	"quantity" double precision NOT NULL,
	"note" text DEFAULT '' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "movements_type_check" CHECK ("type" IN ('IN', 'OUT')),
	CONSTRAINT "movements_quantity_check" CHECK ("quantity" > 0)
);
--> statement-breakpoint
CREATE TABLE "products" (
	"id" serial PRIMARY KEY,
	"sku" text NOT NULL UNIQUE,
	"name" text NOT NULL,
	"category" text DEFAULT '' NOT NULL,
	"unit" text DEFAULT 'adet' NOT NULL,
	"price" double precision DEFAULT 0 NOT NULL,
	"min_stock" double precision DEFAULT 0 NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "supplier_invoices" (
	"supplier_id" integer,
	"month" text,
	"amount" double precision NOT NULL,
	"invoice_no" text DEFAULT '' NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "supplier_invoices_pkey" PRIMARY KEY("supplier_id","month")
);
--> statement-breakpoint
CREATE TABLE "suppliers" (
	"id" serial PRIMARY KEY,
	"name" text NOT NULL UNIQUE
);
--> statement-breakpoint
CREATE INDEX "idx_fruit_order_lines_order" ON "fruit_order_lines" ("order_id");--> statement-breakpoint
CREATE INDEX "idx_fruit_orders_date" ON "fruit_orders" ("order_date");--> statement-breakpoint
CREATE INDEX "idx_movements_product" ON "movements" ("product_id");--> statement-breakpoint
ALTER TABLE "fruit_order_lines" ADD CONSTRAINT "fruit_order_lines_order_id_fruit_orders_id_fkey" FOREIGN KEY ("order_id") REFERENCES "fruit_orders"("id") ON DELETE CASCADE;--> statement-breakpoint
ALTER TABLE "fruit_orders" ADD CONSTRAINT "fruit_orders_supplier_id_suppliers_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "suppliers"("id");--> statement-breakpoint
ALTER TABLE "movements" ADD CONSTRAINT "movements_product_id_products_id_fkey" FOREIGN KEY ("product_id") REFERENCES "products"("id") ON DELETE CASCADE;--> statement-breakpoint
ALTER TABLE "supplier_invoices" ADD CONSTRAINT "supplier_invoices_supplier_id_suppliers_id_fkey" FOREIGN KEY ("supplier_id") REFERENCES "suppliers"("id");