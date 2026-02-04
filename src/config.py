from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_email: str = ""
    kalshi_password: str = ""
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"

    # Polymarket
    polymarket_gamma_api: str = "https://gamma-api.polymarket.com"
    polymarket_clob_api: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Polymarket trading
    poly_private_key: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_funder_address: str = ""

    # Telegram notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # App
    db_path: str = "sports_arb.db"
    host: str = "0.0.0.0"
    port: int = 8000
    poll_interval: int = 10
    min_arb_percent: float = 0.5
    max_arb_percent: float = 50.0
    min_volume: int = 0

    # Live mode settings
    allow_live_arbs: bool = True
    live_min_confidence: str = "high"
    live_max_spread_pct: float = 10.0
    live_max_roi: float = 50.0

    # Executor settings
    executor_enabled: bool = False
    executor_min_bet: float = 2.5  # Minimum $2.50 ensures both legs >= $1
    executor_max_bet: float = 5.0  # Increased to allow reasonable arbs
    executor_min_roi: float = 1.0
    executor_max_roi: float = 50.0
    executor_max_daily_trades: int = 50
    executor_max_daily_loss: float = 5.0
    executor_min_platform_balance: float = 1.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
