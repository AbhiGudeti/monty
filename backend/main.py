from fastapi import FastAPI, HTTPException
import yfinance as yf
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel
from datetime import datetime
from enum import Enum
from fastapi.middleware.cors import CORSMiddleware
from core.monte_carlo import MonteCarloSimulator
from core.helpers.backtest_service import BacktestRequest
import google.generativeai as genai
import ssl


app = FastAPI(
    title="Monty",
    description="An integration of backtesting, forward testing, and AI portfolio management.",
    version="1.0.0"
)

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
# /etc/letsencrypt/live/monty.sathwik.tech
ssl_context.load_cert_chain('cert.pem', keyfile='privkey.pem')

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MonteCarloRequest(BaseModel):
    lookback_years: int = 10
    simulation_length_days: int = 252
    num_simulations: int = 500
    backtest_request: BacktestRequest

class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class MAType(str, Enum):
    SMA = "SMA"
    EMA = "EMA"

class MAComparisonType(str, Enum):
    CROSS_ABOVE = "CROSS_ABOVE"
    CROSS_BELOW = "CROSS_BELOW"
    ABOVE = "ABOVE"
    BELOW = "BELOW"

class MACondition(BaseModel):
    period: int = 20
    ma_type: MAType
    comparison: MAComparisonType
    deviation_pct: float

class RSICondition(BaseModel):
    period: int = 14
    comparison: Literal["ABOVE", "BELOW"]
    value: float

class MACDCrossoverType(str, Enum):
    BULLISH = "BULLISH"  # MACD crosses above Signal Line
    BEARISH = "BEARISH"  # MACD crosses below Signal Line

class MACDComparisonType(str, Enum):
    ABOVE_ZERO = "ABOVE_ZERO"  # MACD above zero line
    BELOW_ZERO = "BELOW_ZERO"  # MACD below zero line
    HISTOGRAM_POSITIVE = "HISTOGRAM_POSITIVE"  # Histogram > 0
    HISTOGRAM_NEGATIVE = "HISTOGRAM_NEGATIVE"  # Histogram < 0

class MACDCondition(BaseModel):
    crossover: Optional[MACDCrossoverType] = None  
    macd_comparison: Optional[MACDComparisonType] = None  
    histogram_positive: Optional[bool] = None  
    macd_signal_deviation_pct: Optional[float] = None 

class BBComparisonType(str, Enum):
    ABOVE_UPPER = "ABOVE_UPPER"
    BELOW_LOWER = "BELOW_LOWER"
    CROSS_MIDDLE_UP = "CROSS_MIDDLE_UP"
    CROSS_MIDDLE_DOWN = "CROSS_MIDDLE_DOWN"

