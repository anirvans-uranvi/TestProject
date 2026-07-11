-- seed.sql
-- Reference data only: current Nifty 50 constituents and their company
-- metadata, compiled from NSE/index-provider listings as of 2026-07-11.
-- NSE reconstitutes the index semi-annually (cutoffs Jan 31 / Jul 31) —
-- re-run scripts/fetch_nifty50_constituents.py periodically to refresh
-- this list; don't assume it stays accurate indefinitely.
--
-- Time-series data (prices, fundamentals, dividends, daily snapshots) is
-- NOT seeded here — run `python scripts/seed_mock_data.py` after applying
-- migrations to populate realistic mock history for local development.

insert into companies (symbol, name, sector, industry) values
    ('RELIANCE',   'Reliance Industries Ltd',              'Energy',              'Refineries / Oil & Gas'),
    ('HDFCBANK',   'HDFC Bank Ltd',                         'Financial Services',  'Private Bank'),
    ('BHARTIARTL', 'Bharti Airtel Ltd',                     'Telecommunication',   'Telecom Services'),
    ('ICICIBANK',  'ICICI Bank Ltd',                        'Financial Services',  'Private Bank'),
    ('SBIN',       'State Bank of India',                   'Financial Services',  'Public Bank'),
    ('TCS',        'Tata Consultancy Services Ltd',         'Information Technology', 'IT Services'),
    ('BAJFINANCE', 'Bajaj Finance Ltd',                      'Financial Services',  'NBFC'),
    ('LT',         'Larsen & Toubro Ltd',                    'Industrials',         'Engineering & Construction'),
    ('HINDUNILVR', 'Hindustan Unilever Ltd',                 'Consumer Staples',    'FMCG'),
    ('SUNPHARMA',  'Sun Pharmaceutical Industries Ltd',      'Healthcare',          'Pharmaceuticals'),
    ('MARUTI',     'Maruti Suzuki India Ltd',                'Consumer Discretionary', 'Passenger Cars'),
    ('INFY',       'Infosys Ltd',                            'Information Technology', 'IT Services'),
    ('ADANIPORTS', 'Adani Ports and Special Economic Zone Ltd', 'Industrials',      'Port Operations'),
    ('AXISBANK',   'Axis Bank Ltd',                          'Financial Services',  'Private Bank'),
    ('ADANIENT',   'Adani Enterprises Ltd',                  'Industrials',         'Diversified / Trading'),
    ('TITAN',      'Titan Company Ltd',                      'Consumer Discretionary', 'Jewellery & Watches'),
    ('M&M',        'Mahindra & Mahindra Ltd',                'Consumer Discretionary', 'Passenger & Commercial Vehicles'),
    ('KOTAKBANK',  'Kotak Mahindra Bank Ltd',                'Financial Services',  'Private Bank'),
    ('ITC',        'ITC Ltd',                                'Consumer Staples',    'FMCG / Tobacco'),
    ('ULTRACEMCO', 'UltraTech Cement Ltd',                   'Materials',           'Cement'),
    ('NTPC',       'NTPC Ltd',                               'Utilities',           'Power Generation'),
    ('HCLTECH',    'HCL Technologies Ltd',                   'Information Technology', 'IT Services'),
    ('ONGC',       'Oil & Natural Gas Corporation Ltd',      'Energy',              'Oil Exploration'),
    ('BAJAJFINSV', 'Bajaj Finserv Ltd',                      'Financial Services',  'Diversified Financials'),
    ('JSWSTEEL',   'JSW Steel Ltd',                          'Materials',           'Steel & Iron'),
    ('BEL',        'Bharat Electronics Ltd',                 'Industrials',         'Defence & Aerospace'),
    ('BAJAJ-AUTO', 'Bajaj Auto Ltd',                          'Consumer Discretionary', 'Two & Three Wheelers'),
    ('NESTLEIND',  'Nestle India Ltd',                       'Consumer Staples',    'FMCG / Food Products'),
    ('ETERNAL',    'Eternal Ltd',                            'Consumer Discretionary', 'E-Commerce / Food Delivery'),
    ('COALINDIA',  'Coal India Ltd',                          'Energy',              'Mining'),
    ('POWERGRID',  'Power Grid Corporation of India Ltd',    'Utilities',           'Power Transmission'),
    ('ASIANPAINT', 'Asian Paints Ltd',                        'Materials',           'Paints'),
    ('SHRIRAMFIN', 'Shriram Finance Ltd',                     'Financial Services',  'NBFC'),
    ('TATASTEEL',  'Tata Steel Ltd',                          'Materials',           'Steel & Iron'),
    ('GRASIM',     'Grasim Industries Ltd',                   'Materials',           'Diversified / Cement'),
    ('HINDALCO',   'Hindalco Industries Ltd',                 'Materials',           'Non-Ferrous Metals'),
    ('INDIGO',     'InterGlobe Aviation Ltd',                 'Industrials',         'Airlines'),
    ('EICHERMOT',  'Eicher Motors Ltd',                       'Consumer Discretionary', 'Two Wheelers'),
    ('SBILIFE',    'SBI Life Insurance Company Ltd',          'Financial Services',  'Insurance'),
    ('WIPRO',      'Wipro Ltd',                                'Information Technology', 'IT Services'),
    ('JIOFIN',     'Jio Financial Services Ltd',               'Financial Services',  'NBFC'),
    ('TRENT',      'Trent Ltd',                                'Consumer Discretionary', 'Retailing'),
    ('TECHM',      'Tech Mahindra Ltd',                        'Information Technology', 'IT Services'),
    ('APOLLOHOSP', 'Apollo Hospitals Enterprise Ltd',          'Healthcare',          'Hospitals'),
    ('TMPV',       'Tata Motors Passenger Vehicles Ltd',       'Consumer Discretionary', 'Passenger Vehicles'),
    ('HDFCLIFE',   'HDFC Life Insurance Co Ltd',               'Financial Services',  'Insurance'),
    ('CIPLA',      'Cipla Ltd',                                'Healthcare',          'Pharmaceuticals'),
    ('TATACONSUM', 'Tata Consumer Products Ltd',               'Consumer Staples',    'FMCG / Tea & Coffee'),
    ('MAXHEALTH',  'Max Healthcare Institute Ltd',              'Healthcare',          'Hospitals'),
    ('DRREDDY',    'Dr Reddys Laboratories Ltd',                'Healthcare',          'Pharmaceuticals')
on conflict (symbol) do update set
    name = excluded.name,
    sector = excluded.sector,
    industry = excluded.industry,
    updated_at = now();

insert into nifty50_constituents (symbol, company_name, sector, index_effective_from, is_current)
select symbol, name, sector, date '2026-07-11', true
from companies
on conflict (symbol, index_effective_from) do nothing;
