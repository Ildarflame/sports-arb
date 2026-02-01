from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Kalshi
    kalshi_email: str = ""
    kalshi_password: str = ""
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"

    # Polymarket
    polymarket_gamma_api: str = "https://gamma-api.polymarket.com"
    polymarket_clob_api: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # App
    db_path: str = "sports_arb.db"
    host: str = "0.0.0.0"
    port: int = 8000
    poll_interval: int = 10
    min_arb_percent: float = 0.5
    max_arb_percent: float = 50.0
    min_volume: int = 0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
