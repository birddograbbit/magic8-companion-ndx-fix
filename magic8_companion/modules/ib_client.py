import asyncio
from typing import List, Dict, Optional
from ib_async import IB, Stock, Option, MarketOrder, Contract, util, Position, OptionChain, Ticker, Index
from ..unified_config import settings
from .ib_oi_fetcher import IBOpenInterestFetcher  # Import the OI fetcher
import logging

logger = logging.getLogger(__name__)

# Import the singleton connection manager
from .ib_client_manager import get_ib_connection, disconnect_ib, is_ib_connected

class IBClient:
    def __init__(self, host: str = settings.ib_host, port: int = settings.ib_port, client_id: int = settings.ib_client_id):
        # Don't create our own IB instance - use the singleton
        self.host = host
        self.port = port
        self.client_id = client_id
        self.oi_fetcher = None  # Will be initialized after connection

    async def _ensure_connected(self):
        """Ensure we have a connection from the singleton."""
        ib = await get_ib_connection()
        if not ib:
            raise ConnectionError("Failed to connect to IB or not currently connected.")
        
        # Initialize OI fetcher after successful connection if needed
        if settings.enable_oi_streaming and not self.oi_fetcher:
            self.oi_fetcher = IBOpenInterestFetcher(ib)
            logger.info("OI streaming enabled")
            
        return ib
    
    @property
    def ib(self):
        """Property to get the IB instance (for backward compatibility)."""
        # This is a bit of a hack but maintains compatibility
        # In async context, use _ensure_connected() instead
        from .ib_client_manager import _ib_connection
        return _ib_connection._ib

    async def disconnect(self):
        """Disconnect from IB using singleton."""
        await disconnect_ib()
        self.oi_fetcher = None

    async def get_positions(self) -> List[Dict]:
        """Return list of open option positions"""
        ib = await self._ensure_connected()
        positions_data = []

        ib_positions: List[Position] = ib.positions()

        for pos in ib_positions:
            contract: Contract = pos.contract
            if contract.secType == 'OPT':
                positions_data.append({
                    'symbol': contract.symbol,
                    'conId': contract.conId,
                    'strike': contract.strike,
                    'right': contract.right, # 'C' or 'P'
                    'expiry': contract.lastTradeDateOrContractMonth,
                    'multiplier': int(contract.multiplier) if contract.multiplier else 100,
                    'quantity': pos.position,
                    'average_cost': pos.avgCost,
                    # Market price and value might require fetching tickers if not directly available
                    # For simplicity, we'll rely on avgCost for now for P&L,
                    # but real P&L requires current market price.
                    # 'market_price': 0, # Placeholder, would need ib.reqMktData
                    # 'market_value': 0, # Placeholder
                    # 'unrealized_pnl': 0, # Placeholder, (market_price * quantity * mult) - (avg_cost * quantity * mult)
                })
        return positions_data

    async def qualify_underlying_with_fallback(self, symbol_name: str) -> Optional[Contract]:
        """Try multiple symbol variations and exchanges to qualify underlying contract."""
        ib = await self._ensure_connected()
        
        # Symbol variations to try (e.g., SPX -> SPXW for 0DTE)
        symbol_variations = {
            'SPX': ['SPX', 'SPXW'],
            'RUT': ['RUT'],
            'SPY': ['SPY'],
            'QQQ': ['QQQ'],
            'IWM': ['IWM'],
            'VIX': ['VIX'],
            'NDX': ['NDX']  # Added NDX
        }.get(symbol_name, [symbol_name])
        
        # Exchange preferences
        exchange_map = {
            'SPX': ['CBOE', 'SMART'],
            'SPXW': ['CBOE', 'SMART'],
            'RUT': ['SMART', 'CBOE', 'RUSSELL'],
            'SPY': ['SMART', 'CBOE', 'ARCA', 'BATS'],
            'QQQ': ['SMART', 'NASDAQ', 'CBOE'],
            'IWM': ['SMART', 'ARCA', 'CBOE'],
            'VIX': ['CBOE', 'SMART'],
            'NDX': ['NASDAQ', 'SMART']  # Added NDX with NASDAQ as primary exchange
        }
        
        for sym_variant in symbol_variations:
            exchanges = exchange_map.get(sym_variant, ['SMART'])
            
            for exchange in exchanges:
                try:
                    # Create appropriate contract type
                    if symbol_name in ['SPX', 'RUT', 'VIX', 'NDX']:  # Added NDX to Index list
                        underlying_contract = Index(sym_variant, exchange, 'USD')
                    else:
                        underlying_contract = Stock(sym_variant, exchange, 'USD')
                    
                    # Try to qualify
                    qualified = await ib.qualifyContractsAsync(underlying_contract)
                    if qualified and qualified[0].conId:
                        logger.info(f"Qualified {symbol_name} as {sym_variant} on {exchange}")
                        return qualified[0]
                    
                except Exception as e:
                    logger.debug(f"Failed to qualify {sym_variant} on {exchange}: {e}")
                    continue
        
        logger.error(f"Failed to qualify {symbol_name} with any symbol/exchange combination")
        return None

    async def qualify_option_with_fallback(self, symbol_name: str, expiry_date: str, strike: float, right: str, trading_class: str = None) -> Optional[Contract]:
        """Try multiple symbols and exchanges to qualify option contract."""
        ib = await self._ensure_connected()
        
        # For SPX, try both SPX and SPXW symbols
        symbol_variations = {
            'SPX': ['SPXW', 'SPX'],  # Prefer SPXW for 0DTE
            'RUT': ['RUT'],
            'SPY': ['SPY'],
            'QQQ': ['QQQ'],
            'IWM': ['IWM'],
            'NDX': ['NDX']  # Added NDX
        }.get(symbol_name, [symbol_name])
        
        # Exchange preferences - prioritize SMART
        exchange_map = {
            'SPX': ['SMART', 'CBOE'],
            'SPXW': ['SMART', 'CBOE'],
            'RUT': ['SMART', 'CBOE', 'RUSSELL'],
            'SPY': ['SMART', 'CBOE', 'ARCA', 'BATS', 'AMEX', 'ISE'],
            'QQQ': ['SMART', 'NASDAQ', 'CBOE', 'ARCA'],
            'IWM': ['SMART', 'ARCA', 'CBOE'],
            'NDX': ['SMART', 'NASDAQ']  # Added NDX with SMART prioritized for options
        }
        
        for sym_variant in symbol_variations:
            exchanges = exchange_map.get(sym_variant, ['SMART'])
            
            for exchange in exchanges:
                try:
                    # Create option contract
                    opt_contract = Option(
                        symbol=sym_variant,
                        lastTradeDateOrContractMonth=expiry_date,
                        strike=strike,
                        right=right,
                        exchange=exchange,
                        currency='USD'
                    )
                    
                    # Set trading class if provided (e.g., SPXW)
                    if trading_class:
                        opt_contract.tradingClass = trading_class
                    elif sym_variant == 'SPXW':
                        opt_contract.tradingClass = 'SPXW'
                    
                    # Try to qualify
                    qualified = await ib.qualifyContractsAsync(opt_contract)
                    if qualified and qualified[0].conId:
                        if sym_variant != symbol_name or exchange != 'CBOE':
                            logger.info(f"Qualified {symbol_name} option as {sym_variant} {strike} {right} on {exchange}")
                        return qualified[0]
                    
                except Exception as e:
                    logger.debug(f"Failed to qualify {sym_variant} {strike} {right} on {exchange}: {e}")
                    continue
        
        logger.warning(f"Failed to qualify {symbol_name} {strike} {right} option with any symbol/exchange combination")
        return None

    async def get_atm_options(self, symbols: List[str], days_to_expiry: int = 0) -> List[Dict]:
        """
        Return basic option data for ATM options for the given symbols and DTE.
        Enhanced with symbol/exchange fallback logic for better contract qualification.
        Now includes OI data fetched via streaming.
        """
        ib = await self._ensure_connected()
        options_data = []

        if not symbols:
            return options_data

        # For 0DTE, expiry date is today. Format: YYYYMMDD
        from datetime import datetime
        expiry_date = datetime.now().strftime('%Y%m%d')

        for symbol_name in symbols:
            # Get current price of underlying with fallback
            underlying_contract = await self.qualify_underlying_with_fallback(symbol_name)
            
            if not underlying_contract:
                logger.error(f"Could not qualify underlying for {symbol_name}")
                continue

            # Get spot price
            tickers: List[Ticker] = await ib.reqTickersAsync(underlying_contract)
            await asyncio.sleep(0.1)

            spot_price = None
            if tickers and tickers[0] and (tickers[0].marketPrice() or tickers[0].close):
                spot_price = tickers[0].marketPrice() if tickers[0].marketPrice() else tickers[0].close
                if not spot_price or spot_price <= 0 or str(spot_price) == 'nan':
                    print(f"Could not get valid spot price for {symbol_name}, using placeholder 5000")
                    spot_price = 5000
            else:
                print(f"Could not get spot price for {symbol_name}, using placeholder 5000")
                spot_price = 5000

            # Determine ATM strikes
            if symbol_name in ['SPX', 'SPXW', 'NDX']:  # Added NDX to use 5-point strikes
                atm_strike = round(spot_price / 5) * 5  # Round to nearest 5
            elif symbol_name == 'SPY':
                atm_strike = round(spot_price)  # Round to nearest 1
            else:
                atm_strike = round(spot_price / 5) * 5  # Default to 5

            # Get a wider range of strikes around ATM for better gamma calculations
            strike_increment = 5 if symbol_name in ['SPX', 'SPXW', 'RUT', 'NDX'] else 1  # Added NDX to 5-point increment list
            num_strikes_each_side = 20
            strikes_to_check = [atm_strike + i * strike_increment
                                for i in range(-num_strikes_each_side, num_strikes_each_side + 1)]

            qualified_options = []
            
            # Qualify options with fallback
            for strike in strikes_to_check:
                for right in ['C', 'P']:
                    qualified_opt = await self.qualify_option_with_fallback(
                        symbol_name, expiry_date, strike, right,
                        trading_class=underlying_contract.tradingClass if hasattr(underlying_contract, 'tradingClass') else None
                    )
                    if qualified_opt:
                        qualified_options.append(qualified_opt)

            if not qualified_options:
                print(f"No qualified option contracts found for {symbol_name} and strikes {strikes_to_check} for expiry {expiry_date}")
                continue

            # Request market data for qualified options
            tickers_for_options: List[Ticker] = await ib.reqTickersAsync(*qualified_options)

            # Store options data and contracts for OI fetching
            symbol_options_data = []
            symbol_contracts = []

            for ticker in tickers_for_options:
                contract: Contract = ticker.contract
                # Get implied volatility
                iv = None
                if ticker.modelGreeks and ticker.modelGreeks.impliedVol is not None and not str(ticker.modelGreeks.impliedVol) == 'nan':
                    iv = ticker.modelGreeks.impliedVol
                elif ticker.impliedVolatility is not None and not str(ticker.impliedVolatility) == 'nan':
                    iv = ticker.impliedVolatility

                # Don't try to get OI from snapshot - it will be added via streaming
                open_interest = 0  # Default to 0, will be updated by OI fetcher

                gamma = None
                delta = None
                if ticker.modelGreeks:
                    if hasattr(ticker.modelGreeks, 'gamma') and ticker.modelGreeks.gamma is not None and not str(ticker.modelGreeks.gamma) == 'nan':
                        gamma = ticker.modelGreeks.gamma
                    if hasattr(ticker.modelGreeks, 'delta') and ticker.modelGreeks.delta is not None and not str(ticker.modelGreeks.delta) == 'nan':
                        delta = ticker.modelGreeks.delta

                option_data = {
                    'symbol': symbol_name,  # Use original symbol name
                    'underlying_symbol': contract.symbol,  # Actual qualified symbol (might be SPXW)
                    'conId': contract.conId,
                    'strike': contract.strike,
                    'right': contract.right,
                    'expiry': contract.lastTradeDateOrContractMonth,
                    'bid': ticker.bid if ticker.bid != -1 else None,
                    'ask': ticker.ask if ticker.ask != -1 else None,
                    'implied_volatility': iv,
                    'open_interest': open_interest,  # Will be updated by OI fetcher
                    'gamma': gamma,
                    'delta': delta,
                    'underlying_price_at_fetch': spot_price
                }
                
                symbol_options_data.append(option_data)
                symbol_contracts.append(contract)

            # Enhance with OI data if we have an OI fetcher and it's enabled
            if self.oi_fetcher and settings.enable_oi_streaming and symbol_options_data:
                try:
                    logger.info(f"Fetching OI data for {len(symbol_contracts)} {symbol_name} contracts...")
                    symbol_options_data = await self.oi_fetcher.enhance_options_with_oi(
                        symbol_options_data, symbol_contracts
                    )
                except Exception as e:
                    logger.error(f"Failed to fetch OI data for {symbol_name}: {e}")
                    # Continue with 0 OI values rather than failing completely
            else:
                if not settings.enable_oi_streaming:
                    logger.info("OI streaming disabled - using default OI values of 0")

            options_data.extend(symbol_options_data)

        return options_data

