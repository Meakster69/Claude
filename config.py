import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

INTRADAY_API_KEY = os.environ["INTRADAY_API_KEY"]
INTRADAY_SECRET_KEY = os.environ["INTRADAY_SECRET_KEY"]
INTRADAY_BASE_URL = os.getenv("INTRADAY_BASE_URL", "https://paper-api.alpaca.markets")
