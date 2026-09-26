-- P5.2/P5.3: archetypes from classification key §14.1 v2 (2026-09-26).
-- Answers by review-fable (claude-fable-5-1); thresholds: recurring >= 70 %
-- (software incl. contracted usage-based), capex/revenue > 10 % or backlog /
-- order intake / book-to-bill as KPI, top decile = mcap >= 30 bn USD (US, EU/CA)
-- or >= 5 bn PLN (GPW), EBIT = yfinance Operating Income > 0 for 3 FY.
-- v2 order: 1-6, 7 (A2/A3), 9 (A1), 8 (A9), else A0.
-- source_tags: Z = yfinance measured, W = business-model judgement, N = unknown.
-- Status [W]; owner approval pending. Idempotent.
INSERT INTO archetype_assignments (instrument_id, archetype, archetype_secondary, answers,
    resolution, key_version, source_tags, valid_from, reason)
SELECT i.id, v.arch, v.sec, v.answers, v.res, 'v2', v.tags, DATE '2026-09-26', v.reason
FROM (VALUES
  ('NOW','A2',NULL,'NNNNNNT--','q7','W,Z','subscription ~97 % rev, OpInc > 0'),
  ('META','A1',NULL,'NNNNNNNTT','q9','Z','mcap 1.91 T, OpInc > 0 3y; capex/rev 34.7 %'),
  ('SE','A4',NULL,'NNNNNT---','q6','W','Shopee ~65-70 % rev dominant'),
  ('MELI','A4','A5','NNNNNT---','segment_gross_profit','W,N','commerce ~57 % / fintech ~43 % rev, segment GP not reported'),
  ('NU','A5',NULL,'NNT------','q3','W,Z','digital bank'),
  ('MARA','A8',NULL,'NT-------','q2','W,Z','~50k BTC on balance sheet'),
  ('KKR','A7','A5','NNNT-----','segment_gross_profit','W','FRE > insurance operating earnings'),
  ('AMZN','A1','A4','NNNNNNNTT','q9','Z,W','capex/rev 18.4 %; AWS as IaaS not counted as software'),
  ('BABA','A4','A9','NNNNNT---','q6','W,Z','Taobao/Tmall > 60 % profit'),
  ('UBER','A4',NULL,'NNNNNT---','q6','W','gross bookings x take-rate'),
  ('RBRK','A3',NULL,'NNNNNNT--','q7','W,Z','subscription ~95 %, OpInc < 0'),
  ('DLO','A6',NULL,'NNNNT----','q5','W','TPV x take-rate'),
  ('CRCL','A0',NULL,'NN?NNNNNN','none','W,Z','reserve interest without credit losses; mcap 24.3 bn'),
  ('ACMR','A0',NULL,'NNNNNNNNN','none','Z,W','capex/rev 6.4 %, no backlog KPI; K says A9 (calibration miss)'),
  ('NVO','A1',NULL,'NNNNNNNTT','q9','Z','mcap 171 bn, OpInc > 0 3y'),
  ('GRAB','A4',NULL,'NNNNNT---','q6','W','GMV x take-rate'),
  ('TSLA','A1',NULL,'NNNNNNNNT','q9','Z','mcap 1.47 T, OpInc > 0 3y; capex/rev 9.0 %'),
  ('FTAI','A9',NULL,'NNNNNNNTN','q8','Z','capex/rev 27.4 %; mcap 18.0 bn'),
  ('NVDA','A1',NULL,'NNNNNNNNT','q9','Z','mcap 5.43 T'),
  ('RKLB','A9',NULL,'NNNNNNNTN','q8','Z,W','capex/rev 26.0 %, backlog reported'),
  ('AXON','A0',NULL,'NNNNNNN?N','none','W,Z','recurring ~45 %, RPO not backlog, OpInc 2025 < 0'),
  ('APH','A1',NULL,'NNNNNNNNT','q9','Z','mcap 207 bn'),
  ('NFLX','A1',NULL,'NNNNNNNNT','q9','Z,W','streaming excluded from q7'),
  ('ORCL','A1',NULL,'NNNNNNNTT','q9','Z,W','strict software share ~55 % < 70 %; capex/rev 82.6 %'),
  ('MKSI','A0',NULL,'NNNNNNNNN','none','Z,W','mcap 17.6 bn, no backlog KPI; K says A9 (calibration miss)'),
  ('TEM','A0',NULL,'NNNNNNN?N','none','W,Z','recurring < 70 %, RPO not backlog'),
  ('KLAR','A5',NULL,'NNT------','q3','W','BNPL credit book'),
  ('COIN','A4','A7','NNNNNT---','segment_gross_profit','W,N','transaction ~54 % / S&S ~46 % rev'),
  ('MBLY','A0',NULL,'NNNNNNNNN','none','Z,W','design wins not backlog, OpInc < 0'),
  ('RRX','A9',NULL,'NNNNNNNTN','q8','W,Z','orders reported as KPI; mcap 10.4 bn'),
  ('CSU','A2',NULL,'NNNNNNT--','q7','W,Z','maintenance & recurring ~72 %'),
  ('BESI','A9',NULL,'NNNNNNNTN','q8','W,Z','backlog reported quarterly'),
  ('ADYEN','A6',NULL,'NNNNT----','q5','W','processed volume x take-rate'),
  ('ASML','A1',NULL,'NNNNNNNTT','q9','Z','EUR 585 bn, OpInc > 0 3y'),
  ('IFX','A1',NULL,'NNNNNNNTT','q9','Z','~USD 86 bn, OpInc > 0 3y; K says A9 (calibration miss)'),
  ('CBF','A2',NULL,'NNNNNNT--','q7','W,Z','hosting + CPaaS both software'),
  ('SGN','A9',NULL,'NNNNNNNTN','q8','W,Z','order book reported'),
  ('CPR','A9',NULL,'NNNNNNNTN','q8','W,Z','construction order book; Compremum S.A. (K lists CPR as games - error in K)'),
  ('XTP','A9',NULL,'NNNNNNNTN','q8','W,Z','order backlog published'),
  ('VRC','A2',NULL,'NNNNNNT--','q7','W,Z','CPaaS usage-based contracted + subscription'),
  ('ETFBTCPL','poza_14',NULL,'T--------','q1','Z','ETF - outside §14'),
  ('ANR','A0',NULL,'NNNNNNNNN','none','W,Z','1P e-commerce with inventory'),
  ('NWG','A9',NULL,'NNNNNNNTN','q8','W,Z','order book; mcap 4.68 bn PLN'),
  ('MDV','A1',NULL,'NNNNNNNNT','q9','Z','mcap 7.09 bn PLN >= 5 bn, OpInc > 0 3y'),
  ('ACP','A1',NULL,'NNNNNNNTT','q9','Z,W','17.7 bn PLN, OpInc > 0 3y'),
  ('4MS','A0',NULL,'NNNNNNNNN','none','W,Z','cosmetics'),
  ('SLV','A9',NULL,'NNNNNNNTN','q8','W,Z','CRO backlog reported'),
  ('TXT','A2',NULL,'NNNNNNT--','q7','W,Z','SaaS ~100 % subscription'),
  ('GMT','A0',NULL,'NNNNNNN?N','none','Z,W','revenue ~0, capex/rev degenerate'),
  ('FPGEZ26','poza_14','A9','T--------','q1','Z','future - outside §14; base PGE.WA capex/rev 18.8 %'),
  ('FCDRZ26','poza_14','A1','T--------','q1','Z','future - outside §14; base CDR.WA 24.4 bn PLN'),
  ('FMDVZ26','poza_14','A1','T--------','q1','Z','future - outside §14; base = MDV')
) AS v(ticker, arch, sec, answers, res, tags, reason)
JOIN instruments i ON i.broker_ticker = v.ticker
ON CONFLICT (instrument_id, valid_from) DO UPDATE SET
  archetype = EXCLUDED.archetype, archetype_secondary = EXCLUDED.archetype_secondary,
  answers = EXCLUDED.answers, resolution = EXCLUDED.resolution,
  key_version = EXCLUDED.key_version, source_tags = EXCLUDED.source_tags, reason = EXCLUDED.reason;