class ADXComparisonType(str, Enum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    DI_CROSS_ABOVE = "DI_CROSS_ABOVE"
    DI_CROSS_BELOW = "DI_CROSS_BELOW"

class BBCondition(BaseModel):
    period: int = 20
    std_dev: float = 2.0
    comparison: BBComparisonType

class ADXCondition(BaseModel):
    period: int = 14
    comparison: ADXComparisonType
    value: float = 25.0

class EntryCondition(BaseModel):
    ma_condition: Optional[MACondition] = None
    rsi_condition: Optional[RSICondition] = None
    macd_condition: Optional[MACDCondition] = None
    bb_condition: Optional[BBCondition] = None
    adx_condition: Optional[ADXCondition] = None
    trade_direction: TradeDirection

class ExitCondition(BaseModel):
    stop_loss_pct: float
    take_profit_pct: float
    position_size_pct: float

class BacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    timeframe: str = "1d"
    initial_capital: float = 10000.0
    entry_conditions: EntryCondition
    exit_conditions: ExitCondition

class Position:
    def __init__(self, entry_price: float, entry_date: datetime, size: float, 
                 initial_value: float, direction: TradeDirection):
        self.entry_price = entry_price
        self.entry_date = entry_date
        self.size = size
        self.initial_value = initial_value
        self.direction = direction

    def calculate_pnl(self, current_price: float) -> tuple[float, float]:
        if self.direction == TradeDirection.BUY:
            pnl = (current_price - self.entry_price) * self.size
        else:
            pnl = (self.entry_price - current_price) * self.size

        pnl_pct = (pnl / self.initial_value) * 100 if self.initial_value != 0 else 0
        return pnl, pnl_pct

class BacktestService:
    def __init__(self, debug=True):
        self.debug = debug
    
    def get_historical_data(self, symbol: str, start_date: str, end_date: str, timeframe="1d") -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date, end=end_date, interval=timeframe)
        if self.debug:
            print(f"\nFetched {len(df)} data points for {symbol}")
        return df

    def calculate_sma(self, data: pd.DataFrame, period: int) -> pd.Series:
        return data['Close'].rolling(window=period).mean()

    def calculate_ema(self, data: pd.DataFrame, period: int) -> pd.Series:
        return data['Close'].ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, data: pd.DataFrame, period: int) -> pd.Series:
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    
    def calculate_macd(self,df, short_period=12, long_period=26):
        short_ema = df['Close'].ewm(span=short_period, adjust=False).mean() # calculate the exponental weighted movement to short
        long_ema = df['Close'].ewm(span=long_period, adjust=False).mean() # calculate the exponential weighted movement for long
        return short_ema - long_ema # subtract them
    
    
    def calculate_signal_line(self,df, macd_column='MACD', signal_period=9): # need the signal line to predict divergence
        return df[macd_column].ewm(span=signal_period, adjust=False).mean()

    def calculate_macd_divergence(self,df, divergence_type='BULLISH'):
        price_trend = df['Close'].diff()
        macd_trend = df['MACD'].diff()
        if divergence_type == 'BULLISH':
            return (price_trend < 0) & (macd_trend > 0)
        elif divergence_type == 'BEARISH':
            return (price_trend > 0) & (macd_trend < 0)
        return None
    
    def calculate_adx(self, data: pd.DataFrame, period: int) -> pd.Series:
        data['TR'] = np.maximum(
            data['High'] - data['Low'],
            np.maximum(
                abs(data['High'] - data['Close'].shift(1)),
                abs(data['Low'] - data['Close'].shift(1))
            )
        )

        data['+DM'] = np.where(
            (data['High'] - data['High'].shift(1)) > (data['Low'].shift(1) - data['Low']),
            np.maximum(data['High'] - data['High'].shift(1), 0),
            0
        )

        data['-DM'] = np.where(
            (data['Low'].shift(1) - data['Low']) > (data['High'] - data['High'].shift(1)),
            np.maximum(data['Low'].shift(1) - data['Low'], 0),
            0
        )

        data['TR14'] = data['TR'].ewm(span=period, min_periods=period).mean()
        data['+DM14'] = data['+DM'].ewm(span=period, min_periods=period).mean()
        data['-DM14'] = data['-DM'].ewm(span=period, min_periods=period).mean()

        data['+DI14'] = 100 * (data['+DM14'] / data['TR14'])
        data['-DI14'] = 100 * (data['-DM14'] / data['TR14'])

        data['DX'] = 100 * abs(data['+DI14'] - data['-DI14']) / (data['+DI14'] + data['-DI14'])
        adx = data['DX'].ewm(span=period, min_periods=period).mean()

        return adx

    def calculate_bollinger_bands(self, data: pd.DataFrame, period: int, std_dev: float) -> pd.DataFrame:
        middle = data['Close'].rolling(window=period).mean()
        std = data['Close'].rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)

        # calc bandwidth (volatility indicator)
        bandwidth = ((upper - lower) / middle) * 100

        # calc %B (position within bands)
        percent_b = (data['Close'] - lower) / (upper - lower)

        # calc typical price
        typical_price = (data['High'] + data['Low'] + data['Close']) / 3

        return pd.DataFrame({
            'BB_middle': middle,
            'BB_upper': upper,
            'BB_lower': lower,
            'BB_bandwidth': bandwidth,
            'BB_percent_b': percent_b,
            'BB_typical_price': typical_price
        })


    def calculate_indicators(self, df: pd.DataFrame, entry_conditions: EntryCondition) -> pd.DataFrame:
        if entry_conditions.ma_condition:
            ma_cond = entry_conditions.ma_condition
            period = ma_cond.period
            
            if ma_cond.ma_type == MAType.SMA:
                df[f'MA_{period}'] = self.calculate_sma(df, period)
            else:
                df[f'MA_{period}'] = self.calculate_ema(df, period)
                
            deviation = ma_cond.deviation_pct / 100
            df[f'MA_{period}_upper'] = df[f'MA_{period}'] * (1 + deviation)
            df[f'MA_{period}_lower'] = df[f'MA_{period}'] * (1 - deviation)

        if entry_conditions.rsi_condition:
            period = entry_conditions.rsi_condition.period
            df[f'RSI_{period}'] = self.calculate_rsi(df, period)
        
        if entry_conditions.macd_condition:
            #macd and signal line
            df['MACD'] = self.calculate_macd(df)
            df['Signal_Line'] = self.calculate_signal_line(df)

            #histogram
            df['MACD_Histogram'] = df['MACD'] - df['Signal_Line']

            ###macd conditions
            if entry_conditions.macd_condition and entry_conditions.macd_condition.crossover: # hasattribute
                if entry_conditions.macd_condition.crossover == "BULLISH":
                    df['MACD_Crossover'] = (df['MACD'].shift(1) < df['Signal_Line'].shift(1)) & (df['MACD'] > df['Signal_Line'])
                elif entry_conditions.macd_condition.crossover == "BEARISH":
                    df['MACD_Crossover'] = (df['MACD'].shift(1) > df['Signal_Line'].shift(1)) & (df['MACD'] < df['Signal_Line'])

            if hasattr(entry_conditions.macd_condition, 'histogram_positive'):
                df['MACD_Histogram_Positive'] = df['MACD_Histogram'] > 0

            if entry_conditions.macd_condition and entry_conditions.macd_condition.macd_comparison == MACDComparisonType.ABOVE_ZERO:
                df['MACD_Above_Zero'] = df['MACD'] > 0

            if hasattr(entry_conditions.macd_condition, 'divergence'):
                df['MACD_Divergence'] = self.calculate_macd_divergence(df, entry_conditions.macd_condition.divergence)

            if hasattr(entry_conditions.macd_condition, 'macd_signal_deviation_pct'):
                deviation = entry_conditions.macd_condition.macd_signal_deviation_pct / 100
                df['MACD_Signal_Deviation'] = abs(df['MACD'] - df['Signal_Line']) > (df['Signal_Line'] * deviation)
        
        if entry_conditions.bb_condition:
            bb_cond = entry_conditions.bb_condition
            bb_df = self.calculate_bollinger_bands(df, bb_cond.period, bb_cond.std_dev)
            
            # Add all BB indicators
            df['BB_middle'] = bb_df['BB_middle']
            df['BB_upper'] = bb_df['BB_upper']
            df['BB_lower'] = bb_df['BB_lower']
            df['BB_bandwidth'] = bb_df['BB_bandwidth']
            df['BB_percent_b'] = bb_df['BB_percent_b']
            df['BB_typical_price'] = bb_df['BB_typical_price']

        if entry_conditions.adx_condition:
            adx_cond = entry_conditions.adx_condition
            adx_data = self.calculate_adx(df, adx_cond.period)
            df['ADX'] = adx_data

        return df

    def check_entry_conditions(self, row: pd.Series, prev_row: pd.Series, entry_conditions: EntryCondition) -> Optional[TradeDirection]:
        ma_condition_met = False
        rsi_condition_met = False
        macd_condition_met = False
        bb_condition_met = False
        adx_condition_met = False
        
        if entry_conditions.ma_condition:
            ma_cond = entry_conditions.ma_condition
            period = ma_cond.period
            ma_col = f'MA_{period}'
            
            if ma_cond.comparison == MAComparisonType.CROSS_ABOVE:
                ma_condition_met = (prev_row['Close'] <= prev_row[f'{ma_col}_upper'] and 
                                   row['Close'] > row[f'{ma_col}_upper'])
            elif ma_cond.comparison == MAComparisonType.CROSS_BELOW:
                ma_condition_met = (prev_row['Close'] >= prev_row[f'{ma_col}_lower'] and 
                                   row['Close'] < row[f'{ma_col}_lower'])
            elif ma_cond.comparison == MAComparisonType.ABOVE:
                ma_condition_met = row['Close'] > row[f'{ma_col}_upper']
            elif ma_cond.comparison == MAComparisonType.BELOW:
                ma_condition_met = row['Close'] < row[f'{ma_col}_lower']

        if entry_conditions.rsi_condition:
            rsi_cond = entry_conditions.rsi_condition
            rsi_col = f'RSI_{rsi_cond.period}'
            
            if rsi_cond.comparison == "ABOVE":
                rsi_condition_met = row[rsi_col] > rsi_cond.value
            else:
                rsi_condition_met = row[rsi_col] < rsi_cond.value

        if entry_conditions.macd_condition:
            macd_cond = entry_conditions.macd_condition
            conditions_met = []

            if macd_cond.crossover:
                if macd_cond.crossover == MACDCrossoverType.BULLISH:
                    conditions_met.append(row['MACD_Crossover'])
                elif macd_cond.crossover == MACDCrossoverType.BEARISH:
                    conditions_met.append(row['MACD_Crossover'])

            if macd_cond.histogram_positive is not None:
                conditions_met.append(row['MACD_Histogram_Positive'] == macd_cond.histogram_positive)

            if macd_cond.macd_comparison:
                if macd_cond.macd_comparison == MACDComparisonType.ABOVE_ZERO:
                    conditions_met.append(row['MACD'] > 0)
                elif macd_cond.macd_comparison == MACDComparisonType.BELOW_ZERO:
                    conditions_met.append(row['MACD'] < 0)

            if macd_cond.macd_signal_deviation_pct:
                conditions_met.append(row['MACD_Signal_Deviation'])

            macd_condition_met = all(conditions_met) if conditions_met else True
        
        if entry_conditions.bb_condition:
            bb_cond = entry_conditions.bb_condition
            
            if bb_cond.comparison == BBComparisonType.ABOVE_UPPER:
                bb_condition_met = row['Close'] > row['BB_upper']
            elif bb_cond.comparison == BBComparisonType.BELOW_LOWER:
                bb_condition_met = row['Close'] < row['BB_lower']
            elif bb_cond.comparison == BBComparisonType.CROSS_MIDDLE_UP:
                bb_condition_met = (prev_row['Close'] <= prev_row['BB_middle'] and row['Close'] > row['BB_middle'])
            elif bb_cond.comparison == BBComparisonType.CROSS_MIDDLE_DOWN:
                bb_condition_met = (prev_row['Close'] >= prev_row['BB_middle'] and row['Close'] < row['BB_middle'])

        if entry_conditions.adx_condition:
            adx_cond = entry_conditions.adx_condition
            
            if adx_cond.comparison == ADXComparisonType.ABOVE:
                adx_condition_met = row['ADX'] > adx_cond.value
            elif adx_cond.comparison == ADXComparisonType.BELOW:
                adx_condition_met = row['ADX'] < adx_cond.value
            elif adx_cond.comparison == ADXComparisonType.DI_CROSS_ABOVE:
                adx_condition_met = (prev_row['+DI14'] <= prev_row['-DI14'] and row['+DI14'] > row['-DI14'])
            elif adx_cond.comparison == ADXComparisonType.DI_CROSS_BELOW:
                adx_condition_met = (prev_row['+DI14'] >= prev_row['-DI14'] and row['+DI14'] < row['-DI14'])

        if ((entry_conditions.ma_condition is None or ma_condition_met) and 
            (entry_conditions.rsi_condition is None or rsi_condition_met) and
            (entry_conditions.macd_condition is None or macd_condition_met) and
            (entry_conditions.bb_condition is None or bb_condition_met) and
            (entry_conditions.adx_condition is None or adx_condition_met)):
            return entry_conditions.trade_direction
        
        return None

    def run_backtest(self, request: BacktestRequest) -> Dict[str, Any]:
        df = self.get_historical_data(request.symbol, request.start_date, request.end_date, request.timeframe)
        df = self.calculate_indicators(df, request.entry_conditions)
        
        open_positions = []
        trades = []
        equity_curve = [request.initial_capital]
        cash = request.initial_capital
        current_capital = request.initial_capital
        
        for i in range(1, len(df)):
            current_row = df.iloc[i]
            prev_row = df.iloc[i-1]
            
            total_position_value = sum(
                position.initial_value + position.calculate_pnl(current_row['Close'])[0] 
                for position in open_positions
            )
            
            current_capital = cash + total_position_value
            
            for position in open_positions[:]:
                pnl, pnl_pct = position.calculate_pnl(current_row['Close'])
                
                should_exit = False
                exit_reason = None
                
                if position.direction == TradeDirection.BUY:
                    if pnl_pct <= -request.exit_conditions.stop_loss_pct:
                        should_exit = True
                        exit_reason = "Stop Loss"
                    elif pnl_pct >= request.exit_conditions.take_profit_pct:
                        should_exit = True
                        exit_reason = "Take Profit"
                else:
                    if pnl_pct <= -request.exit_conditions.stop_loss_pct:
                        should_exit = True
                        exit_reason = "Stop Loss"
                    elif pnl_pct >= request.exit_conditions.take_profit_pct:
                        should_exit = True
                        exit_reason = "Take Profit"
                
                if should_exit:
                    cash += position.initial_value + pnl
                    
                    trades.append({
                        'entry_date': position.entry_date.isoformat(),
                        'exit_date': current_row.name.isoformat(),
                        'direction': position.direction,
                        'entry_price': position.entry_price,
                        'exit_price': current_row['Close'],
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'exit_reason': exit_reason
                    })
                    
                    if self.debug:
                        print(f"\nExiting {position.direction} trade at {current_row.name}")
                        print(f"Exit Price: ${current_row['Close']:.2f}")
                        print(f"P&L: ${pnl:.2f} ({pnl_pct:.2f}%)")
                        print(f"Reason: {exit_reason}")
                    
                    open_positions.remove(position)
            
            trade_direction = self.check_entry_conditions(current_row, prev_row, request.entry_conditions)
            
            max_position_value = current_capital * request.exit_conditions.position_size_pct / 100
            
            if trade_direction:
                position_value = max_position_value
                position_size = position_value / current_row['Close']
                
                if position_value <= cash:
                    cash -= position_value
                    new_position = Position(
                        entry_price=current_row['Close'],
                        entry_date=current_row.name,
                        size=position_size,
                        initial_value=position_value,
                        direction=trade_direction
                    )
                    open_positions.append(new_position)
                    
                    if self.debug:
                        print(f"\nEntering {trade_direction} trade at {current_row.name}")
                        print(f"Entry Price: ${current_row['Close']:.2f}")
                        print(f"Position Size: {position_size:.2f} shares")
                        print(f"Position Value: ${position_value:.2f}")
            
            equity_curve.append(current_capital)
        
        if trades:
            winning_trades = [t for t in trades if t['pnl'] > 0]
            total_trades = len(trades)
            
            return {
                'total_trades': total_trades,
                'winning_trades': len(winning_trades),
                'losing_trades': total_trades - len(winning_trades),
                'win_rate': (len(winning_trades) / total_trades) * 100,
                'initial_capital': request.initial_capital,
                'final_capital': current_capital,
                'total_return_pct': ((current_capital - request.initial_capital) / 
                                request.initial_capital) * 100,
                'max_drawdown_pct': self._calculate_max_drawdown(equity_curve),
                'avg_profit': (sum(t['pnl'] for t in winning_trades) / 
                            len(winning_trades)) if winning_trades else 0,
                'avg_loss': (sum(t['pnl'] for t in trades if t['pnl'] <= 0) / 
                        (total_trades - len(winning_trades))) 
                        if total_trades > len(winning_trades) else 0,
                'trades': trades,
                'equity_curve': equity_curve
            }
        else:
            return {
                'message': 'No trades executed during the backtest period',
                'data_points': len(df),
                'date_range': f"{df.index[0]} to {df.index[-1]}"
            }

    def _calculate_max_drawdown(self, equity_curve: List[float]) -> float:
        if not equity_curve:
            return 0.0
            
        peak = float('-inf')
        max_drawdown = 0.0
        
        for equity in equity_curve:
            if equity > peak:
                peak = equity
            if peak > 0:  
                drawdown = (peak - equity) / peak * 100
                max_drawdown = max(max_drawdown, drawdown)
                
        return max_drawdown

