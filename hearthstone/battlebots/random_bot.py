import random
import typing
from typing import List

from hearthstone.agent import Agent, generate_valid_actions, Action
if typing.TYPE_CHECKING:
    from hearthstone.cards import Card
    from hearthstone.player import Player


class RandomBot(Agent):
    authors = ["Jeremy Salwen"]
    def __init__(self, seed: int):
        self.local_random = random.Random(seed)

    def rearrange_cards(self, player: 'Player') -> List['Card']:
        card_list = player.in_play.copy()
        self.local_random.shuffle(card_list)
        return card_list

    def buy_phase_action(self, player: 'Player') -> Action:
        all_actions = list(generate_valid_actions(player))
        return self.local_random.choice(all_actions)

    def discover_choice_action(self, player: 'Player') -> 'Card':
        return self.local_random.choice(player.discovered_cards)
