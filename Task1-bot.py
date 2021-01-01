import copy, time

from enum import Enum
from fmclient import Agent, OrderSide, Order, OrderType, Session, Market
from typing import List

# Student details
SUBMISSION = {"number": "student number", "name": "name"}

# ------ Add a variable called PROFIT_MARGIN -----
PROFIT_MARGIN = 20

# Enum for the roles of the bot
class Role(Enum):
    BUYER = 0
    SELLER = 1


# Let us define another enumeration to deal with the type of bot
class BotType(Enum):
    MARKET_MAKER = 0
    REACTIVE = 1


#---------------------------------------------------

class DSBot(Agent):
    # ------ Add an extra argument bot_type to the constructor -----
    def __init__(self, account, email, password, marketplace_id, bot_type):
        super().__init__(account, email, password, marketplace_id, name="DSBot")
        self._public_market_id = 0
        self._private_market_id = 0
        self._role = None 
        self._waiting_for_order = False
        self._standing_priv_order = None

        # ------ Add new class variable _bot_type to store the type of the bot
        self._bot_type = BotType(bot_type)

#---------------------------------------------------

    def role(self, order):
        """
        Updates bot's role based on the order side of a private 'order'
        """
        if order.order_side == OrderSide.BUY:
            self._role = Role(0)
        else:
            self._role = Role(1)
        return self._role

#---------------------------------------------------

    def pre_start_tasks(self):
        pass

#---------------------------------------------------

    def initialised(self):
        """
        Initial operations - getting public and private market IDs
        """
        for market in self.markets.values():
            if market.private_market:
                self._private_market_id = market.fm_id
            else:
                self._public_market_id = market.fm_id
        
#---------------------------------------------------

    def order_accepted(self, order: Order):

        self.inform(f"{order.order_side} for {order.units}@{order.price} was accepted")
        self._waiting_for_order = False

        all_orders = list(Order.all().values())
        my_current_orders = [o for o in all_orders if o.is_pending and o.mine]

        #allow market maker orders to remain pending for 2 seconds
        if self._bot_type == BotType.MARKET_MAKER and not order.is_private:
            time.sleep(2)
        
        #if there is still a pending order, cancel it.
        if order in my_current_orders:
            cancel_order = self.make_cancel_order(order)
            self.send_order(cancel_order)
            self._waiting_for_order = True
        else:
        #otherwise, act in the private market to arbitrage
            if not order.is_private and order.order_type != OrderType.CANCEL:
                if self._role == Role.BUYER:
                    oside = OrderSide.SELL
                else: 
                    oside = OrderSide.BUY

                match_order = self.make_order(self._private_market_id, oside, \
                self._standing_priv_order.price)
                self.send_order(match_order)
                self._waiting_for_order = True
            
