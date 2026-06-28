-- 0002: typed, indexed money/mileage columns auto-derived from the legacy TEXT
-- columns via STORED generated columns. Backfills existing rows automatically and
-- requires no write-path changes (they keep writing the TEXT columns).
ALTER TABLE loads ADD COLUMN IF NOT EXISTS total_rate_num NUMERIC(12,2)
  GENERATED ALWAYS AS (
    COALESCE(NULLIF(regexp_replace(substring(total_rate_usd FROM '[0-9][0-9,]*\.?[0-9]*'), ',', '', 'g'), '')::numeric, 0)
  ) STORED;
ALTER TABLE loads ADD COLUMN IF NOT EXISTS charge_num NUMERIC(12,2)
  GENERATED ALWAYS AS (
    COALESCE(NULLIF(regexp_replace(substring(charge FROM '[0-9][0-9,]*\.?[0-9]*'), ',', '', 'g'), '')::numeric, 0)
  ) STORED;
ALTER TABLE loads ADD COLUMN IF NOT EXISTS miles_num NUMERIC(10,1)
  GENERATED ALWAYS AS (
    COALESCE(NULLIF(regexp_replace(substring(miles FROM '[0-9][0-9,]*\.?[0-9]*'), ',', '', 'g'), '')::numeric, 0)
  ) STORED;
ALTER TABLE loads ADD COLUMN IF NOT EXISTS deadhead_miles_num NUMERIC(10,1)
  GENERATED ALWAYS AS (
    COALESCE(NULLIF(regexp_replace(substring(deadhead_miles FROM '[0-9][0-9,]*\.?[0-9]*'), ',', '', 'g'), '')::numeric, 0)
  ) STORED;
CREATE INDEX IF NOT EXISTS idx_loads_total_rate_num ON loads(total_rate_num);
