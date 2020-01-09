# -*- coding: utf-8 -*-
# 版权所有 2019 深圳米筐科技有限公司（下称“米筐科技”）
#
# 除非遵守当前许可，否则不得使用本软件。
#
#     * 非商业用途（非商业用途指个人出于非商业目的使用本软件，或者高校、研究所等非营利机构出于教育、科研等目的使用本软件）：
#         遵守 Apache License 2.0（下称“Apache 2.0 许可”），
#         您可以在以下位置获得 Apache 2.0 许可的副本：http://www.apache.org/licenses/LICENSE-2.0。
#         除非法律有要求或以书面形式达成协议，否则本软件分发时需保持当前许可“原样”不变，且不得附加任何条件。
#
#     * 商业用途（商业用途指个人出于任何商业目的使用本软件，或者法人或其他组织出于任何目的使用本软件）：
#         未经米筐科技授权，任何个人不得出于任何商业目的使用本软件（包括但不限于向第三方提供、销售、出租、出借、转让本软件、
#         本软件的衍生产品、引用或借鉴了本软件功能或源代码的产品或服务），任何法人或其他组织不得出于任何目的使用本软件，
#         否则米筐科技有权追究相应的知识产权侵权责任。
#         在此前提下，对本软件的使用同样需要遵守 Apache 2.0 许可，Apache 2.0 许可与本许可冲突之处，以本许可为准。
#         详细的授权流程，请联系 public@ricequant.com 获取。

import os
import pickle
import numbers
from collections import defaultdict
from enum import Enum
from datetime import date
from typing import Dict, Optional

import six
import numpy as np
import pandas as pd
from rqrisk import Risk

from rqalpha.const import EXIT_CODE, DEFAULT_ACCOUNT_TYPE, RUN_TYPE
from rqalpha.events import EVENT
from rqalpha.interface import AbstractMod

from .bechmark import Benchmark


