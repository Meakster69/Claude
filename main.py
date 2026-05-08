from alpaca.trading.client import TradingClient
import config

client = TradingClient(
    api_key=config.ALPACA_API_KEY,
    secret_key=config.ALPACA_SECRET_KEY,
    paper=True,
)

account = client.get_account()
print(f"Account status : {account.status}")
print(f"Buying power   : ${float(account.buying_power):,.2f}")
print(f"Portfolio value: ${float(account.portfolio_value):,.2f}")
