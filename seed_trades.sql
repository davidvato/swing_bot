-- seed_trades.sql: generado automaticamente por seed_db.py
-- Ejecutar solo si la DB esta vacia (lo hace dashboard_app.py al arrancar)

INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (1, '2026-08-31 09:30:32 EDT', 'NVDA', 'BUY', 15000.0, 216.999766, NULL, 69.124452436, NULL, NULL, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (2, '2026-08-31 09:30:33 EDT', 'AMD', 'BUY', 15000.0, 469.964928, NULL, 31.917254047, NULL, NULL, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (3, '2026-09-03 09:30:29 EDT', 'MSFT', 'BUY', 15001.15, 496.82, NULL, 30.19433597681253, NULL, NULL, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (4, '2026-09-03 09:30:30 EDT', 'PLTR', 'BUY', 15001.15, 169.46, NULL, 88.52325032456037, NULL, NULL, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (5, '2026-09-03 09:32:34 EDT', 'AMD', 'SELL_SL', 15000.0, 469.964928, 445.8934, 31.917254047, -768.2971, -0.05122, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (6, '2026-09-04 09:30:12 EDT', 'AMD', 'BUY', 15093.02, 456.16, NULL, 33.08711855489302, NULL, NULL, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (7, '2026-09-04 14:33:38 EDT', 'NVDA', 'SELL_TP', 15000.0, 216.999766, 230.285, 69.124452436, 918.3345, 0.061222, 0.15);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (8, '2026-09-04 17:45:17 EDT', 'AMD', 'SELL_EOW', 15093.01, 467.115915, 476.4, 32.311059266, 299.9786, 0.019875, NULL);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (9, '2026-09-04 17:45:17 EDT', 'MSFT', 'SELL_EOW', 15001.14, 508.162897, 499.4028, 29.520337047, -258.601, -0.017239, NULL);
INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES (10, '2026-09-04 17:45:17 EDT', 'PLTR', 'SELL_EOW', 15001.14, 175.58, 174.3983, 85.437635265, -100.9617, -0.00673, NULL);