async def example_usage():
    # This function is for demonstration and testing from a script.
    # Ensure IB TWS or Gateway is running and API is configured.
    # Ensure .env file has IB_HOST, IB_PORT, IB_CLIENT_ID

    # Note: util.patchAsyncio() should be called if running in Jupyter or certain environments
    # util.patchAsyncio() # if needed

    client = IBClient()
    try:
        await client._ensure_connected() # Call explicitly for example, usually called by methods

        print("\nFetching positions...")
        positions = await client.get_positions()
        if positions:
            for p in positions:
                print(f"  Position: {p.get('quantity')} x {p.get('symbol')} {p.get('expiry')} {p.get('strike')} {p.get('right')}")
        else:
            print("  No open option positions found.")

        print("\nFetching ATM options for SPX (0DTE)...")
        # For 0DTE, days_to_expiry is 0. The function calculates today's date.
        atm_spx_options = await client.get_atm_options(['SPX'], days_to_expiry=0)
        if atm_spx_options:
            for opt in atm_spx_options[:5]:  # Show first 5 options
                print(f"  SPX Option: K={opt['strike']} {opt['right']}, "
                      f"Bid={opt['bid']}, Ask={opt['ask']}, "
                      f"IV={opt['implied_volatility']:.4f if opt['implied_volatility'] else 'N/A'}, "
                      f"OI={opt['open_interest']}, "
                      f"Gamma={opt['gamma']:.6f if opt['gamma'] else 'N/A'}")
        else:
            print("  No ATM SPX options data found.")

    except ConnectionError as e:
        print(f"Main Example Usage: IB Connection Error: {e}")
    except Exception as e:
        print(f"An error occurred in example_usage: {e}")
    finally:
        await client.disconnect()
        # ib_async might keep the event loop running if not stopped explicitly
        # For a script, you might need to cancel pending tasks or stop the loop
        # For example, by calling ib.disconnect() and then ib.wait_for_disconnect()
        # Or, if part of a larger asyncio app, it integrates into that loop.

if __name__ == '__main__':
    # To run this example:
    # 1. Make sure your .env file is in the project root (where you'd run the main app from)
    #    and contains IB_HOST, IB_PORT, IB_CLIENT_ID.
    # 2. IB TWS or Gateway must be running and API connections enabled.
    # 3. Run this file directly (e.g., python -m magic8_companion.modules.ib_client)
    #    This requires Python to handle the module path correctly.

    # util.logToConsole() # Optional: for more detailed ib_async logs
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If loop is already running (e.g. in Jupyter), use ib.run(example_usage())
            # This is a simplified script execution, assuming no prior loop.
            print("Warning: Loop is already running. This script might behave unexpectedly.")
            task = loop.create_task(example_usage())
            # In a real app, you'd await this task or manage it.
        else:
            loop.run_until_complete(example_usage())
    except KeyboardInterrupt:
        print("Keyboard interrupt caught.")
    finally:
        print("Example usage finished.")
        # Consider loop.close() if this is the only thing running,
        # but be careful if other async tasks are managed elsewhere.