#---------------------------------------------------

    def order_rejected(self, info, order: Order):

        price = order.price
        units = order.units

        if order.is_private:
            market = Market(self._private_market_id)
        else:
            market = Market(self._public_market_id)

        #possible reasons for a rejected order
        if price > market.max_price or price < market.min_price:
            self.inform("Order rejected, price out of range")
        elif units > market.max_units or price < market.min_units:
            self.inform("Order rejected, units out of range")
        elif price % market.price_tick != 0:
            self.inform("Order rejected, price not divisible by price tick")
        elif units % market.unit_tick != 0:
            self.inform("Order rejected, units not divisible by unit tick")

        self._waiting_for_order = False

 #---------------------------------------------------
       
    def received_orders(self, orders: List[Order]):

        all_orders = list(Order.all().values())
        current_orders = [order for order in all_orders if order.is_pending]

        #update best bid/ask if a cancel order is detected
        for order in orders:
            if order.order_type == OrderType.CANCEL:
                #self.inform('Cancel order')

                best_bid = self._get_best_bid(all_orders)
                best_ask = self._get_best_ask(all_orders)
                return

        #update standing private order and role 
        priv_orders = [order for order in all_orders if order.is_private and \
            order.is_pending]
        if priv_orders:
            self._standing_priv_order = priv_orders[0]
            #self.inform("private order updated")
            self.role(self._standing_priv_order)
        else:
            self._standing_priv_order = None
            #self.inform("no private order, do not trade")
            return
        
        #Obtaining best bid/ask
        best_bid = self._get_best_bid(current_orders)
        best_ask = self._get_best_ask(current_orders)
        
        if best_bid == None and best_ask == None:
            self.inform("No best ask and best bid, do nothing")
            return
        elif self._role == Role.SELLER and best_bid == None:
            self.inform("Is seller, no best bid, do nothing")
            return
        elif self._role ==  Role.BUYER and best_ask == None:
            self.inform("Is buyer, no best ask, do nothing")
            return
        
        best_bid_price = best_bid.price if best_bid else None
        best_ask_price = best_ask.price if best_ask else None

        priv_price = self._standing_priv_order.price

        #Printing possible trade opportunities, not necessarily acting on them
        if (self._role == Role.SELLER and best_bid_price > priv_price + \
            PROFIT_MARGIN) or (self._role == Role.BUYER and best_ask_price  < \
            priv_price - PROFIT_MARGIN):
            self._print_trade_opportunity(best_ask)

        #Pending order check
        if not self._check_existing_order(current_orders):
            my_order = [order for order in current_orders if order.mine]
            if len(my_order) > 0:
                self.inform(f"Cannot trade, {len(my_order)} pending order(s)")
                return
            else:
                return


        #Market Maker strategy
        if self._bot_type == BotType.MARKET_MAKER:

            #if seller and no pending orders
            if self._role == Role.SELLER and not self._waiting_for_order and \
                 self._check_existing_order(current_orders):
                
                #send sell order at standing private price + profit margin
                new_order = self.make_order(self._public_market_id, \
                    OrderSide.SELL, priv_price + PROFIT_MARGIN)

                if self._check_holdings(new_order, self.holdings):
                    self.send_order(new_order)
                    self._waiting_for_order = True

            #if buyer and no pending order
            elif self._role == Role.BUYER and not self._waiting_for_order and \
                 self._check_existing_order(current_orders):
                new_order = self.make_order(self._public_market_id, \
                    OrderSide.BUY, priv_price - PROFIT_MARGIN)

                #send by order at standing private price - profit margin
                if self._check_holdings(new_order, self.holdings):
                    self.send_order(new_order)
                    self._waiting_for_order = True

        if best_ask_price == None or best_bid_price == None:
            return

        #Reactive strategy 
        if self._bot_type == BotType.REACTIVE:
            if self._role == Role.SELLER and best_bid_price > priv_price + \
                PROFIT_MARGIN:
                #send sell order at best bid in public market 
                #if best bid is a profitable opportunity
                
                if not self._waiting_for_order and \
                    self._check_existing_order(current_orders):
                    new_order = self.make_order(self._public_market_id, \
                        OrderSide.SELL, best_bid_price)
                    
                    if self._check_holdings(new_order, self.holdings):
                        self.send_order(new_order)
                        self._waiting_for_order = True

            elif self._role == Role.BUYER and best_ask_price < priv_price - \
                 PROFIT_MARGIN:
                #send buy order at best ask in public market 
                #if best ask is a profitable opportunity

                if not self._waiting_for_order and self._check_existing_order(current_orders):
                    new_order = self.make_order(self._public_market_id, \
                    OrderSide.BUY, best_ask_price)
                    
                    if self._check_holdings(new_order, self.holdings):
                        self.send_order(new_order)
                        self._waiting_for_order = True
            else:
                self.inform("No profitable trading opportunities")

#---------------------------------------------------

    def _print_trade_opportunity(self, other_order):
        self.inform(f"{self._role} - Profitable order: {other_order.units}@{other_order.price}, Standing Private order: {self._standing_priv_order.units}@{self._standing_priv_order.price}")

