-- Excel'deki sütun sırası; yeni meyveler bunların arkasına eklenir.
INSERT INTO "fruits" ("name", "sort_order", "unit") VALUES
  ('Çilek', 0, 'kg'),
  ('Muz', 1, 'kg'),
  ('Ananas', 2, 'adet'),
  ('Limon', 3, 'kg'),
  ('Portakal', 4, 'kg')
ON CONFLICT ("name") DO NOTHING;
--> statement-breakpoint
INSERT INTO "suppliers" ("name") VALUES
  ('Çilek Tarım - İSMAİL ÇARBOĞA'),
  ('Yılmaz Kardeşler Tarım'),
  ('Taşdemirler Gıda - Recep Çatan Recoba')
ON CONFLICT ("name") DO NOTHING;
