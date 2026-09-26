-- P4.3 themes for level-2 risk budget (§19.2, <= 3% of satellite capital per theme).
-- Status [W]: proposed by review-fable 2026-09-26, accepted by main session as
-- working version; owner approval pending (brief CC-P §5). Idempotent.
UPDATE instruments i SET theme = t.theme
FROM (VALUES
  ('ACMR','semi/AI-infra'),('APH','semi/AI-infra'),('ASML','semi/AI-infra'),('BESI','semi/AI-infra'),
  ('IFX','semi/AI-infra'),('MKSI','semi/AI-infra'),('NVDA','semi/AI-infra'),('ORCL','semi/AI-infra'),
  ('ACP','GPW-tech'),('CBF','GPW-tech'),('SGN','GPW-tech'),('SHO','GPW-tech'),('TXT','GPW-tech'),
  ('VRC','GPW-tech'),('FCDRZ26','GPW-tech'),
  ('CSU','software'),('NOW','software'),('RBRK','software'),('TEM','software'),
  ('AMZN','platformy-US'),('META','platformy-US'),('NFLX','platformy-US'),('UBER','platformy-US'),
  ('COIN','krypto'),('CRCL','krypto'),('ETFBTCPL','krypto'),('MARA','krypto'),
  ('AXON','przemysl'),('FTAI','przemysl'),('RKLB','przemysl'),('RRX','przemysl'),
  ('BABA','Azja-platformy'),('GRAB','Azja-platformy'),('SE','Azja-platformy'),
  ('DLO','fintech-LatAm'),('MELI','fintech-LatAm'),('NU','fintech-LatAm'),
  ('ADYEN','finanse-DM'),('KKR','finanse-DM'),('KLAR','finanse-DM'),
  ('ANR','GPW-konsumpcja'),('MDV','GPW-konsumpcja'),('FMDVZ26','GPW-konsumpcja'),
  ('CPR','GPW-przemysl/energia'),('NWG','GPW-przemysl/energia'),('FPGEZ26','GPW-przemysl/energia'),
  ('4MS','GPW-small'),('GMT','GPW-small'),('XTP','GPW-small'),
  ('MBLY','auto/autonomia'),('TSLA','auto/autonomia'),
  ('NVO','zdrowie'),('SLV','zdrowie')
) AS t(ticker, theme)
WHERE i.broker_ticker = t.ticker;
