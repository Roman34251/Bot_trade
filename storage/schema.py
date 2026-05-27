CREATE_TABLES_SQL = """

CREATE TABLE IF NOT EXISTS ohlcv (
    id          BIGSERIAL PRIMARY KEY,
    exchange    VARCHAR(20)   NOT NULL,
    symbol      VARCHAR(20)   NOT NULL,
    timeframe   VARCHAR(5)    NOT NULL,
    timestamp   TIMESTAMPTZ   NOT NULL,
    open        NUMERIC(20,8) NOT NULL,
    high        NUMERIC(20,8) NOT NULL,
    low         NUMERIC(20,8) NOT NULL,
    close       NUMERIC(20,8) NOT NULL,
    volume      NUMERIC(30,8) NOT NULL,
    created_at  TIMESTAMPTZ   DEFAULT NOW(),
    UNIQUE(exchange, symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup
    ON ohlcv(exchange, symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS indicators (
    id               BIGSERIAL PRIMARY KEY,
    exchange         VARCHAR(20)   NOT NULL,
    symbol           VARCHAR(20)   NOT NULL,
    timeframe        VARCHAR(5)    NOT NULL,
    timestamp        TIMESTAMPTZ   NOT NULL,
    vwap_session     NUMERIC(20,8),
    vwap_weekly      NUMERIC(20,8),
    vwap_bias        VARCHAR(10),
    poc              NUMERIC(20,8),
    vah              NUMERIC(20,8),
    val              NUMERIC(20,8),
    cvd              NUMERIC(30,8),
    cvd_signal       VARCHAR(20),
    composite_signal VARCHAR(10),
    signal_strength  NUMERIC(5,2),
    created_at       TIMESTAMPTZ   DEFAULT NOW(),
    UNIQUE(exchange, symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_indicators_lookup
    ON indicators(exchange, symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS trades (
    id            BIGSERIAL PRIMARY KEY,
    trade_id      VARCHAR(50)   UNIQUE,
    exchange      VARCHAR(20)   NOT NULL,
    symbol        VARCHAR(20)   NOT NULL,
    direction     VARCHAR(5)    NOT NULL,
    mode          VARCHAR(10),
    entry_price   NUMERIC(20,8) NOT NULL,
    exit_price    NUMERIC(20,8),
    stop_loss     NUMERIC(20,8) NOT NULL,
    take_profit   NUMERIC(20,8) NOT NULL,
    quantity      NUMERIC(20,8) NOT NULL,
    pnl_usdt      NUMERIC(20,8),
    pnl_pct       NUMERIC(10,4),
    entry_reason  JSONB,
    status        VARCHAR(20)   DEFAULT 'open',
    opened_at     TIMESTAMPTZ   DEFAULT NOW(),
    closed_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE UNIQUE   NOT NULL,
    total_trades   INT           DEFAULT 0,
    winning_trades INT           DEFAULT 0,
    losing_trades  INT           DEFAULT 0,
    gross_pnl      NUMERIC(20,8) DEFAULT 0,
    net_pnl        NUMERIC(20,8) DEFAULT 0,
    fees_paid      NUMERIC(20,8) DEFAULT 0,
    win_rate       NUMERIC(5,2),
    avg_rr         NUMERIC(5,2),
    max_drawdown   NUMERIC(10,4),
    bot_paused     BOOLEAN       DEFAULT FALSE,
    created_at     TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fetch_progress (
    id              SERIAL PRIMARY KEY,
    exchange        VARCHAR(20) NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    timeframe       VARCHAR(5)  NOT NULL,
    last_fetched_at TIMESTAMPTZ,
    total_candles   INT         DEFAULT 0,
    is_complete     BOOLEAN     DEFAULT FALSE,
    UNIQUE(exchange, symbol, timeframe)
);

"""
