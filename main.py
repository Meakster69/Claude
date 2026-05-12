from alpaca.trading.client import TradingClient
import config


def show(label: str, api_key: str, secret_key: str) -> None:
    client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
    account = client.get_account()
    print(f"[{label}]")
    print(f"  Account status : {account.status}")
    print(f"  Buying power   : ${float(account.buying_power):,.2f}")
    print(f"  Portfolio value: ${float(account.portfolio_value):,.2f}")


show("main paper", config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
show("intraday Funding", config.INTRADAY_API_KEY, config.INTRADAY_SECRET_KEY)
