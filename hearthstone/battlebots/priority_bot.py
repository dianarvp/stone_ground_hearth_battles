import typing
from typing import List
from hearthstone.agent import generate_valid_actions, BuyAction, EndPhaseAction, SummonAction, SellFromBoardAction
from hearthstone.agent import  TavernUpgradeAction, RerollAction
from hearthstone.battlebots.bot_types import PriorityFunctionBot

from hearthstone.player import Player, StoreIndex

if typing.TYPE_CHECKING:
    from hearthstone.agent import Action
    from hearthstone.cards import Card


class PriorityBot(PriorityFunctionBot):
    def rearrange_cards(self, player: 'Player') -> List['Card']:
        card_list = player.in_play.copy()
        self.local_random.shuffle(card_list)
        return card_list

    def buy_phase_action(self, player: 'Player') -> 'Action':
        all_actions = list(generate_valid_actions(player))

        if player.tavern_tier < 2:
            upgrade_action = TavernUpgradeAction()
            if upgrade_action.valid(player):
                return upgrade_action

        top_hand_priority = max([self.priority(player, card) for card in player.hand], default=None)
        top_store_priority = max([self.priority(player, card) for card in player.store], default=None)
        bottom_board_priority = min([self.priority(player, card) for card in player.in_play], default=None)

        if top_hand_priority:
            if player.room_on_board():
                return [action for action in all_actions if type(action) is SummonAction and self.priority(player, player.hand[action.index]) == top_hand_priority][0]
            else:
                if top_hand_priority > bottom_board_priority:
                    return [action for action in all_actions if type(action) is SellFromBoardAction and self.priority(player, player.in_play[action.index]) == bottom_board_priority][0]

        if top_store_priority:
            if player.room_on_board() or bottom_board_priority < top_store_priority:
                buy_action = BuyAction([StoreIndex(i) for i, card in enumerate(player.store) if self.priority(player, card) == top_store_priority][0])
                if buy_action.valid(player):
                    return buy_action

        reroll_action = RerollAction()
        if reroll_action.valid(player):
            return reroll_action

        return EndPhaseAction(False)

    def discover_choice_action(self, player: 'Player') -> 'Card':
        discover_cards = player.discovered_cards
        discover_cards = sorted(discover_cards, key=lambda card: self.priority(player, card), reverse=True)
        return discover_cards[0]