backtest_service = BacktestService()

@app.get("/")
async def root():
    return {
        "message": "Welcome to the Monty Backend API",
        "docs_url": "/docs",
    }

@app.post("/backtest", response_model=Dict[str, Any])
async def run_backtest(request: BacktestRequest):
    try:
        results = backtest_service.run_backtest(request)
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class MonteCarloRequest(BaseModel):
    lookback_years: int = 10
    simulation_length_days: int = 252
    num_simulations: int = 500
    backtest_request: BacktestRequest

    class Config:
        from_attributes = True

@app.post("/montecarlo", response_model=Dict[str, Any])
async def run_monte_carlo(request: MonteCarloRequest):
    try:
        simulator = MonteCarloSimulator(
            lookback_years=request.lookback_years,
            simulation_length_days=request.simulation_length_days
        )
        
        backtest_request = BacktestRequest(
            symbol=request.backtest_request.symbol,
            start_date=request.backtest_request.start_date,
            end_date=request.backtest_request.end_date,
            timeframe=request.backtest_request.timeframe,
            initial_capital=request.backtest_request.initial_capital,
            entry_conditions=request.backtest_request.entry_conditions.model_dump(),
            exit_conditions=request.backtest_request.exit_conditions.model_dump()
        )
        
        results = simulator.run_simulations(
            backtest_request=backtest_request,
            num_simulations=request.num_simulations
        )
        
        return {
            "avg_return": round(results.avg_return, 2),
            "median_return": round(results.median_return, 2),
            "highest_return": round(results.highest_return, 2),
            "worst_return": round(results.worst_return, 2),
            "avg_drawdown": round(results.avg_drawdown, 2),
            "median_drawdown": round(results.median_drawdown, 2),
            "worst_drawdown": round(results.worst_drawdown, 2),
            "win_rate": round(results.win_rate, 2),
            "sharpe_ratio": round(results.sharpe_ratio, 2),
            "simulation_count": results.simulation_count,
            "successful_simulations": results.successful_simulations,
            "success_rate": round((results.successful_simulations / results.simulation_count) * 100, 2)
        }
    except Exception as e:
        error_msg = str(e)
        print(f"Monte Carlo simulation failed: {error_msg}")
        raise HTTPException(
            status_code=500,
            detail=f"Monte Carlo simulation failed: {error_msg}"
        )
    