#---------------------------------------------------

    def received_completed_orders(self, orders, market_id=None):
        """
        Not sure what this does, deprecated method?
        """
        pass

#---------------------------------------------------

    def received_holdings(self, holdings):
        """
        Track holdings whenever a trade is executed
        """
        self.inform(self.holdings.name)

        #Informing total cash and available cash
        self.inform('---')
        self.inform(f'Total cash: {holdings.cash}, Available cash: {holdings.cash_available}')

        asset_holdings = holdings.assets

        #Informing of updates in assets
        for market, assets in asset_holdings.items():
            self.inform(f'{market.item} - Total units: {assets.units}, Available units: {assets.units_available}')

        self.inform('---')

#---------------------------------------------------

    def received_session_info(self, session: Session):
        """
        Informs of changes in the marketplace
        """
        
        if session.is_open:
            self.inform("---")
            self.inform("Marketplace is now open")
            self.inform(f"Public Market ID: {self._public_market_id}")
            self.inform(f"Private Market ID: {self._private_market_id}")
            self.inform(f"This is a {self._bot_type.name} bot")
            self.inform("---")
        elif session.is_paused:
            self.inform("Marketplace paused")
        else:
            self.inform("Markplace is now closed")

        
# ------ Helper Functions -------

    def _get_best_bid(self, active_orders, inform = False):
        """
        Obtain best bid from current pending orders
        if inform = True, print best bid in console
        """
        #list of all pending, public buy orders and buy orders that are not mine
        active_orders = [order for order in active_orders if order.is_pending \
            and not order.mine and not order.is_private]

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
        active_orders = [order for order in active_orders if order.is_pending \
            and not order.mine and not order.is_private]

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

    def _check_holdings(self, prosp_order, holdings):
        """
        Checks holdings to see if a prospective order can be made
        """
        price = prosp_order.price
        units = prosp_order.units
        oside = prosp_order.order_side
        market = prosp_order.market
        cash_available = holdings.cash_available
        assets_available = holdings.assets[market].units_available
        
        #If buy order, and available holdings is < units, then it can't trade
        if oside == OrderSide.BUY and cash_available < price * units:
            self.inform("Not enough cash to meet prospective order")
            return(False)
        #If sell order, and available cash is < price * units, then it 
        #can't trade
        elif oside == OrderSide.SELL and assets_available < units:
            self.inform("Not enough sell to meet prospective order")
            return(False)
        else:
            self.inform("Prospective order can be met")
            return(True)


    def _check_existing_order(self, current_orders):
        """
        Checks for pending orders, if there are not, returns true. 
        Otherwise returns false.
        """
        my_orders = [order for order in current_orders if order.mine]

        return not my_orders

    
    def make_order(self, market_id, oside, price, otype = OrderType.LIMIT, \
        units = 1):
        """
        Makes and returns Order object with the defined parameters
        """
        new_order = Order.create_new()
        new_order.market = Market(market_id)
        new_order.order_side = oside
        new_order.order_type = otype
        new_order.price = price
        new_order.units = units
        new_order.ref = f'{otype} Order in {new_order.market} \
             for {new_order.units}@{new_order.price}'
        if market_id == self._private_market_id:
            new_order.owner_or_target = "M000"
        return new_order
    
    def make_cancel_order(self, order):
        """
        Makes and returns a cancel order for the parameter 'order'
        """
        cancel_order = copy.copy(order)
        cancel_order.order_type = OrderType.CANCEL
        return cancel_order
    
if __name__ == "__main__":
    FM_ACCOUNT = "account-name"
    FM_EMAIL = "email"
    FM_PASSWORD = "student-id"
    MARKETPLACE_ID = 915
    BOT_TYPE = 0

    kanga_market = DSBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, MARKETPLACE_ID, BOT_TYPE)
    kanga_market.run()
