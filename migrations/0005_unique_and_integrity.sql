-- 0005: missing UNIQUE constraints that prevent duplicate-account / duplicate-load /
-- duplicate-KPI races (M2, M3, M4). Each index is created inside a PL/pgSQL DO block
-- that swallows unique_violation, so a dirty/duplicate LIVE database SKIPS the index
-- (with a NOTICE) instead of crashing startup. We NEVER delete or dedup rows here.
--
-- The app code (database.py) is defensive either way: it keeps a fast pre-check and
-- catches IntegrityError, so a skipped index only means "no DB-level race guard",
-- never a runtime crash.

-- M2: one dispatcher account per email (case-insensitive). NULL emails are allowed
-- to repeat (btree treats NULLs as distinct), so unconfigured rows are unaffected.
DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatchers_email_unique
        ON dispatchers (LOWER(email));
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'skipping idx_dispatchers_email_unique: duplicate dispatcher emails exist; resolve them, then re-run';
END$$;

-- M3: one load per (dispatcher, motive dispatch). Partial so manually-created loads
-- (motive_dispatch_id IS NULL) are never constrained.
DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS idx_loads_dispatcher_motive_unique
        ON loads (dispatcher_id, motive_dispatch_id)
        WHERE motive_dispatch_id IS NOT NULL;
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'skipping idx_loads_dispatcher_motive_unique: duplicate motive_dispatch_id rows exist; resolve them, then re-run';
END$$;

-- M4: one KPI entry per (company, load). Partial so entries without a load
-- (load_id IS NULL) are never constrained.
DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS idx_kpi_company_load_unique
        ON kpi_entries (company_id, load_id)
        WHERE load_id IS NOT NULL;
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'skipping idx_kpi_company_load_unique: duplicate (company_id, load_id) KPI rows exist; resolve them, then re-run';
END$$;

-- L11 (generated money/mileage overflow) — NOTE / intentionally skipped.
-- loads.total_rate_num / charge_num are NUMERIC(12,2) and miles_num /
-- deadhead_miles_num are NUMERIC(10,1) GENERATED STORED columns (see 0002). A
-- pathological free-text token (>10 integer digits) overflows the cast and aborts
-- the load INSERT. Widening these would require dropping & recomputing STORED
-- generated columns (a full table rewrite with version-dependent restrictions),
-- which risks data on a live DB. Per the audit guidance we DO NOT widen here; the
-- safer real fix is to clamp/validate the rate token in the extractor/write path.