@app.post("/monte-carlo", response_model=Dict[str, Any])
async def run_monte_carlo(request: Dict[str, Any]):
    try:
        backtest_request = request.get('backtest_request', {})
        
        if not isinstance(backtest_request, dict):
            backtest_request = backtest_request.dict()
        
        if 'symbol' not in backtest_request:
            raise ValueError("Missing 'symbol' in backtest request")
        
        mc_simulator = MonteCarloSimulator(
            lookback_years=request.get('lookback_years', 10),
            simulation_length_days=request.get('simulation_length_days', 252)
        )
        
        backtest_request_obj = BacktestRequest(
            symbol=backtest_request['symbol'],
            start_date=backtest_request.get('start_date', '2024-01-01'),
            end_date=backtest_request.get('end_date', '2024-12-31'),
            timeframe=backtest_request.get('timeframe', '1d'),
            initial_capital=backtest_request.get('initial_capital', 10000),
            entry_conditions=backtest_request.get('entry_conditions', {}),
            exit_conditions=backtest_request.get('exit_conditions', {})
        )
        
        results = mc_simulator.run_simulations(
            backtest_request=backtest_request_obj,
            num_simulations=request.get('num_simulations', 500)
        )
        
        results_dict = {
            'avg_return': results.avg_return,
            'median_return': results.median_return,
            'highest_return': results.highest_return,
            'worst_return': results.worst_return,
            'avg_drawdown': results.avg_drawdown,
            'median_drawdown': results.median_drawdown,
            'worst_drawdown': results.worst_drawdown,
            'win_rate': results.win_rate,
            'sharpe_ratio': results.sharpe_ratio,
            'simulation_count': results.simulation_count,
            'successful_simulations': results.successful_simulations
        }
        
        return results_dict
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/monte-carlo-analysis")
async def analyze_monte_carlo_results(request: Dict[str, Any]):
    try:
        results = request.get('results')
        strategy = request.get('strategy')
        genai.configure(api_key="AIzaSyBXYZL4CjM3i3yh_gpbAbSerzsK-1CcjC0")
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"""
        Analyze these Monte Carlo simulation results and provide specific recommendations:
        
        Results:
        - Average Return: {results['avg_return']}%
        - Median Return: {results['median_return']}%
        - Win Rate: {results['win_rate']}%
        - Average Drawdown: {results['avg_drawdown']}%
        - Worst Drawdown: {results['worst_drawdown']}%
        - Sharpe Ratio: {results['sharpe_ratio']}
        
        Strategy Settings:
        - Entry Indicators: {strategy['entry_conditions']}
        - Exit Conditions: Stop Loss {strategy['exit_conditions']['stop_loss_pct']}%, Take Profit {strategy['exit_conditions']['take_profit_pct']}%
        - Position Size: {strategy['exit_conditions']['position_size_pct']}%
        
        Provide targeted advice for:
        1. Risk management adjustments
        2. Entry/exit condition optimization
        3. Position sizing recommendations
        Keep feedback specific and data-driven. 
        Don't bother sending the simulation results again since I've already displayed it myself.
        """

        response = model.generate_content(prompt)
        print("Response generated")

        return {
            'analysis': response.text
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
