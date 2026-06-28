-- 0003: real fleet assets — drivers (roster), trucks (power units), assignments.
-- Cross refs (drivers.truck_id, trucks.current_driver_id) are soft INTEGER refs
-- to avoid a circular FK; company_id keeps a hard FK.

CREATE TABLE IF NOT EXISTS drivers (
    id              SERIAL PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    dba             TEXT,
    status          TEXT DEFAULT 'active',        -- active | on_leave | inactive
    driver_type     TEXT DEFAULT 'company',       -- company | owner_operator
    truck_id        INTEGER,
    phone           TEXT,
    email           TEXT,
    cdl_number      TEXT,
    cdl_class       TEXT,
    cdl_state       TEXT,
    cdl_expiry      DATE,
    medical_expiry  DATE,
    pay_type        TEXT,                          -- mileage | percentage | salary
    pay_rate        NUMERIC(10,2),
    driver_group_id INTEGER REFERENCES driver_groups(id) ON DELETE SET NULL,
    notes           TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_drivers_company ON drivers(company_id);
CREATE INDEX IF NOT EXISTS idx_drivers_cdl_expiry ON drivers(cdl_expiry);
CREATE INDEX IF NOT EXISTS idx_drivers_medical_expiry ON drivers(medical_expiry);

CREATE TABLE IF NOT EXISTS trucks (
    id                SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    unit_number       TEXT NOT NULL,
    status            TEXT DEFAULT 'active',        -- active | inactive | in_shop
    make              TEXT,
    model             TEXT,
    year              TEXT,
    vin               TEXT,
    license_plate     TEXT,
    license_state     TEXT,
    ownership         TEXT DEFAULT 'company',       -- company | lease | owner_operator
    odometer          NUMERIC(12,1),
    eld_unit_ref      TEXT,
    current_driver_id INTEGER,
    notes             TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trucks_company ON trucks(company_id);

CREATE TABLE IF NOT EXISTS assignments (
    id            SERIAL PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    truck_id      INTEGER,
    driver_id     INTEGER,
    co_driver_id  INTEGER,
    trailer_id    INTEGER,
    group_name    TEXT,
    status        TEXT DEFAULT 'active',
    notes         TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_assignments_company ON assignments(company_id);