class AnalyserMod(AbstractMod):
    def __init__(self):
        self._env = None
        self._mod_config = None
        self._enabled = False

        self._orders = []
        self._trades = []
        self._total_portfolios = []
        self._total_benchmark_portfolios = []
        self._sub_accounts = defaultdict(list)
        self._positions = defaultdict(list)

        self._benchmark_daily_returns = []
        self._portfolio_daily_returns = []

        self._benchmark = None  # type: Optional[Benchmark]

    def start_up(self, env, mod_config):
        self._env = env
        self._mod_config = mod_config
        self._enabled = (
            mod_config.record or mod_config.plot or mod_config.output_file or
            mod_config.plot_save_file or mod_config.report_save_path or mod_config.bechmark
        )
        if self._enabled:
            env.event_bus.add_listener(EVENT.POST_SYSTEM_INIT, self._subscribe_events)
            if mod_config.benchmark:
                if env.config.base.run_type == RUN_TYPE.BACKTEST:
                    from .bechmark import BackTestPriceSeriesBenchmark
                    self._benchmark = BackTestPriceSeriesBenchmark(mod_config.benchmark, env)
                else:
                    from .bechmark import RealTimePriceSeriesBenchmark
                    self._benchmark = RealTimePriceSeriesBenchmark(mod_config.benchmark, env)

    def _subscribe_events(self, _):
        self._env.event_bus.add_listener(EVENT.TRADE, self._collect_trade)
        self._env.event_bus.add_listener(EVENT.ORDER_CREATION_PASS, self._collect_order)
        self._env.event_bus.add_listener(EVENT.POST_AFTER_TRADING, self._collect_daily)

    def _collect_trade(self, event):
        self._trades.append(self._to_trade_record(event.trade))

    def _collect_order(self, event):
        self._orders.append(event.order)

    def _collect_daily(self, _):
        date = self._env.calendar_dt.date()
        portfolio = self._env.portfolio

        self._portfolio_daily_returns.append(portfolio.daily_returns)
        self._total_portfolios.append(self._to_portfolio_record(date, portfolio))

        if self._benchmark is None:
            self._benchmark_daily_returns.append(0)
        else:
            self._benchmark_daily_returns.append(self._benchmark.daily_returns)
            self._total_benchmark_portfolios.append({
                "date": date,
                "unit_net_value": self._benchmark.total_returns + 1
            })

        for account_type, account in six.iteritems(self._env.portfolio.accounts):
            self._sub_accounts[account_type].append(self._to_account_record(date, account))
            for order_book_id, position in six.iteritems(account.positions):
                self._positions[account_type].append(self._to_position_record(date, order_book_id, position))

    def _symbol(self, order_book_id):
        return self._env.data_proxy.instruments(order_book_id).symbol

    @staticmethod
    def _safe_convert(value, ndigits=3):
        if isinstance(value, Enum):
            return value.name

        if isinstance(value, numbers.Real):
            return round(float(value), ndigits)

        return value

    def _to_portfolio_record(self, date, portfolio):
        return {
            'date': date,
            'cash': self._safe_convert(portfolio.cash),
            'total_value': self._safe_convert(portfolio.total_value),
            'market_value': self._safe_convert(portfolio.market_value),
            'unit_net_value': self._safe_convert(portfolio.unit_net_value, 6),
            'units': portfolio.units,
            'static_unit_net_value': self._safe_convert(portfolio.static_unit_net_value),
        }

    def _to_benchmark_record(self, date, unit_net_value):
        # type: (date, float) -> Dict
        return {
            "date": date,
            "unit_net_value": unit_net_value
        }

    ACCOUNT_FIELDS_MAP = {
        DEFAULT_ACCOUNT_TYPE.STOCK: ['dividend_receivable'],
        DEFAULT_ACCOUNT_TYPE.FUTURE: ['position_pnl', 'trading_pnl', 'daily_pnl', 'margin'],
        DEFAULT_ACCOUNT_TYPE.OPTION: ['position_pnl', 'trading_pnl', 'daily_pnl', 'margin'],
        DEFAULT_ACCOUNT_TYPE.BOND: [],
    }

    def _to_account_record(self, date, account):
        data = {
            'date': date,
            'cash': self._safe_convert(account.cash),
            'transaction_cost': self._safe_convert(account.transaction_cost),
            'market_value': self._safe_convert(account.market_value),
            'total_value': self._safe_convert(account.total_value),
        }

        for f in self.ACCOUNT_FIELDS_MAP[account.type]:
            data[f] = self._safe_convert(getattr(account, f))

        return data

    POSITION_FIELDS_MAP = {
        DEFAULT_ACCOUNT_TYPE.STOCK.name: [
            'quantity', 'last_price', 'avg_price', 'market_value'
        ],
        DEFAULT_ACCOUNT_TYPE.FUTURE.name: [
            'margin', 'margin_rate', 'contract_multiplier', 'last_price',
            'buy_pnl', 'buy_margin', 'buy_quantity', 'buy_avg_open_price',
            'sell_pnl', 'sell_margin', 'sell_quantity', 'sell_avg_open_price'
        ],
        DEFAULT_ACCOUNT_TYPE.BOND.name: [
            'quantity', 'last_price', 'avg_price', 'market_value'
        ],
    }

    def _to_position_record(self, date, order_book_id, position):
        data = {
            'order_book_id': order_book_id,
            'symbol': self._symbol(order_book_id),
            'date': date,
        }

        for f in self.POSITION_FIELDS_MAP[position.type]:
            data[f] = self._safe_convert(getattr(position, f))
        return data

    def _to_trade_record(self, trade):
        return {
            'datetime': trade.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            'trading_datetime': trade.trading_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            'order_book_id': trade.order_book_id,
            'symbol': self._symbol(trade.order_book_id),
            'side': self._safe_convert(trade.side),
            'position_effect': self._safe_convert(trade.position_effect),
            'exec_id': trade.exec_id,
            'tax': trade.tax,
            'commission': trade.commission,
            'last_quantity': trade.last_quantity,
            'last_price': self._safe_convert(trade.last_price),
            'order_id': trade.order_id,
            'transaction_cost': trade.transaction_cost,
        }

    def tear_down(self, code, exception=None):
        if code != EXIT_CODE.EXIT_SUCCESS or not self._enabled:
            return

        # 当 PRE_SETTLEMENT 事件没有被触发当时候，self._total_portfolio 为空list
        if len(self._total_portfolios) == 0:
            return

        strategy_name = os.path.basename(self._env.config.base.strategy_file).split(".")[0]
        data_proxy = self._env.data_proxy

        summary = {
            'strategy_name': strategy_name,
            'start_date': self._env.config.base.start_date.strftime('%Y-%m-%d'),
            'end_date': self._env.config.base.end_date.strftime('%Y-%m-%d'),
            'strategy_file': self._env.config.base.strategy_file,
            'run_type': self._env.config.base.run_type.value,
        }
        for account_type, starting_cash in six.iteritems(self._env.config.base.accounts):
            summary[account_type] = starting_cash

        risk = Risk(
            np.array(self._portfolio_daily_returns),
            np.array(self._benchmark_daily_returns),
            data_proxy.get_risk_free_rate(
                self._env.config.base.start_date, self._env.config.base.end_date
            )
        )
        summary.update({
            'alpha': self._safe_convert(risk.alpha, 3),
            'beta': self._safe_convert(risk.beta, 3),
            'sharpe': self._safe_convert(risk.sharpe, 3),
            'information_ratio': self._safe_convert(risk.information_ratio, 3),
            'downside_risk': self._safe_convert(risk.annual_downside_risk, 3),
            'tracking_error': self._safe_convert(risk.annual_tracking_error, 3),
            'sortino': self._safe_convert(risk.sortino, 3),
            'volatility': self._safe_convert(risk.annual_volatility, 3),
            'max_drawdown': self._safe_convert(risk.max_drawdown, 3),
        })

        summary.update({
            'total_value': self._safe_convert(self._env.portfolio.total_value),
            'cash': self._safe_convert(self._env.portfolio.cash),
            'total_returns': self._safe_convert(self._env.portfolio.total_returns),
            'annualized_returns': self._safe_convert(self._env.portfolio.annualized_returns),
            'unit_net_value': self._safe_convert(self._env.portfolio.unit_net_value),
            'units': self._env.portfolio.units,
        })

        if self._benchmark:
            summary['benchmark_total_returns'] = self._safe_convert(self._benchmark.total_returns)
            summary['benchmark_annualized_returns'] = self._safe_convert(self._benchmark.annualized_returns)

        trades = pd.DataFrame(self._trades)
        if 'datetime' in trades.columns:
            trades = trades.set_index('datetime')

        df = pd.DataFrame(self._total_portfolios)

        df['date'] = pd.to_datetime(df['date'])

        total_portfolios = df.set_index('date').sort_index()

        result_dict = {
            'summary': summary,
            'trades': trades,
            'portfolio': total_portfolios,
        }

        if self._benchmark:
            b_df = pd.DataFrame(self._total_benchmark_portfolios)
            df['date'] = pd.to_datetime(df['date'])
            benchmark_portfolios = b_df.set_index('date').sort_index()
            result_dict['benchmark_portfolio'] = benchmark_portfolios

        if not self._env.get_plot_store().empty:
            plots = self._env.get_plot_store().get_plots()
            plots_items = defaultdict(dict)
            for series_name, value_dict in six.iteritems(plots):
                for date, value in six.iteritems(value_dict):
                    plots_items[date][series_name] = value
                    plots_items[date]["date"] = date

            df = pd.DataFrame([dict_data for date, dict_data in six.iteritems(plots_items)])

            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            result_dict["plots"] = df

        for account_type, account in six.iteritems(self._env.portfolio.accounts):
            account_name = account_type.lower()
            portfolios_list = self._sub_accounts[account_type]
            df = pd.DataFrame(portfolios_list)
            df["date"] = pd.to_datetime(df["date"])
            account_df = df.set_index("date").sort_index()
            result_dict["{}_account".format(account_name)] = account_df

            positions_list = self._positions[account_type]
            positions_df = pd.DataFrame(positions_list)
            if "date" in positions_df.columns:
                positions_df["date"] = pd.to_datetime(positions_df["date"])
                positions_df = positions_df.set_index("date").sort_index()
            result_dict["{}_positions".format(account_name)] = positions_df

        if self._mod_config.output_file:
            with open(self._mod_config.output_file, 'wb') as f:
                pickle.dump(result_dict, f)

        if self._mod_config.report_save_path:
            from .report import generate_report
            generate_report(result_dict, self._mod_config.report_save_path)

        if self._mod_config.plot or self._mod_config.plot_save_file:
            from .plot import plot_result
            plot_result(result_dict, self._mod_config.plot, self._mod_config.plot_save_file)

        return result_dict
