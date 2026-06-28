-- 0004: AR invoices (customer billing) + recruiting pipeline leads.

CREATE TABLE IF NOT EXISTS invoices (
    id             SERIAL PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    invoice_number TEXT,
    customer_id    INTEGER,
    load_id        INTEGER,
    amount         NUMERIC(12,2) DEFAULT 0,
    status         TEXT DEFAULT 'draft',   -- draft | sent | paid
    issue_date     DATE,
    due_date       DATE,
    paid_date      DATE,
    notes          TEXT,
    created_at     TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices(company_id);

CREATE TABLE IF NOT EXISTS recruiting_leads (
    id               SERIAL PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    phone            TEXT,
    email            TEXT,
    source           TEXT,
    stage            TEXT DEFAULT 'new',  -- new | contacted | screening | interview | offer | hired | rejected
    experience_years TEXT,
    cdl_class        TEXT,
    position         TEXT,
    notes            TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recruiting_company ON recruiting_leads(company_id);
