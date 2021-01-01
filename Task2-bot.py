import numpy as np
from itertools import combinations
import copy, datetime, time

from typing import List

from fmclient import Agent, Session
from fmclient import Order, OrderSide, OrderType, Market

# Submission details
SUBMISSION = {"number": "number", "name": "name"}


class CAPMBot(Agent):

    def __init__(self, account, email, password, marketplace_id, risk_penalty=0.0175, session_time=10):
        """
        Constructor for the Bot
        :param account: Account name
        :param email: Email id
        :param password: password
        :param marketplace_id: id of the marketplace
        :param risk_penalty: Penalty for risk
        :param session_time: Total trading time for one session
        """
        super().__init__(account, email, password, marketplace_id, name="CAPM Bot")
        self._payoffs = {}
        self._risk_penalty = risk_penalty
        self._session_time = session_time
        self._market_ids = {} 

        # Attributes for performance evaluation
        self._cov_matrix = None
        self._exp_payoffs_matrix = None

        # Attributes for order price determination
        self._best_bid_dict = {}
        self._best_ask_dict = {}
        self._fair_buy_prices = {}
        self._fair_sell_prices = {}

        # Attributes to track server response
        self._order_count = 0
        self._waiting_for_order = False

        # Attributes to determine bot strategy
        self._start_time = datetime.datetime.now()
        self._time_elapsed = 0
        self._MM_PROPORTION = 0.2
        self._MARGIN_MAX = 50

    #-------

    def initialised(self):
        # Extract payoff distribution for each security
        for market_id, market_info in self.markets.items():
            security = market_info.item
            description = market_info.description
            self._payoffs[security] = [int(a) for a in description.split(",")]

            # Storing market ids in dictionary
            self._market_ids[market_info.item] = market_id
        
    #-------

    def get_potential_performance(self, orders):
        """
        Returns the portfolio performance if the given list of orders is executed.
        The performance as per the following formula:
        Performance = ExpectedPayoff - b * PayoffVariance, where b is the penalty for risk.
        :param orders: list of orders
        :return: performance as calculated by the formula as a float
        """
        performance = self._find_performance(self.holdings, orders)

        return performance

    #-------

    def is_portfolio_optimal(self, return_combo = False):
        """
        Returns true if the current holdings are optimal (as per the 
        performance formula) based on current best bids/asks. 
        Returns false and/or the best combo of bid/asks otherwise.
        Order combinations that improve performance but cannot be 
        traded on due to resource constraints do not contribute 
        towards non-optimality.
        """
        best_quotes = list(self._best_ask_dict.values()) + list(self._best_bid_dict.values())
        best_quotes = list(filter(None, best_quotes))

        holdings = self.holdings
        best_combo = None
        curr_performance = self._find_performance(holdings)

        # List comp for all combinations of best bid/asks
        all_combos = [list(combo) for r in range(len(best_quotes) + 1) for combo in combinations(best_quotes, r)]

        for combo in all_combos:

            # Orders the bot needs to submit to respond to combo            
            resp_orders = self._flip_oside(combo)

            # Check if the combo improves performance and holdings are
            # sufficient to respond
            prosp_performance = self._find_performance(holdings, resp_orders)
            if prosp_performance > curr_performance and self._can_react(resp_orders):
                best_combo = combo
                curr_performance = prosp_performance

        # Return boolean value and best combination of orders if required
        if best_combo is None and not return_combo:
            self.inform("Portfolio is currently optimal")
            return True
        elif best_combo != None and not return_combo:
            return False
        elif best_combo is None:
            self.inform("Portfolio is currently optimal")
            return True, best_combo
        else:
            return False, best_combo

    #-------

    def order_accepted(self, order):
        if order.order_type == OrderType.LIMIT:
            self.inform(f"Accepted limit order: {order.order_side} in {order.market.item} {order.units}@{order.price}")

        elif order.order_type == OrderType.CANCEL:
            self._waiting_for_order = False
            self._order_count -= 1

    #-------

    def order_rejected(self, info, order):
        price = order.price
        units = order.units
        market = order.market

        # Possible reasons for a rejected order
        if price > market.max_price or price < market.min_price:
            self.inform("Order rejected, price out of range")
        elif units > market.max_units or price < market.min_units:
            self.inform("Order rejected, units out of range")
        elif price % market.price_tick != 0:
            self.inform("Order rejected, price not divisible by price tick")
        elif units % market.unit_tick != 0:
            self.inform("Order rejected, units not divisible by unit tick")

        self._order_count -= 1

    #-------

    def received_orders(self, orders: List[Order]):
        # Track best bids/asks for reactive strategy, this is tracked
        # throughout the session in case is_portfolio optimal is called
        current_orders = [o for o in Order.all().values() if o.is_pending]

        for market_id, market_info in sorted(self.markets.items()):
            orders_in_market = []
            security = market_info.item
            for order in current_orders:
                if order.market.fm_id == market_id:
                    orders_in_market.append(order)

            self._best_bid_dict[security] = self._get_best_bid(orders_in_market)
            self._best_ask_dict[security] = self._get_best_ask(orders_in_market)     

        my_orders = [o for o in Order.current().values() if o.mine and o.is_pending]

        # Clear stale orders (based on order depth) when running reative strategy
        if self._reactive_condition and len(my_orders) != 0:
            for o in my_orders:
                if self._find_order_depth(o, current_orders) > 2 and \
                    not self._waiting_for_order:
                        cancel_order = self._make_cancel_order(o)
                        self.send_order(cancel_order)
                        self._waiting_for_order = True

        # Clear order book if there are unmet orders only when the MM strategy is running
        if self._mm_condition() and len(my_orders) != 0:
                self._clear_orders()

    #-------

    def received_session_info(self, session: Session):
        """
        Informs of changes in the marketplace
        """
        # Find the expected payoffs and covariance matrix 
        # for performance evaluation
        self._cov_matrix = self._find_cov_matrix(self._payoffs)
        self._exp_payoffs_matrix = self._find_exp_payoffs_matrix(self._payoffs)
        
        if session.is_open:
            self.inform("---")
            self.inform("Marketplace is now open")
            self.inform("---")
            self._start_time = datetime.datetime.now()

        elif session.is_paused:
            self.inform(f"Marketplace paused, time elapsed {self._find_time_elapsed()}")
        else:
            self.inform(f"Marketplace is now closed, time elapsed {self._find_time_elapsed()}")

    #-------

    def pre_start_tasks(self):
        # Check for strategy execution conditions periodically
        self.execute_periodically_conditionally(self._mm_strategy, 7, self._mm_condition)
        self.execute_periodically_conditionally(self._reactive_strategy, 5, self._reactive_condition)
        self.execute_periodically(self._unstuck_bot, 30)

    #-------

    def received_holdings(self, holdings):
        """
        Updates to necessary information whenever a trade is executed
        """
        # Informs of initial details 
        if self._find_time_elapsed() < 1/30: 
            # Trader ID
            self.inform(holdings.name)

            # Initial allocation of holdings
            self.inform('---')
            self.inform(f'Total cash: {holdings.cash}, Available cash: {holdings.cash_available}')
            for market, assets in (sorted(holdings.assets.items(), key = lambda x: x[0].item)):
                self.inform(f'{market.name} - Total units: {assets.units}, Available units: {assets.units_available}')

        #Update fair prices whenever holdings change
        for market_id, market_info in sorted(self.markets.items()):
            security = market_info.item
            self._fair_buy_prices[security] = self._find_fair_price(market_id, holdings, OrderSide.BUY)
            self._fair_sell_prices[security] = self._find_fair_price(market_id, holdings, OrderSide.SELL)
        
        # Trade notes for cash if cash is below $10.00 and there are 
        # sufficient notes. Taking a small decrease in performance.
        # Assumes the note market is the one with the highest market id
        note_id = sorted(self._market_ids.values())[-1]
        if holdings.cash < 1000 and holdings.assets[Market(note_id)].units \
            > 2 and self._order_count == 0:
            order = self._make_order(note_id, OrderSide.SELL, 495, units = 2)
            if self._check_holdings(order, holdings, True):
                self.send_order(order)
                self._order_count += 2

    #------- Helper Functions -------

    #------- Functions for MM trading strategy -------  

    def _mm_condition(self):
        """
        Returns True when the MM strategy is to be executed
        """
        time = self._find_time_elapsed()

        # Do not run on the first instance as variables are yet to be initiated
        if time < 2/30:
            return False
        
        # Run MM strategy in the first MM_PROPORTION of the session time.
        if time < self._MM_PROPORTION * self._session_time and self._order_count == 0:
            return True
        elif time < self._MM_PROPORTION * self._session_time and self._order_count != 0:
            self._clear_orders()
            return False
        else:
            return False

    def _mm_strategy(self):
        """
        Implementation of the MM strategy
        """
        self.inform("---")
        self.inform("MM Strategy")
        self.inform("---")

        margin = self._find_margin(self._find_time_elapsed())        
        
        # Informs of details before an interation is executed
        self.inform(f"Time elapsed {round(self._find_time_elapsed(), 2)} minutes")
        self.inform(f"Margin: {margin}")

        if self.holdings != None:
            self._find_performance(self.holdings, inform = True)

        # Sending buy orders, if fair price > 0, buying at < fair price will improve performance
        # if fair price < 0, even buying at 0 will decrease performance.
        # Quotes are fair price - margin
        for asset, prices in self._fair_buy_prices.items():
            if to_cents(prices) > margin:
                new_order = self._make_mm_order(asset, to_cents(prices), margin, OrderSide.BUY)

                if self._check_holdings(new_order, self.holdings, True):
                    self.send_order(new_order)
                    self._order_count += 1
                    time.sleep(0.3)
        
        # Sending sell orders, if fair price < 0, selling at > -fair price will improve performance
        # if fair price > 0, selling at 0 will increase performance.
        # Quotes are -fair price + margin
        for asset, prices in self._fair_sell_prices.items():

            if to_cents(prices) > 0:
                new_order = self._make_mm_order(asset, 0, margin, OrderSide.SELL)

                if self._check_holdings(new_order, self.holdings, True):
                        self.send_order(new_order)
                        self._order_count += 1
                        time.sleep(0.3)

            elif to_cents(-prices) > margin:
                prices = to_cents(-prices)
                new_order = self._make_mm_order(asset, prices, margin, OrderSide.SELL)
                
                if self._check_holdings(new_order, self.holdings, True):
                        self.send_order(new_order)
                        self._order_count += 1
                        time.sleep(0.3)



    def _find_fair_price(self, market_id, holdings, oside, inform = False):
        """
        Computes the fair price of an asset in a market based on current 
        holdings and order side. The fair price is the change in performance
        if an order of zero price is traded.
        """
        pre_performance = self._find_performance(holdings)
        order = [self._make_order(market_id, oside, 0)]
        post_performance = self._find_performance(holdings, order)
        delta = post_performance - pre_performance

        if inform:
            self.inform(f"{Market(market_id).item} - {oside}, Fair price: {delta}")
        return delta  

    def _find_margin(self, time_elapsed):
        """
        Computes a margin above/below the fair price to trade at when the bot
        is acting as a market maker. The function is based on a logistic curve
        """
        time_max = self._session_time * self._MM_PROPORTION
        b = time_max / 2
        margin_denom = 1 + np.exp(1/np.sqrt(b)*(time_elapsed - b))
        margin = np.ceil(self._MARGIN_MAX / margin_denom)
        return margin

    def _make_mm_order(self, assets, prices, margin, oside):
        """
        Helper function for MM strategy
        """
        market_id = self._market_ids[assets]
        tick = self.markets[market_id].price_tick

        # Setting price rounded based on order side and price tick
        if oside == OrderSide.BUY:
            oprice = int(np.floor((prices - margin) / tick) * tick)
        else:
            oprice = int(np.ceil((prices + margin) / tick) * tick)

        new_order = self._make_order(market_id, oside, oprice)
        return new_order

    #------- Functions for reactive trading strategy -------  

    def _reactive_condition(self):
        """
        Returns True when the reactive strategy is to be executed
        """
        self._order_count = len([o for o in Order.current().values() if o.mine])
        time = self._find_time_elapsed()

        # Do not run on the first instance as variables are yet to be initiated
        if time < 2/30:
            return False
        
        # Run reactive strategy in the latter 1 - MM_PROPORTION of the session time.
        if time > self._MM_PROPORTION * self._session_time and self._order_count == 0:
            return True
        elif self._order_count != 0:
            return False
        else:
            return False

    def _reactive_strategy(self):
        self.inform("---")
        self.inform("Reactive Strategy")
        self.inform("---")

        self.inform(f"Time elapsed {round(self._find_time_elapsed(), 2)} minutes")

        if self.holdings != None:
            self._find_performance(self.holdings, inform = True)

        # Checks if current holdings are optimal and the best combination
        # of best bid/asks to respond to if not optimal.
        is_optimal, best_combo = self.is_portfolio_optimal(return_combo=True)

        # Act on the best combination of bid/asks.
        if not is_optimal:
            resp_orders = self._flip_oside(best_combo)
            for o in resp_orders:
                new_order = self._make_order(o.market.fm_id, o.order_side, o.price, units = o.units)
                self.send_order(new_order)
                self._order_count += 1

    def _can_react(self, order_list):
        """
        Helper function to determine if holdings are sufficient to react
        to a list of orders. Used in the is_portfolio_optimal function
        """
        cash_req = 0
        holdings = self.holdings
        asset_req_dict = {}

        # Cash available must be greater than the sum of the prices of the
        # buy orders
        for o in order_list:
            if o.order_side == OrderSide.BUY:
                cash_req += o.price
        
        if cash_req > holdings.cash_available:
            return False

        # Assets available in each market must be greater than the sum of 
        # the units of sell orders 
        for market in holdings.assets:
            asset_req_dict[market.item] = 0

        for o in order_list:
            if o.order_side == OrderSide.SELL:
                asset_req_dict[o.market.item] += 1

        for market, assets in holdings.assets.items():
            if asset_req_dict[market.item] > assets.units_available:
                return False

        # If the function is yet to terminate, then holdings are sufficient
        return True
    
    #------- Functions for order management -------

    def _find_order_depth(self, order, curr_orders):
        """
        Helper function to find how deep an order is within a market.
        Used to determine stale reactive orders.
        """
        market = order.market.fm_id
        price = order.price
        order_depth = 0

        # All current orders in the same market as the order.
        orders_in_market = [o for o in curr_orders if not o.mine \
            and o.market.fm_id == market]
        
        # Determining how many orders are to be executed before the order 
        # under consideration
        if order.order_side == OrderSide.BUY:
            shallow_orders = [o for o in orders_in_market if o.price > price]
            for o in shallow_orders:
                order_depth += o.units
        else:
            shallow_orders = [o for o in orders_in_market if o.price < price]
            for o in shallow_orders:
                order_depth += o.units

        return order_depth

    def _flip_oside(self, order_list):
        """
        Flips the order side of a list of orders
        """
        resp_orders = []

        for o in order_list:
            oside = OrderSide.BUY if o.order_side == OrderSide.SELL else OrderSide.SELL
            resp_orders.append(self._make_order(o.market.fm_id, oside, o.price))
        
        return resp_orders

    def _get_best_bid(self, active_orders, inform = False):
        """
        Obtain best bid from current pending orders
        if inform = True, print best bid in console
        """
        #list of all pending, public buy orders and buy orders that are not mine
        active_orders = [o for o in active_orders if \
            o.is_pending and not o.mine ]

        buy_orders = []
        for order in active_orders:
            if order.order_side == OrderSide.BUY:
                buy_orders.append(order)

        #sort list based on price, highest to lowest
        buy_orders = sorted(buy_orders, key=lambda x: x.price, reverse=True)
        best_bid = buy_orders[0] if len(buy_orders) > 0 else None
        if inform:
            self.inform(f"Best Bid: {best_bid}")
        return(best_bid)

    def _get_best_ask(self, active_orders, inform = False):
        """
        Obtain best ask from current pending orders
        if inform = True, print best ask in console
        """

        #list of all pending, public sell orders and sell orders that are not mine
        active_orders = [o for o in active_orders if \
            o.is_pending and not o.mine ]

        sell_orders = []
        for order in active_orders:
            if order.order_side == OrderSide.SELL:
                sell_orders.append(order)

        #sort list based on price, lowest to highest
        sell_orders = sorted(sell_orders, key=lambda x: x.price)
        best_ask = sell_orders[0] if len(sell_orders) > 0 else None
        if inform:
            self.inform(f"Best Ask: {best_ask}")
        return(best_ask)

    def _clear_orders(self):
        """
        Clears all pending orders in the order book that are mine
        """
        all_orders = Order.all().values()
        my_orders = [o for o in all_orders if o.mine and o.is_pending]
        
        self._order_count = len(my_orders)

        if self._order_count != 0:
            for order in my_orders:
                if not self._waiting_for_order:
                    cancel_order = self._make_cancel_order(order)
                    self.send_order(cancel_order)
                    self._waiting_for_order = True


    def _make_cancel_order(self, order):
        """
        Makes and returns a cancel order for the parameter 'order'
        """
        cancel_order = copy.copy(order)
        cancel_order.order_type = OrderType.CANCEL
        return cancel_order

    def _find_time_elapsed(self):
        """
        Returns the number of minutes (as float) the session has been open 
        """
        time_now = datetime.datetime.now()
        time_delta = time_now - self._start_time 
        time_elapsed = time_delta.total_seconds() / 60

        return time_elapsed

    def _make_order(self, market_id, oside, price, otype = OrderType.LIMIT, \
        units = 1):
        """
        Makes and returns Order object with the defined parameters
        """
        new_order = Order.create_new()
        new_order.mine = True
        new_order.market = Market(market_id)
        new_order.order_side = oside
        new_order.order_type = otype
        new_order.price = price
        new_order.units = units
        new_order.ref = f'{otype} Order in {new_order.market.fm_id} \
             for {new_order.units}@{new_order.price}'
        return new_order

    def _check_holdings(self, prosp_order, holdings, inform=False):
        """
        Checks holdings to see if a prospective order can be made
        """
        price = prosp_order.price
        units = prosp_order.units
        oside = prosp_order.order_side
        market = prosp_order.market
        cash_available = holdings.cash_available
        assets_available = holdings.assets[market].units_available
        
        #If buy order, and available cash is < units, then it can't trade
        if oside == OrderSide.BUY and cash_available < price * units:
            if inform:
                self.inform("Not enough cash to meet prospective buy order")
            return(False)
        #If sell order, and available assets is < price * units, then it 
        #can't trade
        elif oside == OrderSide.SELL and assets_available < units:
            if inform:
                self.inform("Not enough assets to meet prospective sell order")
            return(False)
        else:
            return(True)

    def _unstuck_bot(self):
        """
        At times the bot is unable to cancel orders as a cancel order
        is accepted by the server after a new cycle of a strategy is
        run. This function is run periodically to unstuck the bot in 
        these instances
        """
        if self._waiting_for_order:
            self._waiting_for_order = False
            self.inform("Bot unstucked")


    #------- Functions for portfolio performance evaluation -------
    
    def _adj_cash(self, cash, orders):
        """
        Takes a cash figure and adjusts it based on a list of orders.
        Helper functions of _find_performance
        """
        for order in orders:
            if order.order_side == OrderSide.BUY:
                cash -= to_dollar(order.price)
            else:
                cash += to_dollar(order.price)
        return cash

    
    def _adj_holdings(self, holdings, orders):
        """
        Takes an array of holdings and adjusts it based on a list of orders.
        Helper functions of _find_performance
        """
        adjustment = np.zeros(len(holdings))
        markets = sorted(self._market_ids.keys())
        for order in orders:
            index = markets.index(order.market.item)
            if order.order_side == OrderSide.BUY:
                adjustment[index] += 1
            else:
                adjustment[index] -= 1

        holdings = holdings + adjustment
        return holdings

    def _find_performance(self, holdings, prosp_orders = False, inform = False):
        """
        Computes the performance of the portfolio based on holdings, If there 
        are prospective orders to be evaluated calculate performance as if 
        these orders have been traded
        """
        unit_holdings = []
        cash_holdings = to_dollar(holdings.cash)

        #Creating an array of holdings of each unit
        for assets in (sorted(holdings.assets.values(), key = lambda x: x.market.item)):
            unit_holdings.append(assets.units)
        unit_holdings = np.array(unit_holdings)
        
        #If there are prospective orders to be evaluated, adjust cash and holdings
        if prosp_orders:
            cash_holdings = self._adj_cash(cash_holdings, prosp_orders)
            unit_holdings = self._adj_holdings(unit_holdings, prosp_orders)

        #Calculation of performance
        payoff_exp = np.dot(unit_holdings.T, self._exp_payoffs_matrix) + cash_holdings
        payoff_var = np.dot(unit_holdings.T, np.dot(self._cov_matrix, unit_holdings))

        performance = payoff_exp - self._risk_penalty * payoff_var

        if inform and prosp_orders:
            self.inform(f"Prospective performance: {performance}")
        elif inform:
            self.inform(f"Performance: {performance}")

        return performance

    def _find_exp_payoffs_matrix(self, payoffs):
        """
        Creating an array of expected payoffs of assets
        """
        asset_payoffs = []
        payoffs = sorted(payoffs.items())
        for payoff in payoffs:
            payoff_dist = payoff[1]
            payoff_dist = [to_dollar(num) for num in payoff_dist]
            asset_payoffs.append(self._find_exp(payoff_dist))

        asset_payoffs = np.array(asset_payoffs)
        return asset_payoffs

    def _find_cov_matrix(self, payoffs):
        """
        Computes the covariance matrix of payoffs given the payoff dictionary
        """
        payoffs = sorted(payoffs.items())
        payoffs = [[to_dollar(num) for num in payoff[1]] for payoff in payoffs]
        cov_matrix = np.cov(payoffs, bias = True)
        return cov_matrix

    def _find_exp(self, payoff):
        """
        Computes the expected value of an input with the payoff as a list.
        """
        return (1/len(payoff)) * sum(payoff)

#------- Global functions -------
def to_dollar(cents):
    return cents / 100

def to_cents(dollar):
    return dollar * 100

if __name__ == "__main__":
    FM_ACCOUNT = "account-name"
    FM_EMAIL = "email"
    FM_PASSWORD = "student-id"
    MARKETPLACE_ID = 1017

    bot = CAPMBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, MARKETPLACE_ID)
    bot.run()
