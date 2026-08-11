-- 0019_add_bankex.sql
--
-- Adds BANKEX (BSE Bank index) as a fourth `company_type = 'Index'` row,
-- alongside the three migration 0018 already seeds (NIFTY, BANKNIFTY,
-- SENSEX). Unlike SENSEX, BANKEX now has a real data source behind it:
-- src/data_providers/bse_fo_provider.py confirms BSE's own F&O bhavcopy
-- carries live BANKEX index-option rows (same UDiFF format NSE uses) --
-- see that module's file header. Split into its own migration rather
-- than amending 0018 because 0018 has already been applied.

insert into companies (symbol, name, company_type)
values
    ('BANKEX', 'BSE Bankex Index', 'Index')
on conflict (symbol) do update set company_type = excluded.company_type;
